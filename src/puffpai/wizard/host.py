"""What the wizard packages need from the command line, in one place.

The twelve packages came from BuildBot, where the whole wizard lived in one
``puffpai/cli.py`` and every package could say ``from puffpai import cli`` and
reach the helper it needed. This tree splits that file in two - ``cli.py`` is a
verb dispatcher and ``setup_cli.py`` holds the conversation - so the names the
packages reach for were not there.

This module is the host: **one implementation per behaviour, and no duplicates**.

* Where this tree already has the behaviour (the vault ceremony, reading a PIN,
  the step banner, the refusal type, the persona scaffold, the closing screen),
  the name here is an ALIAS to :mod:`puffpai.setup_cli`. Copying those would have
  given this build two vault ceremonies that could drift apart, which is the bug
  this module exists to avoid.
* Where the behaviour only ever existed in BuildBot's ``cli.py`` (the launcher
  block, the posture objection, the RAM parser, the resume screen), it is ported
  here once.
* Where the behaviour exists in both builds but is genuinely different (how a
  first device is let in: this build uses the console setup CODE, not a pairing
  code and an ``npm run pair:code``), the HomeBotBuild answer is what is
  implemented, and the docstring says so.

:mod:`puffpai.cli` re-exports every name below, so the package files are
BYTE-IDENTICAL to BuildBot's and keep working with ``from puffpai import cli``.
Nothing here is imported at ``cli`` import time except this module, which itself
imports nothing heavier than :mod:`os`/:mod:`sys` at module scope.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Optional

from puffpai import prove_cli, setup_cli

# ---------------------------------------------------------------------------
# aliases: this tree already owns these behaviours
# ---------------------------------------------------------------------------

#: The refusal type. One class, so ``except SetupRefused`` in the runner catches
#: what ``setup_cli`` raises and what a package raises.
SetupRefused = setup_cli.SetupRefused

enter_step = setup_cli.enter_step
step_banner = setup_cli.step_banner
current_step_label = setup_cli.current_step_label
_ask = setup_cli._ask
_ask_yes_no = setup_cli._ask_yes_no
_ask_paragraph = setup_cli._ask_paragraph
_pick_by_id = setup_cli._pick_by_id
_read_pin = setup_cli._read_pin
_print_refusal = setup_cli._print_refusal
_print_crash = setup_cli._print_crash
_crash_log_path = setup_cli._crash_log_path
_maybe_scaffold_persona = setup_cli._maybe_scaffold_persona
_with_persona_surfaces = setup_cli._with_persona_surfaces
_print_closing = setup_cli._print_closing
GOAL_MAX_CHARS = setup_cli.GOAL_MAX_CHARS
NO_STEP = setup_cli.NO_STEP


#: End of input is STICKY. Once the answers run out, every later question takes
#: its default instead of asking again, so a redirected file that is shorter than
#: the wizard still finishes the wizard. Per run, not per process:
#: :func:`_reset_stdin_state` clears it at the top of every ``puffpai setup``.
#:
#: The flag itself lives on :mod:`puffpai.cli` - the module the packages and the
#: tests reach for by name - and the two functions below read and write it
#: there, so there is exactly one latch however it is reached.
def _latch(value: Optional[bool] = None) -> bool:
    from puffpai import cli
    if value is not None:
        cli._stdin_exhausted = bool(value)
    return bool(getattr(cli, "_stdin_exhausted", False))


def _reset_stdin_state() -> None:
    """Forget that stdin ran out. One run, one stdin."""
    _latch(False)


def _read_line(prompt: str) -> Optional[str]:
    """One line from stdin, or ``None`` when there is nothing left to read.

    Two things a bare ``input()`` does not do. It FLUSHES stdout first, because
    a run with stdout redirected to a file is block-buffered and a prompt nobody
    can see makes every stall look like it happened at whatever step printed
    last. And end-of-file is sticky - see :func:`_latch`.

    Returns ``None`` at end of input, never ``""``: an empty line is a real
    answer (it means "the default") and running out of input is not.
    """
    if _latch():
        return None
    try:
        sys.stdout.flush()
    except (OSError, ValueError):  # pragma: no cover - closed stdout
        pass
    try:
        line = input(prompt)
    except (EOFError, KeyboardInterrupt, OSError, ValueError):
        _latch(True)
        print()
        return None
    return line.rstrip("\r\n")


def _stdin_is_tty() -> bool:
    """True when a hidden PIN can be typed on this run.

    A real terminal, **or** the documented stdin-secrets switch this tree ships
    for driving the interactive wizard from a script
    (:data:`setup_cli.STDIN_SECRETS_ENV`, used by
    ``tests/test_wizard_interactive.py``). Without the second clause every
    scripted interactive run would be refused at the vault card as "nobody is
    there", which is exactly the case that switch exists to describe.
    """
    if os.environ.get(setup_cli.STDIN_SECRETS_ENV) == "1":
        return True
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# names, addresses and the launchers this install has
# ---------------------------------------------------------------------------

#: What this bot is called when nobody has named it. The same string as
#: ``setup_ops.HOMEBOT_DEFAULTS["bot_name"]`` - stated here as a literal rather
#: than imported so that importing the CLI does not drag in the census, the
#: catalogs and the vault. ``tests/test_wizard_homebot.py`` holds the two to
#: each other.
DEFAULT_BOT_NAME = "HomeBot"


def bot_name(fallback: str = "") -> str:
    """What THIS build is called, read from the config the dashboard reads."""
    fallback = fallback or DEFAULT_BOT_NAME
    try:
        from puffpai.config import get_config
        return str(getattr(get_config().identity, "name", "") or fallback)
    except Exception:  # noqa: BLE001
        return fallback


def _dashboard_url(cfg) -> str:
    """The dashboard address as configured, or "" when nothing is configured.

    Nothing here guesses a port. A wizard that prints an address it invented
    hands the owner a dead link at the exact moment they are deciding whether
    the thing works.
    """
    env = os.environ.get("PUFFPAI_DASHBOARD_URL", "").strip()
    if env:
        return env
    raw = getattr(cfg, "_raw", None) or {}
    block = raw.get("dashboard") if isinstance(raw, dict) else None
    if isinstance(block, dict):
        url = str(block.get("url") or "").strip()
        if url:
            return url
        host = str(block.get("host") or "").strip()
        port = block.get("port")
        if host and port:
            return f"http://{host}:{port}"
    return ""


#: The dashboard start sequence, as steps, named once. Every printed form below
#: is joined from this tuple so no shell's copy can drift from another's.
DASHBOARD_START_STEPS: tuple[str, ...] = (
    "cd dashboard",
    "npm install",
    "npm run build",
    "npm run start",
)

#: The POSIX form.
DASHBOARD_START_COMMAND = " && ".join(DASHBOARD_START_STEPS)

#: The Windows form. ``&&`` is not a statement separator in PowerShell 5.1, so
#: the POSIX line does not run at all in the shell that opens when a Windows
#: owner clicks "Windows PowerShell". ``;`` is accepted by PowerShell 5.1,
#: PowerShell 7 and bash alike.
DASHBOARD_START_COMMAND_WINDOWS = "; ".join(DASHBOARD_START_STEPS)


def dashboard_start_command(os_name: str = "") -> str:
    """The dashboard start command for the shell the owner is actually in."""
    return (DASHBOARD_START_COMMAND_WINDOWS
            if (os_name or os.name) == "nt" else DASHBOARD_START_COMMAND)


def pair_code_command(os_name: str = "") -> str:
    """How to reprint the code that lets a browser finish setup.

    **This build's answer, not BuildBot's.** BuildBot pairs a device with a
    dashboard pairing code and an ``npm run pair:code``. This build gates
    first-run setup on a console SETUP CODE
    (:mod:`puffpai.security.setup_code`), and the command that reprints it is a
    verb of this program, so it is the same on every shell.
    """
    return "puffpai setup-code"


def pairing_block_lines() -> list[str]:
    """How to get into your own build from another device, as lines.

    LOCAL BY CONSTRUCTION, and that is the feature: the code proves console
    access, so the only way to read it is to have the console. There is no HTTP
    route that returns it.

    Never raises. If the code cannot be read or minted, the command alone is
    printed - an owner running one extra command is a much better outcome than a
    wizard that crashes on its closing screen.
    """
    code = None
    try:
        from puffpai.security import setup_code
        if setup_code.is_required():
            code = setup_code.current()
    except Exception:  # noqa: BLE001 - a display convenience never crashes setup
        code = None

    lines = ["", "Setup from a browser on another device needs this machine's "
                 "setup code."]
    if code:
        lines += ["Type it into the browser when it asks:",
                  "",
                  f"    {code}",
                  "",
                  "It is destroyed the moment setup finishes. If you lose it "
                  "before then,",
                  "print a fresh one on this machine:"]
    else:
        lines.append("Print it on this machine when you need it:")
    lines += ["",
              f"    {pair_code_command()}",
              "",
              "After setup, changing this build needs an unlocked vault rather "
              "than a code."]
    return lines


def installed_launchers(os_name: str = "") -> Optional[dict[str, str]]:
    """The launchers THIS install has, or ``None`` when it was not installed by one.

    This build ships no install pointer, so there is nothing to read and the
    honest answer is ``None`` - which is exactly what the Handoff card treats as
    "print the manual start lines". The parameter and the return shape are
    BuildBot's so the package that reads it is unchanged; when this tree grows
    an installer, this is the one function that has to learn about it.
    """
    try:
        from puffpai import install_pointer  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no installer on this build
        return None
    pointer = install_pointer.find_pointer()
    if not pointer:
        return None
    env_path = install_pointer.read_pointer(pointer)
    if not env_path:
        return None
    values = install_pointer.parse_env_file(env_path)
    windows = (os_name or os.name) == "nt"
    port = (values.get("PUFFPAI_DASHBOARD_PORT") or "").strip()
    wanted = values.get("PUFFPAI_WITH_DASHBOARD")
    has_dashboard = port.isdigit() if wanted is None else str(wanted).strip() == "1"
    return {
        "env_file": env_path,
        "backend": ".\\run-backend.ps1" if windows else "./run-backend.sh",
        "dashboard": ((".\\run-dashboard.ps1" if windows else "./run-dashboard.sh")
                      if has_dashboard else ""),
        "dashboard_url": (f"http://127.0.0.1:{port}"
                          if has_dashboard and port.isdigit() else ""),
    }


# ---------------------------------------------------------------------------
# what the owner may state, and what the posture will allow
# ---------------------------------------------------------------------------

MAX_STATED_RAM_GB = 4096.0


def _valid_ram_gb(raw: Any) -> tuple[Optional[float], str]:
    """Parse a stated RAM figure in GB. Returns (value, reason-it-was-rejected)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, ""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, f"'{raw}' is not a number, so I cannot fit a brain to it."
    if value != value or value in (float("inf"), float("-inf")):
        return None, f"'{raw}' is not a number, so I cannot fit a brain to it."
    if value <= 0:
        return None, (f"{value:g} GB is not an amount of memory. Tell me the "
                      f"real figure, or leave it blank and I will say unknown.")
    if value > MAX_STATED_RAM_GB:
        return None, (f"{value:g} GB is past anything this fits a brain for "
                      f"(the ceiling is {MAX_STATED_RAM_GB:g} GB). If that is "
                      f"really the machine, the brain menu is not your limit.")
    return value, ""


#: What each run-egress posture will let a service URL be, in plain words. Shown
#: with a refusal so the owner does not have to go read SECURITY.md to act on it.
POSTURE_URL_RULES: tuple[str, ...] = (
    "--run-egress island (or enumerated) allows a private-network address "
    "(10.x, 172.16-31.x, 192.168.x) as well as loopback",
    "--run-egress sealed allows loopback only (127.0.0.1 / localhost)",
)


def _posture_violation(service_id: str, url: str, posture: Optional[dict]) -> Any:
    """The posture objection to writing ``url``, or ``None``. Never raises.

    Run at the step that CAPTURES the address, not at the write six steps later:
    a refusal after the vault exists and its one-time recovery code has already
    scrolled past is not a refusal, it is a mess. The posture is passed in
    explicitly because the wizard has not written it to config yet.
    """
    try:
        from puffpai.security import lan_posture
    except Exception:  # pragma: no cover - import guard only
        return None
    egress = str((posture or {}).get("run_egress") or "")
    if not egress:
        return None
    try:
        return lan_posture.check_url(service_id, url, egress, resolve=True)
    except Exception:  # pragma: no cover - a resolver fault is not a refusal
        return None


def _refuse_for_posture(violation: Any) -> SetupRefused:
    """Turn a posture violation into the refusal the owner reads."""
    return SetupRefused(
        "that address is not allowed by this build's posture",
        getattr(violation, "user_message", str(violation)),
        list(POSTURE_URL_RULES) + [
            "or re-run `puffpai setup` and choose a different address",
        ])


def _ollama_address_source(args: argparse.Namespace, cfg) -> tuple[str, str]:
    """``(address, where it was configured)``; the source is ``""`` if nowhere.

    An empty source is the ONLY licence this wizard has to go guessing. A
    configured address is an answer, whichever of the three places it came from,
    so a box that has been pointed away from port 11434 deliberately is not
    probed there anyway.
    """
    flag = getattr(args, "ollama_url", None)
    if flag:
        return str(flag), "the --ollama-url flag"

    # BuildBot's config module exposes ``ollama_url_from_env()``; this tree's
    # does not - it applies the override while it builds the config object and
    # names the variable in ``OLLAMA_URL_ENV``. Reading the variable here is the
    # same question asked of this tree's config module. (Importing the BuildBot
    # helper inside a bare ``except`` would fail here, so an address set in the
    # environment would be invisible and a config-file address named and
    # RETURNED instead of it.)
    from puffpai.config import OLLAMA_URL_ENV

    env_url = os.environ.get(OLLAMA_URL_ENV, "").strip()
    if env_url:
        return env_url, f"the {OLLAMA_URL_ENV} environment variable"

    # The shipped default is not an answer. BuildBot's config module reads only
    # the owner's data dir, so a fresh build there has no ``ollama`` block at
    # all and the wizard probes; this tree's config module always layers the
    # SHIPPED config/puffpai_config.json underneath, which carries
    # ``http://localhost:11434``. Taking that as "your config file" meant this
    # build never probed - an owner running Ollama on 11435 (or in WSL, or in
    # Docker) was never offered the address that actually answers, and the
    # survey code was dead. A config url that is still the shipped default
    # means nobody has answered yet; anything else is a deliberate answer.
    from puffpai import brain_health

    raw = getattr(cfg, "_raw", None) or {}
    block = raw.get("ollama") if isinstance(raw, dict) else None
    url = str((block or {}).get("url") or "").strip() if isinstance(block, dict) else ""
    if url and not brain_health._same_server(url, brain_health.DEFAULT_URL):
        return url, "your config file"

    return cfg.ollama.url, ""


# ---------------------------------------------------------------------------
# Zone 1: the vault blocks and the recovery ceremony's limits
# ---------------------------------------------------------------------------

def _vault_dir(cfg) -> str:
    """Resolve the vault directory from config (absolute)."""
    vault_dir = cfg.paths.vault_data_dir
    return (vault_dir if os.path.isabs(vault_dir)
            else os.path.join(cfg.DATA_DIR, vault_dir))


VAULT_REJECTED_LINE = ("the vault was not created - re-run with a compliant "
                       "--vault-pin")


def _vault_skipped(setup_ops) -> dict:
    """A vault nobody asked for. Not an error: ``ready``, because it was a choice."""
    block = setup_ops.vault_block(configured=False, sealed=False)
    block["ready"] = True
    block["rejected"] = False
    return block


def _vault_rejected(setup_ops, reason: str) -> dict:
    """A vault the owner DID ask for and did not get. That is a failed build."""
    block = setup_ops.vault_block(configured=False, sealed=False)
    block["ready"] = False
    block["rejected"] = True
    block["rejected_reason"] = str(reason)
    return block


def _existing_vault_block() -> dict:
    """The vault block for a vault this run did not create.

    Re-uses what the last run recorded when there is a manifest, so a re-run
    reports the truth about the recovery code instead of overwriting it with a
    fresh "none issued".
    """
    from puffpai import build_manifest, setup_ops

    previous = (build_manifest.read_manifest() or {}).get("vault")
    if isinstance(previous, dict) and previous.get("configured"):
        block = dict(previous)
    else:
        block = setup_ops.vault_block(configured=True, sealed=True)
    block["ready"] = True
    block["rejected"] = False
    return block


# How many times the recovery screen may be shown before the wizard accepts that
# it is not going to be written down today. Bounded so a broken stdin cannot spin
# forever, and generous enough that a person copying 25 characters gets to fumble.
_RECOVERY_MAX_SHOWS = 3
_RECOVERY_MAX_MISMATCHES = 5

# What the owner can type instead of the code to say "not right now". The code on
# screen is then burned and a fresh one issued, rather than the wizard shrugging
# and moving on with a code nobody copied.
_RECOVERY_ESCAPES = ("skip", "later", "cannot", "can't", "cant", "no")


# ---------------------------------------------------------------------------
# `puffpai prove`: the two lines it says about where a verdict came from
# ---------------------------------------------------------------------------

#: Aliases, not copies. This tree already says both of these - ``prove_cli`` is
#: where ``puffpai prove`` lives here, and it is the module that PRINTS them. A
#: second spelling on ``cli`` would be a second string to keep in step with what
#: the command actually says. ``prove_cli`` imports nothing heavier than
#: argparse/os/sys at module scope, so aliasing costs the CLI nothing.
PROVE_OLD_BACKEND = prove_cli.PROVE_OLD_BACKEND
PROVE_LOCAL_LABEL = prove_cli.PROVE_LOCAL_LABEL


# ---------------------------------------------------------------------------
# Zone 2: what the closing screen says about packs, and the step behind it
# ---------------------------------------------------------------------------

def _print_packs_not_downloaded(interview: Optional[dict],
                                sources: Optional[dict] = None) -> None:
    """Say out loud that the packs picked in the interview are not here yet.

    Picking a pack records a want. It does not fetch a file, and an owner who
    read the menu as a download list ends the wizard believing their bot knows
    things it has never seen. So every pack that is chosen and not downloaded is
    named with its size and the one command that fetches it.

    Never raises: a status that cannot be computed is a missing paragraph, not a
    wizard that stops at Zone 2.
    """
    import logging

    from puffpai.knowledge import build_sources
    from puffpai.wizard import fixes

    try:
        manifest = {"knowledge": {
            "packs": [i for i in (interview or {}).get("selected", [])
                      if str(i).startswith("pack:")],
            "sources": sources or {}}}
        if build_sources.sourcing_skipped(manifest):
            # Nothing was offered, so there is nothing that was "chosen". One
            # plain line and the command that runs the step.
            print()
            print("  Knowledge setup was skipped, so nothing was chosen and")
            print("  nothing was downloaded.")
            print(f"    {fixes.render({'kind': 'command', 'id': 'setup.rerun', 'args': {}})}")
            return
        chosen = manifest["knowledge"]["packs"]
        if not chosen:
            return
        status = build_sources.source_status(manifest)
        pending = [item for item in status.get("items", [])
                   if item.get("status")
                   == build_sources.STATUS_SELECTED_NOT_DOWNLOADED]
        if not pending:
            return
        print()
        print("  The packs you picked are NOT downloaded yet. Picking one")
        print("  records what you want; nothing was fetched. Each one, its")
        print("  size, and the command that fetches it:")
        for item in pending:
            print(f"    {item.get('label') or item.get('id')} "
                  f"({item.get('size_label') or 'size unknown'})")
            if item.get("command"):
                print(f"      {item['command']}")
        print("  Until you run those, the bot answers from its base model and")
        print("  whatever is already in your knowledge folder.")
    except Exception as exc:  # noqa: BLE001 - a paragraph is never worth a stop
        logging.getLogger(__name__).warning(
            "Could not report pack download status: %s", exc)


def _setup_knowledge_step(args: argparse.Namespace, cfg, *,
                          posture: Optional[dict] = None,
                          gates: Optional[dict] = None,
                          interview: Optional[dict] = None,
                          non_interactive: bool = False) -> dict:
    """Zone 2, the Brain: the knowledge block this build records.

    BUILDBOT NOTE. In BuildBot this is a hand-written step in ``cli.py`` that
    asks the whole of Zone 2 itself. This tree has no such
    step: the Integrate package owns Zone 2, and ``setup_cli`` never grew a
    ``_step_knowledge``. So this is not a second implementation - it is the
    command-line entry point onto the one that exists, kept because the closing
    screen and ``puffpai prove`` both ask "what did Zone 2 record".

    The one branch answered here rather than by the card is the skip, because
    skipping is a decision about whether the card runs at all:

        a run that skipped this step offered nothing, so nothing downstream may
        say a source was chosen here.

    That is what ``sources.skipped`` records, and it is what
    ``build_sources.sourcing_skipped`` reads.
    """
    from puffpai.wizard import loader, runner as wizard_runner

    enter_step("Zone 2, the Brain")
    if getattr(args, "skip_knowledge", False):
        print("Skipping knowledge setup. Edit config later to point at your "
              "ZIM folder.")
        return {"sources": {"offered": [], "fetched": [], "not_fetched": [],
                            "skipped": "skipped"}}

    packages, _ = loader.load_builtin_packages()
    package = packages["integrate"]
    module = loader.load_module(package)
    ctx = wizard_runner.RunContext(
        args=args, cfg=cfg, non_interactive=non_interactive,
        shared={"interview": dict(interview or {}),
                "posture": dict(posture or {}),
                "gates": dict(gates or {})},
        sections={}, package=package)
    ctx.proposal = module.propose(ctx)
    return module.apply(ctx, {})["data"]["block"]


# ---------------------------------------------------------------------------
# the entries the runner owns itself, and this build's extra steps
# ---------------------------------------------------------------------------

def _wizard_runner_parts(may_complete=None) -> dict:
    """The schedule entries that belong to the runner rather than to a package.

    The engine's own: writing the Ollama config, noting what was written, the
    manifest (assembled from the sections the packages wrote), the first screen,
    and the pieces that build from a goal (the needs step, the one summary, the
    factory and the question before building). A HomeBot setup schedules none
    of the goal pieces; they are wired here so a run that does schedule them
    finds them.

    This build's own entries come from :mod:`puffpai.homebot_preset`: the
    preset seed, the model download offer and the download, Home Assistant, and
    the storage and household defaults.

    *may_complete* is a callable the caller supplies when finishing the build is
    not the same thing as writing the manifest. Fail closed: anything that is
    not a callable returning true leaves setup incomplete.
    """
    from puffpai import homebot_preset

    def persist_config(ctx):
        from puffpai import setup_ops
        os.makedirs(ctx.cfg.CONFIG_DIR, exist_ok=True)
        path = setup_ops.set_ollama_config(ctx.shared.get("chosen_model"),
                                           ctx.shared.get("ollama_url"))
        return {"shared": {"config_path": path}}

    def wrote_paths(ctx):
        # The paths are in the run record; the owner is not shown a list of
        # files in the middle of the wizard.
        return {"note": "wrote " + ", ".join(
            str(path) for path in (ctx.shared.get("config_path"),
                                   ctx.shared.get("ha_path")) if path)}

    def summary(ctx):
        from puffpai import cli
        return cli._wizard_summary_step(ctx)

    def first_look(ctx):
        from puffpai.wizard import first_look as wizard_first_look
        return wizard_first_look.run_console(ctx)

    def needs(ctx):
        from puffpai.wizard import service as wizard_service
        return wizard_service.run_needs_console(ctx)

    def factory(ctx):
        from puffpai import cli
        from puffpai.wizard import factory as wizard_factory
        if not getattr(ctx, "console", False):
            return wizard_factory.apply_approved(ctx)
        result = cli._streamed_factory(ctx, wizard_factory.apply_approved)
        from puffpai.wizard import service as wizard_service
        wizard_service.waiting_console(ctx)
        return result

    def before_building(ctx):
        from puffpai import cli
        return cli._setup_toolsmith_model_step(ctx)

    def manifest(ctx):
        from puffpai import setup_ops
        _setup_manifest_step(ctx, non_interactive=ctx.non_interactive)
        if may_complete is None or (callable(may_complete) and may_complete()):
            setup_ops.mark_complete()
            return None
        return {"note": "the manifest is written; this build is not finished "
                        "while a step that blocks is still waiting for you"}

    parts = {"persist_config": persist_config, "wrote_paths": wrote_paths,
             "manifest": manifest, "summary": summary, "factory": factory,
             "first_look": first_look, "needs": needs,
             # Toolsmith's model, asked on the console just before the build
             # when the run has not settled it (a HomeBot run settles it to
             # this computer, so it asks nothing).
             "before_building": before_building}
    parts.update(homebot_preset.runner_parts())
    return parts


def _setup_manifest_step(ctx, *, non_interactive: bool = False) -> None:
    """Assemble, show and write the build manifest from what the cards wrote.

    This build's manifest, not BuildBot's: it carries ``household``, ``storage``
    and the shipped ``persona`` as well as the sections the twelve packages own.
    Everything is read out of the run's own sections and shared notebook, so a
    package that wrote nothing contributes nothing rather than a guess.
    """
    from puffpai import build_manifest, homebot_preset, setup_ops

    setup_cli.enter_step("The build manifest")
    sections = ctx._sections
    machine = sections.get("machine") or {}
    persona = sections.get("persona") or {}
    posture_section = sections.get("posture") or {}
    knowledge_section = sections.get("knowledge") or {}
    storage = ctx.shared.get("storage") or {}
    household = ctx.shared.get("household") or {}
    ha_block = ctx.shared.get("ha_block")

    defaults = setup_ops.HOMEBOT_DEFAULTS
    name = str(persona.get("bot_name") or "") or defaults["bot_name"]
    interview = persona.get("interview") or {}

    knowledge = {
        "zim_dir": str(knowledge_section.get("zim_dir")
                       or storage.get("knowledge_dir") or ""),
        "auto_index": bool(knowledge_section.get("auto_index", False)),
        "packs": list(knowledge_section.get("packs") or []),
    }
    knowledge.update(knowledge_section.get("block") or {})

    choices: dict[str, Any] = {
        "identity": {"identity": defaults["persona"], "name": name},
        "brain": (sections.get("brain") or {}).get("choice") or {},
        "crew": (sections.get("crew") or {}).get("sizing") or {},
        "knowledge": knowledge,
        "integrations": setup_ops.integrations_block(
            ha_block, media_library=bool(storage.get("media_dir"))),
    }
    if storage:
        choices["storage"] = setup_ops.storage_block(storage)
    if posture_section.get("posture") is not None:
        choices["posture"] = setup_ops.posture_block(posture_section["posture"])
    surfaces = (sections.get("surfaces") or {}).get("surfaces")
    if surfaces is None and homebot_preset.is_active(ctx.shared):
        # The preset asks no page question: every page is on, as the
        # browser's build records it.
        surfaces = homebot_preset.surfaces(ctx.args)
    if surfaces is not None:
        choices["surfaces"] = setup_ops.surfaces_block(surfaces)
    if posture_section.get("vault") is not None:
        choices["vault"] = posture_section["vault"]
    if posture_section.get("bridge") is not None:
        choices["bridge"] = posture_section["bridge"]
    if household:
        choices["household"] = setup_ops.household_block(household)

    # The persona this build ships, recorded as a fact about the build. A
    # persona the wizard SCAFFOLDED is recorded beside it, never instead of it:
    # this is still HomeBot, and the owner's own app is a second fact about the
    # same box.
    #
    # The GOAL goes in this block whether or not anything was scaffolded.
    # ``build_context.collect_goal`` reads ``persona.goal`` off the manifest, so
    # a block written only for a scaffolded build meant every ``--yes`` run told
    # its model the owner had written nothing down. One builder for that half
    # (``purpose_skill.recorded_persona``), so the card wizard and this step
    # cannot record the same run differently.
    from puffpai import homebot_preset
    from puffpai.wizard.packages.purpose import skill as purpose_skill

    persona_block: dict[str, Any] = {"id": defaults["persona"],
                                     "pack": defaults["pack"], "name": name}
    if homebot_preset.is_active(ctx.shared) and not interview.get("goal"):
        # A HomeBot build asks no goal: what it is for is the persona's own.
        interview = ctx.shared.get("interview") or interview
        persona_block = homebot_preset.persona_block(name)
    else:
        recorded = purpose_skill.recorded_persona(interview, name)
        if isinstance(recorded, dict):
            persona_block.update(recorded)
            persona_block["id"] = defaults["persona"]
            persona_block["pack"] = defaults["pack"]
    scaffolded = persona.get("scaffolded") or interview.get("persona")
    if scaffolded:
        persona_block["scaffolded"] = scaffolded
    choices["persona"] = persona_block
    choices["interview"] = {
        "goal": str(interview.get("goal") or ""),
        "matched": [str(m.get("id")) for m in (interview.get("flat") or [])
                    if isinstance(m, dict)],
    }

    preview = setup_ops.preview_manifest(machine.get("census") or {}, **choices)

    print()
    print()
    print("What was built")
    print("--------------")
    for line in build_manifest.manifest_summary_lines(preview["manifest"]):
        print(f"  {line}")
    diff = preview.get("diff") or []
    if diff and diff != ["New build manifest (no previous one to compare)."]:
        print("  Changes from your last run:")
        for line in diff:
            print(f"    {line}")
    for warning in preview.get("warnings") or []:
        print(f"  note: {warning}")

    health = setup_ops.config_health_status()
    if health.get("blocking"):
        print("  Some of this build's own configuration files could not be read:")
        for line in health.get("summary") or []:
            print(f"    {line}")
        if not non_interactive and not setup_cli._ask_yes_no(
                "  Record the build anyway?", True):
            print("No manifest was written. Your other settings are already "
                  "saved.")
            return

    if getattr(ctx.args, "dry_run", False):
        print("  --dry-run: nothing was written.")
        return
    path = setup_ops.commit_manifest(preview["manifest"])
    print(f"Wrote {path}")


# ---------------------------------------------------------------------------
# `puffpai setup`
# ---------------------------------------------------------------------------

def _cmd_setup(args: argparse.Namespace) -> int:
    """Run the wizard; never let an unexpected failure reach the owner raw.

    Exit codes: 0 finished (READY or NOT READY - a brainless build still ends 0,
    because the documented quickstart chains commands and a wizard that broke
    the chain would take the owner further from the fix), 1 a deliberate
    refusal, 2 a bug.
    """
    setup_cli._current_step = setup_cli.NO_STEP
    # One run, one stdin: the end-of-file latch is per-run state, not process
    # state, so a second wizard in the same process starts with its answers
    # unread.
    _reset_stdin_state()
    # One name, two spellings. The card packages read ``args.name`` and this
    # tree's own steps read ``args.bot_name``; the flag has one dest, so the
    # other spelling is mirrored here rather than each reader guessing.
    chosen_name = getattr(args, "bot_name", None) or getattr(args, "name", None)
    args.name = chosen_name
    args.bot_name = chosen_name
    try:
        return _run_setup(args)
    except SetupRefused as refusal:
        setup_cli._print_refusal(refusal)
        return 1
    except KeyboardInterrupt:
        print()
        print("Stopped. Nothing further was written; re-run `puffpai setup` "
              "when you are ready.")
        return 1
    except Exception as exc:  # noqa: BLE001 - this is the floor, it catches all
        setup_cli._print_crash(exc, setup_cli._crash_log_path(exc))
        return 2


def _run_setup(args: argparse.Namespace) -> int:
    """The first-run wizard: the runner's schedule, card by card.

    The owner sees the same wizard the linear ``setup_cli.run_setup`` walked -
    the same steps, in the same order, asking the same questions with the same
    defaults. What is new is underneath: one package owns each section of the
    record, every step's outcome is written down as it happens, and a step that
    fails records a gap instead of vanishing.

    **A stopped run is offered back, on its own screen.** See
    :func:`_offer_resume`.
    """
    from puffpai.config import get_config
    from puffpai.wizard import render_cli, runner as wizard_runner

    cfg = get_config()
    _print_setup_header()
    run = wizard_runner.Runner(
        args=args, cfg=cfg, non_interactive=bool(getattr(args, "yes", False)),
        schedule=setup_schedule(),
        runner_parts=_wizard_runner_parts(),
        # This console is the face a skill may show a value once through - a
        # recovery code, a setup code. It belongs to THIS run: nothing registers
        # it globally, so no other run and no later request can inherit it.
        show_once=render_cli.ConsoleShowOnce())
    _offer_resume(args, run)
    for refusal in run.refusals:
        print(f"note: a wizard package was not loaded - {refusal}")
    run.run()
    return 0


def setup_schedule() -> tuple:
    """The HomeBot setup schedule (:func:`puffpai.homebot_preset.schedule`).

    Check this computer, pick the thinking model (and offer its download), set
    up the vault, offer Home Assistant, then build. No goal is asked. The
    console and the browser (:mod:`puffpai.wizard.service`) both walk this
    schedule, so the two faces build the same thing.
    """
    from puffpai import homebot_preset
    return homebot_preset.schedule()


#: What ``puffpai setup --yes`` says when it finds a run somebody stopped. A
#: scripted run never resumes: nobody is there to be offered the choice, and
#: silently continuing would skip questions on the strength of a file.
RESUME_SET_ASIDE_LINE = (
    "A setup that stopped part-way was set aside. This run starts from the "
    "beginning.")

#: What it says about a record it will not read. Plain, and it does not guess
#: why: "not ours" is the whole of what this build knows.
RESUME_UNREADABLE_LINE = (
    "The record of an earlier setup could not be read, so it was set aside. "
    "This run starts from the beginning.")


def _offer_resume(args: argparse.Namespace, run) -> None:
    """One screen, before the first card: continue a stopped run, or start over.

    Three rules, each the answer to a way this could go wrong: ``--yes`` never
    resumes silently, continuing re-verifies what blocks (the vault on disk, the
    model in the Ollama), and starting over archives rather than deletes.
    """
    from puffpai.wizard import render_cli, runner as wizard_runner

    stopped, why = wizard_runner.find_stopped_run(args=args, cfg=run.cfg,
                                                  schedule=run.schedule)
    if stopped is None:
        if why == wizard_runner.RESUME_UNREADABLE:
            wizard_runner.archive_state(run.state_path())
            print(RESUME_UNREADABLE_LINE)
            print()
        return
    if bool(getattr(args, "yes", False)):
        wizard_runner.archive_state(run.state_path())
        print(RESUME_SET_ASIDE_LINE)
        print()
        return
    if render_cli.resume_screen(stopped.resume_summary()) != "continue":
        wizard_runner.archive_state(run.state_path())
        print("Starting over. The old record was kept beside this one.")
        print()
        return
    run.adopt(stopped)
    asked_again = run.reverify_blocking()
    if asked_again:
        print("Picking up where it stopped. These were checked against this "
              "machine and did not hold, so they are asked again:")
        for card_id in asked_again:
            print(f"  - {card_id}")
    else:
        print("Picking up where it stopped.")
    print()


def _print_setup_header() -> None:
    """The welcome banner, printed once before the first step either way."""
    from puffpai import __version__
    print("=" * 60)
    print(f"  Welcome to {DEFAULT_BOT_NAME} v{__version__}")
    print("  Privacy-first, local-first household AI setup wizard")
    print("=" * 60)
    print()


__all__ = [
    "DASHBOARD_START_COMMAND", "DASHBOARD_START_COMMAND_WINDOWS",
    "DASHBOARD_START_STEPS", "DEFAULT_BOT_NAME", "MAX_STATED_RAM_GB", "NO_STEP",
    "POSTURE_URL_RULES", "RESUME_SET_ASIDE_LINE", "RESUME_UNREADABLE_LINE",
    "SetupRefused", "VAULT_REJECTED_LINE", "bot_name", "current_step_label",
    "dashboard_start_command", "enter_step", "installed_launchers",
    "pair_code_command", "pairing_block_lines", "step_banner",
]
