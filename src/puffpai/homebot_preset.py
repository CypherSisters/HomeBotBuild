"""The HomeBot preset: what a HomeBot build is, decided in one place.

HomeBot asks no goal. Setup checks this computer, picks the thinking model that
fits it (and offers to download it when it is missing), sets up the Vault,
offers to connect Home Assistant, and builds the rest from what is written
here:

* every HomeBot page and capability on: the build is never trimmed to a goal;
* the five household apps on, as ``packs/homebot/pack.json`` lists them;
* the Bridge closed: the finished bot may use only the outside services the
  owner switches on, and none is switched on. Run-time reach is recorded as
  ``enumerated`` so the engine knows each switch exists and asks for it by
  name; no switch is opened by the build;
* Toolsmith on the model on this computer (the cloud model is added in
  Settings).

The console (``puffpai setup``) and the browser (``/wizard/*``) both walk
:func:`schedule` with the parts in :func:`runner_parts`, so both faces build the
same thing. The engine modules stay goal-agnostic: they read what this preset
seeds into the run, never the name of the build.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from typing import Any, Callable, Optional

#: The preset's id, recorded in the run's notebook as ``shared["preset"]``.
PRESET_ID = "homebot"

#: The shipped pack whose apps a HomeBot build turns on.
PACK_ID = "homebot"

#: The goal the engine is given at setup: none. With no goal the engine plans
#: nothing, finds nothing and installs nothing while the build runs; the owner's
#: own asks after setup (Add more, Find more, Toolsmith) are what it works on.
ENGINE_GOAL = ""

#: Run-time reach: only the outside services the owner switched on.
RUN_EGRESS = "enumerated"

#: The run notebook key saying the run-time reach is this preset's default
#: (nobody stated one), so the posture records it as defaulted.
RUN_EGRESS_DEFAULTED_KEY = "run_egress_defaulted"
#: The outside switches the build opens: none.
EGRESS_SWITCHES: tuple[str, ...] = ()

#: The Bridge a HomeBot build records: Moat. With every outside switch off,
#: nothing reaches the internet; the home network (Home Assistant included)
#: still works.
BRIDGE_POSTURE = "moat"

#: Toolsmith's model at setup. The cloud model is added later in Settings.
TOOLSMITH_MODEL = "local"

#: Schedule entries a HomeBot build does not run at setup. The goal, the name
#: and the age band are not asked; with no goal there is no needs step, no plan
#: to approve, nothing for the factory to make and no build plan to draw; the
#: build is not trimmed to a page list and gets no scaffolded place of its own.
DROPPED_ENTRIES = frozenset({
    "purpose.goal", "purpose.identity", "purpose.place",
    "runner.needs", "runner.summary", "surfaces.card",
    "runner.factory", "assemble.plan",
})

#: This build's own steps that ask nothing at setup: their defaults are taken,
#: and they are changed later on the Household and Settings pages.
SILENT_PARTS = ("storage", "household")

#: The run notebook keys this preset writes.
MODEL_DOWNLOAD_KEY = "model_download"
HOME_ASSISTANT_KEY = "home_assistant_answer"

#: The switch a model download needs.
MODEL_SWITCH = "model_pull"

#: Plain words for the setup questions this preset adds.
HA_TITLE = "Connect your Home Assistant"
HA_INTRO = ("Optional. If you run Home Assistant, HomeBot can work your lights, "
            "switches and sensors through it. Press Enter to skip; you can "
            "connect it later on the Smart Home page or in Settings.")
HA_TOKEN_HELP = ("To make a token: open Home Assistant, click your name at the "
                 "bottom left, open the Security tab, and under Long-lived "
                 "access tokens click Create token. Copy it and paste it here.")
MODEL_SWITCH_LINE = ("Downloading it switches on one outside service, Model "
                     "downloads, for this download only. It goes back off when "
                     "the download ends. Nothing else is switched on.")


def purpose() -> str:
    """What a HomeBot build is for, as recorded in its manifest."""
    from puffpai import setup_ops
    return str(setup_ops.HOMEBOT_DEFAULTS["goal"])


def pack_apps() -> list[str]:
    """The app ids ``packs/homebot/pack.json`` lists. Empty when unreadable."""
    try:
        from puffpai import app_registry
        path = os.path.join(app_registry._packs_dir(), PACK_ID, "pack.json")  # noqa: SLF001
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, AttributeError):
        return []
    apps = data.get("apps") if isinstance(data, dict) else None
    out: list[str] = []
    for entry in apps or ():
        app_id = entry.get("id") if isinstance(entry, dict) else entry
        if isinstance(app_id, str) and app_id:
            out.append(app_id)
    return out


#: Names a build ships with before anybody names it.
_SHIPPED_NAMES = frozenset({"", "puffpai", "buildbot"})


def bot_name() -> str:
    """The name this build already answers to, or HomeBot when it still has
    the name it shipped with."""
    from puffpai.wizard import host
    name = host.bot_name(host.DEFAULT_BOT_NAME)
    return host.DEFAULT_BOT_NAME if name.strip().lower() in _SHIPPED_NAMES \
        else name


def interview() -> dict[str, Any]:
    """The interview a HomeBot build records: nothing asked, no goal."""
    return {"goal": ENGINE_GOAL, "selected": [], "proposal": None,
            "persona": None}


def is_active(shared: Any) -> bool:
    """True for a run this preset seeded."""
    return isinstance(shared, dict) and shared.get("preset") == PRESET_ID


# ---------------------------------------------------------------------------
# the schedule
# ---------------------------------------------------------------------------

def schedule(base: Optional[tuple] = None) -> tuple:
    """The setup schedule: the engine's own, without the goal, plus this build's
    steps, each where it belongs.

    * ``runner.preset`` first: it seeds the run (no goal, the name, the reach).
    * ``runner.model_offer`` after the first screen: the model download is
      offered while the owner is looking at the model.
    * ``runner.home_assistant``, ``runner.storage`` and ``runner.household``
      after the vault, before the build starts.
    * ``runner.model_download`` before the Brain card, so the Brain card finds
      the model on this computer when the owner said yes to it.
    """
    from puffpai.wizard import runner as wizard_runner

    steps: list = [wizard_runner.Step("runner.preset", "", "preset")]
    for step in (base if base is not None else wizard_runner.SCHEDULE):
        if step.entry in DROPPED_ENTRIES:
            continue
        if step.entry == "brain.card":
            steps.append(wizard_runner.Step("runner.model_download", "",
                                            "model_download"))
        steps.append(step)
        if step.entry == "runner.first_look":
            steps.append(wizard_runner.Step("runner.model_offer", "",
                                            "model_offer", ("census",)))
        elif step.entry == "secure.vault":
            steps.append(wizard_runner.Step("runner.home_assistant", "",
                                            "home_assistant"))
            steps.append(wizard_runner.Step("runner.storage", "", "storage"))
            steps.append(wizard_runner.Step("runner.household", "",
                                            "household"))
    return tuple(steps)


# ---------------------------------------------------------------------------
# the parts
# ---------------------------------------------------------------------------

def seed(ctx) -> dict:
    """The ``runner.preset`` entry: what the run knows before anything is asked.

    Writes the empty interview the cards read, the name, Toolsmith's model and
    the run-time reach. The reach goes on the run's arguments, where the cards
    read it; a reach the command line stated wins. A reach this preset put
    there is the build's default, never the owner's statement, and the posture
    records it as defaulted (``RUN_EGRESS_DEFAULTED_KEY``).
    """
    args = ctx.args
    use_household_apps()
    reach_defaulted = getattr(args, "run_egress", None) is None
    if reach_defaulted:
        setattr(args, "run_egress", RUN_EGRESS)
    if getattr(args, "egress_door", None) is None:
        setattr(args, "egress_door", list(EGRESS_SWITCHES))
    name = bot_name()
    if not getattr(args, "dry_run", False):
        from puffpai.wizard import host
        if host.bot_name("").strip().lower() in _SHIPPED_NAMES:
            from puffpai import setup_ops
            setup_ops.set_identity(name)
    shared = {
        "preset": PRESET_ID,
        "interview": interview(),
        "bot_name": name,
        "wording": "standard",
        "toolsmith_asked": TOOLSMITH_MODEL,
        # What the build is for, read by the cards that show it, and the note
        # that every outside service being off is this build, not a gap.
        "stated_purpose": purpose(),
        "switches_off_by_design": True,
        RUN_EGRESS_DEFAULTED_KEY: reach_defaulted,
    }
    return {"shared": shared, "note": "HomeBot build: no goal, nothing to plan"}


def _picked_model(ctx) -> tuple[str, dict]:
    """The model the first screen picked (or recommends) and that screen."""
    from puffpai.wizard import first_look as wizard_first_look

    shown = wizard_first_look.screen(args=ctx.args, cfg=ctx.cfg,
                                     shared=ctx.shared, sections=ctx._sections,  # noqa: SLF001
                                     non_interactive=bool(ctx.non_interactive))
    chosen = (ctx.shared.get(wizard_first_look.KEY) or {}).get("model")
    model = str(chosen or (shown.get("recommend") or {}).get("value") or "")
    return model, shown


def _installed(model: str, installed: Any) -> bool:
    names = {str(m) for m in (installed or ())}
    return bool(model) and (model in names or f"{model}:latest" in names
                            or model.removesuffix(":latest") in names)


def model_question(shared: dict, shown: dict, model: str) -> dict:
    """What the download offer says, for both faces. ``needed`` False: nothing
    to offer (the model is here, or nothing fits this computer)."""
    choice = next((c for c in shown.get("choices") or ()
                   if c.get("value") == model), {})
    needed = bool(model) and not _installed(model, shared.get("installed_models"))
    size = choice.get("size_bytes")
    label = str(choice.get("name") or model)
    from puffpai.wizard import cards
    size_words = cards.format_bytes(int(size)) if size else "a large"
    return {
        "model": model,
        "label": label,
        "needed": needed,
        "size_bytes": size,
        "line": (f"{label} is not on this computer yet. It is a {size_words} "
                 f"download." if size else
                 f"{label} is not on this computer yet."),
        "switch_line": MODEL_SWITCH_LINE,
        "switch": MODEL_SWITCH,
        "question": "Download it when the build starts?",
        "later": ("Without it, HomeBot cannot answer yet. The last screen of "
                  "setup shows how to download it later."),
    }


def model_offer(ctx) -> dict:
    """The ``runner.model_offer`` entry: offer the picked model's download.

    The owner's yes is recorded here and the download runs at
    ``runner.model_download``, after the vault. ``--yes`` downloads only with
    ``--download-model``. The browser records the same answer through
    ``POST /wizard/model-download``.
    """
    from puffpai.wizard import host

    recorded = ctx.shared.get(MODEL_DOWNLOAD_KEY)
    if isinstance(recorded, dict) and "wanted" in recorded:
        return {"note": "the download question was already answered"}
    model, shown = _picked_model(ctx)
    words = model_question(ctx.shared, shown, model)
    if not words["needed"]:
        return {"shared": {MODEL_DOWNLOAD_KEY: {"model": model, "needed": False,
                                                "wanted": False}},
                "note": "the model is on this computer" if model
                        else "no model fits this computer"}
    if not getattr(ctx, "console", False):
        # The browser asks this itself and records the answer.
        return {"note": "waiting for the owner's answer in the browser"}
    if ctx.non_interactive:
        wanted = bool(getattr(ctx.args, "download_model", False)
                      or getattr(ctx.args, "pull", False))
    else:
        import textwrap
        print()
        for text in (words["line"], words["switch_line"]):
            for line in textwrap.wrap(text, 76):
                print(f"  {line}")
        answer = host._read_line(f"  {words['question']} [y/N] ")
        wanted = (answer or "").strip().lower() in ("y", "yes")
        if not wanted:
            for line in textwrap.wrap(words["later"], 76):
                print(f"  {line}")
    return {"shared": {MODEL_DOWNLOAD_KEY: {"model": model, "needed": True,
                                            "wanted": bool(wanted)}},
            "note": "download wanted" if wanted else "download not wanted"}


def record_model_answer(shared: dict, shown: dict, wanted: bool) -> dict:
    """The browser's answer to the download offer, in the console's shape."""
    from puffpai.wizard import first_look as wizard_first_look
    chosen = (shared.get(wizard_first_look.KEY) or {}).get("model")
    model = str(chosen or (shown.get("recommend") or {}).get("value") or "")
    words = model_question(shared, shown, model)
    entry = {"model": model, "needed": words["needed"],
             "wanted": bool(wanted) and words["needed"]}
    shared[MODEL_DOWNLOAD_KEY] = entry
    return entry


def pull_with_switch(model: str, url: str,
                     say: Callable[[str], None] = lambda _line: None) -> dict:
    """Download *model* into the Ollama at *url* with the Model downloads
    switch on for this download only.

    The caller has the owner's yes for this model (the setup screen that named
    it, or the Download button). The switch goes back off when the download
    ends unless it was already on. Returns ``{"ok": bool, "message": str}``.
    """
    from puffpai import brain_health

    try:
        from puffpai.security import egress_admin, egress_doors
        was_open, _why = egress_doors.egress_allowed(MODEL_SWITCH)
        if not was_open:
            egress_admin.set_door_open(MODEL_SWITCH, True)
    except Exception as exc:  # noqa: BLE001 - a switch that will not move is said plainly
        message = (f"The Model downloads switch could not be turned on "
                   f"({getattr(exc, 'reason', type(exc).__name__)}). Nothing was "
                   f"downloaded.")
        say(f"  {message}")
        say(f"  You can download it later: {brain_health.pull_command(model, url)}")
        return {"ok": False, "message": message}
    try:
        say(f"  Downloading {model}. This can take a while.")
        result = brain_health.pull_model(model, url=url,
                                         on_progress=lambda line: say(f"    {line}"))
    finally:
        if not was_open:
            try:
                egress_admin.set_door_open(MODEL_SWITCH, False)
            except Exception:  # noqa: BLE001 - reported below, never raised
                say("  The Model downloads switch could not be turned back "
                    "off. Turn it off on the Bridge page.")
    ok = bool(result.get("ok"))
    message = str(result.get("message") or ("Downloaded." if ok else "Not downloaded."))
    say(f"  {message}")
    return {"ok": ok, "message": message}


def model_download(ctx) -> dict:
    """The ``runner.model_download`` entry: download the model the owner said
    yes to, with its switch on for this download only (:func:`pull_with_switch`).
    """
    from puffpai import brain_health

    answer = ctx.shared.get(MODEL_DOWNLOAD_KEY)
    if not isinstance(answer, dict) or not answer.get("wanted"):
        return {"note": "no download was asked for"}
    model = str(answer.get("model") or "")
    url = str(ctx.shared.get("ollama_url") or brain_health.DEFAULT_URL)
    say = print if getattr(ctx, "console", False) else (lambda _line: None)
    if not model:
        return {"note": "no model to download"}
    ok = pull_with_switch(model, url, say)["ok"]
    out: dict = {"note": "model downloaded" if ok else "model download failed"}
    if ok:
        installed = list(ctx.shared.get("installed_models") or [])
        if model not in installed:
            installed.append(model)
        out["shared"] = {"installed_models": installed}
    return out


def home_assistant(ctx) -> dict:
    """The ``runner.home_assistant`` entry: optional, and skipped with Enter.

    The console asks it (:func:`puffpai.setup_cli._step_ha`); the browser
    connects through ``/setup/ha`` and ``/setup/ha/test`` and this entry then
    records what is configured.
    """
    from puffpai import setup_cli, setup_ops

    if getattr(ctx, "console", False):
        block, present = setup_cli._step_ha(ctx.args, setup_ops,
                                            non_interactive=ctx.non_interactive)
    else:
        present = setup_ops.ha_configured()
        block = setup_ops.ha_block(present, setup_ops.ha_url(),
                                   token_configured=present)
    return {"shared": {"ha_block": block, "ha_present": bool(present)}}


def _silent(step, ctx) -> Any:
    """Run one of this build's steps on its defaults, printing nothing."""
    from puffpai import setup_ops
    with contextlib.redirect_stdout(io.StringIO()):
        return step(ctx.args, setup_ops, non_interactive=True)


def storage(ctx) -> dict:
    """The ``runner.storage`` entry: the default folders, asked nothing."""
    from puffpai import setup_cli
    return {"shared": {"storage": _silent(setup_cli._step_storage, ctx)}}


def household(ctx) -> dict:
    """The ``runner.household`` entry: the owner's profile, asked nothing.
    Names are set on the Household page."""
    from puffpai import setup_cli
    return {"shared": {"household": _silent(setup_cli._step_household, ctx)}}


def runner_parts() -> dict:
    """This preset's entries, by part name, for the runner."""
    return {"preset": seed, "model_offer": model_offer,
            "model_download": model_download,
            "home_assistant": home_assistant, "storage": storage,
            "household": household}


# ---------------------------------------------------------------------------
# what the manifest records
# ---------------------------------------------------------------------------

def persona_block(name: str) -> dict[str, Any]:
    """The manifest's persona block for a HomeBot build."""
    from puffpai import setup_ops
    defaults = setup_ops.HOMEBOT_DEFAULTS
    return {"id": defaults["persona"], "pack": defaults["pack"],
            "name": name or defaults["bot_name"], "goal": purpose(),
            "scaffolded": False}


def surfaces(args: Any = None) -> dict[str, Any]:
    """Every surface this build knows, switched on: the allowlist a HomeBot
    build records, the same one the browser's build sends to
    ``/setup/surfaces`` (every id in ``available``). A page the owner dropped
    on the command line (``--drop-surface``) stays dropped."""
    from puffpai import build_interview, setup_ops
    derivation = setup_ops.derive_surfaces({"goal": "", "flat": []})
    add = sorted(build_interview.surface_taxonomy())
    add += [str(s) for s in (getattr(args, "add_surface", None) or [])]
    drop = [str(s) for s in (getattr(args, "drop_surface", None) or [])]
    return setup_ops.apply_surface_choices(derivation, add=add, remove=drop)


def first_apps_on() -> list[str]:
    """The household apps a HomeBot build has on before the owner changes any."""
    return pack_apps()


def use_household_apps() -> None:
    """A HomeBot bot with no first-apps file yet starts with its household
    apps on (:func:`puffpai.first_apps.set_default_on`). Called by the setup
    run (:func:`seed`) and the backend at start."""
    from puffpai import first_apps
    first_apps.set_default_on(first_apps_on)


# ---------------------------------------------------------------------------
# one whole build, for tests and scripts
# ---------------------------------------------------------------------------

def run_build(*, data_dir: Optional[str] = None,
              state_dir: Optional[str] = None,
              vault_pin: Optional[str] = None) -> dict:
    """Run the whole HomeBot setup once, unattended, and return its record.

    The same schedule and parts ``puffpai setup --yes`` runs: no question is
    asked, nothing is downloaded (no ``--download-model``) and no outside
    switch is turned on. *data_dir* and *state_dir*, when given, are where it
    builds (``PUFFPAI_DATA_DIR`` / ``PUFFPAI_STATE_DIR`` for the call, and the
    config is reloaded before and after). *vault_pin* creates the vault; without
    it the vault is left for later.

    Returns ``{"entries": {...}, "gaps": [...], "manifest": {...}}``.
    """
    import argparse

    from puffpai import build_manifest, config as config_mod
    from puffpai.wizard import host, render_cli, runner as wizard_runner

    saved = {key: os.environ.get(key)
             for key in ("PUFFPAI_DATA_DIR", "PUFFPAI_STATE_DIR")}
    if data_dir:
        os.environ["PUFFPAI_DATA_DIR"] = data_dir
    if state_dir:
        os.environ["PUFFPAI_STATE_DIR"] = state_dir
    try:
        cfg = config_mod.get_config(_force_reload=True)
        parser = argparse.ArgumentParser(prog="puffpai")
        sub = parser.add_subparsers(dest="command")
        from puffpai import cli
        cli._setup_arguments(sub.add_parser("setup"))  # noqa: SLF001
        argv = ["setup", "--yes"]
        if vault_pin:
            argv += ["--vault-pin", vault_pin]
        else:
            argv += ["--skip-vault"]
        args = parser.parse_args(argv)
        args.name = args.bot_name = getattr(args, "bot_name", None)
        run = wizard_runner.Runner(
            args=args, cfg=cfg, non_interactive=True,
            schedule=host.setup_schedule(),
            runner_parts=host._wizard_runner_parts(),  # noqa: SLF001
            show_once=render_cli.ConsoleShowOnce())
        with contextlib.redirect_stdout(io.StringIO()):
            host._reset_stdin_state()  # noqa: SLF001
            run.run()
        return {"entries": dict(run.entries), "gaps": list(run.gaps),
                "manifest": build_manifest.read_manifest() or {}}
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            config_mod.get_config(_force_reload=True)
        except Exception:  # noqa: BLE001 - the caller's config is theirs to fix
            pass


__all__ = [
    "BRIDGE_POSTURE", "DROPPED_ENTRIES", "EGRESS_SWITCHES", "ENGINE_GOAL", "HA_INTRO", "HA_TITLE",
    "HA_TOKEN_HELP", "MODEL_DOWNLOAD_KEY", "PACK_ID", "PRESET_ID", "RUN_EGRESS",
    "RUN_EGRESS_DEFAULTED_KEY",
    "TOOLSMITH_MODEL", "first_apps_on", "interview", "is_active",
    "use_household_apps",
    "model_question", "pack_apps", "persona_block", "pull_with_switch", "purpose",
    "record_model_answer", "run_build", "runner_parts", "schedule",
    "surfaces",
]
