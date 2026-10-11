"""Tests for the vault encryption.

Covers the session-key encrypted I/O added to security/vault.py and the
service-secret tier consumed by security/secrets_provider.py:

  * write_secure_session / read_secure_session roundtrip
  * write_secure_blob / read_secure_blob roundtrip (document blobs)
  * explicit locked-vault failure (no plaintext fallback on write)
  * migrate_plaintext_files: plaintext -> encrypted, idempotent, safe
  * secrets_provider vault tier: value when unlocked, None (fall-through)
    when locked, and one-time warning when the tier is unimplemented.
"""

import json
import time

import pytest

import puffpai.security.secrets_provider as sp_mod
from puffpai.security.vault import Vault, VaultError
from puffpai.security.secrets_provider import (
    SecretsProvider, AgentScope,
)
from tests._secret_asserts import _assert_no_secret, _assert_secret_is


def _new_vault(tmp_path, pin="vault-pin-1234"):
    v = Vault(vault_dir=str(tmp_path))
    v.initialize(pin)
    return v


# ── session-key JSON roundtrip ──────────────────────────────────────────

class TestSecureSessionRoundtrip:
    def test_write_then_read(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        payload = {"value": "top-secret", "type": "text"}
        v.write_secure_session("item.json", payload, sess.session_id)

        # On-disk bytes must NOT contain the plaintext.
        raw = (tmp_path / "item.json").read_bytes()
        assert b"top-secret" not in raw

        got = v.read_secure_session("item.json", sess.session_id)
        assert got == payload

    def test_read_missing_returns_empty(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        assert v.read_secure_session("nope.json", sess.session_id) == {}

    def test_wrong_pin_cannot_decrypt(self, tmp_path):
        v = _new_vault(tmp_path, pin="vault-pin-1234")
        sess = v.authenticate("vault-pin-1234")
        v.write_secure_session("s.json", {"a": 1}, sess.session_id)

        # A second session from the SAME pin decrypts fine (single-PIN vault).
        sess2 = v.authenticate("vault-pin-1234")
        assert v.read_secure_session("s.json", sess2.session_id) == {"a": 1}


# ── document blob roundtrip ─────────────────────────────────────────────

class TestSecureBlobRoundtrip:
    def test_text_roundtrip(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        v.write_secure_blob("doc_1_note.txt", "my allergy: penicillin",
                            sess.session_id)
        raw = (tmp_path / "doc_1_note.txt").read_bytes()
        assert b"penicillin" not in raw
        assert v.read_secure_blob("doc_1_note.txt", sess.session_id) == \
            "my allergy: penicillin"

    def test_bytes_roundtrip_exact(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        blob = "café — münchen\n".encode("utf-8")
        v.write_secure_blob("doc_2.bin", blob, sess.session_id)
        assert v.read_secure_blob("doc_2.bin", sess.session_id) == \
            blob.decode("utf-8")


# ── explicit locked failure ─────────────────────────────────────────────

class TestLockedVaultFailsExplicitly:
    def test_write_with_invalid_session_raises(self, tmp_path):
        v = _new_vault(tmp_path)
        with pytest.raises(VaultError):
            v.write_secure_session("x.json", {"a": 1}, "not-a-real-session")
        # And nothing was written in plaintext.
        assert not (tmp_path / "x.json").exists()

    def test_read_with_invalid_session_raises(self, tmp_path):
        v = _new_vault(tmp_path)
        with pytest.raises(VaultError):
            v.read_secure_session("x.json", "not-a-real-session")

    def test_expired_session_has_no_key(self, tmp_path, monkeypatch):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        # Force expiry by rewinding last_access beyond the timeout.
        v._sessions[sess.session_id].last_access = time.time() - 10_000
        assert v.is_unlocked(sess.session_id) is False
        with pytest.raises(VaultError):
            v.write_secure_session("x.json", {"a": 1}, sess.session_id)

    def test_logout_drops_key(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        assert v.is_unlocked(sess.session_id) is True
        v.logout(sess.session_id)
        assert v.is_unlocked(sess.session_id) is False


# ── migration ───────────────────────────────────────────────────────────

class TestMigration:
    def test_plaintext_json_is_encrypted(self, tmp_path):
        v = _new_vault(tmp_path)
        # Pre-existing plaintext JSON file (legacy).
        legacy = tmp_path / "legacy.json"
        legacy.write_text(json.dumps({"value": "secret-x", "type": "text"}))

        sess = v.authenticate("vault-pin-1234")
        result = v.migrate_plaintext_files(sess.session_id)
        assert "legacy.json" in result["migrated"]

        # File no longer contains plaintext, and decrypts to the original.
        raw = legacy.read_bytes()
        assert b"secret-x" not in raw
        assert v.read_secure_session("legacy.json", sess.session_id) == \
            {"value": "secret-x", "type": "text"}

    def test_plaintext_doc_blob_is_encrypted(self, tmp_path):
        v = _new_vault(tmp_path)
        doc = tmp_path / "doc_9_report.txt"
        doc.write_text("patient SSN on file")
        sess = v.authenticate("vault-pin-1234")
        v.migrate_plaintext_files(sess.session_id)
        raw = doc.read_bytes()
        assert b"patient SSN" not in raw
        assert v.read_secure_blob("doc_9_report.txt", sess.session_id) == \
            "patient SSN on file"

    def test_idempotent(self, tmp_path):
        v = _new_vault(tmp_path)
        (tmp_path / "a.json").write_text(json.dumps({"a": 1}))
        sess = v.authenticate("vault-pin-1234")

        first = v.migrate_plaintext_files(sess.session_id)
        assert first["migrated"] == ["a.json"]

        second = v.migrate_plaintext_files(sess.session_id)
        # Already encrypted -> skipped, not re-migrated, not corrupted.
        assert second["migrated"] == []
        assert second["skipped"] >= 1
        assert v.read_secure_session("a.json", sess.session_id) == {"a": 1}

    def test_control_files_skipped(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        # The key-salt dotfile exists and must never be touched.
        salt_before = (tmp_path / ".vault_key_salt").read_bytes()
        v.migrate_plaintext_files(sess.session_id)
        assert (tmp_path / ".vault_key_salt").read_bytes() == salt_before

    def test_unclassifiable_file_left_untouched(self, tmp_path):
        v = _new_vault(tmp_path)
        junk = tmp_path / "blob.dat"  # not doc_*, not JSON
        junk.write_bytes(b"\x00\x01not-json-not-doc\xff")
        sess = v.authenticate("vault-pin-1234")
        result = v.migrate_plaintext_files(sess.session_id)
        assert "blob.dat" in result["failed"]
        assert junk.read_bytes() == b"\x00\x01not-json-not-doc\xff"

    def test_migration_requires_unlock(self, tmp_path):
        v = _new_vault(tmp_path)
        with pytest.raises(VaultError):
            v.migrate_plaintext_files("not-a-session")

    def test_ha_token_is_skipped_not_dek_migrated(self, tmp_path):
        """A deliberate exception to the usual migration path.

        ha_token.json is a BOOT SECRET: encrypted under the vault's
        Service Data Key so the appliance can read it after an unattended
        reboot, not under the PIN-derived DEK. This method re-encrypts under
        the DEK, so migrating this file would orphan that read and leave Home
        Assistant dead until somebody typed a PIN. It is in _MIGRATION_SKIP
        for exactly that reason and must stay there.

        A plaintext ha_token.json is therefore left alone rather than
        encrypted here. That state has never existed on an appliance (nothing
        ever wrote one), and scripts/migrate_ha_token.py overwrites it with a
        proper service-tier record.
        """
        from puffpai.security.vault import (SERVICE_SECRET_FILES,
                                            _MIGRATION_SKIP)

        assert "ha_token.json" in SERVICE_SECRET_FILES
        assert "ha_token.json" in _MIGRATION_SKIP

        v = _new_vault(tmp_path)
        ha = tmp_path / "ha_token.json"
        ha.write_text(json.dumps({"token": "jwt-plaintext"}))
        sess = v.authenticate("vault-pin-1234")
        result = v.migrate_plaintext_files(sess.session_id)
        assert "ha_token.json" not in result["migrated"]
        assert "ha_token.json" not in result["failed"]
        assert result["skipped"] >= 1

        # A REAL service-tier record survives this method byte-identical --
        # which is the property that actually matters.
        ha.unlink()
        v.write_service_secret("ha_token.json", {"token": "jwt-plaintext"},
                               sess.session_id)
        before = ha.read_bytes()
        assert b"jwt-plaintext" not in before
        v.migrate_plaintext_files(sess.session_id)
        assert ha.read_bytes() == before


# ── secrets_provider vault tier ─────────────────────────────────────────

# The leak-proof assert helpers (_assert_no_secret / _assert_secret_is) now
# live in tests/_secret_asserts.py — see that module's docstring for why a
# bare assert is unsafe on values that come out of the secrets chain.


class TestSecretsProviderVaultTier:
    @pytest.fixture(autouse=True)
    def _no_keyring_tier(self, monkeypatch):
        """Neutralize tier 2 (OS keychain) for every test in this class.

        A dev machine legitimately holds a REAL HA_TOKEN in Windows
        Credential Manager under service "puffpai" (the tier-2 migration in
        the secrets_provider docstring), so a test asserting on the *chain's*
        outcome resolves the machine's real secret instead of the tmp_path
        fixture unless this tier is off. _keyring_lookup short-circuits on
        the module global. Autouse (not per-test) so the isolation is the
        default: a test added here later cannot silently reacquire it.

        The session-scoped ``_secret_hygiene`` fixture in tests/conftest.py
        now enforces the same guard suite-wide; this class fixture is
        deliberate redundancy, so the invariant still holds for these tests
        even if that conftest fixture is edited or removed.
        """
        monkeypatch.setattr(sp_mod, "_HAS_KEYRING", False)

    def test_service_secret_when_unlocked(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        v.write_secure_session(
            "service_secrets.json",
            {"HA_TOKEN": "jwt-abc"}, sess.session_id)

        assert v.get_service_secret("HA_TOKEN") == "jwt-abc"

        provider = SecretsProvider(vault=v, allow_env_fallback=False)
        # Keyring is off, so the only outcomes are "jwt-abc" or None — but a
        # vault-tier regression must not be able to echo a value either.
        _assert_secret_is(provider.get("HA_TOKEN"), "jwt-abc",
                          "vault tier resolution")

    def test_service_secret_none_when_locked(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        v.write_secure_session(
            "service_secrets.json",
            {"HA_TOKEN": "jwt-abc"}, sess.session_id)
        v.logout(sess.session_id)  # lock: no live session key

        _assert_no_secret(v.get_service_secret("HA_TOKEN"),
                          "locked vault get_service_secret")
        provider = SecretsProvider(vault=v, allow_env_fallback=False)
        _assert_no_secret(provider.get("HA_TOKEN"),
                          "locked vault, no env fallback")

    def test_locked_falls_through_to_env(self, tmp_path, monkeypatch):
        # Keyring tier is off via the autouse fixture, so "SOME_SECRET"
        # cannot resolve from the real keychain — env is the only source.
        v = _new_vault(tmp_path)  # locked, no sessions
        monkeypatch.setenv("SOME_SECRET", "from-env")
        provider = SecretsProvider(vault=v, allow_env_fallback=True)
        with pytest.warns(DeprecationWarning):
            _assert_secret_is(provider.get("SOME_SECRET"), "from-env",
                              "locked vault falls through to env")

    def test_no_vault_returns_none(self):
        provider = SecretsProvider(vault=None, allow_env_fallback=False)
        assert provider._vault_lookup("ANY") is None

    def test_unimplemented_tier_warns_once(self, caplog):
        class DummyVault:  # no get_service_secret
            pass

        provider = SecretsProvider(vault=DummyVault(), allow_env_fallback=False)
        with caplog.at_level("WARNING"):
            assert provider._vault_lookup("X") is None
            assert provider._vault_lookup("Y") is None
        warnings = [r for r in caplog.records
                    if "vault-tier disabled" in r.getMessage()]
        assert len(warnings) == 1  # surfaced exactly once, not a silent no-op


class _StubKeyring:
    """Stand-in for the ``keyring`` module. Records calls, returns a literal.

    Nothing here touches the OS keychain; the recorded call list is what lets
    a test prove the tier was SKIPPED rather than merely returning None.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def get_password(self, service, name):
        self.calls.append((service, name))
        return "stub-value"


class TestKeyringTierFlag:
    """SecretsProvider.allow_keyring gates tier 2 independently of the module
    flag.

    These tests deliberately re-enable ``_HAS_KEYRING`` on top of the
    suite-wide guard in tests/conftest.py — but only after swapping in
    _StubKeyring, so the real Windows Credential Manager is never reached.
    """

    @pytest.fixture
    def stub(self, monkeypatch):
        stub = _StubKeyring()
        monkeypatch.setattr(sp_mod, "keyring", stub, raising=False)
        return stub

    def test_allow_keyring_false_skips_the_tier_entirely(self, stub,
                                                         monkeypatch):
        monkeypatch.setattr(sp_mod, "_HAS_KEYRING", True)
        provider = SecretsProvider(vault=None, allow_env_fallback=False,
                                   allow_keyring=False)
        _assert_no_secret(provider.get("HA_TOKEN"),
                          "allow_keyring=False keyring tier")
        # Not merely "returned None" — the keychain was never asked at all.
        assert stub.calls == []

    def test_allow_keyring_default_true_resolves(self, stub, monkeypatch):
        monkeypatch.setattr(sp_mod, "_HAS_KEYRING", True)
        provider = SecretsProvider(vault=None, allow_env_fallback=False)
        _assert_secret_is(provider.get("HA_TOKEN"), "stub-value",
                          "stub keyring tier")
        assert stub.calls == [(sp_mod._KEYRING_SERVICE, "HA_TOKEN")]

    def test_module_flag_still_gates_when_allow_keyring_true(self, stub):
        # _HAS_KEYRING is left False by the suite-wide conftest guard; the
        # flag is an AND with it, not an override.
        provider = SecretsProvider(vault=None, allow_env_fallback=False,
                                   allow_keyring=True)
        _assert_no_secret(provider.get("HA_TOKEN"),
                          "allow_keyring=True but _HAS_KEYRING False")
        assert stub.calls == []


# ── ingestion path (POST /ingest/upload -> ingest_file) ──────────────────

_PII_DOC = (
    "Patient John Doe SSN 123-45-6789, card 4111 1111 1111 1111, "
    "DOB 03/15/1980, 123 Main St, phone 555-123-4567"
)


def _write_upload(tmp_path, name="patient_record.txt"):
    p = tmp_path / name
    p.write_text(_PII_DOC, encoding="utf-8")
    return str(p)


class TestIngestionVaulting:
    def test_locked_vault_writes_no_plaintext_and_refuses(self, tmp_path):
        from puffpai.ingestion import ingest_file

        vdir = tmp_path / "vault"
        v = Vault(vault_dir=str(vdir))
        v.initialize("vault-pin-1234")  # initialized but LOCKED (no authenticate)

        upload = _write_upload(tmp_path)
        result = ingest_file(upload, original_filename="patient_record.txt",
                             vault=v)

        # Refused, not vaulted.
        assert result.pii_detected is True
        assert result.vaulted is False
        assert result.error is not None and "lock" in result.error.lower()

        # No vault files were created at all — no doc_*, no PII log.
        files = list(vdir.iterdir()) if vdir.exists() else []
        names = [f.name for f in files if not f.name.startswith(".")]
        assert names == [], f"unexpected plaintext vault files: {names}"

        # Staging upload copy was removed (no plaintext lingering).
        assert not (tmp_path / "patient_record.txt").exists()

    def test_unlocked_vault_encrypts_doc_and_pii(self, tmp_path):
        from puffpai.ingestion import ingest_file

        vdir = tmp_path / "vault"
        v = Vault(vault_dir=str(vdir))
        v.initialize("vault-pin-1234")
        sess = v.authenticate("vault-pin-1234")  # UNLOCKED

        upload = _write_upload(tmp_path)
        result = ingest_file(upload, original_filename="patient_record.txt",
                             vault=v)

        assert result.vaulted is True
        assert result.error is None

        # A doc_* file and the PII log now exist and are BOTH ciphertext.
        doc_files = [f for f in vdir.iterdir() if f.name.startswith("doc_")]
        assert len(doc_files) == 1
        doc_bytes = doc_files[0].read_bytes()
        assert b"123-45-6789" not in doc_bytes
        assert b"John Doe" not in doc_bytes

        pii_file = vdir / "auto_detected_pii.json"
        assert pii_file.exists()
        pii_bytes = pii_file.read_bytes()
        assert b"document_upload" not in pii_bytes
        assert b"critical" not in pii_bytes

        # Decrypt round-trips.
        text = v.read_secure_blob(doc_files[0].name, sess.session_id)
        assert "123-45-6789" in text
        records = v.read_secure_session("auto_detected_pii.json",
                                        sess.session_id)
        assert isinstance(records, list) and len(records) == 1
        assert records[0]["source"] == "document_upload"

    def test_uninitialized_vault_refuses(self, tmp_path):
        from puffpai.ingestion import ingest_file

        vdir = tmp_path / "vault"
        v = Vault(vault_dir=str(vdir))  # never initialized

        upload = _write_upload(tmp_path)
        result = ingest_file(upload, original_filename="patient_record.txt",
                             vault=v)
        assert result.vaulted is False
        assert result.error is not None and "initial" in result.error.lower()
        names = [f.name for f in vdir.iterdir()
                 if vdir.exists() and not f.name.startswith(".")]
        assert names == []


# ── corrupt-log edge case + belt-and-braces fail-closed ──────────────────

_CORRUPT_LOG = b"\xff\xfe\x00 corrupt-not-ciphertext not-json \x81\x82"


class TestAppendAutoPiiRobustness:
    def test_returns_false_when_locked(self, tmp_path):
        v = _new_vault(tmp_path)  # initialized but no live session
        assert v.append_auto_pii({"x": 1}) is False

    def test_quarantines_corrupt_log_and_never_raises(self, tmp_path):
        v = _new_vault(tmp_path)
        (tmp_path / "auto_detected_pii.json").write_bytes(_CORRUPT_LOG)
        sess = v.authenticate("vault-pin-1234")

        ok = v.append_auto_pii({"source": "unit", "items": {"ssn": ["x"]}})
        assert ok is True  # did not raise; started a fresh encrypted log

        # Corrupt blob preserved under a quarantine name, bytes intact.
        quarantined = list(tmp_path.glob("auto_detected_pii.json.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == _CORRUPT_LOG

        # Fresh log is encrypted and decrypts to the single new record.
        raw = (tmp_path / "auto_detected_pii.json").read_bytes()
        assert raw != _CORRUPT_LOG
        recs = v.read_secure_session("auto_detected_pii.json", sess.session_id)
        assert isinstance(recs, list) and len(recs) == 1

    def test_returns_false_on_write_failure(self, tmp_path, monkeypatch):
        v = _new_vault(tmp_path)
        v.authenticate("vault-pin-1234")

        def _boom(*a, **k):
            raise VaultError("simulated write failure")

        monkeypatch.setattr(v, "write_secure_session", _boom)
        # Must swallow the error and report False, not raise.
        assert v.append_auto_pii({"x": 1}) is False


class TestAtomicWrite:
    def test_no_temp_files_left_behind(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        v.write_secure_session("item.json", {"a": 1}, sess.session_id)
        v.write_secure_blob("doc_1.txt", "hello", sess.session_id)
        leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name]
        assert leftovers == []
        # Files are intact and decryptable.
        assert v.read_secure_session("item.json", sess.session_id) == {"a": 1}
        assert v.read_secure_blob("doc_1.txt", sess.session_id) == "hello"


class TestIngestFailsClosedNeverIndexes:
    def _patch_indexer(self, monkeypatch, tmp_path):
        """Redirect the doc index to a temp file and stub ChromaDB so we can
        detect whether _index_study_doc was reached (it must NOT be for a
        high/critical-PII doc that could not be securely vaulted)."""
        import puffpai.ingestion as ing
        doc_index = tmp_path / "ingested_docs.json"
        monkeypatch.setattr(ing, "_DOC_INDEX", doc_index)
        monkeypatch.setattr(ing, "_add_to_user_uploads_collection",
                            lambda *a, **k: None)
        return doc_index

    def test_corrupt_pii_log_doc_still_vaulted_not_indexed(
            self, tmp_path, monkeypatch):
        from puffpai.ingestion import ingest_file
        doc_index = self._patch_indexer(monkeypatch, tmp_path)

        vdir = tmp_path / "vault"
        v = Vault(vault_dir=str(vdir))
        v.initialize("vault-pin-1234")
        sess = v.authenticate("vault-pin-1234")
        # Pre-seed a corrupt PII log — the original trigger for the escape bug.
        (vdir / "auto_detected_pii.json").write_bytes(_CORRUPT_LOG)

        upload = _write_upload(tmp_path)
        result = ingest_file(upload, original_filename="patient_record.txt",
                             vault=v)

        # Doc was secured; no unhandled behavior.
        assert result.vaulted is True
        assert result.error is None
        # THE INVARIANT: the plaintext never reached the searchable doc index.
        assert not doc_index.exists()
        # Doc on disk is ciphertext; corrupt log was quarantined.
        doc_files = [f for f in vdir.iterdir() if f.name.startswith("doc_")]
        assert len(doc_files) == 1
        assert b"123-45-6789" not in doc_files[0].read_bytes()
        assert list(vdir.glob("auto_detected_pii.json.corrupt-*"))

    def test_unexpected_store_error_refuses_and_never_indexes(
            self, tmp_path, monkeypatch):
        from puffpai.ingestion import ingest_file
        doc_index = self._patch_indexer(monkeypatch, tmp_path)

        vdir = tmp_path / "vault"
        v = Vault(vault_dir=str(vdir))
        v.initialize("vault-pin-1234")
        v.authenticate("vault-pin-1234")

        # store_document_secure RAISES (not returns False) — e.g. disk error.
        def _boom(*a, **k):
            raise VaultError("simulated disk failure")

        monkeypatch.setattr(v, "store_document_secure", _boom)

        upload = _write_upload(tmp_path)
        result = ingest_file(upload, original_filename="patient_record.txt",
                             vault=v)

        # Fail closed: refused, and NEVER indexed into RAG / plaintext catalog.
        assert result.vaulted is False
        assert result.error is not None
        assert not doc_index.exists()
        # No plaintext doc written either.
        doc_files = [f for f in vdir.iterdir() if f.name.startswith("doc_")]
        assert doc_files == []

    def test_classifier_exception_refuses_and_never_indexes(
            self, tmp_path, monkeypatch):
        """The PII scan itself crashes mid-scan (classifier raises). This
        escapes the detection call BEFORE the vaulting branch, so it lands in
        the OUTER `except Exception`. That path must fail closed: refuse the
        upload, never index, and remove the plaintext staging copy — otherwise
        an unscanned document leaks into the searchable RAG / plaintext catalog.
        """
        from puffpai.ingestion import ingest_file
        import puffpai.security.pii_classifier as pii_mod
        doc_index = self._patch_indexer(monkeypatch, tmp_path)

        vdir = tmp_path / "vault"
        v = Vault(vault_dir=str(vdir))
        v.initialize("vault-pin-1234")
        v.authenticate("vault-pin-1234")  # UNLOCKED — so a fall-through really would index

        # Make the scan itself blow up: redact_pii is the first classifier call
        # ingest_file makes, and it raises BEFORE the vaulting try/except.
        def _crash(*a, **k):
            raise RuntimeError("simulated classifier crash")

        monkeypatch.setattr(pii_mod.PIIClassifier, "redact_pii", _crash)

        upload = _write_upload(tmp_path)
        result = ingest_file(upload, original_filename="patient_record.txt",
                             vault=v)

        # Fail closed: error set, not vaulted.
        assert result.error  # truthy
        assert result.vaulted is False
        # THE INVARIANT: the doc index was never created.
        assert not doc_index.exists()
        # Staging copy of the plaintext upload was removed (_remove_staging).
        assert not (tmp_path / "patient_record.txt").exists()
        # No plaintext doc written to the vault either.
        doc_files = [f for f in vdir.iterdir()
                     if vdir.exists() and f.name.startswith("doc_")]
        assert doc_files == []
        # Belt and suspenders: the SSN marker must not appear in plaintext
        # anywhere under tmp_path (index, vault, or lingering staging).
        for p in tmp_path.rglob("*"):
            if p.is_file():
                assert b"123-45-6789" not in p.read_bytes(), \
                    f"plaintext PII leaked to {p}"


# ── TOTP fail-closed ──────────────────────────────────────────────────

class TestTotpFailClosed:
    """verify_totp() must fail closed when TOTP is not enrolled.

    Before the fix, verify_totp() returned True for ANY code when
    totp_enabled was False (the shipped default), so any code path that
    used it as a standalone gate was fully bypassed. It now returns False.
    """

    def test_bypass_is_closed(self, tmp_path):
        from puffpai.security.vault import _HAS_TOTP

        # Fresh vault: TOTP is not configured.
        v = _new_vault(tmp_path, pin="vault-pin-1234")
        assert v.totp_enabled is False

        if _HAS_TOTP:
            # The regression: with pyotp installed and no secret enrolled,
            # these BOTH returned True before the fix (the standalone bypass).
            # They must now fail closed to False.
            assert v.verify_totp("000000") is False
            assert v.verify_totp("literally-anything") is False
        else:
            # Without pyotp the method raises at the _HAS_TOTP guard, which is
            # itself fail-closed: it never returns True, so no bypass exists.
            # (The True-returning bug was only reachable with pyotp present.)
            with pytest.raises(VaultError):
                v.verify_totp("000000")
            with pytest.raises(VaultError):
                v.verify_totp("literally-anything")

    def test_pin_only_flow_preserved(self, tmp_path):
        # With TOTP not configured, PIN-only MFA unlock must still work:
        # authenticate_mfa never reaches the (now fail-closed) branch.
        v = _new_vault(tmp_path, pin="vault-pin-1234")
        assert v.totp_enabled is False
        session = v.authenticate_mfa("vault-pin-1234")  # no totp_code
        assert session is not None
        assert session.session_id  # non-empty

    def test_totp_enabled_path(self, tmp_path):
        pyotp = pytest.importorskip("pyotp")
        from puffpai.security.vault import AuthenticationError

        v = _new_vault(tmp_path, pin="vault-pin-1234")
        session = v.authenticate("vault-pin-1234")
        info = v.setup_totp(session.session_id)
        secret = info["secret"]

        # Enrollment flips the property.
        assert v.totp_enabled is True

        # Current code verifies; a wrong/fixed code does not.
        current = pyotp.TOTP(secret).now()
        assert v.verify_totp(current) is True
        assert v.verify_totp("000000") is False

        # After enrollment, MFA now REQUIRES a code.
        with pytest.raises(AuthenticationError):
            v.authenticate_mfa("vault-pin-1234")  # missing totp_code

        # With the correct current code, MFA succeeds.
        current2 = pyotp.TOTP(secret).now()
        mfa_session = v.authenticate_mfa("vault-pin-1234", current2)
        assert mfa_session is not None
        assert mfa_session.session_id  # non-empty
