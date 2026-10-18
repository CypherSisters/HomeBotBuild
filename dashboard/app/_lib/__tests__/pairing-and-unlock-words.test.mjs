/**
 * Unit test for the words a screen is shown when it is not paired, and when
 * the Vault does not open. Plain Node, like the other tests here.
 *
 *   node app/_lib/__tests__/pairing-and-unlock-words.test.mjs
 *
 * Requires Node >=22.6 for native TypeScript type stripping. Exits non-zero on
 * any failure.
 *
 * What is being guarded: only a real pairing refusal (the gate's 401
 * pairing_required, a backend 403 not_paired) is read as "pair this screen";
 * the how-to names `puffpai pair`; and a refused unlock never shows the
 * backend's raw message (the "Send {pin, initialize}" text reached an owner).
 */
import assert from "node:assert/strict";

const pw = await import("../pairingWords.ts");
const vs = await import("../vaultSession.ts");

let pass = 0;
let fail = 0;
function check(name, fn) {
  try { fn(); pass++; console.log(`ok   ${name}`); }
  catch (e) { fail++; console.log(`FAIL ${name}\n     ${e.message}`); }
}

check("the gate's 401 and a backend 403 not_paired are pairing replies", () => {
  assert.equal(pw.isPairingReply(401, { error: "pairing_required" }), true);
  assert.equal(pw.isPairingReply(403, { ok: false, code: "not_paired", message: "x" }), true);
});

check("other 401s and 403s are not", () => {
  assert.equal(pw.isPairingReply(401, { success: false, message: "Invalid PIN" }), false);
  assert.equal(pw.isPairingReply(403, { error: "setup_code_required" }), false);
  assert.equal(pw.isPairingReply(500, { error: "pairing_required" }), false);
  assert.equal(pw.isPairingReply(401, null), false);
  assert.equal(pw.isPairingReply(401, "pairing_required"), false);
});

check("the how-to names puffpai pair and the paired-screen way", () => {
  assert.match(pw.PAIR_PAGE_WORDS, /^Pair this screen to use this page\./);
  assert.match(pw.PAIR_HOW_HERE, /puffpai pair/);
  assert.match(pw.PAIR_HOW_PAIRED, /Settings, then Paired devices/);
  assert.doesNotMatch(pw.PAIR_PAGE_WORDS, /kiosk/i);
});

const RAW = 'Vault not initialized. Send {"pin": "...", "initialize": true} to set up.';

check("not set up: plain words, never the raw message", () => {
  const said = vs.unlockRefusalWords(400, { success: false, message: RAW, needs_initialization: true });
  assert.equal(said, vs.VAULT_NOT_SET_UP_LINE);
  assert.doesNotMatch(said, /[{}]|initialize|Send/);
});

check("a wrong PIN says so plainly", () => {
  assert.equal(vs.unlockRefusalWords(401, { success: false, message: "Invalid PIN" }), vs.VAULT_WRONG_PIN_LINE);
});

check("the try limit passes on only the wait", () => {
  const said = vs.unlockRefusalWords(401, {
    success: false,
    message: "Too many failed attempts. Try again in 42 seconds.",
  });
  assert.equal(said, "Too many tries. Wait 42 seconds, then try again.");
});

check("a raw message of any other kind is never shown", () => {
  for (const [status, body] of [
    [400, { message: RAW }],
    [401, { message: RAW }],
    [503, { message: "Vault module unavailable" }],
    [500, { message: "Traceback (most recent call last)" }],
  ]) {
    const said = vs.unlockRefusalWords(status, body);
    assert.notEqual(said, body.message);
    assert.doesNotMatch(said, /[{}]|module|Traceback/);
  }
});

check("the words follow the copy rules", () => {
  const all = [
    pw.PAIR_PAGE_WORDS, pw.PAIR_TILE_LINE, vs.VAULT_NOT_SET_UP_LINE,
    vs.VAULT_WRONG_PIN_LINE, vs.VAULT_NO_ANSWER_LINE,
    vs.unlockRefusalWords(401, { error: "pairing_required" }),
    vs.unlockRefusalWords(400, {}),
    vs.unlockRefusalWords(401, { message: "totp code required for MFA" }),
  ];
  for (const s of all) {
    assert.doesNotMatch(s, /\u2014|\u2013| - |\bdoors?\b/i, s);
  }
});

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
