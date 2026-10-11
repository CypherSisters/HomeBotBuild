"""The vault's boot-secret tier (the machine-bound service wrap).

The problem: the HA long-lived token must be readable by the service after
every reboot, but the vault's data key is wrapped ONLY under the owner's PIN
and their recovery code. Moving the token into the vault on that basis would
take Home Assistant down until a human typed a PIN after every reboot, which
breaks the no-lockout rule ("unlock/recovery cannot lock the user out") on a
family appliance.

The answer is a third wrap. It is deliberately NOT a third wrap of the Zone-1
data key: that would mean anyone who reads the machine key can decrypt the
family's documents and pii_refs.json. Instead a separate random Service Data
Key encrypts the boot-secret allowlist only, and is wrapped twice -- under the
machine key (unattended reads) and under the DEK (so the PIN always reaches
it). These tests pin that blast radius down.

Fake token material only.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.security import vault as vault_mod  # noqa: E402
from puffpai.security.vault import Vault, VaultError  # noqa: E402

FAKE_TOKEN = "eyJ0ZXN0IjoiZmFrZXBlcnNvbiJ9.not-a-real-token.Test-Fakeperson"
FAKE_MACHINE_KEY = "0" * 32 + "f" * 32          # 64 chars, obviously fake
OTHER_MACHINE_KEY = "a" * 64
PIN = "vault-pin-1234"


@pytest.fixture
def machine_key(tmp_path, monkeypatch):
    """A machine-bound key file at the env-override location, mode 600."""
    path = tmp_path / "vault_service.key"
    path.write_text(FAKE_MACHINE_KEY, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv(vault_mod._SERVICE_KEY_ENV, str(path))
    vault_mod._service_key_warned.clear()
    return path


@pytest.fixture
def no_machine_key(monkeypatch, tmp_path):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv(vault_mod._SERVICE_KEY_ENV,
                       str(tmp_path / "definitely-absent.key"))
    vault_mod._service_key_warned.clear()


def _vault(tmp_path) -> Vault:
    v = Vault(vault_dir=str(tmp_path / "vault"))
    v.initialize(PIN)
    return v


# ── reading the machine key ─────────────────────────────────────────────────

def test_machine_key_is_read_from_the_env_override(machine_key):
    secret, source = vault_mod.read_service_key()
    assert secret == FAKE_MACHINE_KEY
    assert source == vault_mod._SERVICE_KEY_ENV


def test_systemd_credentials_directory_wins(tmp_path, monkeypatch, machine_key):
    """LoadCredential= is the production path, so it is tried first."""
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    cred_path = cred_dir / vault_mod._SERVICE_KEY_CRED_NAME
    cred_path.write_text(OTHER_MACHINE_KEY, encoding="utf-8")
    # systemd hands LoadCredential= files out at 0400; pytest's tmp_path
    # inherits the process umask, which on the Orin is 0002, so without this
    # the file lands group-readable and the vault refuses it — correctly. The
    # fixture, not the product, was wrong.
    try:
        os.chmod(cred_path, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    secret, source = vault_mod.read_service_key()
    assert secret == OTHER_MACHINE_KEY
    assert source == "systemd-credential"


def test_absent_key_is_a_reason_not_a_crash(no_machine_key):
    secret, reason = vault_mod.read_service_key()
    assert secret is None
    assert isinstance(reason, str) and reason


def test_a_too_short_key_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "short.key"
    path.write_text("abc", encoding="utf-8")
    # 0600 so LENGTH is the only thing left to refuse it for: under a 0002
    # umask the mode check fires first and the reason is about permissions,
    # not about the key being short.
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv(vault_mod._SERVICE_KEY_ENV, str(path))
    vault_mod._service_key_warned.clear()
    secret, reason = vault_mod.read_service_key()
    assert secret is None
    assert "short" in reason


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX permission bits are not meaningful here")
def test_a_world_readable_key_is_refused_not_used(tmp_path, monkeypatch):
    """The whole claim of this tier is that the secret is not readable by the
    world. Honouring an 0644 key would make the claim a lie while looking like
    it worked."""
    path = tmp_path / "leaky.key"
    path.write_text(FAKE_MACHINE_KEY, encoding="utf-8")
    os.chmod(path, 0o644)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv(vault_mod._SERVICE_KEY_ENV, str(path))
    vault_mod._service_key_warned.clear()
    secret, reason = vault_mod.read_service_key()
    assert secret is None
    assert "beyond its owner" in reason


# ── enrolment and the unattended read ───────────────────────────────────────

def test_cold_start_with_no_human_reads_the_token(tmp_path, machine_key):
    """The core case. Store under the owner's PIN, then read it back through a vault
    object that has never seen a PIN and holds no session -- which is exactly
    what puffpai-api is at 03:00 after a power cut."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    assert v.enroll_service_wrap(sess.session_id) is True
    v.write_service_secret("ha_token.json", {"token": FAKE_TOKEN},
                           sess.session_id)

    cold = Vault(vault_dir=str(tmp_path / "vault"))   # no PIN, no session
    assert cold.has_live_session is False
    assert cold.read_service_secret_unattended("ha_token.json") == {
        "token": FAKE_TOKEN}


def test_the_record_on_disk_is_not_plaintext(tmp_path, machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    v.write_service_secret("ha_token.json", {"token": FAKE_TOKEN},
                           sess.session_id)
    raw = (tmp_path / "vault" / "ha_token.json").read_bytes()
    assert FAKE_TOKEN.encode() not in raw
    assert b"token" not in raw


def test_the_keyring_never_holds_the_service_key_in_the_clear(tmp_path,
                                                              machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    keyring = json.loads(
        (tmp_path / "vault" / ".vault_keyring.json").read_text(encoding="utf-8"))
    assert set(keyring) >= {"pin", "service", "service_by_dek"}
    blob = json.dumps(keyring)
    assert FAKE_MACHINE_KEY not in blob
    assert PIN not in blob
    # Only public KDF material and ciphertext.
    assert set(keyring["service"]) <= {"kdf", "n", "r", "p", "salt", "nonce",
                                       "ct", "created"}


def test_enrolment_without_a_machine_key_fails_and_says_so(tmp_path,
                                                           no_machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    assert v.enroll_service_wrap(sess.session_id) is False
    assert v.has_service_wrap is False


def test_enrolment_refuses_a_locked_vault(tmp_path, machine_key):
    v = _vault(tmp_path)
    assert v.enroll_service_wrap("not-a-session") is False
    assert v.has_service_wrap is False


def test_enrolment_is_idempotent_and_keeps_the_same_service_key(tmp_path,
                                                                machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    v.write_service_secret("ha_token.json", {"token": FAKE_TOKEN},
                           sess.session_id)
    assert v.enroll_service_wrap(sess.session_id) is True
    cold = Vault(vault_dir=str(tmp_path / "vault"))
    assert cold.read_service_secret_unattended("ha_token.json")["token"] \
        == FAKE_TOKEN


def test_a_rotated_machine_key_needs_re_enrolment(tmp_path, machine_key,
                                                  monkeypatch):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    v.write_service_secret("ha_token.json", {"token": FAKE_TOKEN},
                           sess.session_id)

    machine_key.write_text(OTHER_MACHINE_KEY, encoding="utf-8")
    cold = Vault(vault_dir=str(tmp_path / "vault"))
    assert cold.read_service_secret_unattended("ha_token.json") is None

    #...and re-enrolling under the new key restores it, without rewriting the
    # record: the Service Data Key itself never changed.
    assert v.enroll_service_wrap(sess.session_id) is True
    cold2 = Vault(vault_dir=str(tmp_path / "vault"))
    assert cold2.read_service_secret_unattended("ha_token.json")["token"] \
        == FAKE_TOKEN


# ── blast radius: what the machine key does NOT open ────────────────────────

def test_the_machine_key_cannot_open_zone1_data(tmp_path, machine_key):
    """The point of not wrapping the DEK. Someone who reads the machine key
    gets the HA token and nothing else -- not the documents, not pii_refs."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    v.write_secure_session("pii_refs.json",
                           {"ssn": "219-09-9999",
                            "name": "Test Fakeperson"}, sess.session_id)

    cold = Vault(vault_dir=str(tmp_path / "vault"))
    # Not on the service allowlist at all.
    with pytest.raises(VaultError):
        cold._service_secret_path("pii_refs.json")
    assert cold.read_service_secret_unattended("pii_refs.json") is None
    # And the DEK path is still locked without a PIN.
    with pytest.raises(VaultError):
        cold.read_secure_session("pii_refs.json", "no-such-session")


def test_the_service_tier_is_an_allowlist_not_a_convention(tmp_path,
                                                           machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    for name in ("pii_refs.json", "vault_documents.json", "anything.json"):
        with pytest.raises(VaultError):
            v.write_service_secret(name, {"x": 1}, sess.session_id)


def test_service_records_cannot_escape_the_vault_directory(tmp_path,
                                                           machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    for name in ("../ha_token.json", "sub/ha_token.json"):
        with pytest.raises(VaultError):
            v.write_service_secret(name, {"x": 1}, sess.session_id)


# ── strict mode ─────────────────────────────────────────────────────────────

def test_removing_the_wrap_locks_the_unattended_path_but_not_the_owner(
        tmp_path, machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    v.write_service_secret("ha_token.json", {"token": FAKE_TOKEN},
                           sess.session_id)

    assert v.remove_service_wrap(sess.session_id) is True
    assert v.has_service_wrap is False

    cold = Vault(vault_dir=str(tmp_path / "vault"))
    assert cold.read_service_secret_unattended("ha_token.json") is None

    # The owner can still read it, and can re-enroll -- no lockout.
    warm = Vault(vault_dir=str(tmp_path / "vault"))
    sess2 = warm.authenticate(PIN)
    assert warm.read_service_secret("ha_token.json",
                                    sess2.session_id)["token"] == FAKE_TOKEN
    assert warm.enroll_service_wrap(sess2.session_id) is True


def test_removing_the_wrap_refuses_a_locked_vault(tmp_path, machine_key):
    """An availability decision is not something an unauthenticated caller
    gets to make for the household."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    assert v.remove_service_wrap("not-a-session") is False
    assert v.has_service_wrap is True


def test_remove_is_idempotent(tmp_path, machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    assert v.remove_service_wrap(sess.session_id) is True


# ── recovery: a forgotten PIN must not orphan the boot secret ───────────────

def test_the_recovery_code_still_reaches_the_boot_secret(tmp_path, machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    v.write_service_secret("ha_token.json", {"token": FAKE_TOKEN},
                           sess.session_id)
    code = v.generate_recovery_code(sess.session_id)

    fresh = Vault(vault_dir=str(tmp_path / "vault"))
    recovered = fresh.recover_with_code(code, "vault-pin-9876")
    assert fresh.read_service_secret(
        "ha_token.json", recovered.session_id)["token"] == FAKE_TOKEN


# ── failure modes never raise on the boot path ──────────────────────────────

def test_unattended_read_returns_none_when_nothing_is_enrolled(tmp_path,
                                                               machine_key):
    v = _vault(tmp_path)
    assert v.read_service_secret_unattended("ha_token.json") is None


def test_unattended_read_returns_none_on_a_corrupt_record(tmp_path,
                                                          machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    (tmp_path / "vault" / "ha_token.json").write_bytes(b"not ciphertext")
    assert v.read_service_secret_unattended("ha_token.json") is None


def test_pin_read_refuses_when_no_service_key_was_ever_minted(tmp_path,
                                                              machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    with pytest.raises(VaultError):
        v.read_service_secret("ha_token.json", sess.session_id)


# ── the in-place migration must not touch boot secrets ──────────────────────

def test_migrate_plaintext_files_skips_the_boot_secret(tmp_path, machine_key):
    """It re-encrypts under the DEK. Doing that to an SDK-encrypted record
    would orphan the unattended read."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    v.write_service_secret("ha_token.json", {"token": FAKE_TOKEN},
                           sess.session_id)
    before = (tmp_path / "vault" / "ha_token.json").read_bytes()

    report = v.migrate_plaintext_files(sess.session_id)
    assert "ha_token.json" not in report["migrated"]
    assert "ha_token.json" not in report["failed"]
    assert (tmp_path / "vault" / "ha_token.json").read_bytes() == before

    cold = Vault(vault_dir=str(tmp_path / "vault"))
    assert cold.read_service_secret_unattended("ha_token.json")["token"] \
        == FAKE_TOKEN


# ── status is content-free ──────────────────────────────────────────────────

def test_service_tier_status_never_carries_key_material(tmp_path, machine_key):
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    status = v.service_tier_status()
    assert status["service_wrap_present"] is True
    assert status["service_key_wrapped_by_pin"] is True
    assert status["machine_key_present"] is True
    blob = json.dumps(status)
    assert FAKE_MACHINE_KEY not in blob
    assert PIN not in blob


def test_a_failed_enrolment_leaves_no_keyring_write_behind(tmp_path,
                                                           no_machine_key):
    """No machine key -> no half-enrolled state. Minting the service key
    first would leave an unused wrap in the keyring on every failed attempt."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    before = (tmp_path / "vault" / ".vault_keyring.json").read_text(
        encoding="utf-8")
    assert v.enroll_service_wrap(sess.session_id) is False
    after = (tmp_path / "vault" / ".vault_keyring.json").read_text(
        encoding="utf-8")
    assert after == before
    assert "service" not in json.loads(after)


def test_a_missing_record_costs_no_key_derivation(tmp_path, machine_key,
                                                  monkeypatch):
    """Between the root step and the owner step the wrap exists but the record
    does not, and /system/status is polled. That state must not run scrypt."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)
    assert not (tmp_path / "vault" / "ha_token.json").exists()

    calls = []
    real = v._derive_kek
    monkeypatch.setattr(v, "_derive_kek",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    assert v.read_service_secret_unattended("ha_token.json") is None
    assert calls == []


def test_the_boot_tier_never_creates_a_pin_less_keyring(tmp_path, machine_key):
    """A keyring written by this tier with no "pin" wrap would be rebuilt from
    scratch by the next unlock -- dropping the service wraps and orphaning the
    boot secrets with nothing failing loudly. Fail closed instead."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    (tmp_path / "vault" / ".vault_keyring.json").unlink()

    assert v.enroll_service_wrap(sess.session_id) is False
    with pytest.raises(VaultError):
        v.write_service_secret("ha_token.json", {"token": FAKE_TOKEN},
                               sess.session_id)
    assert not (tmp_path / "vault" / ".vault_keyring.json").exists()


def test_a_legacy_unlock_preserves_existing_service_wraps(tmp_path,
                                                          machine_key):
    """_resolve_dek_on_unlock's legacy branch must not rebuild the keyring
    from scratch. Anything already in it -- including the service wraps -- has
    to survive."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    v.enroll_service_wrap(sess.session_id)

    # Simulate the legacy state: a keyring with the service wraps but no "pin".
    kr_file = tmp_path / "vault" / ".vault_keyring.json"
    keyring = json.loads(kr_file.read_text(encoding="utf-8"))
    keyring.pop("pin")
    kr_file.write_text(json.dumps(keyring), encoding="utf-8")

    fresh = Vault(vault_dir=str(tmp_path / "vault"))
    fresh.authenticate(PIN)          # takes the legacy-migration branch
    after = json.loads(kr_file.read_text(encoding="utf-8"))
    assert "pin" in after
    assert "service" in after
    assert "service_by_dek" in after


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX permission bits are not meaningful here")
def test_a_group_readable_key_is_refused_too(tmp_path, monkeypatch):
    """0640 root:puffpai would let a compromised service user read
    the raw /etc key file, which is exactly what the systemd-credential path
    exists to deny. The claim has to be true, not nearly true."""
    path = tmp_path / "group.key"
    path.write_text(FAKE_MACHINE_KEY, encoding="utf-8")
    os.chmod(path, 0o640)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv(vault_mod._SERVICE_KEY_ENV, str(path))
    vault_mod._service_key_warned.clear()
    secret, reason = vault_mod.read_service_key()
    assert secret is None
    assert "beyond its owner" in reason


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX permission bits are not meaningful here")
def test_the_modes_the_installer_and_systemd_actually_produce_are_accepted():
    """0600 (installer) and 0400 (systemd credential) must both pass, or the
    tightened check would break the very paths it is meant to protect."""
    import tempfile
    for mode in (0o600, 0o400):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "k"
            path.write_text(FAKE_MACHINE_KEY, encoding="utf-8")
            os.chmod(path, mode)
            os.environ.pop("CREDENTIALS_DIRECTORY", None)
            os.environ[vault_mod._SERVICE_KEY_ENV] = str(path)
            vault_mod._service_key_warned.clear()
            secret, source = vault_mod.read_service_key()
            assert secret == FAKE_MACHINE_KEY, oct(mode)
    os.environ.pop(vault_mod._SERVICE_KEY_ENV, None)


# -- the keyring lock ---------------------------------------------------------

def test_concurrent_enrol_and_recovery_never_drop_an_entry(tmp_path,
                                                           machine_key):
    """Concurrent keyring writers, raced.

    enroll_service_wrap and generate_recovery_code are both read-modify-write
    on the keyring. Raced without a lock, an entry is lost almost every time,
    often the RECOVERY wrap, with generate_recovery_code still handing the
    owner a code as though it were enrolled. That is a lockout with a delay
    fuse on it.
    """
    import threading

    for _ in range(8):
        vdir = tmp_path / ("v%d" % _)
        v = Vault(vault_dir=str(vdir))
        v.initialize(PIN)
        sess = v.authenticate(PIN)

        # Two instances over the same directory == two writers.
        other = Vault(vault_dir=str(vdir))
        other_sess = other.authenticate(PIN)

        errors = []

        def enrol():
            try:
                assert v.enroll_service_wrap(sess.session_id) is True
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def recover():
            try:
                other.generate_recovery_code(other_sess.session_id)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=enrol),
                   threading.Thread(target=recover)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], errors
        keyring = json.loads(
            (vdir / ".vault_keyring.json").read_text(encoding="utf-8"))
        # Both writers reported success, so BOTH entries must be there.
        assert "service" in keyring, "the service wrap was lost"
        assert "recovery" in keyring, "the recovery wrap was lost"
        assert "pin" in keyring
        # And the wraps still work, not merely exist.
        cold = Vault(vault_dir=str(vdir))
        assert cold.has_service_wrap is True
        assert cold.has_recovery_code is True


def test_the_lock_is_reentrant(tmp_path, machine_key):
    """enroll_service_wrap holds it and calls _sdk_from_session, which takes it
    again. A non-reentrant lock would deadlock the migration."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    assert v.enroll_service_wrap(sess.session_id) is True
    with v._keyring_lock:
        with v._keyring_lock:
            assert v.has_service_wrap is True


def test_a_write_that_cannot_get_the_lock_fails_rather_than_racing(
        tmp_path, machine_key, monkeypatch):
    """Fail closed on contention. Proceeding unlocked is how the recovery wrap
    got dropped in the first place."""
    v = _vault(tmp_path)
    sess = v.authenticate(PIN)
    monkeypatch.setattr(vault_mod, "_KEYRING_LOCK_TIMEOUT", 0.05)

    lock = v._keyring_lock

    def _refuse():
        raise VaultError("another vault operation is holding the keyring lock")

    monkeypatch.setattr(lock, "_acquire_os_lock", _refuse)
    with pytest.raises(VaultError):
        v.generate_recovery_code(sess.session_id)
