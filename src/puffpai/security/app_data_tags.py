"""Schema-position PII tagging for the HTML apps' documents.

The idea in one sentence: **where a value sits in the schema is a stronger
signal than what the value looks like.** ``calendar.meds[]`` holds medications
whether the entry reads "amoxicillin 500mg", "the blue one", or "hers, 8pm".
No regex sees the second and third; the field name sees all three.

WHAT THIS CLOSES
----------------
The two highest-tier stores on the appliance, Calendar's medication list and
FitFam's health history for a family member, possibly a minor, are invisible
to every one of the seven text detectors the appliance ships, and
``PUT /apps/<name>/data`` writes straight through without ever consulting the
classifier. Without this module the most sensitive household data would be
both untagged and unscanned. This module is the structured answer: it
needs no NLP, produces no false negatives on the fields it covers, and cannot be
defeated by phrasing.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* It does not read, transform, redact, or move a single value. It emits
  **classes, tiers, schema paths and counts** — that is the whole output
  vocabulary, and it is what lets ``/system/status`` answer "app data holding P4
  classes: N" without the status endpoint becoming a disclosure channel.
* It does not tag free text. A medication named in a Calendar event *title* is
  not in ``meds[]`` and this module will not find it; that is a declared gap,
  and the chat-path ``health.condition`` detector in
  :mod:`puffpai.security.data_classifier` covers a different slice of it.
THE LADDER
----------
P0 public · P1 household-internal · P2 personal · P3 sensitive-PII ·
P4 critical-PII. Unsure rounds UP. :data:`P_TIER_SEVERITY` bridges to the
three-level severity vocabulary the scrub/vault path already speaks, so the two
ladders can never disagree about what "worse" means.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

TIER_P3 = "P3"
TIER_P4 = "P4"

#: The bridge between this ladder and ``scrub_and_vault._TYPE_TIER``'s
#: critical/high/medium vocabulary. P4 is critical; P3 is high. Anything lower
#: never reaches this module, because this module only tags what it is sure
#: about and everything it is sure about is at least P3.
P_TIER_SEVERITY = {TIER_P4: "critical", TIER_P3: "high"}

#: Under this is a minor. The only two minor signals the schemas actually carry
#: are a numeric ``age`` and a ``role``/``type`` string of ``child``, and
#: pretending to more coverage than that would be
#: the exact fail-open this module exists to prevent.
MINOR_AGE_CEILING = 18

_CHILD_ROLE_WORDS = frozenset({"child", "kid", "minor"})

#: Data classes emitted. Names match the vault's data-class taxonomy so the vault
#: architect's retention rules key off the same strings.
CLS_HEALTH_CONDITION = "health.condition"
CLS_HEALTH_DEVICE_READING = "health.device_reading"
CLS_FINANCIAL = "financial.account_balance"
CLS_CHILD = "child.any"


def _entry_count(value) -> int:
    """How many entries a schema position holds. Never inspects them."""
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    if value in (None, "", 0, False):
        return 0
    return 1


def _tag(app, path, cls, tier, count):
    return {"app": app, "path": path, "class": cls, "tier": tier,
            "count": int(count), "severity": P_TIER_SEVERITY[tier]}


def _is_minor(entry) -> bool:
    """True when a person record carries a signal that they are a minor.

    Two signals, both structural: an ``age``/``years`` integer under
    :data:`MINOR_AGE_CEILING`, or a ``role``/``type``/``relationship`` string of
    child/kid/minor.

    A MISSING or unparseable age is NOT treated as a minor. That looks like a
    fail-open and is deliberately not one: tagging every ageless person record
    ``child.any`` would put a P4 flag on every adult in the house, and a tier
    that fires on everything is a tier the owner learns to ignore. The honest
    position: these are the only two signals that exist, and everything else
    is an unclosed gap, stated rather than papered over.
    """
    if not isinstance(entry, dict):
        return False
    for key in ("role", "type", "relationship"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip().lower() in _CHILD_ROLE_WORDS:
            return True
    for key in ("age", "years"):
        raw = entry.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            age = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            continue
        if 0 <= age < MINOR_AGE_CEILING:
            return True
    return False


def _people(container):
    """Yield person records from either a ``{name: {...}}`` map or a list.

    FitFam ships ``users`` as a map keyed by name and Calendar ships ``family``
    as a list; a tagger that only understood one shape would silently stop
    tagging the day either schema is normalised.
    """
    if isinstance(container, dict):
        for value in container.values():
            if isinstance(value, dict):
                yield value
    elif isinstance(container, (list, tuple)):
        for value in container:
            if isinstance(value, dict):
                yield value


def _tag_calendar(doc):
    tags = []
    meds = _entry_count(doc.get("meds"))
    if meds:
        # P4 and never-egress. A medication list joined to a named household is
        # health data under Art.9 no matter how each row is worded.
        tags.append(_tag("calendar", "calendar.meds[]",
                         CLS_HEALTH_CONDITION, TIER_P4, meds))
    bills = _entry_count(doc.get("bills"))
    if bills:
        tags.append(_tag("calendar", "calendar.bills[]",
                         CLS_FINANCIAL, TIER_P4, bills))
    # Calendar's family[] is the one place on the appliance a minor is
    # explicitly named (role: "child"), and that string is one of only two
    # minor signals that exist anywhere in the tree. Leaving the signal we DO
    # have unused, in a document that also holds meds and bills, would be
    # choosing the less restrictive option for no reason.
    minors = sum(1 for person in _people(doc.get("family")) if _is_minor(person))
    if minors:
        tags.append(_tag("calendar", "calendar.family[].role",
                         CLS_CHILD, TIER_P4, minors))
    return tags


def _tag_fitfam(doc):
    tags = []
    users = doc.get("users")
    minors = sum(1 for person in _people(users) if _is_minor(person))
    if minors:
        tags.append(_tag("fitfam", "fitfam.users{}.age",
                         CLS_CHILD, TIER_P4, minors))
    # Self-reported workouts and streaks are health readings, and in this schema
    # they are ALWAYS joined to a person's name — there is no de-identified
    # shape of this store — so the join that would normally promote the tier is
    # unconditional here.
    history = _entry_count(doc.get("history"))
    if history:
        tags.append(_tag("fitfam", "fitfam.history[]",
                         CLS_HEALTH_DEVICE_READING, TIER_P4, history))
    prefs = _entry_count(doc.get("prefs"))
    if prefs:
        tags.append(_tag("fitfam", "fitfam.prefs",
                         CLS_HEALTH_DEVICE_READING, TIER_P4, prefs))
    # Per-user history/prefs, for the shape where they hang off the user record
    # rather than the document root. Counted separately so the path stays true.
    per_user_history = 0
    per_user_prefs = 0
    for person in _people(users):
        per_user_history += _entry_count(person.get("history"))
        per_user_prefs += _entry_count(person.get("prefs"))
    if per_user_history:
        tags.append(_tag("fitfam", "fitfam.users{}.history[]",
                         CLS_HEALTH_DEVICE_READING, TIER_P4, per_user_history))
    if per_user_prefs:
        tags.append(_tag("fitfam", "fitfam.users{}.prefs",
                         CLS_HEALTH_DEVICE_READING, TIER_P4, per_user_prefs))
    return tags


def _tag_freshtable(doc):
    tags = []
    # Dietary preference is the most Art.9-adjacent field in the whole app set:
    # halal and kosher reveal religion, gluten-free and low-FODMAP reveal a
    # condition. GDPR Art.9 does not require the datum be LABELLED religion,
    # only that it reveals it — which is why this is tagged by position and not
    # by a lexicon that would miss most phrasings and over-fire on the rest.
    count = 0
    for member in _people(doc.get("familyMembers")):
        count += _entry_count(member.get("prefs"))
    if count:
        tags.append(_tag("freshtable", "freshtable.familyMembers[].prefs",
                         CLS_HEALTH_CONDITION, TIER_P4, count))
    return tags


def _tag_trivia(doc):
    """Trivia's player roster.

    Trivia stores player names AND ages. A first name typed free-hand by a
    child is undetectable by pattern (the labelled-name detector can never
    see it because there is no ``Name:`` cue), but the age field is a real
    structural minor signal. Tagging it is the whole reason schema position
    beats pattern here.

    ``results[]`` is tagged only when the roster actually contains a minor: a
    score history is a children's record because of WHO it is about, and
    asserting that with no minor signal present would be a guess. When a minor
    IS present the whole dated history is a child's performance record and the
    90-day retention rule for children's records attaches to it.
    """
    tags = []
    players = doc.get("players")
    minors = sum(1 for person in _people(players) if _is_minor(person))
    if minors:
        tags.append(_tag("trivia", "trivia.players[].age",
                         CLS_CHILD, TIER_P4, minors))
        results = _entry_count(doc.get("results"))
        if results:
            tags.append(_tag("trivia", "trivia.results[]",
                             CLS_CHILD, TIER_P4, results))
    return tags


#: app -> tagger. An app that is absent from this map produces no tags, which is
#: the correct default: a tag rule is a claim about a schema, and claiming one
#: for a schema nobody has read is worse than claiming none.
_TAGGERS = {
    "calendar": _tag_calendar,
    "fitfam": _tag_fitfam,
    "freshtable": _tag_freshtable,
    "trivia": _tag_trivia,
}

#: Apps this module never inspects, and why. Empty in this build: every app
#: store that carries household content goes through the ladder below.
SKIPPED_APPS: dict[str, str] = {}


def tag_document(app, doc) -> list:
    """Tags for one app document. Values are never read, only shapes counted.

    Returns ``[]`` for an unknown app, a non-dict document, or a document whose
    tagged positions are all empty. Never raises: a tagging failure must not be
    able to block a household's calendar write.
    """
    tagger = _TAGGERS.get(str(app or "").strip().lower())
    if tagger is None or not isinstance(doc, dict):
        return []
    try:
        return tagger(doc)
    except Exception:  # pragma: no cover - defensive; tagging is never fatal
        return []


def summarize_tags(tags) -> dict:
    """Counts-and-class-names roll-up. No values, no paths-to-values, no text.

    This is the exact shape ``/system/status`` publishes, so the reviewable
    claim "the status endpoint cannot leak household data" is a claim about this
    one function.
    """
    tags = list(tags or [])
    p4 = sorted({t["class"] for t in tags if t.get("tier") == TIER_P4})
    p3 = sorted({t["class"] for t in tags if t.get("tier") == TIER_P3})
    return {
        "p4_class_count": len(p4),
        "p3_class_count": len(p3),
        "p4_classes": p4,
        "p3_classes": p3,
        "tagged_positions": len(tags),
        "tagged_entries": sum(int(t.get("count", 0)) for t in tags),
        "apps_tagged": sorted({t["app"] for t in tags}),
    }


def tag_documents(docs) -> dict:
    """Summarise a ``{app: document}`` mapping in one call."""
    tags = []
    for app, doc in (docs or {}).items():
        if app in SKIPPED_APPS:
            continue
        tags.extend(tag_document(app, doc))
    summary = summarize_tags(tags)
    summary["skipped_apps"] = sorted(SKIPPED_APPS)
    return summary


def structured_detection_classes(app, doc) -> dict:
    """``{class: count}`` for the two classes the CLASSIFIER now owns.

    :mod:`puffpai.security.data_classifier` grows ``health.condition`` and
    ``child.any`` as real detectors; for structured documents this is where
    their signal comes from. Other classes tagged here (financial, device
    readings) are for the vault architect's retention rules and deliberately do
    not become classifier detectors — the classifier's job is the scrub path,
    and scrubbing a bill amount out of a chat turn is not something anything
    asked for.
    """
    out = {}
    for tag in tag_document(app, doc):
        cls = tag.get("class")
        if cls in (CLS_HEALTH_CONDITION, CLS_CHILD):
            out[cls] = out.get(cls, 0) + int(tag.get("count", 0))
    return out


__all__ = [
    "TIER_P3", "TIER_P4", "P_TIER_SEVERITY", "MINOR_AGE_CEILING",
    "CLS_HEALTH_CONDITION", "CLS_HEALTH_DEVICE_READING", "CLS_FINANCIAL",
    "CLS_CHILD", "SKIPPED_APPS",
    "tag_document", "tag_documents", "summarize_tags",
    "structured_detection_classes",
]
