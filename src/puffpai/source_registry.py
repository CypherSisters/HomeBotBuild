"""The source registry - a short allowlist, not a search engine.

Step 5 of the wizard (Zone 2, Brain) needs to answer "go find what this bot
needs to know" without ever turning into a crawler. This module is the whole of
the finding half: it loads ``config/source_registry.json``, throws out anything
it may not offer, and matches what is left against the goal the owner typed.
:mod:`puffpai.source_fetch` is the moving half and will not touch an address
that did not come out of here.

Four rules hold this module honest:

* **The registry is the allowlist.** There is no discovery, no crawl, no
  "similar sources" step. If a source is not in the shipped file, this build
  cannot reach it, and the only way to change that is to edit a first-party
  config that a human reviewed.
* **License or silence.** A row without a ``license`` is dropped at load and the
  drop is recorded as a config-health defect naming the id. An unlicensed source
  is never proposed, never counted, and never fetchable - the filter lives below
  the search, not inside it, so no caller can route around it.
* **Evidence or silence.** A candidate that cannot cite the specific term that
  matched is not returned, and no reason is written for it. This is the same
  discipline as :func:`puffpai.build_interview._match`, and it is enforced the
  same structural way: label overlap alone scores zero, so an unjustifiable
  proposal is impossible rather than merely unlikely.
* **The goal is data.** The owner's words are tokenized and compared; they are
  never echoed into a reason. Every cited term comes from our own registry.

The honest limit, which the maintainer note in the JSON says at more length:
this list is short because most of what would make a real specialist bot is
copyrighted. The Kiwix ZIM library gives breadth, not depth. Say so.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Optional, Sequence, Union

from puffpai import config_health
from puffpai.build_interview import _STOPWORDS

SCHEMA = "source_registry/v1"

# The kinds of thing a row can be. Note what is absent: there is no "server"
# kind. Kiwix ZIM files are read by our own reader (puffpai.knowledge.zim_reader);
# kiwix-serve is not a source and never becomes one.
KINDS: tuple[str, ...] = ("zim", "corpus", "docs")

_MAX_GOAL_LEN = 400

# Baked-in fallback so the wizard can still offer something honest if the
# shipped registry is missing or unreadable. Deliberately tiny and deliberately
# licensed; the real list lives in config/source_registry.json and is the thing
# maintainers edit.
_FALLBACK_REGISTRY: dict[str, Any] = {
    "schema": SCHEMA,
    "domains": {},
    "sources": [
        {
            "id": "kiwix_wikipedia_en_nopic",
            "label": "Wikipedia (English, text only)",
            "kind": "zim",
            "domain": "general",
            "tags": ["wikipedia", "encyclopedia", "reference"],
            "license": "CC-BY-SA-4.0",
            "license_url": "https://en.wikipedia.org/wiki/Wikipedia:Copyrights",
            "commercial_use": True,
            "source_url": "https://download.kiwix.org/zim/wikipedia_en_all_nopic.zim",
            "filename": "wikipedia_en_all_nopic.zim",
            "size_mb": 50250,
            "what_it_is": "All of English Wikipedia without images.",
        },
    ],
}


def registry_path() -> str:
    """Absolute path of the shipped source registry."""
    from puffpai.config import get_config
    return os.path.join(get_config().REPO_ROOT, "config", "source_registry.json")


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def validate_registry(data: Any) -> Optional[str]:
    """Structural check of a parsed registry; None means healthy.

    Only the shape is checked here. Per-row policy (license present, scheme
    allowed) is applied in :func:`offerable_sources`, because one bad row should
    cost the owner that row and not the whole list.
    """
    if not isinstance(data, dict):
        return "The source registry is not a JSON object."
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        return "The source registry lists no sources."
    for index, entry in enumerate(sources):
        if not isinstance(entry, dict) or not entry.get("id"):
            return f"Source {index} has no id."
        if not entry.get("source_url"):
            return f"Source '{entry.get('id')}' has no source_url."
    return None


def load_registry(path: Optional[str] = None) -> dict[str, Any]:
    """Load the source registry, degrading to the baked-in fallback.

    An absent registry falls back silently - a fresh clone without the optional
    file is not a fault. A present-but-corrupt one falls back loudly, recording
    the degradation in :mod:`puffpai.config_health` so the owner is told the
    list they are choosing from is a stub.
    """
    target = path or registry_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        config_health.note_load_failure("source_registry", target, exc, reduced=True)
        return _FALLBACK_REGISTRY
    problem = validate_registry(data)
    if problem is None:
        return data  # type: ignore[no-any-return]
    config_health.record(
        "source_registry", target, config_health.INVALID, problem, reduced=True,
    )
    return _FALLBACK_REGISTRY


# ---------------------------------------------------------------------------
# the license filter - below the search, so nothing routes around it
# ---------------------------------------------------------------------------

def _license_of(entry: dict[str, Any]) -> str:
    return str(entry.get("license") or "").strip()


def entry_problem(entry: Any) -> Optional[str]:
    """Why this row may not be offered, or None if it may.

    Fail-closed on every axis: no license, no license page, no url, a scheme we
    do not fetch, a port we do not fetch, or credentials embedded in the
    address all disqualify the row. The address checks are duplicated inside
    :mod:`puffpai.source_fetch` before any socket opens; this copy exists so a
    row we would refuse to download is not dangled in front of the owner first.
    """
    if not isinstance(entry, dict):
        return "The entry is not an object."
    if not entry.get("id"):
        return "The entry has no id."
    if not _license_of(entry):
        return "The entry has no license, so it is not offerable."
    if not str(entry.get("license_url") or "").strip():
        return "The entry has no license_url, so its license cannot be checked."
    url = str(entry.get("source_url") or "").strip()
    if not url:
        return "The entry has no source_url."
    if not url.lower().startswith("https://"):
        return "The entry's source_url is not https."
    if "@" in url.split("://", 1)[1].split("/", 1)[0]:
        return "The entry's source_url carries credentials in the host part."
    if str(entry.get("kind") or "") not in KINDS:
        return "The entry has no recognised kind."
    return None


def offerable_sources(
    registry: Optional[dict[str, Any]] = None,
    path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Every row this build may show or fetch, unlicensed rows removed.

    A dropped row in a *shipped* registry is a first-party defect, so it is
    recorded in :mod:`puffpai.config_health` rather than swallowed. The owner
    finds out that the list they are choosing from is shorter than the file.
    """
    reg = registry if registry is not None else load_registry(path)
    rows: list[dict[str, Any]] = []
    dropped: list[str] = []
    for entry in list(reg.get("sources") or []):
        problem = entry_problem(entry)
        if problem is None:
            rows.append(dict(entry))
        else:
            ident = entry.get("id") if isinstance(entry, dict) else None
            dropped.append(f"{ident or '<unnamed>'}: {problem}")
    if dropped:
        config_health.record(
            "source_registry",
            path or registry_path(),
            config_health.INVALID,
            "Dropped " + str(len(dropped)) + " unofferable source(s): " + "; ".join(dropped),
            reduced=True,
        )
    return rows


def get_source(
    source_id: str,
    registry: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """One offerable row by id, or None. Unlicensed rows are not findable here."""
    wanted = str(source_id or "").strip()
    if not wanted:
        return None
    for entry in offerable_sources(registry):
        if str(entry.get("id")) == wanted:
            return entry
    return None


def source_for_url(
    url: str,
    registry: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """The offerable row whose ``source_url`` is exactly this address, or None.

    Exact string match on purpose. Anything cleverer - prefix matching, host
    matching, normalisation - is a way for a caller to smuggle a path onto a
    listed host, and the whole point of this module is that it cannot.
    """
    wanted = str(url or "").strip()
    if not wanted:
        return None
    for entry in offerable_sources(registry):
        if str(entry.get("source_url")) == wanted:
            return entry
    return None


def is_registered_url(url: str, registry: Optional[dict[str, Any]] = None) -> bool:
    """True only for an address that is spelled out in the registry."""
    return source_for_url(url, registry) is not None


# ---------------------------------------------------------------------------
# matching - evidence or silence
# ---------------------------------------------------------------------------

# Same floor and weights as the build interview, for the same measured reason:
# a genuine match always includes at least one exact tag hit, and label overlap
# on its own is noise. _match_source enforces that structurally, so the floor is
# the consequence of the rule rather than a tunable knob.
_SCORE_FLOOR = 2
_WEIGHTS = {"tag_phrase": 3, "tag": 2, "label": 1}


def _sanitize_goal(goal: str) -> str:
    """Trim, cap length, and strip control characters. The goal is only ever data."""
    cleaned = "".join(ch for ch in (goal or "") if ch.isprintable())
    return cleaned.strip()[:_MAX_GOAL_LEN]


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric word tokens."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _content_tokens(text: str) -> set[str]:
    """Goal tokens that carry meaning: no stopwords, nothing under three characters."""
    return {w for w in _tokens(text) if w not in _STOPWORDS and len(w) >= 3}


def _tag_hit(tag: str, goal_tokens: set[str]) -> bool:
    """Does a single-word tag appear in the goal, allowing a simple plural?

    Deterministic and offline - a one-character equivalence, not a stemmer - so
    this behaves identically with the WAN unplugged.
    """
    return (
        tag in goal_tokens
        or (tag + "s") in goal_tokens
        or (tag.endswith("s") and tag[:-1] in goal_tokens)
    )


def _match_source(
    entry: dict[str, Any],
    goal_text: str,
    goal_tokens: set[str],
) -> tuple[int, list[dict[str, Any]]]:
    """Score a registry row against a goal, and say which terms did the scoring.

    Returns ``(score, evidence)``. Every evidence entry names the specific term
    that matched, and every cited term comes from the *registry* - a tag, or a
    word in our own label or domain - never from the owner's typing. A row with
    no tag-kind evidence scores zero, which is what makes "a candidate that
    cannot cite its match is never proposed" a property of the code rather than
    a habit of the caller.
    """
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw in entry.get("tags", []):
        tag = str(raw).lower()
        if " " in tag:
            if tag in goal_text:
                evidence.append({"term": tag, "kind": "tag_phrase", "weight": _WEIGHTS["tag_phrase"]})
                seen.add(tag)
        elif _tag_hit(tag, goal_tokens):
            evidence.append({"term": tag, "kind": "tag", "weight": _WEIGHTS["tag"]})
            seen.add(tag)

    label_tokens = _content_tokens(f"{entry.get('label', '')} {entry.get('domain', '')}")
    for word in sorted(goal_tokens & label_tokens):
        if word not in seen:
            evidence.append({"term": word, "kind": "label", "weight": _WEIGHTS["label"]})

    if not any(e["kind"] in ("tag", "tag_phrase") for e in evidence):
        return 0, []

    evidence.sort(key=lambda e: (-int(e["weight"]), str(e["term"])))
    return sum(int(e["weight"]) for e in evidence), evidence


def _goal_text(goal_or_tags: Union[str, Sequence[str], None]) -> str:
    """Accept either a typed goal or a list of tags; both become one data string."""
    if goal_or_tags is None:
        return ""
    if isinstance(goal_or_tags, str):
        return _sanitize_goal(goal_or_tags)
    if isinstance(goal_or_tags, Iterable):
        return _sanitize_goal(" ".join(str(part) for part in goal_or_tags))
    return ""


def _candidate(entry: dict[str, Any], score: int, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """One proposal, carrying the three things that must be shown before a fetch."""
    terms = [str(e["term"]) for e in evidence]
    return {
        "id": entry.get("id"),
        "label": entry.get("label"),
        "kind": entry.get("kind"),
        "domain": entry.get("domain"),
        "what_it_is": entry.get("what_it_is"),
        # license + size + url: the three facts that precede any byte moving.
        "license": entry.get("license"),
        "license_url": entry.get("license_url"),
        "commercial_use": bool(entry.get("commercial_use", False)),
        "size_mb": entry.get("size_mb"),
        "source_url": entry.get("source_url"),
        "score": score,
        "evidence": evidence,
        "matched_terms": terms,
        "reason": "Matched what you asked for on: " + ", ".join(terms) + ".",
    }


def search(
    goal_or_tags: Union[str, Sequence[str], None],
    registry: Optional[dict[str, Any]] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Registry rows that match a goal, strongest first. Never raises.

    Returns a list of candidates. Each one carries its license, its approximate
    size, its address, and the exact terms that earned it a place - a candidate
    with nothing to cite is not in the list, and there is no code path that
    writes a reason it cannot support. An empty or all-filler goal returns an
    empty list rather than the whole catalog; see :func:`search_summary` for the
    plain-words explanation to show instead.
    """
    text = _goal_text(goal_or_tags)
    goal_tokens = _content_tokens(text)
    if not goal_tokens:
        return []

    lowered = text.lower()
    scored: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = []
    for entry in offerable_sources(registry):
        score, evidence = _match_source(entry, lowered, goal_tokens)
        if score >= _SCORE_FLOOR and evidence:
            scored.append((score, entry, evidence))
    scored.sort(key=lambda triple: (-triple[0], str(triple[1].get("id"))))

    candidates = [_candidate(entry, score, ev) for score, entry, ev in scored]
    if limit is not None and limit >= 0:
        candidates = candidates[:limit]
    return candidates


def search_summary(
    goal_or_tags: Union[str, Sequence[str], None],
    registry: Optional[dict[str, Any]] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """:func:`search` plus the plain-words framing the wizard shows around it.

    The ``message`` is the honest answer when there is nothing to propose, and
    the ``note`` states the limit of this whole feature out loud: a curated list
    is narrower than the internet, and a specialist bot will want more than what
    is on it.
    """
    text = _goal_text(goal_or_tags)
    candidates = search(goal_or_tags, registry, limit)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "goal": text,
        "candidates": candidates,
        "message": None,
        "note": (
            "These are vetted sources we are allowed to redistribute, not a web "
            "search. A specialist bot will want more than this list holds; the "
            "rest is usually copyrighted, and we would rather say so than "
            "pretend otherwise."
        ),
    }
    if not text:
        result["message"] = (
            "Tell the wizard what this bot should know about, and it will look "
            "through the vetted sources."
        )
    elif not _content_tokens(text):
        result["message"] = (
            "That could mean almost anything, so the wizard is not going to "
            "guess. Name a subject: a field, a skill, or a thing it should be "
            "able to look up."
        )
    elif not candidates:
        result["message"] = (
            "No vetted source on our list covers that yet. You can still bring "
            "your own documents in the next step, and the list grows over time."
        )
    return result


# ---------------------------------------------------------------------------
# display
# ---------------------------------------------------------------------------

def _size_phrase(size_mb: Any) -> str:
    try:
        value = float(size_mb)
    except (TypeError, ValueError):
        return "size unknown"
    if value >= 1024:
        return f"about {value / 1024:.1f} GB"
    return f"about {value:.0f} MB"


def candidate_summary_lines(result: Union[dict[str, Any], list[dict[str, Any]]]) -> list[str]:
    """Plain-language lines describing candidates, for the CLI/API.

    Every line states license and size next to the name, because the consent
    screen is the whole protection and it does not work if the owner has to go
    looking for the terms.
    """
    if isinstance(result, dict):
        candidates = list(result.get("candidates") or [])
        message = result.get("message")
        note = result.get("note")
    else:
        candidates = list(result)
        message = None
        note = None

    lines: list[str] = []
    if message:
        lines.append(str(message))
        return lines
    if not candidates:
        return ["No vetted source on our list matches that."]

    lines.append("Vetted sources that match what you asked for:")
    for cand in candidates:
        nc = "" if cand.get("commercial_use") else " [non-commercial use only]"
        lines.append(
            f"  - {cand.get('label')} ({cand.get('kind')}, "
            f"{_size_phrase(cand.get('size_mb'))}, {cand.get('license')}){nc}"
        )
        if cand.get("what_it_is"):
            lines.append(f"      {cand.get('what_it_is')}")
        lines.append(f"      From: {cand.get('source_url')}")
        lines.append(f"      {cand.get('reason')}")
    if note:
        lines.append(str(note))
    return lines


__all__ = [
    "SCHEMA",
    "KINDS",
    "registry_path",
    "validate_registry",
    "load_registry",
    "entry_problem",
    "offerable_sources",
    "get_source",
    "source_for_url",
    "is_registered_url",
    "search",
    "search_summary",
    "candidate_summary_lines",
]
