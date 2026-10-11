"""The bytes half of the Zone 2 sourcing step, kept apart from the prompting.

Integrate's ``apply`` is the only place in the wizard that downloads, and it
does it through the door that already exists: ``setup_ops.source_preview``
(the consent screen: license, size, address, all from the registry, no socket)
and then ``setup_ops.fetch_source`` with the acknowledgement a person gave.
**It adds no network call site of its own** - the egress-door tests are the
contract, and they test that path.

What lives here rather than in the package: the loops, so the skill file stays
a readable statement of what Integrate decides.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

from typing import Any, Optional

from puffpai.wizard import sources_plan


def fetch_plan(items: list[dict[str, Any]], selected: list[str]
               ) -> tuple[list[str], list[str]]:
    """(fetch now, leave for later) for a selection, by size.

    A source bigger than :data:`sources_plan.HEAVY_BYTES` is **deferred by
    default**. Nobody should discover that their wizard is 55 GB into a
    download because they ticked "Wikipedia" two screens ago; the fetch command
    for a deferred source is recorded as a gap, which is a thing they can do
    later on purpose.
    """
    by_id = {item["id"]: item for item in items}
    now, later = [], []
    for source_id in selected:
        item = by_id.get(source_id) or {}
        size = item.get("bytes")
        if size is not None and int(size) > sources_plan.HEAVY_BYTES:
            later.append(source_id)
        else:
            now.append(source_id)
    return now, later


def run_fetches(source_ids: list[str], posture: dict,
                *, acknowledged: bool) -> dict[str, Any]:
    """Fetch each id through the existing path. Returns the sourcing halves.

    Nothing is downloaded without ``acknowledged``: the size and the license
    were on the card, and a run where nobody read the card is a run where
    nobody accepted them. A refusal is recorded, never raised - an undownloaded
    source is a gap, not a stop.
    """
    from puffpai import setup_ops

    fetched: list[dict[str, Any]] = []
    not_fetched: list[str] = []
    for source_id in source_ids:
        preview = setup_ops.source_preview(source_id, posture)
        if not preview.get("ok"):
            not_fetched.append(source_id)
            continue
        if not acknowledged:
            not_fetched.append(source_id)
            continue
        result = setup_ops.fetch_source(source_id, posture,
                                        size_acknowledged=True)
        if result.get("ok"):
            fetched.append(setup_ops.source_manifest_entry(result))
        else:
            not_fetched.append(source_id)
    return {"fetched": fetched, "not_fetched": not_fetched}


def own_data_block(named: list[str], *, skip: bool) -> dict[str, Any]:
    """Survey and approve exactly the folders that were named. Nothing else.

    The rule: a folder nobody named is never opened, and a declined folder
    leaves no trace - not a path, not a count.
    """
    from puffpai import setup_ops

    empty = setup_ops.data_sources_block({"approved": []})
    if skip or not named:
        return empty
    surveyed = setup_ops.survey_data_locations([str(p) for p in named])
    approve = [str(loc.get("path")) for loc in (surveyed.get("locations") or [])
               if loc.get("surveyed")]
    return setup_ops.data_sources_block(
        setup_ops.approve_data_locations(surveyed, approve))


def index_handoff(zim_dir: str, *, auto_index: bool,
                  start: bool = True) -> dict[str, Any]:
    """What is waiting, how long it will take, and whether it was started.

    The estimate is measured from this machine's own history or it is not given
    at all; there is no calibration run, for the same reason there never was
    one - calibrating means indexing in the foreground.
    """
    from puffpai import setup_ops

    if not zim_dir:
        return {"packs": 0, "estimate": None, "started": 0, "waiting": False}
    volume = setup_ops.pending_index_volume(zim_dir)
    packs = int(volume.get("packs") or 0)
    if not packs:
        return {"packs": 0, "estimate": None, "started": 0, "waiting": False}
    estimate = setup_ops.index_estimate_for(volume)
    started = 0
    if auto_index and start:
        started = setup_ops.start_indexing(zim_dir)
    return {"packs": packs, "estimate": estimate, "started": int(started),
            "waiting": True}


def seconds_from(estimate: Optional[dict[str, Any]]) -> tuple[Optional[float], str]:
    """(seconds, basis) for a card, or (None, "unknown"). Never invented."""
    if not isinstance(estimate, dict):
        return None, "unknown"
    basis = str(estimate.get("basis") or "unknown")
    if basis not in ("measured", "calibrated"):
        return None, "unknown"
    seconds = estimate.get("central_seconds")
    if not isinstance(seconds, (int, float)):
        return None, "unknown"
    return float(seconds), basis


__all__ = ["fetch_plan", "run_fetches", "own_data_block", "index_handoff",
           "seconds_from"]
