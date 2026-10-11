"""Zone-1 PIN-gate hardening tests.

Proves the fail-closed invariants of the vault's Zone-1 gate:

  * Zone-1 vault read/write and auto-PII append are REFUSED when the vault is
    locked, and SUCCEED after a correct-PIN unlock; a wrong PIN never unlocks.
  * The dual-zone RAG never touches the Zone-1 (``puffpai_vault``) collection
    unless explicitly authenticated, while Zone-2 (``puffpai_knowledge``) is
    queryable with the vault locked.
  * The Archivist agent — the sole Zone-1 holder — only opens the vault zone
    for a VERIFIED-unlocked session, not a bare ``vault_session_id`` string.
  * The HA-token plaintext fallback (a Zone-1 secret) honors its own config
    flag: withheld when disabled, available by default.
  * The unlock flow is pluggable for a future second factor (PIN stays the
    mandatory first factor) without breaking PIN-only unlock.

Run: pytest tests/test_zone1_gate.py  (see pytest.ini / tests/conftest.py).
"""

import pytest

from puffpai.security.vault import Vault, VaultError, AuthenticationError


def _new_vault(tmp_path, pin="vault-pin-1234"):
    v = Vault(vault_dir=str(tmp_path))
    v.initialize(pin)
    return v


# ── Zone-1 vault read/write is PIN-gated (fail closed when locked) ──────────

class TestZone1VaultGate:
    def test_write_refused_when_locked(self, tmp_path):
        v = _new_vault(tmp_path)  # initialized but NOT unlocked (no session)
        with pytest.raises(VaultError):
            v.write_secure_session("secret.json", {"a": 1}, "bogus-session")
        assert not (tmp_path / "secret.json").exists()

    def test_read_refused_when_locked(self, tmp_path):
        v = _new_vault(tmp_path)
        with pytest.raises(VaultError):
            v.read_secure_session("secret.json", "bogus-session")

    def test_blob_read_refused_when_locked(self, tmp_path):
        v = _new_vault(tmp_path)
        with pytest.raises(VaultError):
            v.read_secure_blob("doc_1.txt", "bogus-session")

    def test_auto_pii_append_refused_when_locked(self, tmp_path):
        # Zone-1 write via the no-session writer path must refuse (never
        # write plaintext) when there is no live session.
        v = _new_vault(tmp_path)
        assert v.append_auto_pii({"source": "unit", "x": 1}) is False
        assert not (tmp_path / "auto_detected_pii.json").exists()

    def test_reads_and_writes_succeed_after_unlock(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        v.write_secure_session("secret.json", {"a": 1}, sess.session_id)
        assert v.read_secure_session("secret.json", sess.session_id) == {"a": 1}
        # On-disk bytes are ciphertext, not the plaintext value marker.
        assert b'"a"' not in (tmp_path / "secret.json").read_bytes()
        # Auto-PII append now persists (encrypted).
        assert v.append_auto_pii({"source": "unit", "x": 1}) is True

    def test_wrong_pin_does_not_unlock(self, tmp_path):
        v = _new_vault(tmp_path, pin="vault-pin-1234")
        with pytest.raises(AuthenticationError):
            v.authenticate("9999")
        # No live session was created by the failed attempt.
        assert v.has_live_session is False

    def test_locked_after_logout(self, tmp_path):
        v = _new_vault(tmp_path)
        sess = v.authenticate("vault-pin-1234")
        assert v.is_unlocked(sess.session_id) is True
        v.logout(sess.session_id)
        assert v.is_unlocked(sess.session_id) is False
        with pytest.raises(VaultError):
            v.write_secure_session("x.json", {"a": 1}, sess.session_id)


# ── Dual-zone RAG: Zone-2 open when locked, Zone-1 only when authenticated ──

class _FakeCollection:
    def __init__(self, name, docs):
        self._name = name
        self._docs = docs
        self.queried = False

    def query(self, query_embeddings=None, n_results=5, **kw):
        self.queried = True
        n = len(self._docs)
        return {
            "documents": [self._docs],
            "distances": [[0.1] * n],
            "metadatas": [[{"title": f"{self._name}"} for _ in range(n)]],
        }


@pytest.fixture
def _stub_embeddings(monkeypatch):
    # Avoid loading a real embedding model; _query_collection embeds the query.
    import puffpai.embeddings as emb
    monkeypatch.setattr(emb, "embed_documents", lambda texts: [[0.0, 0.0]])
    return emb


class TestDualZoneRagGate:
    def _rag(self):
        from puffpai.knowledge.rag import DualZoneRAG
        vault_coll = _FakeCollection("vault", ["MY PRIVATE MEDICAL NOTE"])
        know_coll = _FakeCollection("knowledge", ["general fact"])
        return DualZoneRAG(vault_collection=vault_coll,
                           knowledge_collection=know_coll), vault_coll, know_coll

    def test_knowledge_queryable_when_locked(self, _stub_embeddings):
        rag, vault_coll, know_coll = self._rag()
        resp = rag.query("anything", authenticated=False)
        assert know_coll.queried is True          # Zone-2 available
        assert vault_coll.queried is False        # Zone-1 never touched
        assert resp.knowledge_count > 0
        assert resp.vault_count == 0

    def test_default_is_unauthenticated(self, _stub_embeddings):
        # No authenticated= arg at all must NOT open the vault collection.
        rag, vault_coll, know_coll = self._rag()
        rag.query("anything")
        assert vault_coll.queried is False

    def test_vault_queried_only_when_authenticated(self, _stub_embeddings):
        rag, vault_coll, know_coll = self._rag()
        resp = rag.query("anything", authenticated=True)
        assert vault_coll.queried is True
        assert resp.vault_count > 0


# ── Archivist agent: Zone-1 requires a VERIFIED-unlocked session ────────────

class _RecordingRAG:
    def __init__(self):
        self.calls = []

    def query(self, q, n_results=5, authenticated=False,
              knowledge_only=False, vault_only=False):
        self.calls.append({"authenticated": authenticated,
                           "knowledge_only": knowledge_only})

        class _R:
            vault_count = 1 if authenticated and not knowledge_only else 0
            knowledge_count = 2

            def context_string(self, max_results=8):
                return "ctx"
        return _R()


class _FakeVault:
    def __init__(self, unlocked_ids=()):
        self._unlocked = set(unlocked_ids)

    def is_unlocked(self, session_id):
        return session_id in self._unlocked


def _archivist(rag, vault=None):
    from puffpai.agents.archivist import ArchivistAgent
    return ArchivistAgent(rag=rag, vault=vault)


def _retrieve(arch, **payload):
    from puffpai.agents.envelope import Envelope
    env = Envelope(intent="retrieve",
                   payload={"query": "x", **payload},
                   requester=payload.get("profile", "owner"))
    return arch.handle(env)


class TestArchivistZoneGate:
    def test_unlocked_session_opens_vault(self):
        rag = _RecordingRAG()
        arch = _archivist(rag, vault=_FakeVault(unlocked_ids={"sess"}))
        reply = _retrieve(arch, profile="owner", vault_session_id="sess")
        assert "vault" in reply["zones_queried"]
        assert rag.calls[-1]["authenticated"] is True

    def test_locked_session_stays_zone2(self):
        rag = _RecordingRAG()
        arch = _archivist(rag, vault=_FakeVault(unlocked_ids=set()))
        reply = _retrieve(arch, profile="owner", vault_session_id="sess")
        assert reply["zones_queried"] == ["knowledge"]
        assert rag.calls[-1] == {"authenticated": False, "knowledge_only": True}

    def test_no_vault_cannot_verify_stays_zone2(self):
        rag = _RecordingRAG()
        arch = _archivist(rag, vault=None)
        reply = _retrieve(arch, profile="owner", vault_session_id="sess")
        assert reply["zones_queried"] == ["knowledge"]
        assert rag.calls[-1]["authenticated"] is False

    def test_kid_never_authenticated_even_if_unlocked(self):
        rag = _RecordingRAG()
        arch = _archivist(rag, vault=_FakeVault(unlocked_ids={"sess"}))
        reply = _retrieve(arch, profile="child", vault_session_id="sess")
        assert reply["zones_queried"] == ["knowledge"]
        assert rag.calls[-1]["authenticated"] is False


# ── HA-token plaintext fallback flag ─────────────────────────────────────

class TestHaTokenFallbackFlag:
    @pytest.fixture(autouse=True)
    def _fake_config(self, monkeypatch):
        import puffpai.security.ha_vault as hv
        # Never touch the real plaintext config (may hold a live token).
        monkeypatch.setattr(
            hv, "_load_plaintext_config",
            lambda: {"url": "http://x", "token": "fake-jwt", "enabled": True})
        monkeypatch.setattr(hv, "_FALLBACK_WARNED", False, raising=False)
        return hv

    def test_default_allows_fallback(self, monkeypatch, _fake_config):
        monkeypatch.delenv("PUFFPAI_ALLOW_PLAINTEXT_HA_TOKEN_FALLBACK",
                           raising=False)
        # session_id=None -> vault path skipped -> plaintext fallback path.
        assert _fake_config.get_ha_token() == "fake-jwt"

    def test_disabled_withholds_token(self, monkeypatch, _fake_config):
        monkeypatch.setenv("PUFFPAI_ALLOW_PLAINTEXT_HA_TOKEN_FALLBACK", "0")
        assert _fake_config.get_ha_token() is None

    def test_off_word_withholds_token(self, monkeypatch, _fake_config):
        monkeypatch.setenv("PUFFPAI_ALLOW_PLAINTEXT_HA_TOKEN_FALLBACK", "false")
        assert _fake_config.get_ha_token() is None


# ── Pluggable auth-factor seam (PIN mandatory; extensible for voice MFA) ─────

class TestPluggableAuthSeam:
    def test_pin_only_unlock_preserved(self, tmp_path):
        v = _new_vault(tmp_path, pin="vault-pin-1234")
        assert v.required_factors() == ["pin"]
        sess = v.authenticate_mfa("vault-pin-1234")
        assert v.is_unlocked(sess.session_id) is True

    def test_wrong_pin_never_unlocks(self, tmp_path):
        v = _new_vault(tmp_path, pin="vault-pin-1234")
        with pytest.raises(AuthenticationError):
            v.authenticate_mfa("vault-pin-0000")
        assert v.has_live_session is False

    def test_pin_is_not_a_pluggable_factor(self, tmp_path):
        v = _new_vault(tmp_path)
        with pytest.raises(ValueError):
            v.register_auth_factor("pin", lambda code: True)

    def test_registered_required_factor_is_enforced(self, tmp_path, monkeypatch):
        v = _new_vault(tmp_path, pin="vault-pin-1234")
        v.register_auth_factor("voice", lambda code: code == "matched-print")
        # Make the new factor required without needing TOTP enrollment.
        monkeypatch.setattr(v, "required_factors", lambda: ["pin", "voice"])

        # Correct PIN but MISSING the required factor -> refuse, no session.
        with pytest.raises(AuthenticationError):
            v.authenticate_mfa("vault-pin-1234")
        assert v.has_live_session is False

        # Correct PIN but WRONG factor code -> refuse.
        with pytest.raises(AuthenticationError):
            v.authenticate_mfa("vault-pin-1234", factor_codes={"voice": "impostor"})
        assert v.has_live_session is False

        # Correct PIN + correct factor -> unlock.
        sess = v.authenticate_mfa("vault-pin-1234", factor_codes={"voice": "matched-print"})
        assert v.is_unlocked(sess.session_id) is True
