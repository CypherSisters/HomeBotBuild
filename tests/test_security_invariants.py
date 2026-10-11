"""Small security invariants: the wizard's HA write, the pending-apply kind,
the bind warning, test isolation, and the recovery-code format.

Five small rows, each one a thing that must not be true of this build. Fake
token material only; nothing here opens a socket.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

FAKE_TOKEN = "eyJ0ZXN0IjoiZmFrZXBlcnNvbiJ9.not-a-real-token.Test-Fakeperson"


# ── 1. the wizard writes the HA credential through the one source module ────

class TestWizardHaWriteGoesThroughTheSource:
    """``setup_ops`` never opens ``home_assistant_config.json`` itself.

    Two independent writers of a Zone-1 credential's file is exactly what the
    one-source gate forbids, and it is also how the wizard's idea of where that
    file lives would drift from the token source's.
    """

    def test_set_ha_config_writes_through_ha_token_source(self, tmp_path,
                                                          monkeypatch):
        from puffpai import setup_ops
        from puffpai.security import ha_token_source

        monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(tmp_path))
        from puffpai.config import get_config
        get_config(_force_reload=True)

        seen = {}
        real = ha_token_source.write_ha_config

        def _spy(url, token, *, enabled=True, config_dir=""):
            seen.update(url=url, enabled=enabled, config_dir=config_dir)
            return real(url, token, enabled=enabled, config_dir=config_dir)

        monkeypatch.setattr(ha_token_source, "write_ha_config", _spy)
        path = setup_ops.set_ha_config("http://127.0.0.1:8123", FAKE_TOKEN,
                                       posture="sealed")

        assert seen["url"] == "http://127.0.0.1:8123"
        assert Path(path).parent == tmp_path
        on_disk = json.loads(Path(path).read_text(encoding="utf-8"))
        assert on_disk == {"url": "http://127.0.0.1:8123",
                           "token": FAKE_TOKEN, "enabled": True}

    def test_the_write_is_atomic_and_leaves_no_temp_file(self, tmp_path):
        """A half-written credential file is a credential file the appliance
        reads at the next boot."""
        from puffpai.security import ha_token_source

        path = Path(ha_token_source.write_ha_config(
            "http://127.0.0.1:8123", FAKE_TOKEN, config_dir=str(tmp_path)))
        assert path.exists()
        assert [p.name for p in tmp_path.iterdir()] == [path.name]

    @pytest.mark.skipif(os.name == "nt",
                        reason="chmod only toggles the read-only bit on Windows")
    def test_the_file_is_mode_600(self, tmp_path):
        from puffpai.security import ha_token_source

        path = Path(ha_token_source.write_ha_config(
            "http://127.0.0.1:8123", FAKE_TOKEN, config_dir=str(tmp_path)))
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_a_failed_write_leaves_the_old_file_intact(self, tmp_path,
                                                       monkeypatch):
        """``os.replace`` is what makes this true; assert it rather than trust
        it, because the failure mode is a config with no token in it."""
        from puffpai.security import ha_token_source, ha_vault

        first = Path(ha_token_source.write_ha_config(
            "http://127.0.0.1:8123", FAKE_TOKEN, config_dir=str(tmp_path)))
        before = first.read_text(encoding="utf-8")

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(ha_vault.os, "replace", _boom)
        with pytest.raises(OSError):
            ha_token_source.write_ha_config("http://127.0.0.1:8123", "other",
                                            config_dir=str(tmp_path))
        assert first.read_text(encoding="utf-8") == before
        assert [p.name for p in tmp_path.iterdir()] == [first.name]

    def test_the_reader_and_the_writer_name_the_same_file(self, tmp_path,
                                                          monkeypatch):
        """``ha_vault`` resolved the file from ``__file__`` while ``config.py``
        honoured ``$PUFFPAI_CONFIG_DIR``: on a scratch install the token
        fallback read the developer's real file next to the checkout."""
        from puffpai.security import ha_token_source, ha_vault

        monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(tmp_path))
        ha_token_source.write_ha_config("http://127.0.0.1:8123", FAKE_TOKEN)
        assert ha_vault._config_file().parent == tmp_path
        assert ha_vault._load_plaintext_config()["url"] == "http://127.0.0.1:8123"
        assert Path(ha_token_source.ha_config_path()).parent == tmp_path

    def test_an_explicit_module_redirect_still_wins(self, tmp_path, monkeypatch):
        """The existing tests monkeypatch ``_HA_CONFIG_FILE``; that has to keep
        outranking the environment or the boot suite silently stops testing
        what it says it does."""
        from puffpai.security import ha_vault

        target = tmp_path / "elsewhere.json"
        monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(tmp_path / "ignored"))
        monkeypatch.setattr(ha_vault, "_HA_CONFIG_FILE", target)
        assert ha_vault._config_file() == target

    def test_writing_a_new_token_drops_the_resolution_cache(self, tmp_path,
                                                            monkeypatch):
        from puffpai.security import ha_token_source as src

        calls = []
        monkeypatch.setattr(src, "invalidate_cache",
                            lambda: calls.append(True))
        src.write_ha_config("http://127.0.0.1:8123", FAKE_TOKEN,
                            config_dir=str(tmp_path))
        assert calls == [True]


# ── 3. one name for the pending-apply record ────────────────────────────────

def test_the_network_edge_pending_kind_has_one_name():
    """The wizard wrote ``zone3_network_edge`` and the HTTP route wrote
    ``bridge_network_edge``, so whichever face the owner used decided whether
    the other could ever find the record. Grep, because the next divergence
    will be introduced by a copy-paste, not by a call."""
    offenders = []
    for path in list((REPO / "src").rglob("*.py")) + list(
            (REPO / "tests").rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        if "bridge_network_edge" in path.read_text(encoding="utf-8",
                                                   errors="replace"):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == [], (
        "these still use the old pending-apply kind; it is "
        "'zone3_network_edge' everywhere: " + ", ".join(offenders))


def test_both_faces_record_the_same_kind(tmp_path, monkeypatch):
    """The HTTP route's record has to be findable by the code the CLI's record
    is read with -- ``record_pending_apply`` keys on ``kind``, so two spellings
    meant two entries and a reader that saw one of them."""
    from puffpai import setup_ops

    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    from puffpai.config import get_config
    get_config(_force_reload=True)

    setup_ops.record_pending_apply("zone3_network_edge",
                                   {"posture": "island", "requested": True})
    setup_ops.record_pending_apply("zone3_network_edge",
                                   {"posture": "sealed", "requested": True})
    kinds = [item["kind"] for item in setup_ops.pending_apply()]
    assert kinds == ["zone3_network_edge"], (
        "a second spelling would have left two rows here")


# ── 4. one bind-warning helper, used by both entry points ───────────────────

class TestLoopbackBindWarning:
    """``start_api.py`` warned on a non-loopback bind; ``api_server``'s own
    ``__main__`` did not, and that is the entry point a developer reaches for.
    """

    def test_the_helper_lives_in_one_place(self):
        import start_api
        from puffpai.security import netaddr

        assert start_api.is_loopback_bind is netaddr.is_loopback_bind

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.1.1", "::1", "[::1]",
                                      "localhost", "  127.0.0.1  ", "LOCALHOST"])
    def test_loopback_binds(self, host):
        from puffpai.security import netaddr
        assert netaddr.is_loopback_bind(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "", "   ", "::", "192.168.1.8",
                                      "puffpai.local", None, "127.0.0.1.evil"])
    def test_everything_else_is_not_loopback(self, host):
        """Unrecognised must mean 'warn'. A bind address nobody can classify is
        the one most likely to be exposed."""
        from puffpai.security import netaddr
        assert netaddr.is_loopback_bind(host) is False

    def test_api_server_main_warns_on_a_non_loopback_bind(self):
        """``__main__`` is not importable as a unit, so read it: the check has
        to be in the block that calls ``app.run``."""
        text = (REPO / "src" / "puffpai" / "api_server.py").read_text(
            encoding="utf-8", errors="replace")
        main_block = text.split("if __name__ == '__main__':", 1)[1]
        assert "is_loopback_bind" in main_block
        assert "SECURITY" in main_block
        assert main_block.index("is_loopback_bind") < main_block.index("app.run")


# ── 5. no test may write the shipped config ─────────────────────────────────

class TestConfigIsolation:
    def test_the_config_dir_is_a_temp_dir(self):
        from puffpai.config import get_config

        config_dir = Path(get_config().CONFIG_DIR).resolve()
        assert config_dir != (REPO / "config").resolve(), (
            "this test is reading the shipped config directory; the autouse "
            "fixture in tests/conftest.py is not doing its job")

    def test_the_state_dir_is_a_temp_dir(self):
        from puffpai.config import get_config, STATE_DIR_ENV

        assert os.environ.get(STATE_DIR_ENV, "").strip()
        assert Path(get_config().DATA_DIR).resolve() != REPO.resolve()

    def test_writing_the_main_config_does_not_touch_the_checkout(self):
        from puffpai import setup_ops

        path = Path(setup_ops.write_main_config({"location": "Nowhere, XX"}))
        assert (REPO / "config") not in path.parents
        assert json.loads(path.read_text(encoding="utf-8"))["location"] == \
            "Nowhere, XX"

    def test_the_seeded_copy_still_has_the_shipped_values(self):
        """Isolation that empties the config would test a different appliance.
        The tmp dir is a COPY, so defaults still come from the shipped file."""
        from puffpai.config import get_config

        shipped = REPO / "config" / "puffpai_config.json"
        if not shipped.exists():           # pragma: no cover - checkout variant
            pytest.skip("no shipped config in this checkout")
        want = json.loads(shipped.read_text(encoding="utf-8"))
        if want.get("ollama", {}).get("model"):
            assert get_config().ollama.model == want["ollama"]["model"]


# ── 6. the recovery code the vault actually mints ───────────────────────────

class TestRecoveryCodeFormat:
    """``zone_vault`` documented and generated a 5x5 code over its own
    alphabet; ``security/vault.py`` -- the thing that actually mints and
    redeems one -- produces 6 groups of 4 base32 characters. The wizard was
    describing a format the vault does not use, and would have told an owner
    their correctly-transcribed code was the wrong shape."""

    def test_zone_vault_describes_the_vaults_real_format(self):
        from puffpai import zone_vault
        from puffpai.security import vault as vault_mod

        code = vault_mod._format_recovery_code(
            b"\x00" * vault_mod._RECOVERY_CODE_BYTES)
        groups = code.split("-")
        assert len(groups) == zone_vault.RECOVERY_GROUPS
        assert all(len(g) == zone_vault.RECOVERY_GROUP_LENGTH for g in groups)
        assert len(code.replace("-", "")) == zone_vault.RECOVERY_CODE_LENGTH
        assert set(code.replace("-", "")) <= set(zone_vault.RECOVERY_ALPHABET)

    def test_generate_goes_through_the_vault(self):
        from puffpai import zone_vault

        code = zone_vault.generate_recovery_code()
        groups = code.split("-")
        assert len(groups) == zone_vault.RECOVERY_GROUPS
        assert all(len(g) == zone_vault.RECOVERY_GROUP_LENGTH for g in groups)
        assert set(code.replace("-", "")) <= set(zone_vault.RECOVERY_ALPHABET)
        assert code != zone_vault.generate_recovery_code()

    def test_the_entropy_claim_matches_the_format(self):
        from puffpai import zone_vault
        from puffpai.security import vault as vault_mod

        assert zone_vault.RECOVERY_CODE_ENTROPY_BITS == \
            vault_mod._RECOVERY_CODE_BYTES * 8

    def test_a_code_the_vault_minted_validates(self):
        from puffpai import zone_vault
        from puffpai.security import vault as vault_mod
        import secrets as _secrets

        code = vault_mod._format_recovery_code(
            _secrets.token_bytes(vault_mod._RECOVERY_CODE_BYTES))
        assert zone_vault.is_recovery_code(code) is True

    @pytest.mark.parametrize("bad", [
        "", None, "ABCD-EFGH", "ABCD-EFGH-IJKL-MNOP-QRST-UVW",
        "ABCD-EFGH-IJKL-MNOP-QRST-UVW1",      # 1 and 0 are not in base32
        "ABCD-EFGH-IJKL-MNOP-QRST-UVW0",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ2345678",  # too long
    ])
    def test_a_code_that_is_not_the_vaults_is_refused(self, bad):
        from puffpai import zone_vault
        assert zone_vault.is_recovery_code(bad) is False

    @pytest.mark.parametrize("typed", [
        "abcd-efgh-ijkl-mnop-qrst-uvwx",
        "ABCD EFGH IJKL MNOP QRST UVWX",
        "ABCDEFGHIJKLMNOPQRSTUVWX",
        "  abcd efgh-ijkl  mnop qrst uvwx \n",
    ])
    def test_the_typed_back_check_tolerates_case_spaces_and_hyphens(self, typed):
        from puffpai import zone_vault

        shown = "ABCD-EFGH-IJKL-MNOP-QRST-UVWX"
        assert zone_vault.verify_recovery_confirmation(shown, typed) is True

    def test_it_still_refuses_a_different_code(self):
        from puffpai import zone_vault

        assert zone_vault.verify_recovery_confirmation(
            "ABCD-EFGH-IJKL-MNOP-QRST-UVWX",
            "ABCD-EFGH-IJKL-MNOP-QRST-UVWY") is False
        assert zone_vault.verify_recovery_confirmation("", "") is False
