"""Operational self-healing — orchestration Phase 5a.

Two halves, both offline:

* the Concierge ``service_degraded`` decider (classification table, dedup
  window, never-raises contract), and
* the Node-RED ``health_check`` tab that feeds it — parsed as data, so the wire
  graph, the untouched tabs and the security invariants are checked without a
  running Node-RED.

Run: pytest tests/test_self_healing.py -q
"""

from __future__ import annotations

import json
import os

import pytest

from puffpai.agents import Budget, Envelope
from puffpai.agents.caretaker import DEFAULT_SELF_UNIT, RESTARTABLE_UNITS
from puffpai.agents.concierge import (
    CORE_SERVICES,
    SERVICE_DEGRADED_DEDUP_SECONDS,
    ConciergeAgent,
    reset_service_degraded_dedup,
    service_key,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOWS_PATH = os.path.join(REPO_ROOT, "nodered_flows", "flows.json")
HEALTH_TAB = "598639b02b37a8d9"


# ── fixtures ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_dedup():
    reset_service_degraded_dedup()
    yield
    reset_service_degraded_dedup()


@pytest.fixture
def clock(monkeypatch):
    """A pinned monotonic clock the tests can advance."""
    state = {"t": 1000.0}
    monkeypatch.setattr("puffpai.agents.concierge._monotonic",
                        lambda: state["t"])
    return state


def _degraded(data: dict) -> Envelope:
    return Envelope(
        intent="event",
        payload={"source": "nodered", "event_type": "service_degraded",
                 "data": data},
        requester="system",
        origin_agent="nodered",
        budget=Budget(max_actions=2, max_seconds=15.0, allow_egress=False),
        state_changing=False,
    )


def _decide(data: dict) -> dict:
    reply = ConciergeAgent().handle(_degraded(data))
    assert reply["ok"] is True
    assert reply["event_type"] == "service_degraded"
    return reply["decision"]


def _payload(*down, overall="degraded"):
    return {"overall": overall, "down_count": len(down),
            "down_services": list(down),
            "services": [{"name": n, "status": "down"} for n in down],
            "checked_at": "2026-08-04T12:00:00.000Z"}


# ── classification table ────────────────────────────────────────────

CLASSIFICATION = [
    # down services                            severity     suggested_action
    (["PuffPAI API"],                          "attention", "needs_human"),
    (["Ollama LLM"],                           "attention", "needs_human"),
    (["PuffPAI Dashboard"],                    "attention", "restart_service"),
    (["Home Assistant"],                       "info",      "logged"),
    (["PuffPAI API", "Ollama LLM"],            "attention", "needs_human"),
    (["PuffPAI API", "Home Assistant"],        "attention", "needs_human"),
    (["PuffPAI Dashboard", "Home Assistant"],  "attention", "restart_service"),
    (["PuffPAI API", "Ollama LLM",
      "Home Assistant"],                       "attention", "needs_human"),
    (["Frobnicator"],                          "info",      "needs_human"),
    (["PuffPAI Dashboard", "Frobnicator"],     "attention", "needs_human"),
]


@pytest.mark.parametrize("down,severity,action", CLASSIFICATION)
def test_classification_table(down, severity, action):
    decision = _decide(_payload(*down))
    assert decision["severity"] == severity
    assert decision["suggested_action"] == action
    for name in down:
        assert name in decision["summary"]


def test_restart_suggestion_names_only_a_caretaker_allowlisted_unit():
    decision = _decide(_payload("PuffPAI Dashboard"))
    assert decision["unit"] == "puffpai-dashboard"
    assert decision["unit"] in RESTARTABLE_UNITS
    assert decision["unit"] != DEFAULT_SELF_UNIT


def test_self_restart_is_never_suggested_for_the_api():
    """puffpai-api is allowlisted but is the unit we run inside."""
    decision = _decide(_payload("PuffPAI API"))
    assert decision["suggested_action"] == "needs_human"
    assert decision["restartable_units"] == []
    assert "unit" not in decision


def test_decision_says_it_is_a_suggestion_not_an_action():
    decision = _decide(_payload("PuffPAI Dashboard"))
    assert decision["suggestion_only"] is True
    assert decision["action_taken"] is None
    assert "does not restart" in decision["note"]


def test_healthy_payload_is_info_and_logged():
    decision = _decide({"overall": "healthy", "down_count": 0,
                        "down_services": [], "services": []})
    assert decision["severity"] == "info"
    assert decision["suggested_action"] == "logged"
    assert decision["summary"] == "all monitored services are up"


def test_unnamed_degradation_asks_for_a_human():
    decision = _decide({"overall": "degraded", "down_count": 3,
                        "down_services": []})
    assert decision["severity"] == "info"
    assert decision["suggested_action"] == "needs_human"
    assert "none were named" in decision["summary"]


def test_peripheral_only_never_reaches_attention():
    decision = _decide(_payload("Home Assistant"))
    assert decision["peripheral_down"] == ["home-assistant"]
    assert decision["core_down"] == []
    assert decision["severity"] == "info"


def test_unknown_service_is_never_treated_as_restartable():
    decision = _decide(_payload("Frobnicator"))
    assert decision["unknown_down"] == ["Frobnicator"]
    assert decision["restartable_units"] == []
    assert "unit" not in decision


@pytest.mark.parametrize("name,key", [
    ("PuffPAI API", "puffpai-api"),
    ("puffpai api", "puffpai-api"),
    ("  Ollama LLM  ", "ollama"),
    ("Home Assistant", "home-assistant"),
    ("PuffPAI Dashboard", "puffpai-dashboard"),
    ("nope", ""),
    (None, ""),
    (42, ""),
])
def test_service_key_table(name, key):
    assert service_key(name) == key


def test_core_services_are_the_units_puffpai_owns():
    assert "home-assistant" not in CORE_SERVICES
    assert RESTARTABLE_UNITS <= CORE_SERVICES


# ── never raises ────────────────────────────────────────────────────

MALFORMED = [
    {},
    {"down_services": None},
    {"down_services": "PuffPAI API"},            # a bare string, not a list
    {"down_services": [None, "", 0]},
    {"down_services": [{"name": "x"}]},          # objects instead of names
    {"down_count": "lots", "down_services": ["PuffPAI API"]},
    {"down_count": -4, "down_services": []},
    {"overall": {"nested": True}, "down_services": ["Ollama LLM"]},
    {"down_services": ["x" * 5000]},
    {"down_services": ["svc"] * 500},
    {"down_services": {"a": 1}},
]


@pytest.mark.parametrize("data", MALFORMED)
def test_malformed_payloads_never_raise(data):
    decision = _decide(data)
    assert decision["severity"] in ("info", "attention")
    assert decision["suggested_action"] in ("logged", "restart_service",
                                            "needs_human")
    assert isinstance(decision["summary"], str)


def test_names_are_length_capped_in_the_summary():
    decision = _decide({"down_count": 1, "down_services": ["y" * 500]})
    assert len(decision["down_services"][0]) == 64


def test_list_is_capped_so_a_flood_cannot_grow_the_event_log():
    decision = _decide({"down_count": 500, "down_services": ["svc"] * 500})
    assert len(decision["down_services"]) == 12


def test_event_with_a_non_dict_data_still_classifies():
    envelope = Envelope(
        intent="event",
        payload={"source": "nodered", "event_type": "service_degraded",
                 "data": "not-a-dict"},
        requester="system", origin_agent="nodered",
        budget=Budget(max_actions=2, max_seconds=15.0, allow_egress=False),
        state_changing=False,
    )
    reply = ConciergeAgent().handle(envelope)
    assert reply["ok"] is True
    assert reply["decision"]["severity"] == "info"


def test_decider_makes_no_bus_calls():
    """Classification is deterministic: it spends no envelope budget."""
    class Recording:
        def __init__(self):
            self.sent = []

        def send(self, envelope, to):
            self.sent.append(to)
            return {"ok": True}

        def get(self, name):
            return object()

    bus = Recording()
    agent = ConciergeAgent()
    agent.bind_bus(bus)
    envelope = _degraded(_payload("PuffPAI API"))
    agent.handle(envelope)
    assert bus.sent == []
    assert envelope.budget.actions_used == 0


# ── dedup across a time window ──────────────────────────────────────

def test_identical_degradation_inside_the_window_is_deduplicated(clock):
    first = _decide(_payload("PuffPAI API"))
    assert first["duplicate"] is False
    assert first["severity"] == "attention"

    clock["t"] += 60.0
    repeat = _decide(_payload("PuffPAI API"))
    assert repeat["duplicate"] is True
    assert repeat["severity"] == "info"
    assert repeat["suggested_action"] == "logged"
    assert repeat["escalated_action"] == "needs_human"
    assert "already reported" in repeat["summary"]


def test_the_window_expires_and_the_fault_is_reported_again(clock):
    assert _decide(_payload("PuffPAI API"))["duplicate"] is False
    clock["t"] += SERVICE_DEGRADED_DEDUP_SECONDS + 1
    again = _decide(_payload("PuffPAI API"))
    assert again["duplicate"] is False
    assert again["severity"] == "attention"


def test_a_worsening_fault_is_never_suppressed(clock):
    _decide(_payload("Home Assistant"))
    clock["t"] += 60.0
    worse = _decide(_payload("Home Assistant", "PuffPAI API"))
    assert worse["duplicate"] is False
    assert worse["severity"] == "attention"
    assert worse["suggested_action"] == "needs_human"


def test_a_different_fault_is_never_suppressed(clock):
    _decide(_payload("Ollama LLM"))
    clock["t"] += 60.0
    other = _decide(_payload("PuffPAI Dashboard"))
    assert other["duplicate"] is False
    assert other["suggested_action"] == "restart_service"


def test_order_of_names_does_not_defeat_dedup(clock):
    _decide(_payload("PuffPAI API", "Home Assistant"))
    clock["t"] += 60.0
    assert _decide(_payload("Home Assistant", "PuffPAI API"))["duplicate"] is True


def test_recovery_clears_the_memory_so_a_relapse_is_reported(clock):
    assert _decide(_payload("PuffPAI API"))["duplicate"] is False
    clock["t"] += 60.0
    _decide({"overall": "healthy", "down_count": 0, "down_services": []})
    clock["t"] += 60.0
    relapse = _decide(_payload("PuffPAI API"))
    assert relapse["duplicate"] is False
    assert relapse["severity"] == "attention"


def test_dedup_table_does_not_grow_without_bound(clock):
    from puffpai.agents import concierge as mod

    for i in range(50):
        _decide(_payload(f"svc-{i}"))
        clock["t"] += 60.0
    # Everything older than the window has been evicted: 50 minutes of history
    # at one distinct fault a minute leaves at most the last 30.
    assert len(mod._degradation_seen) <= 31


def test_dedup_holds_across_concierge_instances(clock):
    ConciergeAgent().handle(_degraded(_payload("Ollama LLM")))
    clock["t"] += 60.0
    second = ConciergeAgent().handle(_degraded(_payload("Ollama LLM")))
    assert second["decision"]["duplicate"] is True


# ── flows.json integrity ────────────────────────────────────────────

@pytest.fixture(scope="module")
def flows():
    with open(FLOWS_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def by_id(flows):
    return {n["id"]: n for n in flows}


def test_flows_json_parses_and_ids_are_unique(flows):
    ids = [n["id"] for n in flows]
    assert len(ids) == len(set(ids))


def test_every_wire_target_exists(flows, by_id):
    for node in flows:
        for port, targets in enumerate(node.get("wires") or []):
            for target in targets:
                assert target in by_id, (
                    f"{node['id']} ({node.get('name')}) port {port} "
                    f"wires to missing node {target}")


def test_every_catch_scope_id_exists(flows, by_id):
    catches = [n for n in flows if n["type"] == "catch"]
    assert catches
    for node in catches:
        for target in node.get("scope") or []:
            assert target in by_id, f"catch {node['id']} scopes missing {target}"
            assert by_id[target].get("z") == node.get("z"), (
                f"catch {node['id']} scopes a node on another tab")


def test_every_node_lives_on_a_real_tab(flows, by_id):
    tabs = {n["id"] for n in flows if n["type"] == "tab"}
    for node in flows:
        if node["type"] != "tab":
            assert node.get("z") in tabs, f"{node['id']} is on no tab"


EXPECTED_TABS = [
    "calendar_sync", "daily_briefing", "daily_news", "daily_weather",
    "grocery_list", "health_check", "meal_planner", "media_now_playing",
    "network_scan", "block_device", "weather_alerts", "puffpai_voice",
    "good_morning", "leaving_home", "security_check",
]

EXPECTED_WEBHOOKS = [
    "/webhook/block-device", "/webhook/calendar-sync", "/webhook/daily-briefing",
    "/webhook/daily-news", "/webhook/daily-weather", "/webhook/good-morning",
    "/webhook/grocery-list", "/webhook/health-check", "/webhook/leaving-home",
    "/webhook/meal-planner", "/webhook/media-now-playing",
    "/webhook/network-scan", "/webhook/puffpai_voice", "/webhook/security-check",
    "/webhook/weather-alerts",
]


def test_the_fifteen_tabs_are_unchanged(flows):
    assert [n["label"] for n in flows if n["type"] == "tab"] == EXPECTED_TABS


def test_the_fifteen_webhook_paths_are_unchanged(flows):
    paths = sorted(n["url"] for n in flows if n["type"] == "http in")
    assert paths == sorted(EXPECTED_WEBHOOKS)
    assert len(paths) == 15


def test_only_the_health_check_tab_gained_nodes(flows):
    """Phase 5a touched one tab; the node census of the others is pinned."""
    counts: dict[str, int] = {}
    for node in flows:
        if node["type"] != "tab":
            counts[node["z"]] = counts.get(node["z"], 0) + 1
    tabs = {n["id"]: n["label"] for n in flows if n["type"] == "tab"}
    expected = {
        "calendar_sync": 7, "daily_briefing": 14, "daily_news": 7,
        "daily_weather": 13, "grocery_list": 7,
        # health_check: the 12 pre-Phase-5a nodes + inject, switch, prep, POST,
        # terminus.
        "health_check": 12 + 5,
        "meal_planner": 7, "media_now_playing": 10, "network_scan": 15,
        "block_device": 14, "weather_alerts": 12, "puffpai_voice": 9,
        "good_morning": 9, "leaving_home": 7, "security_check": 7,
    }
    assert {tabs[z]: c for z, c in counts.items()} == expected


# ── the health_check tab specifically ───────────────────────────────

def _tab_nodes(flows, tab=HEALTH_TAB):
    return [n for n in flows if n.get("z") == tab]


def test_the_manual_webhook_still_reaches_the_three_probes(flows, by_id):
    http_in = next(n for n in _tab_nodes(flows) if n["type"] == "http in")
    assert http_in["url"] == "/webhook/health-check"
    names = {by_id[t]["name"] for t in http_in["wires"][0]}
    assert names == {"prep PuffPAI check", "prep Ollama check",
                     "prep Home Assistant check"}


def test_the_cron_feeds_exactly_the_same_probes(flows, by_id):
    injects = [n for n in _tab_nodes(flows) if n["type"] == "inject"]
    assert len(injects) == 1
    inject = injects[0]
    assert inject["crontab"] == "*/5 * * * *"
    assert inject["once"] is False
    http_in = next(n for n in _tab_nodes(flows) if n["type"] == "http in")
    assert inject["wires"] == http_in["wires"]


def test_compile_feeds_the_respond_or_log_switch(flows, by_id):
    compile_node = next(n for n in _tab_nodes(flows)
                        if n.get("name") == "compile health report")
    switch = by_id[compile_node["wires"][0][0]]
    assert switch["type"] == "switch"
    assert switch["name"] == "respond or log"
    assert switch["property"] == "res"
    assert [r["t"] for r in switch["rules"]] == ["nnull", "null"]
    # nnull -> the http response node; null (cron) -> the agent-event branch.
    assert by_id[switch["wires"][0][0]]["type"] == "http response"
    assert by_id[switch["wires"][1][0]]["name"] == \
        "scheduled health check -> agent event"


def test_headers_are_cleared_before_every_http_response_node(flows, by_id):
    """Every path into a response node passes a ``delete msg.headers``.

    msg.headers still holds the OUTBOUND request headers (credentials, on the
    error path). Node-RED's ``http response`` node echoes msg.headers to the
    caller, so every backwards path from a response node must reach a function
    that deletes them before it reaches a source node.
    """
    tab = _tab_nodes(flows)
    responses = [n for n in tab if n["type"] == "http response"]
    assert responses

    predecessors: dict[str, list[dict]] = {}
    for node in tab:
        for targets in node.get("wires") or []:
            for target in targets:
                predecessors.setdefault(target, []).append(node)

    def clears(node):
        return (node["type"] == "function"
                and "delete msg.headers" in node.get("func", ""))

    def guarded(node, seen):
        """True when every backwards path from *node* hits a clearing function."""
        if clears(node):
            return True
        if node["id"] in seen:
            return True                      # a cycle cannot introduce headers
        seen = seen | {node["id"]}
        upstream = predecessors.get(node["id"]) or []
        assert upstream, (
            f"{node['id']} ({node.get('name')}) reaches an http response "
            "with no msg.headers clear on the way")
        return all(guarded(p, seen) for p in upstream)

    for response in responses:
        for pred in predecessors.get(response["id"]) or []:
            assert guarded(pred, set()), (
                f"a path into response {response['id']} never clears msg.headers")


def test_the_agent_event_post_node_is_configured_safely(flows, by_id):
    prep = next(n for n in _tab_nodes(flows)
                if n.get("name") == "scheduled health check -> agent event")
    post = by_id[prep["wires"][0][0]]
    assert post["type"] == "http request"
    assert post["senderr"] is True          # errors stay on the branch
    assert post["method"] == "use"          # method comes from msg.method
    assert post["url"] == ""                # url comes from msg.url
    assert post["headers"] == []
    terminus = by_id[post["wires"][0][0]]
    assert terminus["wires"] == [[]]        # the cron branch ends here


def test_the_scheduled_branch_only_posts_on_degradation(flows):
    prep = next(n for n in _tab_nodes(flows)
                if n.get("name") == "scheduled health check -> agent event")
    func = prep["func"]
    assert "down_count" in func
    assert "return null" in func            # healthy tick emits nothing
    assert 'event_type: "service_degraded"' in func
    assert 'source: "nodered"' in func
    # Config-domain URL + credential only; never from the request body.
    assert 'env.get("PUFFPAI_API_URL")' in func
    assert 'env.get("PUFFPAI_API_KEY")' in func
    assert "msg.payload.ha_url" not in func
    # Flow-side dedup so an unchanged fault is not re-posted every 5 minutes.
    assert "health_last_down" in func


def test_the_posted_services_are_trimmed_of_upstream_bodies(flows):
    prep = next(n for n in _tab_nodes(flows)
                if n.get("name") == "scheduled health check -> agent event")
    code = "\n".join(line for line in prep["func"].splitlines()
                     if not line.strip().startswith("//"))
    # `details` carries the raw HA/PuffPAI response bodies; it must not travel.
    assert "details" not in code
    assert "name:" in code and "status:" in code


def test_the_catch_scope_still_covers_the_three_probes(flows, by_id):
    catch = next(n for n in _tab_nodes(flows) if n["type"] == "catch")
    names = {by_id[t]["name"] for t in catch["scope"]}
    assert names == {"GET /health", "GET /api/tags", "GET HA /api/"}
