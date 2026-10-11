"""Owner egress *policies* — the settings that are not doors but gate egress.

A door says WHERE the appliance may go. A policy here says WHO on the
appliance may walk through an open door. They are separate questions, and
without this module only one of them is reachable from the UI:

* ``egress_doors`` resolves ``env > overlay > config > default`` and the owner
  overlay is writable by the service, so the Settings switch is real.
* ``agents.chat_allow_egress`` — the opt-in that lets an ad-hoc chat turn carry
  egress at all — resolved ``env > config > default`` via
  :func:`puffpai.agents.preferences.flag`. On a hardened appliance
  (``ProtectSystem=strict``) ``/opt/puffpai/config`` is outside
  ``ReadWritePaths``, so without an overlay the only way to flip it is to edit
  a read-only file as root. That is not a switch; it is a maintenance window.

This module gives policies the same overlay tier the doors already have, in
their **own** file, so a policy write can never touch a door flag and a
corrupt door overlay can never silently change a policy (or the reverse).

Precedence, per policy, first tier with an opinion wins:

1. ``env``     — ``PUFFPAI_CHAT_ALLOW_EGRESS``. An operator forcing a policy
   from the systemd unit must not be overridable by anything the running
   service can write. That is the whole reason this tier is first.
2. ``overlay`` — ``$PUFFPAI_DATA_DIR/state/egress_policy.json``, written by
   :func:`puffpai.security.egress_admin.set_policy` and nothing else.
3. ``config``  — ``agents.chat_allow_egress`` in ``puffpai_config.json``.
   Byte-for-byte the key :func:`preferences.flag` already read, so an appliance
   that had it set keeps its setting.
4. ``default`` — ``False``. Every policy here ships off.

Every ambiguity resolves to **off**, never on: a junk value, an unreadable
overlay and an unreadable config are all "not consent".

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass

from puffpai.logging_config import get_logger
from puffpai.security import egress_doors

logger = get_logger(__name__)

_lock = threading.RLock()

#: Basename of the policy overlay. Deliberately NOT ``egress_doors.json``:
#: there are already two files by that name (the door overlay and the
#: last-used snapshot) and a third would be a trap.
POLICY_NAME = "egress_policy.json"

#: Marks a file as the policy overlay, so a reader pointed at the wrong file
#: says so rather than obeying it.
POLICY_KIND = "egress_policy_overlay"

#: Same posture as the door overlay: this records what the household consented
#: to, and it is not for every account on the appliance.
POLICY_DIR_MODE = 0o750
POLICY_FILE_MODE = 0o640

#: The tiers a policy's value can come from, highest precedence first. Mirrors
#: :data:`egress_doors.FLAG_SOURCES` so the UI can render both the same way.
POLICY_SOURCES = ("env", "overlay", "config", "default")


@dataclass(frozen=True)
class Policy:
    """One owner-settable egress policy.

    *config_section* / *config_key* name the pre-existing config location, so
    the overlay is an ADDITION to how a setting was already read and never a
    migration an owner has to perform.
    """

    id: str
    env_var: str
    config_section: str
    config_key: str
    purpose: str
    data_effect: str
    settings_hint: str
    default: bool = False
    notes: str = ""


POLICIES: tuple[Policy, ...] = (
    Policy(
        id="chat_allow_egress",
        env_var="PUFFPAI_CHAT_ALLOW_EGRESS",
        config_section="agents",
        config_key="chat_allow_egress",
        purpose="Whether an ad-hoc chat turn may carry an egress budget at "
                "all — that is, whether asking PuffPAI a question in chat can "
                "make her look something up online.",
        data_effect="Nothing leaves the house because of this setting alone. "
                    "It decides whether a chat turn may use the web doors you "
                    "have opened; with every web door shut it changes "
                    "nothing.",
        settings_hint="Looking things up from chat needs the chat egress "
                      "opt-in, which is off — turn it on in "
                      "Settings > Egress.",
        default=False,
        notes="Read by api_server._chat_envelope_budget and by "
              "assistant_tools.ask_agent, which are the only two places a chat "
              "envelope can acquire allow_egress. Both AND it with the "
              "relevant door, so this opt-in can never widen reach past the "
              "doors: it gates WHO may walk through, never WHERE.",
    ),
)

_BY_ID: dict[str, Policy] = {p.id: p for p in POLICIES}

_UNSET = object()
_overlay_cache: object = _UNSET
_OVERRIDE: str | None = None


def policy(policy_id: str) -> Policy | None:
    """The policy with this id, or None."""
    return _BY_ID.get(str(policy_id).strip().lower())


def policy_ids() -> tuple[str, ...]:
    """Every policy id, in registry order."""
    return tuple(p.id for p in POLICIES)


def set_policy_override(path: str | None) -> None:
    """Point the policy overlay at *path* (tests, and an operator moving it)."""
    global _OVERRIDE
    with _lock:
        _OVERRIDE = path
    reset_cache()


def policy_path() -> str:
    """Absolute path to the policy overlay.

    Lands beside the door overlay under ``$PUFFPAI_DATA_DIR/state`` — the one
    tree a hardened unit actually grants the service — for the same reason the
    door overlay does: flipping an owner switch must not require giving the API
    server write access to the whole appliance configuration.
    """
    if _OVERRIDE:
        return _OVERRIDE
    from puffpai.statedir import resolve_runtime_dir
    return os.path.join(
        resolve_runtime_dir(
            os.path.join(egress_doors._REPO_ROOT_FOR_OVERLAY, "state"),
            env_subdir="state"),
        POLICY_NAME)


def reset_cache() -> None:
    """Forget the cached overlay *and* the config the registry caches.

    Both are inputs to :func:`resolve`, so clearing one without the other is
    how a write lands on disk and the next read still answers with the old
    value.
    """
    global _overlay_cache
    with _lock:
        _overlay_cache = _UNSET
    egress_doors.reset_cache()


def _raw_overlay() -> dict | None:
    """The overlay's ``policies`` map, ``{}`` if there is none, ``None`` if broken.

    Three distinct answers, as in :func:`egress_doors._raw_overlay`: ``{}`` is
    "no file yet, the config decides"; a dict is the owner's explicit settings;
    ``None`` is "the file is there and will not parse", which resolves every
    policy OFF. A file we cannot read is not consent.
    """
    global _overlay_cache
    with _lock:
        cached = _overlay_cache
    if cached is not _UNSET:
        return cached  # type: ignore[return-value]
    # Resolved outside the lock: policy_path() reaches puffpai.statedir, which
    # may import puffpai.config, and holding this lock across that import is
    # how a lock-ordering deadlock gets built.
    path = policy_path()
    result: dict | None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except FileNotFoundError:
        result = {}
    except (OSError, ValueError) as exc:
        logger.warning(
            "egress policy overlay unreadable (%s): %s — every policy stays "
            "off", path, exc)
        result = None
    else:
        if not isinstance(loaded, dict):
            logger.warning("egress policy overlay %s is not an object — every "
                           "policy stays off", path)
            result = None
        else:
            policies = loaded.get("policies")
            if policies is None and loaded.get("kind") != POLICY_KIND:
                # Pointed at some other JSON file. Say so rather than failing
                # every policy off on a file that was never a policy overlay.
                logger.warning(
                    "egress policy overlay %s has no 'policies' object; "
                    "ignoring it", path)
                result = {}
            elif not isinstance(policies, dict):
                logger.warning("egress policy overlay %s has a non-object "
                               "'policies' — every policy stays off", path)
                result = None
            else:
                result = policies
    with _lock:
        _overlay_cache = result
    return result


def resolve(policy_id: str) -> tuple[bool, str, str]:
    """``(value, source, reason)`` for one policy. Never raises.

    *source* is one of :data:`POLICY_SOURCES`. *reason* is for logs, tests and
    the Settings card — "off" and "off BECAUSE the unit file forces it and your
    click cannot win" are different facts, and only the second one explains why
    a switch will not move.
    """
    p = policy(policy_id)
    if p is None:
        return False, "default", f"no egress policy named {policy_id!r}"

    raw_env = os.environ.get(p.env_var)
    if raw_env is not None and raw_env.strip() != "":
        decided = egress_doors._as_bool(raw_env)
        if decided is None:
            return False, "env", (f"{p.env_var}={raw_env.strip()!r} is not a "
                                  f"flag value; the opt-in stays off")
        return decided, "env", (f"{p.env_var}="
                                f"{'on' if decided else 'off'} "
                                f"(environment wins)")

    overlay = _raw_overlay()
    if overlay is None:
        return False, "overlay", (
            f"the policy overlay at {policy_path()} will not parse; every "
            f"opt-in stays off until it is fixed or removed")
    if p.id in overlay:
        decided = egress_doors._as_bool(overlay.get(p.id))
        if decided is None:
            return False, "overlay", (f"overlay {p.id} is not a flag value; "
                                      f"the opt-in stays off")
        return decided, "overlay", (f"overlay {p.id}={decided} "
                                    f"(set by the owner)")

    section = egress_doors._raw_config().get(p.config_section)
    if isinstance(section, dict) and p.config_key in section:
        decided = egress_doors._as_bool(section.get(p.config_key))
        if decided is None:
            return False, "config", (f"{p.config_section}.{p.config_key} is "
                                     f"not a flag value; the opt-in stays off")
        return decided, "config", (f"{p.config_section}.{p.config_key}="
                                   f"{decided}")

    return p.default, "default", (f"{p.config_section}.{p.config_key} is not "
                                  f"set; default is "
                                  f"{'on' if p.default else 'off'}")


def enabled(policy_id: str) -> bool:
    """Just the boolean. Fails off on every ambiguity."""
    return resolve(policy_id)[0]


def policy_status() -> list[dict]:
    """Every policy, its value and where that value comes from.

    Published by ``/system/status`` beside ``egress_doors`` so the Settings
    card can render the opt-in with the same provenance the doors carry.
    ``writable`` is false exactly when an environment variable is forcing the
    value, because that is when the write would land and be inert.
    """
    out: list[dict] = []
    for p in POLICIES:
        value, source, reason = resolve(p.id)
        out.append({
            "id": p.id,
            "enabled": bool(value),
            "reason": reason,
            "source": source,
            "writable": source != "env",
            "purpose": p.purpose,
            "data_effect": p.data_effect,
            "config_flag": f"{p.config_section}.{p.config_key}",
            "env_var": p.env_var,
            "default": p.default,
        })
    return out


#: The doors an ad-hoc chat turn can plausibly want. Both are Toolsmith
#: intents; nothing else a chat turn does needs an egress budget.
CHAT_EGRESS_DOORS = ("web_fetch", "web_search")


def chat_egress_allowed(door_id: str | None = None) -> tuple[bool, str]:
    """May a CHAT-minted envelope carry ``allow_egress``? ``(ok, reason)``.

    **Both halves are required, and they answer different questions:**

    * the ``chat_allow_egress`` policy — may an ad-hoc chat turn reach outward
      at all, as opposed to a planned run that was granted a budget elsewhere;
    * the door — may this appliance reach that class of destination at all.

    ANDing them is what keeps the opt-in from widening reach: with every web
    door shut, turning the opt-in on changes nothing, and with the opt-in off,
    an open door still lets a planned run through. Neither can substitute for
    the other.

    *door_id* names the specific door when the caller knows the intent
    (``assistant_tools.ask_agent`` does). With it omitted the answer is "any web
    door", which is the right coarse grant for a planned run seeded from chat:
    the Toolsmith re-checks the exact door before a socket opens, so this
    budget can only ever be the outer bound.

    Never raises: a registry that will not import fails the answer to False.
    """
    opt_in, _source, opt_reason = resolve("chat_allow_egress")
    if not opt_in:
        return False, f"chat egress opt-in is off ({opt_reason})"

    ids = (door_id,) if door_id else CHAT_EGRESS_DOORS
    reasons = []
    for did in ids:
        try:
            allowed, reason = egress_doors.egress_allowed(str(did))
        except Exception as exc:  # a broken registry is not consent
            logger.warning("egress door registry unreadable for %s: %s",
                           did, exc)
            return False, f"egress door registry unreadable ({exc})"
        if allowed:
            return True, f"chat egress opt-in on and door {did} open"
        reasons.append(f"{did}: {reason}")
    return False, ("chat egress opt-in on but no web door is open ("
                   + "; ".join(reasons) + ")")


__all__ = [
    "CHAT_EGRESS_DOORS",
    "POLICIES",
    "POLICY_DIR_MODE",
    "POLICY_FILE_MODE",
    "POLICY_KIND",
    "POLICY_NAME",
    "POLICY_SOURCES",
    "Policy",
    "chat_egress_allowed",
    "enabled",
    "policy",
    "policy_ids",
    "policy_path",
    "policy_status",
    "reset_cache",
    "resolve",
    "set_policy_override",
]
