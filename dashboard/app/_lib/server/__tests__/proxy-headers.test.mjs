/**
 * What the keyed proxy tells the backend about its caller.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * The backend keys its 300-second vault-unlocked chat session -- protected
 * questions answered from the DECRYPTED vault with no further PIN -- on a
 * per-caller id. Through this proxy every request looks like 127.0.0.1, so a
 * header the caller could choose would put the whole household in one unlock
 * bucket. The identity is DERIVED here from the verified pairing cookie, and a
 * client-supplied copy must never reach the backend.
 *
 * ASSERTED
 *   1. A client's X-Puff-Device-Id / X-Puff-Anonymous / X-Client-Id /
 *      X-Puff-Display / Authorization / client-addr header is DROPPED or
 *      overwritten, whatever it says; any other X-* never crosses.
 *   2. The device id sent upstream is derived from the cookie, is not the raw
 *      device id, and is stable per device; no cookie / bad MAC / revoked
 *      device -> no identity header, and X-Puff-Anonymous instead.
 *   3. X-Vault-Token crosses ONLY on /vault/*, X-Setup-Code ONLY on the
 *      first-run families (/setup, /wizard, /build/prove, /build/self), and
 *      the server-generated X-PuffPAI-Client-Addr is always set.
 *   4. The route really calls upstreamHeaders(req, API_KEY, session.sessionId,
 *      path, clientAddr(req)) -- a pure function nobody calls proves nothing.
 *   5. No server-side module makes an access decision from a display hint.
 *
 *   npm run test:proxy-headers
 *   node app/_lib/server/__tests__/proxy-headers.test.mjs
 *
 * Requires Node >=22.6 for native TypeScript type stripping. Exits non-zero on
 * any failure. SAFETY: PUFF_PAIRING_STATE / PUFF_PAIRED_DEVICES are pointed at
 * a fresh temp directory BEFORE pairing.ts is imported, so no real household
 * state is read or written, and nothing here opens a socket.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

/* ── sandbox ───────────────────────────────────────────────────────── */

const DIR = fs.mkdtempSync(path.join(os.tmpdir(), "puff-proxyhdr-test-"));
const STATE_FILE = path.join(DIR, "pairing.json");
const DEVICES_FILE = path.join(DIR, "devices.json");
process.env.PUFF_PAIRING_STATE = STATE_FILE;
process.env.PUFF_PAIRED_DEVICES = DEVICES_FILE;
process.env.PUFF_PAIRING_ENFORCE = "all";

/** Fixed, well-formed 32-byte hex. A test fixture, not anybody's secret. */
const SECRET = "b".repeat(64);
const now = new Date().toISOString();
fs.writeFileSync(
  STATE_FILE,
  JSON.stringify({
    version: 1,
    secret: SECRET,
    code: "7XK4P2M9",
    createdAt: now,
    codeRotatedAt: now,
  }),
);
fs.writeFileSync(DEVICES_FILE, JSON.stringify({ version: 1, devices: [] }));

const {
  upstreamHeaders,
  FORWARDED_CLIENT_HEADERS,
  DEVICE_ID_HEADER,
  CLIENT_ADDR_HEADER,
  ANONYMOUS_HEADER,
} = await import("../proxyHeaders.ts");
const {
  RELAYED_RESPONSE_HEADERS,
  KNOWLEDGE_ASSET_RESPONSE_HEADERS,
  KNOWLEDGE_ASSET_CSP,
  INLINE_ASSET_TYPES,
  isKnowledgeAssetPath,
  relayedResponseHeaders,
  knowledgeAssetContentType,
  hardenKnowledgeAssetHeaders,
} = await import("../proxyHeaders.ts");
const P = await import("../pairing.ts");

/* ── harness ───────────────────────────────────────────────────────── */

let failures = 0;
let passes = 0;
function check(label, actual, expected) {
  try {
    assert.deepEqual(actual, expected);
    passes += 1;
    console.log(`  ok   ${label}`);
  } catch {
    failures += 1;
    console.log(`  FAIL ${label}: got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);
  }
}
function ok(label, cond) {
  check(label, Boolean(cond), true);
}

const API_KEY = "test-api-key-not-a-real-one";

/** A request carrying the given headers and (optionally) a cookie value. */
function reqWith(headers = {}, cookie = null) {
  const map = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  if (cookie) map.set("cookie", `puff_device=${cookie}`);
  return {
    headers: {
      get: (n) => map.get(String(n).toLowerCase()) ?? null,
      forEach: (cb) => map.forEach((v, k) => cb(v, k)),
    },
    cookies: { get: (n) => (n === "puff_device" && cookie ? { value: cookie } : undefined) },
  };
}

/** The raw derivation: cookie -> identity, with no v1 upgrade. */
const build = (req, key = API_KEY, addr = "unknown", segs = []) =>
  upstreamHeaders(req, key, P.deviceSessionIdFor(req), segs, addr);

/** Same, on a given backend path, for the scoped relays. */
const buildAt = (req, segs) => build(req, API_KEY, "unknown", segs);

/**
 * Exactly what the route does: proxyDeviceSessionFor(), which additionally
 * upgrades a legacy v1 cookie in place so the first proxied call already carries
 * an identity. Asserted against the route source in section 4.
 */
const buildRoute = (req, key = API_KEY, addr = "unknown", segs = []) =>
  upstreamHeaders(req, key, P.proxyDeviceSessionFor(req).sessionId, segs, addr);

/** Header names the policy may relay, lowercase. */
const RELAYED = FORWARDED_CLIENT_HEADERS.map((r) => r.header);

const hdr = (h, name) => {
  const found = Object.entries(h).find(([k]) => k.toLowerCase() === name.toLowerCase());
  return found ? found[1] : undefined;
};

const device = P.addPairedDevice("Kitchen tablet");
const other = P.addPairedDevice("Sam's phone");
const goodCookie = P.issueDeviceCookieValueV2(device.id);
const expected = P.deviceSessionId(device.id);

/* ── 1. client-supplied authority headers are dropped ──────────────── */

console.log("\n--- a client cannot assert who it is ---");
{
  const out = build(
    reqWith(
      {
        "X-Client-Id": "192.168.1.42",
        [DEVICE_ID_HEADER]: P.deviceSessionId(other.id),
        [CLIENT_ADDR_HEADER]: "127.0.0.1",
        Authorization: "Bearer stolen",
        "X-API-Key": "stolen",
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
        Cookie: "should-not-be-relayed=1",
        [ANONYMOUS_HEADER]: "1",
        "X-Puff-Display": "kiosk",
        "X-Zone3-Ticket": "attacker-consent",
        "X-Anything-Else": "nope",
      },
      goodCookie,
    ),
  );
  check("X-Client-Id is DROPPED (the vault-session hijack)", hdr(out, "x-client-id"), undefined);
  check("a client X-Puff-Device-Id is overwritten with its own derived id",
    hdr(out, DEVICE_ID_HEADER), expected);
  check("a client client-addr claim is overwritten with the server's",
    hdr(out, CLIENT_ADDR_HEADER), "unknown");
  check("a client Authorization never wins", hdr(out, "authorization"), `Bearer ${API_KEY}`);
  check("X-API-Key is dropped", hdr(out, "x-api-key"), undefined);
  check("X-Forwarded-For is dropped", hdr(out, "x-forwarded-for"), undefined);
  check("X-Real-IP is dropped", hdr(out, "x-real-ip"), undefined);
  check("the pairing cookie is never forwarded", hdr(out, "cookie"), undefined);
  check("a client X-Puff-Anonymous on a paired request is dropped",
    hdr(out, ANONYMOUS_HEADER), undefined);
  check("a client X-Puff-Display is dropped (a display hint, never upstream)",
    hdr(out, "x-puff-display"), undefined);
  check("a client X-Zone3-Ticket is dropped", hdr(out, "x-zone3-ticket"), undefined);
  check("an arbitrary X-* header is dropped", hdr(out, "x-anything-else"), undefined);
  check("no key configured -> no Authorization at all",
    hdr(build(reqWith({}, goodCookie), ""), "authorization"), undefined);
}

/* ── 1b. each relay is SCOPED to the paths that read it ────────────── */

console.log("\n--- the vault token is scoped to /vault/* and the post-setup gated writes ---");
for (const segs of [
  ["vault", "contents"],
  ["vault", "item", "passport"],
  ["vault", "store"],
  ["vault", "document"],
  ["vault", "resolve"],
  ["vault", "lock"],
  ["vault", "secrets", "ha_admin_token"],
  ["vault", "recovery", "status"],
  ["setup", "identity"],
  ["wizard", "apply"],
  ["wizard", "run"],
  ["persona", "scaffold"],
]) {
  check(`relayed on /${segs.join("/")}`,
    hdr(buildAt(reqWith({ "X-Vault-Token": "tok" }, goodCookie), segs), "x-vault-token"), "tok");
}
for (const segs of [
  ["apps", "calendar", "data"],
  ["chat", "send"],
  ["ha", "entities"],
  ["knowledge", "upload"],
  ["build", "prove"],
  ["system", "status"],
  ["vaultish"],
  ["setupx"],
  ["personas"],
  [],
]) {
  check(`NOT relayed on /${segs.join("/") || "(root)"}`,
    hdr(buildAt(reqWith({ "X-Vault-Token": "tok" }, goodCookie), segs), "x-vault-token"), undefined);
}

console.log("\n--- the setup code is scoped to the first-run families ---");
for (const segs of [
  ["setup", "vault"],
  ["setup", "status"],
  ["wizard", "apply"],
  ["wizard", "cards"],
  ["build", "prove"],
  ["build", "prove", "live"],
  ["build", "self"],
  ["persona", "scaffold"],
  ["persona", "list"],
]) {
  check(`relayed on /${segs.join("/")}`,
    hdr(buildAt(reqWith({ "X-Setup-Code": "ABCD-1234" }), segs), "x-setup-code"), "ABCD-1234");
}
for (const segs of [
  ["vault", "contents"],
  ["chat", "send"],
  ["build"],
  ["build", "other"],
  ["buildings"],
  ["setupx"],
  ["personas"],
  [],
]) {
  check(`NOT relayed on /${segs.join("/") || "(root)"}`,
    hdr(buildAt(reqWith({ "X-Setup-Code": "ABCD-1234" }), segs), "x-setup-code"), undefined);
}

/* ── 2. the identity comes from the cookie ─────────────────────────── */

console.log("\n--- the device identity is derived, not accepted ---");
{
  check("a verified cookie yields the derived id", hdr(build(reqWith({}, goodCookie)), DEVICE_ID_HEADER), expected);
  ok("...which is NOT the raw device id", hdr(build(reqWith({}, goodCookie)), DEVICE_ID_HEADER) !== device.id);
  ok("...and matches the backend's shape check", /^[A-Za-z0-9_-]{8,64}$/.test(expected));
  check("no cookie -> no device header", hdr(build(reqWith({})), DEVICE_ID_HEADER), undefined);
  check("a cookie with a bad MAC -> no device header",
    hdr(build(reqWith({}, goodCookie.slice(0, -4) + "0000")), DEVICE_ID_HEADER), undefined);
  check("garbage cookie -> no device header",
    hdr(build(reqWith({}, "not-a-cookie")), DEVICE_ID_HEADER), undefined);
  check("two devices derive different ids",
    P.deviceSessionId(device.id) === P.deviceSessionId(other.id), false);
  check("the same device is stable across calls", P.deviceSessionId(device.id), expected);

  const legacy = P.issueDeviceCookieValue();
  check("a legacy v1 cookie identifies no device -> no header from the raw derivation",
    hdr(build(reqWith({}, legacy)), DEVICE_ID_HEADER), undefined);

  P.revokePairedDevice(device.id);
  check("a revoked device -> no device header",
    hdr(build(reqWith({}, goodCookie)), DEVICE_ID_HEADER), undefined);
  const re = P.addPairedDevice("Kitchen tablet again");
  ok("a malformed device id cannot be signed",
    (() => { try { P.deviceSessionId("../../etc/passwd"); return false; } catch { return true; } })());
  ok("re-pairing mints a different id", P.deviceSessionId(re.id) !== expected);
}

/* ── 2b. "no identity" is SAID, never implied ──────────────────────── */

console.log("\n--- the anonymous marker ---");
{
  // The bug: omitting the identity header made a proxied request with no
  // identity indistinguishable from a direct loopback caller, so the backend
  // keyed them all on peer:127.0.0.1 — ONE shared vault-unlock bucket for the
  // v1 phone, the unpaired laptop, the device-token upload and the kiosk.
  const fresh = P.addPairedDevice("Paired for the marker checks");
  const freshCookie = P.issueDeviceCookieValueV2(fresh.id);

  check("no cookie -> the marker is SET", hdr(build(reqWith({})), ANONYMOUS_HEADER), "1");
  check("...and no identity rides with it",
    hdr(build(reqWith({})), DEVICE_ID_HEADER), undefined);
  check("a paired v2 device -> no marker",
    hdr(build(reqWith({}, freshCookie)), ANONYMOUS_HEADER), undefined);
  check("...just its own identity",
    hdr(build(reqWith({}, freshCookie)), DEVICE_ID_HEADER), P.deviceSessionId(fresh.id));
  check("a client-supplied marker is REPLACED with ours",
    hdr(build(reqWith({ [ANONYMOUS_HEADER]: "0" })), ANONYMOUS_HEADER), "1");
  check("a client-supplied marker on a PAIRED request is dropped",
    hdr(build(reqWith({ [ANONYMOUS_HEADER]: "1" }, freshCookie)), ANONYMOUS_HEADER), undefined);
  ok("the marker is not in the relay allowlist",
    !RELAYED.includes(ANONYMOUS_HEADER.toLowerCase()));
  check("a bad-MAC cookie -> marked anonymous",
    hdr(build(reqWith({}, "v2.aaaaaaaaaaaa.1.2." + "f".repeat(64))), ANONYMOUS_HEADER), "1");
}

/* ── 2c. a v1 cookie is upgraded ON the proxied request ────────────── */

console.log("\n--- v1 is upgraded on the request, not the one after it ---");
{
  const v1 = P.issueDeviceCookieValue();
  const req = reqWith({}, v1);
  const decided = P.proxyDeviceSessionFor(req);
  ok("a verified v1 cookie yields an identity", Boolean(decided.sessionId));
  ok("...and a replacement cookie", Boolean(decided.setCookie));
  ok("...which is a v2 cookie under the pairing cookie name",
    decided.setCookie?.name === P.PAIRING_COOKIE &&
      String(decided.setCookie?.value).startsWith("v2."));
  check("the route therefore sends an identity on the FIRST v1 call",
    hdr(buildRoute(req), DEVICE_ID_HEADER), decided.sessionId);
  check("...and no marker", hdr(buildRoute(req), ANONYMOUS_HEADER), undefined);

  const after = P.proxyDeviceSessionFor(reqWith({}, decided.setCookie.value));
  check("the upgraded cookie derives the SAME identity next request",
    after.sessionId, decided.sessionId);
  check("...and is not re-issued on every request", after.setCookie, null);
  check("the same v1 cookie is deduped to one device",
    P.proxyDeviceSessionFor(reqWith({}, v1)).sessionId, decided.sessionId);

  const otherV1 = P.issueDeviceCookieValue(Date.now() + 1000);
  const otherDecided = P.proxyDeviceSessionFor(reqWith({}, otherV1));
  ok("two distinct v1 cookies upgrade to two distinct identities",
    Boolean(otherDecided.sessionId) && otherDecided.sessionId !== decided.sessionId);

  // Middleware's lazy path and the proxy's eager path must agree about which
  // device a v1 cookie became, or one page load would mint two registry rows.
  const shared = P.issueDeviceCookieValue(Date.now() + 2000);
  const viaProxy = P.proxyDeviceSessionFor(reqWith({}, shared));
  const viaMiddleware = P.renewalCookieFor(reqWith({}, shared));
  ok("renewalCookieFor and proxyDeviceSessionFor share the upgrade map",
    Boolean(viaMiddleware) &&
      P.proxyDeviceSessionFor(reqWith({}, viaMiddleware.value)).sessionId ===
        viaProxy.sessionId);

  check("no cookie at all -> still anonymous through the route helper",
    hdr(buildRoute(reqWith({})), ANONYMOUS_HEADER), "1");
}
{
  // The marker is about identity, not about what a path may reach: an
  // unidentified caller on /vault/* still relays its own vault token, and is
  // still refused by the backend if that token is not a valid session.
  const out = buildAt(reqWith({ "X-Vault-Token": "tok" }), [
    "vault",
    "contents",
  ]);
  check("the marker rides on /vault/* too", hdr(out, ANONYMOUS_HEADER), "1");
  check(
    "...and does not suppress the scoped relay",
    hdr(out, "x-vault-token"),
    "tok",
  );
}

/* ── 3. the allowlisted client headers still work ──────────────────── */

console.log("\n--- the headers the dashboard genuinely sends ---");
{
  const out = buildAt(
    reqWith(
      {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "X-Vault-Token": "vault-session-token",
      },
      goodCookie,
    ),
    ["vault", "contents"],
  );
  check("X-Vault-Token passes on /vault/* (a session token, not an identity)",
    hdr(out, "x-vault-token"), "vault-session-token");
  check("X-Setup-Code (first-run) is untouched on /setup/*",
    hdr(buildAt(reqWith({ "X-Setup-Code": "ABCD-1234" }), ["setup", "vault"]), "x-setup-code"),
    "ABCD-1234");
  check("Content-Type passes", hdr(out, "content-type"), "application/json");
  check("Accept passes", hdr(out, "accept"), "text/event-stream");
  check("Content-Length is not forwarded",
    hdr(build(reqWith({ "Content-Length": "12" })), "content-length"), undefined);
  check("the allowlist is exactly the two documented headers",
    [...RELAYED].sort(), ["x-setup-code", "x-vault-token"]);
  ok("every relay rule carries a reason",
    FORWARDED_CLIENT_HEADERS.every((r) => typeof r.why === "string" && r.why.length > 20));
  check("the client address is always asserted, even with no cookie",
    hdr(build(reqWith({}), API_KEY, "unknown"), CLIENT_ADDR_HEADER), "unknown");
}

/* ── 4. the route actually uses this policy ────────────────────────── */

console.log("\n--- the one caller ---");
{
  const HERE = path.dirname(fileURLToPath(import.meta.url));
  const routeSrc = fs.readFileSync(
    path.join(HERE, "..", "..", "..", "api", "pai", "[...path]", "route.ts"),
    "utf8",
  );
  ok("the route builds headers with upstreamHeaders(req, API_KEY, ...)",
    /upstreamHeaders\(\s*req,\s*API_KEY,/.test(routeSrc));
  ok("...deriving the identity from the cookie, not from a header",
    /const\s+session\s*=\s*proxyDeviceSessionFor\(req\)/.test(routeSrc));
  ok("...passing session.sessionId, not anything read off the request",
    /upstreamHeaders\(\s*req,\s*API_KEY,\s*session\.sessionId,/.test(routeSrc));
  ok("...then the decoded path, then the server-generated client address",
    /upstreamHeaders\(\s*req,\s*API_KEY,\s*session\.sessionId,\s*path,\s*clientAddr\(req\),?\s*\)/.test(routeSrc));
  ok("clientAddr() asserts nothing it cannot prove",
    /function\s+clientAddr\([^)]*\)\s*:\s*string\s*\{\s*return\s+"unknown";\s*\}/.test(routeSrc));
  ok("...and attaching the upgrade/renewal cookie it decided on",
    /if\s*\(session\.setCookie\)\s*res\.cookies\.set\(session\.setCookie\)/.test(routeSrc));
  ok("the route never reads the device id header off the request",
    !/headers\.get\(\s*["'][xX]-[pP]uff-[dD]evice-[iI]d/.test(routeSrc));
  ok("the route never reads the anonymous marker off the request",
    !/headers\.get\(\s*["'][xX]-[pP]uff-[aA]nonymous/.test(routeSrc));
  ok("the route has no header relay loop of its own",
    !/req\.headers\.forEach/.test(routeSrc));
  ok(
    "the route copies response headers only via relayedResponseHeaders(path)",
    /for\s*\(\s*const\s+h\s+of\s+relayedResponseHeaders\(\s*path\s*\)\s*\)/.test(routeSrc) &&
      !/for\s*\(\s*const\s+h\s+of\s+\[\s*["']content-type["']/.test(routeSrc),
  );
}

/* ── 5. display mode is presentation, never authentication ─────────── */

console.log("\n--- display mode is never an access decision ---");
{
  // There is no isApplianceDisplay() in this repo (asserted below, so the day
  // someone adds one this test is already waiting). What DOES exist is the
  // client-side kiosk hint: ?puffMode=kiosk, persisted in localStorage and read
  // only by the pre-hydration script in app/layout.tsx to toggle CSS classes.
  // All of its inputs are client-controlled, so nothing that grants access may
  // consult them. Checked across middleware, the API routes, and _lib/server.
  const HERE2 = path.dirname(fileURLToPath(import.meta.url));
  const DASH = path.resolve(HERE2, "..", "..", "..", "..");
  const files = [];
  const walk = (p) => {
    let st;
    try { st = fs.statSync(p); } catch { return; }
    if (st.isDirectory()) {
      for (const e of fs.readdirSync(p)) {
        if (e === "__tests__" || e === "node_modules") continue;
        walk(path.join(p, e));
      }
      return;
    }
    if (/\.(ts|tsx|mjs|js)$/.test(p)) files.push(p);
  };
  [
    path.join(DASH, "middleware.ts"),
    path.join(DASH, "app", "api"),
    path.join(DASH, "app", "_lib", "server"),
  ].forEach(walk);
  ok("found server-side modules to check", files.length > 5);
  const offenders = files.filter((f) => {
    const code = fs
      .readFileSync(f, "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
    return /isApplianceDisplay|x-puff-display|puffMode/i.test(code);
  });
  ok(`no server-side module reads a display hint (${offenders.length ? offenders.join(", ") : "none"})`,
    offenders.length === 0);
}

/* ── 5b. response headers: the asset sandbox comes back, nothing else ─── */

console.log("\n--- response headers relayed back to the browser ---");
{
  const DASHBOARD = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..");
  const base = [...RELAYED_RESPONSE_HEADERS].sort();
  check(
    "ordinary paths relay exactly content-type, cache-control, content-disposition",
    JSON.stringify(base),
    JSON.stringify(["cache-control", "content-disposition", "content-type"]),
  );
  for (const segs of [
    ["chat", "send"],
    ["vault", "contents"],
    ["knowledge", "library", "wiki", "article"],
    ["knowledge", "library", "wiki", "main"],
    ["knowledge", "library", "asset"],
    ["knowledge", "library", "wiki", "asset", "x"],
    ["knowledge", "asset"],
    ["apps", "library", "wiki", "asset"],
    [],
  ]) {
    const got = relayedResponseHeaders(segs);
    ok(
      `/${segs.join("/")} gains no CSP`,
      !got.includes("content-security-policy") && !isKnowledgeAssetPath(segs),
    );
    check(`/${segs.join("/")} relays only the base set`, got.length, base.length);
  }
  const asset = relayedResponseHeaders(["knowledge", "library", "wiki", "asset"]);
  ok("the asset endpoint relays its sandbox CSP", asset.includes("content-security-policy"));
  ok("...and Content-Disposition", asset.includes("content-disposition"));
  ok("...and nosniff", asset.includes("x-content-type-options"));
  ok("...and still the base set", base.every((h) => asset.includes(h)));
  check("...with no duplicates", new Set(asset).size, asset.length);
  ok(
    "no relayed response header can set cookies or redirect",
    ![...asset, ...base].some((h) => /set-cookie|location|refresh/i.test(h)),
  );

  const assetRoute = fs.readFileSync(
    path.join(DASHBOARD, "app", "api", "knowledge", "asset", "route.ts"),
    "utf8",
  );
  ok(
    "/api/knowledge/asset re-applies the asset allowlist to the upstream type",
    /hardenKnowledgeAssetHeaders\(\s*headers,\s*upstream\.headers\.get\(\s*["']content-type["']\s*\)\s*\)/.test(
      assetRoute,
    ),
  );
  ok(
    "...and never copies the upstream Content-Type through",
    !/set\(\s*["']Content-Type["']\s*,\s*upstream/i.test(assetRoute),
  );
  ok(
    "/api/pai re-applies it on the asset path",
    /if\s*\(\s*isKnowledgeAssetPath\(path\)\s*\)\s*\{\s*hardenKnowledgeAssetHeaders\(\s*respHeaders,/.test(
      fs.readFileSync(path.join(DASHBOARD, "app", "api", "pai", "[...path]", "route.ts"), "utf8"),
    ),
  );
  ok(
    "KNOWLEDGE_ASSET_RESPONSE_HEADERS names the sandbox CSP and the download",
    KNOWLEDGE_ASSET_RESPONSE_HEADERS.includes("content-security-policy") &&
      KNOWLEDGE_ASSET_RESPONSE_HEADERS.includes("content-disposition"),
  );
}

/* ── 5c. the asset MIME allowlist ───────────────────────────────────── */

console.log("\n--- asset Content-Type: exact allowlist, never the pack's string ---");
{
  // Headless Chrome ran script for the first three under the old regex.
  for (const raw of [
    "image/png;a,text/html",
    "text/css;,text/html",
    'video/mp4;x="a",text/html',
    "image/png,text/html",
    "image/png, text/html",
    'image/png;charset="x,text/html"',
    'image/png;"',
    "audio/ogg;codecs=opus,text/html",
    "image/png\r\nContent-Type: text/html",
    "image/png\nX: y",
    "image/png\u0000",
    "image/pngx",
    "image /png",
    "image/svg+xml",
    "image/svg+xml;foo=image/png",
    "text/html;image/png",
    ";image/png",
    "text/html",
    "application/xhtml+xml",
    "text/css",
    "text/vtt;,text/html",
    "text/vtt,text/html",
    'text/vtt;x="a",text/html',
    "application/pdf",
    "application/javascript",
    "",
    null,
    undefined,
  ]) {
    const got = knowledgeAssetContentType(raw);
    ok(
      `${JSON.stringify(raw)} is an octet-stream attachment`,
      got.contentType === "application/octet-stream" && got.inline === false,
    );
  }
  for (const [raw, clean] of [
    ["image/png", "image/png"],
    [" IMAGE/PNG ", "image/png"],
    ["image/png; q=1", "image/png"],
    ["image/jpg", "image/jpeg"],
    ["video/mp4; codecs=avc1", "video/mp4"],
    ["audio/x-wav", "audio/wav"],
    ["application/font-woff", "font/woff"],
    ["text/vtt", "text/vtt"],
    ["TEXT/VTT; charset=utf-8", "text/vtt"],
  ]) {
    const got = knowledgeAssetContentType(raw);
    ok(
      `${JSON.stringify(raw)} is inline as ${clean}`,
      got.contentType === clean && got.inline === true,
    );
  }
  ok(
    "every inline type is image, audio, video, font or text/vtt, and never SVG",
    Object.values(INLINE_ASSET_TYPES).every(
      (t) =>
        (/^(image|audio|video|font)\/[a-z0-9.+-]+$/.test(t) || t === "text/vtt") &&
        t !== "image/svg+xml",
    ),
  );
  ok(
    "no inherited key is honoured",
    knowledgeAssetContentType("constructor").inline === false &&
      knowledgeAssetContentType("__proto__").inline === false,
  );
  check("the asset CSP", KNOWLEDGE_ASSET_CSP, "default-src 'none'; sandbox");

  const bad = new Headers({ "Content-Disposition": "inline" });
  hardenKnowledgeAssetHeaders(bad, "image/png;a,text/html");
  check("harden: smuggled type -> octet-stream", bad.get("content-type"), "application/octet-stream");
  check("...attachment", bad.get("content-disposition"), "attachment");
  check("...nosniff", bad.get("x-content-type-options"), "nosniff");
  check("...sandbox CSP", bad.get("content-security-policy"), KNOWLEDGE_ASSET_CSP);

  const good = new Headers({ "Content-Disposition": "attachment" });
  hardenKnowledgeAssetHeaders(good, "image/png; q=1");
  check("harden: image/png stays inline with the clean type", good.get("content-type"), "image/png");
  check("...no Content-Disposition", good.get("content-disposition"), null);
  check("...still nosniff", good.get("x-content-type-options"), "nosniff");
  check("...still the sandbox CSP", good.get("content-security-policy"), KNOWLEDGE_ASSET_CSP);

  const dup = new Headers();
  dup.append("Content-Type", "image/png");
  dup.append("Content-Type", "text/html");
  check(
    "duplicate upstream Content-Type headers (joined with ', ') fail closed",
    knowledgeAssetContentType(dup.get("content-type")).inline,
    false,
  );
}

console.log(
  failures === 0
    ? `\nproxy headers: ${passes} passed, 0 failed`
    : `\nproxy headers: ${passes} passed, ${failures} FAILED`,
);
process.exit(failures === 0 ? 0 : 1);
