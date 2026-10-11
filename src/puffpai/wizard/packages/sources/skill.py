"""Sources: where this bot learns from. The plan, and not one byte.

Sourcing is split in two so that asking never downloads: this half asks only
"what should it learn from", writes that down, and hands it to Integrate.
Nothing here opens a socket, and the card says so in so many words: *this is a
plan, nothing is downloaded yet.*

What the plan carries:

* the deterministic registry search over the goal - the matcher, which cites
  the word that matched - plus the ``pack:*`` ids the owner chose in the
  interview, mapped onto registry rows by ``build_sources.PACK_SOURCE_IDS``;
* paid and keyed rows **listed and unselectable**: they exist, they cost money
* or need a key, and this build has nowhere
  safe to put a key, so they are recorded as ``declared, no key path yet`` and
  gapped rather than half-offered;
* on an online build only, model suggestions from outside the registry as
  ``info`` rows labelled "not vetted: check the
  license yourself", dropped unless their hostname is registrable, never
  fetchable from here;
* the **tool needs** this kind of bot implies, as plain catalog ids, for the
  Tools skill to resolve later.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

from typing import Any

from puffpai.wizard import sources_plan, sources_suggest

#: The words that go on the card, unchanged, every time.
PLAN_NOTE = "This is a plan, nothing is downloaded yet."


def _interview(ctx) -> dict:
    """What the owner said this bot is for. Persona first, shared as fallback."""
    try:
        persona = ctx.read("persona")
    except Exception:  # noqa: BLE001 - an undeclared read is the runner's business
        persona = {}
    interview = persona.get("interview") if isinstance(persona, dict) else None
    if not interview:
        interview = ctx.shared.get("interview") or {}
    return interview if isinstance(interview, dict) else {}


def _posture(ctx) -> dict:
    posture = ctx.shared.get("posture")
    if not posture:
        try:
            posture = (ctx.read("posture") or {}).get("posture")
        except Exception:  # noqa: BLE001
            posture = {}
    return posture if isinstance(posture, dict) else {}


def _gates(ctx, posture: dict) -> dict:
    gates = ctx.shared.get("gates")
    if not gates:
        from puffpai import setup_ops
        gates = setup_ops.posture_gates(posture)
    return gates if isinstance(gates, dict) else {}


def _skip_reason(ctx, gates: dict) -> str:
    """Why there is no plan to make, in today's words, or ``""``."""
    if getattr(ctx.args, "skip_knowledge", False):
        return "skipped"
    if getattr(ctx.args, "skip_sourcing", False):
        return "owner"
    if not gates.get("may_offer_online_sourcing"):
        return "offline_build"
    return ""


def _machine(ctx) -> dict:
    """What Discover found: the Ollama address and the models it answered with."""
    machine = {}
    try:
        machine = ctx.read("machine") or {}
    except Exception:  # noqa: BLE001 - an undeclared read is the runner's business
        machine = {}
    out = dict(machine)
    for key in ("ollama_url", "installed_models"):
        if ctx.shared.get(key):
            out[key] = ctx.shared[key]
    return out


def _suggest_client(ctx):
    """The client Sources asks, or ``None`` - and ``None`` opens no socket.

    Order: a client handed in (tests, and anything that wants to inject one),
    then this build's own local model. The model is only asked when Discover
    actually saw an Ollama answer with models installed, so a build that never
    surveyed one never calls out to find out.
    """
    if getattr(ctx.args, "no_suggestions", False):
        return None
    injected = ctx.shared.get("source_suggest_client")
    if injected is not None:
        return injected
    machine = _machine(ctx)
    installed = [str(m) for m in (machine.get("installed_models") or [])]
    url = str(machine.get("ollama_url") or "")
    if not url or not installed:
        return None
    wanted = [str(ctx.shared.get("chosen_model") or ""),
              str(getattr(getattr(ctx.cfg, "ollama", None), "model", "") or "")]
    model = next((m for m in wanted if m and m in installed), installed[0])
    return sources_suggest.default_client(url, model)


def _suggestions(ctx, goal: str, online: bool) -> tuple[list[dict], str]:
    """Unvetted suggestions for *goal*, asking the model at most once.

    Propose stays side-effect free and stays quiet: no call offline, none
    without a goal, and none at all when this same goal was already answered -
    the answer is cached in the applied ``sources`` section, which is the only
    place it is ever written.
    """
    goal = str(goal or "").strip()
    cached = ctx.applied("suggested", default=None)
    if (goal and isinstance(cached, list)
            and str(ctx.applied("suggested_for", default="") or "") == goal):
        return [row for row in cached if isinstance(row, dict)], ""
    detail = sources_suggest.suggest_detail(
        goal, online=online, client=_suggest_client(ctx))
    return detail["rows"], detail["note"]


def propose(ctx) -> dict:
    """Search the registry and draw the plan. Read-only.

    Every figure comes from the shipped registry, so this works with the WAN
    unplugged - which matters, because the build that most needs an honest plan
    is the one that cannot go and look.
    """
    from puffpai import cli

    cli.enter_step("Zone 2, the Brain")
    interview = _interview(ctx)
    posture = _posture(ctx)
    gates = _gates(ctx, posture)
    goal = str(interview.get("goal") or "")
    packs = [str(i) for i in (interview.get("selected") or [])
             if str(i).startswith("pack:")]

    skipped = _skip_reason(ctx, gates)
    if skipped:
        return {"card": _skipped_card(skipped), "plan": _empty_plan(goal, skipped),
                "items": [], "selected": [], "suggested": [], "tool_needs": []}

    plan = sources_plan.plan_rows(goal, packs)
    items = plan["items"]
    fetchable = [item for item in items if item["selectable"]]
    blocked = [item for item in items if not item["selectable"]]
    recommended = sources_plan.default_selection(items)
    # The propose rule (CONVERTING.md): what was applied is what the card shows
    # as chosen; the registry's default selection stays the recommendation.
    # Otherwise a re-drawn card shows the first proposal again, and an owner who
    # had unticked a 55 GB pack is shown it ticked.
    applied = ctx.applied("selected", default=None)
    known = {item["id"] for item in fetchable}
    selected = ([str(s) for s in applied if str(s) in known]
                if isinstance(applied, list) else recommended)
    online = str(posture.get("build_network") or "") == "online"
    suggested, suggest_note = _suggestions(ctx, goal, online)
    needs = sources_plan.tool_needs(goal, items, selected)

    total, all_known = sources_plan.total_bytes(items, selected)
    fields: list[dict[str, Any]] = [
        {"id": "plan_note", "kind": "info", "label": "What this step does",
         "current": PLAN_NOTE + " Choosing a source records what you want; the "
                    "next step decides what is fetched now."},
    ]
    if fetchable:
        fields.append({
            "id": "selected", "kind": "multi_choice",
            "label": "What it learns from",
            "options": [{"value": item["id"],
                         "label": sources_plan.option_label(item)}
                        for item in fetchable[:40]],
            "current": [i for i in selected
                        if i in {item["id"] for item in fetchable[:40]}],
            "proposed": [i for i in recommended
                         if i in {item["id"] for item in fetchable[:40]}],
            "help": "Sizes and licenses are the registry's own. "
                    + PLAN_NOTE,
        })
    else:
        fields.append({
            "id": "nothing_matched", "kind": "info", "label": "Nothing matched",
            "current": str(plan.get("message") or
                           "No vetted source on our list covers that yet."),
        })

    for number, item in enumerate(blocked[:6], 1):
        fields.append({
            "id": f"needs_a_key_{number}", "kind": "info",
            "label": f"{item['label']} - {sources_plan.NO_KEY_PATH}",
            "current": f"{item['why_not']}. It is listed so you know it "
                       f"exists: {item['what_it_is'] or item['source_url']}",
        })
    # The suggestions block exists only when there is something in it; a build
    # that asked and got nothing gets one quiet line instead, never an error.
    for number, item in enumerate(suggested[:sources_suggest.MAX_SUGGESTIONS], 1):
        fields.append({
            "id": f"suggested_{number}", "kind": "info",
            "label": f"{item['label']} - {sources_suggest.UNVETTED_NOTE}",
            "current": f"{item['host']} - {item['why']} Nothing is fetched from "
                       f"here: add it yourself if you want it.",
        })
    if suggest_note and not suggested:
        fields.append({
            "id": "suggestions_note", "kind": "info",
            "label": "Sources beyond our list", "current": suggest_note,
        })
    if needs:
        fields.append({
            "id": "tool_needs", "kind": "info",
            "label": "What a bot like this will need to reach for",
            "current": ", ".join(need["id"] for need in needs[:12]),
            "help": "The next-but-one step says which of these this build "
                    "actually has.",
        })
    for pack in plan.get("unmapped_packs") or ():
        fields.append({
            "id": f"unmapped_{len(fields)}", "kind": "info",
            "label": f"{pack} has no vetted download",
            "current": "Put the pack in the knowledge folder yourself and it "
                       "is indexed from there.",
        })

    card = {
        "skill": "sources", "section": "sources",
        "title": "What it learns from",
        "decided": _decided(items, selected, total, all_known),
        "status": "gap" if blocked else "ready",
        "consequential": total > sources_plan.HEAVY_BYTES,
        "consequential_reasons": (["heavy_bytes"]
                                  if total > sources_plan.HEAVY_BYTES else []),
        "estimate": {"bytes": total, "seconds": 0,
                     "basis": "calibrated" if all_known else "unknown"},
        "fields": fields,
        "gaps": [_keyed_gap(item) for item in blocked[:6]],
    }
    return {"card": card, "plan": plan, "items": items, "selected": selected,
            "suggested": suggested, "tool_needs": needs, "blocked": blocked}


def _decided(items: list[dict], selected: list[str], total: int,
             all_known: bool) -> str:
    if not items:
        return "Nothing vetted matched this goal. " + PLAN_NOTE
    size = (sources_plan.size_label(total / sources_plan.MB) if all_known
            else "size not known for all of them")
    return (f"{len(items)} vetted source(s) found, {len(selected)} in the plan "
            f"({size}). " + PLAN_NOTE)


def _keyed_gap(item: dict) -> dict:
    return {
        "skill": "sources",
        "what": f"{item['label']} needs a key or a subscription, so it is "
                f"listed and not selected.",
        "why": f"{sources_plan.NO_KEY_PATH}: {item['why_not']}.",
        "impact": "Your bot will not have anything from that source, and will "
                  "say so rather than guess.",
        "fix": {"kind": "command", "id": "setup.rerun", "args": {}},
    }


def _empty_plan(goal: str, skipped: str) -> dict:
    return {"goal": goal, "message": None, "note": None, "items": [],
            "offered": [], "unmapped_packs": [], "skipped": skipped}


def _skipped_card(skipped: str) -> dict:
    why = {
        "owner": "You skipped this. Nothing was searched and nothing is "
                 "planned.",
        "skipped": "Knowledge setup was skipped, so there is no plan to make.",
        "offline_build": "You chose an offline build, so nothing is fetched "
                         "while building. Assemble the packs on a connected "
                         "machine and bring them over on removable media.",
    }[skipped]
    return {
        "skill": "sources", "section": "sources",
        "title": "What it learns from",
        "decided": why,
        "status": "ready",
        "consequential": False,
        "estimate": {"bytes": 0, "seconds": 0, "basis": "calibrated"},
        "fields": [{"id": "skipped", "kind": "info", "label": "No plan",
                    "current": why + " " + PLAN_NOTE}],
    }


def apply(ctx, answers: dict) -> dict:
    """Write the plan. Moves nothing, downloads nothing, asks for no key.

    The selection is the owner's, in this order: what they changed on the card,
    then ``--fetch-source`` from the command line, then the proposal. A
    ``--yes`` run with neither selects **nothing** - which is what today's step
    does, and for the same reason: consent nobody gave is not consent.
    """
    proposal = ctx.proposal or {}
    items = list(proposal.get("items") or [])
    plan = dict(proposal.get("plan") or _empty_plan("", ""))
    skipped = str(plan.get("skipped") or "")
    fetchable = {item["id"] for item in items if item["selectable"]}

    named = [str(s) for s in (getattr(ctx.args, "fetch_source", None) or [])]
    applied = ctx.applied("selected", default=None)
    if "selected" in answers:
        selected = [str(s) for s in (answers.get("selected") or [])]
        chosen_by = "owner"
    elif named:
        selected = named
        chosen_by = "command_line"
    elif isinstance(applied, list) and applied:
        # Already settled on an earlier pass. Re-applying this card without an
        # answer keeps the owner's plan rather than re-deciding it.
        selected = [str(s) for s in applied]
        chosen_by = str(ctx.applied("selected_by", default="owner") or "owner")
    elif ctx.non_interactive:
        # Nobody read the card, so nobody accepted the sizes on it.
        selected, chosen_by = [], "non_interactive"
    else:
        selected = list(proposal.get("selected") or [])
        chosen_by = "proposal"

    refused = [source_id for source_id in selected
               if source_id in {item["id"] for item in items}
               and source_id not in fetchable]
    selected = [source_id for source_id in selected if source_id not in refused]

    total, all_known = sources_plan.total_bytes(items, selected)
    blocked = [item for item in items if not item["selectable"]]
    data = {
        "plan": {
            "goal": plan.get("goal", ""),
            "skipped": skipped or None,
            "items": items,
            "message": plan.get("message"),
            "unmapped_packs": plan.get("unmapped_packs") or [],
        },
        "offered": list(plan.get("offered") or []),
        "selected": selected,
        "selected_by": chosen_by,
        "unavailable": [{"id": item["id"], "label": item["label"],
                         "status": sources_plan.NO_KEY_PATH,
                         "why": item["why_not"]} for item in blocked],
        "suggested": list(proposal.get("suggested") or []),
        # The cache key, and the whole cache: the goal these suggestions were
        # for. A re-drawn card for the same goal asks the model nothing.
        "suggested_for": str(plan.get("goal") or "").strip(),
        "tool_needs": list(proposal.get("tool_needs") or []),
        "bytes": total,
        "bytes_known": all_known,
    }
    result: dict[str, Any] = {
        "section": "sources", "data": data,
        "shared": {"sources_plan": data},
    }
    gaps = [_keyed_gap(item) for item in blocked]
    if gaps:
        result["gaps"] = gaps
    return result


def verify(ctx) -> dict:
    """Observe the registry on disk: does every planned id still resolve?

    Spec C.4's verify, and it reads the shipped file rather than the proposal:
    a plan naming a row that is not in this build's registry cannot be carried
    out, and saying so here is cheaper than a failed download later.
    """
    from puffpai import source_registry

    section = ctx.own()
    selected = [str(s) for s in (section.get("selected") or [])]
    if not selected:
        return {"status": "pass",
                "evidence": "nothing is planned, so there is nothing to resolve"}

    try:
        registry = source_registry.load_registry()
    except Exception as exc:  # noqa: BLE001 - unreachable is a gap, not a crash
        registry = None
        missing, unlicensed = list(selected), []
        note = f"the source registry could not be read ({exc})"
    else:
        missing, unlicensed = [], []
        note = ""
        for source_id in selected:
            entry = source_registry.get_source(source_id, registry)
            if entry is None:
                missing.append(source_id)
            elif not entry.get("license"):
                unlicensed.append(source_id)

    if missing or unlicensed:
        bad = (missing + unlicensed)[0]
        return {
            "status": "gap",
            "evidence": note or (f"{len(missing)} planned source(s) are not in "
                                 f"this build's registry"),
            "gap": {
                "skill": "sources",
                "what": "A source in the plan is not in this build's vetted "
                        "registry.",
                "why": note or (f"{bad} does not resolve to a licensed row in "
                                f"config/source_registry.json."),
                "impact": "Nothing will be downloaded for it, and your bot will "
                          "not know that subject.",
                "fix": {"kind": "command", "id": "setup.rerun", "args": {}},
            },
        }
    return {"status": "pass",
            "evidence": f"{len(selected)} planned source(s) resolve in the "
                        f"registry, each with a license"}
