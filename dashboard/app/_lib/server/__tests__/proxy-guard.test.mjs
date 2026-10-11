/**
 * The two guards in front of the keyed proxy at app/api/pai/[...path]:
 * WHO may use it (the household pairing cookie) and WHAT they may reach with
 * the appliance's key (PROXY_ALLOWED_PREFIXES).
 *
 *   npm run test:proxy   (or: node app/_lib/server/__tests__/proxy-guard.test.mjs)
 *
 * Plain Node, matching the pairing tests next to it. Requires Node >=22.6 for
 * native TypeScript type stripping. Exits non-zero on any failure.
 *
 * SAFETY: PUFF_PAIRING_STATE and PUFF_PAIRED_DEVICES point at a fresh OS-temp
 * directory BEFORE pairing.ts is imported, so no household state is read or
 * written. The secret below is a fixed test fixture; the appliance's real
 * secret never appears in this repo.
 *
 * Lives under app/_lib/ (underscore-private), so Next never routes it.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE_FOR_ROUTE = path.dirname(fileURLToPath(import.meta.url));

/* ── sandbox: set before the first pairing import ──────────────────── */

const DIR = fs.mkdtempSync(path.join(os.tmpdir(), "puff-proxy-guard-"));
const STATE_FILE = path.join(DIR, "pairing.json");
const DEVICES_FILE = path.join(DIR, "devices.json");
process.env.PUFF_PAIRING_STATE = STATE_FILE;
process.env.PUFF_PAIRED_DEVICES = DEVICES_FILE;
// The live appliance runs "all". Pin it so a stray value in the environment
// cannot change what these assertions mean.
process.env.PUFF_PAIRING_ENFORCE = "all";

/** Fixed, well-formed 32-byte hex. A test fixture, not anybody's secret. */
const SECRET = "b".repeat(64);
fs.writeFileSync(
  STATE_FILE,
  JSON.stringify({
    version: 1,
    secret: SECRET,
    code: "7XK4P2M9",
    createdAt: new Date().toISOString(),
    rotatedAt: new Date().toISOString(),
  }),
);
/* One registered device, written BEFORE pairing.ts is imported: a v2 cookie
   only verifies for an id that is in the registry, so membership is what makes
   the "paired" half of these assertions mean anything. */
const DEVICE_ID = "TestDevice01";
fs.writeFileSync(
  DEVICES_FILE,
  JSON.stringify({
    version: 1,
    devices: [
      {
        id: DEVICE_ID,
        name: "Test device",
        createdAt: new Date().toISOString(),
        lastSeenAt: null,
      },
    ],
  }),
);

const { issueDeviceCookieValueV2, requirePairing, PAIRING_COOKIE } =
  await import("../pairing.ts");
const { PROXY_ALLOWED_PREFIXES, isAllowedProxyPath, notAllowedResponse } =
  await import("../proxyAllowlist.ts");

const COOKIE = issueDeviceCookieValueV2(DEVICE_ID);

let pass = 0;
let fail = 0;
function check(label, got, want) {
  const ok = got === want;
  if (ok) pass++;
  else fail++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}  -> ${got} (want ${want})`);
}

const ORIGIN = "http://10.0.0.9:3003";
function req(pathname, { paired = false, method = "GET" } = {}) {
  const headers = { origin: ORIGIN, host: "10.0.0.9:3003" };
  if (paired) headers.cookie = `${PAIRING_COOKIE}=${COOKIE}`;
  return new Request(ORIGIN + pathname, { method, headers });
}

/* ── 1. the pairing gate on /api/pai (the WHO) ─────────────────────── */
//
// This is what makes an unauthenticated LAN client a 401 rather than a client
// holding the owner's key. requirePairing() is the route's own second layer;
// middleware.ts runs the same check first via its /api/pai/:path* matcher.

console.log("\n--- unpaired LAN client is refused ---");
check(
  "GET /api/pai/apps/calendar/data, no cookie",
  requirePairing(req("/api/pai/apps/calendar/data"))?.status ?? 0,
  401,
);
check(
  "POST /api/pai/ha/service/light/turn_on, no cookie",
  requirePairing(req("/api/pai/ha/service/light/turn_on", { method: "POST" }))?.status ?? 0,
  401,
);
check(
  "GET /api/pai/vault/contents, no cookie",
  requirePairing(req("/api/pai/vault/contents"))?.status ?? 0,
  401,
);
check(
  "garbage cookie value is not a pass",
  (() => {
    const r = new Request(ORIGIN + "/api/pai/apps/calendar/data", {
      headers: { cookie: `${PAIRING_COOKIE}=v2.TestDevice01.1.2.deadbeef`, origin: ORIGIN },
    });
    return requirePairing(r)?.status ?? 0;
  })(),
  401,
);

console.log("\n--- paired client passes the gate ---");
check(
  "GET /api/pai/apps/calendar/data, valid cookie",
  requirePairing(req("/api/pai/apps/calendar/data", { paired: true })),
  null,
);
check(
  "POST /api/pai/ha/service/light/turn_on, valid cookie",
  requirePairing(req("/api/pai/ha/service/light/turn_on", { paired: true, method: "POST" })),
  null,
);
// The pairing endpoints must stay reachable or a fresh device could never pair.
check(
  "POST /api/pair is never gated",
  requirePairing(req("/api/pair", { method: "POST" })),
  null,
);

/* ── 2. the allowlist (the WHAT) ───────────────────────────────────── */

console.log("\n--- allowed: the surface the dashboard actually calls ---");
check("apps/calendar/data", isAllowedProxyPath(["apps", "calendar", "data"]), true);
check("ha/service/light/turn_on", isAllowedProxyPath(["ha", "service", "light", "turn_on"]), true);
check("knowledge/upload", isAllowedProxyPath(["knowledge", "upload"]), true);
check("widgets/sync (not a surface in this build)", isAllowedProxyPath(["widgets", "sync"]), false);
check("api/media/control", isAllowedProxyPath(["api", "media", "control"]), true);
// Settings > Home point (HomePointCard.tsx). Rides the existing "api" prefix;
// if it were not allowed, paired browsers would get 404 while :8000 said 200.
check("api/home-point", isAllowedProxyPath(["api", "home-point"]), true);
check("v1/chat/completions", isAllowedProxyPath(["v1", "chat", "completions"]), true);
check("health", isAllowedProxyPath(["health"]), true);
// Directions page (app/dashboard/directions). Missing = 404 for paired browsers.
check("maps/routes/latest", isAllowedProxyPath(["maps", "routes", "latest"]), true);

console.log("\n--- refused with 404: anything not on the list ---");
check("admin", isAllowedProxyPath(["admin"]), false);
check("shutdown", isAllowedProxyPath(["shutdown"]), false);
check("config/secrets", isAllowedProxyPath(["config", "secrets"]), false);
check("apikey", isAllowedProxyPath(["apikey"]), false);
check("empty path", isAllowedProxyPath([]), false);
check("the refusal is a 404, not a 403", notAllowedResponse().status, 404);

console.log("\n--- refused: traversal and separator tricks ---");
check("..", isAllowedProxyPath([".."]), false);
check("apps/../admin", isAllowedProxyPath(["apps", "..", "admin"]), false);
check("apps/.", isAllowedProxyPath(["apps", "."]), false);
// Next decodes catch-all segments, so %2f arrives as a segment containing "/".
check("decoded %2f inside a segment", isAllowedProxyPath(["apps", "a/../../admin"]), false);
check("decoded %5c inside a segment", isAllowedProxyPath(["apps", "a\\b"]), false);
check("space in a segment", isAllowedProxyPath(["apps", "a b"]), false);
check("newline in a segment", isAllowedProxyPath(["apps", "a\nb"]), false);
check("empty middle segment", isAllowedProxyPath(["apps", "", "data"]), false);
check("over-long segment", isAllowedProxyPath(["apps", "a".repeat(300)]), false);
check("query smuggled into a segment", isAllowedProxyPath(["apps", "a?b=c"]), false);

console.log("\n--- refused: case games on the first segment ---");
check("HA (uppercase)", isAllowedProxyPath(["HA", "status"]), false);
check("Apps (mixed)", isAllowedProxyPath(["Apps", "calendar", "data"]), false);

console.log("\n--- refused: non-string / hostile input ---");
check("not an array", isAllowedProxyPath("apps"), false);
check("null segment", isAllowedProxyPath([null]), false);
check("number segment", isAllowedProxyPath(["apps", 5]), false);

console.log("\n--- the two guards are independent ---");
// A paired device still cannot reach an unlisted path, and an unpaired one
// still cannot reach a listed one. Neither guard substitutes for the other.
check(
  "paired + unlisted path is still refused",
  requirePairing(req("/api/pai/admin/keys", { paired: true })) === null &&
    isAllowedProxyPath(["admin", "keys"]),
  false,
);
check(
  "unpaired + listed path is still 401",
  requirePairing(req("/api/pai/apps/calendar/data"))?.status ?? 0,
  401,
);

/* ── 2b. the forwarded-header policy ───────────────────────────────── */
//
// A source-level guard, and honest about being one: the route handler cannot be
// imported here (it pulls in next/server and "@/" path aliases). The header
// POLICY itself moved out to app/_lib/server/proxyHeaders.ts, which IS a pure
// importable function — its runtime behaviour (which headers cross, on which
// paths, and where the device identity comes from) is covered assertion by
// assertion in __tests__/proxy-headers.test.mjs. What is left here is the cheap,
// likely regression: someone "simplifying" the relay, or the route quietly
// rebuilding its own header map beside the policy.
//
// Why it matters in BOTH directions:
//   - drop X-Vault-Token and the vault UI silently breaks: the browser unlocks
//     fine, then every /vault/* call reaches the backend with no session token
//     and is refused 403 "Vault is locked".
//   - relay X-* wholesale and a client can hand itself X-Puff-Display: kiosk,
//     or put an X-Puff-Sensitive of its own choosing into the map.
{
  const routeSrc = fs.readFileSync(
    path.resolve(HERE_FOR_ROUTE, "../../../api/pai/[...path]/route.ts"),
    "utf8",
  );
  const policySrc = fs.readFileSync(
    path.resolve(HERE_FOR_ROUTE, "../proxyHeaders.ts"),
    "utf8",
  );
  check(
    "the policy relays X-Vault-Token by name",
    /upstream:\s*["']X-Vault-Token["']/.test(policySrc),
    true,
  );
  check(
    "...reading that header, by name, off the request",
    /header:\s*["']x-vault-token["']/.test(policySrc) &&
      /req\.headers\.get\(\s*rule\.header\s*\)/.test(policySrc),
    true,
  );
  check(
    "policy does NOT relay X-* wholesale",
    /startsWith\(\s*["']x-["']\s*\)/.test(policySrc),
    false,
  );
  check(
    "route does NOT relay X-* wholesale",
    /startsWith\(\s*["']x-["']\s*\)/.test(routeSrc),
    false,
  );
  // Scoped to the vault subtree: the session token is meaningless to /apps,
  // /ha, /knowledge and the rest, so it must not be handed to them.
  check(
    "the vault-token relay is scoped to /vault/*",
    /path\[0\]\s*===\s*["']vault["']/.test(policySrc),
    true,
  );
  check(
    "...and the route passes the path in, so the scope is live",
    // The identity argument is session.sessionId, decided by
    // proxyDeviceSessionFor(req) just above the call — null there means "assert
    // no identity", which upstreamHeaders turns into X-Puff-Anonymous. What
    // matters here is that `path` follows it, then the server-generated client
    // address (never anything read off the request).
    /upstreamHeaders\(\s*req\s*,\s*API_KEY\s*,\s*session\.sessionId\s*,\s*path\s*,\s*clientAddr\(\s*req\s*\)\s*,?\s*\)/.test(
      routeSrc,
    ) && /const\s+session\s*=\s*proxyDeviceSessionFor\(\s*req\s*\)/.test(routeSrc),
    true,
  );
  check(
    "the route builds no header map of its own beside the policy",
    /const\s+headers\s*:\s*Record<string,\s*string>\s*=\s*\{\}/.test(routeSrc),
    false,
  );
  check(
    "route does not forward cookies upstream",
    /headers\[\s*["']Cookie["']\s*\]/i.test(routeSrc),
    false,
  );
  check(
    "policy does not forward cookies upstream",
    /headers\[\s*["']Cookie["']\s*\]/i.test(policySrc),
    false,
  );
}

/* ── 3. the allowlist covers the source ────────────────────────────── */

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../../../..");
const files = [];
function walk(d, exts) {
  if (!fs.existsSync(d)) return;
  for (const e of fs.readdirSync(d, { withFileTypes: true })) {
    if (e.name === "node_modules" || e.name === ".next" || e.name === "__tests__") continue;
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p, exts);
    else if (exts.test(e.name)) files.push(p);
  }
}
walk(path.join(ROOT, "app"), /\.(ts|tsx|js|jsx|mjs|html)$/);
walk(path.join(ROOT, "public"), /\.html$/);

const BASES = "API_URL|API_BASE|apiBase|CAM_API_BASE|API_ROOT|SYNC_API|BASE";
const PATTERNS = [
  /\/api\/pai\/([A-Za-z0-9_.-]+)/g,
  new RegExp("\\$\\{\\s*(?:" + BASES + ")\\s*\\}\\/([A-Za-z0-9_.-]+)", "g"),
  new RegExp("\\b(?:" + BASES + ")\\s*\\+\\s*[`'\"]\\/([A-Za-z0-9_.-]+)", "g"),
  /\b(?:apiGet|apiPost|apiPut|apiDelete|apiStream|req)\s*(?:<[^>]*>)?\s*\(\s*[`'"]\/([A-Za-z0-9_.-]+)/g,
];
// SYNC_API in the app pages is itself `/api/pai/apps/<app>`, so its suffixes
// are sub-paths of "apps" rather than first segments. "X" is a placeholder in
// a comment that explains the prefix-stripping, and "..." is the "/api/pai/..."
// shorthand in a doc comment.
const NOT_A_PREFIX = new Set(["data", "updated", "action", "pending", "X", ".", "..", "..."]);
/** Files that enforce the rules rather than call through them. See the loop. */
const NOT_A_CALLER = /(^|[\\/])app[\\/]_lib[\\/]server[\\/]/;

const found = new Map();
for (const f of files) {
  // app/_lib/server/ is the ENFORCEMENT code (pairing gate, allowlist, proxy
  // guards). It documents hostile example paths on purpose, so scanning it for
  // "call sites" reads those examples as things the dashboard calls. It is not
  // a caller; the UI is.
  if (NOT_A_CALLER.test(path.relative(ROOT, f))) continue;
  const src = fs.readFileSync(f, "utf8");
  const usesProxy =
    /["'`]\/api\/pai/.test(src) ||
    /from\s+["'][^"']*\/api["']/.test(src) ||
    new RegExp("\\b(?:" + BASES + ")\\b").test(src);
  if (!usesProxy) continue;
  for (const re of PATTERNS) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(src))) {
      const seg = m[1];
      if (!seg || NOT_A_PREFIX.has(seg)) continue;
      if (!found.has(seg)) found.set(seg, path.relative(ROOT, f));
    }
  }
}

console.log("\n--- every proxy path in the source is allowlisted ---");
const missing = [...found.entries()].filter(([seg]) => !isAllowedProxyPath([seg]));
for (const [seg, f] of missing) {
  fail++;
  console.log(
    `FAIL  the dashboard calls /api/pai/${seg} (${f}) but "${seg}" is not in ` +
      `PROXY_ALLOWED_PREFIXES — that call will 404. Add it deliberately, or ` +
      `stop calling it from the browser.`,
  );
}
if (missing.length === 0) {
  pass++;
  console.log(`PASS  ${found.size} distinct proxy prefixes found in source, all allowlisted`);
}

// The reverse direction is a note, not a failure: an entry can legitimately be
// listed slightly ahead of the code that uses it, and an unused entry is a much
// smaller problem than a missing one.
const unused = PROXY_ALLOWED_PREFIXES.filter((p) => !found.has(p));
if (unused.length) console.log(`NOTE  listed but not seen in source: ${unused.join(", ")}`);

fs.rmSync(DIR, { recursive: true, force: true });
console.log(`\n${pass} passed, ${fail} failed`);
assert.equal(fail, 0, `${fail} proxy-guard assertion(s) failed`);
