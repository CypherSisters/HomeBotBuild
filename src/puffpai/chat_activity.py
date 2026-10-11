"""Per-turn Agent Team attribution: which agent actually handled this chat turn.

Why this module exists
----------------------
``GET /agents/status`` derives each agent's row from the activity feed
(``agent-activity.jsonl``, owned by :mod:`puffpai.agent_activity`). The other
writers are the announcer, the Zigbee consent pair, ``/agents/event``
(Node-RED), the bridge, and the legacy ``cortex`` task queue; none of them
covers the CHAT dispatch path, so without this module most of the roster
agents would show as "never engaged" on the Agent Team page while they are in
fact answering turns all day.

This module records ONE ``turn_handled`` event per (turn, agent), appended
through the same :func:`puffpai.security.consent_requests.log_agent_activity`
writer the rest of the feed uses — same file, same resolution, same permissions.

PRIVACY RULE (non-negotiable)
-----------------------------
An activity event is a household-visible, on-disk, indefinitely retained record.
It therefore carries **no content whatsoever**: never the user's utterance,
never the reply text, never a tool argument, never a home entity name, never a
conversation id. Everything written here is drawn from a FIXED vocabulary that
exists in this file and in :mod:`puffpai.capability_registry` — capability ids,
route names, roster agent ids, a source enum, and an elapsed millisecond count.
There is no code path that copies a caller-supplied string into an event.
``tests/test_agent_activity_from_chat.py::test_no_pii_reaches_the_feed`` proves
it by driving a turn whose utterance and reply both contain a fake SSN and
asserting neither appears in the written JSONL.

Fail-soft
---------
The feed RECORDS what the agents did; it does not participate in doing it.
Every public entry point swallows its own exceptions and logs at debug, so a
broken registry, an unwritable feed, or a bad map entry can never change a reply
or raise into the chat path. Cost is bounded: no LLM calls, and the
tool/capability ownership maps are built once per process and cached.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import functools
import threading
import time

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: The agent every unattributable turn falls to. The Concierge is the router
#: that owns the turn when no capability does — this is a real answer, not a
#: placeholder, which is why nothing here ever invents a different owner.
DEFAULT_AGENT = "concierge"

#: The closed set of ``result["source"]`` values. ``fastpath`` means "a
#: deterministic pre-model handler answered it" — that covers the ``_try_*``
#: chain AND the two direct route handlers (briefing, library), which are reads
#: of a local store with no model in the loop.
SOURCES = ("fastpath", "agent_graph", "planner", "rag", "chitchat")

_SOURCE_LABEL = {
    "fastpath": "fastpath",
    "agent_graph": "agent graph",
    "planner": "planner",
    "rag": "rag",
    "chitchat": "chitchat",
}

#: ``_try_*`` fastpath -> the capability registry row that owns it. The OWNER
#: comes from the registry (``Capability.agent``), never from this table — this
#: table only says which row a fastpath belongs to, so re-homing a capability
#: in the registry re-homes its fastpath with it.
FASTPATH_CAPABILITY = {
    "_try_web_lookup": "utility.websearch",
    "_try_capability_lookup": "introspection.capabilities",
    "_try_capabilities": "introspection.capabilities",
    "_try_weather_alerts": "weather.alerts",
    "_try_weather_answer": "weather.current",
    "_try_maps_directions": "maps.directions",
    "_try_timer_set": "calendar.timers",
    "_try_camera_alerts": "cameras.alerts",
    "_try_calendar_delete": "calendar.tasks",
    "_try_calendar_recurring": "calendar.tasks",
    "_try_add_task": "calendar.tasks",
    "_try_med_fastpath": "health.meds",
    "_try_med_log": "health.meds",
    "_try_shopping_add": "pantry.shopping",
    "_try_shopping_remove": "pantry.shopping",
    "_try_climate_set": "home.control",
    "_try_meal_add": "food.mealplan",
    "_try_meal_plan": "food.mealplan",
    "_try_list_agents": "agents.roster",
    "_try_ask_agent": "agents.task",
    "_try_dashboard_manage": "dashboard.widgets",
    "_try_vault_open": "vault.status",
    "_try_vault_pin_followup": "vault.display",
    "_try_fitness_delete": "fitness.training",
    "_try_workout_log": "fitness.training",
    "_try_fitness_program": "fitness.training",
    "_try_media_show": "display.show",
    "_try_show": "display.show",
    "_try_calculate": "utility.calculate",
    "_try_untargeted_house_command": "home.control",
    "_try_zone3_enforce": "security.zone3",
    "_try_now_playing": "streaming.spotify",
    "_try_tunein_radio": "streaming.radio",
    "_try_radio_sources": "streaming.radio",
    "_try_media_play": "media.library",
    "_try_tv_stop": "media.library",
    "_try_media_stop": "media.library",
    "_try_media_volume": "media.library",
    "_try_media_library_query": "media.library",
    # "next"/"skip" reaches whatever is playing, the same owner as stop.
    "_try_media_skip": "media.library",
    "_try_taste_playlist": "media.playlists",
    "_try_radio_extras": "streaming.radio",
    "_try_books": "books.library",
    # "can you see the file I sent you?" is answered from the uploads.
    "_try_my_files": "knowledge.documents",
    "_try_trivia_start": "learning.trivia",
    "_try_document_read": "knowledge.documents",
    "_try_latest_upload_read": "knowledge.documents",
    # naming an upload, and answering the offer Puff spoke about one.
    # Both act on the household's own documents — knowledge.documents owns them.
    "_try_rename_upload": "knowledge.documents",
    "_try_upload_offer_reply": "knowledge.documents",
    "_try_daily_briefing": "briefing.daily",
    "_try_device_count": "home.devices",
    "_try_doors_locked": "home.devices",
    "_try_zigbee_pair_status": "home.onboarding",
    # "yes, open it again" after an empty pairing window files a new pairing
    # request, so it belongs to the pairing row. "Do you see the bulbs" is a
    # read of live device state, the same row as the device count.
    "_try_pairing_reopen": "home.onboarding",
    "_try_see_lights": "home.devices",
    # A new automation is proposed through the ha_provision consent queue, and
    # the PIN reply applies those approved changes with the admin key from the
    # vault. Both belong to the row that proposes and applies HA changes, the
    # way the vault display's PIN reply is credited to vault.display.
    "_try_automation_author": "home.provisioning",
    "_try_ha_change_pin_followup": "home.provisioning",
    # The spoken approval hook shipped without a row here, so the
    # attribution probe had nothing to credit it to. It mirrors
    # sentinel_consent_resolve one for one, which is security.consent.
    "_try_consent_approval": "security.consent",
    "_try_code_request": "dev.code",
    "_try_registry_read": "utility.registry_read",
    # Asks for the name, the dose and the time; writes nothing.
    "_try_med_schedule_ask": "health.meds",
    # the household roster is a capability of its own -- one
    # store, read and written from voice, chat and the dashboard.
    "_try_household_add": "home.household",
    "_try_household_list": "home.household",
    # the skills tier. Listing, disabling and removing a skill are the
    # household's own management of what it approved -- one row, skills.manage.
    # A skill ANSWERING is attributed the same way rather than to its own
    # skill.<id> row, because the feed is a record of who handled the turn and
    # the Toolsmith is the agent that owns every sandboxed run.
    "_try_skills_list": "skills.manage",
    "_try_skill_dispatch": "skills.manage",
    "_try_skill_remove": "skills.manage",
    "_try_skill_disable": "skills.manage",
    "_try_skill_enable": "skills.manage",
    # The honest capability gap is the registry describing itself, so it is
    # credited to the same row as "what can you do".
    "_try_capability_gap": "introspection.capabilities",
}

#: Fastpaths that CHANGE something the household can check afterwards. Their
#: (evidence-backed) confirmation sentence is kept by
#: :mod:`puffpai.turn_context` so "what did you just do" can repeat what
#: actually happened instead of describing the appliance from a template.
#:
#: Without it, two minutes after setting a thermostat and adding oat milk to
#: the shopping list, "what did you just do / did you change something" can
#: be answered "I didn't change anything ... I can't directly control devices
#: or run scripts": two false sentences in one reply.
#:
#: Recorded HERE rather than at each write site on purpose: the probe already
#: wraps every fastpath and already sees its return value, so one hook covers
#: every write, present and future, and no write handler has to remember.
#: Fastpaths that ANSWER QUESTIONS ABOUT the previous turn. They must never
#: become the previous turn themselves, or "where did you find that" asked
#: twice cites itself.
SELF_REFERENTIAL_FASTPATHS = frozenset({
    "_try_source_citation",
    "_try_recent_activity",
})

WRITE_FASTPATHS = frozenset({
    "_try_climate_set",
    "_try_shopping_add",
    "_try_shopping_remove",
    "_try_meal_add",
    "_try_meal_plan",
    "_try_add_task",
    "_try_calendar_recurring",
    "_try_timer_set",
    "_try_alarm_set",
    "_try_med_fastpath",
    "_try_household_add",
    # a rename changes what a document is called, which the household
    # can check in the library list.
    "_try_rename_upload",
})

#: Deliberately ABSENT: the delete fastpaths (``_try_calendar_delete``,
#: ``_try_fitness_delete``) and the whole-house gate. Each of those can answer
#: with a QUESTION ("which of the two do you mean?", "whose workout?") or a
#: refusal, and a question recorded as an action is exactly the kind of false
#: account this record exists to prevent. They earn a place here once they
#: report their outcome separately from their prose.
#:
#: The members of this set have the same problem: the thermostat fastpath
#: can answer "Which thermostat do you mean: Upstairs, Living Room?", which is
#: not something it did. The sentence is classified as it is stored
#: (``turn_context.outcome_kind``: write | read | ask) and "what did you just
#: do" reads only the first two, so a write fastpath that happens to ask is not
#: an account of a deed.

#: Fastpaths that answer a real turn but have NO registry row to own them. They
#: are attributed to the Concierge rather than to a guessed owner.
#:
#: TODO: give these registry rows (or map them to
#: existing ones) so the Agent Team page credits the right agent instead of the
#: Concierge. Until then, do NOT invent owners here.
#:
#:   * ``_try_time_answer``        — the clock. No capability row exists.
#:   * ``_try_alarm_clarify``      — alarms. ``calendar.timers`` is TIMERS; the
#:   * ``_try_alarm_set``            alarm scheduler is a separate subsystem
#:   * ``_try_alarm_dismiss``        (``puffpai.alarm_scheduler``) with no row.
#:   * ``_try_household_read``     — a MULTI-domain reader (bills, pantry,
#:                                   shopping, ...); one row cannot describe it.
#:                                   Mapping its reader names onto capability
#:                                   ids is the real fix.
#:   * ``_try_voice_fragment_guard`` — a guard, not a capability.
#:   * ``_try_capability_read``    — dispatches DYNAMICALLY by tool name and
#:                                   reports its own capability id at the call
#:                                   site (see ``record_capability``); this
#:                                   entry is only the fallback when the tool
#:                                   could not be resolved back to a row.
#:   * ``_try_source_citation``   — answers ABOUT the previous turn (where did
#:   * ``_try_recent_activity``      you find that / what did you just do).
#:                                   Neither reads a capability; they read the
#:                                   appliance's own record of the last turns.
UNMAPPED_FASTPATHS = frozenset({
    "_try_source_citation",
    "_try_recent_activity",
    #   * ``_try_automations_read`` — a UNION of HA, the task store and
    #                                 Node-RED; no single row describes it.
    #   * ``_try_system_health``    — the caretaker probe; ops, not a
    #                                 household capability.
    "_try_automations_read",
    "_try_system_health",
    "_try_time_answer",
    "_try_alarm_clarify",
    "_try_alarm_set",
    "_try_alarm_dismiss",
    "_try_household_read",
    "_try_voice_fragment_guard",
    "_try_capability_read",
    #   * ``_try_pending_clarifier`` — completes whatever capability ASKED
    #                                 the question it is answering (a calendar
    #                                 delete, an alarm, a thermostat, a
    #                                 medication). It has no capability of its
    #                                 own, and the hook it finishes is the one
    #                                 that should be credited.
    "_try_pending_clarifier",
    #   * ``_try_app_fastpath``     answers for whichever declarative app's
    #                                 manifest matched, and those apps have no
    #                                 registry rows of their own.
    "_try_app_fastpath",
})

#: ``_try_chitchat`` answers, but as small talk rather than as a capability.
_CHITCHAT_FASTPATHS = frozenset({"_try_chitchat"})

#: Routes whose direct handler is a deterministic local read, and the capability
#: row that describes it. Used only when nothing else claimed the turn.
_ROUTE_CAPABILITY = {
    "briefing": "briefing.daily",
    "library": "knowledge.library",
}

_LOCAL = threading.local()


# ── flag ────────────────────────────────────────────────────────────

def enabled() -> bool:
    """Whether chat turns are recorded into the Agent Team feed.

    Fails CLOSED on a broken config: a feed entry is never worth a traceback.
    """
    try:
        from puffpai.config import get_config
        return bool(get_config().agents.activity_from_chat)
    except Exception as exc:
        logger.debug("activity_from_chat flag unavailable: %s", exc)
        return False


# ── ownership maps (built once, cached) ─────────────────────────────

_MAPS_LOCK = threading.Lock()
_maps: dict[str, dict[str, str]] | None = None


def _ownership() -> dict[str, dict[str, str]]:
    """``{"cap": {capability_id: agent}, "tool": {tool_name: capability_id}}``.

    Built from :mod:`puffpai.capability_registry` ONCE per process — a chat turn
    must never rebuild the registry. :func:`reset_caches` drops it for tests.
    """
    global _maps
    if _maps is not None:
        return _maps
    cap: dict[str, str] = {}
    tool: dict[str, str] = {}
    try:
        from puffpai import capability_registry as registry
        for row in registry.CAPABILITIES:
            if row.agent:
                cap[row.id] = str(row.agent)
            for name in (row.all_tools or ()):
                # First row to claim a tool owns it; the registry treats
                # ``authority_tool`` as the single owner, and it is listed first.
                tool.setdefault(str(name), row.id)
    except Exception as exc:
        logger.debug("capability registry unavailable for attribution: %s", exc)
    built = {"cap": cap, "tool": tool}
    with _MAPS_LOCK:
        if _maps is None:
            _maps = built
        return _maps


def reset_caches() -> None:
    """Drop the cached ownership maps (tests that rebuild the registry)."""
    global _maps
    with _MAPS_LOCK:
        _maps = None


def owner_of_capability(capability_id: str | None) -> str:
    """The roster agent that owns ``capability_id``, or the Concierge."""
    if not capability_id:
        return DEFAULT_AGENT
    return _ownership()["cap"].get(capability_id) or DEFAULT_AGENT


def capability_of_tool(tool_name: str | None) -> str | None:
    """The capability id whose row declares ``tool_name``, if any."""
    if not tool_name:
        return None
    return _ownership()["tool"].get(str(tool_name))


# ── turn scope ──────────────────────────────────────────────────────

class _Turn:
    """One chat turn's attribution, keyed by agent so it cannot double-count."""

    __slots__ = ("t0", "records", "source_hint")

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.records: dict[str, tuple[str | None, str]] = {}
        self.source_hint: str | None = None

    def add(self, agent: str, capability_id: str | None, source: str) -> None:
        # One event per (turn, agent): the FIRST claim wins, so a compound turn
        # whose two clauses both land on the Steward writes one Steward line.
        self.records.setdefault(agent, (capability_id, source))


def _stack() -> list:
    stack = getattr(_LOCAL, "stack", None)
    if stack is None:
        stack = []
        _LOCAL.stack = stack
    return stack


def _current() -> "_Turn | None":
    stack = _stack()
    return stack[-1] if stack else None


def begin_turn() -> None:
    """Open a turn scope. Nested calls (compound clauses) JOIN the outer turn."""
    try:
        _stack().append(_Turn() if not _stack() else _stack()[-1])
    except Exception as exc:  # pragma: no cover - defence in depth
        logger.debug("begin_turn failed: %s", exc)


def end_turn(route: str) -> None:
    """Close a turn scope; write the feed entries when the OUTERMOST one closes."""
    try:
        stack = _stack()
        if not stack:
            return
        turn = stack.pop()
        if stack:  # an inner clause closed; the outer turn still owns the write
            return
        _write(turn, route)
    except Exception as exc:  # never raise into the chat path
        logger.debug("end_turn failed: %s", exc)


# ── recording ───────────────────────────────────────────────────────

def record_capability(capability_id: str | None, source: str = "fastpath") -> None:
    """Attribute this turn to the agent that owns ``capability_id``."""
    try:
        turn = _current()
        if turn is None:
            return
        turn.add(owner_of_capability(capability_id), capability_id, source)
    except Exception as exc:
        logger.debug("record_capability failed: %s", exc)


def record_fastpath(fastpath_name: str) -> None:
    """Attribute this turn to the capability behind a winning ``_try_*`` hook."""
    try:
        if fastpath_name in _CHITCHAT_FASTPATHS:
            record_capability(None, "chitchat")
            return
        record_capability(FASTPATH_CAPABILITY.get(fastpath_name), "fastpath")
    except Exception as exc:
        logger.debug("record_fastpath failed: %s", exc)


def record_tools(tool_names, source: str = "agent_graph") -> None:
    """Attribute this turn to every agent whose tool actually RAN.

    ``tool_names`` is a list of tool NAMES only — the agent graph's execution
    record also holds result snippets, and those must never reach this module.
    """
    try:
        turn = _current()
        if turn is None:
            return
        for name in (tool_names or ()):
            if not isinstance(name, str):
                continue
            capability_id = capability_of_tool(name)
            turn.add(owner_of_capability(capability_id), capability_id, source)
    except Exception as exc:
        logger.debug("record_tools failed: %s", exc)


def record_source(source: str) -> None:
    """Note HOW the turn was answered when no capability claimed it."""
    try:
        turn = _current()
        if turn is not None and turn.source_hint is None and source in SOURCES:
            turn.source_hint = source
    except Exception as exc:  # pragma: no cover - defence in depth
        logger.debug("record_source failed: %s", exc)


# ── the write ───────────────────────────────────────────────────────

def _task_label(capability_id: str | None, source: str) -> str:
    """A short FIXED label. Built only from a capability id and the source enum."""
    subject = capability_id or "general Q&A"
    return f"{subject} via {_SOURCE_LABEL.get(source, source)}"


def _write(turn: "_Turn", route: str) -> None:
    if not enabled():
        return
    try:
        from puffpai.security.consent_requests import log_agent_activity
    except Exception as exc:
        logger.debug("activity writer unavailable: %s", exc)
        return

    # ``route`` is one of the router's own fixed names; anything else is
    # dropped rather than written, so a caller cannot smuggle text through it.
    route_name = route if route in ("briefing", "agent", "library", "rag") else "rag"
    elapsed_ms = int((time.monotonic() - turn.t0) * 1000)

    records = dict(turn.records)
    if not records:
        capability_id = _ROUTE_CAPABILITY.get(route_name)
        source = ("fastpath" if capability_id
                  else (turn.source_hint or ("planner" if route_name == "agent"
                                             else "rag")))
        records = {owner_of_capability(capability_id): (capability_id, source)}

    for agent, (capability_id, source) in records.items():
        try:
            log_agent_activity(
                agent, "turn_handled",
                _task_label(capability_id, source), "completed",
                {"route": route_name,
                 "capability_id": capability_id,
                 "ms": elapsed_ms,
                 "source": source if source in SOURCES else "rag"},
            )
        except Exception as exc:  # the writer already swallows; belt and braces
            logger.debug("activity write failed for %s: %s", agent, exc)


# ── fastpath probes ─────────────────────────────────────────────────

def probe(name: str, func):
    """Wrap one ``_try_*`` fastpath so a TRUTHY return records its owner.

    The wrapper is transparent: same signature, same return value, and
    :func:`functools.wraps` keeps ``__wrapped__`` so
    ``inspect.getsource``/``ast`` introspection (which is how
    ``tests/test_instant_answer_precedence.py`` pins the dispatcher's hook
    order) still sees the original function.
    """

    @functools.wraps(func)
    def _probed(*args, **kwargs):
        result = func(*args, **kwargs)
        if result:
            record_fastpath(name)
            try:
                from puffpai import turn_context
                if name not in SELF_REFERENTIAL_FASTPATHS:
                    # WHAT answered this turn, so the next turn's "where did
                    # you find that" can say "a direct reading of X" instead of
                    # running a search. The two fastpaths that READ this record
                    # are excluded: they would otherwise overwrite the very
                    # turn they exist to describe.
                    turn_context.note_answer("fastpath", name)
                if name in WRITE_FASTPATHS and isinstance(result, str):
                    turn_context.note_action(result)
            except Exception as exc:       # never let a note change a reply
                logger.debug("turn-context note failed for %s: %s", name, exc)
        return result

    _probed.__puffpai_activity_probe__ = name
    return _probed


def install_probes(module, dispatcher: str = "_try_instant_answer") -> list[str]:
    """Wrap every ``_try_*`` the dispatcher calls. Returns the names wrapped.

    The list is READ FROM the dispatcher's own source rather than hand-kept, so
    a fastpath added to the chain is attributed the day it lands (falling to the
    Concierge with a TODO until it gets a registry row) instead of silently
    going unrecorded — which is the exact failure this whole module exists to
    fix. This is ONE hook, not one edit per fastpath: the dispatcher's body is
    not touched, so its pinned call order is unchanged.
    """
    wrapped: list[str] = []
    try:
        import ast
        import inspect
        import re

        src = inspect.getsource(getattr(module, dispatcher))
        src = re.sub(r"^ +", "", src, count=1) if src.startswith(" ") else src
        tree = ast.parse(src)
        names: list[str] = []
        for node in ast.walk(tree.body[0]):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (func.id if isinstance(func, ast.Name)
                      else func.attr if isinstance(func, ast.Attribute) else None)
            if called and called.startswith("_try_") and called not in names:
                names.append(called)
        for called in names:
            target = getattr(module, called, None)
            if not callable(target) or hasattr(target, "__puffpai_activity_probe__"):
                continue
            setattr(module, called, probe(called, target))
            wrapped.append(called)
    except Exception as exc:  # attribution is never worth a boot failure
        logger.debug("fastpath probe install failed: %s", exc)
    return wrapped


__all__ = [
    "DEFAULT_AGENT",
    "FASTPATH_CAPABILITY",
    "SOURCES",
    "UNMAPPED_FASTPATHS",
    "begin_turn",
    "capability_of_tool",
    "enabled",
    "end_turn",
    "install_probes",
    "owner_of_capability",
    "probe",
    "record_capability",
    "record_fastpath",
    "record_source",
    "record_tools",
    "reset_caches",
]
