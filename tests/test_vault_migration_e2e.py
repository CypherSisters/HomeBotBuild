"""The migration script's REAL store path, with a real PIN.

Why this file exists. If `scripts/migrate_ha_token.py` built its own `Vault`
and authenticated it while `ha_vault.store_ha_token_service_tier` stored
through `ha_vault._get_vault` (a *different* instance whose `_sessions` dict
has never seen that session id), a real run with the correct PIN would print

    stored in vault: False
    vault is locked for this session; the owner's PIN is required

and exit 1, blaming the owner's PIN. Only running the store path shows it.

Every test here therefore exercises the REAL path — a real `Vault`, a real
PIN, a real keyring, a real encrypted record — and **never monkeypatches
`_get_vault`**, because monkeypatching it is exactly what hides the bug.

Fake token material only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from puffpai.security import ha_token_source as src  # noqa: E402
from puffpai.security import ha_vault  # noqa: E402
from puffpai.security import vault as vault_mod  # noqa: E402
from puffpai.security.vault import Vault  # noqa: E402

FAKE_TOKEN = "eyJ0ZXN0IjoiZmFrZXBlcnNvbiJ9.not-a-real-token.Test-Fakeperson"
FAKE_MACHINE_KEY = "c" * 64
PIN = "vault-pin-1234"


def _load_script():
    path = REPO / "scripts" / "migrate_ha_token.py"
    spec = importlib.util.spec_from_file_location("_migrate_e2e", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An unmigrated appliance: plaintext token on disk, initialized vault,
    machine key installed, nothing in the vault yet."""
    key = tmp_path / "vault_service.key"
    key.write_text(FAKE_MACHINE_KEY, encoding="utf-8")
    try:
        os.chmod(key, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv(vault_mod._SERVICE_KEY_ENV, str(key))
    vault_mod._service_key_warned.clear()

    cfg = tmp_path / "home_assistant_config.json"
    cfg.write_text(json.dumps({
        "url": "http://homeassistant.local:8123",
        "token": FAKE_TOKEN,
        "enabled": True,
    }), encoding="utf-8")
    monkeypatch.setattr(ha_vault, "_HA_CONFIG_FILE", cfg)
    monkeypatch.setattr(ha_vault, "_FALLBACK_WARNED", False)

    vault_dir = tmp_path / "vault"
    setup = Vault(vault_dir=str(vault_dir))
    setup.initialize(PIN)

    src.invalidate_cache()
    return {"vault_dir": vault_dir, "config": cfg, "key": key}


# ── the defect itself ────────────────────────────────────────────────────────

def test_a_script_owned_vault_can_store_the_token(box):
    """The script authenticates its OWN Vault, exactly as the real one
    does, and the store must land — no monkeypatching anywhere."""
    vault = Vault(vault_dir=str(box["vault_dir"]))
    session = vault.authenticate(PIN)
    assert vault.enroll_service_wrap(session.session_id) is True

    result = ha_vault.migrate_ha_token_to_vault(
        session.session_id, scrub_plaintext=False, vault=vault)

    assert result["stored"] is True, result["reason"]
    assert result["scrubbed"] is False
    assert (box["vault_dir"] / "ha_token.json").exists()


def test_without_the_injection_it_fails_closed_and_says_why(box, monkeypatch):
    """The bug's shape, pinned. A session id from one instance is meaningless
    to another, and the refusal must SAY so rather than blame the PIN, which
    would send the owner hunting for a typo that does not exist."""
    script_vault = Vault(vault_dir=str(box["vault_dir"]))
    session = script_vault.authenticate(PIN)

    other = Vault(vault_dir=str(box["vault_dir"]))   # a different instance
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: other)

    result = ha_vault.migrate_ha_token_to_vault(session.session_id)
    assert result["stored"] is False
    assert "instance-scoped" in result["reason"]
    # And the plaintext config is untouched, so Home Assistant keeps working.
    assert json.loads(box["config"].read_text(encoding="utf-8"))["token"] \
        == FAKE_TOKEN


def test_store_is_readable_by_a_cold_process_afterwards(box):
    """The point of the whole exercise: what the script wrote is what an
    unattended boot reads."""
    vault = Vault(vault_dir=str(box["vault_dir"]))
    session = vault.authenticate(PIN)
    vault.enroll_service_wrap(session.session_id)
    ha_vault.migrate_ha_token_to_vault(session.session_id, vault=vault)

    cold = Vault(vault_dir=str(box["vault_dir"]))
    assert cold.has_live_session is False
    assert cold.read_service_secret_unattended("ha_token.json") == {
        "token": FAKE_TOKEN}


# ── the whole script, end to end, with the PIN typed ────────────────────────

def _run_script(mod, box, monkeypatch, argv, *, pin=PIN, ha_status=200):
    """Drive main with the PIN supplied and Home Assistant mocked."""
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: pin)

    class _Resp:
        status_code = ha_status

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    real_vault = Vault

    def _vault_here(*a, **k):
        return real_vault(vault_dir=str(box["vault_dir"]))

    monkeypatch.setattr(vault_mod, "Vault", _vault_here)
    return mod.main(argv)


def test_the_script_completes_with_the_correct_pin(box, monkeypatch, capsys):
    """The operator-visible run: exit 0 with the correct PIN."""
    mod = _load_script()
    monkeypatch.setenv(mod.BACKUP_ENV, str(box["config"]) + ".bak")

    rc = _run_script(mod, box, monkeypatch, [])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "stored in vault  : True" in out
    assert "read back via    : vault-service (unattended)" in out
    assert "live HA check    : PASS" in out
    assert FAKE_TOKEN not in out          # never the value
    # Store only: the plaintext copy is still keeping the house working.
    assert json.loads(box["config"].read_text(encoding="utf-8"))["token"] \
        == FAKE_TOKEN


def test_the_script_scrubs_only_after_a_live_check_passes(box, monkeypatch,
                                                          capsys):
    mod = _load_script()
    backup = Path(str(box["config"]) + ".bak")
    backup.write_bytes(box["config"].read_bytes())      # the root-only rollback
    monkeypatch.setenv(mod.BACKUP_ENV, str(backup))

    rc = _run_script(mod, box, monkeypatch, ["--scrub"])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "plaintext scrub  : done" in out
    on_disk = box["config"].read_text(encoding="utf-8")
    assert json.loads(on_disk)["token"] == ha_vault.MIGRATED_MARKER
    assert FAKE_TOKEN not in on_disk
    # The rollback still holds the original.
    assert json.loads(backup.read_text(encoding="utf-8"))["token"] == FAKE_TOKEN


def test_a_failing_live_check_never_touches_the_plaintext(box, monkeypatch,
                                                          capsys):
    """HA answers 401 — refuse, exit 4, leave the house working."""
    mod = _load_script()
    backup = Path(str(box["config"]) + ".bak")
    backup.write_bytes(box["config"].read_bytes())
    monkeypatch.setenv(mod.BACKUP_ENV, str(backup))
    before = box["config"].read_bytes()

    rc = _run_script(mod, box, monkeypatch, ["--scrub"], ha_status=401)

    assert rc == 4
    assert "live HA check    : FAIL" in capsys.readouterr().out
    assert box["config"].read_bytes() == before


def test_the_script_is_idempotent(box, monkeypatch, capsys):
    mod = _load_script()
    backup = Path(str(box["config"]) + ".bak")
    backup.write_bytes(box["config"].read_bytes())
    monkeypatch.setenv(mod.BACKUP_ENV, str(backup))

    assert _run_script(mod, box, monkeypatch, ["--scrub"]) == 0
    capsys.readouterr()
    sha_before = box["config"].read_bytes()

    assert _run_script(mod, box, monkeypatch, ["--scrub"]) == 0
    out = capsys.readouterr().out
    assert "already migrated : True" in out
    assert box["config"].read_bytes() == sha_before


def test_a_wrong_pin_stops_before_anything_is_written(box, monkeypatch,
                                                      capsys):
    mod = _load_script()
    monkeypatch.setenv(mod.BACKUP_ENV, str(box["config"]) + ".bak")
    before = box["config"].read_bytes()

    rc = _run_script(mod, box, monkeypatch, [], pin="9999")

    assert rc == 1
    assert "Could not unlock the vault" in capsys.readouterr().err
    assert box["config"].read_bytes() == before
    assert not (box["vault_dir"] / "ha_token.json").exists()


def test_no_machine_key_refuses_rather_than_shipping_a_dead_boot_path(
        box, monkeypatch, capsys):
    """Exit 3 with the systemd-run command, not a silent skip that leaves the
    appliance unable to read its own credential after a reboot."""
    mod = _load_script()
    box["key"].unlink()
    vault_mod._service_key_warned.clear()
    monkeypatch.setenv(mod.BACKUP_ENV, str(box["config"]) + ".bak")
    before = box["config"].read_bytes()

    rc = _run_script(mod, box, monkeypatch, [])

    out = capsys.readouterr().out
    assert rc == 3
    assert "service wrap : NOT enrolled" in out
    assert box["config"].read_bytes() == before
    assert not (box["vault_dir"] / "ha_token.json").exists()


def test_strict_mode_needs_no_machine_key_and_still_stores(box, monkeypatch,
                                                           capsys):
    mod = _load_script()
    box["key"].unlink()
    vault_mod._service_key_warned.clear()
    monkeypatch.setenv(mod.BACKUP_ENV, str(box["config"]) + ".bak")

    rc = _run_script(mod, box, monkeypatch, ["--strict"])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "service wrap : removed (strict mode)" in out
    assert "read back via    : vault-session (PIN)" in out
    assert (box["vault_dir"] / "ha_token.json").exists()
