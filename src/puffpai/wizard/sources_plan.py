"""The plan Sources writes and Integrate carries out. No bytes move here.

Sourcing is split in two: Sources decides *what this bot should learn from* and
Integrate decides *what is actually fetched now*. Both halves need the same
handful of facts about a registry row - its size in bytes, whether it is
documents or a live source, whether it can be fetched at all - so they live
here rather than in either package.

Three rules this module exists to keep:

* **Nothing here opens a socket.** Every figure is read from the shipped
  registry, so a plan can be drawn on an air-gapped machine.
* **A size is either known or it is not.** A row without ``size_mb`` carries no
  byte figure at all and drags the whole estimate's basis to ``unknown``,
  because a total that quietly treats "no idea" as zero is a total that lies.
* **Paid and keyed rows are listed and never selectable**: this build has
* nowhere safe to put a key, so the honest thing is
  to name the source, say what it costs, and refuse to pretend it is one click
  away.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

from typing import Any, Optional

MB = 1024 * 1024

#: The one threshold in the split. A source bigger than this is deferred by
#: default rather than downloaded while somebody waits at a wizard, and a plan
#: totalling more than this makes its card consequential (``heavy_bytes``).
#: There was no such number in ``cards.py`` to reuse; it is stated once, here,
#: so Sources and Integrate cannot disagree about what "heavy" means.
HEAVY_BYTES = 2 * 1024 * 1024 * 1024

#: The plain words a paid or keyed row carries on the card, and the status it
#: is recorded under: recorded, never entered.
NO_KEY_PATH = "declared, no key path yet"
KEYED_NOTE = ("costs money / needs a key; this build has nowhere safe to put "
              "the key yet")


def size_bytes(size_mb: Any) -> Optional[int]:
    """Bytes for a registry row's declared size, or ``None`` when unknown."""
    try:
        value = float(size_mb)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return int(value * MB)


def size_label(size_mb: Any) -> str:
    """Plain words for a size. Unknown stays unknown, and says so.

    One formatter for every size an owner reads (``cards.format_bytes``), so a
    row cannot say "about 57.2 GB" in words and 61.4 GB in the estimate beside
    it.
    """
    from puffpai.wizard import cards

    value = size_bytes(size_mb)
    if value is None:
        return "size not known"
    return f"about {cards.format_bytes(value)}"


def form_of(entry: dict[str, Any]) -> str:
    """``documents`` or ``live_source``, the way :mod:`build_sources` means it."""
    from puffpai.knowledge import build_sources

    return (build_sources.FORM_LIVE_SOURCE
            if str(entry.get("kind") or "") == "docs"
            else build_sources.FORM_DOCUMENTS)


def needs_key(entry: dict[str, Any]) -> bool:
    """Does this row need a credential? Nothing in the shipped registry does.

    Read from the row rather than from a list of ids on purpose: the registry
    grows, and a keyed row added to it must be caught by the code that refuses
    keys rather than by somebody remembering to update this module.
    """
    return bool(entry.get("requires_key") or entry.get("api_key")
                or entry.get("auth"))


def costs_money(entry: dict[str, Any]) -> bool:
    return bool(entry.get("paid") or entry.get("subscription")
                or entry.get("cost"))


def why_not_fetchable(entry: dict[str, Any]) -> str:
    """Why this row can never be selected here, or ``""`` if it can."""
    keyed, paid = needs_key(entry), costs_money(entry)
    if keyed and paid:
        return KEYED_NOTE
    if keyed:
        return ("needs a key; this build has nowhere safe to put the key yet")
    if paid:
        return ("costs money; a subscription is yours to hold, and this build "
                "has nowhere safe to put the key yet")
    return ""


def row(entry: dict[str, Any], *, reason: str = "",
        matched_terms: Optional[list[str]] = None,
        selected_by: Optional[list[str]] = None) -> dict[str, Any]:
    """One candidate as the card and the plan both carry it.

    Everything the owner has to see before a byte moves is on this row:
    license, size, commercial use, the address, and which word of their goal
    earned it a place.
    """
    source_id = str(entry.get("id") or "")
    why_not = why_not_fetchable(entry)
    return {
        "id": source_id,
        "label": str(entry.get("label") or source_id),
        "kind": str(entry.get("kind") or "unknown"),
        "form": form_of(entry),
        "domain": str(entry.get("domain") or ""),
        "what_it_is": str(entry.get("what_it_is") or ""),
        "license": str(entry.get("license") or ""),
        "license_url": str(entry.get("license_url") or ""),
        "commercial_use": bool(entry.get("commercial_use", False)),
        "size_mb": entry.get("size_mb"),
        "bytes": size_bytes(entry.get("size_mb")),
        "size_label": size_label(entry.get("size_mb")),
        "source_url": str(entry.get("source_url") or ""),
        "matched_terms": [str(t) for t in (matched_terms or [])],
        "reason": reason,
        "selected_by": [str(p) for p in (selected_by or [])],
        "selectable": not why_not,
        "why_not": why_not,
        "status": NO_KEY_PATH if why_not else "",
    }


def option_label(item: dict[str, Any]) -> str:
    """The one line an owner reads before choosing a source."""
    parts = [item["label"], item["size_label"], item["license"] or "license unstated"]
    parts.append("commercial use ok" if item["commercial_use"]
                 else "not for commercial use")
    parts.append("a live source, not a shelf of documents"
                 if item["form"] == "live_source" else "documents")
    return " - ".join(p for p in parts if p)


def plan_rows(goal: str, packs: list[str], *,
              registry: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The deterministic half of Sources: registry search plus the pack picks.

    Two inputs, both the owner's: the goal they typed, matched against the
    registry by today's code (which cites the word that matched), and the
    ``pack:*`` ids they chose in the interview, mapped onto registry rows by
    :data:`build_sources.PACK_SOURCE_IDS`.

    ``offered`` stays exactly what today's sourcing step recorded - the search
    result, in search order - because the build manifest and
    :func:`build_sources.source_status` are already keyed on it. Pack-mapped
    rows are added to ``items`` for the card and not to ``offered``.
    """
    from puffpai import source_registry
    from puffpai.knowledge import build_sources

    result = source_registry.search_summary(goal, registry)
    items: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    offered: list[str] = []

    for candidate in result.get("candidates") or []:
        entry = source_registry.get_source(str(candidate.get("id")), registry)
        if entry is None:
            continue
        item = row(entry,
                   reason=str(candidate.get("reason") or ""),
                   matched_terms=candidate.get("matched_terms") or [])
        offered.append(item["id"])
        items.append(item)
        seen[item["id"]] = item

    unmapped: list[str] = []
    for pack in packs:
        mapped = build_sources.PACK_SOURCE_IDS.get(pack)
        if not mapped:
            unmapped.append(pack)
            continue
        for source_id in mapped:
            known = seen.get(source_id)
            if known is not None:
                if pack not in known["selected_by"]:
                    known["selected_by"].append(pack)
                continue
            entry = source_registry.get_source(source_id, registry)
            if entry is None:
                continue
            item = row(entry,
                       reason=f"You chose {pack} when you said what this bot "
                              f"is for.",
                       selected_by=[pack])
            items.append(item)
            seen[item["id"]] = item

    return {
        "goal": str(result.get("goal") or ""),
        "message": result.get("message"),
        "note": result.get("note"),
        "items": items,
        "offered": offered,
        "unmapped_packs": unmapped,
    }


def default_selection(items: list[dict[str, Any]]) -> list[str]:
    """What the card proposes: every fetchable row a pack picked, and no more.

    A row the owner's interview chose is a row they already said they wanted.
    A row the search merely matched is a suggestion, and suggestions start
    unticked - the wizard is not allowed to enrol somebody in 55 GB because
    their goal contained the word "reference".
    """
    return [item["id"] for item in items
            if item["selectable"] and item["selected_by"]]


def total_bytes(items: list[dict[str, Any]],
                ids: list[str]) -> tuple[int, bool]:
    """(bytes, every size known) for a selection. An unknown size is never 0."""
    wanted = set(ids)
    total, all_known = 0, True
    for item in items:
        if item["id"] not in wanted:
            continue
        if item["bytes"] is None:
            all_known = False
            continue
        total += int(item["bytes"])
    return total, all_known


def tool_needs(goal: str, items: list[dict[str, Any]],
               selected: list[str]) -> list[dict[str, Any]]:
    """What a bot like this will need to reach for. Ids, not prose.

    Sources' second output: Sources names the needs and Tools resolves them.
    The ids are the catalog's own (``integration:weather``), matched from the
    goal by the same deterministic matcher the build interview uses, so nothing
    here invents a capability that does not ship.

    A live source in the plan is a need of its own kind: something has to be
    able to reach it at runtime, and saying so here is how Tools finds out.
    """
    from puffpai import build_interview

    needs: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        proposal = build_interview.propose(goal or "")
    except Exception:  # noqa: BLE001 - no catalog is "no needs", never a stop
        proposal = {"flat": []}
    for match in proposal.get("flat") or []:
        kind = str(match.get("kind") or "")
        if kind not in ("app", "integration"):
            continue
        need_id = str(match.get("id") or "")
        if not need_id or need_id in seen:
            continue
        seen.add(need_id)
        terms = ", ".join(str(t) for t in (match.get("matched_terms") or []))
        needs.append({
            "id": need_id,
            "label": str(match.get("label") or need_id),
            "kind": kind,
            "why": f"Your goal asked for it: {terms}." if terms
                   else "Matched what this bot is for.",
        })

    chosen = set(selected)
    for item in items:
        if item["id"] not in chosen or item["form"] != "live_source":
            continue
        need_id = f"source:{item['id']}"
        if need_id in seen:
            continue
        seen.add(need_id)
        needs.append({
            "id": need_id,
            "label": item["label"],
            "kind": "live_source",
            "why": "A live source, so something has to be able to reach it "
                   "while the bot is running.",
        })
    return needs


__all__ = [
    "HEAVY_BYTES", "KEYED_NOTE", "NO_KEY_PATH",
    "size_bytes", "size_label", "form_of", "needs_key", "costs_money",
    "why_not_fetchable", "row", "option_label", "plan_rows",
    "default_selection", "total_bytes", "tool_needs",
]
