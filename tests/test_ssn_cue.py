"""A SPOKEN SSN must be detected, scrubbed and vaulted.

A spoken SSN can reach the appliance like this. The owner SAID:

    "My Social Security number is 000123456."

Whisper emits no dashes, so the brain received nine bare digits.
``_SSN_CANDIDATE`` requires a separator (``(\\d{3})([-\\s])(\\d{2})\\2(\\d{4})``),
so nothing matched: the scrub-and-vault path never ran, the raw turn went
to the model, and the model answered "I'm sorry, but I can't help with
that." The number in that test is ALSO structurally invalid (area 000), but
that is not what the fix is about — a valid-shape spoken SSN ("one two three
four five six seven eight nine") was missed in exactly the same way, and
that is the hole these tests pin shut.

The fix is deliberately narrow: a bare 9-digit run counts ONLY when an SSN
cue (ssn / social security [number] / social) appears within the 60
characters immediately BEFORE it, AND ``_valid_ssn`` passes. Without the cue
requirement the detector would fire on routing numbers, order ids and
catalog codes — the false positives the bare-digit shape was excluded for in
the first place.

Fixture note: 123456789 is structurally VALID
(area 123, group 45, serial 6789), so it can distinguish a working detector
from a broken one. It is a published fake, which is a QUALITY-GATE fact, not
a detector fact — the gate scrubs it without vaulting, and the tests below
assert that the bare form lands in exactly the same place as the dashed
form. 123456788 is the GOOD (vaultable) counterpart, mirroring
tests/test_scrub_and_vault.py.
"""

import pytest

from puffpai.security.data_classifier import DataClassifier


# Structurally valid, published fake -> detected, scrubbed, NOT vaulted.
BARE_SSN = "123456789"
DASHED_SSN = "123-45-6789"
# Structurally valid, not a published fake -> detected, scrubbed AND vaulted.
GOOD_BARE_SSN = "123456788"
GOOD_DASHED_SSN = "123-45-6788"


@pytest.fixture
def classifier():
    return DataClassifier()


def ssns(classifier, text):
    return classifier.scan_for_pii(text).ssns


# ---------------------------------------------------------------------------
# The live transcript, and the phrasings a voice turn actually produces.
# ---------------------------------------------------------------------------

class TestCuedSpokenSSNIsDetected:

    @pytest.mark.parametrize("text", [
        "My Social Security number is 123456789.",          # the live shape
        "my social security number is 123456789.",
        "my social security number is 123456789",
        "SSN: 123456789",
        "SSN 123456789.",
        "my ssn is 123456789",
        "My SSN is 123456789. Thanks",
        "my ssn is 123456789, ok",
        "whats my social 123456789",                        # STT clipping
        "Social Security Number 123456789",
    ])
    def test_cued_bare_nine_digits_is_detected(self, classifier, text):
        assert ssns(classifier, text) == [BARE_SSN], (
            f"sec-ssn-cue: {text!r} was invisible to the SSN detector")

    def test_severity_matches_the_dashed_form(self, classifier):
        spoken = classifier.scan_for_pii(
            "My Social Security number is 123456789.")
        typed = classifier.scan_for_pii("My SSN is 123-45-6789.")
        assert spoken.has_pii is True
        assert spoken.severity == typed.severity == "critical"
        assert "ssn" in spoken.pii_types
        assert "ssn" in typed.pii_types
        assert spoken.summary() == typed.summary()

    def test_redaction_marker_is_the_same(self, classifier):
        redacted, detection = classifier.redact_pii(
            "My Social Security number is 123456789.")
        assert detection.ssns == [BARE_SSN]
        assert BARE_SSN not in redacted
        assert "[REDACTED-SSN]" in redacted
        assert redacted == "My Social Security number is [REDACTED-SSN]."

    def test_spoken_and_typed_redact_to_the_same_sentence(self, classifier):
        spoken, _ = classifier.redact_pii("my ssn is 123456789.")
        typed, _ = classifier.redact_pii("my ssn is 123-45-6789.")
        assert spoken == typed == "my ssn is [REDACTED-SSN]."

    def test_both_shapes_in_one_text_are_both_found_in_order(self, classifier):
        # The dashed pass runs first, then the cued bare pass; both land in
        # detection.ssns so both are scrubbed.
        found = ssns(classifier, "ssn 123-45-6789 and also ssn 123456788")
        assert sorted(found) == sorted([DASHED_SSN, GOOD_BARE_SSN])


# ---------------------------------------------------------------------------
# Structural validity still gates. A cue does not make an impossible number
# an SSN.
# ---------------------------------------------------------------------------

class TestCuedButStructurallyInvalid:

    @pytest.mark.parametrize("text,why", [
        ("My Social Security number is 000123456.", "area 000 is never issued"),
        ("my ssn is 666123456", "area 666 is never issued"),
        ("my ssn is 900123456", "area 9xx is never issued"),
        ("my ssn is 999999999", "area 9xx is never issued"),
        ("SSN: 123004567", "group 00 is never issued"),
        ("SSN: 123450000", "serial 0000 is never issued"),
    ])
    def test_invalid_shape_is_not_detected(self, classifier, text, why):
        assert ssns(classifier, text) == [], (
            f"sec-ssn-cue: {text!r} should be rejected ({why})")


# ---------------------------------------------------------------------------
# No cue, no match. This is the whole reason bare 9-digit runs were excluded.
# ---------------------------------------------------------------------------

class TestUncuedNineDigitRunsAreNotSSNs:

    @pytest.mark.parametrize("text", [
        "the routing number is 123456789",
        "routing number 123456789",
        "order 123456789",
        "order number 123456789 shipped yesterday",
        "tracking 123456788",
        "account 123456789",
        "part no. 123456789",
        "the meter read 123456789 kWh",
        "123456789",
    ])
    def test_bare_run_without_a_cue_is_not_detected(self, classifier, text):
        assert ssns(classifier, text) == [], (
            f"sec-ssn-cue: {text!r} must NOT fire the SSN detector")

    def test_the_cue_must_come_BEFORE_the_number(self, classifier):
        """One-directional on purpose: a cue that FOLLOWS the digits does not
        vouch for them. Keeping the rule one-directional is what makes it easy
        to reason about, and the observed disclosure always says the cue
        first. Declared false negative, not an oversight."""
        assert ssns(classifier, "123456789 is my ssn") == []
        assert ssns(classifier, "123456789 (social security)") == []

    def test_a_cue_too_far_back_does_not_vouch(self, classifier):
        """The cue window is 60 characters, so a cue two clauses away cannot
        launder an unrelated number later in the line."""
        far = ("my ssn is on file already, and the wire went out today to the "
               "vendor with reference 123456789")
        assert ssns(classifier, far) == []

    def test_a_cue_just_inside_the_window_still_counts(self, classifier):
        near = "my ssn, which I hate saying out loud, is 123456789"
        assert ssns(classifier, near) == [BARE_SSN]


# ---------------------------------------------------------------------------
# Digit-run isolation: the same lookarounds as the dashed form.
# ---------------------------------------------------------------------------

class TestAdjacentDigitsAndDecimals:

    @pytest.mark.parametrize("text", [
        "my ssn is 1234567890",          # 10 digits — not an SSN
        "my ssn is 0123456789",          # 10 digits, leading zero
        "my ssn is 12345678901234",      # long run
        "my ssn is 123456789.5",         # decimal tail
        "my ssn is 12.123456789",        # fractional part
        "my ssn is 123456789-0",         # delimited run
        "my ssn is 123456789/2",         # delimited run
        "my ssn is 2026-123456789",      # delimited run
    ])
    def test_runs_that_are_not_isolated_are_not_detected(self, classifier, text):
        assert ssns(classifier, text) == [], (
            f"{text!r} is not an isolated 9-digit run")

    def test_sentence_final_period_still_detects(self, classifier):
        # A full stop is punctuation, not a decimal point.
        assert ssns(classifier, "my ssn is 123456789.") == [BARE_SSN]


# ---------------------------------------------------------------------------
# Regression: the written forms behave exactly as before.
# ---------------------------------------------------------------------------

class TestDashedFormUnchanged:

    @pytest.mark.parametrize("text,expected", [
        ("SSN: 123-45-6789", "123-45-6789"),
        ("my ssn is 123-45-6789.", "123-45-6789"),
        ("My SSN is 123-45-6789. Thanks", "123-45-6789"),
        ("123-45-6789", "123-45-6789"),          # dashed needs no cue
        ("123 45 6789", "123 45 6789"),          # spaced form, no cue
        ("my ssn is 123 45 6789", "123 45 6789"),
    ])
    def test_written_forms_still_detected(self, classifier, text, expected):
        assert ssns(classifier, text) == [expected]

    @pytest.mark.parametrize("text", [
        "96813-3213",            # ZIP+4
        "121.775-12-3456",       # decimal-prefixed
        "123-45-67890",          # over-long
        "000-12-3456",           # area 000
        "123-00-4567",           # group 00
        "123-45-0000",           # serial 0000
    ])
    def test_written_non_ssns_still_rejected(self, classifier, text):
        assert ssns(classifier, text) == []

    def test_clean_text_still_clean(self, classifier):
        pii = classifier.scan_for_pii("The weather today is sunny and warm.")
        assert pii.has_pii is False
        assert pii.severity == "none"


# ---------------------------------------------------------------------------
# Vault routing: the spoken form must land where the typed form lands.
# ---------------------------------------------------------------------------

class TestVaultRoutingIsIdentical:
    """Same harness as tests/test_scrub_and_vault.py: a real unlocked Vault in
    tmp_path, driven through the scrub_and_vault choke point."""

    @staticmethod
    def _unlocked_vault(tmp_path, pin="vault-pin-1234"):
        from puffpai.security.vault import Vault
        v = Vault(vault_dir=str(tmp_path))
        v.initialize(pin)
        v.authenticate(pin)
        return v

    def test_quality_gate_treats_both_shapes_alike(self):
        from puffpai.agents.pii_quality_gate import is_vault_worthy
        assert is_vault_worthy("ssn", GOOD_BARE_SSN) is \
            is_vault_worthy("ssn", GOOD_DASHED_SSN) is True
        # Published fake, dashed or bare: scrubbed, never vaulted.
        assert is_vault_worthy("ssn", BARE_SSN) is \
            is_vault_worthy("ssn", DASHED_SSN) is False
        assert is_vault_worthy("ssn", "000123456") is False

    def test_good_spoken_ssn_is_vaulted_like_the_typed_one(self, tmp_path):
        from puffpai.security.scrub_and_vault import (
            scrub_and_vault, STATUS_OK)
        v = self._unlocked_vault(tmp_path)
        san = scrub_and_vault(
            f"My Social Security number is {GOOD_BARE_SSN}",
            user_id="owner", source="voice", vault=v)

        assert san.status == STATUS_OK
        assert san.vaulted_count == 1
        assert san.scrubbed_not_vaulted_count == 0
        assert GOOD_BARE_SSN not in san.clean_text
        assert "{{vault:" in san.clean_text
        assert "your SSN" in san.clean_text
        ref_id = san.handles[0]["ref_id"]
        rec = v.get_pii_ref(ref_id)
        assert rec is not None and rec["value"] == GOOD_BARE_SSN

    def test_published_fake_spoken_ssn_is_scrubbed_not_vaulted(self, tmp_path):
        from puffpai.security.scrub_and_vault import (
            scrub_and_vault, STATUS_OK)
        v = self._unlocked_vault(tmp_path)
        san = scrub_and_vault(f"my ssn is {BARE_SSN}",
                              source="voice", vault=v)

        assert san.status == STATUS_OK
        assert san.vaulted_count == 0
        assert san.scrubbed_not_vaulted_count == 1
        assert BARE_SSN not in san.clean_text
        assert "[REDACTED-SSN]" in san.clean_text
        assert "{{vault:" not in san.clean_text
        assert not (tmp_path / "pii_refs.json").exists()

    def test_uncued_routing_number_reaches_the_model_untouched(self, tmp_path):
        from puffpai.security.scrub_and_vault import scrub_and_vault
        v = self._unlocked_vault(tmp_path)
        text = "the routing number is 123456789"
        san = scrub_and_vault(text, source="voice", vault=v)
        assert san.clean_text == text
        assert san.vaulted_count == 0
        assert san.scrubbed_not_vaulted_count == 0
