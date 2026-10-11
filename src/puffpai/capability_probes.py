"""Runtime availability probes for the capability registry.

WHY THIS EXISTS
---------------
``capability_registry`` declares what PuffPAI can do. Its ``status`` field is a
STATIC declaration, written when the capability was built, and it can drift
away from the box:

  * ``security.zone3`` declared ``live`` while ``GET /zone3/status`` answers
    "Zone 3 not enabled" would make "what can you do" advertise a network
    edge that does not exist.
  * ``security.network`` advertises block / unblock / lockdown, which the
    appliance may not be able to carry out at all (see ``firewall_manager``'s
    module docstring for the reasons).

Rather than hand-edit a status that drifts the moment someone starts a
sinkhole, the affected entries derive their availability from the same gate the
real endpoint uses, at read time. Same pattern as
``capability_registry._any_door_open``, which already probes the egress doors
for the spoken overview.

FAIL DIRECTION. ``_any_door_open`` fails OPEN, because its failure mode is
understating a working area. These probes fail CLOSED: their whole job is to
stop OVERSTATING, and a probe that cannot answer is not evidence that the
capability is there. An unavailable answer costs the user a sentence saying so;
an over-claim costs them a security control they think they have.

Probes are cheap by construction — a cached-config read and an inert object
construction, no sockets, no subprocesses — because a surface may serialize the
whole registry on every dashboard poll. A short TTL keeps even that off the hot
path.
"""

from __future__ import annotations

import threading
import time

#: How long a probe answer is reused. Short enough that flipping Pi-hole on
#: shows up within a poll or two, long enough that serializing ~90 capabilities
#: does not re-probe ~90 times.
_TTL_SECONDS = 15.0

_lock = threading.Lock()
#: cap_id -> (expires_at_monotonic, available, reason)
_cache: dict = {}


def _probe_firewall_writes() -> "tuple[bool, str]":
    """Can PuffPAI actually add or remove a firewall rule on this host?"""
    from puffpai.firewall_manager import get_firewall_backend

    backend = get_firewall_backend()
    if getattr(backend, "writes_supported", True):
        return True, ""
    reason = getattr(backend, "unavailable_reason", "") or (
        "Blocking a device is not available on this platform.")
    return False, reason


def _probe_zone3() -> "tuple[bool, str]":
    """Is the Zone-3 network edge actually enabled?

    Deliberately the SAME gate ``api_server._get_zone3`` uses (the config flag),
    not an HTTP call to our own ``/zone3/status``: a self-request from inside a
    request would deadlock a single-worker server, and the config flag is what
    decides that endpoint's answer anyway.
    """
    from puffpai.config import get_config

    if not getattr(get_config().zone3, "enabled", False):
        return False, ("Zone 3 (the network-edge DNS filter) is not enabled on "
                       "this box. The appliance's own firewall is still in "
                       "force and still refuses every inbound connection "
                       "except the doors it declares.")
    return True, ""


def _probe_code_model() -> "tuple[bool, str]":
    """Is a coding model configured on this box?

    ``config.ollama.code_model`` is EMPTY by default — the tag is written by
    the deploy step once a model has been chosen and pulled, so this row must
    not advertise code writing on a box that has none. A config read only: the
    model is on-demand and asking Ollama whether it is pulled would be a socket
    on a path a dashboard poll can hit.
    """
    from puffpai.inference import code_model

    if code_model.is_configured():
        return True, ""
    return False, ("No coding model is set up on this box yet, so I can't "
                   "write code or automations for you.")


def _probe_websearch() -> "tuple[bool, str]":
    """Is web search actually usable right now?

    The one probe here that was added to stop UNDER-claiming rather than
    over-claiming: this row was declared ``needs-auth``
    forever, so "what can you do" said web search "needs a one-time setup
    first" on a box where the doors were open and the fastpath was answering
    live searches. See :mod:`puffpai.websearch_status` for the four states and
    for why asking this question does not itself send anything outbound.
    """
    from puffpai import websearch_status

    return websearch_status.probe_capability()


def _probe_skill(cap_id: str) -> "tuple[bool, str]":
    """``(available, reason)`` for one installed skill's row.

    A skill is available only when ALL of it holds: the subsystem is switched
    on, this box has a sandbox to run it in, the skill itself is enabled, its
    files still hash to what the owner approved, and its own fixtures passed
    here. Anything else is ``offline`` — a skill that cannot run must not be
    listed as something the appliance does.
    """
    from puffpai.skills import dispatch as _dispatch
    from puffpai.skills import store as _store

    usable, reason = _dispatch.available()
    if not usable:
        return False, reason
    skill_id = cap_id.split(".", 1)[1] if "." in cap_id else ""
    record = _store.load(skill_id)
    if record is None:
        return False, "That skill is not installed any more."
    if not record["state"].get("enabled"):
        return False, "You switched this skill off."
    if not record["intact"]:
        return False, (f"This skill's files are not the ones you approved "
                       f"({record['integrity_reason']}).")
    if not (record["state"].get("last_test") or {}).get("ok"):
        return False, "This skill has not passed its own tests on this box."
    return True, ""


def _probe_maps() -> "tuple[bool, str]":
    """Are both offline map engines answering on this box?

    Two loopback HTTP checks with a short timeout; the TTL below keeps them
    off the dashboard's poll path. Sends no household data.
    """
    from puffpai import maps_directions

    return maps_directions.probe_capability()


#: cap_id -> probe. A capability NOT listed here keeps its declared status and
#: is reported available: the registry stays the source of truth and this
#: module only overrides the entries that were measured to be wrong.
_PROBES = {
    "security.network": _probe_firewall_writes,
    "security.zone3": _probe_zone3,
    "dev.code": _probe_code_model,
    "utility.websearch": _probe_websearch,
    "maps.directions": _probe_maps,
}

#: Capabilities whose declared ``status`` should be replaced with "offline"
#: when the probe says unavailable — i.e. the WHOLE capability is down.
#:
#: ``security.network`` is deliberately absent. Its authority tool is
#: ``privacy_scan_network`` (an ARP scan), which works fine here; only the
#: write half is unavailable. Calling the whole thing offline would trade one
#: wrong answer for another, so it reports ``available: false`` with a reason
#: that names exactly what is missing, and keeps its declared status.
#:
#: ``dev.code`` IS listed: with no model tag configured there is no half of it
#: that works, so "offline" is the honest status rather than a live row with an
#: unavailable flag nobody reads out loud.
#:
#: ``utility.websearch`` is deliberately ABSENT, for the ``security.network``
#: reason: its unavailable states are "you switched it off" and "the container
#: is down", and collapsing both into the single word "offline" would throw
#: away the sentence that tells the owner WHICH — and which switch to go to.
#: Its declared status stays LIVE and the probe's reason carries the detail.
_STATUS_FOLLOWS_PROBE = frozenset({"security.zone3", "dev.code",
                                   "maps.directions"})

#: Rows contributed by installed skills. Their status ALWAYS follows the probe:
#: unlike the declared capabilities, a skill row is written by the store from
#: state that can change between two questions (switched off, files tampered
#: with, sandbox gone), so the declared status is never the fresher answer.
SKILL_PREFIX = "skill."


def probe(cap_id: str) -> "tuple[bool, str]":
    """``(available, reason)`` for one capability. Never raises.

    Capabilities with no probe are ``(True, "")`` — unprobed is not the same as
    unavailable, and the registry's declaration stands for them.
    """
    fn = _PROBES.get(cap_id)
    if fn is None and str(cap_id).startswith(SKILL_PREFIX):
        # By PREFIX, not by entry: skill rows appear and disappear at run time,
        # so a table keyed on exact ids would have to be rewritten on every
        # install — and a row that slipped through unprobed would be a skill
        # reported live on a box that cannot run it.
        fn = lambda: _probe_skill(cap_id)  # noqa: E731
    if fn is None:
        return True, ""

    now = time.monotonic()
    with _lock:
        hit = _cache.get(cap_id)
        if hit is not None and hit[0] > now:
            return hit[1], hit[2]

    try:
        available, reason = fn()
    except Exception as exc:
        # Fail closed: see the module docstring. Name the type only — an
        # exception message can carry a path or a URL with data in it.
        available = False
        reason = (f"PuffPAI could not confirm this is available "
                  f"({type(exc).__name__}), so it is not claiming it is.")

    with _lock:
        _cache[cap_id] = (now + _TTL_SECONDS, available, reason)
    return available, reason


def status_for(cap_id: str, declared_status: str) -> str:
    """The status to REPORT for a capability: declared, unless a probe refutes it."""
    if (cap_id not in _STATUS_FOLLOWS_PROBE
            and not str(cap_id).startswith(SKILL_PREFIX)):
        return declared_status
    available, _reason = probe(cap_id)
    return declared_status if available else "offline"


def reset_cache() -> None:
    """Drop every cached probe answer. For tests and for a config reload."""
    with _lock:
        _cache.clear()
