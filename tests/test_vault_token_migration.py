"""The HA-token vault migration, and the trap inside it.

``store_ha_token`` writes ``ha_token.json`` into the vault and rewrites the
plaintext config to ``MIGRATED_TO_VAULT``, but ``migrate_plaintext_files``
cannot see the config file, so without an explicit migration step the token
never moves: no ``ha_token.json`` in the vault dir, no marker in the config,
and "falling back to plaintext" journal warnings every day.

Two things are tested here.

**The trap.** After a successful migration the plaintext config holds the
string ``MIGRATED_TO_VAULT``. If ``get_ha_token`` accepted anything that is not
the setup placeholder, a locked vault's fallback would return that literal
string *as the token*, and the appliance would send
``Authorization: Bearer MIGRATED_TO_VAULT`` to Home Assistant. A migration whose
success marker is indistinguishable from a credential is a migration that
breaks the house quietly. Every read path must treat it as "no token".

**The migration.** ``migrate_ha_token_to_vault`` is reachable, idempotent,
refuses without an unlocked session, and does **not** scrub the plaintext config
by default — because ``config.py`` and ``ha_bridge`` still read the token from
that file directly, so scrubbing it before they are rewired takes Home Assistant
down. Fake token material only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.security import ha_vault  # noqa: E402

#: Obviously fake, JWT-shaped enough to exercise the length/looks checks.
FAKE_TOKEN = "eyJ0ZXN0IjoiZmFrZXBlcnNvbiJ9.not-a-real-token.Test-Fakeperson"


@pytest.fixture
def ha_config(tmp_path, monkeypatch):
    """Point ha_vault at a throwaway plaintext config holding a fake token."""
    path = tmp_path / "home_assistant_config.json"
    path.write_text(json.dumps({
        "url": "http://homeassistant.local:8123",
        "token": FAKE_TOKEN,
        "enabled": True,
    }), encoding="utf-8")
    monkeypatch.setattr(ha_vault, "_HA_CONFIG_FILE", path)
    monkeypatch.setattr(ha_vault, "_FALLBACK_WARNED", False)
    return path


# ── the trap: a marker is not a credential ───────────────────────────────────

def test_migration_marker_is_not_a_token():
    assert ha_vault._usable_token(ha_vault.MIGRATED_MARKER) is None
    assert ha_vault._usable_token("YOUR_LONG_LIVED_ACCESS_TOKEN_HERE") is None
    assert ha_vault._usable_token("") is None
    assert ha_vault._usable_token("   ") is None
    assert ha_vault._usable_token(None) is None
    assert ha_vault._usable_token(FAKE_TOKEN) == FAKE_TOKEN


def test_plaintext_fallback_never_returns_the_marker(ha_config, monkeypatch):
    """The core guarantee of the migration.

    A migrated box with a locked vault must get None, not the string
    "MIGRATED_TO_VAULT" handed onward as a bearer token.
    """
    ha_config.write_text(json.dumps({
        "url": "http://homeassistant.local:8123",
        "token": ha_vault.MIGRATED_MARKER,
        "enabled": True,
    }), encoding="utf-8")
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: None)
    assert ha_vault.get_ha_token() is None
    assert ha_vault.get_ha_token("some-session") is None


def test_get_ha_config_blanks_a_marker_rather_than_passing_it_on(ha_config,
                                                                 monkeypatch):
    ha_config.write_text(json.dumps({
        "url": "http://homeassistant.local:8123",
        "token": ha_vault.MIGRATED_MARKER,
        "enabled": True,
    }), encoding="utf-8")
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: None)
    cfg = ha_vault.get_ha_config()
    assert cfg["token"] == ""
    assert cfg["url"] == "http://homeassistant.local:8123"


def test_plaintext_fallback_still_serves_a_real_token(ha_config, monkeypatch):
    """The unmigrated box must keep working — that is why the fallback is on."""
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: None)
    assert ha_vault.get_ha_token() == FAKE_TOKEN


# ── status ───────────────────────────────────────────────────────────────────

def test_status_is_content_free(ha_config):
    status = ha_vault.ha_token_migration_status()
    assert status["plaintext_token_present"] is True
    assert status["plaintext_token_len"] == len(FAKE_TOKEN)
    assert status["migrated_marker_present"] is False
    # Nothing in the report may carry the token itself.
    assert FAKE_TOKEN not in json.dumps(status)


def test_status_reports_a_migrated_box(ha_config):
    ha_config.write_text(json.dumps({"token": ha_vault.MIGRATED_MARKER}),
                         encoding="utf-8")
    status = ha_vault.ha_token_migration_status()
    assert status["migrated_marker_present"] is True
    assert status["plaintext_token_present"] is False


# ── the migration ────────────────────────────────────────────────────────────

class _FakeVault:
    """Just enough Vault to exercise the migration's decision points.

    Note ``write_service_secret``, not ``write_secure_session``: the token
    goes into the vault's BOOT-SECRET tier (encrypted under the Service Data
    Key), which is what makes the unattended 03:00 read possible at all.
    """

    def __init__(self, *, initialized=True, unlocked=True, writes_ok=True):
        self.is_initialized = initialized
        self._unlocked = unlocked
        self._writes_ok = writes_ok
        self.written: dict = {}

    def is_unlocked(self, _session_id):
        return self._unlocked

    def write_service_secret(self, filename, data, _session_id):
        if not self._writes_ok:
            from puffpai.security.vault import VaultError
            raise VaultError("write refused")
        self.written[filename] = data

    def read_service_secret_unattended(self, filename):
        return self.written.get(filename)

    def read_service_secret(self, filename, _session_id):
        return self.written.get(filename)

    def _first_live_session_id(self):
        return "unlocked-session" if self._unlocked else None


def test_migration_refuses_without_an_unlocked_session(ha_config, monkeypatch):
    """BLOCKED-on-owner-PIN is the designed answer, not an error to work around."""
    vault = _FakeVault(unlocked=False)
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)
    result = ha_vault.migrate_ha_token_to_vault("locked-session")
    assert result["stored"] is False
    assert "PIN" in result["reason"]
    # And the plaintext config is untouched, so HA keeps working.
    assert json.loads(ha_config.read_text(encoding="utf-8"))["token"] == FAKE_TOKEN


def test_migration_refuses_on_an_uninitialized_vault(ha_config, monkeypatch):
    monkeypatch.setattr(ha_vault, "_get_vault",
                        lambda: _FakeVault(initialized=False))
    result = ha_vault.migrate_ha_token_to_vault("s")
    assert result["stored"] is False
    assert "not initialized" in result["reason"]


def test_migration_stores_encrypted_and_leaves_plaintext_by_default(
        ha_config, monkeypatch):
    vault = _FakeVault()
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)

    result = ha_vault.migrate_ha_token_to_vault("unlocked-session")

    assert result["stored"] is True
    assert result["scrubbed"] is False
    assert vault.written["ha_token.json"] == {"token": FAKE_TOKEN}
    # Storing is additive. The plaintext copy is removed only after a separate,
    # verified --scrub, so a half-finished migration never takes HA down.
    assert json.loads(ha_config.read_text(encoding="utf-8"))["token"] == FAKE_TOKEN
    assert "--scrub" in result["reason"]


def test_migration_scrubs_only_when_asked(ha_config, monkeypatch):
    vault = _FakeVault()
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)

    result = ha_vault.migrate_ha_token_to_vault("unlocked-session",
                                                scrub_plaintext=True)

    assert result["stored"] is True
    assert result["scrubbed"] is True
    on_disk = json.loads(ha_config.read_text(encoding="utf-8"))
    assert on_disk["token"] == ha_vault.MIGRATED_MARKER
    assert FAKE_TOKEN not in ha_config.read_text(encoding="utf-8")


def test_migration_is_idempotent_on_an_already_migrated_box(ha_config,
                                                            monkeypatch):
    ha_config.write_text(json.dumps({"token": ha_vault.MIGRATED_MARKER}),
                         encoding="utf-8")
    vault = _FakeVault()
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)
    result = ha_vault.migrate_ha_token_to_vault("unlocked-session")
    assert result["already_stored"] is True
    assert result["stored"] is False
    assert vault.written == {}  # no pointless re-write


def test_migration_reports_a_failed_vault_write_without_touching_plaintext(
        ha_config, monkeypatch):
    vault = _FakeVault(writes_ok=False)
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)
    with pytest.warns(UserWarning):
        result = ha_vault.migrate_ha_token_to_vault("unlocked-session")
    assert result["stored"] is False
    assert "vault write failed" in result["reason"]
    assert json.loads(ha_config.read_text(encoding="utf-8"))["token"] == FAKE_TOKEN


def test_no_token_material_appears_in_any_result(ha_config, monkeypatch):
    vault = _FakeVault()
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: vault)
    for kwargs in ({}, {"scrub_plaintext": True}):
        ha_config.write_text(json.dumps({"token": FAKE_TOKEN}), encoding="utf-8")
        result = ha_vault.migrate_ha_token_to_vault("s", **kwargs)
        assert FAKE_TOKEN not in json.dumps(result)


# ── the owner's command exists and refuses to scrub casually ────────────────

def test_the_owner_script_verifies_before_it_scrubs():
    """The interlock that matters most: never remove the copy that keeps
    the house working until the vaulted copy has been proven to work."""
    script = (Path(__file__).resolve().parents[1] / "scripts"
              / "migrate_ha_token.py")
    text = script.read_text(encoding="utf-8")
    assert "getpass" in text        # the PIN is never echoed or passed in argv
    assert "--status" in text
    assert "--dry-run" in text
    assert "--scrub" in text
    assert "--strict" in text
    assert "_live_ha_check" in text
    assert "_read_back_vaulted_token" in text
    # It refuses to scrub without the root-only rollback copy.
    assert "Refusing to scrub" in text
    assert "PUFFPAI_HA_CONFIG_BACKUP" in text


def test_the_root_step_is_a_script_the_owner_runs_with_sudo():
    sh = (Path(__file__).resolve().parents[1] / "scripts"
          / "install_vault_service_key.sh")
    text = sh.read_text(encoding="utf-8")
    assert "--dry-run" in text
    assert "LoadCredential=" in text
    assert "chmod 600" in text or "-m 0600" in text
    assert "id -u" in text          # refuses to run as anyone but root
    # It must never regenerate a key that the vault keyring already wraps.
    assert "NOT regenerating" in text
    # And it takes exactly one rollback copy, without clobbering it.
    assert "NOT overwriting" in text


def test_the_failclosed_dropin_is_not_installed_by_default():
    """The env change ships as a file an operator installs AFTER verifying."""
    conf = (Path(__file__).resolve().parents[1] / "systemd"
            / "puffpai-api.service.d" / "20-ha-token-failclosed.conf")
    text = conf.read_text(encoding="utf-8")
    assert "PUFFPAI_ALLOW_PLAINTEXT_HA_TOKEN_FALLBACK=0" in text
    assert "DO NOT INSTALL THIS FILE UNTIL" in text
    # The install order must name the root step and the owner step, in order.
    assert text.index("install_vault_service_key.sh") < \
        text.index("migrate_ha_token.py")
    # The header must not document an invocation that cannot enroll the wrap.
    # This is the one file an operator reads for the procedure, so it must
    # carry the systemd-run form and must NOT carry the sudo -u one.
    assert "systemd-run" in text
    assert "--property=LoadCredential=puffpai-vault-service-key" in text
    # It may NAME the sudo -u form, but only to explain why it does not work
    # -- never as a command block an operator could copy and run.
    for line in text.splitlines():
        if "sudo -u puffpai" in line:
            assert "cannot" in line, line
        if "migrate_ha_token.py" in line:
            assert "sudo -u puffpai" not in line, line
    #...and warn about the concurrent-keyring-writer hazard.
    assert "recovery code" in text


# ── the owner script actually runs, and refuses in the right places ─────────


def _load_migration_script():
    """Import scripts/migrate_ha_token.py by path (it is not a package)."""
    import importlib.util
    path = (Path(__file__).resolve().parents[1] / "scripts"
            / "migrate_ha_token.py")
    spec = importlib.util.spec_from_file_location("_migrate_ha_token", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_status_runs_and_changes_nothing(ha_config, capsys):
    mod = _load_migration_script()
    before = ha_config.read_bytes()
    assert mod.main(["--status"]) == 0
    assert ha_config.read_bytes() == before
    out = capsys.readouterr().out
    assert FAKE_TOKEN not in out          # never the value
    assert "plaintext token present: True" in out


def test_dry_run_changes_nothing_and_names_the_missing_root_step(
        ha_config, tmp_path, monkeypatch, capsys):
    mod = _load_migration_script()
    monkeypatch.setenv(mod.BACKUP_ENV, str(tmp_path / "absent.bak"))
    from puffpai.security import vault as vault_mod
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv(vault_mod._SERVICE_KEY_ENV, str(tmp_path / "nokey"))
    vault_mod._service_key_warned.clear()

    before = ha_config.read_bytes()
    assert mod.main(["--dry-run", "--scrub"]) == 0
    assert ha_config.read_bytes() == before

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would SKIP the service wrap" in out
    assert "install_vault_service_key.sh" in out
    assert FAKE_TOKEN not in out


def test_scrub_refuses_without_the_root_only_rollback(ha_config, tmp_path,
                                                      monkeypatch, capsys):
    """No rollback, no scrub. The plaintext config is what keeps the house
    working until the vaulted copy is proven."""
    mod = _load_migration_script()
    monkeypatch.setenv(mod.BACKUP_ENV, str(tmp_path / "definitely-absent.bak"))
    before = ha_config.read_bytes()

    def _no_prompt(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("prompted for the PIN before checking the backup")

    monkeypatch.setattr("getpass.getpass", _no_prompt)

    assert mod.main(["--scrub"]) == 2
    assert ha_config.read_bytes() == before
    assert "Refusing to scrub" in capsys.readouterr().err


def test_the_script_hands_the_owner_a_runnable_privilege_bridge():
    """The privilege trap a plain sudo invocation walks into.

    The migration must run AS puffpai (it writes the vault keyring, the vault
    record and the config; as root it would leave root-owned files the service
    then cannot write). But the machine key is 0600 root:root. A plain
    `sudo -u puffpai` run therefore cannot enroll the service wrap at all --
    it would silently ship an appliance with no unattended boot path.

    systemd-run is the bridge: the transient unit gets the credential exactly
    the way puffpai-api does, while the process stays unprivileged.
    """
    mod = _load_migration_script()
    cmd = mod.SYSTEMD_RUN_CMD
    assert "systemd-run" in cmd
    assert "--pty" in cmd                    # getpass needs a terminal
    assert "--uid=puffpai" in cmd            # NOT as root
    assert "LoadCredential=puffpai-vault-service-key" in cmd
    assert "migrate_ha_token.py" in cmd

    text = (Path(__file__).resolve().parents[1] / "scripts"
            / "migrate_ha_token.py").read_text(encoding="utf-8")
    # The --scrub re-run needs the credential too: it repeats the whole flow,
    # enrolment included.
    assert 'SYSTEMD_RUN_CMD + " --scrub"' in text
    # And the root installer must hand the owner the same command.
    sh = (Path(__file__).resolve().parents[1] / "scripts"
          / "install_vault_service_key.sh").read_text(encoding="utf-8")
    assert "systemd-run" in sh
    assert "--uid=puffpai" in sh
