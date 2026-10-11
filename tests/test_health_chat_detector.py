"""The free-text ``health.condition`` detector on the chat/voice path.

THE GAP THIS CLOSES. The seven structured detectors alone give:

    'I did test-fakeworkout for 1 minute' -> [] none
    'add my insulin to the meds list' -> [] none
    'Emma is gluten-free and Alex is halal' -> [] none
    'my ssn is 219-09-9999.' -> ['ssn'] critical

So an SSN is caught and the household's medication list is not, and because
``_prepare_user_message``'s legacy branch persists the REDACTED display text to
chat history, "not caught" means "written to chat history verbatim".

TWO PROPERTIES ARE UNDER TEST, and the second matters as much as the first:
detection on disclosure, and SILENCE on general health questions. Any detection
suppresses that turn's knowledge retrieval (``_pii_scrubbed_this_turn``), so a
detector that fired on "tell me about diabetes" would quietly break the ability
to ask about health at all.

All sentences are fake. Fake PII per the standing rules: SSN 219-09-9999.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from puffpai.security.data_classifier import DataClassifier  # noqa: E402
from puffpai.security.scrub_and_vault import (  # noqa: E402
    _LABEL, _ORDER, _REDACT_TOKEN, _TYPE_TIER, scrub_and_vault,
)


@pytest.fixture
def classifier():
    return DataClassifier()


#: Turns that ARE a disclosure about a person's health.
DISCLOSURES = [
    "add my insulin to the meds list",
    "add amoxicillin to the meds",
    "log my insulin dose for tonight",
    "Fakechild's meds go at 8pm",
    "my blood pressure was high today",
    "her prescription needs a refill",
    "I have asthma",
    "she is diabetic",
    "he was diagnosed last spring",
    "our therapist moved the appointment",
]

#: Turns that are NOT — general questions, app chatter, house control. Every
#: one of these must stay invisible to the detector.
NON_DISCLOSURES = [
    "tell me about diabetes",
    "what is insulin used for",
    "how does blood pressure medication work",
    "what's on the medication list?",
    "I did test-fakeworkout for 1 minute",
    "add milk to the shopping list",
    "turn on the living room lamp",
    "what time is it",
    "what is the weather tomorrow",
    "play some jazz",
    "set a timer for 10 minutes",
    "who won the game last night",
]


@pytest.mark.parametrize("text", DISCLOSURES)
def test_disclosures_are_detected(classifier, text):
    detection = classifier.scan_for_pii(text, include_health=True)
    assert detection.health_conditions, text
    assert "health.condition" in detection.pii_types
    assert detection.severity == "high"


@pytest.mark.parametrize("text", NON_DISCLOSURES)
def test_non_disclosures_stay_silent(classifier, text):
    detection = classifier.scan_for_pii(text, include_health=True)
    assert detection.health_conditions == [], text
    assert not detection.has_pii, text


def test_the_medication_name_itself_is_redacted(classifier):
    """The failure mode a single big alternation produces.

    With "add my insulin to the meds list", an "add... to the meds" branch
    consumes the whole clause, so the DESTINATION is redacted and the
    MEDICATION survives. Both must go.
    """
    redacted, detection = classifier.redact_pii(
        "add my insulin to the meds list", include_health=True)
    assert "insulin" not in redacted.lower()
    assert redacted == "add my [REDACTED-HEALTH] to the [REDACTED-HEALTH] list"
    assert set(t.lower() for t in detection.health_conditions) == {
        "insulin", "meds"}


def test_redaction_is_case_insensitive(classifier):
    redacted, _ = classifier.redact_pii(
        "My Insulin is in the fridge. Insulin again.", include_health=True)
    assert "insulin" not in redacted.lower()


def test_a_cue_does_not_vouch_across_a_sentence_boundary(classifier):
    """A window that ran past a full stop would let one disclosure launder
    every health word after it — and would redact the answer to a legitimate
    general question that happened to follow one."""
    redacted, detection = classifier.redact_pii(
        "I have asthma. Tell me about insulin.", include_health=True)
    assert redacted == "I have [REDACTED-HEALTH]. Tell me about insulin."
    assert [t.lower() for t in detection.health_conditions] == ["asthma"]


def test_possessive_cue_is_case_sensitive(classifier):
    """``what's`` is not a person's name.

    The cue regex runs under IGNORECASE, which would let ``[A-Z][a-z]+'s``
    match ``what's`` and fire on a plain question; the ``(?-i:...)`` island is
    what stops it.
    """
    assert classifier.scan_for_pii(
        "what's on the medication list?", include_health=True
    ).health_conditions == []
    assert classifier.scan_for_pii(
        "Fakechild's meds go at 8pm", include_health=True).health_conditions


# ── the default is OFF, and that is deliberate ─────────────────────────────

@pytest.mark.parametrize("text", DISCLOSURES)
def test_detector_is_off_by_default(classifier, text):
    """Ingest and every other existing caller keep byte-identical behaviour.

    Document upload does NOT opt in: a medical document the owner deliberately
    uploaded is meant to be searchable, and redacting its clinical terms would
    gut the thing they asked for. Turning this on globally is a product decision
    about upload utility, not a security improvement.
    """
    detection = classifier.scan_for_pii(text)
    assert detection.health_conditions == []
    assert not detection.has_pii


def test_the_seven_original_detectors_are_untouched(classifier):
    assert classifier.scan_for_pii("my ssn is 219-09-9999.").pii_types == ["ssn"]
    assert classifier.scan_for_pii(
        "card 4111 1111 1111 1111").pii_types == ["credit_card"]
    assert classifier.scan_for_pii(
        "reach me at test@example.com").pii_types == ["email"]
    #...and they behave identically with the new flag on.
    assert classifier.scan_for_pii(
        "my ssn is 219-09-9999.", include_health=True).pii_types == ["ssn"]


# ── scrub_and_vault integration + the honesty contract ───────────

def test_scrub_and_vault_removes_the_health_term():
    result = scrub_and_vault("add my insulin to the meds list",
                             source="chat", vault=None, include_health=True)
    assert "insulin" not in result.clean_text.lower()
    assert "[REDACTED-HEALTH]" in result.clean_text
    assert result.pii_detected
    assert result.pii_types == ["health.condition"]
    assert result.severity == "high"


def test_scrub_and_vault_default_leaves_health_alone():
    result = scrub_and_vault("add my insulin to the meds list",
                             source="ingest", vault=None)
    assert result.clean_text == "add my insulin to the meds list"
    assert not result.pii_detected


def test_health_is_scrubbed_but_never_vaulted():
    """Which is what makes the notice a TRUE sentence.

    ``is_vault_worthy`` does not recognise the class, so it returns False and
    the span becomes ``[REDACTED-HEALTH]`` rather than a ``{{vault:...}}``
    handle. The reply can therefore say "I did not keep it" without hedging.
    """
    result = scrub_and_vault("I have asthma", source="chat", vault=None,
                             include_health=True)
    assert result.handles == []
    assert result.vaulted_count == 0
    assert result.scrubbed_not_vaulted_count == 1
    assert "{{vault:" not in result.clean_text


def test_health_and_child_are_wired_into_every_scrub_table():
    """A type present in one table and missing from another fails silently:
    ``_TYPE_TIER.get(t, "critical")`` would quietly re-tier it, and
    ``_REDACT_TOKEN.get(t, "[REDACTED]")`` would emit a generic token."""
    for pii_type in ("health.condition", "child.any"):
        assert pii_type in _TYPE_TIER
        assert pii_type in _REDACT_TOKEN
        assert pii_type in _LABEL
        assert pii_type in _ORDER


def test_a_classifier_without_the_keyword_degrades_rather_than_failing_closed():
    """An injected test double or an older classifier must not turn a scan into
    a whole-message placeholder."""
    class OldClassifier:
        def scan_for_pii(self, text):
            return DataClassifier().scan_for_pii(text)

    result = scrub_and_vault("my ssn is 219-09-9999.", source="chat",
                             vault=None, classifier=OldClassifier(),
                             include_health=True)
    assert "219-09-9999" not in result.clean_text
    assert result.status == "OK"


def test_log_redaction_always_covers_health():
    """A log line is never the reason a household needs its medication list."""
    from puffpai.security.scrub_and_vault import redact_for_log

    line = redact_for_log("add my insulin to the meds list", limit=200)
    assert "insulin" not in line.lower()


# ── the honest reply, per ────────────────────────────────────────

def test_honesty_notice_names_the_health_detail_and_claims_no_storage():
    from puffpai.api_server import _pii_honesty_notice

    notice = _pii_honesty_notice({
        'detected': True, 'types': ['health.condition'], 'vaulted': [],
        'severity': 'high', 'status': 'OK',
    })
    assert notice == ("I spotted a health detail and did not keep it — it was "
                      "removed from this conversation and not saved anywhere.")


def test_a_health_notice_only_ever_PREPENDS():
    """NOT the substitution — and that is the deliberate difference.

    Putting ``health.condition`` in ``_PII_TYPE_WORD_RE`` would throw "Got it
    - I've noted your medication." away and replace it with the notice. Two
    reasons that is wrong for this class, and only this class:

      * the appliance HAS somewhere to keep a medication (the calendar keeps
        a medication list), so on a storing turn the notice is false and the
        discarded reply would be the true one;
      * even when the notice is true, a medication reply is often the only
        thing the household gets from the turn.

    An SSN has neither property, so its substitution stays. See
    tests/test_medication_storage_honesty.py for the storing half.
    """
    from puffpai.api_server import _apply_pii_honesty
    from puffpai.security import turn_writes

    turn_writes.end_turn()
    meta = {'detected': True, 'types': ['health.condition'], 'vaulted': [],
            'severity': 'high', 'status': 'OK'}

    claim = "Got it — I've noted your medication."
    kept = _apply_pii_honesty(claim, meta)
    assert kept.startswith("I spotted a health detail")
    assert claim in kept

    prepended = _apply_pii_honesty("The living room lamp is now on.", meta)
    assert prepended.startswith("I spotted a health detail")
    assert "The living room lamp is now on." in prepended


def test_a_turn_with_a_health_detail_counts_as_scrubbed():
    """Retrieval must not run on a turn we just scrubbed health out of —
    the same rule established for SSNs."""
    from puffpai.api_server import _pii_scrubbed_this_turn

    assert _pii_scrubbed_this_turn(
        {'detected': True, 'types': ['health.condition']})
    assert not _pii_scrubbed_this_turn({'detected': False})
