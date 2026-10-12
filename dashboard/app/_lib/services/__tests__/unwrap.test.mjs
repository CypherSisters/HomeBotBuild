/**
 * Unit test for setupService's unwrap(). Plain Node, like the other tests here.
 *
 *   npm run test:unwrap   (or: node app/_lib/services/__tests__/unwrap.test.mjs)
 *
 * Requires Node >=22.6 for native TypeScript type stripping. Exits non-zero on
 * any failure.
 *
 * What is being guarded: /setup/surfaces answers with `surfaces: [...]` beside
 * `available` and `reasons`. If unwrap treated that array as the payload, the
 * wizard's goal step would show no surfaces at all and save an empty choice.
 */
import assert from "node:assert/strict";

const { unwrap } = await import("../setupService.ts");

let pass = 0;
let fail = 0;
function check(name, fn) {
  try { fn(); pass++; console.log(`ok   ${name}`); }
  catch (e) { fail++; console.log(`FAIL ${name}\n     ${e.message}`); }
}

check("an array under the key is a field of the envelope, not the payload", () => {
  const reply = { ok: true, surfaces: ["chat", "voice"], available: [{ id: "chat" }], reasons: { chat: "x" } };
  const d = unwrap(reply, "surfaces");
  assert.equal(d, reply);
  assert.deepEqual(d.surfaces, ["chat", "voice"]);
  assert.equal(d.available.length, 1);
});

check("a plain object under the key is the payload", () => {
  const reply = { ok: true, census: { cpu: 8 } };
  assert.deepEqual(unwrap(reply, "census"), { cpu: 8 });
});

check("a missing key falls back to the envelope", () => {
  const reply = { ok: true, cpu: 8 };
  assert.equal(unwrap(reply, "census"), reply);
});

check("null or undefined data gives an empty object", () => {
  assert.deepEqual(unwrap(null, "x"), {});
  assert.deepEqual(unwrap(undefined, "x"), {});
});

check("an empty array under the key still falls back to the envelope", () => {
  const reply = { surfaces: [], available: [] };
  assert.equal(unwrap(reply, "surfaces"), reply);
});

console.log(`\n${pass} passed, ${fail} failed`);
if (fail) process.exit(1);
