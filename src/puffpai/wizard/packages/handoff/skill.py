"""Handoff: how to start it, and everything that is missing.

Nothing for the owner to decide: every field is read-only. The card says
how to start **this** install -- the installer's launchers if there is a
pointer, the manual recipe if there is not -- the dashboard address that
install's own port makes, and then every gap any skill recorded, each with the
one command that fixes it, rendered by ``fixes.safe_gaps`` from a registered id.

**The pairing code is not on the card, and this skill does not print it.** It
cannot be on the card: a card is stored in the run record and returned over
HTTP, and the code is the one thing that gets a stranger's phone into the
dashboard. So ``apply`` hands the block to the run's **show-once face**
(``ctx.show_once``) -- the console, when the console is drawing this wizard,
and nobody at all over HTTP, where the dashboard has its own pairing flow. The
lines come from ``cli.pairing_block_lines()``; nothing here prints and nothing
here records the code.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations


def _start_lines(ctx) -> tuple[list[str], str, bool]:
    """How to start this install, its dashboard address, and whether it has one.

    The same two branches ``cli._print_closing`` has, for the same reason: an
    installed build starts with the installer's launchers and nothing else, and
    printing the manual recipe at it tells an owner their finished install is
    unfinished.
    """
    from puffpai import cli

    launchers = cli.installed_launchers()
    if launchers:
        lines = [launchers["backend"]]
        if launchers["dashboard"]:
            lines.append(launchers["dashboard"])
        return lines, str(launchers.get("dashboard_url") or ""), bool(
            launchers["dashboard"])

    lines = ["puffpai serve", cli.dashboard_start_command()]
    return lines, cli._dashboard_url(ctx.cfg), True


def propose(ctx) -> dict:
    """Read the install and the run's gaps. Nothing is asked, nothing written."""
    from puffpai import cli

    cli.enter_step("The closing screen")
    commands, url, has_dashboard = _start_lines(ctx)
    gaps = [gap for gap in (ctx.own().get("gaps") or ()) if isinstance(gap, dict)]

    verdict, detail = _verdict(ctx)
    fields = [{"id": "verdict", "kind": "info",
               "label": "Where this build got to",
               "current": f"{verdict} {detail}".strip()}]
    fields += [{"id": f"start_{index}", "kind": "info",
               "label": "Then run" if index else "Start your bot",
               "current": command}
              for index, command in enumerate(commands)]
    if url:
        fields.append({"id": "dashboard", "kind": "info",
                       "label": "Then open", "current": url})
    elif has_dashboard:
        fields.append({"id": "dashboard", "kind": "info",
                       "label": "Then open",
                       "current": "the address the dashboard prints"})
    if has_dashboard:
        # The code itself goes to the show-once face and is stored nowhere.
        # This line is the part that is still true in an hour, when the code
        # has expired.
        fields.append({"id": "pairing", "kind": "info",
                       "label": "Let your first device in",
                       "current": f"the code is shown on this terminal once; "
                                  f"print a fresh one with "
                                  f"{cli.pair_code_command()}"})

    proof = _proof_line(ctx)
    if proof:
        fields.append({"id": "proof", "kind": "info",
                       "label": "Proof it works", "current": proof})

    card = {
        "skill": "handoff", "section": "handoff",
        "title": "How to start it",
        "decided": _decided(commands, gaps),
        "status": "gap" if gaps else "ready",
        "consequential": False,
        "estimate": {"bytes": 0, "seconds": 0, "basis": "measured"},
        "fields": fields,
        "gaps": gaps,
    }
    return {"card": card, "commands": commands, "dashboard_url": url,
            "has_dashboard": has_dashboard, "gaps": gaps}


def _verdict(ctx) -> tuple[str, str]:
    """Did this build finish, and if not, what has to happen first.

    The same four sentences ``cli._print_closing`` has always printed, for the
    same two independent reasons: a vault the owner asked for and did not get
    is as unfinished as a missing brain, and "Setup complete" over either of
    them is the wizard lying on its last screen.
    """
    brain = ctx.shared.get("brain_ready") or {}
    vault = ctx.shared.get("vault_block") or {}
    brain_ready = bool(brain.get("ready"))
    vault_rejected = bool(vault.get("rejected"))
    if brain_ready and not vault_rejected:
        return ("Setup complete. Your bot is ready.", "")
    if not brain_ready and vault_rejected:
        head = ("Setup finished - NOT READY: the brain is missing and the "
                "vault was not created.")
    elif vault_rejected:
        head = "Setup finished - NOT READY: the vault was not created."
    else:
        head = "Setup finished - NOT READY: the brain is missing."

    detail = "" if brain_ready else str(brain.get("reason") or "")
    first = str(brain.get("remedy") or "") or str(brain.get("pull_command") or "")
    if first and not brain_ready:
        detail = (f"{detail} Do this first, because nothing else can answer "
                  f"until it is done: {first}").strip()
    return (head, detail)


def _proof_line(ctx) -> str:
    """What Prove found, in one line, and the command that proves the rest.

    Prove's gaps are already on this card -- the runner mirrors every gap into
    ``handoff.gaps``. What is not is the verdict itself and the fact that the
    static half is not the whole proof, which is exactly the honesty the
    closing screen carries and must not lose by becoming a card.
    """
    from puffpai import prove

    record = ctx.shared.get("prove_record")
    if not isinstance(record, dict) or not record.get("checks"):
        return ""
    checks = [c for c in record["checks"] if isinstance(c, dict)]
    failed = [c for c in checks if c.get("result") == prove.FAIL]
    if failed:
        head = ("This build did not check out: "
                + "; ".join(str(c.get("reason") or "") for c in failed[:3]))
    else:
        head = f"{len(checks)} checks ran and none failed."
    return f"{head} {prove.RUN_LINE}"


def _decided(commands: list[str], gaps: list) -> str:
    first = commands[0] if commands else "puffpai serve"
    if not gaps:
        return f"Start it with {first}; nothing is missing."
    what = "thing is" if len(gaps) == 1 else "things are"
    return f"Start it with {first}; {len(gaps)} {what} still missing."


def apply(ctx, answers: dict) -> dict:
    """Record the closing screen, and show the pairing code once.

    The code goes to the run's show-once face and is stored nowhere -- not in
    the card, not in the section, not in the run log. A run with no face (every
    HTTP request) shows no code: the dashboard has its own pairing flow, and a
    pairing code in an API response is a pairing code in somebody's proxy log.
    """
    from puffpai import cli

    proposal = ctx.proposal or {}
    commands = proposal.get("commands")
    if commands is None:
        commands, url, has_dashboard = _start_lines(ctx)
    else:
        url = proposal.get("dashboard_url") or ""
        has_dashboard = bool(proposal.get("has_dashboard"))

    sink = ctx.face()
    shown_code = False
    if has_dashboard and sink is not None:
        sink.show(cli.pairing_block_lines())
        shown_code = True

    return {"section": "handoff",
            "data": {"closing_shown": True,
                     "pairing_shown": shown_code,
                     "commands": list(commands),
                     "dashboard_url": url,
                     "prove_shown": bool(
                         (ctx.shared.get("prove_record") or {}).get("checks"))},
            "note": "closing screen rendered as a card"}


def verify(ctx) -> dict:
    """Does every command this card names resolve on this install?

    The check is Prove's own (``prove._check_handoff``), reused rather than
    re-implemented: the failure it was written for -- a closing screen printing
    commands that do not exist on the machine -- is exactly the one this
    verify is for, and two copies of it would be two answers.
    """
    from puffpai import cli, prove

    try:
        check = prove._check_handoff(cli.installed_launchers())
    except Exception as exc:  # noqa: BLE001 - unreachable is a gap
        return {"status": "gap",
                "evidence": f"the start-up lines could not be checked ({exc})",
                "gap": _gap("The start-up lines on this screen were not "
                            "checked.", str(exc))}
    if check.get("result") == prove.PASS:
        return {"status": "pass", "evidence": str(check.get("reason") or "")}
    return {"status": "gap", "evidence": str(check.get("reason") or ""),
            "gap": _gap("Something the closing screen names is not on this "
                        "machine.", str(check.get("reason") or ""))}


def _gap(what: str, why: str) -> dict:
    return {"skill": "handoff", "what": what, "why": why,
            "impact": "Following the closing screen will not start your bot.",
            "fix": {"kind": "command", "id": "setup.rerun", "args": {}}}
