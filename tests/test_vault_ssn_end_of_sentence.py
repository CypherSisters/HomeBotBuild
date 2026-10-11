"""An SSN that ends a sentence must still be detected.

What /vault/scan-pii must return:

    "my ssn is 123-45-6789" -> pii_detected: true, severity: critical
    "my ssn is 123-45-6789." -> pii_detected: true (the full-stop case)
    "My SSN is 123-45-6789. Thanks"-> pii_detected: true
    "my ssn is 123-45-6789, ok" -> pii_detected: true

The trailing lookahead on ``_SSN_CANDIDATE`` must not spell the dot as a
bare member of ``[\\d.\\-/]``, or a full stop suppresses the match the same
way a continuing digit does. The consequence of a miss: the raw SSN reaches
the LLM prompt, ``/data/puffpai/chat_history/*.json`` and the systemd journal,
is not vaulted, and the user sees no PII alert. The ingest path shares the
detector, so a document whose SSN ends a sentence would be indexed in
plaintext.

Fixture note: these tests use 123-45-6789 and 219-09-9999 (the SSA
advertising number). They deliberately do NOT use 000-12-3456, because
``_valid_ssn`` rejects area 000 by design, so that value cannot distinguish a
working detector from a broken one.
"""

import pytest

from puffpai.security.data_classifier import DataClassifier


@pytest.fixture
def classifier():
    return DataClassifier()


def ssns(classifier, text):
    return classifier.scan_for_pii(text).ssns


# ---------------------------------------------------------------------------
# The sentence shapes an SSN is spoken or typed in.
# ---------------------------------------------------------------------------

class TestSentenceShapes:

    @pytest.mark.parametrize("text", [
        "my ssn is 123-45-6789",
        "my ssn is 123-45-6789.",
        "My SSN is 123-45-6789. Thanks",
        "my ssn is 123-45-6789, ok",
        "SSN 123-45-6789",
        "SSN 123-45-6789.",
        "Test Fakeperson SSN is 123-45-6789. Please store it.",
    ])
    def test_every_sentence_shape_is_detected(self, classifier, text):
        assert ssns(classifier, text) == ["123-45-6789"], (
            f"{text!r} was invisible to the SSN detector")


# ---------------------------------------------------------------------------
# Trailing punctuation must never suppress detection.
# ---------------------------------------------------------------------------

class TestTrailingPunctuation:

    @pytest.mark.parametrize("tail,label", [
        (".", "full stop"),
        ("!", "exclamation"),
        ("?", "question mark"),
        (")", "closing paren"),
        ("]", "closing bracket"),
        ('"', "closing quote"),
        (",", "comma"),
        (";", "semicolon"),
        (":", "colon"),
        ("\n", "newline"),
        ("", "end of string"),
        (". ", "full stop then space"),
        (".\n", "full stop then newline"),
        ("...", "ellipsis"),
    ])
    def test_sentence_punctuation_does_not_suppress(self, classifier,
                                                    tail, label):
        text = f"my ssn is 219-09-9999{tail}"
        assert ssns(classifier, text) == ["219-09-9999"], label

    def test_space_separated_form_also_survives_a_period(self, classifier):
        assert ssns(classifier, "SSN 219 09 9999.") == ["219 09 9999"]

    def test_redaction_removes_the_value_when_it_ends_a_sentence(self,
                                                                 classifier):
        redacted, detection = classifier.redact_pii("my ssn is 219-09-9999.")
        assert "219-09-9999" not in redacted
        assert "[REDACTED-SSN]" in redacted
        assert detection.has_pii


# ---------------------------------------------------------------------------
# Negative controls: the false positives the lookaround exists to stop.
# ---------------------------------------------------------------------------

class TestNegativeControls:

    @pytest.mark.parametrize("text", [
        "1.2.3.4",                    # dotted quad
        "192.168.1.100",              # IP address
        "192.168.50.140",                # the HA host
        "123-45-67890",               # longer digit run
        "219-09-9999.5",              # decimal continuation
        "219-09-9999.55",
        "v1.123-45-6789",             # digit-dot prefix
        "121.775",                    # frequency
        "162.475 MHz on the marine band",
        "96813-3213",                 # ZIP+4
        "order 123-45-6789-01",       # longer delimited run
        "123-45-6789/2",              # slash continuation
        "2026-09-03",                 # ISO date
        "version 4.12.0",
    ])
    def test_not_an_ssn(self, classifier, text):
        assert ssns(classifier, text) == [], f"false positive on {text!r}"

    @pytest.mark.parametrize("text", [
        "ssn 000-12-3456.",           # area 000 is never issued
        "ssn 666-12-3456.",           # area 666 is never issued
        "ssn 900-12-3456.",           # area 900+ is never issued
        "ssn 999-12-3456.",
        "ssn 123-00-6789.",           # group 00 is never issued
        "ssn 123-45-0000.",           # serial 0000 is never issued
    ])
    def test_valid_ssn_structural_rules_are_unchanged(self, classifier, text):
        assert ssns(classifier, text) == [], (
            "_valid_ssn must keep rejecting structurally impossible numbers")


# ---------------------------------------------------------------------------
# The same class of bug on the other detectors.
# ---------------------------------------------------------------------------

class TestOtherDetectorsSurviveTrailingPunctuation:
    """The SSN lookaround was the only one that spelled the dot as a bare
    class member; these pin that the rest stay correct so the next person does
    not have to re-derive it."""

    @pytest.mark.parametrize("text,expect", [
        ("card 4111 1111 1111 1111.", "credit_cards"),
        ("card 4111-1111-1111-1111.", "credit_cards"),
        ("card 4111111111111111!", "credit_cards"),
        ("call me at 555-867-5309.", "phones"),
        ("call me at (555) 867-5309.", "phones"),
        ("DOB: 1985-04-12.", "dates_of_birth"),
        ("date of birth 4/12/1985.", "dates_of_birth"),
        ("born on April 12, 1985.", "dates_of_birth"),
        ("123 Main Street.", "addresses"),
        ("name: Test Fakeperson.", "names"),
        ("patient: Test Fakeperson.", "names"),
        ("contact: test.fakeperson@example.com.", "emails"),
    ])
    def test_detected_despite_trailing_period(self, classifier, text, expect):
        found = getattr(classifier.scan_for_pii(text), expect)
        assert found, f"{expect} missed in {text!r}"

    def test_luhn_still_rejects_a_bad_card_before_a_period(self, classifier):
        assert classifier.scan_for_pii("card 4111 1111 1111 1112.").credit_cards == []


# ---------------------------------------------------------------------------
# Severity / routing: a sentence-final SSN must reach the same tier.
# ---------------------------------------------------------------------------

class TestSeverityUnchanged:

    def test_sentence_final_ssn_gets_the_same_verdict_as_the_bare_one(
            self, classifier):
        bare = classifier.scan_for_pii("my ssn is 219-09-9999")
        final = classifier.scan_for_pii("my ssn is 219-09-9999.")
        assert bare.has_pii and final.has_pii
        assert bare.ssns == final.ssns
        assert (classifier.classify_content("my ssn is 219-09-9999.")[0]
                == classifier.classify_content("my ssn is 219-09-9999")[0])
