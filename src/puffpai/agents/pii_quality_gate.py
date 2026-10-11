"""Archivist-owned quality gate for the Scrub-and-Vault pipeline.

This module is the VAULT half of the Scrub-vs-Vault split. It answers a single
question, per detected PII span:

    Is this GOOD, real personal data that is worth storing (vault it), or is it
    test / placeholder / junk data that should be scrubbed but NOT stored?

The SCRUB half (reflexive, always-on redaction of every LLM-visible surface) is
deterministic and never depends on this gate — a span that fails this gate is
still redacted, it is just not persisted. That keeps the confidentiality
guarantee independent of any judgement call made here.

Ownership: the Archivist agent is the sole holder of Zone-1 (vault) access, so
the decision of *what earns a vault entry* is the Archivist's to make. This
module lives in the ``agents`` package and is imported/exposed by
:class:`puffpai.agents.archivist.ArchivistAgent` (``vault_quality_gate``). The
request-path choke point (:mod:`puffpai.security.scrub_and_vault`) consults this
gate; it never vaults on its own judgement.

"GOOD" for a structured type (SSN, credit card) means:
  * it passes the SAME structural validator the classifier uses to detect it
    (SSA structural rules for SSN, Luhn for cards), AND
  * it is not a well-known published test/placeholder value, AND
  * it is not structurally trivial (all-same-digit, or a strict sequential run).

For types without a strong structural validator (email, phone, name, address,
DOB) the gate is conservative in the safe direction: it vaults by default and
only rejects the obvious dummies (example.com, 555-01xx, "John Doe", etc.).
Rejecting here only costs a vault entry — the value is still scrubbed either way.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import re

# Reuse the EXACT validators the detector uses, so the gate cannot disagree with
# detection about what a structurally-valid SSN / card is.
from puffpai.security.data_classifier import _luhn_ok, _valid_ssn


# ── Test / placeholder reject lists (small, commented, extensible) ──────────
#
# These are values that are structurally valid (they pass Luhn / the SSA rules)
# but are published fakes — they show up in documentation, sample forms, unit
# tests, and demos. Storing them would pollute the vault with non-data. Add new
# known fakes here; keep them digits-only (separators are stripped before the
# lookup) except the SSN set, which is compared in canonical dashed form.

#: SSNs that are famously used as fakes / placeholders.
_TEST_SSNS = frozenset({
    "123-45-6789",   # sequential — the canonical fake SSN
    "000-00-0000",
    "111-11-1111",
    "078-05-1120",   # the "Woolworth wallet" SSN, the most-misused SSN ever
    "219-09-9999",   # SSA-published advertising sample
    "457-55-5462",   # SSA-published sample
})

#: Card PANs published by the networks / processors as test numbers. All pass
#: Luhn by construction, so only this explicit list (plus the trivial-run check)
#: keeps them out of the vault. Stored digits-only.
_TEST_CARDS = frozenset({
    "4111111111111111",   # Visa test
    "4012888888881881",   # Visa test
    "4222222222222",      # Visa test (13-digit)
    "4242424242424242",   # Stripe Visa test
    "4000056655665556",   # Stripe Visa (debit) test
    "5555555555554444",   # Mastercard test
    "5105105105105100",   # Mastercard test
    "2223003122003222",   # Mastercard (2-series) test
    "378282246310005",    # Amex test
    "371449635398431",    # Amex test
    "6011111111111117",   # Discover test
    "6011000990139424",   # Discover test
    "3530111333300000",   # JCB test
    "3566002020360505",   # JCB test
})

#: Obvious dummy email addresses / domains.
_TEST_EMAIL_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "test.com", "test.test",
    "email.com", "domain.com", "sample.com", "localhost", "none.com",
})
_TEST_EMAILS = frozenset({
    "test@test.com", "user@example.com", "john@example.com",
    "jane@example.com", "foo@bar.com", "a@a.com", "noreply@example.com",
})

#: Placeholder phone numbers. 555-01xx is the reserved fictional-US range; the
#: rest are common dummies. Compared digits-only.
_TEST_PHONES = frozenset({
    "0000000000", "1111111111", "1234567890", "9999999999",
    "5551234567", "5555555555", "18005551234", "8005551234",
})

#: Placeholder names people type when a form demands a name.
_TEST_NAMES = frozenset({
    "john doe", "jane doe", "test user", "first last", "john smith",
    "jane smith", "foo bar", "lorem ipsum", "n a", "no name",
})


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _all_same_digit(digits: str) -> bool:
    """True if every digit is identical (e.g. 4444...). Trivial, non-real."""
    return len(digits) >= 2 and len(set(digits)) == 1


def _is_sequential_run(digits: str) -> bool:
    """True for a strict ascending or descending consecutive run.

    Catches 123456789 / 0123456789 / 987654321 and the like — the kind of value
    a person taps to fill a required field, never a real SSN or card.
    """
    if len(digits) < 4:
        return False
    ds = [ord(c) - 48 for c in digits]
    asc = all(b - a == 1 for a, b in zip(ds, ds[1:]))
    desc = all(a - b == 1 for a, b in zip(ds, ds[1:]))
    return asc or desc


def _trivial_number(digits: str) -> bool:
    return _all_same_digit(digits) or _is_sequential_run(digits)


def _canonical_ssn(value: str) -> str:
    """Return the SSN in canonical ``NNN-NN-NNNN`` form for reject-list lookup.

    The detector matches SSNs with a consistent separator that may be a dash or
    a space; normalize to dashes so a spaced fake matches the dashed reject set.
    """
    d = _digits(value)
    if len(d) != 9:
        return value.strip()
    return f"{d[:3]}-{d[3:5]}-{d[5:]}"


def is_vault_worthy(pii_type: str, value: str) -> bool:
    """Return True if ``value`` is GOOD data worth storing in the vault.

    A False result means "scrub it but do not vault it" — the value is test /
    placeholder / structurally-invalid data. Never raises: any unexpected input
    is treated as NOT vault-worthy (fail toward not storing junk; the value is
    scrubbed regardless).
    """
    try:
        if not value or not isinstance(value, str):
            return False
        t = (pii_type or "").lower()
        digits = _digits(value)

        if t == "ssn":
            canon = _canonical_ssn(value)
            if canon in _TEST_SSNS:
                return False
            if len(digits) != 9:
                return False
            if _trivial_number(digits):
                return False
            # Reuse the detector's SSA structural validator.
            return _valid_ssn(digits[:3], digits[3:5], digits[5:])

        if t in ("credit_card", "card", "cc"):
            if digits in _TEST_CARDS:
                return False
            if not (13 <= len(digits) <= 19):
                return False
            if _trivial_number(digits):
                return False
            # Reuse the detector's Luhn check.
            return _luhn_ok(digits)

        if t == "email":
            v = value.strip().lower()
            if v in _TEST_EMAILS:
                return False
            domain = v.rsplit("@", 1)[-1] if "@" in v else ""
            if domain in _TEST_EMAIL_DOMAINS:
                return False
            return True

        if t == "phone":
            if digits in _TEST_PHONES:
                return False
            # 555-01xx is the reserved fictional range (any area code).
            if len(digits) >= 7 and digits[-7:-4] == "555" and \
                    digits[-4:-2] == "01":
                return False
            if _trivial_number(digits):
                return False
            return True

        if t == "name":
            if value.strip().lower() in _TEST_NAMES:
                return False
            return True

        # address, date_of_birth, and any future type: no strong structural
        # validator and no cheap dummy signature, so vault by default. Scrubbing
        # already happened regardless; the only cost of a false "worthy" here is
        # one extra encrypted vault entry.
        if t in ("address", "date_of_birth", "dob"):
            return True

        # Unknown type: conservative — do not vault something we can't reason
        # about. It is still scrubbed by the caller.
        return False
    except Exception:
        return False


__all__ = ["is_vault_worthy"]
