"""Zigbee pairing assistant — ask first, open second, then ask again.

The owner's story is "pair the new light bulbs when I restart them; see them,
and ask if I want to add them". That story contains **two** permission
questions, not one, and this module exists to keep them apart:

1. *May I open the pairing window?* An open window means any Zigbee device
   within radio range may join the network for its whole duration. The joiners
   cannot be enumerated in advance, so this is a physical-proximity decision a
   person makes, never a decision a model makes. It is asked as a Sentinel
   consent request (``action="zigbee_pair"``) and answered on one of the two
   surfaces that can carry a human's answer: the chat tool
   ``sentinel_approve_device`` or the dashboard route
   ``/sentinel/consent/requests/<entity_id>``.
2. *May I manage the bulb that just joined?* Joining the radio network is not
   consent to be actuated. Each newly seen entity gets its own
   ``action="control_device"`` request, and nothing here approves them — the
   owner does, on the same two surfaces.

Where the permit window can be opened from
------------------------------------------
Exactly one function opens it: :func:`complete_approved_pairing`, which is the
completion hook the two approval surfaces call after
:func:`puffpai.security.consent_requests.resolve` has recorded the human's
answer. It is the only place in the tree that names
:data:`puffpai.ha_bridge.HUMAN_CONFIRMED`, and the token is exactly what the
recorded approval attests.

"Only call it from the approval sites" would be a convention, and conventions
are not controls, so the constraint is also *enforced against the durable
record*: :func:`_recent_approval` re-reads the consent queue and refuses unless
it finds a ``zigbee_pair`` request that is ``status="approved"``, was resolved
by a human surface (:data:`HUMAN_RESOLVERS`), was resolved within
:data:`APPROVAL_FRESHNESS_SECONDS`, and has not already been spent. An
in-process caller that skips the approval flow therefore gets a refusal, not a
pairing window, and a stale approval cannot be replayed into a second window
later.

The Steward's ``zigbee_pair`` intent is marked state-changing but actuates
nothing: it calls :func:`request_pairing`, which only files the question. The
planner cannot reach either intent — neither is in the planning loop's
CATALOGUE, and neither is in the Concierge's ``RESOLVABLE_INTENTS`` (the menu
the LLM refinement path may pick from), so the only route in is a deterministic
phrase match on a human's own message.

The watcher
-----------
After the window opens, one bounded daemon thread diffs Home Assistant's entity
list. It only ever READS states, files consent requests, writes a report, and —
at most — *closes* the window early. It never actuates a device and never opens
anything. Its runtime is hard-capped at ``duration + GRACE_SECONDS``, and one
session runs at a time, so a second ``request_pairing`` while a window is open
reports the current status rather than opening a second one.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

# consent_requests is a low leaf (stdlib + statedir + agent_activity), so it is
# safe at module scope. puffpai.ha_bridge / puffpai.zigbee_manager are NOT:
# they reach config and the secrets provider, and this module is imported from
# the API server and the chat tools. They are imported at call time.
from puffpai import agent_activity as _activity
from puffpai.security import consent_requests as _creq
from puffpai.statedir import resolve_runtime_dir

logger = logging.getLogger(__name__)


# ── the two consent questions ───────────────────────────────────────

#: Synthetic target for the window itself. ``zha.permit`` names no entity, and
#: the consent queue keys on ``(entity_id, action)``; this is the same id
#: :data:`puffpai.zigbee_manager.PAIRING_WINDOW_TARGET` uses in its refusal
#: proposal, so a person reads one name for one thing across both surfaces.
PAIRING_ENTITY_ID = "zha.pairing_window"
PAIRING_LABEL = "Zigbee pairing window"

#: Question 1 — may the window open at all.
PAIRING_ACTION = "zigbee_pair"

#: Question 2 — may PuffPAI actuate a device that just joined. Deliberately the
#: same action name the rest of the appliance uses for "this device may be
#: controlled", so an approval here grants exactly that and nothing more.
DEVICE_CONSENT_ACTION = "control_device"

#: Stamped into each per-device request's context so the status view can tell a
#: bulb that arrived through pairing from one Sentinel blocked for other reasons.
PAIRING_SOURCE = "zigbee_pairing"

#: Only a human surface's answer counts. ``resolve(by=...)`` records who
#: answered; an agent-minted value is not in this set.
#:
#: ``user_via_voice`` is the deterministic SPOKEN answer: the
#: phrase rule in :mod:`puffpai.consent_voice`, applied by ``api_server``
#: before any model sees the turn. It is a human surface for the same reason
#: ``user_via_chat`` is — the owner's own words, matched literally, never a
#: model deciding to say yes on their behalf.
HUMAN_RESOLVERS = frozenset({"user_via_chat", "user_via_ui", "user_via_voice"})


# ── bounds ──────────────────────────────────────────────────────────

#: Long enough to walk to a lamp and flip it, short enough that an unattended
#: window is not a standing invitation.
DEFAULT_PAIRING_SECONDS = 120

#: ZHA's own ceiling (``zha.permit`` accepts 0–254). Restated here so a bad
#: context value is clamped before it reaches the manager's validator.
MAX_PAIRING_SECONDS = 254

#: How long the watcher outlives the window. A device that starts joining in
#: the last second still shows up as an entity a few seconds later.
GRACE_SECONDS = 15

#: State polling interval. HA caches states for 5 s anyway.
POLL_SECONDS = 4.0

#: Early close: once something has joined and nothing new has appeared for this
#: long, the window has done its job.
QUIET_CLOSE_SECONDS = 20.0

#: …but only bother when closing early actually saves meaningful exposure.
EARLY_CLOSE_MIN_REMAINING = 30.0

#: How many times the watcher may retry an early close that did not take.
#: Retrying only ever *reduces* exposure, but a close HA keeps refusing (the
#: admin-only ``zha.permit`` with no close script configured) must not turn
#: into one POST every poll for the life of the window.
MAX_EARLY_CLOSE_ATTEMPTS = 3

#: An approval older than this is not a mandate for a window opening now.
APPROVAL_FRESHNESS_SECONDS = 300.0

#: How long an UNANSWERED pairing question lingers before it auto-expires.
#: The "may I open the window?" ask dies fast: an
#: unanswered one is a standing invitation to open a radio, and its status line
#: otherwise nags forever; expiring it also means a week-old ask can no longer be
#: answered into a live window, because the approval surfaces only resolve
#: requests that are still open. Passed to
#: :func:`~puffpai.security.consent_requests.create_request` as ``ttl_seconds``;
#: expiry flips the request to ``status="expired"`` and records no denial.
WINDOW_REQUEST_TTL_SECONDS = 6 * 60 * 60            # 6 hours

#: The "may I manage this bulb?" ask can linger much longer: the device really
#: joined the network and really is sitting there unmanaged, so the reminder
#: stays useful: 30 days, then it expires quietly
#: (again, no denial recorded — the bulb just stops nagging).
DEVICE_REQUEST_TTL_SECONDS = 30 * 24 * 60 * 60      # 30 days

#: Domains a newly paired device must be in for the "may I manage it?" question
#: to be worth asking. Everything else (sensors, diagnostics) is reported but
#: not proposed for control.
ACTUATABLE_DOMAINS = frozenset({"light", "switch"})

#: The owner's own words are echoed back into a stored reason and a chat reply;
#: bound them.
MAX_REASON_CHARS = 200

REPORT_FILENAME = "zigbee_pairing_report.json"


# ── writable state location ─────────────────────────────────────────
#
# Same canon every other state writer uses (see puffpai.statedir): NEVER a
# package-relative path, because a hardened systemd unit mounts the code tree
# read-only and the report would silently vanish.

#: Repo root — three directory levels up from ``src/puffpai/zigbee_pairing.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: The shared ``<repo>/data`` default the rest of the state writers converged on.
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

_data_dir_cache: str | None = None


def _data_dir() -> str:
    """Directory holding the pairing report, resolved on first use and cached.

    Lazy on purpose: :func:`~puffpai.statedir.resolve_runtime_dir` imports
    :mod:`puffpai.config` at call time, and resolving at import would drag the
    secrets provider into this module's import graph.
    """
    global _data_dir_cache
    if _data_dir_cache is None:
        _data_dir_cache = resolve_runtime_dir(_DEFAULT_DATA_DIR)
    return _data_dir_cache


def report_path() -> str:
    """Absolute path of the pairing report. ``REPORT_PATH`` overrides (tests/ops)."""
    override = globals().get("REPORT_PATH")
    if isinstance(override, str) and override:
        return override
    return os.path.join(_data_dir(), REPORT_FILENAME)


def reset_cache() -> None:
    """Forget the resolved state dir. For tests that move ``$PUFFPAI_DATA_DIR``."""
    global _data_dir_cache
    _data_dir_cache = None


# ── session state ───────────────────────────────────────────────────
#
# One window at a time, per process. The lock guards the session dict and the
# spent-approval set; it is never held across a network call or a sleep.

_LOCK = threading.RLock()

#: ``None`` when no window is open. Otherwise the live session.
_session: dict | None = None

#: ``resolved_at`` stamps of approvals already spent on a window. An approval is
#: a one-shot mandate: without this, a fresh approval could be replayed into a
#: second window any time inside :data:`APPROVAL_FRESHNESS_SECONDS`.
_spent_approvals: set[str] = set()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0].strip().lower() if "." in entity_id else ""


def _clean_text(value, limit: int = MAX_REASON_CHARS) -> str:
    """A bounded, single-line rendering of text that came from outside.

    The owner's message reaches this module as free text and ends up in a
    persisted JSON reason and in a chat reply, so newlines and control
    characters are flattened and the length is capped.
    """
    text = "" if value is None else str(value)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    return text[:limit]


def _clamped_duration(value) -> int:
    """*value* as a permit duration, or :data:`DEFAULT_PAIRING_SECONDS`.

    The context this reads was written by :func:`request_pairing`, but it round
    trips through a JSON file on disk, so it is treated as untrusted: anything
    the manager's own validator rejects, and anything outside 1..254, falls back
    to the default rather than being coerced into a longer window.
    """
    from puffpai.zigbee_manager import validate_permit_duration

    try:
        seconds = validate_permit_duration(value)
    except Exception:
        return DEFAULT_PAIRING_SECONDS
    if not 1 <= seconds <= MAX_PAIRING_SECONDS:
        return DEFAULT_PAIRING_SECONDS
    return seconds


# ── piece 1: file the question ──────────────────────────────────────

def request_pairing(reason_text: str = "",
                    duration: int = DEFAULT_PAIRING_SECONDS) -> dict:
    """File the consent request for a pairing window. **Opens nothing.**

    This is what the Steward's ``zigbee_pair`` intent calls. It writes a
    question to the Sentinel consent queue and returns the sentence the owner
    should read; the window itself only opens later, from
    :func:`complete_approved_pairing`, and only after a human answers.

    A second call while a window is already open reports the live status
    instead of filing again — :func:`~puffpai.security.consent_requests.
    create_request` is already idempotent per ``(entity_id, action)``, but the
    owner should be told "it's open, go flip the bulbs", not asked twice.
    """
    seconds = _clamped_duration(duration)

    with _LOCK:
        if _live_session() is not None:
            status = pairing_status()
            status["already_open"] = True
            return status

    detail = _clean_text(reason_text)
    reason = (
        f"Opening the Zigbee pairing window lets ANY Zigbee device within radio "
        f"range of the house join the network for {seconds} seconds — the "
        f"joiners cannot be listed in advance, so this needs your say-so, not "
        f"mine. Nothing that joins becomes controllable until you approve it "
        f"separately.")
    script_note = _permit_script_note()
    if script_note:
        reason += script_note
    if detail:
        reason += f" (asked for: {detail})"

    req = _creq.create_request(
        entity_id=PAIRING_ENTITY_ID,
        label=PAIRING_LABEL,
        action=PAIRING_ACTION,
        reason=reason,
        requested_by="steward",
        context={"duration": seconds,
                 "conversation_id": _current_conversation()},
        ttl_seconds=WINDOW_REQUEST_TTL_SECONDS,
    )

    message = (
        f"I can open the Zigbee pairing window for {seconds} seconds, but that "
        f"lets any Zigbee device in range join, so it needs your approval "
        f"first. Say \"approve pairing\" here, or tap Approve on the dashboard "
        f"banner — then power-cycle the bulbs and I'll watch for them.")
    return {"ok": True, "executed": False, "awaiting_approval": True,
            "request": req, "duration": seconds, "message": message}


# ── piece 2: the completion hook (the ONLY opener) ──────────────────

def _recent_approval(preferred: dict | None = None) -> dict | None:
    """The durable record of a human approving a pairing window, or ``None``.

    This is the control behind "only the approval sites may open the window".
    It re-reads the consent queue rather than trusting its caller, so the
    mandate is a persisted, human-resolved, fresh, unspent record — not an
    argument someone passed in.

    *preferred* (the request dict the approval site just resolved) only breaks
    ties; it is matched against the queue and never used in its place.
    """
    now = datetime.now(timezone.utc)
    best: dict | None = None
    best_at = None
    for req in _creq._load():
        if not isinstance(req, dict):
            continue
        if req.get("action") != PAIRING_ACTION:
            continue
        if req.get("status") != "approved":
            continue
        if req.get("resolved_by") not in HUMAN_RESOLVERS:
            continue
        stamp = req.get("resolved_at")
        if not isinstance(stamp, str) or stamp in _spent_approvals:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if (now - when).total_seconds() > APPROVAL_FRESHNESS_SECONDS:
            continue
        if (preferred is not None
                and preferred.get("resolved_at") == stamp):
            return req
        if best_at is None or when > best_at:
            best, best_at = req, when
    return best


def _resolve_actuators(obj):
    """``(manager, bridge)`` from whatever the approval site had to hand.

    The two call sites hold an :class:`~puffpai.ha_bridge.HABridge`; tests hand
    in a double that is both. Anything that can open the window is a
    :class:`~puffpai.zigbee_manager.ZigbeeManager`, so the bridge is wrapped
    rather than actuated directly — that keeps the call on
    ``HABridge.call_service``, the choke point.
    """
    manager = obj if hasattr(obj, "open_permit_join") else None
    bridge = obj if hasattr(obj, "get_all_states") else None
    if bridge is None and manager is not None:
        bridge = getattr(manager, "_bridge", None)
    if bridge is None:
        from puffpai.ha_bridge import get_bridge
        bridge = get_bridge()
    if manager is None:
        from puffpai.zigbee_manager import ZigbeeManager
        manager = ZigbeeManager(bridge=bridge)
    return manager, bridge


def _entity_snapshot(bridge) -> dict[str, str]:
    """``{entity_id: friendly name}`` for everything Home Assistant knows."""
    try:
        states = bridge.get_all_states(force=True)
    except TypeError:                      # a double without the keyword
        states = bridge.get_all_states()
    snapshot: dict[str, str] = {}
    for raw in states or []:
        if not isinstance(raw, dict):
            continue
        entity_id = raw.get("entity_id")
        if not isinstance(entity_id, str) or "." not in entity_id:
            continue
        attrs = raw.get("attributes")
        name = attrs.get("friendly_name") if isinstance(attrs, dict) else None
        snapshot[entity_id] = _clean_text(name or entity_id, 120)
    return snapshot


def _live_session() -> dict | None:
    """The current session while its watcher is still inside its hard cap."""
    session = _session
    if session is None:
        return None
    if time.monotonic() >= session["watch_deadline"]:
        return None
    return session


def _is_admin_refusal(error) -> bool:
    """True when HA refused because the everyday account is not an admin."""
    text = str(error)
    lowered = text.lower()
    return ("401" in text or "403" in text
            or "unauthorized" in lowered or "forbidden" in lowered)


def _script_not_triggered(error) -> bool:
    """True when the permit script was accepted by HA but never actually ran.

    ``ZigbeeManager`` stamps the reason code into the error string so every
    surface that only carries the string — the activity feed, the spoken
    status — can still name this outcome.
    """
    try:
        from puffpai.zigbee_manager import SCRIPT_NOT_TRIGGERED
    except Exception:                              # pragma: no cover
        SCRIPT_NOT_TRIGGERED = "script_not_triggered"
    return SCRIPT_NOT_TRIGGERED in str(error)


def _refusal_message(error: str, *, proposed: bool = False) -> str:
    """Turn HA's raw ``open_permit_join`` error into plain words for a person.

    ``zha.permit`` is an admin-only HA service; the two failures we can name
    for certain are "the PuffPAI user isn't an administrator" (401/403) and
    "there is no ZHA integration to permit-join in the first place" (400/404
    or a "not found" message). Anything else keeps the raw error so nothing
    useful is thrown away.

    *proposed* says the self-heal ran: PuffPAI has filed two ``ha_provision``
    consent requests for the scoped permit scripts (see
    :func:`puffpai.ha_provisioning.propose_zigbee_permit_scripts`). The owner
    is then told what to approve, NOT told to promote the everyday account —
    that promotion is the thing the least-privilege hardening exists to avoid, and
    it is named here only to say we are deliberately not asking for it.
    """
    text = str(error)
    lowered = text.lower()
    if _script_not_triggered(text):
        # Not a refusal at all on the wire: HA answered 200 and did nothing,
        # because the script never loaded. Say exactly that instead of
        # dressing it up as a permission problem.
        try:
            from puffpai.zigbee_manager import SCRIPT_NOT_TRIGGERED_MESSAGE
            return SCRIPT_NOT_TRIGGERED_MESSAGE
        except Exception:                          # pragma: no cover
            return text
    if _is_admin_refusal(text):
        if proposed:
            return ("Home Assistant won't let my everyday account open the "
                    "pairing window. I've proposed two small scoped scripts in "
                    "Home Assistant that fix that permanently — approve them "
                    "on the dashboard with the vault unlocked, then ask me to "
                    "pair again. (I am deliberately NOT asking you to make my "
                    "everyday account an administrator in Home Assistant "
                    "Settings; those two scripts are all I need.)")
        return ("Home Assistant refused to open the pairing window: the "
                "PuffPAI user isn't an administrator in Home Assistant, and "
                "Zigbee pairing needs one (Settings › People › the PuffPAI "
                "user › Administrator).")
    if "400" in text or "404" in text or "not found" in lowered:
        return ("Home Assistant has no Zigbee integration (ZHA) set up, so "
                "there's no pairing window to open.")
    return f"I couldn't open the pairing window: {text}"


def _permit_script_note() -> str:
    """The clause telling the owner the window length may not be ours to set.

    Home Assistant refuses ``zha.permit`` to this appliance's non-admin user,
    so the window may be opened by triggering the household's own script — and
    then the length is whatever the administrator wrote into that script, not
    the number in the request. The owner is told that *before* approving,
    because it is the only number that will actually be in force.
    """
    try:
        from puffpai.zigbee_manager import permit_script_entity
        entity = permit_script_entity(closing=False)
    except Exception:
        return ""
    if not entity:
        return ""
    return (f" If Home Assistant refuses my own request (the PuffPAI user is "
            f"not an administrator there), I'll trigger your {entity} instead "
            f"— and then the window lasts exactly as long as that script says, "
            f"which is yours to set, not mine.")


def complete_approved_pairing(actuator=None, *, request: dict | None = None,
                              vault_session_id: str | None = None) -> dict:
    """Open the pairing window — the completion hook for an APPROVED request.

    Called from exactly two places, both of which have just recorded a human's
    answer with :func:`puffpai.security.consent_requests.resolve`:

    * ``assistant_tools.sentinel_approve_device`` (chat, ``by="user_via_chat"``)
    * ``api_server.sentinel_consent_resolve`` (dashboard, ``by="user_via_ui"``)

    That recorded answer is what :data:`~puffpai.ha_bridge.HUMAN_CONFIRMED`
    attests here, and this is the only function in the tree that presents the
    token for ``zha.permit``. It is not taken on trust: :func:`_recent_approval`
    re-reads the queue and refuses without a fresh, human-resolved, unspent
    ``zigbee_pair`` approval, so calling this from anywhere else opens nothing.

    Returns immediately after the window opens; the watcher runs in a bounded
    daemon thread.
    """
    global _session
    from puffpai.ha_bridge import HUMAN_CONFIRMED

    approval = _recent_approval(request)
    if approval is None:
        logger.warning("zigbee pairing: refused — no fresh human approval on "
                       "record for %s", PAIRING_ACTION)
        return {"ok": False, "opened": False,
                "error": "no approved Zigbee pairing request on record",
                "message": ("I can't open the pairing window — I don't have "
                            "your approval on record. Ask me to pair the bulbs "
                            "and approve the request, and I'll open it then.")}

    seconds = _clamped_duration((approval.get("context") or {}).get("duration"))

    with _LOCK:
        live = _live_session()
        if live is not None:
            status = pairing_status()
            status["already_open"] = True
            return status
        # Spend the mandate before anything can fail: a half-opened window that
        # left the approval reusable would be a replay hole.
        stamp = approval.get("resolved_at")
        if isinstance(stamp, str):
            _spent_approvals.add(stamp)

    manager, bridge = _resolve_actuators(actuator)

    try:
        baseline = _entity_snapshot(bridge)
    except Exception as exc:
        logger.warning("zigbee pairing: could not snapshot HA states: %s", exc)
        baseline = {}

    # Admin-direct first: with the vault open, ONE
    # fixed zha.permit call as the HA admin, so HA's own answer proves the
    # radio opened. Locked vault or a refusal falls back to the everyday path
    # (zha.permit, then the owner's permit script) exactly as before.
    admin = _admin_permit(seconds, vault_session_id, HUMAN_CONFIRMED)
    result = None
    if admin.get("ok"):
        result = {"success": True, "duration": seconds,
                  "duration_source": DURATION_SOURCE_ADMIN, "verified": True,
                  "script_entity": None, "last_triggered": None}
    elif admin.get("attempted"):
        logger.warning("zigbee pairing: admin-direct permit failed (%s); "
                       "falling back to the everyday path", admin.get("error"))
    try:
        if result is None:
            result = manager.open_permit_join(seconds,
                                              human_confirmed=HUMAN_CONFIRMED)
    except Exception as exc:
        logger.error("zigbee pairing: opening the window failed: %s", exc)
        return {"ok": False, "opened": False, "error": str(exc),
                "message": f"I couldn't open the pairing window: {exc}"}

    if not (isinstance(result, dict) and result.get("success")):
        error = (result or {}).get("error", "unknown error") \
            if isinstance(result, dict) else "unknown error"
        logger.warning("zigbee pairing: HA refused to open the window: %s", error)
        # Self-heal. Rather than end an admin-only refusal at an
        # explanation and a chore for the owner, PROPOSE the fix:
        # two single-line scripts that let the restricted everyday account open
        # and close the window. This only files consent requests — the owner
        # approves them with the vault unlocked and PuffPAI does the rest.
        proposed = False
        if _is_admin_refusal(error):
            try:
                from puffpai import ha_provisioning as _prov
                proposed = bool(_prov.propose_zigbee_permit_scripts().get("ok"))
            except Exception as exc:               # never break the refusal path
                logger.warning("zigbee pairing: could not propose the permit "
                               "scripts: %s", type(exc).__name__)
        reason = (result.get("reason") if isinstance(result, dict) else None)
        message = _refusal_message(error, proposed=proposed)
        _log("pairing_window_refused",
             f"Zigbee pairing window refused: {error}",
             "failed", {"error": error, "proposed_scripts": proposed,
                        "reason": reason})
        return {"ok": False, "opened": False, "error": error,
                "reason": reason,
                "proposed_scripts": proposed, "message": message}

    via_script = (result.get("duration_source") == "ha_script")
    script_entity = str(result.get("script_entity") or "") if via_script else ""
    if via_script:
        # The window is the script's, not ours. Size the bounded watch to the
        # script's fixed length so the watcher neither stops early (missing a
        # joiner it should be filing consent for) nor believes a window is
        # still open after HA closed it.
        seconds = _clamped_duration(result.get("duration"))
        logger.info("zigbee pairing: pairing script %s STARTED (%ss, fixed by "
                    "the script); the radio state is not confirmed",
                    script_entity, seconds)

    started_at = _utcnow()
    now = time.monotonic()
    session = {
        "started_at": started_at,
        "window_seconds": seconds,
        "window_deadline": now + seconds,
        "watch_deadline": now + seconds + GRACE_SECONDS,
        "approved_by": approval.get("resolved_by"),
        "opened_via": result.get("duration_source"),
        "conversation_id": ((request or {}).get("context") or {}).get(
            "conversation_id") or (approval.get("context") or {}).get(
            "conversation_id"),
    }
    with _LOCK:
        _session = session

    thread = threading.Thread(
        target=_watch, name="zigbee-pairing-watch", daemon=True,
        args=(manager, bridge, baseline, session))
    thread.start()
    session["thread"] = thread

    how = f" via HA script {script_entity}" if via_script else ""
    _log("pairing_window_opened",
         f"Zigbee pairing window open for {seconds}s{how} "
         f"(approved by {approval.get('resolved_by')})",
         "running", {"window_seconds": seconds,
                     "duration_source": result.get("duration_source"),
                     "script_entity": script_entity or None,
                     "verified": bool(result.get("verified")),
                     "last_triggered": result.get("last_triggered")})

    clear_reopen_offer()
    if via_script:
        # Honest wording: a moved last_triggered proves HA STARTED the script,
        # not that zha.permit inside it opened the radio.
        message = (
            f"I started your pairing script ({script_entity}). Home Assistant "
            f"ran it, but with the vault locked I can't confirm the radio "
            f"opened. If it did, it stays open for {seconds} seconds, so "
            f"power-cycle the bulbs now. I'll watch and tell you what joins.")
    elif result.get("duration_source") == DURATION_SOURCE_ADMIN:
        message = (
            f"The pairing window is open for {seconds} seconds; Home Assistant "
            f"accepted it. Power-cycle the bulbs now. I'll watch for anything "
            f"new and ask you before I manage it.")
    else:
        message = (
            f"Pairing window is open for {seconds} seconds. Power-cycle the "
            f"bulbs now. I'll watch for anything new and ask you before I "
            f"manage it.")
    return {"ok": True, "opened": True, "window_seconds": seconds,
            "started_at": started_at, "message": message,
            "status": "started" if via_script else "open",
            "duration_source": result.get("duration_source"),
            "script_entity": script_entity or None,
            "verified": bool(result.get("verified")),
            "last_triggered": result.get("last_triggered")}


# ── the watcher ─────────────────────────────────────────────────────

def _log(event: str, task: str, status: str, result: dict | None = None) -> None:
    """Append to the Agent Team activity feed. Never raises.

    Routed through :func:`puffpai.security.consent_requests.log_agent_activity`
    so the feed's path is resolved in one place (see
    :mod:`puffpai.agent_activity`) rather than re-derived here.
    """
    try:
        _creq.log_agent_activity("steward", event, task, status, result)
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("zigbee pairing: activity log failed: %s", exc)


def _file_device_consent(entity_id: str, name: str) -> bool:
    """Ask whether a newly paired device may be managed. Never auto-approves.

    Idempotent twice over: ``create_request`` returns the existing open request
    for a ``(entity_id, action)`` pair, and the watcher only calls this the
    first time it sees an entity.
    """
    try:
        _creq.create_request(
            entity_id=entity_id,
            label=name or entity_id,
            action=DEVICE_CONSENT_ACTION,
            reason="newly paired — allow PuffPAI to manage this device",
            requested_by="steward",
            context={"source": PAIRING_SOURCE},
            ttl_seconds=DEVICE_REQUEST_TTL_SECONDS,
        )
        return True
    except Exception as exc:
        logger.warning("zigbee pairing: could not file consent for %s: %s",
                       entity_id, exc)
        return False


def _watch(manager, bridge, baseline: dict[str, str], session: dict) -> None:
    """Diff HA's entity list for the life of the window. **Never raises.**

    Reads and files questions. It never actuates a device, and the only service
    call it can make is :meth:`~puffpai.zigbee_manager.ZigbeeManager.
    close_permit_join` — the ungated direction, because shortening exposure
    never needs permission.
    """
    global _session
    seen: dict[str, str] = {}
    filed: list[str] = []
    closed_early = False
    close_attempts = 0
    last_new = time.monotonic()

    try:
        while True:
            now = time.monotonic()
            if now >= session["watch_deadline"]:
                break
            time.sleep(min(POLL_SECONDS, max(0.1, session["watch_deadline"] - now)))

            try:
                current = _entity_snapshot(bridge)
            except Exception as exc:
                logger.debug("zigbee pairing: state read failed: %s", exc)
                continue

            for entity_id, name in current.items():
                if entity_id in baseline or entity_id in seen:
                    continue
                seen[entity_id] = name
                last_new = time.monotonic()
                logger.info("zigbee pairing: new entity %s (%s)", entity_id, name)
                if _domain(entity_id) in ACTUATABLE_DOMAINS:
                    if _file_device_consent(entity_id, name):
                        filed.append(entity_id)

            # Early close: something joined and the air has been quiet since.
            # Only ever *reduces* exposure, so it needs no confirmation.
            remaining = session["window_deadline"] - time.monotonic()
            if (seen and not closed_early
                    and close_attempts < MAX_EARLY_CLOSE_ATTEMPTS
                    and remaining >= EARLY_CLOSE_MIN_REMAINING
                    and (time.monotonic() - last_new) >= QUIET_CLOSE_SECONDS):
                close_attempts += 1
                try:
                    outcome = manager.close_permit_join()
                except Exception as exc:
                    logger.debug("zigbee pairing: early close failed: %s", exc)
                else:
                    # A close that came back unsuccessful must not set
                    # ``closed_early``, which would report a window shut that
                    # is in fact still open, and with ``zha.permit``
                    # admin-only, that is the *expected* failure, not a rare one.
                    if isinstance(outcome, dict) and not outcome.get("success"):
                        logger.warning(
                            "zigbee pairing: could not close the window early "
                            "(%s); it will close on its own timeout",
                            outcome.get("error"))
                    else:
                        closed_early = True
                        logger.info("zigbee pairing: closed the window early "
                                    "with %.0fs left (%d device(s) joined)",
                                    remaining, len(seen))
    except Exception as exc:                       # pragma: no cover - defensive
        logger.error("zigbee pairing watcher aborted: %s", exc)
    finally:
        # Entities that vanished before the window closed were renamed in HA
        # while pairing (a rename mints new entity ids); counting both would
        # turn two bulbs into "36 new device(s)".
        present = dict(seen)
        try:
            final = _entity_snapshot(bridge)
            if final:
                present = {e: final.get(e, n) for e, n in seen.items()
                           if e in final}
        except Exception as exc:
            logger.debug("zigbee pairing: final state read failed: %s", exc)
        devices = group_devices(present)
        report = {
            "started_at": session.get("started_at"),
            "window_seconds": session.get("window_seconds"),
            "finished_at": _utcnow(),
            "closed_early": closed_early,
            "opened_via": session.get("opened_via"),
            "new_devices": devices,
            "device_count": len(devices),
            "new_entities": [{"entity_id": eid, "name": seen[eid]}
                             for eid in sorted(seen)],
            "consent_filed": sorted(filed),
        }
        try:
            _write_report(report)
        except Exception as exc:
            logger.warning("zigbee pairing: could not write the report: %s", exc)
        _log("pairing_window_closed",
             f"Zigbee pairing finished: {len(devices)} new device(s) "
             f"({len(seen)} entities), {len(filed)} awaiting your approval",
             "completed",
             {"new_entities": [r["entity_id"] for r in report["new_entities"]],
              "consent_filed": report["consent_filed"]})
        try:
            _announce_result(devices, filed, session)
        except Exception as exc:  # pragma: no cover - speech is a courtesy
            logger.debug("zigbee pairing: announce failed: %s", exc)
        with _LOCK:
            if _session is session:
                _session = None


def close_report_text(devices: list, filed, session: dict | None = None) -> str:
    """The sentence the owner hears AND reads when a window closes."""
    if devices:
        n = len(devices)
        names = list(dict.fromkeys(devices))
        listed = (", ".join(names[:-1]) + " and " + names[-1]
                  if len(names) > 1 else names[0])[:180]
        extra = (" I've asked on the dashboard whether I should manage "
                 f"{'it' if n == 1 else 'them'}." if filed else "")
        return (f"The pairing window closed. {n} new "
                f"{'device' if n == 1 else 'devices'} joined: {listed}.{extra}")
    started_only = (session or {}).get("opened_via") == "ha_script"
    why = (" I only know the script started, not that the radio opened."
           if started_only else "")
    return ("The pairing window closed and nothing new joined." + why +
            " Reset the bulb the way its maker says, then say yes and I'll "
            "open it again.")


def _announce_result(devices: list, filed, session: dict | None = None) -> None:
    """Raise a hand about what the pairing window caught (or didn't).

    Proactive by design: spoken through :mod:`puffpai.announcer` (satellite or
    kiosk), AND posted into the chat thread the owner was using, because the
    kiosk may say it while the owner is at the bulbs and the chat they were
    typing in would otherwise stay silent. With nothing joined, the "say yes" offer is
    made answerable for :data:`REOPEN_OFFER_SECONDS`.
    """
    text = close_report_text(devices, filed, session)
    if not devices:
        _set_reopen_offer()
    try:
        from puffpai import announcer
        announcer.announce(text, source="zigbee_pairing")
    except Exception as exc:                       # pragma: no cover
        logger.debug("zigbee pairing: announcer failed: %s", exc)
    poster = _thread_poster
    if poster is not None:
        try:
            # The thread that STARTED the pairing; the poster falls back to
            # the most recent thread only when that one is unknown.
            poster(text, (session or {}).get("conversation_id"))
        except Exception as exc:
            logger.debug("zigbee pairing: thread post failed: %s",
                         type(exc).__name__)


def group_devices(entities: dict) -> list:
    """Count DEVICES, not entities. ``{entity_id: name}`` -> device names.

    Home Assistant names a device's entities "<device> <feature>", so a name
    that extends another name in the set belongs to that device. Two devices
    of the same model share a name; their count is the most entities with that
    exact name in any one domain (two bulbs = two ``light`` entities).
    """
    names = {n for n in entities.values() if n}
    roots = sorted(n for n in names
                   if not any(m != n and n.startswith(m + " ") for m in names))
    devices = []
    for root in roots:
        per_domain = {}
        for eid, name in entities.items():
            if name == root:
                dom = eid.split(".", 1)[0]
                per_domain[dom] = per_domain.get(dom, 0) + 1
        devices.extend([root] * max(1, max(per_domain.values(), default=1)))
    return devices


# ── hooks: chat-thread post, reopen offer, admin permit ─────────────

#: Set by api_server at import: ``poster(text)`` appends an assistant message
#: to the owner's most recent chat thread.
_thread_poster = None

#: How long "say yes and I'll open it again" stays answerable.
REOPEN_OFFER_SECONDS = 10 * 60
_reopen_offer_at = None

DURATION_SOURCE_ADMIN = "admin_direct"


def _current_conversation():
    """The conversation the current turn belongs to, or None."""
    try:
        from puffpai import turn_context
        return turn_context.current_conversation() or None
    except Exception:
        return None


def set_thread_poster(fn) -> None:
    global _thread_poster
    _thread_poster = fn


def _set_reopen_offer() -> None:
    global _reopen_offer_at
    with _LOCK:
        _reopen_offer_at = time.monotonic()


def reopen_offer_active() -> bool:
    with _LOCK:
        return (_reopen_offer_at is not None
                and time.monotonic() - _reopen_offer_at <= REOPEN_OFFER_SECONDS)


def clear_reopen_offer() -> None:
    global _reopen_offer_at
    with _LOCK:
        _reopen_offer_at = None


def _admin_permit(seconds: int, vault_session_id, confirmation) -> dict:
    """The admin-direct ``zha.permit``. Never raises; returns {} on error.

    *confirmation* is handed down by :func:`complete_approved_pairing`, the
    only function in this module allowed to name the token. There is no admin
    early close: HA closes the window on its own timer, and the everyday
    close path (the owner's close script) still runs.
    """
    try:
        from puffpai import ha_provisioning as _prov
        return _prov.admin_permit_join(int(seconds),
                                       human_confirmed=confirmation,
                                       vault_session_id=vault_session_id) or {}
    except Exception as exc:
        logger.debug("zigbee pairing: admin permit unavailable: %s",
                     type(exc).__name__)
        return {}


def _write_report(report: dict) -> None:
    path = report_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def read_report() -> dict | None:
    """The last pairing report, or ``None`` when there has never been one."""
    try:
        with open(report_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# ── piece 3: status ─────────────────────────────────────────────────

def pairing_status() -> dict:
    """Window state plus the last report, rendered for a person.

    Read-only: it opens nothing, closes nothing, and approves nothing.
    """
    with _LOCK:
        session = _live_session()
        window_open = False
        remaining = 0
        window_seconds = None
        started_at = None
        if session is not None:
            window_seconds = session.get("window_seconds")
            started_at = session.get("started_at")
            remaining = int(max(0.0, session["window_deadline"] - time.monotonic()))
            window_open = remaining > 0

    try:
        open_requests = _creq.list_open()
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("zigbee pairing: could not read the consent queue: %s", exc)
        open_requests = []

    awaiting = [
        {"entity_id": r.get("entity_id"), "label": r.get("label")}
        for r in open_requests
        if (r.get("context") or {}).get("source") == PAIRING_SOURCE
    ]
    pending_window = any(r.get("action") == PAIRING_ACTION
                         for r in open_requests)
    report = read_report()

    return {
        "ok": True,
        "window_open": window_open,
        "seconds_remaining": remaining,
        "window_seconds": window_seconds,
        "started_at": started_at,
        "pending_request": pending_window,
        "awaiting_approval": awaiting,
        "last_report": report,
        "last_outcome": (last_outcome := _last_pairing_outcome()),
        "message": _status_prose(window_open, remaining, pending_window,
                                 awaiting, report, last_outcome),
    }


_OUTCOME_EVENTS = ("pairing_window_opened", "pairing_window_closed",
                  "pairing_window_refused")


def _last_pairing_outcome() -> dict | None:
    """The most recent ``pairing_window_*`` activity entry, if any.

    Read-only and best-effort: a missing or unreadable feed yields ``None``
    rather than an error, matching :func:`pairing_status`'s own tolerance for
    a dead activity log.
    """
    try:
        path = _activity.get_activity_log_path()
        if not os.path.exists(path):
            return None
        last = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("agent") == "steward" and \
                        entry.get("event") in _OUTCOME_EVENTS:
                    last = entry
        return last
    except Exception as exc:                       # pragma: no cover - defensive
        logger.debug("zigbee pairing: could not read the activity feed: %s", exc)
        return None


def _status_prose(window_open: bool, remaining: int, pending_window: bool,
                  awaiting: list[dict], report: dict | None,
                  last_outcome: dict | None = None) -> str:
    if window_open:
        return (f"The pairing window is open for another {remaining} seconds — "
                f"power-cycle the bulbs now.")

    parts: list[str] = []
    if pending_window:
        parts.append("A pairing window is waiting on your approval — say "
                     "\"approve pairing\" and I'll open it.")

    if report is None:
        if last_outcome is not None and \
                last_outcome.get("event") == "pairing_window_refused":
            when = str(last_outcome.get("timestamp") or "")[11:16]
            result = last_outcome.get("result") or {}
            reason = result.get("error", "unknown error")
            lead = ("but the window never actually opened"
                    if _script_not_triggered(reason)
                    else "but Home Assistant refused to open the window")
            parts.append(
                f"Approved at {when}, {lead}: "
                f"{_refusal_message(reason, proposed=bool(result.get('proposed_scripts')))}")
        elif not parts:
            parts.append("I haven't run a Zigbee pairing window yet.")
        return " ".join(parts)

    found = report.get("new_entities") or []
    # DEVICES, not entities (one bulb is a light plus its energy sensors).
    # A report written before new_devices existed is grouped here.
    devices = report.get("new_devices")
    if not isinstance(devices, list):
        devices = group_devices({str(d.get("entity_id")): d.get("name")
                                 for d in found if isinstance(d, dict)})
    if not found or not devices:
        parts.append("The last pairing window found nothing new — the bulbs may "
                     "need another power cycle while the window is open.")
    else:
        n = len(devices)
        names = ", ".join(list(dict.fromkeys(devices))[:8])
        ids = ", ".join(str(d.get("entity_id")) for d in found[:8])
        more = f" (+{len(found) - 8} more)" if len(found) > 8 else ""
        parts.append(f"The last pairing window found {n} new "
                     f"{'device' if n == 1 else 'devices'}: {names} "
                     f"({ids}{more}).")

    if awaiting:
        labels = ", ".join(str(a.get("label") or a.get("entity_id"))
                           for a in awaiting[:8])
        parts.append(f"{len(awaiting)} of them are waiting for you to approve "
                     f"before I can manage them: {labels}. Say \"approve\" and "
                     f"name one, or use the dashboard banner.")
    elif found:
        parts.append("Nothing is waiting on your approval.")

    return " ".join(parts)


__all__ = [
    "ACTUATABLE_DOMAINS",
    "DEFAULT_PAIRING_SECONDS",
    "DEVICE_CONSENT_ACTION",
    "DEVICE_REQUEST_TTL_SECONDS",
    "PAIRING_ACTION",
    "PAIRING_ENTITY_ID",
    "PAIRING_LABEL",
    "PAIRING_SOURCE",
    "WINDOW_REQUEST_TTL_SECONDS",
    "complete_approved_pairing",
    "pairing_status",
    "read_report",
    "report_path",
    "request_pairing",
    "reset_cache",
]
