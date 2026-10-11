"""The wizard behind a face that is not a console.

The dashboard is a renderer of the same cards the CLI draws. That
needs three answers from the backend and nothing else -- what the cards are,
what changed when the owner sent answers back, and what the run log says. This
module is those three, and it is deliberately thin: every rule (one writer per
section, reserved sections, gap records, the log) still lives in the runner,
so a card reaching a browser cannot mean a different wizard.

**Proposing is read-only.** :func:`propose_cards` runs each converted skill's
``propose`` and writes nothing -- no run state, no config, no manifest. It is
safe to call on every page load, and a skill whose propose raises is reported
as a refusal rather than taking the page down.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import errno
import json
import os
import time
import types
from typing import Any

from puffpai.wizard import cards, loader, runner as wizard_runner, stale

#: The name of the file that says a build is running. One run at a time, and
#: the reason is not tidiness: two runs applying the same cards would both write
#: the same sections and both write the same run record, so the record would
#: describe neither of them.
LOCK_NAME = "wizard_run.lock"

#: How long a lock left behind by a process that died is honoured. Under it, a
#: second run is refused (fail closed); over it, the lock is taken -- otherwise
#: one crash would wedge the wizard for good with no way out but a file
#: deletion nobody would think to do.
LOCK_TTL_S = 900


class SkillNotConverted(LookupError):
    """Asked for a skill that has no card."""


class RunInProgress(RuntimeError):
    """A build is already running. Two at once would write over each other."""


class AnswerRefused(cards.CardError):
    """An answer this build will not apply, and which field it was about.

    The message is :class:`~puffpai.wizard.cards.CardError`'s, unchanged -- it
    was written to be shown to a person. ``field`` is the id the message names
    when it names a known one, so a browser can put it beside the field instead
    of at the bottom of the card.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def _args(extra: dict | None = None) -> types.SimpleNamespace:
    """The flags a step reads, with nothing set: the browser passes answers.

    Everything the steps read is read with ``getattr(args, name, None)``, so an
    empty namespace means "nothing was stated on a command line", which is
    exactly true of a request that arrived over HTTP.
    """
    return types.SimpleNamespace(**(extra or {}))


def _cfg():
    from puffpai.config import get_config
    return get_config()


def _lifecycle_packages() -> tuple[dict[str, loader.Package], list[str]]:
    packages, refusals = loader.load_builtin_packages()
    return packages, refusals


def has_lifecycle(package: loader.Package) -> bool:
    """True when this package is converted: propose, apply and verify."""
    try:
        module = loader.load_module(package)
    except Exception:
        return False
    return all(callable(getattr(module, name, None))
               for name in ("propose", "apply", "verify"))


def parts_of(package: loader.Package) -> tuple[str, ...]:
    """The questions this package asks: ``("",)`` for one card, else its names.

    A package declares them with a module-level ``PARTS`` tuple (Secure:
    posture, vault, bridge). Nothing is inferred -- a package that says nothing
    has exactly one card.
    """
    try:
        module = loader.load_module(package)
    except Exception:
        return ("",)
    declared = getattr(module, "PARTS", None)
    if not isinstance(declared, (list, tuple)) or not declared:
        return ("",)
    names = tuple(str(name) for name in declared
                  if isinstance(name, str) and cards.ID_RE.match(name))
    return names or ("",)


def _context(package: loader.Package, sections: dict, shared: dict,
             args, cfg, part: str = "") -> wizard_runner.RunContext:
    """A context for one propose. **No show-once face, ever.**

    Nothing reached through this module has a screen it can put a value on
    exactly once: the caller is an HTTP request, and a browser response is not
    that screen. So the vault card refuses to create a vault from here and says
    where the PIN can be set instead, and Handoff shows no pairing
    code. This is a fail-closed default, not an oversight.
    """
    return wizard_runner.RunContext(
        args=args, cfg=cfg, non_interactive=True, shared=shared,
        sections=sections, package=package, part=part, show_once=None,
        remote=True)


def _saved(cfg, args) -> wizard_runner.Runner:
    """A runner holding the last run's record, if there is one to hold."""
    run = wizard_runner.Runner(args=args, cfg=cfg, non_interactive=True,
                               remote=True)
    try:
        run.load_state()
    except Exception:  # pragma: no cover - load_state does not raise
        pass
    return run


def _scheduled_ids() -> list[str]:
    """Every card id the schedule runs, in schedule order.

    The schedule is the source of truth for order and for what exists, so the
    dashboard and the CLI cannot end up with two different lists of rows --
    which is the drift (fourteen steps against eight) this whole port is
    against.
    """
    out: list[str] = []
    for step in wizard_runner.SCHEDULE:
        part = wizard_runner.lifecycle_part(step.part)
        if part is None or not step.skill:
            continue
        out.append(f"{step.skill}:{part}" if part else step.skill)
    return out


def _settled(run: wizard_runner.Runner, skill_id: str, part: str) -> bool:
    """Has the owner already applied this card in this build?"""
    step = _step_for(skill_id, part)
    outcome = run.entries.get(step.entry, {}).get("outcome")
    return outcome in ("done", "skipped", "not_built")


def propose_cards(args=None, cfg=None) -> dict:
    """Every card the schedule runs, in schedule order. Writes nothing.

    ``order`` carries the ids explicitly -- a JSON array's order is a promise
    and a JSON object's key order is not -- and ``not_converted`` names the
    skills that have no card at all, so a row nobody can see is not how an
    owner finds out a decision was made for them.

    **Later cards see what earlier cards propose.** A run applies as it goes, so
    the console wizard's Brain step reads the address Discover *wrote*. Nothing
    is applied here, so without help every card proposed against an empty
    notebook: on a fresh build Discover said "Ollama at 127.0.0.1:11435" and the
    Brain row beside it still sized a download against the default address, and
    Crew said "room for up to 0 specialists" because no census had been shared.
    So a *provisional* notebook is threaded through the proposes: each card that
    is not yet applied contributes its ``shared_preview`` -- what its apply would
    put in ``shared`` if this proposal were accepted (CONVERTING.md) -- and the
    later proposes read it. It is a local dict: the run's own notebook, the run
    state and the config are untouched, so this stays safe to call on every page
    load. A card the owner has already applied contributes nothing, because the
    notebook loaded from the run state already holds what it really wrote.
    """
    args = args or _args()
    cfg = cfg or _cfg()
    run = _saved(cfg, args)
    packages, refusals = _lifecycle_packages()

    out: list[dict] = []
    order: list[str] = []
    applied: list[str] = []
    provisional = dict(run.shared)
    for card_id in _scheduled_ids():
        skill_id, part = cards.split_card_id(card_id)
        package = packages.get(skill_id)
        if package is None or not has_lifecycle(package):
            continue
        settled = _settled(run, skill_id, part)
        try:
            card, proposal = _propose(package, run, args, cfg, part=part,
                                      shared=provisional)
        except Exception as exc:
            refusals.append(
                f"The '{card_id}' step could not say what it found: "
                f"{type(exc).__name__}: {exc}")
            continue
        if not settled:
            preview = proposal.get("shared_preview")
            if isinstance(preview, dict):
                provisional.update(
                    {key: value for key, value in preview.items()
                     if isinstance(key, str)})
        out.append(card)
        order.append(cards.card_id(card))
        if settled:
            applied.append(cards.card_id(card))

    converted = {cards.split_card_id(cid)[0] for cid in _scheduled_ids()}
    not_converted = [
        {"skill": skill_id,
         "title": cards.plain(
             getattr(packages.get(skill_id), "label", "") or skill_id,
             cards.MAX_TITLE)}
        for skill_id in wizard_runner.SKILL_ORDER if skill_id not in converted]
    return {"cards": out, "order": order, "not_converted": not_converted,
            "refusals": refusals, "applied": applied,
            "total": cards.total_estimate(out),
            "stale": list(run.stale)}


def propose_one(package: loader.Package, run: wizard_runner.Runner,
                args, cfg, part: str = "") -> dict:
    """One skill's card, validated. Raises if it will not validate."""
    return _propose(package, run, args, cfg, part=part)[0]


def _propose(package: loader.Package, run: wizard_runner.Runner, args, cfg,
             part: str = "", shared: dict | None = None) -> tuple[dict, dict]:
    """The card and the whole proposal behind it.

    *shared* replaces the run's notebook for this one propose, which is how
    :func:`propose_cards` shows a card what the cards above it would share.
    None means the run's own notebook, unchanged.
    """
    module = loader.load_module(package)
    notebook = run.shared if shared is None else shared
    context = _context(package, run.sections, notebook, args, cfg, part)
    proposal = module.propose(context) or {}
    card = cards.validate_card(proposal.get("card"))
    if part and card["part"] != part:
        raise cards.CardError(
            f"'{package.id}' was asked for its '{part}' card and proposed "
            f"'{card['part'] or 'no part'}'.")
    return card, proposal


def _resolve(card_id: Any) -> tuple[loader.Package, str, str]:
    """A card id off the wire into (package, skill, part), or a refusal."""
    skill, part = cards.split_card_id(card_id)
    if not skill:
        raise SkillNotConverted(
            "Name the step to apply, as its id from /wizard/cards.")
    packages, _ = _lifecycle_packages()
    package = packages.get(skill)
    if package is None or not has_lifecycle(package):
        raise SkillNotConverted(
            f"'{skill}' is not a step this build can show a card for yet.")
    known = parts_of(package)
    if part and part not in known:
        raise SkillNotConverted(
            f"'{skill}' has no '{part}' card.")
    if not part and known != ("",):
        # A multi-question package named without a part would be whichever
        # question the run happened to be up to. Asking the caller to say which
        # is the only answer that cannot silently apply the wrong one.
        raise SkillNotConverted(
            f"'{skill}' asks more than one question; name which "
            f"({', '.join(f'{skill}:{name}' for name in known)}).")
    return package, skill, part


def _step_for(skill: str, part: str, *,
              requires: bool = True) -> wizard_runner.Step:
    """The schedule's own entry for this card, so a re-apply is the same entry.

    Falling back to a synthesised entry would give the same card two entry ids
    -- one from the wizard and one from the dashboard -- and the run record
    would then say a step both ran and did not.

    ``requires=False`` drops the entry's dependency list, and only
    :func:`apply_skill` uses it. Re-running **one** card is how an owner
    changes anything, and the run's shared notebook is a property of
    a whole wizard run, not of the build: gating a single re-apply on it would
    mean the dashboard could never change a step until it had walked the whole
    wizard again. A skill's own propose reads its section for what it needs.
    """
    want = f"{wizard_runner.V2_PART}:{part}" if part else wizard_runner.V2_PART
    for step in wizard_runner.SCHEDULE:
        if step.skill == skill and step.part == want:
            return step if requires else wizard_runner.Step(
                step.entry, step.skill, step.part)
    return wizard_runner.Step(f"{skill}.{part or wizard_runner.V2_PART}",
                              skill, want)


def apply_skill(skill: str, answers: Any, args=None, cfg=None) -> dict:
    """Validate, apply, verify, record -- through the runner, not around it.

    The answers are checked against a freshly proposed card before the entry
    runs, so a bad answer is a refusal the caller can act on rather than a gap
    recorded in the owner's build. The runner then proposes again on its own,
    which is cheap by construction (propose is read-only) and is the
    correct behaviour anyway: what is applied is checked against the machine as
    it is now, not as it was when a browser tab was opened.
    """
    args = args or _args()
    cfg = cfg or _cfg()
    package, skill_id, part = _resolve(skill)

    run = _saved(cfg, args)
    card = propose_one(package, run, args, cfg, part=part)
    try:
        clean = cards.validate_answers(card, answers)
    except cards.CardError as exc:
        raise AnswerRefused(
            str(exc),
            cards.field_for_error(str(exc),
                                  [f["id"] for f in card["fields"]])) from None

    outcome = _apply_now(run, clean, skill_id, part, requires=False)
    updated = propose_one(package, run, args, cfg, part=part)
    row = run.log[-1] if run.log else {}
    return {
        "skill": cards.card_id(card),
        "outcome": outcome,
        "card": updated,
        "changed": cards.redact_answers(card, clean),
        "stale": stale.stale_for_answers(card, clean),
        "verified": row.get("verified") or {},
        "gaps": [gap for gap in run.gaps if gap.get("skill") == skill_id],
    }


def asks_for_secret(card_id: Any, answers: Any) -> bool:
    """Do these answers fill a ``secret_entry`` field of that card?

    Used by the API's attempt limiter, so it never raises: anything it cannot
    work out counts as a secret, because the failure this protects is a caller
    grinding at a PIN and the cheap answer to "I do not know" is "count it".
    """
    if not isinstance(answers, dict) or not answers:
        return False
    try:
        package, _skill, part = _resolve(card_id)
        cfg, args = _cfg(), _args()
        card = propose_one(package, _saved(cfg, args), args, cfg, part=part)
    except Exception:  # noqa: BLE001 - a limiter never takes a route down
        return True
    secret_ids = {field["id"] for field in card["fields"]
                  if field["kind"] == "secret_entry"}
    return bool(secret_ids & set(answers))


def _apply_now(run: wizard_runner.Runner, clean: dict, skill_id: str,
               part: str, *, requires: bool) -> str:
    """Run one entry with the answers already validated. No face, by design."""
    run.renderer = lambda shown, **kwargs: clean
    step = _step_for(skill_id, part, requires=requires)
    run.entries.pop(step.entry, None)
    return run.run_step(step)


# -- accept the rest ----------------------------------------------------------

def _lock_path(cfg) -> str:
    config_dir = getattr(cfg, "CONFIG_DIR", None) or os.getcwd()
    return os.path.join(config_dir, LOCK_NAME)


def _take_lock(cfg) -> str:
    """Claim the one-run-at-a-time lock, or raise :class:`RunInProgress`."""
    path = _lock_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in (1, 2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                age = LOCK_TTL_S + 1
            if attempt == 1 and age > LOCK_TTL_S:
                # A run that died leaves this behind. Honoured while it could
                # still be a live run, taken once it cannot be -- the
                # alternative is a wizard nobody can start again.
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            raise RunInProgress(
                "A build is already running. Wait for it to finish, or "
                "re-open this page in a few minutes.") from None
        else:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump({"pid": os.getpid(), "started_at": time.time()}, out)
            return path
    raise RunInProgress("A build is already running.")


def _drop_lock(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def run_rest(acknowledged: Any = (), args=None, cfg=None) -> dict:
    """Apply, in schedule order, every card the owner has not answered.

    Three things are never accepted on the owner's behalf, and each is a
    refusal rather than a silence on the row:

    * a **consequential** card -- a heavy download, runtime egress, a vault PIN
      still unset, a destructive re-run -- unless its id is in *acknowledged*,
      which is the browser saying the owner opened that row and read it;
    * a card carrying a **secret_entry**, because nobody typed the secret and
      applying it would record a decision the owner did not make.

    What is **not** refused, and was: the wizard's own entries -- the Ollama
    config, the build manifest, Prove's static checks. They ask the owner
    nothing, so there is no consent to be missing, and skipping them was how a
    build made entirely in a browser ended with no manifest and an unprovable
    bot. They run at their scheduled positions, only when their inputs exist,
    with their output captured and no show-once face
    (:func:`_run_support_step`). First-run setup is **not** marked complete
    while a card that blocks is still waiting: the manifest records what was
    decided, and ``complete``/``outstanding`` in the reply say what is not.

    ``acknowledged`` is checked against the real card set: an id this build
    does not have is a refusal, not an ignored string, because an unchecked id
    list is a list a caller can pad until something matches.
    """
    args = args or _args()
    cfg = cfg or _cfg()
    known = set(_scheduled_ids())
    seen: list[str] = []
    for value in (acknowledged or ()):
        card_id = value if isinstance(value, str) else ""
        if card_id not in known:
            raise SkillNotConverted(
                f"{card_id or value!r} is not a step in this build, so it is "
                f"not something to acknowledge.")
        seen.append(card_id)
    accepted = set(seen)

    lock = _take_lock(cfg)
    try:
        return _run_rest_locked(accepted, args, cfg)
    finally:
        _drop_lock(lock)


def _run_rest_locked(accepted: set[str], args, cfg) -> dict:
    packages, refusals = _lifecycle_packages()
    run = _saved(cfg, args)
    # A renderer, so ``Runner.show_once()`` answers None: nothing reached from
    # here has a screen a value could be shown on exactly once, and a skill that
    # needs one declines rather than creating a secret nobody will ever see.
    run.renderer = lambda card, **kwargs: {}
    results: list[dict] = []
    #: Cards that block and were not settled. While this is non-empty the build
    #: is not finished, whatever else ran.
    outstanding: list[str] = []
    run.runner_parts = _support_parts(lambda: not outstanding)

    for step in wizard_runner.SCHEDULE:
        part = wizard_runner.lifecycle_part(step.part)
        if part is None or not step.skill:
            results.append(_run_support_step(run, step))
            continue
        card_id = f"{step.skill}:{part}" if part else step.skill
        skill_id = step.skill
        package = packages.get(skill_id)
        if package is None or not has_lifecycle(package):
            continue
        done = run.entries.get(step.entry, {}).get("outcome")
        if done in ("done", "skipped", "not_built"):
            results.append({"card": card_id, "outcome": done,
                            "applied": False,
                            "why": "the owner already settled this one"})
            continue
        try:
            card = propose_one(package, run, args, cfg, part=part)
        except Exception as exc:
            results.append({"card": card_id, "outcome": "failed",
                            "applied": False,
                            "why": f"{type(exc).__name__}: {exc}"})
            refusals.append(f"The '{card_id}' step could not propose: "
                            f"{type(exc).__name__}: {exc}")
            if getattr(package, "blocking", False):
                outstanding.append(card_id)
            continue

        if cards.has_secret_field(card):
            results.append({"card": card_id, "outcome": "needs_you",
                            "applied": False,
                            "why": "this one asks for something only you can "
                                   "type"})
            if getattr(package, "blocking", False):
                outstanding.append(card_id)
            continue
        if card["consequential"] and card_id not in accepted:
            results.append({"card": card_id, "outcome": "needs_you",
                            "applied": False,
                            "why": "open this row and read it first: "
                                   + ", ".join(card["consequential_reasons"])})
            if getattr(package, "blocking", False):
                outstanding.append(card_id)
            continue

        outcome = _apply_now(run, {}, skill_id, part, requires=True)
        row = run.log[-1] if run.log else {}
        results.append({"card": card_id, "outcome": outcome, "applied": True,
                        "why": "", "verified": row.get("verified") or {}})
        if outcome not in ("done", "not_built") and getattr(
                package, "blocking", False):
            outstanding.append(card_id)

    return {"results": results, "refusals": refusals,
            "gaps": run.gaps, "stale": list(run.stale),
            "complete": not outstanding, "outstanding": outstanding}


def _support_parts(may_complete):
    """The wizard's own entries -- the config, the manifest -- for a browser.

    The same three the console wizard runs (``cli._wizard_runner_parts``), with
    one difference stated where it happens: this run may end with a blocking
    card still waiting for the owner, so *may_complete* decides whether first-run
    setup is marked done. The manifest itself is written either way -- a record
    of what was decided is the honest artefact, and the run state says what is
    still outstanding.
    """
    from puffpai import cli
    try:
        return cli._wizard_runner_parts(may_complete)
    except Exception:  # pragma: no cover - defensive
        return {}


def _run_support_step(run: wizard_runner.Runner,
                      step: wizard_runner.Step) -> dict:
    """Run one non-card entry server-side, quietly, at its scheduled position.

    These are the entries that make a build a build -- the Ollama config, the
    build manifest, Prove's static checks. A dashboard-only run that skipped
    them would let a browser walk every card and end with no
    ``config/build_manifest.json`` at all and ``GET /build/prove`` answering
    ``ran: false``.

    Three things hold:

    * **Requirements are honoured.** An entry whose inputs an earlier step never
      produced is reported as waiting, and is not run against nothing.
    * **Nothing is printed.** These entries print for a console wizard; here the
      caller is an HTTP request and stdout is a server log, so it is captured
      and dropped.
    * **No face.** The runner was given a renderer, so ``show_once()`` is None.
    """
    import contextlib
    import io

    missing = [key for key in step.requires if run.shared.get(key) is None]
    if missing:
        return {"card": step.entry, "outcome": "needs_you", "applied": False,
                "why": f"waiting on {', '.join(missing)} from an earlier step"}
    # Re-run every time: the manifest is a record of the sections as they stand,
    # and a second /wizard/run after another card was applied has to rewrite it
    # rather than report the first one as still true.
    run.entries.pop(step.entry, None)
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink):
            outcome = run.run_step(step)
    except Exception as exc:  # noqa: BLE001 - one entry never takes the route down
        return {"card": step.entry, "outcome": "failed", "applied": False,
                "why": f"{type(exc).__name__}: {exc}"}
    return {"card": step.entry, "outcome": outcome, "applied": True, "why": ""}


# -- resume -------------------------------------------------------------------

def resume_offer(args=None, cfg=None) -> dict:
    """Is there a stopped setup to continue, and what would continuing keep?

    Read-only, like :func:`propose_cards`: it reads the record and observes
    nothing. The re-verify that decides what a continued run may actually skip
    happens in :func:`resume_choice`, when the owner has said to continue --
    verifying costs a disk read and an Ollama call, and a page load is not a
    reason to make them.
    """
    args = args or _args()
    cfg = cfg or _cfg()
    run, why = wizard_runner.find_stopped_run(args=args, cfg=cfg)
    if run is None:
        return {"offer": False,
                "why": why or "There is no stopped setup to continue."}
    summary = run.resume_summary()
    summary["offer"] = True
    summary["why"] = ""
    return summary


def resume_choice(choice: Any, args=None, cfg=None) -> dict:
    """``continue`` or ``restart``, applied to the record. Same rules as the CLI.

    **Continue is not "trust the file".** Every blocking card the record calls
    done is verified against this machine again (:meth:`Runner.reverify_blocking`)
    and anything that does not hold is dropped from the record, so the next
    ``GET /wizard/cards`` shows it unanswered and ``POST /wizard/run`` asks it
    again. The vault is checked on disk whatever the record says.

    **Restart archives.** The old record is moved aside, not deleted.

    The same one-run-at-a-time lock ``/wizard/run`` takes is taken here: both
    write the run record, and two writers of one record describe neither run.
    """
    args = args or _args()
    cfg = cfg or _cfg()
    if choice not in ("continue", "restart"):
        raise AnswerRefused(
            "Say whether to continue the stopped setup or start over: "
            "'continue' or 'restart'.", "choice")
    run, why = wizard_runner.find_stopped_run(args=args, cfg=cfg)
    if run is None:
        return {"choice": choice, "resumed": False, "archived": False,
                "reasked": [], "why": why or "there is no stopped setup here"}
    lock = _take_lock(cfg)
    try:
        if choice == "restart":
            moved = wizard_runner.archive_state(run.state_path())
            return {"choice": choice, "resumed": False,
                    "archived": bool(moved), "reasked": [],
                    "why": "the stopped setup was set aside"}
        reasked = run.reverify_blocking()
        run.save()
        return {"choice": choice, "resumed": True, "archived": False,
                "reasked": reasked, "why": ""}
    finally:
        _drop_lock(lock)


def run_state(args=None, cfg=None) -> dict:
    """What the log says: one row per skill, the gaps, and what is stale.

    Read back through :meth:`Runner.load_state`, which re-renders every stored
    gap through the fix registry -- a command line this build prints is never
    read off disk.
    """
    args = args or _args()
    cfg = cfg or _cfg()
    run = wizard_runner.Runner(args=args, cfg=cfg, non_interactive=True)
    found = run.load_state()
    return {
        "schema": wizard_runner.STATE_SCHEMA,
        "found": bool(found),
        "started_at": run.started_at if found else None,
        # The order is carried explicitly: a JSON object's key order is not a
        # promise, and the fixed skill order is part of what the log says.
        "order": list(wizard_runner.SKILL_ORDER),
        "skills": run.skill_status(),
        "entries": run.entries,
        "log": run.log,
        "gaps": run.gaps,
        "stale": list(run.stale),
        "next_entry": run.next_entry(),
        # The resume banner's whole input. It rides on the state route rather
        # than having one of its own because it is the same question the log
        # answers -- what happened last time -- and a second request for one
        # boolean is a second thing that can be out of date with the first.
        "resume": resume_offer(args, cfg),
    }


__all__ = ["AnswerRefused", "LOCK_NAME", "RunInProgress", "SkillNotConverted",
           "apply_skill", "asks_for_secret", "has_lifecycle", "parts_of",
           "propose_cards", "propose_one", "resume_choice", "resume_offer",
           "run_rest", "run_state"]
