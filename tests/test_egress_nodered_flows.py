"""The Node-RED flows ask their Bridge switch before they reach the internet.

Node-RED opens these connections itself, so the registry's request wrapper is
not on the path. Each function node in ``nodered_flows/flows.json`` that is
about to call ``api.weather.gov`` or the feed in ``NEWS_RSS_URL`` first asks
the backend ``GET /system/egress/door/<id>`` and skips the call unless the
answer is a clear yes.

Two layers are checked here:

* **Static.** Every function node that names one of those destinations also
  carries the switch check for the right switch, loads the ``http``/``https``
  modules the check uses, and sits in its tab's catch scope so a refused
  webhook run is answered instead of left hanging. The set of such nodes is
  pinned exactly.
* **Behaviour.** When ``node`` is on the PATH, each edited function body is run
  the way Node-RED runs it (an async function in a ``vm`` context) against a
  local stand-in for the backend: a clear yes passes the message on; a closed
  switch, a 401, an unreadable or ambiguous answer, and an unreachable backend
  all stop it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / "nodered_flows" / "flows.json"

#: Text in a function body that means "this node is about to go out", and the
#: switch that covers it. ``props.forecast`` is the forecast address the points
#: answer hands back, which is also api.weather.gov.
_MARKERS = {
    "api.weather.gov": "weather",
    "props.forecast": "weather",
    "NEWS_RSS_URL": "news_rss",
}

#: node id -> switch. The whole list, so a new outbound node cannot slip in
#: without a decision here.
GATED = {
    "9e7d610ef057c0f5": "weather",   # daily_briefing: prep NWS points
    "56350936f883a697": "weather",   # daily_briefing: prep forecast
    "0394af3f452f57ef": "news_rss",  # daily_briefing: prep news feed
    "47ece2902994315a": "news_rss",  # daily_news: prep feed request
    "44fa3c7c50affd34": "weather",   # daily_weather: prep NWS points
    "be74868c5dbe3da7": "weather",   # daily_weather: prep forecast
    "9e14669ee0905f05": "weather",   # daily_weather: prep active alerts
    "52e7b3fb0849b21b": "weather",   # weather_alerts: prep NOAA alerts
}


@pytest.fixture(scope="module")
def flows():
    return json.loads(FLOWS.read_text(encoding="utf-8"))


def _functions(flows):
    return [n for n in flows if n.get("type") == "function"]


def _switch_for(func: str) -> set:
    return {door for marker, door in _MARKERS.items() if marker in func}


# ── static ───────────────────────────────────────────────────────────────────

def test_every_outbound_function_node_is_the_gated_list(flows):
    outbound = {n["id"]: _switch_for(n.get("func") or "")
                for n in _functions(flows) if _switch_for(n.get("func") or "")}
    assert outbound == {k: {v} for k, v in GATED.items()}


@pytest.mark.parametrize("node_id, door", sorted(GATED.items()))
def test_the_node_asks_its_own_switch(flows, node_id, door):
    node = {n["id"]: n for n in flows}[node_id]
    func = node["func"]
    assert f'}})("{door}");' in func, f"{node['name']} does not ask {door}"
    assert '"/system/egress/door/" + doorId' in func
    assert "if (!bridge.open) {" in func
    # The ask comes before the message is handed on to the http request node.
    assert func.index("askBridge") < func.rindex("return msg;")
    # The key goes only to PUFFPAI_API_URL, never to a host from the request.
    assert 'env.get("PUFFPAI_API_KEY")' in func
    assert "req_body.api_key" not in func.split("askBridge", 1)[1]
    assert {"var": "bridgeHttp", "module": "http"} in node["libs"]
    assert {"var": "bridgeHttps", "module": "https"} in node["libs"]


@pytest.mark.parametrize("node_id", sorted(GATED))
def test_a_refusal_is_answered_through_the_tabs_catch(flows, node_id):
    node = {n["id"]: n for n in flows}[node_id]
    catches = [n for n in flows
               if n.get("type") == "catch" and n.get("z") == node["z"]]
    assert any(node_id in (c.get("scope") or []) for c in catches), node["name"]


def test_the_forecast_address_stays_on_api_weather_gov(flows):
    for node_id in ("56350936f883a697", "be74868c5dbe3da7"):
        func = {n["id"]: n for n in flows}[node_id]["func"]
        assert 'msg.url.indexOf("https://api.weather.gov/") !== 0' in func


# ── behaviour ────────────────────────────────────────────────────────────────

_HARNESS = r"""
const fs = require("fs"), vm = require("vm"), http = require("http"), https = require("https");
const flows = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const byId = {};
for (const n of flows) { byId[n.id] = n; }

function backend(scenario) {
    const seen = [];
    const server = http.createServer((req, res) => {
        seen.push({ method: req.method, url: req.url, key: req.headers["x-api-key"] || "" });
        const door = req.url.split("/").pop();
        const send = (code, body) => {
            res.writeHead(code, { "Content-Type": "application/json" });
            res.end(typeof body === "string" ? body : JSON.stringify(body));
        };
        if (scenario === "open") { send(200, { success: true, id: door, open: true, hosts: [], dynamic_hosts: false }); }
        else if (scenario === "closed") { send(200, { success: true, id: door, open: false, hosts: [], dynamic_hosts: false }); }
        else if (scenario === "unauthorized") { send(401, { error: "Unauthorized" }); }
        else if (scenario === "not_json") { send(200, "<html>hello</html>"); }
        else if (scenario === "other_switch") { send(200, { success: true, id: "tunein", open: true }); }
        else if (scenario === "truthy_string") { send(200, { success: true, id: door, open: "true" }); }
        else if (scenario === "server_error") { send(503, { success: false, open: true }); }
        else { send(500, {}); }
    });
    return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, seen })));
}

async function run(c) {
    const { server, seen } = await backend(c.scenario);
    const base = c.scenario === "down" ? "http://127.0.0.1:1" : "http://127.0.0.1:" + server.address().port;
    // The weather steps need a location before they ask the Bridge; a test
    // location keeps these runs about the switch.
    const envVars = { PUFFPAI_API_URL: base, PUFFPAI_API_KEY: "flow-test-key",
                      NEWS_RSS_URL: "https://feeds.example.org/rss.xml",
                      WEATHER_DEFAULT_LAT: "39.7817", WEATHER_DEFAULT_LON: "-89.6501" };
    const logs = { error: [], warn: [], log: [] };
    const node = {
        error: (m) => logs.error.push(String(m)), warn: (m) => logs.warn.push(String(m)),
        log: (m) => logs.log.push(String(m)), debug() {}, trace() {}, status() {},
        send() {}, done() {}, on() {}
    };
    const sandbox = {
        console, Buffer, Date, setTimeout, clearTimeout,
        env: { get: (k) => envVars[k] },
        flow: { get() {}, set() {} }, context: { get() {}, set() {} }, global: { get() {}, set() {} },
        bridgeHttp: http, bridgeHttps: https, __node__: node, msg: c.msg
    };
    const ctx = vm.createContext(sandbox);
    vm.runInContext("results = (async function(msg){ var node = __node__;\n" +
                    byId[c.id].func + "\n})(msg);", ctx);
    let out;
    try { out = await ctx.results; } catch (e) { out = { threw: String(e) }; }
    server.close();
    const shaped = out === null ? null : (Array.isArray(out) ? { list: out.length, first: out[0] }
                                        : { url: out.url || null, threw: out.threw || null });
    return { out: shaped, seen, logs };
}

(async () => {
    const results = [];
    for (const c of cases) { results.push(await run(c)); }
    process.stdout.write(JSON.stringify(results));
})();
"""

_FORECAST_IN = {"payload": {"properties": {
    "forecast": "https://api.weather.gov/gridpoints/BOU/62,60/forecast"}}}


def _input(node_id, with_res=True):
    msg = json.loads(json.dumps(_FORECAST_IN)) if node_id in (
        "56350936f883a697", "be74868c5dbe3da7") else {"payload": {}}
    if with_res:
        msg["res"] = {"_res": "stand-in"}
    return msg


@pytest.fixture(scope="module")
def run_nodes(tmp_path_factory):
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node is not installed")
    work = tmp_path_factory.mktemp("nodered_harness")
    harness = work / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    def run(cases):
        spec = work / "cases.json"
        spec.write_text(json.dumps(cases), encoding="utf-8")
        done = subprocess.run([node_bin, str(harness), str(FLOWS), str(spec)],
                              capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        return json.loads(done.stdout)

    return run


@pytest.mark.parametrize("node_id, door", sorted(GATED.items()))
def test_a_clear_yes_passes_the_message_on(run_nodes, node_id, door):
    (r,) = run_nodes([{"id": node_id, "scenario": "open", "msg": _input(node_id)}])
    assert r["out"] and r["out"]["url"], r
    assert r["out"]["url"].startswith(("https://api.weather.gov/",
                                       "https://feeds.example.org/"))
    assert r["seen"] == [{"method": "GET", "url": f"/system/egress/door/{door}",
                          "key": "flow-test-key"}]
    assert r["logs"]["error"] == []


@pytest.mark.parametrize("node_id, door", sorted(GATED.items()))
def test_a_closed_switch_stops_the_call_and_answers(run_nodes, node_id, door):
    (r,) = run_nodes([{"id": node_id, "scenario": "closed", "msg": _input(node_id)}])
    assert r["out"] is None, r
    assert len(r["logs"]["error"]) == 1
    assert f"Bridge switch '{door}' is not open (the switch is off)" in \
        r["logs"]["error"][0]


def test_a_scheduled_run_with_the_switch_closed_only_logs(run_nodes):
    (r,) = run_nodes([{"id": "52e7b3fb0849b21b", "scenario": "closed",
                       "msg": _input("52e7b3fb0849b21b", with_res=False)}])
    assert r["out"] is None
    assert r["logs"]["error"] == []
    assert "Bridge switch 'weather' is not open" in r["logs"]["warn"][0]


@pytest.mark.parametrize("scenario", ["unauthorized", "not_json", "other_switch",
                                      "truthy_string", "server_error", "down"])
@pytest.mark.parametrize("node_id", ["52e7b3fb0849b21b", "47ece2902994315a"])
def test_anything_but_a_clear_yes_is_no(run_nodes, node_id, scenario):
    (r,) = run_nodes([{"id": node_id, "scenario": scenario, "msg": _input(node_id)}])
    assert r["out"] is None, (scenario, r)
    assert "is not open" in r["logs"]["error"][0]


def test_a_forecast_off_api_weather_gov_is_not_followed(run_nodes):
    msg = {"payload": {"properties": {"forecast": "https://elsewhere.example.org/f"}},
           "res": {"_res": "stand-in"}}
    (r,) = run_nodes([{"id": "be74868c5dbe3da7", "scenario": "open", "msg": msg}])
    assert r["out"] == {"list": 1, "first": None}
    assert r["seen"] == [], "an off-host forecast must not even ask"
