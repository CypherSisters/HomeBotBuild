/**
 * The loopback media/books proxies and the Jellyfin CSP origin fail closed.
 *
 *   npm run test:lantargets
 *   node app/_lib/server/__tests__/lan-targets.test.mjs
 *
 * Requires Node >=22.6 for native TypeScript type stripping. Exits non-zero on
 * any failure.
 *
 * Asserted:
 *   1. with the env unset, empty or malformed, neither proxy has a target, so
 *      neither starts;
 *   2. a loopback target on the proxy's own listen port is refused;
 *   3. the CSP Jellyfin list is loopback-only unless JELLYFIN_LAN_HOST is a
 *      plain host, and a hostile value cannot add a directive or a source;
 *   4. instrumentation.ts and middleware.ts actually use these decisions and
 *      carry no hardcoded LAN address (asserted against their source).
 *
 * Lives under app/_lib/ (underscore-private), so Next never routes it.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DASHBOARD = path.resolve(HERE, "..", "..", "..", "..");

const L = await import("../lanTargets.ts");

let pass = 0;
let fail = 0;
function ok(name, cond) {
  if (cond) pass++;
  else fail++;
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
}

/* ── 1. no env, no proxy ───────────────────────────────────────────── */

for (const [label, env] of [
  ["unset", {}],
  ["empty", { JELLYFIN_LAN_HOST: "", CALIBRE_LAN_HOST: "" }],
  ["whitespace", { JELLYFIN_LAN_HOST: "   ", CALIBRE_LAN_HOST: "\t" }],
  ["only the fallback IP", { JELLYFIN_LAN_IP: "192.168.1.20" }],
  ["only the ports", { JELLYFIN_PROXY_PORT: "8096", CALIBRE_PROXY_PORT: "8083" }],
]) {
  ok(`media proxy does not start: env ${label}`, L.mediaProxyTarget(env).ok === false);
  ok(`books proxy does not start: env ${label}`, L.booksProxyTarget(env).ok === false);
}
ok(
  "the refusal reason names the variable to set",
  /JELLYFIN_LAN_HOST not set/.test(L.mediaProxyTarget({}).reason) &&
    /CALIBRE_LAN_HOST not set/.test(L.booksProxyTarget({}).reason),
);
for (const bad of [
  "http://192.168.1.20",
  "192.168.1.20:8096",
  "host/path",
  "a b",
  '"x"',
  "x; script-src *",
  "[fe80::1]",
  "-leading",
]) {
  ok(`malformed host refused: ${JSON.stringify(bad)}`, L.mediaProxyTarget({ JELLYFIN_LAN_HOST: bad }).ok === false);
}
ok(
  "a malformed JELLYFIN_LAN_IP refuses rather than being ignored",
  L.mediaProxyTarget({ JELLYFIN_LAN_HOST: "media.lan", JELLYFIN_LAN_IP: "1.2.3.4; x" }).ok === false,
);
ok(
  "an invalid listen port refuses",
  L.mediaProxyTarget({ JELLYFIN_LAN_HOST: "media.lan", JELLYFIN_PROXY_PORT: "nope" }).ok === false,
);

/* ── 2. valid targets, and the self-loop refusal ───────────────────── */
{
  const d = L.mediaProxyTarget({ JELLYFIN_LAN_HOST: "192.168.1.20" });
  ok("a plain IPv4 host starts the media proxy", d.ok === true);
  ok("...forwarding to it on 8096", d.ok && d.target.targetHost === "192.168.1.20" && d.target.targetPort === 8096);
  ok("...and JELLYFIN_LAN_IP falls back to the host, not a literal", d.ok && d.target.fallbackAddr === "192.168.1.20");
  const d2 = L.mediaProxyTarget({ JELLYFIN_LAN_HOST: "media.lan", JELLYFIN_LAN_IP: "192.168.1.21" });
  ok("an explicit JELLYFIN_LAN_IP is used as the retry address", d2.ok && d2.target.fallbackAddr === "192.168.1.21");
  const b = L.booksProxyTarget({ CALIBRE_LAN_HOST: "books.lan" });
  ok("a plain hostname starts the books proxy on 8083", b.ok && b.target.targetHost === "books.lan" && b.target.listenPort === 8083);
}
for (const lo of ["127.0.0.1", "localhost", "127.1.2.3", "0.0.0.0", "box.localhost"]) {
  ok(`media: loopback target ${lo} on the listen port is refused`, L.mediaProxyTarget({ JELLYFIN_LAN_HOST: lo }).ok === false);
  ok(`books: loopback target ${lo} on the listen port is refused`, L.booksProxyTarget({ CALIBRE_LAN_HOST: lo }).ok === false);
}
ok(
  "a loopback retry address on the listen port is refused too",
  L.mediaProxyTarget({ JELLYFIN_LAN_HOST: "media.lan", JELLYFIN_LAN_IP: "127.0.0.1" }).ok === false,
);
ok(
  "loopback on a DIFFERENT listen port is allowed (not a self-loop)",
  L.mediaProxyTarget({ JELLYFIN_LAN_HOST: "127.0.0.1", JELLYFIN_PROXY_PORT: "18096" }).ok === true,
);

/* ── 3. the CSP origin list ────────────────────────────────────────── */

ok("CSP with the env unset is loopback only", L.jellyfinCspOrigins({}) === "http://localhost:8096");
ok(
  "CSP names a valid LAN host",
  L.jellyfinCspOrigins({ JELLYFIN_LAN_HOST: "192.168.1.20" }) === "http://localhost:8096 http://192.168.1.20:8096",
);
for (const hostile of [
  "x; script-src *",
  "x script-src *",
  "*",
  "'unsafe-inline'",
  "http://evil.example",
  "evil.example/path",
  "evil.example:80",
  "x\nscript-src *",
]) {
  const out = L.jellyfinCspOrigins({ JELLYFIN_LAN_HOST: hostile });
  ok(`CSP rejects hostile JELLYFIN_LAN_HOST ${JSON.stringify(hostile)}`, out === "http://localhost:8096");
}
ok("no CSP output ever contains a semicolon", ![
  {}, { JELLYFIN_LAN_HOST: "a;b" }, { JELLYFIN_LAN_HOST: "a.b" },
].some((env) => L.jellyfinCspOrigins(env).includes(";")));

/* ── 4. the real modules are wired this way ────────────────────────── */
{
  const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/[^\n]*/g, "$1");
  const inst = fs.readFileSync(path.join(DASHBOARD, "instrumentation.ts"), "utf8");
  const mw = fs.readFileSync(path.join(DASHBOARD, "middleware.ts"), "utf8");
  for (const [name, src] of [["instrumentation.ts", inst], ["middleware.ts", mw]]) {
    ok(`${name} carries no 192.168.50.40`, !src.includes("192.168.50.40"));
    ok(`${name} carries no hardcoded private IPv4 in code`,
      !/\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b/.test(strip(src)));
  }
  const code = strip(inst);
  ok("instrumentation decides the media target via mediaProxyTarget(process.env)", /mediaProxyTarget\(process\.env\)/.test(code));
  ok("instrumentation decides the books target via booksProxyTarget(process.env)", /booksProxyTarget\(process\.env\)/.test(code));
  ok("instrumentation returns before listening when refused",
    (code.match(/if\s*\(\s*!decision\.ok\s*\)\s*\{\s*console\.log\([^;]*\);\s*return;\s*\}/g) || []).length === 2);
  ok("instrumentation reads no *_LAN_HOST/_IP directly",
    !/process\.env\.(JELLYFIN_LAN_HOST|JELLYFIN_LAN_IP|CALIBRE_LAN_HOST)/.test(code));
  ok("middleware builds the Jellyfin CSP list from jellyfinCspOrigins(process.env)",
    /const JELLYFIN = jellyfinCspOrigins\(process\.env\);/.test(strip(mw)));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
