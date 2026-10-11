"""
PuffPAI Data Classifier
Automatically classifies data into security zones.

Given a file path, determines which zone it belongs to.
Given data content, detects PII and routes to the Vault zone.
Critical for the personal knowledge pipeline.
"""

from __future__ import annotations

import re
from pathlib import Path

from .zones import DataZone, ZONE_CLASSIFICATIONS, get_zone_for_file


_EMAIL_PATTERN = re.compile(
    r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'
)
_PHONE_PATTERN = re.compile(
    r'(?<!\d)'
    r'(?:\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}'
    r'|\(\d{3}\)\s*\d{3}[\-.]?\d{4})'
    r'(?!\d)'
)
# SSN candidate: XXX-XX-XXXX or XXX XX XXXX with a *consistent* separator
# (backref \2 forces both separators to match). Bare 9-digit runs are
# intentionally NOT matched — in reference data (phone numbers, IDs, catalog
# codes) they are indistinguishable from an SSN and drove the bulk of the
# false positives. The lookaround rejects candidates that sit inside a longer
# digit / dash / dot / slash run: ZIP+4 like "96813-3213" (which a naive regex
# mis-parses as 968-13-3213), decimals like "121.775", and frequency lists.
#
# The dot must only disqualify a candidate when it is *part of a number*,
# never when it is sentence punctuation. Spelling the dot as a bare member of
# the character class would make "my ssn is 123-45-6789.", the single most
# common way a human writes it, invisible to the detector, while
# "123-45-6789, ok" is caught. The dot is therefore split
# out into its own lookaround that also requires an adjacent digit:
#   trailing  (?!\.\d)  rejects "219-09-9999.5" but accepts ".", "!", "?",
#                       ")", ",", newline and end-of-string;
#   leading   (?<!\d\.) rejects "121.775-12-3456" but accepts a bare leading
#                       dot with no digit in front of it.
# Digits, dashes and slashes stay unconditionally disqualifying on both sides,
# so ZIP+4, "123-45-67890" and delimited runs are always rejected.
# Group 1 = area, group 3 = group, group 4 = serial; validated by _valid_ssn.
_SSN_CANDIDATE = re.compile(
    r'(?<![\d\-/])(?<!\d\.)'
    r'(\d{3})([-\s])(\d{2})\2(\d{4})'
    r'(?![\d\-/])(?!\.\d)'
)
# ── SSN spoken aloud: a CUED bare 9-digit run ──────────────────────────────
#
# Said aloud, "My Social Security number is 123456789." reaches the brain
# with no dashes: Whisper emits nine contiguous digits, _SSN_CANDIDATE (which
# REQUIRES a separator) does not match, and without this pattern the
# scrub-and-vault path never runs and the raw turn goes to the model. Every
# voice disclosure of an SSN would be invisible to the detector.
#
# The comment at the top of _SSN_CANDIDATE still holds: an UNCUED
# 9-digit run is indistinguishable from a routing number, an order id or a
# catalog code, and matching it is the main source of false positives. So
# this pattern buys back exactly the spoken case and nothing else, by
# requiring three things at once:
#   1. the digit run is isolated — same lookarounds as the dashed form, so a
#      run inside a longer digit/dash/slash sequence or a decimal is rejected:
#      leading  (?<![\d\-/.]) rejects "1234567890", "12-123456789" and the
#                             fractional part of ".123456789";
#      trailing (?![\d\-/])(?!\.\d) rejects "1234567890", "123456789-0" and
#                             "123456789.5" while still accepting a sentence
#                             that ends in "." (the sentence-dot rule above);
#   2. an SSN CUE appears in the _SSN_CUE_WINDOW characters immediately
#      BEFORE the run (see _SSN_CUE_RE). Before only — a cue that follows the
#      number does not vouch for it. That is a deliberate simplification: it
#      keeps the rule one-directional and easy to reason about, and "123456789
#      is my ssn" is not how the disclosure is usually phrased;
#   3. _valid_ssn passes, exactly as for the dashed form (area != 000/666/9xx,
#      group != 00, serial != 0000).
# Groups are numbered to MATCH the dashed pattern's consumer contract:
# group 1 = area, group 3 = group, group 4 = serial. Group 2 is an empty
# non-capturing placeholder standing in for the dashed form's separator
# backreference, so scan_for_pii can validate both patterns with one call.
_SSN_BARE_CANDIDATE = re.compile(
    r'(?<![\d\-/.])'
    r'(\d{3})()(\d{2})(\d{4})'
    r'(?![\d\-/])(?!\.\d)'
)
#: The cue that makes a bare 9-digit run an SSN candidate. "social" alone is
#: included because speech-to-text routinely clips "social security number"
#: down to "social" ("what's my social"); it is still a much stronger signal
#: than no cue at all, and _valid_ssn remains the second gate.
_SSN_CUE_RE = re.compile(
    r'\b(?:ssn|social security(?: number)?|social)\b',
    re.IGNORECASE,
)
#: How far back the cue may sit. Long enough for the observed phrasings
#: ("My Social Security number is ", "SSN: "), short enough that a cue in an
#: earlier clause cannot vouch for an unrelated number later in the line.
_SSN_CUE_WINDOW = 60


def _cued_bare_ssns(text: str) -> list:
    """Bare 9-digit runs that are BOTH cued and structurally valid.

    The spoken-SSN half of the detector; see the comment on
    _SSN_BARE_CANDIDATE. Returns the matched digit runs in order of
    appearance, so the caller's redaction and vaulting order is unchanged.
    """
    found = []
    for match in _SSN_BARE_CANDIDATE.finditer(text):
        window = text[max(0, match.start() - _SSN_CUE_WINDOW):match.start()]
        if not _SSN_CUE_RE.search(window):
            continue
        if not _valid_ssn(match.group(1), match.group(3), match.group(4)):
            continue
        found.append(match.group(0))
    return found


# Leading (?<![\d.]) guard: the street number must not be preceded by a digit
# or a decimal point, so a frequency like "162.475 ... St. Thomas" no longer
# yields the fake address "475 ... St".
_ADDRESS_PATTERN = re.compile(
    r'(?<![\d.])'
    r'\b\d{1,5}\s+(?:[NSEW]\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+'
    r'(?:St\.?|Street|Ave\.?|Avenue|Blvd\.?|Boulevard|Dr\.?|Drive|'
    r'Ln\.?|Lane|Ct\.?|Court|Rd\.?|Road|Way|Pl\.?|Place)\b',
    re.IGNORECASE,
)
# Credit-card *candidate* shapes (Visa / MasterCard / Amex grouping). Every
# candidate is Luhn-validated in scan_for_pii before it counts as a card, so a
# 13-19 digit number that fails the checksum (random grouped digits) does not
# fire "critical".
_CREDIT_CARD_PATTERN = re.compile(
    r'\b(?:4\d{3}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}'
    r'|5[1-5]\d{2}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}'
    r'|3[47]\d{2}[\s\-]?\d{6}[\s\-]?\d{5})\b'
)
# Labeled name: the trigger word is anchored with \b so substrings like
# "pageName=" do not fire "name", and the captured name is matched
# case-sensitively via (?-i:...) so it must be Title-Case (e.g. "Jane Doe")
# rather than a run of lowercase or ALL-CAPS words from prose/URLs.
_LABELED_NAME_PATTERN = re.compile(
    r'\b(?:name|patient|client|user|owner|contact)\s*[:=]\s*'
    r'(?-i:([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+))',
    re.IGNORECASE,
)

# ── health.condition (free text) ────────────────────────────────────────────
#
# The eighth detector, and the first one that is about a CLASS rather than a
# FORMAT. It exists because of a measured gap: "add my insulin to the meds
# list" and "I did a workout, she's 15" return `[] none` from every one of the
# seven format detectors, so without it a medication typed into the chat box
# is persisted to chat history in the clear. An SSN is caught; the household's
# health would not be.
#
# DESIGN: high precision, declared false negatives. The trigger is not the
# health word — it is a health word standing in a POSSESSIVE or FIRST-PERSON
# construction, i.e. someone disclosing a fact about a person rather than asking
# about a subject. That single requirement is what keeps "tell me about
# diabetes" and "what is insulin used for" out of the detector, which matters
# more than it looks: any detection on a turn suppresses that turn's knowledge
# retrieval (_pii_scrubbed_this_turn), so a detector that fired on general
# health questions would quietly break the ability to ASK about health.
#
# The lexicon is deliberately short and specific. Broadening it is a decision
# about false positives on ordinary conversation ("my anxiety about the
# deadline"), and the honest place to make that decision is here, in a list, not
# inside a regex someone has to reverse-engineer.
#
# ONLY health terms themselves are captured and redacted, never the possessive
# that proved them, so the shape of the turn survives: "add my insulin to the
# meds list" becomes "add my [REDACTED-HEALTH] to the [REDACTED-HEALTH] list".
_HEALTH_TERMS = (
    # medication / treatment
    r'medications?|medicines?|meds|prescriptions?|prescribed|dosage|doses?|'
    r'insulin|inhaler|epipen|antibiotics?|amoxicillin|antidepressants?|'
    r'chemo(?:therapy)?|dialysis|radiation therapy|'
    # diagnoses and conditions
    r'diagnosis|diagnosed|asthma|diabetes|diabetic|epilepsy|seizures?|'
    r'migraines?|cancer|tumou?r|hiv|lupus|crohn\'?s|celiac|coeliac|'
    r'depression|anxiety disorder|bipolar|adhd|autism|dementia|alzheimer\'?s|'
    # clinical readings and events
    r'blood pressure|blood sugar|a1c|cholesterol|surgery|operation|'
    r'therapist|psychiatrist|oncologist|cardiologist|'
    # reproductive / disability status
    r'pregnan(?:t|cy)|miscarriage|disability|wheelchair'
)
# A health term ALONE is not a detection. It counts only when the surrounding
# clause shows someone DISCLOSING a fact about a person, and that is decided by
# scanning for terms first and then looking backwards for a cue — deliberately
# not by one big alternation. An alternation gets this wrong in a way that
# matters: "add my insulin to the meds list" matches the "add ... to the meds"
# branch starting at "add", the match consumes the whole clause, and "insulin"
# — the actual medication — is never offered to the redactor at all. Two
# passes cannot swallow each other.
_HEALTH_TERM_RE = re.compile(r'\b(?:' + _HEALTH_TERMS + r')\b', re.IGNORECASE)

#: A possessive or first-person construction: someone is talking about a
#: person's health, not about a subject. "your" is deliberately ABSENT — in a
#: user turn "your" addresses the assistant, so it is not a disclosure cue.
#: The apostrophe in ``Name\'s`` is required so a bare capitalised word cannot
#: stand in for a possessive.
_HEALTH_CUE_RE = re.compile(
    r"\b(?:my|our|his|her|their)\b"
    # (?-i:...) is load-bearing: the enclosing re.IGNORECASE would otherwise
    # let [A-Z][a-z]+ match a lowercase word, and "what's on the medication
    # list?" would read as the possessive "What's" and fire on a plain
    # question. Same trick, same reason, as _LABELED_NAME_PATTERN above.
    r"|(?-i:\b[A-Z][a-z]+)['’]s"
    r"|\b(?:i|we|he|she|they)\s+"
    r"(?:am|is|are|was|were|have|has|had|take|takes|took|started|need|needs|"
    r"got|gets|get)\b",
    re.IGNORECASE,
)
#: The household's medication list, named as a destination. On its own this is
#: a QUESTION ("what's on the medication list?") and must not fire — that is a
#: legitimate app read, and firing on it would both redact the answer and
#: suppress the turn's retrieval. Paired with a write verb it is a disclosure.
_HEALTH_MEDLIST_RE = re.compile(
    r"\b(?:meds?|medications?|medicine)\s+list\b"
    r"|\bto\s+(?:the\s+|my\s+|his\s+|her\s+|their\s+|our\s+)?"
    r"(?:meds?|medications?)\b",
    re.IGNORECASE,
)
_HEALTH_WRITE_VERB_RE = re.compile(
    r"\b(?:add|adds|added|adding|log|logs|logged|logging|record|records|"
    r"recorded|put|note|noted|save|saves|remind)\b",
    re.IGNORECASE,
)
#: How far back a cue may sit from the term. Long enough for "my 8pm insulin",
#: short enough that a cue two clauses away cannot vouch for an unrelated word.
_HEALTH_CUE_WINDOW = 40
_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]")


def _health_disclosures(text: str) -> list:
    """Health terms that appear in a DISCLOSURE, in order of appearance.

    Returns the terms themselves — never the possessive that proved them — so
    redaction removes the fact and leaves the sentence shape readable:
    "add my insulin to the meds list" becomes
    "add my [REDACTED-HEALTH] to the [REDACTED-HEALTH] list".
    """
    found = []
    seen = set()
    # Cheap precheck so the per-term sentence walk below (which rescans the
    # text before each match) can only run on text that could possibly need it.
    # Without it, a long document full of health terms would be quadratic.
    medlist_possible = bool(_HEALTH_MEDLIST_RE.search(text)
                            and _HEALTH_WRITE_VERB_RE.search(text))
    for match in _HEALTH_TERM_RE.finditer(text):
        start = match.start()
        # The cue must be in the SAME sentence: a window that runs past a
        # full stop would let "I have asthma. Tell me about insulin" vouch for
        # the second sentence.
        window = text[max(0, start - _HEALTH_CUE_WINDOW):start]
        parts = _SENTENCE_SPLIT_RE.split(window)
        window = parts[-1] if parts else window
        disclosed = bool(_HEALTH_CUE_RE.search(window))
        if not disclosed and medlist_possible:
            # Sentence-level fallback: "add amoxicillin to the meds" carries no
            # possessive, but a write verb aimed at the medication list is an
            # unambiguous statement about the household's own medications.
            sentence_start = 0
            for sep in _SENTENCE_SPLIT_RE.finditer(text[:start]):
                sentence_start = sep.end()
            sentence_end = len(text)
            tail = _SENTENCE_SPLIT_RE.search(text, match.end())
            if tail:
                sentence_end = tail.start()
            sentence = text[sentence_start:sentence_end]
            disclosed = bool(_HEALTH_MEDLIST_RE.search(sentence)
                             and _HEALTH_WRITE_VERB_RE.search(sentence))
        if not disclosed:
            continue
        term = match.group(0)
        key = term.lower()
        if key not in seen:
            seen.add(key)
            found.append(term)
    return found


def _valid_ssn(area: str, group: str, serial: str) -> bool:
    """Apply SSA structural constraints to an SSN candidate.

    Rejects area 000 / 666 / 900-999, group 00, and serial 0000 — the ranges
    the SSA never issues — so that structurally impossible numbers (and the
    numeric noise that merely looks SSN-shaped) do not fire.
    """
    a, g, s = int(area), int(group), int(serial)
    if a == 0 or a == 666 or a >= 900:
        return False
    if g == 0:
        return False
    if s == 0:
        return False
    return True


def _luhn_ok(digits: str) -> bool:
    """True if the digit string passes the Luhn (mod-10) checksum."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
# Month names (full + 3-letter abbreviations), for prose birthdates.
_MONTH_NAME = (
    r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|'
    r'Nov(?:ember)?|Dec(?:ember)?)'
)
# Date shapes we treat as a birthdate when a birth-trigger sits nearby.
# YYYY-MM-DD is listed first so it wins over the looser M/D/Y form.
_DATE_FORMS = (
    r'(?:'
    r'\d{4}-\d{1,2}-\d{1,2}'                                        # YYYY-MM-DD
    r'|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}'                             # M/D/YYYY, M-D-YYYY
    r'|' + _MONTH_NAME + r'\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}'    # Month D, YYYY
    r'|\d{1,2}(?:st|nd|rd|th)?\s+' + _MONTH_NAME + r'\s+\d{4}'      # D Month YYYY
    r')'
)
# A birth-trigger followed, within a short same-line window, by a date.
# The window (no newline, no digit) covers separators and connective words
# like ":", "on", "is/was", so bare dates without a nearby birth word are
# NOT matched. Full match (trigger through date) is redacted, removing the
# date. "b." is kept tight (whitespace only) to limit false positives.
_DOB_PATTERN = re.compile(
    r'(?:'
    r'\b(?:DOB|date\s+of\s+birth|birth\s*date|birthday|born)[^\n\d]{0,20}?'
    r'|\bb\.\s*'
    r')'
    + _DATE_FORMS,
    re.IGNORECASE,
)


class PIIDetection:
    """Result of scanning content for PII."""

    def __init__(self):
        self.emails = []
        self.phones = []
        self.ssns = []
        self.addresses = []
        self.credit_cards = []
        self.names = []
        self.dates_of_birth = []
        #: health.condition — free-text disclosures (chat/voice paths), and the
        #: class name schema-position tagging emits for calendar.meds[] and
        #: freshtable.familyMembers[].prefs. Same class, two signal sources.
        self.health_conditions = []
        #: child.any — populated ONLY by structured signals today
        #: (fitfam.users{} age < 18, calendar.family[] role "child"). There is
        #: no free-text minor detector and pretending otherwise would be the
        #: fail-open this class exists to avoid.
        self.child_indicators = []

    @property
    def has_pii(self) -> bool:
        return bool(
            self.emails or self.phones or self.ssns or
            self.addresses or self.credit_cards or
            self.names or self.dates_of_birth or
            self.health_conditions or self.child_indicators
        )

    @property
    def severity(self) -> str:
        # child.any is critical: a record about a minor is the one class where
        # the cost of under-classifying is not recoverable by the owner later.
        if self.ssns or self.credit_cards or self.child_indicators:
            return "critical"
        # health.condition sits at high, alongside address and DOB. Structured
        # health (calendar.meds[]) is P4/critical in app_data_tags because the
        # schema position is proof; a free-text mention is a weaker signal and
        # is tiered honestly rather than rounded to match.
        if self.addresses or self.dates_of_birth or self.health_conditions:
            return "high"
        if self.emails or self.phones or self.names:
            return "medium"
        return "none"

    @property
    def pii_types(self) -> list:
        types = []
        if self.emails: types.append("email")
        if self.phones: types.append("phone")
        if self.ssns: types.append("ssn")
        if self.addresses: types.append("address")
        if self.credit_cards: types.append("credit_card")
        if self.names: types.append("name")
        if self.dates_of_birth: types.append("date_of_birth")
        if self.health_conditions: types.append("health.condition")
        if self.child_indicators: types.append("child.any")
        return types

    def summary(self) -> str:
        if not self.has_pii:
            return "No PII detected."
        parts = []
        if self.emails: parts.append(f"{len(self.emails)} email(s)")
        if self.phones: parts.append(f"{len(self.phones)} phone number(s)")
        if self.ssns: parts.append(f"{len(self.ssns)} SSN(s)")
        if self.addresses: parts.append(f"{len(self.addresses)} address(es)")
        if self.credit_cards: parts.append(f"{len(self.credit_cards)} credit card(s)")
        if self.names: parts.append(f"{len(self.names)} labeled name(s)")
        if self.dates_of_birth: parts.append(f"{len(self.dates_of_birth)} DOB(s)")
        if self.health_conditions:
            parts.append(f"{len(self.health_conditions)} health mention(s)")
        if self.child_indicators:
            parts.append(f"{len(self.child_indicators)} child record(s)")
        return f"PII detected [{self.severity}]: {', '.join(parts)}"


class DataClassifier:
    """Classifies files and data content into security zones."""

    _PERSONAL_EXTENSIONS = {
        '.vcf', '.vcard', '.ics', '.csv', '.xlsx', '.xls',
    }
    _PERSONAL_FILENAMES = {
        'contacts', 'address_book', 'passwords', 'credentials',
        'medical', 'health', 'insurance', 'tax', 'financial',
        'bank', 'social_security', 'passport', 'license',
    }

    def classify_file(self, filepath) -> DataZone:
        """Determine the security zone for a file."""
        filepath = Path(filepath)
        zone = get_zone_for_file(filepath.name)
        if zone is not None:
            return zone

        relative = filepath.as_posix()
        for zone, config in ZONE_CLASSIFICATIONS.items():
            for pattern in config["files"]:
                if pattern.endswith("/") and pattern.rstrip("/") in relative:
                    return zone

        if filepath.suffix.lower() in self._PERSONAL_EXTENSIONS:
            return DataZone.VAULT
        stem = filepath.stem.lower()
        for keyword in self._PERSONAL_FILENAMES:
            if keyword in stem:
                return DataZone.VAULT
        return DataZone.INTELLIGENCE

    def classify_content(self, text: str, *, include_health: bool = False):
        """Classify text content by scanning for PII."""
        pii = self.scan_for_pii(text, include_health=include_health)
        if pii.has_pii:
            return DataZone.VAULT, pii
        return DataZone.INTELLIGENCE, pii

    def scan_structured_document(self, app, doc) -> PIIDetection:
        """Detect ``health.condition`` / ``child.any`` from SCHEMA POSITION.

        The structured half of the two new detectors. Given an app name and its
        document, it reports how many entries sit in positions the taxonomy
        already classifies (``calendar.meds[]``, ``fitfam.users{}`` with an age
        under 18, ``freshtable.familyMembers[].prefs``) WITHOUT reading a single
        value: the returned detection carries opaque position markers, never
        content, so a caller cannot accidentally log or vault household data by
        holding the result.

        This is why the two classes need no NLP for the cases that matter — the
        field name is the evidence. Never raises.
        """
        detection = PIIDetection()
        try:
            from puffpai.security import app_data_tags
            counts = app_data_tags.structured_detection_classes(app, doc)
        except Exception:
            return detection
        for cls, count in counts.items():
            # Position markers, not values: "calendar.meds[]#1". A caller that
            # redacts on these finds nothing to replace, which is correct — a
            # structured document is tagged and vaulted, never string-scrubbed.
            markers = [f"{app}:{cls}#{i + 1}" for i in range(int(count))]
            if cls == app_data_tags.CLS_HEALTH_CONDITION:
                detection.health_conditions.extend(markers)
            elif cls == app_data_tags.CLS_CHILD:
                detection.child_indicators.extend(markers)
        return detection

    def scan_for_pii(self, text: str, *,
                     include_health: bool = False) -> PIIDetection:
        """Scan text for personally identifiable information.

        ``include_health`` turns on the free-text ``health.condition`` detector
        and DEFAULTS OFF. That default is a deliberate scoping decision, not an
        oversight: the conversational surfaces (chat, voice, log lines) opt in,
        because a medication mentioned there is retained in chat history for no
        reason the owner asked for. Document ingest does NOT opt in, because a
        medical document the owner deliberately uploaded is supposed to be
        searchable, and redacting its clinical terms would gut the thing they
        asked the appliance to do. Turning this on globally is therefore a
        product decision about upload utility, not a security improvement, and
        it is left to be made explicitly.
        """
        detection = PIIDetection()
        if not text or not isinstance(text, str):
            return detection

        scan_text = text[:500_000]
        detection.emails = _EMAIL_PATTERN.findall(scan_text)
        detection.phones = _PHONE_PATTERN.findall(scan_text)
        # SSN: keep only candidates that satisfy the SSA structural rules.
        # Two shapes, one bucket: the written form (XXX-XX-XXXX / XXX XX XXXX)
        # and the spoken form (a bare 9-digit run with an SSN cue in front of
        # it). Both land in detection.ssns, so severity ("critical"), the
        # [REDACTED-SSN] marker and the scrub-and-vault routing are identical
        # for a typed SSN and a dictated one.
        detection.ssns = [
            m.group(0) for m in _SSN_CANDIDATE.finditer(scan_text)
            if _valid_ssn(m.group(1), m.group(3), m.group(4))
        ] + _cued_bare_ssns(scan_text)
        detection.addresses = _ADDRESS_PATTERN.findall(scan_text)
        # Credit card: keep only 13-19 digit candidates that pass Luhn.
        credit_cards = []
        for m in _CREDIT_CARD_PATTERN.finditer(scan_text):
            digits = re.sub(r'\D', '', m.group(0))
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                credit_cards.append(m.group(0))
        detection.credit_cards = credit_cards
        detection.dates_of_birth = _DOB_PATTERN.findall(scan_text)
        name_matches = _LABELED_NAME_PATTERN.findall(scan_text)
        detection.names = [m.strip() for m in name_matches if len(m.strip()) > 2]
        if include_health:
            detection.health_conditions = _health_disclosures(scan_text)
        return detection

    def redact_pii(self, text: str, *,
                   include_health: bool = False) -> tuple[str, PIIDetection]:
        """Scan text for PII and return (redacted_text, detection).

        Replaces detected PII with bracketed placeholders:
          SSN 123-45-6789 → [REDACTED-SSN]
          Credit card → [REDACTED-CC]
          DOB → [REDACTED-DOB]
          Email → [REDACTED-EMAIL]
          Phone → [REDACTED-PHONE]
          Address → [REDACTED-ADDRESS]
          Name → [REDACTED-NAME]
          Health term (include_health only) → [REDACTED-HEALTH]
        """
        detection = self.scan_for_pii(text, include_health=include_health)
        if not detection.has_pii:
            return text, detection

        redacted = text
        # Order matters: replace longer/more specific patterns first
        for ssn in detection.ssns:
            redacted = redacted.replace(ssn, '[REDACTED-SSN]')
        for cc in detection.credit_cards:
            redacted = redacted.replace(cc, '[REDACTED-CC]')
        for dob in detection.dates_of_birth:
            redacted = redacted.replace(dob, '[REDACTED-DOB]')
        for addr in detection.addresses:
            redacted = redacted.replace(addr, '[REDACTED-ADDRESS]')
        for email in detection.emails:
            redacted = redacted.replace(email, '[REDACTED-EMAIL]')
        for phone in detection.phones:
            redacted = redacted.replace(phone, '[REDACTED-PHONE]')
        for name in detection.names:
            redacted = redacted.replace(name, '[REDACTED-NAME]')
        for term in detection.health_conditions:
            # Case-insensitive and word-bounded: "Insulin" at the start of a
            # sentence must not survive because the captured span was lowercase,
            # and "meds" must not eat the "meds" inside "medsafe".
            redacted = re.sub(r'\b' + re.escape(term) + r'\b',
                              '[REDACTED-HEALTH]', redacted,
                              flags=re.IGNORECASE)
        return redacted, detection

    def should_encrypt(self, filepath, content: str = "") -> bool:
        """Determine if a file should be encrypted."""
        zone = self.classify_file(filepath)
        policy = ZONE_CLASSIFICATIONS[zone]["policy"]
        if policy.encrypted_at_rest:
            return True
        if content:
            _, pii = self.classify_content(content)
            if pii.has_pii:
                return True
        return False
