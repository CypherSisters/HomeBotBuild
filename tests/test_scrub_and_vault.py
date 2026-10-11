"""Tests for the native Scrub-and-Vault capability (Increment 1).

Covers the three pieces:

  * the Archivist-owned quality gate (agents/pii_quality_gate.py) — GOOD real
    data vs test/placeholder data;
  * the request-path choke point (security/scrub_and_vault.py) — always scrub,
    vault only the good originals, fail closed;
  * the api_server sink wiring (_prepare_user_message) and the Archivist memory
    sink (_scrub_memory_text) — clean_text reaches the LLM / persistence /
    long-term memory, never raw; feature-flag OFF preserves prior behavior.
"""

import pytest

from puffpai.security.vault import Vault
from puffpai.security import scrub_and_vault as sav
from puffpai.security.scrub_and_vault import (
    scrub_and_vault, scrub_and_vault_enabled,
    STATUS_OK, STATUS_VAULT_LOCKED, STATUS_CLASSIFIER_ERROR,
)
from puffpai.agents.pii_quality_gate import is_vault_worthy


# ── test data ───────────────────────────────────────────────────────────────
# GOOD = structurally valid AND not a known fake.
GOOD_SSN = "123-45-6788"           # valid SSA structure, not a published fake
GOOD_CARD = "4532015112830366"     # passes Luhn, not a network test PAN
# BAD = detected (structurally valid) but a published test/placeholder value.
TEST_SSN = "123-45-6789"           # the canonical sequential fake
TEST_CARD = "4111111111111111"     # Visa test PAN (passes Luhn, published fake)


def _unlocked_vault(tmp_path, pin="vault-pin-1234"):
    v = Vault(vault_dir=str(tmp_path))
    v.initialize(pin)
    v.authenticate(pin)  # live session key -> vault_writable
    return v


# ══════════════════════════════════════════════════════════════════════════
#  Quality gate (Archivist-owned)
# ══════════════════════════════════════════════════════════════════════════
class TestQualityGate:
    def test_good_ssn_is_worthy(self):
        assert is_vault_worthy("ssn", GOOD_SSN) is True

    def test_good_card_is_worthy(self):
        assert is_vault_worthy("credit_card", GOOD_CARD) is True

    def test_test_ssn_rejected(self):
        assert is_vault_worthy("ssn", TEST_SSN) is False

    def test_test_card_rejected(self):
        assert is_vault_worthy("credit_card", TEST_CARD) is False

    def test_luhn_failing_card_rejected(self):
        # A card that fails Luhn is not GOOD even if the caller hands it in.
        assert is_vault_worthy("credit_card", "4111111111111112") is False

    def test_all_same_digit_rejected(self):
        assert is_vault_worthy("credit_card", "4444444444444444") is False

    def test_structurally_invalid_ssn_rejected(self):
        # Area 000 is never issued -> reuse of the detector's SSA validator.
        assert is_vault_worthy("ssn", "000-12-3456") is False

    def test_placeholder_email_and_name_rejected(self):
        assert is_vault_worthy("email", "user@example.com") is False
        assert is_vault_worthy("name", "John Doe") is False

    def test_real_email_worthy(self):
        assert is_vault_worthy("email", "sam@example.invalid") is True

    def test_unknown_type_not_worthy(self):
        assert is_vault_worthy("mystery", "whatever") is False


# ══════════════════════════════════════════════════════════════════════════
#  Choke point — good data is vaulted behind a handle
# ══════════════════════════════════════════════════════════════════════════
class TestVaultGoodData:
    def test_good_ssn_vaulted_and_handle_substituted(self, tmp_path):
        v = _unlocked_vault(tmp_path)
        text = f"My social is {GOOD_SSN}, please remember it."
        san = scrub_and_vault(text, user_id="owner", source="chat", vault=v)

        assert san.status == STATUS_OK
        assert san.vaulted_count == 1
        assert san.scrubbed_not_vaulted_count == 0
        # Raw value is gone; a handle + reasoning label is present.
        assert GOOD_SSN not in san.clean_text
        assert "{{vault:" in san.clean_text
        assert "your SSN" in san.clean_text
        # The handle resolves back to the original via the ref store.
        ref_id = san.handles[0]["ref_id"]
        rec = v.get_pii_ref(ref_id)
        assert rec is not None and rec["value"] == GOOD_SSN
        assert rec["type"] == "ssn" and rec["tier"] == "critical"

    def test_ref_store_is_encrypted_on_disk(self, tmp_path):
        v = _unlocked_vault(tmp_path)
        san = scrub_and_vault(f"card {GOOD_CARD}", source="chat", vault=v)
        assert san.vaulted_count == 1
        refs = tmp_path / "pii_refs.json"
        assert refs.exists()
        raw_bytes = refs.read_bytes()
        # The original value must never sit in plaintext in the ref store.
        assert GOOD_CARD.encode() not in raw_bytes

    def test_good_card_vaulted(self, tmp_path):
        v = _unlocked_vault(tmp_path)
        san = scrub_and_vault(f"pay with {GOOD_CARD}", source="chat", vault=v)
        assert san.vaulted_count == 1
        assert GOOD_CARD not in san.clean_text
        assert "your credit card number" in san.clean_text


# ══════════════════════════════════════════════════════════════════════════
#  Choke point — test/bad data is scrubbed but NOT vaulted
# ══════════════════════════════════════════════════════════════════════════
class TestScrubTestData:
    def test_test_ssn_scrubbed_not_vaulted(self, tmp_path):
        v = _unlocked_vault(tmp_path)
        san = scrub_and_vault(f"fake ssn {TEST_SSN}", source="chat", vault=v)

        assert san.status == STATUS_OK      # no vaultable PII -> not locked
        assert san.vaulted_count == 0
        assert san.scrubbed_not_vaulted_count == 1
        assert TEST_SSN not in san.clean_text
        assert "[REDACTED-SSN]" in san.clean_text
        assert "{{vault:" not in san.clean_text
        # No ref store was ever created.
        assert not (tmp_path / "pii_refs.json").exists()

    def test_test_card_scrubbed_not_vaulted(self, tmp_path):
        v = _unlocked_vault(tmp_path)
        san = scrub_and_vault(f"test card {TEST_CARD}", source="chat", vault=v)
        assert san.vaulted_count == 0
        assert san.scrubbed_not_vaulted_count == 1
        assert TEST_CARD not in san.clean_text
        assert "[REDACTED-CC]" in san.clean_text
        assert not (tmp_path / "pii_refs.json").exists()


# ══════════════════════════════════════════════════════════════════════════
#  Fail-closed behavior
# ══════════════════════════════════════════════════════════════════════════
class TestFailClosed:
    def test_locked_vault_scrubs_and_reports_locked(self, tmp_path):
        # Initialized but NOT authenticated -> no live session key.
        v = Vault(vault_dir=str(tmp_path))
        v.initialize("vault-pin-1234")
        san = scrub_and_vault(f"ssn {GOOD_SSN}", source="chat", vault=v)

        assert san.status == STATUS_VAULT_LOCKED
        assert san.vaulted_count == 0
        # Fail closed: value is still scrubbed, never emitted raw.
        assert GOOD_SSN not in san.clean_text
        assert "[REDACTED-SSN]" in san.clean_text
        assert not (tmp_path / "pii_refs.json").exists()

    def test_no_vault_scrubs_and_reports_locked(self, tmp_path, monkeypatch):
        import puffpai.api_server as api
        monkeypatch.setattr(api, "_get_vault", lambda: None)
        # vault omitted -> choke point resolves _get_vault() -> None -> locked.
        san = scrub_and_vault(f"ssn {GOOD_SSN}", source="chat")
        assert san.status == STATUS_VAULT_LOCKED
        assert GOOD_SSN not in san.clean_text

    def test_classifier_error_never_emits_raw(self):
        class BoomClassifier:
            def scan_for_pii(self, text):
                raise RuntimeError("scanner exploded")

        san = scrub_and_vault(f"ssn {GOOD_SSN}", source="chat",
                              classifier=BoomClassifier())
        assert san.status == STATUS_CLASSIFIER_ERROR
        assert GOOD_SSN not in san.clean_text

    def test_clean_text_never_contains_raw_across_states(self, tmp_path):
        # Unlocked, locked, and no-detection all: raw must never survive.
        good = _unlocked_vault(tmp_path / "u")
        locked = Vault(vault_dir=str(tmp_path / "l"))
        locked.initialize("vault-pin-1234")
        for v in (good, locked):
            san = scrub_and_vault(
                f"ssn {GOOD_SSN} card {GOOD_CARD} test {TEST_SSN}",
                source="chat", vault=v)
            assert GOOD_SSN not in san.clean_text
            assert GOOD_CARD not in san.clean_text
            assert TEST_SSN not in san.clean_text


# ══════════════════════════════════════════════════════════════════════════
#  Feature flag
# ══════════════════════════════════════════════════════════════════════════
class TestFeatureFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("PUFFPAI_SCRUB_AND_VAULT", raising=False)
        assert scrub_and_vault_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "on", "yes"])
    def test_truthy_values_on(self, monkeypatch, val):
        monkeypatch.setenv("PUFFPAI_SCRUB_AND_VAULT", val)
        assert scrub_and_vault_enabled() is True

    @pytest.mark.parametrize("val", ["0", "off", "no", "", "maybe"])
    def test_falsey_values_off(self, monkeypatch, val):
        monkeypatch.setenv("PUFFPAI_SCRUB_AND_VAULT", val)
        assert scrub_and_vault_enabled() is False


# ══════════════════════════════════════════════════════════════════════════
#  api_server sink wiring: chat_history + conversation_history injection
# ══════════════════════════════════════════════════════════════════════════
class TestPrepareUserMessageSinks:
    def test_flag_off_preserves_legacy_behavior(self, tmp_path, monkeypatch):
        import puffpai.api_server as api
        monkeypatch.delenv("PUFFPAI_SCRUB_AND_VAULT", raising=False)
        monkeypatch.setattr(api, "_get_vault", lambda: None)

        msg = f"my ssn is {GOOD_SSN}"
        llm_text, persist_text, meta = api._prepare_user_message(
            msg, source="chat")

        # Legacy: the LLM still receives the ORIGINAL text (unchanged behavior).
        assert llm_text == msg
        # Persistence receives the redacted copy (also prior behavior).
        assert GOOD_SSN not in persist_text
        assert "[REDACTED-SSN]" in persist_text
        assert meta and meta["detected"] is True

    def test_flag_on_without_recovery_code_stays_legacy(self, tmp_path,
                                                        monkeypatch):
        """The go-live interlock: the flag ALONE does not activate scrubbing.

        Vaulting makes the vault the only copy of a scrubbed original, so it
        refuses to start until a PIN recovery code exists — otherwise a
        forgotten PIN destroys the data. Flag-on-but-no-code therefore keeps
        the exact flag-OFF behaviour (and logs a one-time warning), rather
        than half-enabling and stranding the user.
        """
        import puffpai.api_server as api
        monkeypatch.setenv("PUFFPAI_SCRUB_AND_VAULT", "1")
        v = _unlocked_vault(tmp_path)          # no recovery code minted
        monkeypatch.setattr(api, "_get_vault", lambda: v)
        sav.invalidate_recovery_code_cache()

        assert v.has_recovery_code is False
        assert sav.scrub_and_vault_active(vault=v) is False

        msg = f"my ssn is {GOOD_SSN}"
        llm_text, persist_text, meta = api._prepare_user_message(
            msg, source="chat")
        # Identical to the flag-OFF contract above.
        assert llm_text == msg
        assert GOOD_SSN not in persist_text
        assert "[REDACTED-SSN]" in persist_text

    def test_flag_on_llm_and_persistence_get_clean_text(self, tmp_path,
                                                        monkeypatch):
        import puffpai.api_server as api
        monkeypatch.setenv("PUFFPAI_SCRUB_AND_VAULT", "1")
        v = _unlocked_vault(tmp_path)
        # Satisfy the go-live interlock (see the test above): minting a
        # recovery code is what actually flips activation on, and it does so
        # live — the generate success path drops the cached has_recovery_code.
        session = v.authenticate("vault-pin-1234")
        v.generate_recovery_code(session.session_id)
        monkeypatch.setattr(api, "_get_vault", lambda: v)
        assert sav.scrub_and_vault_active(vault=v) is True

        msg = f"my ssn is {GOOD_SSN}"
        llm_text, persist_text, meta = api._prepare_user_message(
            msg, source="chat")

        # BOTH the LLM sink (conversation_history injection) and the chat_history
        # persistence sink receive the SAME clean_text — never raw.
        assert llm_text == persist_text
        assert GOOD_SSN not in llm_text
        assert "{{vault:" in llm_text
        assert meta and meta["handles"] == 1

    def test_flag_on_history_scrub_for_prompt(self, monkeypatch):
        import puffpai.api_server as api
        monkeypatch.setenv("PUFFPAI_SCRUB_AND_VAULT", "1")
        history = [{"user": f"remember {GOOD_SSN}", "assistant": "ok"}]
        cleaned = api._scrub_history_for_prompt(history)
        assert GOOD_SSN not in cleaned[0]["user"]
        assert "[REDACTED-SSN]" in cleaned[0]["user"]

    def test_flag_off_history_unchanged(self, monkeypatch):
        import puffpai.api_server as api
        monkeypatch.delenv("PUFFPAI_SCRUB_AND_VAULT", raising=False)
        history = [{"user": f"remember {GOOD_SSN}", "assistant": "ok"}]
        assert api._scrub_history_for_prompt(history) == history


# ══════════════════════════════════════════════════════════════════════════
#  Archivist sink: user_memory extraction
# ══════════════════════════════════════════════════════════════════════════
class _FakeMemory:
    def __init__(self):
        self.stored = []

    def extract_and_store(self, text):
        self.stored.append(text)

    def save(self):
        pass


class TestArchivistMemorySink:
    def _agent(self, vault, fake):
        from puffpai.agents.archivist import ArchivistAgent
        return ArchivistAgent(vault=vault, memory_factory=lambda uid: fake)

    def test_flag_off_memory_gets_raw(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PUFFPAI_SCRUB_AND_VAULT", raising=False)
        from puffpai.agents.envelope import Envelope
        fake = _FakeMemory()
        agent = self._agent(_unlocked_vault(tmp_path), fake)
        env = Envelope(intent="remember",
                       payload={"text": f"my ssn is {GOOD_SSN}"},
                       requester="owner")
        agent.handle(env)
        # Unchanged behavior: raw text still reaches memory.
        assert fake.stored == [f"my ssn is {GOOD_SSN}"]

    def test_flag_on_without_recovery_code_memorizes_raw(self, tmp_path,
                                                        monkeypatch):
        """The go-live interlock reaches the memory sink too.

        Worth pinning explicitly: while the interlock is inactive the Archivist
        memorizes RAW text, exactly as it did flag-off. That is the deliberate
        trade (don't vault without a recovery net), not an oversight -- so if
        the interlock ever silently becomes the normal path, this test says
        what that costs.
        """
        monkeypatch.setenv("PUFFPAI_SCRUB_AND_VAULT", "1")
        from puffpai.agents.envelope import Envelope
        v = _unlocked_vault(tmp_path)          # no recovery code minted
        fake = _FakeMemory()
        agent = self._agent(v, fake)
        sav.invalidate_recovery_code_cache()
        assert sav.scrub_and_vault_active(vault=v) is False
        agent.handle(Envelope(intent="remember",
                              payload={"text": f"my ssn is {GOOD_SSN}"},
                              requester="owner"))
        assert fake.stored == [f"my ssn is {GOOD_SSN}"]

    def test_flag_on_memory_gets_clean_text(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_SCRUB_AND_VAULT", "1")
        from puffpai.agents.envelope import Envelope
        v = _unlocked_vault(tmp_path)
        # Satisfy the go-live interlock: activation needs a PIN recovery code.
        v.generate_recovery_code(v.authenticate("vault-pin-1234").session_id)
        assert sav.scrub_and_vault_active(vault=v) is True
        fake = _FakeMemory()
        agent = self._agent(v, fake)
        env = Envelope(intent="remember",
                       payload={"text": f"my ssn is {GOOD_SSN}"},
                       requester="owner")
        agent.handle(env)
        assert len(fake.stored) == 1
        assert GOOD_SSN not in fake.stored[0]        # raw never memorized
        assert "{{vault:" in fake.stored[0]          # handle instead

    def test_vault_quality_gate_accessor(self, tmp_path):
        agent = self._agent(_unlocked_vault(tmp_path), _FakeMemory())
        assert agent.vault_quality_gate("ssn", GOOD_SSN) is True
        assert agent.vault_quality_gate("ssn", TEST_SSN) is False
