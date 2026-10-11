"""Surfaces: where the owner meets the bot.

The allowlist is not a cosmetic filter: a surface that is
not on it is **not loaded**, so this card is the shape of the build rather than
a view preference.

* :func:`propose` derives the allowlist the goal implies and the flags amend.
  Read-only: :func:`build_interview.derive_surfaces` and
  :func:`build_interview.apply_surface_choices` are both pure, nothing is asked
  and nothing is written.
* the card offers every surface this build knows how to load, with the owner's
  words as labels and the reason each proposed one is proposed.
* :func:`apply` writes the ``surfaces`` section -- the derivation object,
  through the same two functions.
* :func:`verify` observes the taxonomy: every id on the list is a surface this
  build actually knows how to load.

**The persona's own surface is not one of the choices.** The persona always
gets a rail entry, a home card and search, and no gate may remove it. It
is shown on the card as an included fact, and it survives whatever is unchecked
below it. Taking it off is removing the persona (``puffpai persona remove``),
which takes the surface with it -- not unticking a box here.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

#: How many reason lines the card carries. A card is one screen
#: (``cards.MAX_FIELDS``), and the allowlist itself is the answer.
MAX_SUMMARY_LINES = 20


def _interview(ctx) -> dict:
    """What Purpose recorded. The section first, the run's notebook second."""
    interview = ctx.read("persona").get("interview")
    if not isinstance(interview, dict):
        interview = ctx.shared.get("interview")
    return interview if isinstance(interview, dict) else {}


def _persona_surfaces(interview: dict) -> list[str]:
    """The scaffolded persona's own surface, if this build made one."""
    persona = interview.get("persona")
    if not isinstance(persona, dict):
        return []
    return [str(s) for s in (persona.get("surfaces") or [])]


def _options(derivation: dict, chosen: list[str], always: list[str]) -> list[dict]:
    """Every surface this build can load, in the owner's words.

    The persona's surface is left out: it is not a choice, and an
    option the answer cannot change is a box that lies about what unticking it
    does.
    """
    from puffpai import build_interview as bi

    taxonomy = bi.surface_taxonomy()
    available = [str(s) for s in (derivation.get("available") or [])]
    for sid in chosen:
        if sid not in available:
            available.append(sid)
    out: list[dict] = []
    for sid in available:
        if sid in always:
            continue
        label = str((taxonomy.get(sid) or {}).get("label") or sid)
        if taxonomy.get(sid, {}).get("sensitive"):
            label = f"{label} (sensitive)"
        out.append({"value": sid, "label": label})
    return out[:60]


def propose(ctx) -> dict:
    """Derive the allowlist the goal implies. Read-only."""
    from puffpai import build_interview as bi, cli, setup_ops

    cli.enter_step("Surfaces")
    interview = _interview(ctx)
    proposal = interview.get("proposal")

    if not proposal:
        derivation = setup_ops.derive_surfaces({"goal": "", "flat": []})
    else:
        derivation = cli._with_persona_surfaces(
            setup_ops.derive_surfaces(proposal), interview.get("persona"))

    add = [str(s) for s in (getattr(ctx.args, "add_surface", None) or [])]
    drop = [str(s) for s in (getattr(ctx.args, "drop_surface", None) or [])]
    if add or drop:
        derivation = setup_ops.apply_surface_choices(derivation, add=add,
                                                     remove=drop)
    # The propose rule (CONVERTING.md): an allowlist the owner already settled
    # is what the card shows; the goal's derivation stays the recommendation.
    proposed = [str(s) for s in (derivation.get("surfaces") or [])]
    applied = ctx.applied("surfaces", default=None)
    if isinstance(applied, dict) and applied.get("surfaces") is not None:
        derivation = applied

    always = [s for s in _persona_surfaces(interview)
              if s in (derivation.get("surfaces") or [])]
    chosen = [s for s in (derivation.get("surfaces") or []) if s not in always]

    fields: list[dict] = []
    options = _options(derivation, chosen, always)
    if options:
        fields.append({
            "id": "allowlist", "kind": "multi_choice",
            "label": "What your bot will be allowed to touch",
            "options": options, "current": chosen,
            "proposed": [s for s in proposed
                         if s in {opt["value"] for opt in options}],
            "help": "Anything not on this list is not loaded -- not hidden, "
                    "not loaded. An empty list is a valid build."})
    for sid in always:
        fields.append({
            "id": f"persona_{len(fields)}", "kind": "info",
            "label": "Always included",
            "current": f"{sid} - your own bot's page. It is on the list "
                       f"because your bot is, and it comes off when the "
                       f"persona does."})
    # ``surfaces_summary_lines`` already carries the derivation's message, so
    # the card says it once. These are the reason lines -- every proposed
    # surface has to cite the words in the goal that put it there.
    for line in bi.surfaces_summary_lines(derivation)[:MAX_SUMMARY_LINES]:
        fields.append({"id": f"why_{len(fields)}", "kind": "info",
                       "label": "Why", "current": str(line)})

    card = {
        "skill": "surfaces", "section": "surfaces",
        "title": "Where you meet it",
        "decided": _decided(derivation),
        "status": "ready",
        "consequential": False,
        "estimate": {"bytes": 0, "seconds": 0, "basis": "measured"},
        "fields": fields,
    }
    return {"card": card, "derivation": derivation, "always": always}


def _decided(derivation: dict) -> str:
    surfaces = list(derivation.get("surfaces") or [])
    if not surfaces:
        return "nothing is on the list yet, so your bot loads only what you add"
    sensitive = list(derivation.get("sensitive") or [])
    line = f"{len(surfaces)} surfaces: {', '.join(surfaces)}"
    if sensitive:
        line += f" ({len(sensitive)} sensitive)"
    return line


def apply(ctx, answers: dict) -> dict:
    """Write the ``surfaces`` section: the derivation today's step returns.

    The owner's ticks become the same ``add``/``remove`` lists the
    ``--add-surface`` / ``--drop-surface`` flags produce, and go through the
    same :func:`setup_ops.apply_surface_choices`, so a build made by clicking
    and a build made by flags record the same object.
    """
    from puffpai import setup_ops

    proposal = ctx.proposal or {}
    derivation = proposal.get("derivation")
    if not isinstance(derivation, dict):
        derivation = setup_ops.derive_surfaces({"goal": "", "flat": []})
    always = [str(s) for s in (proposal.get("always") or [])]

    chosen = answers.get("allowlist")
    if chosen is not None:
        keep = set(str(s) for s in chosen) | set(always)
        proposed = [str(s) for s in (derivation.get("surfaces") or [])]
        add = [s for s in keep if s not in proposed]
        remove = [s for s in proposed if s not in keep]
        if add or remove:
            derivation = setup_ops.apply_surface_choices(
                derivation, add=sorted(add), remove=sorted(remove))

    return {"section": "surfaces", "data": {"surfaces": derivation},
            "shared": {"surfaces": derivation}}


def verify(ctx) -> dict:
    """Observe the taxonomy: is every id on the list one this build loads?"""
    from puffpai import build_interview as bi

    derivation = ctx.own().get("surfaces") or {}
    chosen = [str(s) for s in (derivation.get("surfaces") or [])]
    taxonomy = bi.surface_taxonomy()
    unknown = [sid for sid in chosen if sid not in taxonomy]
    if unknown:
        return {"status": "gap",
                "evidence": f"{', '.join(unknown)} is not a surface this build "
                            f"knows how to load",
                "gap": {"skill": "surfaces",
                        "what": "Your build allows a page it does not have.",
                        "why": f"{', '.join(unknown)} is on the allowlist and "
                               f"is not in this build's surface taxonomy.",
                        "impact": "Nothing loads for it, so the allowlist "
                                  "promises something the build cannot show.",
                        "fix": {"kind": "command", "id": "setup.rerun",
                                "args": {}}}}
    return {"status": "pass",
            "evidence": f"all {len(chosen)} allowed surfaces are in this "
                        f"build's taxonomy"}
