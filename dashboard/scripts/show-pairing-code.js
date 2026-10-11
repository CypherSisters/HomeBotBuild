#!/usr/bin/env node
/**
 * Print the household pairing code to the operator's terminal.
 *
 * Bootstrap problem this solves: GET /api/pair/code only answers
 * already-paired devices, so the FIRST device (usually the kiosk) has no way
 * to learn the code over HTTP. The operator runs this on the appliance:
 *
 *   node scripts/show-pairing-code.js
 *
 * Deliberately a manual, operator-run command rather than something printed to
 * the service log on startup — the code is a credential and does not belong in
 * a log file that gets tailed, rotated, or pasted into a chat.
 *
 * Only prints the CODE. The HMAC secret is never displayed.
 *
 * CODES NOW EXPIRE (one hour after they are rotated). Like GET /api/pair/code,
 * this command lazily rotates an expired code before printing, so what lands in
 * the operator's terminal is always redeemable — being handed a dead code by
 * the one tool that exists to hand you a live one would be worse than useless.
 * The rewrite is ADDITIVE and preserves every other field verbatim: the file
 * stays version 1 and the secret is never touched, so an already-paired device
 * (and a rollback to an older build) is completely unaffected.
 *
 * The dashboard re-reads pairing.json on every check rather than caching it, so
 * a rotation performed here is picked up by the running server immediately —
 * no restart, and no window where the printed code is rejected.
 *
 * Env: PUFF_PAIRING_STATE overrides the default <cwd>/data/pairing.json.
 */
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

/** Keep in sync with PAIR_CODE_TTL_SEC in app/_lib/server/pairing.ts. */
const CODE_TTL_MS = 60 * 60 * 1000;
/** Keep in sync with CODE_ALPHABET in app/_lib/server/pairing.ts. */
const CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ";
const CODE_LENGTH = 8;

const file =
  process.env.PUFF_PAIRING_STATE ||
  path.join(process.cwd(), "data", "pairing.json");

let state;
try {
  state = JSON.parse(fs.readFileSync(file, "utf8"));
} catch (err) {
  console.error(`Could not read pairing state at ${file}`);
  console.error(
    "The file is created the first time somebody tries to pair. Start the " +
      "dashboard and load any page, then run this again.",
  );
  console.error(String(err && err.message ? err.message : err));
  process.exit(1);
}

if (!state || typeof state.code !== "string" || state.code.length !== 8) {
  console.error(`Pairing state at ${file} is malformed.`);
  process.exit(1);
}

/** Rejection-free because 256 % 32 === 0 — every byte maps uniformly. */
function randomCode() {
  const bytes = crypto.randomBytes(CODE_LENGTH);
  let out = "";
  for (let i = 0; i < CODE_LENGTH; i++) {
    out += CODE_ALPHABET[bytes[i] % CODE_ALPHABET.length];
  }
  return out;
}

/**
 * Atomic 0600 temp+rename, matching writeState() in pairing.ts. The secret sits
 * in this file; a truncated or world-readable intermediate is not acceptable
 * even for the moment a rename would take.
 */
function writeState(next) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(next, null, 2), { mode: 0o600 });
  try {
    fs.chmodSync(tmp, 0o600);
  } catch {
    /* Windows: chmod is a no-op / may throw. Not fatal. */
  }
  fs.renameSync(tmp, file);
}

const rotatedAtMs = Date.parse(state.codeRotatedAt || "");
// Unparseable/absent rotation time counts as expired — fail closed, and it
// self-heals right here.
const expired =
  !Number.isFinite(rotatedAtMs) || Date.now() >= rotatedAtMs + CODE_TTL_MS;

let rotatedNow = false;
if (expired) {
  try {
    // Spread first: every existing field survives, including the secret. This
    // rotates the CODE only — already-paired devices are untouched.
    const next = Object.assign({}, state, {
      code: randomCode(),
      codeRotatedAt: new Date().toISOString(),
    });
    writeState(next);
    state = next;
    rotatedNow = true;
  } catch (err) {
    console.error(
      "The pairing code has expired and could not be rotated (state file not " +
        "writable). Not printing a code that will be rejected.",
    );
    console.error(String(err && err.message ? err.message : err));
    process.exit(1);
  }
}

const pretty = `${state.code.slice(0, 4)}-${state.code.slice(4)}`;
const expiresAt = new Date(Date.parse(state.codeRotatedAt) + CODE_TTL_MS);
console.log("");
console.log("  PuffPAI household pairing code");
console.log("");
console.log(`      ${pretty}`);
console.log("");
if (rotatedNow) {
  console.log("  (the previous code had expired — this is a fresh one)");
}
console.log(`  rotated: ${state.codeRotatedAt || state.createdAt || "unknown"}`);
console.log(`  expires: ${expiresAt.toISOString()}  (one hour)`);
console.log("  Type this on a household device soon; the device then stays");
console.log("  paired for a year.");
console.log("");
