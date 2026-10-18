"""The ``puffpai setup`` wizard - HomeBotBuild's out-of-box experience.

BuildBot's wizard, fitted to this build. The differences are the point, so they
are named here rather than left to be discovered:

* **The persona is preselected.** This wizard belongs to HomeBot, so the bot's
  name, the pack and the goal arrive filled in (see
  :data:`puffpai.setup_ops.HOMEBOT_DEFAULTS`) and the owner edits them.
* **Home Assistant is optional.** One yes/no question. A "no" is recorded as a
  stated absence (``integrations.home_assistant.present: false``) and never
  mentioned again - a household without Home Assistant has a complete build.
* **Storage is asked, not assumed.** Developers bring their own media, packs and
  disks, so the four directories are a step with validation behind it.
* **Who lives here is a step.** This build ships household profiles, so the
  wizard creates the owner's profile rather than leaving a placeholder named
  "Owner" for somebody to find later.
* **The network edge is asked about, and never touched.** The Bridge step asks
  what is in front of this box today: nothing, this build's own Zone 3, or gear
  the owner already runs. Zone 3 is an OPT-IN that the wizard records and writes
  the commands for; it starts no container, binds no port and changes no DNS.
  "Nothing" is a complete answer and leaves the Bridge fully configured.
* **The enumerated egress table is the real door registry**
  (:mod:`puffpai.security.egress_doors`), not a parallel list. A switch id IS a
  door id; see :func:`puffpai.setup_ops.persist_posture`.

Everything that writes lives in :mod:`puffpai.setup_ops`, which the ``/setup/*``
endpoints and the dashboard onboarding call as well, so the terminal wizard and
the graphical one cannot drift apart. This module prompts and prints; it decides
nothing on its own.

All strings here are ASCII: they reach a Windows console.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional


GOAL_MAX_CHARS = 1000

ISSUES_URL = "https://github.com/CypherSisters/HomeBotBuild/issues"


# The wizard's steps, in the order it walks them, named once so the screen and
# the docs cannot drift apart. Every step calls enter_step() first, so a failure
# can say WHERE it stopped instead of handing somebody a traceback.
#
# Three names here are the CARD wizard's (``puffpai.wizard.runner.SCHEDULE``):
# "The brain", "Build interview" and "Integrations". The card packages and the
# linear steps below name some of the same ground differently - a card's "The
# brain" is this file's "Zone 2, the Brain" - and both names are kept rather
# than one path being renamed to match the other, because ``enter_step`` RAISES
# on a name it does not know and a step nobody can enter is a wizard that stops
# on a stranger's machine. A run walks one path or the other, never both.
SETUP_STEPS: tuple[str, ...] = (
    "Posture",
    "Hardware census",
    "The Ollama address",
    "The brain",
    "Zone 2, the Brain",
    "Crew sizing",
    "Who lives here",
    "What this build is for",
    "Build interview",
    "Surfaces",
    "Storage",
    "Zone 1, the Vault",
    # Home Assistant comes BEFORE the Bridge on purpose: whether this household
    # runs one changes what the Bridge suggests, and a wizard that asked in the
    # other order would be guessing.
    "Home Assistant",
    "Integrations",
    "Zone 3, the Bridge",
    "Brain ready?",
    "The build manifest",
    "The closing screen",
)

#: The step the wizard is currently inside. Set by :func:`enter_step`.
_current_step: str = ""

NO_STEP = "(before the first step)"


def current_step_label() -> str:
    """``Step N of <total> - <name>`` for the step now running, or "" before one."""
    return _current_step


def enter_step(name: str) -> str:
    """Record that the wizard is now INSIDE *name*. Returns the label.

    Raises if *name* is not in :data:`SETUP_STEPS`, so a renamed step is a test
    failure here rather than a wrong number on a stranger's screen.
    """
    global _current_step
    number = SETUP_STEPS.index(name) + 1
    _current_step = f"Step {number} of {len(SETUP_STEPS)} - {name.rstrip('.?')}"
    return _current_step


def step_banner(name: str, tail: str = "") -> str:
    """``Step N of <total> - <name>.`` plus an optional first sentence of prose."""
    number = SETUP_STEPS.index(name) + 1
    head = f"Step {number} of {len(SETUP_STEPS)} - {name}"
    enter_step(name)
    if not head.endswith(("?", ".", "!")):
        head += "."
    return f"{head} {tail}".rstrip() if tail else head


class SetupRefused(Exception):
    """A step refused to continue, on purpose, with something to tell the owner.

    Distinct from a bug: the wizard knows exactly what is wrong and what to do
    about it. :func:`cmd_setup` renders it as a plain screen and exits 1. A bug
    is anything else, and gets the crash screen and exit 2.
    """

    def __init__(self, headline: str, message: str,
                 remedy: Optional[list[str]] = None):
        super().__init__(headline)
        self.headline = headline
        self.message = message
        self.remedy = list(remedy or [])


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str = "", *, non_interactive: bool = False) -> str:
    """Prompt the user, returning ``default`` in non-interactive mode."""
    if non_interactive:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return answer or default


def _ask_yes_no(prompt: str, default: bool = False, *,
                non_interactive: bool = False) -> bool:
    """Yes/no prompt. Returns ``default`` in non-interactive mode."""
    if non_interactive:
        return default
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _ask_paragraph(prompt: str, default: str = "", *, limit: int = GOAL_MAX_CHARS,
                   non_interactive: bool = False) -> str:
    """Read a multi-line answer, ended by a blank line. Blank first line keeps
    the default, which for this build is HomeBot's household goal."""
    if non_interactive:
        return default
    print(prompt)
    print("  Blank line when you are done, or right away to keep this:")
    print(f"    {default}")
    lines: list[str] = []
    used = 0
    dropped = False
    while True:
        try:
            line = input("  > ")
        except (EOFError, KeyboardInterrupt):
            break
        chunk = line.strip()
        if not chunk:
            break
        if used <= limit:
            lines.append(chunk)
            used += len(chunk) + 1
        else:
            dropped = True
    answer = " ".join(lines).strip()
    if len(answer) > limit:
        answer = answer[:limit].rstrip()
        dropped = True
    if dropped:
        print(f"  That was longer than {limit} characters; the build saw the "
              f"first {limit}.")
    return answer or default


#: Test-only switch: read secrets (the vault PIN, the Home Assistant token) from
#: STDIN instead of from the console, so the interactive wizard can be driven by
#: a script. Set ``PUFFPAI_SETUP_STDIN_SECRETS=1``. It is documented in
#: ``docs/WIZARD.md`` and it exists for ``tests/test_wizard_interactive.py``: on
#: Windows ``getpass`` reads the console device directly and ignores a pipe, so
#: without this the interactive path could never be exercised without a human.
#: Nothing is echoed either way - a piped ``input()`` prints nothing back.
STDIN_SECRETS_ENV = "PUFFPAI_SETUP_STDIN_SECRETS"


def _secrets_from_stdin() -> bool:
    """True when a hidden prompt should read the pipe instead of the console."""
    if os.environ.get(STDIN_SECRETS_ENV) == "1":
        return True
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):
        return True


def _read_pin(prompt: str) -> str:
    """Read a PIN without echoing it, falling back to input() with a warning.

    What comes back is stripped. A PIN is sealed with whatever was typed, so a
    trailing space picked up from a pasted line or a scripted answer file seals
    a vault that the same person, typing the same PIN, cannot then open.
    """
    import getpass
    if _secrets_from_stdin():
        # No console to hide the typing in (a pipe, a CI job, a test). input()
        # does not echo what it reads from a pipe, so nothing is disclosed; the
        # warning is there for the person who really is at a dumb terminal.
        print("  (this terminal cannot hide what you type)")
        try:
            return input(f"{prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except Exception:  # noqa: BLE001 - no tty, no hidden input
        print("  (this terminal cannot hide what you type)")
        try:
            return input(f"{prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""


# ---------------------------------------------------------------------------
# failure screens
# ---------------------------------------------------------------------------

def _print_refusal(refusal: SetupRefused) -> None:
    """The screen for a deliberate refusal. No traceback, no jargon."""
    print()
    print("=" * 60)
    print(f"  Setup stopped - {refusal.headline}")
    print("=" * 60)
    if _current_step:
        print(f"Stopped at {_current_step}")
    print(refusal.message)
    if refusal.remedy:
        print()
        print("What to do:")
        for line in refusal.remedy:
            print(f"    {line}")
    print()
    print("Nothing was written that would leave this build in a broken state.")
    print("Fix the above and run `puffpai setup` again.")


def _crash_log_path(exc: BaseException) -> str:
    """Write the traceback to a file and return its path, or "" if we cannot."""
    import datetime
    import traceback
    try:
        from puffpai.config import get_config
        base = os.path.join(get_config().DATA_DIR, "logs")
    except Exception:  # noqa: BLE001
        base = os.path.join(os.getcwd(), "logs")
    try:
        os.makedirs(base, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(base, f"setup-crash-{stamp}.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"step: {_current_step or NO_STEP}\n\n")
            fh.write("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
        return path
    except Exception:  # noqa: BLE001
        return ""


def _print_crash(exc: BaseException, log_path: str) -> None:
    """The screen for an UNEXPECTED failure. One line, a place, a log path."""
    headline = (f"Setup stopped at {_current_step}" if _current_step
                else "Setup stopped before the first step")
    text = str(exc).strip()
    one_line = text.splitlines()[0] if text else exc.__class__.__name__
    print()
    print("=" * 60)
    print(f"  {headline}")
    print("=" * 60)
    print(f"{exc.__class__.__name__}: {one_line}")
    if log_path:
        print(f"Details are in {log_path}")
    else:
        print("The details could not be written to a log file.")
    print()
    print("This is a bug in HomeBot, not something you did wrong. Please open")
    print(f"an issue at {ISSUES_URL} with the")
    print("log above; `puffpai setup` can be re-run safely at any time.")


#: How many times a menu re-asks before it gives up and records "skipped".
#: Bounded on purpose: a typo must not dead-end the wizard, and a closed pipe
#: must not turn a prompt into an infinite loop.
PICK_ATTEMPTS = 5


def _pick_by_id(prompt: str, options: list[dict]) -> Optional[str]:
    """Pick one option by number or by id. Blank means the owner skipped it.

    An answer that is neither a listed number nor a listed id is a typo, and the
    menu says so and asks again rather than passing the typo downstream to be
    silently replaced by a default. After :data:`PICK_ATTEMPTS` tries it returns
    ``None`` - "skipped" - which every caller already handles by taking its own
    safe default. A wrong answer never ends the wizard.
    """
    ids = [str(row.get("id")) for row in options]
    for _attempt in range(PICK_ATTEMPTS):
        answer = _ask(prompt)
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return ids[int(answer) - 1]
        if answer in ids:
            return answer
        print(f"    Not one of these. Type a number 1-{len(options)}, or one of: "
              f"{', '.join(ids)}. Blank to skip.")
    return None


# ---------------------------------------------------------------------------
# the steps
# ---------------------------------------------------------------------------

def _step_posture(args, setup_ops, *, non_interactive: bool) -> dict:
    """The "Posture" step: the two posture axes, asked before anything else.

    The number on the screen is derived from :data:`SETUP_STEPS` (``enter_step``
    raises on a name that is not in it), so these docstrings name the step and
    never number it -- a hand-written number is a number that drifts, and every
    one of them had.
    """
    enter_step("Posture")
    print(step_banner("Posture", "Two questions, asked first because they decide"))
    print("what the rest of this wizard is allowed to offer you.")

    net = getattr(args, "build_network", None)
    egress = getattr(args, "run_egress", None)
    doors = [str(d) for d in (getattr(args, "egress_door", None) or [])]
    options = setup_ops.posture_options()

    if not non_interactive:
        if net is None:
            print()
            print("Can this machine reach the internet WHILE YOU BUILD?")
            for index, row in enumerate(options["build_network"], start=1):
                print(f"  {index}. {row['label']} - {row['description']}")
            net = _pick_by_id("Choose", options["build_network"])
        if egress is None:
            print()
            print("And when the bot is FINISHED and running, what may it reach?")
            for index, row in enumerate(options["run_egress"], start=1):
                print(f"  {index}. {row['label']} - {row['description']}")
                if row.get("note"):
                    print(f"     {row['note']}")
            egress = _pick_by_id("Choose", options["run_egress"])
        if egress == "enumerated" and not doors:
            print()
            print("Which Bridge switches may the finished bot use? Every one is shut until")
            print("you name it. Numbers or ids, comma separated, blank for none.")
            table = options["egress_switches"]
            for index, row in enumerate(table, start=1):
                print(f"  {index}. {row['id']} - {row['description']}")
            raw = _ask("Open")
            for token in [t.strip() for t in raw.split(",") if t.strip()]:
                if token.isdigit() and 1 <= int(token) <= len(table):
                    doors.append(str(table[int(token) - 1]["id"]))
                else:
                    doors.append(token)

    from puffpai import build_posture
    posture = setup_ops.assess_posture(net, egress, doors)
    for line in build_posture.posture_summary_lines(posture):
        print(f"  {line}")
    setup_ops.persist_posture(posture)
    return posture


def _step_hardware(args, setup_ops, *, non_interactive: bool) -> dict:
    """The "Hardware census" step: what this machine is, measured then stated."""
    enter_step("Hardware census")
    print()
    print(step_banner("Hardware census", "What this machine actually has."))
    census = setup_ops.hardware_census()
    from puffpai import hardware_census as hc
    for line in hc.census_summary_lines(census):
        print(f"  {line}")

    stated = getattr(args, "stated_ram", None)
    detected_ram = (census.get("memory") or {}).get("total_gb")
    if stated is None and not non_interactive and detected_ram is None:
        raw = _ask("How much RAM does this machine have, in GB? (blank to skip)")
        try:
            stated = float(raw) if raw else None
        except ValueError:
            stated = None
    if stated:
        census = hc.apply_stated(census, total_ram_gb=float(stated))
        print(f"  Using the RAM you stated: {stated} GB "
              f"(class: {census.get('hardware_class')})")
    return census


def _step_ollama_url(args, setup_ops, *, non_interactive: bool) -> tuple[str, Optional[list[str]]]:
    """The "The Ollama address" step: where Ollama is. Asked, never assumed."""
    enter_step("The Ollama address")
    from puffpai.config import get_config

    print()
    print(step_banner("The Ollama address", "Where the model runtime lives."))
    cfg = get_config()
    url = getattr(args, "ollama_url", None) or cfg.ollama.url
    if not non_interactive:
        url = _ask("Ollama base URL", url)
    models = setup_ops.list_models(url)
    if models is None:
        print(f"  Nothing answered at {url}. That is not fatal: the build is")
        print("  recorded either way and the bot says so out loud until a model")
        print("  is there.")
    else:
        print(f"  Ollama answered at {url} with {len(models)} model(s) installed.")
    return url, models


def _step_brain(args, setup_ops, census, ollama_url, installed, *,
                non_interactive: bool) -> tuple[str, dict]:
    """The "Zone 2, the Brain" step: the model this hardware can carry."""
    enter_step("Zone 2, the Brain")
    print()
    print(step_banner("Zone 2, the Brain", "The model this machine can carry."))
    menu = setup_ops.brain_menu(census, probe_ollama=False)
    options = list(menu.get("options") or [])
    if menu.get("message"):
        print(f"  {menu['message']}")
    for index, row in enumerate(options, start=1):
        here = " (already on this box)" if row.get("installed") else ""
        print(f"  {index}. {row.get('id')} - {row.get('strength') or row.get('label')}{here}")
    chosen = getattr(args, "model", None)
    if chosen is None and not non_interactive and options:
        chosen = _pick_by_id("Choose a model", options)
    if not chosen:
        # `recommended` is the whole catalog row, not an id.
        recommended = menu.get("recommended") or {}
        chosen = str(recommended.get("id")
                     or (options[0]["id"] if options else ""))
    print(f"  Brain: {chosen or 'none chosen'}")

    # The embedder is part of the Brain step because it is the other model this
    # build runs, and because a default that a base install cannot load shows
    # up as a knowledge library that answers nothing, with no error anywhere.
    # Written explicitly either way.
    options = setup_ops.embedding_options(installed)
    backend = options["default"]
    print(f"  Search-by-meaning: {backend} - {options['default_note']}")
    if options["upgrade"]:
        print(f"  Upgrade available: {options['upgrade']} "
              f"({options['upgrade_model']}) - {options['upgrade_note']}")
        print("    Different vectors from the default, so choose it now or "
              "re-index later; nothing is indexed yet on a fresh build.")
        if not non_interactive and _ask_yes_no(
                f"  Use {options['upgrade_model']} for search-by-meaning?",
                False):
            backend = options["upgrade"]
    setup_ops.set_embedding_config(backend, options["upgrade_model"])
    print(f"  Embedding backend written to config: {backend}")

    choice = {
        "model": chosen,
        "url": ollama_url,
        "installed": bool(installed and chosen in (installed or [])),
        "recommended": menu.get("recommended"),
        "embedding_backend": backend,
    }
    return chosen, choice


def _step_crew(setup_ops, census) -> dict:
    """The "Crew sizing" step: how many specialists this machine can carry."""
    enter_step("Crew sizing")
    from puffpai import crew_sizing
    print()
    print(step_banner("Crew sizing", "How big a crew this machine can carry."))
    budget = setup_ops.crew_budget(census)
    for line in crew_sizing.crew_summary_lines(budget):
        print(f"  {line}")
    return budget


def _step_household(args, setup_ops, *, non_interactive: bool) -> dict:
    """The "Who lives here" step: creates the owner's profile, not a placeholder."""
    enter_step("Who lives here")
    print()
    print(step_banner("Who lives here", "HomeBot is a household bot, so it asks."))
    state = setup_ops.household_status()
    if state.get("owner"):
        print(f"  This box already knows its owner: {state['owner']}.")
        if state.get("names"):
            print(f"  Household: {', '.join(state['names'])}")
        return state

    from puffpai import household

    owner = getattr(args, "owner_name", None)
    if owner is None:
        owner = _ask("Your name (the owner of this box)",
                     household.OWNER_PLACEHOLDER,
                     non_interactive=non_interactive)
    if owner == household.OWNER_PLACEHOLDER:
        # A placeholder is not a name. The profile is still created - the build
        # needs an owner row - but the fact that nobody said who they are is
        # recorded rather than left to be discovered on the dashboard months
        # later.
        setup_ops.record_pending_apply("household_owner_name", {
            "needs": "the owner names themselves on the Household page",
        })
    others = [{"name": str(n)} for n in (getattr(args, "member", None) or [])]
    if not non_interactive and not others:
        raw = _ask("Anyone else in the household? Names, comma separated "
                   "(blank to skip)")
        others = [{"name": t.strip()} for t in raw.split(",") if t.strip()]
    state = setup_ops.create_household(owner, others)
    print(f"  Owner profile: {state.get('owner') or 'not created'}")
    if state.get("count", 0) > 1:
        print(f"  Household: {', '.join(state.get('names') or [])}")
    print("  You can add, rename or remove people later on the Household page.")
    return state


def _step_interview(args, setup_ops, census, *, non_interactive: bool) -> tuple[dict, str]:
    """The "What this build is for" step. Pre-filled with the household goal."""
    enter_step("What this build is for")
    from puffpai import build_interview as bi

    defaults = setup_ops.homebot_defaults()
    print()
    print(step_banner("What this build is for",
                      "This is HomeBot, so the answer starts filled in."))
    goal = getattr(args, "goal", None)
    if goal is None:
        goal = _ask_paragraph("  Tell it what this household needs:",
                              default=str(defaults["goal"]),
                              non_interactive=non_interactive)
    goal = goal or str(defaults["goal"])
    proposal = setup_ops.build_interview(goal, census)
    for line in bi.proposal_summary_lines(proposal):
        print(f"  {line}")
    persona = _maybe_scaffold_persona(
        args, goal, proposal,
        bot_name=str(getattr(args, "bot_name", None) or defaults["bot_name"]),
        non_interactive=non_interactive)
    if persona:
        proposal = dict(proposal, persona=persona)
    return proposal, goal


def _maybe_scaffold_persona(args, goal: str, proposal: dict, *, bot_name: str,
                            non_interactive: bool) -> Optional[dict]:
    """Offer a blank persona for this goal, and write it if the owner says yes.

    Offered when the catalog matched NOTHING - the moment a wizard would
    otherwise say "these are the household defaults" and stop being useful
    about the thing the owner actually asked for - and on request with
    ``--scaffold`` or ``--lists``. A goal we ship nothing for is still a build:
    the owner gets an app with their words in it, their lists, and a page, and
    it is theirs to edit rather than ours to have guessed.

    Returns the record for the manifest's persona block, or None when nothing was
    made. Never raises: a refused id or an unwritable folder is a printed
    sentence and a wizard that carries on.
    """
    from puffpai import persona_scaffold

    lists = getattr(args, "lists", None)
    asked = bool(getattr(args, "scaffold", False)) or bool(lists)
    nothing_matched = bool(proposal.get("scaffold_offer"))
    if not (asked or nothing_matched):
        return None

    print()
    if not asked:
        if non_interactive:
            # Nobody is there to answer, and scaffolding an app the owner never
            # asked for is not a default anyone chose.
            print("  Nothing we ship matched that, and this is a "
                  "non-interactive run,")
            print("  so no persona was scaffolded. Run "
                  "`puffpai persona new <id> --goal \"...\"`")
            print("  when you are ready, or re-run with --scaffold.")
            return None
        if not _ask_yes_no("  Scaffold a blank persona for this goal?",
                           default=True):
            print("  Nothing was written. `puffpai persona new` does this any "
                  "time.")
            return None

    suggested = persona_scaffold.suggest_id(bot_name)
    app_id = suggested if (asked and non_interactive) else _ask(
        "  An id for it (lowercase, no spaces)", suggested,
        non_interactive=non_interactive)
    # The APP's name, not the bot's. BuildBot names the scaffolded app after the
    # bot, because there the persona IS the build's identity; here the build is
    # already HomeBot and this is one more app it carries, so a second thing
    # called HomeBot on the same dashboard would read as a mistake.
    display = persona_scaffold.list_label(app_id).title() or str(app_id)
    display = _ask("  A name for it", display, non_interactive=non_interactive)
    if not non_interactive and lists is None:
        lists = _ask("  Lists it should keep (comma separated, blank for none)")

    try:
        result = persona_scaffold.scaffold(
            app_id, name=display or app_id, goal=goal, lists=lists)
    except persona_scaffold.ScaffoldError as exc:
        print(f"  Nothing was written: {exc}")
        print("  `puffpai persona new <id> --name ... --goal ...` when you have "
              "picked a name.")
        return None

    for line in persona_scaffold.scaffold_summary_lines(result):
        print(f"  {line}")
    return {
        "scaffolded": True,
        "app": result["id"],
        "pack": result["id"],
        "name": result["name"],
        "goal": result["goal"],
        "lists": result["lists"],
        "surfaces": result["surfaces"],
        "app_manifest": result["app_path"],
        "pack_manifest": result["pack_path"],
        "note": ("Written by the wizard from the owner's goal; declarative "
                 "only, and editable by hand."),
    }


def _step_surfaces(args, setup_ops, proposal, goal, *, non_interactive: bool) -> dict:
    """The "Surfaces" step: the surface allowlist. Never silently empty."""
    enter_step("Surfaces")
    from puffpai import build_interview as bi

    print()
    print(step_banner("Surfaces", "What this build is allowed to touch."))
    derivation = setup_ops.ensure_surfaces(setup_ops.derive_surfaces(proposal), goal)
    derivation = _with_persona_surfaces(derivation, proposal.get("persona"))
    add = [str(s) for s in (getattr(args, "add_surface", None) or [])]
    drop = [str(s) for s in (getattr(args, "drop_surface", None) or [])]
    if add or drop:
        derivation = setup_ops.apply_surface_choices(derivation, add=add, remove=drop)
    for line in bi.surfaces_summary_lines(derivation):
        print(f"  {line}")
    if derivation.get("defaulted"):
        print("  (these are the household defaults, because no goal matched -")
        print("   a build with nothing on this list would refuse everything)")
    if not non_interactive:
        raw = _ask("Remove any of these? ids, comma separated (blank to keep all)")
        removals = [t.strip() for t in raw.split(",") if t.strip()]
        if removals:
            derivation = setup_ops.apply_surface_choices(derivation, remove=removals)
            for line in bi.surfaces_summary_lines(derivation):
                print(f"  {line}")
    return derivation


def _with_persona_surfaces(derivation: dict, persona: Optional[dict]) -> dict:
    """Add a scaffolded persona's own surface to the proposed allowlist.

    A build that just wrote itself an app, and then did not load the surface that
    app belongs to, would be a wizard arguing with itself. The reason line names
    the app rather than pretending the owner typed the surface by hand, and a
    derivation that only held the household DEFAULTS stops being defaulted - the
    owner's own app is a stated reason for one of these entries.
    """
    if not persona:
        return derivation
    from puffpai import build_interview as bi

    taxonomy = bi.surface_taxonomy()
    surfaces = list(derivation.get("surfaces") or [])
    detail = list(derivation.get("detail") or [])
    reasons = dict(derivation.get("reasons") or {})
    name = str(persona.get("name") or persona.get("app") or "your own app")
    added = False

    for raw in persona.get("surfaces") or []:
        sid = str(raw)
        if sid in surfaces or sid not in taxonomy:
            continue
        label = str((taxonomy[sid] or {}).get("label") or sid)
        reason = (f"{label}, because {name} is your own app and that is where "
                  f"it keeps its lists.")
        surfaces.append(sid)
        reasons[sid] = reason
        detail.append({
            "surface": sid, "label": label,
            "sensitive": bool((taxonomy[sid] or {}).get("sensitive")),
            "from": f"app:{persona.get('app')}",
            "matched_terms": [str(persona.get("app") or "")],
            "reason": reason,
        })
        added = True

    if not added:
        return derivation

    out = dict(derivation)
    out["surfaces"] = sorted(surfaces)
    out["reasons"] = reasons
    if detail:
        out["detail"] = sorted(detail, key=lambda d: str(d.get("surface")))
    # A UNION, never a recomputation: the household-default derivation carries no
    # ``detail`` at all, and rebuilding this list from the one entry added here
    # would quietly drop every sensitive surface the defaults had marked.
    sensitive = list(derivation.get("sensitive") or [])
    for entry in detail:
        sid = str(entry.get("surface"))
        if entry.get("sensitive") and sid not in sensitive:
            sensitive.append(sid)
    out["sensitive"] = sorted(sensitive)
    out["message"] = None
    return out


def _step_storage(args, setup_ops, *, non_interactive: bool) -> dict:
    """The "Storage" step: where this build keeps things. Validated first."""
    enter_step("Storage")
    print()
    print(step_banner("Storage", "Where this build keeps its files."))
    defaults = setup_ops.storage_defaults()
    flags = {
        "state_dir": getattr(args, "state_dir", None),
        "knowledge_dir": getattr(args, "knowledge_dir", None),
        "vault_dir": getattr(args, "vault_dir", None),
        "media_dir": getattr(args, "media_dir", None),
    }
    answers: dict[str, str] = {}
    for key, label, _cfg_key in setup_ops.STORAGE_FIELDS:
        if flags.get(key) is not None:
            answers[key] = str(flags[key])
            continue
        if non_interactive:
            answers[key] = defaults[key]
            continue
        while True:
            value = _ask(f"  {label}", defaults[key])
            ok, reason = setup_ops.validate_storage_path(
                value, allow_empty=(key == "media_dir"))
            if ok:
                answers[key] = value
                break
            print(f"    {reason}")

    applied = setup_ops.apply_storage(answers)
    if not applied["ok"]:
        problems = "; ".join(f"{k}: {v}" for k, v in applied["problems"].items())
        raise SetupRefused(
            "those storage paths will not work",
            f"This build cannot use the folders you gave it: {problems}",
            ["Give a full path this account can write to, or press Enter to "
             "take the default under this install's data folder."],
        )
    for key, _label, _cfg_key in setup_ops.STORAGE_FIELDS:
        shown = applied["paths"][key] or "(none - this household has no media library)"
        print(f"  {key}: {shown}")
    return applied["paths"]


def _step_vault(args, setup_ops, *, non_interactive: bool) -> dict:
    """The "Zone 1, the Vault" step: a PIN the owner sets, and a code they copy."""
    enter_step("Zone 1, the Vault")
    from puffpai import zone_vault
    from puffpai.security.vault import VaultError

    print()
    print(step_banner("Zone 1, the Vault", "Your lock, set by you."))
    for line in zone_vault.PIN_INTRO_LINES:
        print(f"  {line}")

    if getattr(args, "skip_vault", False):
        print("  Skipped. Nothing is protected until you set a PIN.")
        return setup_ops.vault_block(configured=False, sealed=False)

    if setup_ops.vault_initialized():
        print("  This box already has a vault. Leaving it exactly as it is.")
        return setup_ops.vault_block(configured=True, sealed=True,
                                     recovery_issued=False)

    pin = getattr(args, "vault_password", None)
    if pin is None and not non_interactive:
        while True:
            pin = _read_pin("  Choose a vault PIN")
            ok, reason = setup_ops.validate_vault_pin(pin)
            if not ok:
                print(f"    {reason}")
                continue
            again = _read_pin("  Type it again")
            if again != pin:
                print("    Those did not match. Try again.")
                continue
            break
    if not pin:
        print("  No PIN given, so no vault was created. Nothing is protected")
        print("  until you set one; run `puffpai setup` again when you are ready.")
        return setup_ops.vault_block(configured=False, sealed=False)

    ok, reason = setup_ops.validate_vault_pin(pin)
    if not ok:
        raise SetupRefused("that PIN is not strong enough", reason,
                           ["Run `puffpai setup` again and choose another."])

    try:
        block, code = setup_ops.seal_vault_with_recovery(pin)
    except VaultError as exc:
        raise SetupRefused("the vault would not seal", str(exc),
                           ["Check the vault folder from the Storage step is "
                            "writable, then run `puffpai setup` again."])
    del pin

    if non_interactive:
        # Nobody is standing there to read it, and an unattended run's output is
        # captured to a file by whatever started it. PRINTING the code here
        # would put the vault's spare key in that file, so it is not printed at
        # all: it was minted, it is unread, and the owner rotates it from the
        # Vault page. That is the honest state, recorded as such.
        del code
        print("  Vault sealed. A recovery code was issued but NOT shown -")
        print("  an unattended run has nobody to write it down, and printing it")
        print("  would leave it in this run's output. Generate a fresh one from")
        print("  the Vault page and write that one down.")
        setup_ops.record_pending_apply("vault_recovery_code", {
            "needs": "owner generates and writes down a recovery code",
        })
        return block

    print()
    for line in setup_ops.vault_recovery_notice():
        print(f"  {line}")
    print()
    print(f"      {code}")
    print()
    for _attempt in range(3):
        typed = _ask("  Type the code back")
        if setup_ops.confirm_vault_recovery(code, typed):
            block["recovery_confirmed"] = True
            print("  Confirmed. Keep that piece of paper somewhere safe.")
            break
        print("  That does not match what is on the screen.")
    else:
        print("  Not confirmed. The code above is still the only spare key.")
    del code
    return block


def _ask_edge(args, setup_ops, recommendation, *, non_interactive: bool) -> dict:
    """The edge-security branch of the Bridge step. Asks; applies nothing.

    Three answers, and the first one is "nothing". Somebody with no Pi-hole, no
    filtering router and no appliance still finishes this step with a configured
    Bridge - the posture ladder above IS their fence. That is the guard against
    this codebase's recurring bug: an absence is recorded as an answer
    (``declared: true``), never left blank so that a later reader mistakes "we
    never asked" for "there is nothing".

    Zone 3 is offered here as an OPT-IN and nothing more. Its row stays on the
    menu on a machine that cannot run it, marked unavailable with the reason, so
    the owner learns the option exists rather than never seeing it.
    """
    support = recommendation.get("zone3_support") or setup_ops.zone3_support()
    menu = recommendation.get("edge_options") or setup_ops.edge_options()

    kind = getattr(args, "edge", None)
    external_kind = getattr(args, "edge_kind", None) or ""
    external_url = getattr(args, "edge_url", None) or ""
    if external_url and kind is None:
        kind = "external"

    if kind is None and not non_interactive:
        print()
        print(f"  {menu['question']}")
        rows = list(menu.get("kinds") or [])
        for index, row in enumerate(rows, start=1):
            mark = "" if row.get("available", True) else "  [not on this machine]"
            print(f"    {index}. {row['label']}{mark}")
            print(f"       {row['description']}")
            for reason in row.get("reasons") or []:
                if not row.get("available", True):
                    print(f"       {reason}")
        kind = _pick_by_id("  Choose", rows)

    kind = kind or "none"

    if kind == "external" and not non_interactive:
        if not external_kind:
            print()
            print("  What is it? (this is written down, never contacted)")
            ext = list(menu.get("external_kinds") or [])
            for index, row in enumerate(ext, start=1):
                print(f"    {index}. {row['label']}")
            external_kind = _pick_by_id("  Choose", ext) or "other"
        if not external_url:
            print("  Its admin page, if you want it in the build record. This is")
            print("  recorded as a note to yourself; HomeBot never opens it.")
            for _attempt in range(3):
                candidate = _ask("  Admin URL (blank to skip)")
                if not candidate:
                    break
                ok, note = setup_ops.validate_edge_url(candidate)
                if ok:
                    external_url = candidate
                    if note:
                        print(f"    {note}")
                    break
                print(f"    {note}")

    edge = setup_ops.edge_block(kind, external_kind, external_url,
                                support=support)
    return edge


def _step_bridge(args, setup_ops, posture, ha_present, *, non_interactive: bool) -> dict:
    """The "Zone 3, the Bridge" step: maps onto this build's door registry."""
    enter_step("Zone 3, the Bridge")
    from puffpai import zone_bridge

    print()
    print(step_banner("Zone 3, the Bridge", "How much of the world this bot touches."))
    recommendation = setup_ops.bridge_options(
        has_home_assistant=bool(ha_present),
        has_external_firewall=bool(getattr(args, "has_firewall", False)),
        has_managed_lan=bool(getattr(args, "has_managed_lan", False)),
    )
    for line in zone_bridge.posture_summary_lines(recommendation):
        print(f"  {line}")

    chosen = getattr(args, "bridge_posture", None)
    if chosen is None and not non_interactive and not getattr(args, "skip_bridge", False):
        chosen = _pick_by_id("Choose a posture",
                             list(recommendation.get("postures") or []))
    chosen_by = "owner" if chosen else "default"
    # A skipped Bridge takes the SAFE default, never the suggestion: a posture
    # nobody chose should not be the loosest thing the box could have run.
    chosen = chosen or zone_bridge.DEFAULT_POSTURE

    edge = _ask_edge(args, setup_ops, recommendation,
                     non_interactive=non_interactive
                     or bool(getattr(args, "skip_bridge", False)))

    block = setup_ops.bridge_block(
        chosen, run_egress=(posture or {}).get("run_egress"),
        environment=recommendation.get("environment"), chosen_by=chosen_by,
        edge=edge)
    print(f"  Bridge: {block.get('posture')} ({block.get('chosen_by')})")
    for line in zone_bridge.edge_summary_lines(block.get("edge") or {}):
        print(line)

    # Zone 3 is the one answer with work behind it, and the wizard does none of
    # that work: no container is started, no port is bound, no DNS is changed.
    # The commands are written down for the owner instead.
    setup_ops.record_zone3_pending(block.get("edge") or {})

    # What the wizard COULD apply, it already has: the enumerated door table is
    # written into the config the runtime reads (persist_posture, Step 1). The
    # consent TIER a Moat/Drawbridge posture implies lives in the consent gate's
    # own configuration, which this wizard does not own, so it is recorded as
    # pending and reported rather than half-applied.
    if str(block.get("posture")) in ("moat", "drawbridge_up"):
        setup_ops.record_pending_apply("bridge_consent_tier", {
            "posture": block.get("posture"),
            "needs": "consent gate re-ask policy for this posture",
        })
        print("  Recorded for the consent gate: this posture asks more often "
              "than the default. The door table is already applied.")
    return block


def _step_ha(args, setup_ops, *, non_interactive: bool) -> tuple[dict, bool]:
    """The "Connect your Home Assistant" step, which is OPTIONAL and skipped
    with Enter. The address starts filled in; the token is read hidden and
    tested before the step ends."""
    import textwrap

    from puffpai import homebot_preset

    if getattr(args, "run_egress", None) == "sealed":
        # A sealed build reaches nothing, the home network included, so the
        # step is not offered (the Tools card says why).
        return setup_ops.ha_block(present=False), False

    enter_step("Home Assistant")
    print()
    print()
    print(homebot_preset.HA_TITLE)
    print("-" * len(homebot_preset.HA_TITLE))
    for line in textwrap.wrap(homebot_preset.HA_INTRO, 76):
        print(f"  {line}")

    url_flag = getattr(args, "ha_url", None)
    token_flag = getattr(args, "ha_token", None)
    if getattr(args, "skip_ha", False):
        present = False
    elif url_flag or token_flag:
        present = True
    elif non_interactive:
        present = False
    else:
        from puffpai.wizard import host
        answer = host._read_line("  Connect Home Assistant now? [y/N] ")
        present = (answer or "").strip().lower() in ("y", "yes")

    if not present:
        print("  Skipped. Everything else works the same. Connect it later on")
        print("  the Smart Home page or in Settings.")
        return setup_ops.ha_block(present=False), False

    url = url_flag or _ask("  Home Assistant address", setup_ops.DEFAULT_HA_URL,
                           non_interactive=non_interactive)
    token = token_flag
    if token is None and not non_interactive:
        for line in textwrap.wrap(homebot_preset.HA_TOKEN_HELP, 76):
            print(f"  {line}")
        print("  The token is kept in the Home Assistant settings file on this")
        print("  computer and nowhere else, never in a log.")
        token = _read_pin("  Long-lived access token")
    if not token:
        print("  No token given. The address is kept; add the token later on")
        print("  the Smart Home page or in Settings.")
        return setup_ops.ha_block(present=True, url=url,
                                  token_configured=False), True

    try:
        setup_ops.set_ha_config(url, token, enabled=True,
                                posture=getattr(args, "run_egress", None))
    except Exception:  # noqa: BLE001 - said plainly; the token is not kept
        del token
        print("  HomeBot talks to Home Assistant only on your home network, and")
        print(f"  {url} could not be found there. Nothing was saved. Check the")
        print("  address (Home Assistant shows it under Settings, System,")
        print("  Network) and connect it on the Smart Home page or in Settings.")
        return setup_ops.ha_block(present=True, url=url,
                                  token_configured=False), True
    del token_flag
    checked = False
    reachable = None
    if not non_interactive:
        # Skipped in --yes on purpose: an unattended run must not depend on a
        # box on somebody's LAN answering.
        checked = True
        result = setup_ops.test_ha_connection(url, token)
        reachable = bool(result.get("ok"))
        if reachable:
            print(f"  Connected. Home Assistant answered (version "
                  f"{result.get('version')}).")
        else:
            print(f"  {result.get('error')}")
            print("  The address and token are saved. Fix them on the Smart "
                  "Home page or in Settings when you can.")
    del token
    return setup_ops.ha_block(present=True, url=url, token_configured=True,
                              checked=checked, reachable=reachable), True


def _offer_model_pull(args, ollama_url, model, *, non_interactive: bool) -> bool:
    """Offer the download, behind the ``model_pull`` egress door.

    Our socket goes to Ollama on this box, but Ollama then fetches gigabytes
    from registry.ollama.ai - so the download is egress, it has a door, and the
    door is shut until the owner opens it. A shut door is said out loud with the
    command they can run themselves; it is never worked around.
    """
    from puffpai import brain_health
    from puffpai.security.egress_doors import door, egress_allowed

    allowed, _reason = egress_allowed("model_pull")
    if not allowed:
        print(f"  {door('model_pull').settings_hint}")
        return False
    if non_interactive:
        if not getattr(args, "pull", False):
            print(f"  Not downloading (no --pull). By hand: "
                  f"{brain_health.pull_command(model, ollama_url)}")
            return False
    elif not _ask_yes_no(f"  Download {model} now?", False):
        print(f"  Left for later:  "
              f"{brain_health.pull_command(model, ollama_url)}")
        return False

    result = brain_health.pull_model(model, url=ollama_url,
                                    on_progress=lambda line: print(f"    {line}"))
    print(f"  {result.get('message')}")
    return bool(result.get("ok"))


def _step_brain_ready(args, setup_ops, ollama_url, model, *,
                      non_interactive: bool = False) -> dict:
    """The "Brain ready?" step: is the chosen model actually there?"""
    enter_step("Brain ready?")
    print()
    print(step_banner("Brain ready?", "Is the model actually on this box?"))
    from puffpai import brain_health
    status = setup_ops.brain_status(url=ollama_url, model=model)
    ready = status.get("status") == brain_health.CONNECTED
    print(f"  {status.get('message') or status.get('status')}")
    if not ready and model:
        if _offer_model_pull(args, ollama_url, model,
                             non_interactive=non_interactive):
            status = setup_ops.brain_status(url=ollama_url, model=model)
            ready = status.get("status") == brain_health.CONNECTED
            print(f"  {status.get('message') or status.get('status')}")
        if not ready:
            print(f"  Pull it when you can:  ollama pull {model}")
    return {"ready": ready, "status": status}


def _step_manifest(args, setup_ops, *, census, brain, crew, interview, posture,
                   surfaces, vault_block, bridge_block, storage, household,
                   integrations, non_interactive: bool) -> dict:
    """The "The build manifest" step: the record of what this build is."""
    enter_step("The build manifest")
    print()
    print(step_banner("The build manifest", "The record of this build."))

    health = setup_ops.config_health_status()
    if health.get("blocking"):
        print("  Some of this build's own configuration files could not be read:")
        for line in health.get("summary") or []:
            print(f"    {line}")
        if not non_interactive and not _ask_yes_no(
                "  Record the build anyway?", True):
            raise SetupRefused(
                "stopped before writing the build record",
                "You chose not to record a build assembled from fallback "
                "configuration.",
                ["Restore config/capability_catalog.json and "
                 "config/model_catalog.json, then run `puffpai setup` again."])

    preview = setup_ops.preview_manifest(
        census,
        identity={"identity": setup_ops.HOMEBOT_DEFAULTS["persona"],
                  "name": getattr(args, "bot_name", None)
                  or setup_ops.HOMEBOT_DEFAULTS["bot_name"]},
        brain=brain, crew=crew, storage=storage,
        knowledge={"zim_dir": storage.get("knowledge_dir", ""),
                   "embedding_backend": brain.get("embedding_backend", "")},
        integrations=integrations, posture=posture, surfaces=surfaces,
        vault=vault_block, bridge=bridge_block,
    )
    manifest = preview["manifest"]
    manifest["household"] = household
    # The persona this build ships, recorded as a fact about the build. The pack
    # is installed by default because this IS HomeBot's wizard; the name is
    # whatever the owner chose.
    manifest["persona"] = {
        "id": setup_ops.HOMEBOT_DEFAULTS["persona"],
        "pack": setup_ops.HOMEBOT_DEFAULTS["pack"],
        "name": getattr(args, "bot_name", None)
        or setup_ops.HOMEBOT_DEFAULTS["bot_name"],
    }
    # A persona the wizard SCAFFOLDED (goal step) is recorded beside the shipped
    # one rather than instead of it: this build is still HomeBot, and the owner's
    # own app is a second fact about the same box. A statement about files on
    # disk, never a setting.
    scaffolded = interview.get("persona")
    if scaffolded:
        manifest["persona"]["scaffolded"] = scaffolded
    manifest["interview"] = {
        "goal": interview.get("goal", ""),
        "matched": [str(m.get("id")) for m in (interview.get("flat") or [])],
    }
    for line in preview["diff"] or ["  (first build record on this box)"]:
        print(f"  {line}")
    for warning in preview["warnings"]:
        print(f"  ! {warning}")

    if getattr(args, "dry_run", False):
        print("  --dry-run: nothing was written.")
        return manifest
    path = setup_ops.commit_manifest(manifest)
    print(f"  Wrote {path}")
    return manifest


def _print_closing(setup_ops, *, brain, vault_block, ha_present) -> None:
    """The "The closing screen" step: what happened, and what to do next."""
    enter_step("The closing screen")
    from puffpai import zone_vault

    print()
    print("=" * 60)
    print("  Setup finished" if brain.get("ready")
          else "  Setup finished - NOT READY to talk yet")
    print("=" * 60)
    for line in zone_vault.vault_summary_lines(vault_block):
        print(f"  {line}")
    print(f"  Home Assistant: {'configured' if ha_present else 'not used'}")
    if not brain.get("ready"):
        print("  The brain is not on this box yet; pull the model above first.")
    pending = setup_ops.pending_apply()
    if pending:
        print("  Recorded for a later step (nothing silently applied):")
        for item in pending:
            detail = item.get("detail") or {}
            print(f"    - {item.get('kind')}: {detail.get('needs', '')}")
            # The commands are the point of the Zone 3 item: the wizard chose
            # not to run them, so the owner has to be able to read them here
            # rather than go hunting for what "pending" meant.
            for command in detail.get("commands") or []:
                print(f"        {command}")
            if detail.get("doc"):
                sections = "; ".join(detail.get("doc_sections") or [])
                print(f"        read first: {detail['doc']}"
                      + (f" - {sections}" if sections else ""))
            if detail.get("mode"):
                print(f"        starts in {detail['mode']} mode: it watches, "
                      f"it does not block")
    print()
    # `py -3` is the Windows launcher and exists nowhere else, so this line
    # printed a command that does not run on the platform most of these
    # installs are on.
    launcher = "py -3" if sys.platform.startswith("win") else "python"
    print(f"  Start it with:  {launcher} start_api.py")


# ---------------------------------------------------------------------------
# the wizard
# ---------------------------------------------------------------------------

def run_setup(args: argparse.Namespace) -> int:
    """Walk every step. Raises SetupRefused for a deliberate stop."""
    from puffpai import __version__, setup_ops

    non_interactive = bool(getattr(args, "yes", False))

    print("=" * 60)
    print(f"  Welcome to HomeBot v{__version__}")
    print("  Privacy-first, local-first household AI setup wizard")
    print("=" * 60)
    print()

    posture = _step_posture(args, setup_ops, non_interactive=non_interactive)
    census = _step_hardware(args, setup_ops, non_interactive=non_interactive)
    ollama_url, installed = _step_ollama_url(args, setup_ops,
                                             non_interactive=non_interactive)
    model, brain_choice = _step_brain(args, setup_ops, census, ollama_url,
                                      installed, non_interactive=non_interactive)
    crew = _step_crew(setup_ops, census)
    household = _step_household(args, setup_ops, non_interactive=non_interactive)
    interview, goal = _step_interview(args, setup_ops, census,
                                      non_interactive=non_interactive)
    surfaces = _step_surfaces(args, setup_ops, interview, goal,
                              non_interactive=non_interactive)
    storage = _step_storage(args, setup_ops, non_interactive=non_interactive)
    vault_block = _step_vault(args, setup_ops, non_interactive=non_interactive)

    bot_name = getattr(args, "bot_name", None) or setup_ops.HOMEBOT_DEFAULTS["bot_name"]
    if not non_interactive:
        bot_name = _ask("  What should this bot be called?", bot_name)
    args.bot_name = bot_name
    setup_ops.set_identity(bot_name)
    setup_ops.set_ollama_config(model, ollama_url)

    ha_block, ha_present = _step_ha(args, setup_ops,
                                    non_interactive=non_interactive)
    bridge_block = _step_bridge(args, setup_ops, posture, ha_present,
                                non_interactive=non_interactive)
    brain_ready = _step_brain_ready(args, setup_ops, ollama_url, model,
                                    non_interactive=non_interactive)
    brain_choice["ready"] = bool(brain_ready.get("ready"))

    _step_manifest(
        args, setup_ops, census=census, brain=brain_choice, crew=crew,
        interview={"goal": goal, **interview}, posture=setup_ops.posture_block(posture),
        surfaces=setup_ops.surfaces_block(surfaces), vault_block=vault_block,
        bridge_block=bridge_block,
        storage=setup_ops.storage_block(storage),
        household=setup_ops.household_block(household),
        integrations=setup_ops.integrations_block(
            ha_block, media_library=bool(storage.get("media_dir"))),
        non_interactive=non_interactive)

    if not getattr(args, "dry_run", False):
        setup_ops.mark_complete()
    _print_closing(setup_ops, brain=brain_ready, vault_block=vault_block,
                   ha_present=ha_present)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """``puffpai setup``: the card wizard.

    The verb runs :func:`puffpai.wizard.host._cmd_setup`, which walks the
    runner's schedule - the twelve card packages plus this build's three
    uncarded steps. The owner is asked the same questions in the same order;
    what changed is that each answer is written down as it is given, so a run
    that stops can be picked up and a step that fails records a gap.

    The linear walk below (:func:`run_setup`, :func:`cmd_setup_legacy`) is kept
    and still runs: it is what the HomeBot-specific step tests exercise
    directly, and it is the fallback if a build ever ships without the wizard
    packages. The verb does not run it.
    """
    from puffpai.wizard import host
    return host._cmd_setup(args)


def cmd_setup_legacy(args: argparse.Namespace) -> int:
    """The pre-card linear wizard, with the same exit codes.

    Exit codes: 0 finished (READY or NOT READY - a brainless build still ends 0,
    because the documented quickstart chains commands and a wizard that broke
    the chain would take the owner further from the fix), 1 a deliberate
    refusal, 2 a bug.
    """
    global _current_step
    _current_step = ""
    try:
        return run_setup(args)
    except SetupRefused as refusal:
        _print_refusal(refusal)
        return 1
    except KeyboardInterrupt:
        print()
        print("Stopped. Re-run `puffpai setup` when you are ready.")
        return 1
    except Exception as exc:  # noqa: BLE001 - this is the floor, it catches all
        _print_crash(exc, _crash_log_path(exc))
        return 2


def add_arguments(sub: argparse.ArgumentParser) -> None:
    """Attach the setup verb's flags. Called from :mod:`puffpai.cli`."""
    sub.add_argument("--yes", "-y", action="store_true",
                     help="Non-interactive: take every default and ask nothing")
    sub.add_argument("--dry-run", dest="dry_run", action="store_true",
                     help="Walk the wizard and show the manifest diff without "
                          "writing the build record or the completion flag")
    sub.add_argument("--ollama-url", dest="ollama_url", default=None,
                     help="Ollama base URL, for an Ollama that is not at the "
                          "default http://localhost:11434")
    sub.add_argument("--model", default=None,
                     help="Ollama model name (overrides the brain menu)")
    # ``--name`` is the card wizard's spelling and ``--bot-name`` is this
    # tree's; one dest, so a script written against either keeps working and
    # neither can quietly lose to the other.
    sub.add_argument("--name", "--bot-name", dest="bot_name", default=None,
                     help="What this bot is called (default: HomeBot)")
    sub.add_argument("--owner-name", dest="owner_name", default=None,
                     help="The owner's name, for the household profile")
    sub.add_argument("--member", dest="member", action="append", default=None,
                     metavar="NAME",
                     help="Another person in the household (repeatable)")
    sub.add_argument("--build-network", dest="build_network", default=None,
                     choices=["online", "offline"],
                     help="Whether this machine may reach the internet WHILE "
                          "YOU BUILD")
    sub.add_argument("--run-egress", dest="run_egress", default=None,
                     choices=["sealed", "island", "enumerated"],
                     help="What the FINISHED BOT may reach when it runs")
    sub.add_argument("--egress-door", dest="egress_door", action="append",
                     default=None, metavar="DOOR",
                     help="Open one named egress door (repeatable). Door ids "
                          "are this build's own registry ids")
    sub.add_argument("--stated-ram", dest="stated_ram", type=float, default=None,
                     help="Total RAM in GB, when detection is unavailable")
    sub.add_argument("--goal", dest="goal", default=None,
                     help="What this household needs (drives the catalog "
                          "suggestions; defaults to HomeBot's household goal)")
    sub.add_argument("--scaffold", dest="scaffold", action="store_true",
                     help="The goal step: scaffold a blank persona (one app + "
                          "one pack, in your own data directory) for --goal, "
                          "whether or not the catalog matched anything. The same "
                          "thing `puffpai persona new` does")
    sub.add_argument("--lists", dest="lists", default=None,
                     help="The goal step: the lists a scaffolded persona should "
                          "keep, comma separated (e.g. \"tools,projects\"). "
                          "Implies --scaffold")
    sub.add_argument("--add-surface", dest="add_surface", action="append",
                     default=None, metavar="SURFACE",
                     help="Add a surface to the allowlist (repeatable)")
    sub.add_argument("--drop-surface", dest="drop_surface", action="append",
                     default=None, metavar="SURFACE",
                     help="Remove a surface from the allowlist (repeatable)")
    sub.add_argument("--state-dir", dest="state_dir", default=None,
                     help="Where this build keeps its own files")
    sub.add_argument("--knowledge-dir", dest="knowledge_dir", default=None,
                     help="Where .zim knowledge packs live")
    sub.add_argument("--pull", dest="pull", action="store_true",
                     help="Download the chosen model in a non-interactive run "
                          "(needs the model_pull egress door open)")
    sub.add_argument("--vault-dir", dest="vault_dir", default=None,
                     help="Where the Vault's encrypted files live")
    sub.add_argument("--media-dir", dest="media_dir", default=None,
                     help="Your media library (may be empty)")
    sub.add_argument("--skip-vault", dest="skip_vault", action="store_true",
                     help="Do not create the encrypted vault")
    sub.add_argument("--vault-pin", "--vault-password", dest="vault_password",
                     default=None,
                     help="Vault PIN (TEST/AUTOMATION ONLY - normally you are "
                          "asked for it without it being echoed)")
    sub.add_argument("--bridge-posture", dest="bridge_posture", default=None,
                     choices=["open_yard", "fenced", "moat", "drawbridge_up"],
                     help="Zone 3: the Bridge posture")
    sub.add_argument("--skip-bridge", dest="skip_bridge", action="store_true",
                     help="Skip the Bridge questions (records the safe default)")
    sub.add_argument("--edge", dest="edge", default=None,
                     choices=["none", "zone3", "external"],
                     help="What protects your network edge today: nothing, "
                          "HomeBot's own Zone 3 (Linux + Docker, opt-in, never "
                          "started by the wizard), or gear you already run")
    sub.add_argument("--edge-kind", dest="edge_kind", default=None,
                     help="With --edge external: what it is (pihole, adguard, "
                          "firewall, homelab, or your own words)")
    sub.add_argument("--edge-url", dest="edge_url", default=None,
                     help="With --edge external: its admin page, recorded as a "
                          "note. Must be a LAN or loopback address; never opened")
    sub.add_argument("--has-firewall", dest="has_firewall", action="store_true",
                     help="You run your own firewall (informs the suggestion)")
    sub.add_argument("--has-managed-lan", dest="has_managed_lan",
                     action="store_true",
                     help="Your home network is a managed one")
    sub.add_argument("--ha-url", dest="ha_url", default=None,
                     help="Home Assistant base URL (implies you run one)")
    sub.add_argument("--ha-token", dest="ha_token", default=None,
                     help="Home Assistant long-lived token. Written to the HA "
                          "config file only - never to the build record or a log")
    sub.add_argument("--skip-ha", dest="skip_ha", action="store_true",
                     help="Record 'no Home Assistant' and move on")
    # Zone 2, the sourcing half. Nothing here downloads on its own: a
    # non-interactive run needs BOTH --fetch-source (what) and
    # --accept-source-size (the yes that was never given on a card nobody read).
    sub.add_argument("--zim-dir", dest="zim_dir", default=None,
                     help="Where .zim knowledge packs live (the watched folder)")
    sub.add_argument("--no-auto-index", dest="no_auto_index",
                     action="store_true",
                     help="Do not learn new packs in the background")
    sub.add_argument("--skip-knowledge", dest="skip_knowledge",
                     action="store_true",
                     help="Skip Zone 2 entirely: nothing is offered, fetched or "
                          "read")
    sub.add_argument("--skip-sourcing", dest="skip_sourcing",
                     action="store_true",
                     help="Skip the source catalog (the folder and the index "
                          "handoff still run)")
    sub.add_argument("--no-suggestions", dest="no_suggestions",
                     action="store_true",
                     help="Never ask this build's own model for unvetted source "
                          "suggestions")
    sub.add_argument("--fetch-source", dest="fetch_source", action="append",
                     default=None, metavar="SOURCE_ID",
                     help="Name a vetted source to download (repeatable). "
                          "Needs --accept-source-size on a --yes run")
    sub.add_argument("--accept-source-size", dest="accept_source_size",
                     action="store_true",
                     help="On a --yes run: the deliberate yes to the download "
                          "sizes nobody was shown a card for")
    sub.add_argument("--own-data", dest="own_data", action="append",
                     default=None, metavar="PATH",
                     help="A folder of your own to count (repeatable). A folder "
                          "you do not name is never opened")
    sub.add_argument("--skip-own-data", dest="skip_own_data",
                     action="store_true",
                     help="Read none of your own folders")
    sub.set_defaults(func=cmd_setup)


__all__ = [
    "SETUP_STEPS",
    "SetupRefused",
    "add_arguments",
    "cmd_setup",
    "current_step_label",
    "enter_step",
    "run_setup",
    "step_banner",
]
