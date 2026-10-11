"""The ONE caller of skill code, and the switches that can stop it.

Every path that could ever run a skill goes through :func:`run` -- the
fastpath, the install-time test, the dashboard. One caller means one place the
kill switch, the integrity check and the fail-shut behaviour live, instead of
three places where two of them agree.

Order of refusals, and each is a refusal rather than a degraded run:

1. ``PUFFPAI_SKILLS_DISABLED=1`` in the environment. The operator tier wins
   over configuration, exactly as with the egress doors: a switch in the unit
   file must not be overridable by anything the running service can write.
2. ``skills.enabled`` in the config, which ships **false**: skills stay off
   until the owner turns them on.
3. No bubblewrap: skills report ``offline``. Losing the isolation costs the
   capability, never the isolation.
4. The skill is disabled, or its files no longer hash to what was approved.
   Tamper **auto-disables**, writes the ledger line and says so out loud.
5. A door refusal at run time auto-disables the skill for the same reason: a
   skill reaching for a host the owner did not approve is not a skill to retry.
   A TRANSPORT failure on an approved host (no internet, DNS down, a 5xx or
   429) is not a refusal: the skill stays on, its door is left alone, and the
   answer says plainly that the internet could not be reached. A TLS failure
   is treated as a refusal, because from here it looks like tampering.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid

logger = logging.getLogger(__name__)

DISABLE_ENV = "PUFFPAI_SKILLS_DISABLED"

OFFLINE_SENTENCE = ("Skills are switched off on this appliance, so I can't run "
                    "that one.")
NO_SANDBOX_SENTENCE = ("I can't run skills on this box -- the sandbox they run "
                       "inside isn't available, and I won't run skill code "
                       "without it.")


def env_disabled() -> bool:
    """True when the operator switched skills off from the environment."""
    return str(os.environ.get(DISABLE_ENV, "")).strip().lower() in (
        "1", "true", "yes", "on")


def enabled() -> bool:
    """The kill switch: env first, then config, default OFF. Never raises."""
    if env_disabled():
        return False
    try:
        from puffpai.config import get_config
        return bool(getattr(get_config().skills, "enabled", False))
    except Exception as exc:
        logger.debug("skills: config unavailable, staying off (%s)", exc)
        return False


def available() -> tuple[bool, str]:
    """``(usable, reason)`` for the whole subsystem, for probes and for status."""
    if env_disabled():
        return False, f"{DISABLE_ENV} is set, so skills are off."
    if not enabled():
        return False, ("Skills are switched off in this appliance's "
                       "configuration (skills.enabled).")
    from puffpai.skills import sandbox as _sandbox
    if not _sandbox.sandbox_available():
        return False, ("This box has no sandbox helper, so skills cannot run "
                       "at all -- PuffPAI will not run skill code unsandboxed.")
    return True, ""


def run_package(package_dir: str, *, mode: str = "test", inputs=None,
                broker=None) -> dict:
    """Run one skill package directory in the jail. Low level; no kill switch.

    *mode* ``"test"`` uses the package's own fixtures as the broker and asserts
    them in the runner; ``"run"`` needs a live broker passed in. The two are
    separate arguments rather than a flag inside the payload so that "a test
    run reached the network" is not a bug that can exist.
    """
    from puffpai.skills import broker as _broker
    from puffpai.skills import manifest as _manifest
    from puffpai.skills import sandbox as _sandbox
    from puffpai.skills import store as _store

    try:
        manifest, _shas = _manifest.validate_dir(package_dir)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    if not _sandbox.sandbox_available():
        return {"ok": False, "sandbox_unavailable": True,
                "error": ("no sandbox helper on this box; refusing to run "
                          "skill code unsandboxed")}

    hosts = manifest["permissions"]["network_hosts"]
    limits = manifest["limits"]
    if mode == "test":
        import json
        with open(os.path.join(package_dir, "tests_fixtures.json"),
                  encoding="utf-8") as handle:
            fixtures = json.load(handle)
        broker = _broker.FixtureBroker(manifest["id"], hosts, limits,
                                       fixtures=fixtures)
    elif broker is None:
        broker = _broker.LiveBroker(manifest["id"], hosts, limits)

    run_root = os.path.join(_store.runs_root(),
                            f"{manifest['id']}-{uuid.uuid4().hex[:12]}")
    payload = {"mode": "test" if mode == "test" else "run",
               "skill_id": manifest["id"],
               "inputs": dict(inputs or {}),
               "limits": limits}
    started = time.monotonic()
    try:
        outcome = _sandbox.execute(package_dir, payload, broker=broker,
                                   run_root=run_root,
                                   timeout_s=limits["timeout_s"])
    except _sandbox.SandboxUnavailable as exc:
        return {"ok": False, "sandbox_unavailable": True, "error": str(exc)}
    except Exception as exc:
        logger.warning("skills: run of %s failed: %s", manifest["id"],
                       type(exc).__name__)
        return {"ok": False, "error": f"the run failed ({type(exc).__name__})"}
    finally:
        shutil.rmtree(run_root, ignore_errors=True)

    result = outcome.get("result") or {}
    return {
        "ok": bool(outcome.get("ok")),
        "id": manifest["id"],
        "speech": str(result.get("speech") or ""),
        "data": result.get("data") or {},
        "error": str(outcome.get("error") or ""),
        "tampered": outcome.get("tampered") or [],
        "refusals": list(getattr(broker, "refusals", []) or []),
        "failures": list(getattr(broker, "failures", []) or []),
        "requests": int(outcome.get("requests") or 0),
        "seconds": round(time.monotonic() - started, 2),
    }


def _disable(skill_id: str, reason: str, kind: str) -> None:
    from puffpai.skills import store as _store
    try:
        _store.set_enabled(skill_id, False, reason=reason)
    except Exception as exc:  # pragma: no cover
        logger.warning("skills: could not disable %s: %s", skill_id, exc)
    try:
        from puffpai.agent_activity import log_activity
        log_activity("toolsmith", kind,
                     f"Skill '{skill_id}' switched off: {reason}", "blocked", {})
    except Exception as exc:  # pragma: no cover
        logger.debug("skills: activity not recorded (%s)", exc)


def run(skill_id: str, inputs=None) -> dict:
    """Run an installed skill. The single entry point. Never raises."""
    from puffpai.skills import store as _store

    usable, reason = available()
    if not usable:
        sentence = (NO_SANDBOX_SENTENCE if "sandbox" in reason
                    else OFFLINE_SENTENCE)
        return {"ok": False, "error": reason, "speech": sentence}

    record = _store.load(skill_id)
    if record is None:
        return {"ok": False, "error": "no such skill",
                "speech": f"I don't have a skill called {skill_id}."}
    if not record["state"].get("enabled"):
        return {"ok": False, "error": "disabled",
                "speech": (f"The {record['manifest']['name']} skill is "
                           f"switched off right now.")}
    if not record["intact"]:
        _disable(skill_id, record["integrity_reason"], "skill_tampered")
        return {"ok": False, "error": record["integrity_reason"],
                "speech": (f"I've switched the {record['manifest']['name']} "
                           f"skill off: {record['integrity_reason']}. It isn't "
                           f"the code you approved, so I won't run it.")}

    # Skill doors are in-memory only; after a restart nothing has registered
    # them yet. Do it before the broker looks the door up, or the owner's own
    # open door reads as "no such door" and the skill is refused.
    _store.ensure_door_registered(skill_id)
    outcome = run_package(record["dir"], mode="run", inputs=inputs)
    refusals = outcome.get("refusals") or []
    failures = outcome.get("failures") or []
    if outcome.get("tampered"):
        _disable(skill_id, "its files changed while it ran", "skill_tampered")
    elif refusals:
        _disable(skill_id, f"a request was refused ({refusals[0]})",
                 "skill_refused")
    _store.note_run(skill_id, ok=bool(outcome.get("ok")),
                    error=outcome.get("error", ""),
                    refusal=(refusals[0] if refusals else ""))

    name = record["manifest"]["name"]
    if outcome.get("ok"):
        outcome["speech"] = outcome.get("speech") or ""
        return outcome
    if outcome.get("tampered"):
        outcome["speech"] = (
            f"I've switched the {name} skill off: its files changed while it "
            f"ran. It isn't the code you approved, so I won't run it.")
    elif refusals and refusals[0] == _broker_tls_refusal():
        outcome["speech"] = (
            f"I couldn't make a trusted secure connection for the {name} "
            f"skill, so I stopped it and switched it off to be safe. You can "
            f"turn it back on from the Skills page.")
    elif refusals:
        outcome["speech"] = (
            f"The {name} skill tried to reach somewhere "
            f"I don't have your permission for, so I stopped it and switched "
            f"it off. ({refusals[0]})")
    elif failures:
        # Nothing was refused, the approved host just could not be reached
        # or did not answer. The skill stays on and its door stays as the
        # owner left it; the next ask, with the internet back, just works.
        # No exception names and no numbers: this is spoken aloud.
        outcome["speech"] = _failure_sentence(name, failures)
    else:
        # The raw error is in the run record for the Skills page; it is not
        # something to read out loud.
        outcome["speech"] = f"The {name} skill didn't work just now."
    return outcome


def _broker_tls_refusal() -> str:
    from puffpai.skills import broker as _broker
    return _broker.TLS_REFUSAL


def _failure_sentence(name: str, failures) -> str:
    """One plain sentence for a run that failed only on the network."""
    from puffpai.skills import broker as _broker

    def _any(prefix):
        return any(str(f).startswith(prefix) for f in failures)

    if _any(_broker.FAILURE_NETWORK):
        return (f"I couldn't reach the internet just now, so I can't get the "
                f"{name}.")
    if _any(_broker.FAILURE_SETUP):
        return (f"Something went wrong on my side, so I can't get the {name} "
                f"right now.")
    return (f"The service behind the {name} skill isn't answering right now, "
            f"so I can't get the {name}.")


def blocked_sentence(skill_id: str) -> str | None:
    """Why an installed skill cannot run right now, or None if it can.

    For the chat hook: a trigger the household approved must get a true answer
    rather than a fall-through to the model, which would happily invent one.
    Checks the skill's own switch, then its door. Never raises.
    """
    from puffpai.skills import store as _store
    try:
        record = _store.load(skill_id)
    except Exception as exc:  # pragma: no cover
        logger.debug("skills: %s not loadable (%s)", skill_id, exc)
        return None
    if record is None:
        return None
    name = record["manifest"]["name"]
    if not record["state"].get("enabled"):
        return (f"The {name} skill is switched off right now, so I can't run "
                f"it. You can turn it back on from the Skills page.")
    if record["manifest"]["permissions"]["network_hosts"]:
        # A door that is not registered is our fault, not the owner's switch:
        # leave that to run(), which registers it and, failing that, says so
        # without switching anything off.
        if not _store.ensure_door_registered(skill_id):
            return None
        try:
            from puffpai.security import egress_doors as _doors
            allowed, _reason = _doors.egress_allowed(
                _doors.skill_door_id(skill_id, 0))
        except Exception as exc:
            logger.debug("skills: door for %s unreadable (%s)", skill_id, exc)
            allowed = False
        if not allowed:
            return (f"The {name} skill isn't allowed to go online right now, "
                    f"so I can't run it. You can check it on the Skills page.")
    return None


def match(utterance: str, *, include_disabled: bool = False) -> str | None:
    """The skill whose trigger IS this utterance, or None.

    Whole-utterance equality after normalisation -- never containment. The
    argument is the registry's own (``find_for_dispatch``): containment cannot
    tell "this sentence IS the request" from "this sentence MENTIONS the
    request", and a skill is the least trusted thing on the appliance.

    *include_disabled* also matches switched-off skills, so the chat hook can
    say "that skill is off" instead of letting the model guess. It never makes
    a disabled skill RUN: :func:`run` refuses those on its own.
    """
    from puffpai.skills import manifest as _manifest
    from puffpai.skills import store as _store

    said = _manifest.normalise(utterance)
    if not said:
        return None
    for record in _store.all_loaded():
        if not include_disabled and not record["state"].get("enabled"):
            continue
        for trigger in record["manifest"]["triggers"]:
            if _manifest.normalise(trigger) == said:
                return record["id"]
    return None


def listing() -> list:
    """One row per installed skill, for the dashboard and for "what skills do
    I have". Content-free about the household: names, reach and status only."""
    from puffpai.skills import install as _install
    from puffpai.skills import store as _store

    rows = []
    for record in _store.all_loaded():
        hosts = record["manifest"]["permissions"]["network_hosts"]
        state = record["state"]
        rows.append({
            "id": record["id"],
            "name": record["manifest"]["name"],
            "description": record["manifest"]["description"],
            "triggers": record["manifest"]["triggers"],
            "hosts": hosts,
            "domains": sorted({_install.registrable_domain(h) for h in hosts}),
            "enabled": bool(state.get("enabled")),
            "intact": record["intact"],
            "runs": int(state.get("runs") or 0),
            "last_error": state.get("last_error") or "",
            "last_refusal": state.get("last_refusal") or "",
            "tested": bool((state.get("last_test") or {}).get("ok")),
            "author": record["manifest"]["author"],
            "version": record["manifest"]["version"],
        })
    return rows


__all__ = ["DISABLE_ENV", "NO_SANDBOX_SENTENCE", "OFFLINE_SENTENCE",
           "available", "blocked_sentence", "enabled", "env_disabled", "listing", "match", "run",
           "run_package"]
