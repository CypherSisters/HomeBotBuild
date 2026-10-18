"""Release hardening: four unrelated invariants, each with its evidence.

They share one property: each is a place where a value that should live with
the install's STATE, or a statement that should be true, could drift into the
code tree or into a literal.

* the Home Assistant token is never written into the git checkout;
* ``build_manifest`` strips secrets by shape, not by exact key name, so
  ``wifi_password`` does not survive;
* the dashboard binds loopback by default, not every interface;
* the startup line counts the agents rather than naming a number.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Never a real credential. Shaped so a substring search cannot miss it.
FAKE_HA_TOKEN = "eyJfake_ha_token_for_a_test_only_7b3d1a"
FAKE_WIFI = "not-a-real-wifi-password-91c4e7"


class TestTheHaTokenStaysOutOfTheCheckout:
    def test_config_dir_follows_the_data_dir_for_a_relocated_install(
            self, tmp_path, monkeypatch):
        from puffpai import config as config_mod
        monkeypatch.delenv(config_mod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        resolved = config_mod._config_dir_for(str(REPO))
        assert resolved == os.path.normpath(str(tmp_path / "config"))

    def test_the_relocated_dir_is_seeded_from_the_shipped_config(
            self, tmp_path, monkeypatch):
        """A fresh install needs a writable copy of the shipped templates, or
        every value reads as a default the owner cannot edit."""
        from puffpai import config as config_mod
        monkeypatch.delenv(config_mod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        resolved = Path(config_mod._config_dir_for(str(REPO)))
        assert (resolved / "puffpai_config.json").is_file()

    def test_seeding_never_overwrites_an_edited_file(self, tmp_path,
                                                    monkeypatch):
        from puffpai import config as config_mod
        monkeypatch.delenv(config_mod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        target = tmp_path / "config"
        target.mkdir()
        (target / "puffpai_config.json").write_text('{"mine": true}',
                                                    encoding="utf-8")
        config_mod._config_dir_for(str(REPO))
        assert json.loads(
            (target / "puffpai_config.json").read_text(encoding="utf-8")
        ) == {"mine": True}

    def test_a_dev_checkout_with_in_tree_state_keeps_the_old_behaviour(
            self, tmp_path, monkeypatch):
        """The same rule resolve_state_dir uses, so config and state cannot
        disagree about which install this is."""
        from puffpai import config as config_mod
        monkeypatch.delenv(config_mod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        monkeypatch.setattr(config_mod, "_dev_checkout_has_state",
                            lambda root: True)
        fake_repo = tmp_path / "checkout"
        (fake_repo / "config").mkdir(parents=True)
        assert config_mod._config_dir_for(str(fake_repo)) == str(
            fake_repo / "config")

    def test_a_fresh_install_writes_the_ha_token_under_the_data_dir(
            self, tmp_path, monkeypatch):
        from puffpai import config as config_mod, setup_ops
        monkeypatch.delenv(config_mod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        config_mod.get_config(_force_reload=True)
        try:
            # "island" is the posture that permits a LAN Home Assistant; the
            # default "sealed" refuses one before the token touches disk, which
            # is a separate control and not what this test is about.
            path = setup_ops.set_ha_config("http://192.168.1.50:8123",
                                           FAKE_HA_TOKEN, posture="island")
            written = Path(path)
            assert written.is_file()
            # Under the data dir, NOT in the checkout.
            assert str(tmp_path) in str(written.resolve())
            assert str(REPO / "config") not in str(written.resolve())
            assert FAKE_HA_TOKEN in written.read_text(encoding="utf-8")
        finally:
            config_mod.get_config(_force_reload=True)

    def test_the_checkout_holds_no_home_assistant_config(self):
        """The file an in-checkout default would create. It is in .gitignore,
        so it is never one `git add -A` from being committed — but a live
        household credential sitting inside the code tree is still wrong: it is
        what gets copied when the tree is archived, packaged or synced, and
        `git add -f` or a re-generated .gitignore removes the only thing
        protecting it."""
        assert not (REPO / "config" / "home_assistant_config.json").exists()

    def test_git_status_stays_clean_after_a_fresh_install_writes_ha(
            self, tmp_path, monkeypatch):
        from puffpai import config as config_mod, setup_ops
        before = subprocess.run(
            ["git", "status", "--porcelain", "--", "config"],
            cwd=REPO, capture_output=True, text=True).stdout
        monkeypatch.delenv(config_mod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        config_mod.get_config(_force_reload=True)
        try:
            setup_ops.set_ha_config("http://192.168.1.50:8123", FAKE_HA_TOKEN,
                                    posture="island")
        finally:
            config_mod.get_config(_force_reload=True)
        after = subprocess.run(
            ["git", "status", "--porcelain", "--", "config"],
            cwd=REPO, capture_output=True, text=True).stdout
        assert after == before, (
            "writing the HA token changed the checkout:\n" + after)


class TestTheManifestStripsSecretsByShape:
    """Asserted against sanitize_manifest directly, on a manifest shaped like
    one read back off disk or imported from a bundle — which is where an
    unexpected key really arrives. assemble_manifest only ever emits the fields
    it knows, so going through it would test the wrong function."""

    def _sanitize(self, block):
        from puffpai import build_manifest
        manifest = {"schema": build_manifest.SCHEMA, "build": dict(block),
                    "nested": {"deep": [{"inner": dict(block)}]}}
        return json.dumps(build_manifest.sanitize_manifest(manifest))

    @pytest.mark.parametrize("key", [
        "wifi_password",          # the one that actually survived
        "mqtt_password", "smtp_passwd", "spotify_token", "refresh_token",
        "zigbee_network_key", "owner_pin", "client_secret",
        "password", "passwd", "pin", "token", "secret", "key",
        "WIFI_PASSWORD",          # case must not be a bypass
    ])
    def test_a_secret_shaped_key_is_dropped(self, key):
        text = self._sanitize({"name": "Bot", key: FAKE_WIFI})
        assert FAKE_WIFI not in text, key
        assert key not in text, key

    @pytest.mark.parametrize("key", [
        "pin_length", "password_policy", "token_budget", "keyboard",
        "monkey", "spoken",
    ])
    def test_an_innocent_name_survives(self, key):
        """A false positive costs the owner a field in their own record, so the
        rule matches suffixes and never prefixes or substrings."""
        text = self._sanitize({"name": "Bot", key: "kept"})
        assert key in text, key

    def test_the_validator_reports_the_same_keys_it_strips(self):
        from puffpai import build_manifest
        manifest = {"schema": build_manifest.SCHEMA,
                    "build": {"name": "Bot", "wifi_password": FAKE_WIFI}}
        warnings = build_manifest.validate_manifest(manifest)
        assert any("wifi_password" in w for w in warnings), warnings
        # The warning names the KEY and never the value.
        assert all(FAKE_WIFI not in w for w in warnings)


class TestTheDashboardBindsLoopbackByDefault:
    def _scripts(self):
        data = json.loads(
            (REPO / "dashboard" / "package.json").read_text(encoding="utf-8"))
        return data["scripts"]

    def test_start_binds_loopback(self):
        """This build ships no host packet filter, and the dashboard proxy
        holds the appliance's API key — so `npm start` on every interface hands
        the household's backend to the LAN by default."""
        assert "-H 127.0.0.1" in self._scripts()["start"]
        assert "0.0.0.0" not in self._scripts()["start"]

    def test_the_lan_bind_is_an_explicit_opt_in(self):
        assert "0.0.0.0" in self._scripts()["start:lan"]


def test_the_agent_team_line_counts_the_roster():
    """The line counts the roster instead of saying "six agents"."""
    from puffpai.agents.registry import ROSTER
    text = (REPO / "src" / "puffpai" / "api_server.py").read_text(
        encoding="utf-8")
    assert "six agents + bus" not in text
    assert "len(ROSTER)" in text
    assert len(ROSTER) == 7, "roster changed; the LINE is now the only fix"


def test_the_setup_cli_start_command_is_platform_correct():
    """`py -3` is the Windows launcher and exists nowhere else."""
    import sys
    from puffpai import setup_cli
    text = (REPO / "src" / "puffpai" / "setup_cli.py").read_text(
        encoding="utf-8")
    assert 'print("  Start it with:  py -3 start_api.py")' not in text
    assert 'sys.platform.startswith("win")' in text
    assert setup_cli.sys is sys


def test_the_onboarding_placeholders_are_neutral():
    """Placeholders are shipped copy: they must not name a real person or
    hard-code one deployment's paths."""
    text = (REPO / "dashboard" / "app" / "onboarding" /
            "OnboardingWizard.tsx").read_text(encoding="utf-8")
    owner = re.search(r'id="ob-owner"[^>]*placeholder="([^"]*)"', text)
    # HomeBot's setup asks no names (the household is set later on its own
    # page); where a name is asked, its example is the neutral "Alex".
    assert owner is None or owner.group(1) == "Alex"
    assert '/data/puffpai/knowledge' not in text
