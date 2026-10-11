/**
 * Device-cookie assertions: v1 compatibility, v2 identity + revocation,
 * sliding renewal, and the pairing-code TTL. Plain Node — this repo has no test
 * runner, and the module under test is pure enough not to need one.
 *
 *   npm run test:pairing        (runs this and pairing-gate.test.mjs)
 *   node app/_lib/server/__tests__/pairing-cookie.test.mjs
 *
 * Requires Node >=22.6 for native TypeScript type stripping. Exits non-zero on
 * any failure.
 *
 * SAFETY: PUFF_PAIRING_STATE and PUFF_PAIRED_DEVICES are pointed at a fresh
 * directory under the OS temp dir BEFORE pairing.ts is imported, so no real
 * household state is read or written. The secret used here is a fixed test
 * value; the appliance's real secret never appears in this repo.
 *
 * Lives under app/_lib/ (underscore-private), so Next never routes it.
 */
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/* ── sandbox ───────────────────────────────────────────────────────── */

const DIR = fs.mkdtempSync(path.join(os.tmpdir(), "puff-pairing-test-"));
const STATE_FILE = path.join(DIR, "pairing.json");
const DEVICES_FILE = path.join(DIR, "devices.json");

process.env.PUFF_PAIRING_STATE = STATE_FILE;
process.env.PUFF_PAIRED_DEVICES = DEVICES_FILE;
// Not used by anything here, but make sure a stray enforcement value in the
// environment cannot change what these assertions mean.
process.env.PUFF_PAIRING_ENFORCE = "all";

/** Fixed, well-formed 32-byte hex. A test fixture, not anybody's secret. */
const SECRET = "a".repeat(64);
const CODE = "7XK4P2M9";

function writeState(overrides = {}) {
  const now = new Date().toISOString();
  fs.writeFileSync(
    STATE_FILE,
    JSON.stringify(
      {
        version: 1,
        secret: SECRET,
        code: CODE,
        createdAt: now,
        codeRotatedAt: now,
        ...overrides,
      },
      null,
      2,
    ),
  );
}

writeState();
fs.writeFileSync(
  DEVICES_FILE,
  JSON.stringify({ version: 1, devices: [] }, null, 2),
);

const {
  DEVICE_ID_RE,
  PAIR_CODE_TTL_SEC,
  RENEW_AFTER_SEC,
  addPairedDevice,
  findPairedDevice,
  formatCode,
  getPairingCodeInfo,
  getPairingState,
  isPaired,
  issueDeviceCookieValue,
  issueDeviceCookieValueV2,
  listPairedDevices,
  normalizeCode,
  pairedDeviceIdFor,
  renamePairedDevice,
  renewalCookieFor,
  revokePairedDevice,
  rotatePairingCode,
  shouldRenewDeviceCookie,
  verifyCode,
  verifyDeviceCookieIdentity,
  verifyDeviceCookieValue,
} = await import("../pairing.ts");

/* ── harness ───────────────────────────────────────────────────────── */

let pass = 0;
let fail = 0;

function check(label, got, want) {
  const ok = Object.is(got, want);
  if (ok) pass++;
  else fail++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}  -> ${got} (want ${want})`);
}

function section(name) {
  console.log(`\n--- ${name} ---`);
}

/** True if `fn` threw. Used to assert that a failure is NOT swallowed. */
function threw(fn) {
  try {
    fn();
    return false;
  } catch {
    return true;
  }
}

/**
 * Run `fn` with fs.readFileSync failing for any path ending in `suffix`.
 *
 * Deterministic and portable — no chmod (a no-op on Windows) and no reliance on
 * hitting a real resource limit. pairing.ts does `import fs from "node:fs"` and
 * calls `fs.readFileSync(...)` as a property lookup at call time, so it shares
 * this exact object and sees the patch. EMFILE is the code the real incident
 * class produces (a burst of parallel kiosk requests exhausting descriptors).
 */
function withReadFailure(suffix, fn) {
  const real = fs.readFileSync;
  fs.readFileSync = function (p, ...rest) {
    if (String(p).endsWith(suffix)) {
      const err = new Error("EMFILE: too many open files, open");
      err.code = "EMFILE";
      throw err;
    }
    return real.call(this, p, ...rest);
  };
  try {
    return fn();
  } finally {
    fs.readFileSync = real;
  }
}

/* ── cookie construction helpers (independent of the module) ───────── */

function sign(payload) {
  return crypto
    .createHmac("sha256", Buffer.from(SECRET, "hex"))
    .update(payload)
    .digest("hex");
}

function v1Cookie(expEpochSec) {
  const payload = `v1.${expEpochSec}`;
  return `${payload}.${sign(payload)}`;
}

function v2Cookie(deviceId, issued, exp, version = "v2") {
  const payload = `${version}.${deviceId}.${issued}.${exp}`;
  return `${payload}.${sign(payload)}`;
}

/** Flip the last hex nibble of the mac — same length, same charset. */
function tamperMac(value) {
  const parts = value.split(".");
  const mac = parts[parts.length - 1];
  const last = mac.slice(-1);
  parts[parts.length - 1] = mac.slice(0, -1) + (last === "0" ? "1" : "0");
  return parts.join(".");
}

function carrier(cookieValue) {
  return {
    headers: new Headers(
      cookieValue === undefined
        ? {}
        : { cookie: `puff_device=${cookieValue}` },
    ),
  };
}

const SEC = 1000;
const NOW = Date.now();
const nowSec = Math.floor(NOW / 1000);

/* ── v1: must keep verifying, forever ──────────────────────────────── */

section("v1 legacy cookies");

const v1 = issueDeviceCookieValue(NOW);
check("v1 issue -> verify roundtrip", verifyDeviceCookieValue(v1, NOW), true);
check(
  "v1 issued value matches an independently computed HMAC",
  v1 === v1Cookie(nowSec + 365 * 24 * 60 * 60),
  true,
);
check(
  "v1 identity carries no device id",
  verifyDeviceCookieIdentity(v1, NOW).deviceId,
  null,
);
check(
  "v1 identity reports its version",
  verifyDeviceCookieIdentity(v1, NOW).version,
  "v1",
);
check(
  "expired v1 rejected",
  verifyDeviceCookieValue(v1Cookie(nowSec - 10), NOW),
  false,
);
check(
  "v1 expiring exactly now rejected (<=, not <)",
  verifyDeviceCookieValue(v1Cookie(nowSec), nowSec * 1000),
  false,
);
check("tampered v1 mac rejected", verifyDeviceCookieValue(tamperMac(v1), NOW), false);
check(
  "v1 signed with a different secret rejected",
  verifyDeviceCookieValue(`v1.${nowSec + 100}.${"b".repeat(64)}`, NOW),
  false,
);
check("empty cookie rejected", verifyDeviceCookieValue("", NOW), false);
check("undefined cookie rejected", verifyDeviceCookieValue(undefined, NOW), false);
check("garbage cookie rejected", verifyDeviceCookieValue("not.a.cookie", NOW), false);
check(
  "v1 with non-numeric expiry rejected",
  verifyDeviceCookieValue(`v1.abc.${sign("v1.abc")}`, NOW),
  false,
);

/* ── v2: identity + registry membership ────────────────────────────── */

section("v2 device cookies");

const deviceA = addPairedDevice("Wall kiosk", NOW);
check("new device id matches DEVICE_ID_RE", DEVICE_ID_RE.test(deviceA.id), true);
check("new device id is 12 chars", deviceA.id.length, 12);
check("new device id has no dot", deviceA.id.indexOf(".") === -1, true);
check("new device name sanitised through", deviceA.name, "Wall kiosk");

const a2 = issueDeviceCookieValueV2(deviceA.id, NOW);
check("v2 issue -> verify roundtrip", verifyDeviceCookieValue(a2, NOW), true);
check("v2 has five dot-separated fields", a2.split(".").length, 5);
check(
  "v2 identity reports the device id",
  verifyDeviceCookieIdentity(a2, NOW).deviceId,
  deviceA.id,
);
check(
  "v2 identity reports the signed issue time",
  verifyDeviceCookieIdentity(a2, NOW).issued,
  nowSec,
);
check(
  "v2 issued value matches an independently computed HMAC",
  a2 === v2Cookie(deviceA.id, nowSec, nowSec + 365 * 24 * 60 * 60),
  true,
);
check(
  "pairedDeviceIdFor reads the id off a request",
  pairedDeviceIdFor(carrier(a2), NOW),
  deviceA.id,
);
check(
  "pairedDeviceIdFor is null for a v1 caller",
  pairedDeviceIdFor(carrier(v1), NOW),
  null,
);
check(
  "pairedDeviceIdFor is null for no cookie",
  pairedDeviceIdFor(carrier(undefined), NOW),
  null,
);

section("v2 rejections");

check(
  "wrong version string rejected",
  verifyDeviceCookieValue(
    v2Cookie(deviceA.id, nowSec, nowSec + 1000, "v3"),
    NOW,
  ),
  false,
);
check(
  "v1 version string in a 5-field cookie rejected",
  verifyDeviceCookieValue(
    v2Cookie(deviceA.id, nowSec, nowSec + 1000, "v1"),
    NOW,
  ),
  false,
);
check(
  "expired v2 rejected",
  verifyDeviceCookieValue(v2Cookie(deviceA.id, nowSec - 100, nowSec - 10), NOW),
  false,
);
check("tampered v2 mac rejected", verifyDeviceCookieValue(tamperMac(a2), NOW), false);
{
  // Swap the device id for another registered one, keeping the original mac.
  const other = addPairedDevice("Decoy", NOW);
  const parts = a2.split(".");
  parts[1] = other.id;
  check(
    "tampered device id rejected (mac no longer covers it)",
    verifyDeviceCookieValue(parts.join("."), NOW),
    false,
  );
  revokePairedDevice(other.id);
}
{
  // Correctly signed, well-formed id that was never registered.
  const ghost = crypto.randomBytes(9).toString("base64url");
  check(
    "unknown (unregistered) device id rejected despite a valid mac",
    verifyDeviceCookieValue(v2Cookie(ghost, nowSec, nowSec + 1000), NOW),
    false,
  );
}
check(
  "malformed device id rejected (wrong length)",
  verifyDeviceCookieValue(v2Cookie("short", nowSec, nowSec + 1000), NOW),
  false,
);
check(
  "device id containing a dot cannot be signed",
  (() => {
    try {
      issueDeviceCookieValueV2("abc.defghijk", NOW);
      return "no throw";
    } catch {
      return "threw";
    }
  })(),
  "threw",
);
check(
  "non-numeric issued rejected",
  verifyDeviceCookieValue(
    (() => {
      const p = `v2.${deviceA.id}.x.${nowSec + 1000}`;
      return `${p}.${sign(p)}`;
    })(),
    NOW,
  ),
  false,
);
check(
  "four-field cookie rejected",
  verifyDeviceCookieValue(`v2.${deviceA.id}.${nowSec}.${sign("nope")}`, NOW),
  false,
);

/* ── revocation ────────────────────────────────────────────────────── */

section("revocation");

check("device is listed before revoke", !!findPairedDevice(deviceA.id), true);
check("cookie verifies before revoke", verifyDeviceCookieValue(a2, NOW), true);
check("revoke reports success", revokePairedDevice(deviceA.id), true);
check(
  "revoked cookie rejected IMMEDIATELY (no restart, no cache)",
  verifyDeviceCookieValue(a2, NOW),
  false,
);
check("revoke is idempotent-false", revokePairedDevice(deviceA.id), false);
check(
  "revoke of a malformed id is false, not a throw",
  revokePairedDevice("../../etc/passwd"),
  false,
);
check("device gone from the list", findPairedDevice(deviceA.id), null);

/* ── registry file trouble: v2 fails closed, v1 unaffected ─────────── */

section("registry file missing / corrupt");

const deviceB = addPairedDevice("Alex's phone", NOW);
const b2 = issueDeviceCookieValueV2(deviceB.id, NOW);
check("v2 verifies with the registry present", verifyDeviceCookieValue(b2, NOW), true);

fs.rmSync(DEVICES_FILE);
check(
  "missing devices.json -> v2 REJECTED (fail closed)",
  verifyDeviceCookieValue(b2, NOW),
  false,
);
check(
  "missing devices.json -> v1 for the SAME secret still ACCEPTED",
  verifyDeviceCookieValue(v1, NOW),
  true,
);
check("missing devices.json -> empty list, no throw", listPairedDevices().length, 0);

fs.writeFileSync(DEVICES_FILE, "{ this is not json");
check(
  "corrupt devices.json -> v2 REJECTED",
  verifyDeviceCookieValue(b2, NOW),
  false,
);
check(
  "corrupt devices.json -> v1 still ACCEPTED",
  verifyDeviceCookieValue(v1, NOW),
  true,
);

// A file whose records are individually bad: drop the bad ones, keep the good.
fs.writeFileSync(
  DEVICES_FILE,
  JSON.stringify({
    version: 1,
    devices: [
      { id: "not a valid id", name: "x", createdAt: "now", lastSeenAt: null },
      { id: deviceB.id, name: "Alex's phone", createdAt: "now", lastSeenAt: null },
      { nonsense: true },
    ],
  }),
);
check("invalid records filtered on read", listPairedDevices().length, 1);
check("valid record survives filtering", verifyDeviceCookieValue(b2, NOW), true);

/* ── rename ────────────────────────────────────────────────────────── */

section("rename");

check("rename reports success", renamePairedDevice(deviceB.id, "  Kitchen  wall "), true);
check("rename collapses whitespace", findPairedDevice(deviceB.id).name, "Kitchen wall");
check(
  "rename strips control characters",
  (() => {
    renamePairedDevice(deviceB.id, "Hall way");
    return findPairedDevice(deviceB.id).name;
  })(),
  "Hall way",
);
check(
  "rename caps at 64 characters",
  (() => {
    renamePairedDevice(deviceB.id, "z".repeat(200));
    return findPairedDevice(deviceB.id).name.length;
  })(),
  64,
);
check("rename of an unknown id is false", renamePairedDevice("aaaaaaaaaaaa", "x"), false);
check("rename of a malformed id is false", renamePairedDevice("../x", "x"), false);

/* ── last seen ─────────────────────────────────────────────────────── */

section("lastSeenAt throttle");

{
  // addPairedDevice seeds the throttle, so force a stamp by aging the marker:
  // a verification more than an hour later must write, and an immediately
  // following one must not.
  const later = NOW + 2 * 60 * 60 * 1000;
  verifyDeviceCookieValue(b2, later);
  const first = findPairedDevice(deviceB.id).lastSeenAt;
  check("lastSeenAt stamped after the throttle window", first, new Date(later).toISOString());

  verifyDeviceCookieValue(b2, later + 60 * 1000);
  check(
    "lastSeenAt NOT rewritten a minute later (throttled)",
    findPairedDevice(deviceB.id).lastSeenAt,
    first,
  );

  verifyDeviceCookieValue(b2, later + 2 * 60 * 60 * 1000);
  check(
    "lastSeenAt rewritten two hours later",
    findPairedDevice(deviceB.id).lastSeenAt,
    new Date(later + 2 * 60 * 60 * 1000).toISOString(),
  );
}

/* ── renewal decision (pure) ───────────────────────────────────────── */

section("shouldRenewDeviceCookie");

check("fresh cookie is not renewed", shouldRenewDeviceCookie(nowSec, NOW), false);
check(
  "29-day-old cookie is not renewed",
  shouldRenewDeviceCookie(nowSec - (RENEW_AFTER_SEC - 24 * 60 * 60), NOW),
  false,
);
check(
  "exactly 30-day-old cookie IS renewed",
  shouldRenewDeviceCookie(nowSec - RENEW_AFTER_SEC, NOW),
  true,
);
check(
  "31-day-old cookie IS renewed",
  shouldRenewDeviceCookie(nowSec - (RENEW_AFTER_SEC + 24 * 60 * 60), NOW),
  true,
);
check(
  "future-dated cookie is not renewed (clock skew)",
  shouldRenewDeviceCookie(nowSec + 10_000, NOW),
  false,
);
check("NaN issue time is not renewed", shouldRenewDeviceCookie(NaN, NOW), false);

/* ── renewal + upgrade (request level) ─────────────────────────────── */

section("renewalCookieFor");

check("no cookie -> no renewal", renewalCookieFor(carrier(undefined), NOW), null);
check("invalid cookie -> no renewal", renewalCookieFor(carrier("junk"), NOW), null);
check(
  "expired cookie -> no renewal",
  renewalCookieFor(carrier(v1Cookie(nowSec - 10)), NOW),
  null,
);
check("fresh v2 -> no renewal", renewalCookieFor(carrier(b2), NOW), null);

{
  const old = NOW - (RENEW_AFTER_SEC + 60) * SEC;
  const aged = issueDeviceCookieValueV2(deviceB.id, old);
  const renewal = renewalCookieFor(carrier(aged), NOW);
  check("aged v2 -> renewal issued", renewal !== null, true);
  check(
    "renewal keeps the SAME device id",
    verifyDeviceCookieIdentity(renewal.value, NOW).deviceId,
    deviceB.id,
  );
  check(
    "renewal carries a fresh issue time",
    verifyDeviceCookieIdentity(renewal.value, NOW).issued,
    nowSec,
  );
  check("renewal cookie is HttpOnly", renewal.httpOnly, true);
  check("renewal cookie is SameSite=Lax", renewal.sameSite, "lax");
  check("renewal cookie is path /", renewal.path, "/");

  // A revoked device cannot renew its way back in.
  const doomed = addPairedDevice("Doomed", NOW);
  const doomedAged = issueDeviceCookieValueV2(doomed.id, old);
  revokePairedDevice(doomed.id);
  check(
    "revoked device cannot renew",
    renewalCookieFor(carrier(doomedAged), NOW),
    null,
  );
}

section("v1 -> v2 upgrade");

{
  const before = listPairedDevices().length;
  const first = renewalCookieFor(carrier(v1), NOW);
  check("v1 cookie -> upgrade issued", first !== null, true);
  const upgradedId = verifyDeviceCookieIdentity(first.value, NOW).deviceId;
  check("upgrade produced a registered v2 cookie", !!upgradedId, true);
  check("upgrade added exactly one registry row", listPairedDevices().length, before + 1);
  check("upgraded row is named 'Migrated device'", findPairedDevice(upgradedId).name, "Migrated device");

  // Concurrent requests carrying the same v1 cookie must not each mint a row.
  for (let i = 0; i < 8; i++) renewalCookieFor(carrier(v1), NOW);
  check(
    "8 more requests with the same v1 cookie add NO further rows (dedup)",
    listPairedDevices().length,
    before + 1,
  );
  check(
    "dedup reuses the same device id",
    verifyDeviceCookieIdentity(renewalCookieFor(carrier(v1), NOW).value, NOW).deviceId,
    upgradedId,
  );

  // A different v1 cookie is a different device.
  const otherV1 = v1Cookie(nowSec + 1000);
  const second = renewalCookieFor(carrier(otherV1), NOW);
  check("a different v1 cookie mints its own row", listPairedDevices().length, before + 2);
  check(
    "and its own device id",
    verifyDeviceCookieIdentity(second.value, NOW).deviceId !== upgradedId,
    true,
  );

  // Revoking an upgraded row must not be undone by the next request.
  revokePairedDevice(upgradedId);
  check(
    "revoked upgrade is NOT resurrected by the same v1 cookie",
    renewalCookieFor(carrier(v1), NOW),
    null,
  );
}

/* ── pairing code TTL ──────────────────────────────────────────────── */

section("pairing code TTL");

check("TTL is one hour", PAIR_CODE_TTL_SEC, 3600);

{
  const fresh = rotatePairingCode(NOW);
  const bare = normalizeCode(fresh);
  check("fresh code verifies", verifyCode(bare, NOW), true);
  check("fresh code verifies in its display form", verifyCode(fresh, NOW), true);
  check(
    "code just inside the TTL verifies",
    verifyCode(bare, NOW + PAIR_CODE_TTL_SEC * SEC - SEC),
    true,
  );
  check(
    "code at exactly the TTL is REJECTED",
    verifyCode(bare, NOW + PAIR_CODE_TTL_SEC * SEC),
    false,
  );
  check(
    "code past the TTL is REJECTED",
    verifyCode(bare, NOW + (PAIR_CODE_TTL_SEC + 60) * SEC),
    false,
  );
  check("wrong code rejected", verifyCode("22222222", NOW), false);
  check("wrong-length code rejected", verifyCode("222", NOW), false);
  check("empty code rejected", verifyCode("", NOW), false);

  // A failed redeem must not rotate — a stranger cannot spin the wheel.
  verifyCode("22222222", NOW + (PAIR_CODE_TTL_SEC + 60) * SEC);
  check(
    "a failed redeem does NOT rotate the code",
    getPairingState().code,
    bare,
  );

  // Display fetch after expiry hands back a NEW, redeemable code.
  const past = NOW + (PAIR_CODE_TTL_SEC + 60) * SEC;
  const info = getPairingCodeInfo(past);
  check("display fetch after expiry returns a NEW code", info.code !== fresh, true);
  check("the new code redeems", verifyCode(info.code, past), true);
  check("the old code no longer redeems", verifyCode(bare, past), false);
  check(
    "expiresAt is one TTL after rotatedAt",
    Date.parse(info.expiresAt) - Date.parse(info.rotatedAt),
    PAIR_CODE_TTL_SEC * SEC,
  );

  // Within the TTL, a display fetch is stable — it must not rotate on every
  // poll, or the QR on the wall would change under a scanning phone.
  const again = getPairingCodeInfo(past + 60 * SEC);
  check("display fetch inside the TTL does NOT rotate", again.code, info.code);

  // Manual rotate still works and still changes the code.
  const rotated = rotatePairingCode(NOW);
  check("manual rotate changes the code", rotated !== info.code, true);
  check("manual rotate produces a redeemable code", verifyCode(rotated, NOW), true);

  // An unparseable rotation timestamp counts as expired (fail closed).
  writeState({ codeRotatedAt: "not a date" });
  check("unparseable codeRotatedAt -> code rejected", verifyCode(CODE, NOW), false);
  const healed = getPairingCodeInfo(NOW);
  check("unparseable codeRotatedAt self-heals on display", verifyCode(healed.code, NOW), true);
}

/* ── unchanged helpers ─────────────────────────────────────────────── */

section("normalizeCode / formatCode unchanged");

check("formatCode groups in fours", formatCode("7XK4P2M9"), "7XK4-P2M9");
check("formatCode of an empty string", formatCode(""), "");
check("normalizeCode strips dashes", normalizeCode("7xk4-p2m9"), "7XK4P2M9");
check("normalizeCode strips spaces", normalizeCode(" 7XK4 P2M9 "), "7XK4P2M9");
check("normalizeCode drops confusables", normalizeCode("7XK4-P2M9-0O1I"), "7XK4P2M9");
check("normalizeCode of junk", normalizeCode("!!!"), "");
check("normalizeCode of null", normalizeCode(null), "");
check("normalizeCode caps at 64", normalizeCode("A".repeat(200)).length, 64);

/* ── state-file error handling: never mint a secret to recover ─────── */

section("pairing state: a read error must never regenerate the secret");

{
  const ORIGINAL = process.env.PUFF_PAIRING_STATE;

  // Corrupt JSON. The pre-fix behaviour was "generate a fresh state and write
  // it", which silently revokes every device in the house. Must throw instead,
  // and must leave the file exactly as it found it.
  const corruptPath = path.join(DIR, "corrupt-pairing.json");
  const CORRUPT_BYTES = "{ this is not json";
  fs.writeFileSync(corruptPath, CORRUPT_BYTES);
  process.env.PUFF_PAIRING_STATE = corruptPath;
  check("corrupt state file THROWS (does not regenerate)", threw(() => getPairingState()), true);
  check("corrupt state file left byte-identical", fs.readFileSync(corruptPath, "utf8"), CORRUPT_BYTES);

  // Parses, but fails isValidState (no secret). Same rule.
  const badShapePath = path.join(DIR, "badshape-pairing.json");
  fs.writeFileSync(badShapePath, JSON.stringify({ version: 1, code: CODE }));
  process.env.PUFF_PAIRING_STATE = badShapePath;
  check("state file with no secret THROWS", threw(() => getPairingState()), true);
  check(
    "...and was not overwritten with a fresh secret",
    JSON.parse(fs.readFileSync(badShapePath, "utf8")).secret,
    undefined,
  );

  // Unreadable for a reason that is not "absent". A directory at the state path
  // yields EISDIR on both Windows and Linux, so this needs no chmod.
  const dirPath = path.join(DIR, "isdir-pairing.json");
  fs.mkdirSync(dirPath);
  process.env.PUFF_PAIRING_STATE = dirPath;
  check("unreadable state path (EISDIR) THROWS", threw(() => getPairingState()), true);

  // Genuine first boot: nothing there at all. This is the ONLY case that may
  // create a secret.
  const freshPath = path.join(DIR, "fresh", "pairing.json");
  process.env.PUFF_PAIRING_STATE = freshPath;
  const born = getPairingState();
  check("first boot (ENOENT) mints a valid secret", /^[0-9a-f]{64}$/.test(born.secret), true);
  check("first boot persisted the file", fs.existsSync(freshPath), true);

  // Disaster fallback: the file goes unreadable AFTER a good load. Serve the
  // cached secret rather than throwing the whole household offline.
  fs.rmSync(freshPath);
  fs.mkdirSync(freshPath);
  check(
    "unreadable AFTER a good load serves the cached secret",
    getPairingState().secret,
    born.secret,
  );

  process.env.PUFF_PAIRING_STATE = ORIGINAL;
  check("original state restored for later sections", getPairingState().secret, SECRET);
}

/* ── registry: a read failure must ABORT, never truncate ───────────── */

section("registry: read failure aborts the mutation (wipe regression)");

{
  const THREE = {
    version: 1,
    devices: [
      { id: "AAAAAAAAAAAA", name: "one", createdAt: "2026-01-01T00:00:00.000Z", lastSeenAt: null },
      { id: "BBBBBBBBBBBB", name: "two", createdAt: "2026-01-01T00:00:00.000Z", lastSeenAt: null },
      { id: "CCCCCCCCCCCC", name: "three", createdAt: "2026-01-01T00:00:00.000Z", lastSeenAt: null },
    ],
  };
  const SEEDED = JSON.stringify(THREE, null, 2);
  fs.writeFileSync(DEVICES_FILE, SEEDED);
  check("seeded three devices", listPairedDevices().length, 3);

  // THE REGRESSION: one transient read failure during a pair (or during the
  // v1->v2 upgrade the kiosk triggers on its next page load) used to write a
  // one-row file over all three.
  withReadFailure("devices.json", () => {
    check("addPairedDevice THROWS on a read failure", threw(() => addPairedDevice("intruder")), true);
  });
  check("all three rows survived on disk", JSON.parse(fs.readFileSync(DEVICES_FILE, "utf8")).devices.length, 3);
  check("registry file is byte-identical", fs.readFileSync(DEVICES_FILE, "utf8") === SEEDED, true);

  withReadFailure("devices.json", () => {
    check("renamePairedDevice THROWS (no confident false 'not_found')", threw(() => renamePairedDevice("AAAAAAAAAAAA", "x")), true);
    check("revokePairedDevice THROWS (no confident false 'not_found')", threw(() => revokePairedDevice("AAAAAAAAAAAA")), true);
    check("listPairedDevices THROWS (route answers 500, not 'no devices')", threw(() => listPairedDevices()), true);
    // The authorisation path keeps the opposite policy on purpose: unreadable
    // registry => nobody is a member => fail closed, no throw.
    check("findPairedDevice stays lenient (fail closed, no throw)", findPairedDevice("AAAAAAAAAAAA"), null);
    check("verify stays lenient and fails closed", verifyDeviceCookieValue(issueDeviceCookieValueV2("AAAAAAAAAAAA", NOW), NOW), false);
  });
  check("registry STILL byte-identical after every failed mutation", fs.readFileSync(DEVICES_FILE, "utf8") === SEEDED, true);

  // Same rule via a real filesystem error rather than a patched one.
  const ORIGINAL_DEVICES = process.env.PUFF_PAIRED_DEVICES;
  const devDir = path.join(DIR, "isdir-devices.json");
  fs.mkdirSync(devDir);
  process.env.PUFF_PAIRED_DEVICES = devDir;
  check("addPairedDevice THROWS on EISDIR registry", threw(() => addPairedDevice("intruder")), true);
  check("renewalCookieFor NEVER throws on EISDIR registry", threw(() => renewalCookieFor(carrier(v1), NOW)), false);
  check("renewalCookieFor returns null on EISDIR registry", renewalCookieFor(carrier(v1), NOW), null);

  // ENOENT is the one case that may start empty: the very first device.
  const virginPath = path.join(DIR, "virgin", "devices.json");
  process.env.PUFF_PAIRED_DEVICES = virginPath;
  const firstEver = addPairedDevice("First ever", NOW);
  check("absent registry (ENOENT) starts empty and writes", listPairedDevices().length, 1);
  check("...with the new device in it", listPairedDevices()[0].id, firstEver.id);

  process.env.PUFF_PAIRED_DEVICES = ORIGINAL_DEVICES;
  check("registry restored for later sections", listPairedDevices().length, 3);
}

section("renewalCookieFor never throws");

{
  check("...with a patched read failure and a v1 cookie", withReadFailure("devices.json", () => threw(() => renewalCookieFor(carrier(v1), NOW))), false);
  check("...and returns null rather than upgrading", withReadFailure("devices.json", () => renewalCookieFor(carrier(v1), NOW)), null);

  const agedIssued = NOW - (RENEW_AFTER_SEC + 60) * SEC;
  const agedCookie = issueDeviceCookieValueV2("AAAAAAAAAAAA", agedIssued);
  check("...with an aged v2 cookie and a failing registry", withReadFailure("devices.json", () => threw(() => renewalCookieFor(carrier(agedCookie), NOW))), false);
  check("...returning null (membership could not be proved)", withReadFailure("devices.json", () => renewalCookieFor(carrier(agedCookie), NOW)), null);

  // Sanity: the same aged cookie DOES renew when the registry is readable, so
  // the nulls above are the failure policy and not a broken fixture.
  const renewal = renewalCookieFor(carrier(agedCookie), NOW);
  check("sanity — aged v2 renews with a healthy registry", verifyDeviceCookieIdentity(renewal?.value, NOW)?.deviceId, "AAAAAAAAAAAA");
}

/* ── raw Cookie-header parsing ─────────────────────────────────────── */

section("readPairingCookie: raw header branch");

{
  const live = issueDeviceCookieValueV2("BBBBBBBBBBBB", NOW);

  check(
    "cookie found among several, with spaces",
    isPaired({ headers: new Headers({ cookie: `theme=night; puff_device=${live}; other=1` }) }),
    true,
  );
  check(
    "cookie found as the last entry, no trailing semicolon",
    isPaired({ headers: new Headers({ cookie: `a=1; puff_device=${live}` }) }),
    true,
  );
  check(
    "percent-encoded value is decoded before verifying",
    isPaired({ headers: new Headers({ cookie: `puff_device=${live.replace(/\./g, "%2E")}` }) }),
    true,
  );
  check(
    "malformed percent-escape fails closed WITHOUT throwing",
    isPaired({ headers: new Headers({ cookie: "puff_device=v1.123.%zz" }) }),
    false,
  );
  check(
    "a cookie whose NAME merely contains ours is not mistaken for it",
    isPaired({ headers: new Headers({ cookie: `xpuff_device=${live}` }) }),
    false,
  );
  check(
    "no cookie header at all",
    isPaired({ headers: new Headers({}) }),
    false,
  );
  check(
    "malformed header entry with no '=' is skipped",
    isPaired({ headers: new Headers({ cookie: `novalue; puff_device=${live}` }) }),
    true,
  );
  // The NextRequest jar wins over the raw header when both are present.
  check(
    "cookie jar takes precedence over the raw header",
    pairedDeviceIdFor(
      {
        cookies: { get: (n) => (n === "puff_device" ? { value: live } : undefined) },
        headers: new Headers({ cookie: "puff_device=garbage" }),
      },
      NOW,
    ),
    "BBBBBBBBBBBB",
  );
}

/* ── the secret is never touched ───────────────────────────────────── */

section("secret invariants");

check("secret unchanged after every operation above", getPairingState().secret, SECRET);
check(
  "secret unchanged on disk",
  JSON.parse(fs.readFileSync(STATE_FILE, "utf8")).secret,
  SECRET,
);
check(
  "state file still version 1",
  JSON.parse(fs.readFileSync(STATE_FILE, "utf8")).version,
  1,
);
check(
  "state file still has createdAt (additive-only writes)",
  typeof JSON.parse(fs.readFileSync(STATE_FILE, "utf8")).createdAt,
  "string",
);
check(
  "no cookie material in the registry file",
  /secret|hmac|puff_device/i.test(fs.readFileSync(DEVICES_FILE, "utf8")),
  false,
);

/* ── done ──────────────────────────────────────────────────────────── */

try {
  fs.rmSync(DIR, { recursive: true, force: true });
} catch {
  /* best effort */
}

console.log(`\n${pass} passed, ${fail} failed`);
assert.equal(fail, 0, `${fail} device-cookie assertions failed`);
