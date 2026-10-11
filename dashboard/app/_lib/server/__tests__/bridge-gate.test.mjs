/**
 * The Bridge gate in front of the dashboard server's own outbound routes:
 * radio search, the radio play count, TuneIn search and resolve, and the page
 * reader. Each one must ask the backend whether its switch is open before it
 * looks up a name or opens a socket, and must refuse when the answer is no,
 * missing or unreadable.
 *
 *   npm run test:bridge   (or: node app/_lib/server/__tests__/bridge-gate.test.mjs)
 *
 * Plain Node, same house style as the pairing and reader tests: native
 * TypeScript type stripping (Node >=22.15 for module.registerHooks), non-zero
 * exit on any failure.
 *
 * The REAL route handlers are imported. A resolve hook maps the two things
 * only Next's bundler understands (the `@/` path alias and the extensionless
 * `next/server`) onto files, so what runs here is the code that ships.
 * Every network edge is stubbed: globalThis.fetch (the backend question and
 * the radio/TuneIn calls), dns.promises.lookup and http(s).request (the
 * reader). Nothing here reaches the network.
 *
 * Lives under app/_lib/ (underscore-private), so Next never routes it.
 */
import assert from "node:assert/strict";
import dns from "node:dns";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import { registerHooks } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DASH = path.resolve(HERE, "..", "..", "..", "..");

/* ── module resolution: the two Next-only specifier shapes ─────────── */

function withTsExtension(file) {
  if (fs.existsSync(file) && fs.statSync(file).isFile()) return file;
  for (const ext of [".ts", ".tsx"]) {
    if (fs.existsSync(file + ext)) return file + ext;
  }
  return file;
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "next/server") {
      return nextResolve("next/server.js", context);
    }
    if (specifier.startsWith("@/")) {
      const file = withTsExtension(path.join(DASH, specifier.slice(2)));
      return nextResolve(pathToFileURL(file).href, context);
    }
    return nextResolve(specifier, context);
  },
});

/* ── sandbox: set before any pairing import ────────────────────────── */

const DIR = fs.mkdtempSync(path.join(os.tmpdir(), "puff-bridge-gate-"));
process.env.PUFF_PAIRING_STATE = path.join(DIR, "pairing.json");
process.env.PUFF_PAIRED_DEVICES = path.join(DIR, "devices.json");
process.env.PUFF_PAIRING_ENFORCE = "knowledge";
const BACKEND = "http://backend.invalid:8000";
process.env.PUFFPAI_BACKEND_URL = BACKEND;
const KEY = "test-key-not-a-secret";
process.env.PUFFPAI_API_KEY = KEY;

const RADIO_HOSTS = ["all.api.radio-browser.info", "de1.api.radio-browser.info"];
const TUNEIN_HOSTS = ["opml.radiotime.com"];

/* ── the network stubs ─────────────────────────────────────────────── */

/** What the stubbed backend answers, per switch id. */
let backendAnswer = () => {
  throw new TypeError("fetch failed");
};
/** Every URL the route tried to reach that is NOT the backend. */
let outbound = [];
/** Every backend question asked. */
let asked = [];
/** Answer for a non-backend URL. */
let upstream = () => new Response("[]", { status: 200 });

globalThis.fetch = async (input, init = {}) => {
  const url = String(input instanceof Request ? input.url : input);
  if (url.startsWith(BACKEND + "/")) {
    asked.push({ url, auth: (init.headers || {}).Authorization });
    return backendAnswer(url);
  }
  outbound.push(url);
  return upstream(url);
};

let lookups = [];
dns.promises.lookup = async (host) => {
  lookups.push(host);
  // A private answer: the reader's own guard refuses it, so an "open" case
  // proves the route got past the Bridge without any socket being opened.
  return [{ address: "10.0.0.7", family: 4 }];
};
let sockets = 0;
const refuseSocket = () => {
  sockets += 1;
  throw new Error("test: no socket may open");
};
http.request = refuseSocket;
https.request = refuseSocket;

function answerOpen(id, hosts) {
  return () =>
    Response.json({ success: true, id, open: true, hosts, dynamic_hosts: false });
}
function answerClosed(id, hosts) {
  return () =>
    Response.json({ success: true, id, open: false, hosts, dynamic_hosts: false });
}

const bridge = await import("../bridgeSwitch.ts");
const { NextRequest } = await import("next/server.js");
const radioSearch = await import("../../../api/radio/search/route.ts");
const radioClick = await import("../../../api/radio/click/[uuid]/route.ts");
const tuneinSearch = await import("../../../api/tunein/search/route.ts");
const tuneinResolve = await import("../../../api/tunein/resolve/route.ts");
const reader = await import("../../../api/reader/route.ts");

/* ── harness ───────────────────────────────────────────────────────── */

let pass = 0;
let fail = 0;
const failures = [];

function ok(cond, label, extra = "") {
  if (cond) {
    pass++;
    console.log(`PASS  ${label}${extra ? "   " + extra : ""}`);
  } else {
    fail++;
    failures.push(label);
    console.log(`FAIL  ${label}${extra ? "   " + extra : ""}`);
  }
}

function eq(got, want, label) {
  ok(got === want, label, `got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
}

function reset(answer) {
  bridge.clearBridgeCache();
  backendAnswer = answer;
  outbound = [];
  asked = [];
  lookups = [];
  sockets = 0;
  upstream = () => new Response("[]", { status: 200 });
}

/** Let fire-and-forget work scheduled by a handler run. */
const settle = () => new Promise((r) => setTimeout(r, 20));

const req = (url, init) => new NextRequest(url, init);

/** The failure shapes every route must turn into a refusal. */
const FAILURES = [
  ["backend unreachable", () => {
    throw new TypeError("fetch failed");
  }],
  ["backend answers 401", () => Response.json({ error: "Unauthorized" }, { status: 401 })],
  ["backend answers 500", () => new Response("oops", { status: 500 })],
  ["backend answers non-JSON", () => new Response("<html>", { status: 200 })],
  ["backend answers open as a string", (id) => () =>
    Response.json({ success: true, id, open: "true", hosts: [] })],
];

/**
 * Run the shared closed / failure / no-key cases for one route.
 * `call` performs one request and returns the Response.
 */
async function refusalCases(name, id, hosts, call) {
  console.log(`\n--- ${name}: switch '${id}' closed ---`);
  reset(answerClosed(id, hosts));
  let res = await call();
  await settle();
  eq(res.status, 403, `${name}: closed switch is 403`);
  let body = await res.json();
  eq(body.reason, "bridge_closed", `${name}: reason is bridge_closed`);
  eq(body.switch, id, `${name}: refusal names the switch id`);
  ok(
    typeof body.error === "string" &&
      body.error.includes("Settings") &&
      body.error.includes("Bridge") &&
      body.error.includes(bridge.BRIDGE_SWITCH_LABELS[id]),
    `${name}: refusal sentence names the switch and where to turn it on`,
    JSON.stringify(body.error),
  );
  ok(!/door/i.test(body.error) && !body.error.includes("\u2014"),
    `${name}: refusal sentence has no "door" and no em dash`);
  eq(outbound.length, 0, `${name}: closed switch sends nothing out`);
  eq(lookups.length, 0, `${name}: closed switch looks nothing up`);
  eq(asked.length, 1, `${name}: the backend was asked once`);
  eq(asked[0] && asked[0].url, `${BACKEND}/system/egress/door/${id}`,
    `${name}: the question names the switch`);
  eq(asked[0] && asked[0].auth, `Bearer ${KEY}`, `${name}: the question carries the key`);

  for (const [label, make] of FAILURES) {
    const answer = label === "backend answers open as a string" ? make(id) : make;
    reset(answer);
    res = await call();
    await settle();
    eq(res.status, 503, `${name}: ${label} is 503`);
    body = await res.json();
    eq(body.reason, "bridge_unavailable", `${name}: ${label} is bridge_unavailable`);
    eq(outbound.length, 0, `${name}: ${label} sends nothing out`);
    eq(lookups.length, 0, `${name}: ${label} looks nothing up`);
  }

  console.log(`--- ${name}: no API key configured ---`);
  reset(answerOpen(id, hosts));
  process.env.PUFFPAI_API_KEY = "";
  try {
    res = await call();
    await settle();
  } finally {
    process.env.PUFFPAI_API_KEY = KEY;
  }
  eq(res.status, 503, `${name}: no key is 503`);
  eq(asked.length, 0, `${name}: no key means the backend is not even asked`);
  eq(outbound.length, 0, `${name}: no key sends nothing out`);
}

/* ═══════════════════════════════════════════════════════════════════
   1. /api/radio/search  -> radio_browser
   ═══════════════════════════════════════════════════════════════════ */

let n = 0;
const radioSearchCall = () =>
  radioSearch.GET(req(`http://dash.test/api/radio/search?name=jazz${n++}`));

await refusalCases("radio search", "radio_browser", RADIO_HOSTS, radioSearchCall);

console.log("\n--- radio search: switch open ---");
reset(answerOpen("radio_browser", RADIO_HOSTS));
upstream = () =>
  Response.json([
    { stationuuid: "abc-123-def", name: "Jazz", url_resolved: "https://s.example/a", hls: 0 },
  ]);
let res = await radioSearchCall();
eq(res.status, 200, "radio search: open switch proceeds");
let body = await res.json();
eq(body.stations && body.stations.length, 1, "radio search: stations come back");
eq(outbound.length, 1, "radio search: one catalog request");
ok(outbound[0].startsWith("https://all.api.radio-browser.info/json/stations/search?"),
  "radio search: first declared mirror is asked", outbound[0]);

console.log("\n--- radio search: only mirrors the switch declares ---");
reset(answerOpen("radio_browser", ["de1.api.radio-browser.info"]));
upstream = () => new Response("down", { status: 503 });
res = await radioSearchCall();
eq(res.status, 502, "radio search: all declared mirrors down is 502");
ok(outbound.length === 1 && new URL(outbound[0]).hostname === "de1.api.radio-browser.info",
  "radio search: an undeclared mirror is never contacted", JSON.stringify(outbound));
reset(answerOpen("radio_browser", ["nl1.api.radio-browser.info"]));
res = await radioSearchCall();
eq(outbound.length, 0, "radio search: a host only the switch names is not added");

console.log("\n--- radio search: a closed switch serves nothing from the cache ---");
reset(answerOpen("radio_browser", RADIO_HOSTS));
upstream = () =>
  Response.json([
    { stationuuid: "abc-123-def", name: "Jazz", url_resolved: "https://s.example/a", hls: 0 },
  ]);
const cachedUrl = "http://dash.test/api/radio/search?name=cachecheck";
res = await radioSearch.GET(req(cachedUrl));
eq(res.status, 200, "radio search: first call fills the cache");
bridge.clearBridgeCache();
backendAnswer = answerClosed("radio_browser", RADIO_HOSTS);
res = await radioSearch.GET(req(cachedUrl));
eq(res.status, 403, "radio search: cached rows are refused once the switch is closed");

/* ═══════════════════════════════════════════════════════════════════
   2. /api/radio/click/<uuid>  -> radio_browser
   ═══════════════════════════════════════════════════════════════════ */

const clickCall = () =>
  radioClick.POST(new Request("http://dash.test/api/radio/click/abc-123-def", { method: "POST" }), {
    params: Promise.resolve({ uuid: "abc-123-def" }),
  });

await refusalCases("radio click", "radio_browser", RADIO_HOSTS, clickCall);

console.log("\n--- radio click: switch open ---");
reset(answerOpen("radio_browser", RADIO_HOSTS));
res = await clickCall();
await settle();
eq(res.status, 204, "radio click: open switch answers 204");
eq(outbound[0], "https://all.api.radio-browser.info/json/url/abc-123-def",
  "radio click: the play count is reported to a declared mirror");

console.log("\n--- radio click: a bad uuid is refused before the Bridge is asked ---");
reset(answerOpen("radio_browser", RADIO_HOSTS));
res = await radioClick.POST(new Request("http://dash.test/x", { method: "POST" }), {
  params: Promise.resolve({ uuid: "../../etc" }),
});
eq(res.status, 400, "radio click: bad uuid is 400");
eq(asked.length + outbound.length, 0, "radio click: bad uuid asks and sends nothing");

/* ═══════════════════════════════════════════════════════════════════
   3. /api/tunein/search  -> tunein
   ═══════════════════════════════════════════════════════════════════ */

const tuneinSearchCall = () =>
  tuneinSearch.GET(req(`http://dash.test/api/tunein/search?q=kexp${n++}`));

await refusalCases("tunein search", "tunein", TUNEIN_HOSTS, tuneinSearchCall);

console.log("\n--- tunein search: switch open ---");
reset(answerOpen("tunein", TUNEIN_HOSTS));
upstream = () =>
  Response.json({ body: [{ type: "audio", guide_id: "s12345", text: "KEXP" }] });
res = await tuneinSearchCall();
eq(res.status, 200, "tunein search: open switch proceeds");
body = await res.json();
eq(body.stations && body.stations[0] && body.stations[0].stationuuid, "s12345",
  "tunein search: station comes back");
ok(outbound.length === 1 && new URL(outbound[0]).hostname === "opml.radiotime.com",
  "tunein search: only the declared host is contacted", JSON.stringify(outbound));

console.log("\n--- tunein search: the switch must declare the host ---");
reset(answerOpen("tunein", ["elsewhere.example"]));
res = await tuneinSearchCall();
eq(res.status, 502, "tunein search: host not on the switch is refused");
eq(outbound.length, 0, "tunein search: and nothing is sent");

/* ═══════════════════════════════════════════════════════════════════
   4. /api/tunein/resolve  -> tunein
   ═══════════════════════════════════════════════════════════════════ */

let sid = 100;
const resolveCall = () =>
  tuneinResolve.GET(req(`http://dash.test/api/tunein/resolve?id=s${sid++}`));

await refusalCases("tunein resolve", "tunein", TUNEIN_HOSTS, resolveCall);

console.log("\n--- tunein resolve: switch open, direct stream ---");
reset(answerOpen("tunein", TUNEIN_HOSTS));
upstream = () =>
  Response.json({
    body: [
      { url: "http://broadcaster.example/listen.pls", is_direct: false },
      { url: "http://stream.example/live.mp3", is_direct: true },
    ],
  });
res = await resolveCall();
eq(res.status, 200, "tunein resolve: open switch proceeds");
body = await res.json();
eq(body.url_resolved, "http://stream.example/live.mp3", "tunein resolve: direct stream wins");
eq(outbound.length, 1, "tunein resolve: only Tune.ashx was fetched");

console.log("\n--- tunein resolve: a playlist on an undeclared host is skipped ---");
reset(answerOpen("tunein", TUNEIN_HOSTS));
upstream = () =>
  Response.json({
    body: [
      { url: "http://broadcaster.example/listen.pls", is_direct: false },
      { url: "https://192.168.1.10/inside.m3u", is_direct: false },
    ],
  });
res = await resolveCall();
eq(res.status, 502, "tunein resolve: no playable stream is 502");
ok(outbound.length === 1 && new URL(outbound[0]).hostname === "opml.radiotime.com",
  "tunein resolve: playlists off the declared host are never fetched",
  JSON.stringify(outbound));

console.log("\n--- tunein resolve: a playlist on the declared host is unwrapped ---");
reset(answerOpen("tunein", TUNEIN_HOSTS));
upstream = (url) =>
  url.includes("Tune.ashx")
    ? Response.json({
        body: [{ url: "https://opml.radiotime.com/pl/s1.pls", is_direct: false }],
      })
    : new Response("[playlist]\nFile1=http://stream.example/unwrapped.mp3\n", { status: 200 });
res = await resolveCall();
body = await res.json();
eq(body.url_resolved, "http://stream.example/unwrapped.mp3",
  "tunein resolve: declared-host playlist is fetched and unwrapped");

/* ═══════════════════════════════════════════════════════════════════
   5. /api/reader  -> page_reader
   ═══════════════════════════════════════════════════════════════════ */

const readerCall = () =>
  reader.POST(
    req("http://dash.test/api/reader", {
      method: "POST",
      headers: {
        origin: "http://dash.test",
        host: "dash.test",
        "content-type": "application/json",
      },
      body: JSON.stringify({ url: "https://news.example.org/story" }),
    }),
  );

await refusalCases("reader", "page_reader", [], readerCall);
eq(sockets, 0, "reader: no socket opened in any refusal case");

console.log("\n--- reader: switch open ---");
reset(() =>
  Response.json({ success: true, id: "page_reader", open: true, hosts: [], dynamic_hosts: true }),
);
res = await readerCall();
body = await res.json();
eq(lookups[0], "news.example.org", "reader: open switch proceeds to the address check");
eq(body.reason, "blocked_private_address",
  "reader: the fetch path's own guard still applies (stubbed private answer)");
eq(sockets, 0, "reader: and no socket was opened");

console.log("\n--- reader: a cross-origin caller never reaches the Bridge ---");
reset(answerOpen("page_reader", []));
res = await reader.POST(
  req("http://dash.test/api/reader", {
    method: "POST",
    headers: { origin: "http://evil.test", host: "dash.test", "content-type": "application/json" },
    body: JSON.stringify({ url: "https://news.example.org/" }),
  }),
);
eq(res.status, 403, "reader: cross-origin is refused");
eq(asked.length, 0, "reader: and the backend is not asked");

/* ═══════════════════════════════════════════════════════════════════
   6. the cache
   ═══════════════════════════════════════════════════════════════════ */

console.log("\n--- cache: a clear answer is reused briefly, a failure never ---");
let clock = 1_000_000;
const now = () => clock;
reset(answerOpen("tunein", TUNEIN_HOSTS));
let d = await bridge.checkBridgeSwitch("tunein", { now });
ok(d.open, "cache: open answer");
backendAnswer = answerClosed("tunein", TUNEIN_HOSTS);
clock += bridge.BRIDGE_CACHE_MS - 1;
d = await bridge.checkBridgeSwitch("tunein", { now });
ok(d.open && asked.length === 1, "cache: reused inside the window");
clock += 2;
d = await bridge.checkBridgeSwitch("tunein", { now });
ok(!d.open && d.reason === "bridge_closed" && asked.length === 2,
  "cache: closing takes effect once the window passes");
ok(bridge.BRIDGE_CACHE_MS <= 5_000, "cache: the window is a few seconds at most",
  String(bridge.BRIDGE_CACHE_MS));

reset(() => {
  throw new TypeError("fetch failed");
});
await bridge.checkBridgeSwitch("tunein", { now });
backendAnswer = answerOpen("tunein", TUNEIN_HOSTS);
d = await bridge.checkBridgeSwitch("tunein", { now });
ok(d.open && asked.length === 2, "cache: an unreachable backend is not remembered");

console.log("\n--- cache: the answer must be for the switch that was asked ---");
reset(answerOpen("radio_browser", RADIO_HOSTS));
d = await bridge.checkBridgeSwitch("tunein", { now });
ok(!d.open && d.reason === "bridge_unavailable", "cache: an answer for another id is refused");

/* ═══════════════════════════════════════════════════════════════════
   7. labels agree with the Settings card
   ═══════════════════════════════════════════════════════════════════ */

console.log("\n--- labels match the Settings card ---");
const card = fs.readFileSync(
  path.join(DASH, "app", "dashboard", "settings", "EgressDoorsCard.tsx"),
  "utf8",
);
for (const [id, label] of Object.entries(bridge.BRIDGE_SWITCH_LABELS)) {
  ok(card.includes(`${id}: ${JSON.stringify(label)}`),
    `label for ${id} is the one the Settings card shows`, label);
}

/* ── summary ───────────────────────────────────────────────────────── */

fs.rmSync(DIR, { recursive: true, force: true });
console.log(`\n${pass} passed, ${fail} failed`);
if (fail) {
  console.log("Failures:\n  " + failures.join("\n  "));
  process.exit(1);
}
assert.ok(pass > 0);
