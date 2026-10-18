/**
 * Unit test for vaultFetch: the Vault session on engine changes after setup.
 * Plain Node, like the other tests here.
 *
 *   node app/_lib/__tests__/vault-session.test.mjs
 *
 * Requires Node >=22.6 for native TypeScript type stripping. Exits non-zero on
 * any failure.
 *
 * What is being guarded: a change the backend refuses with 409
 * `setup_complete` asks for the PIN once, then goes again with the new
 * session; anything else is returned untouched, and stopping at the PIN
 * prompt returns the refusal as it came.
 */
import assert from "node:assert/strict";

const vs = await import("../vaultSession.ts");

let pass = 0;
let fail = 0;
async function check(name, fn) {
  try { await fn(); pass++; console.log(`ok   ${name}`); }
  catch (e) { fail++; console.log(`FAIL ${name}\n     ${e.message}`); }
}

const LOCKED = () => new Response(JSON.stringify({ ok: false, error: "setup_complete" }), { status: 409 });
const OK = () => new Response(JSON.stringify({ ok: true }), { status: 200 });

function stubFetch(replies) {
  const seen = [];
  globalThis.fetch = async (url, init) => {
    seen.push({ url, token: new Headers(init?.headers).get("X-Vault-Token") });
    return replies.shift()();
  };
  return seen;
}

await check("a locked change asks once and goes again with the new session", async () => {
  vs.setVaultSession("");
  const seen = stubFetch([LOCKED, OK]);
  let asked = 0;
  const off = vs.registerVaultAsker(async () => { asked++; return "tok-1"; });
  const res = await vs.vaultFetch("/api/pai/add/x/approve", { method: "POST", body: "{}" });
  off();
  assert.equal(res.status, 200);
  assert.equal(asked, 1);
  assert.equal(seen.length, 2);
  assert.equal(seen[0].token, null);
  assert.equal(seen[1].token, "tok-1");
});

await check("the session is sent on the next change without asking", async () => {
  const seen = stubFetch([OK]);
  const res = await vs.vaultFetch("/api/pai/keys/x", { method: "POST" });
  assert.equal(res.status, 200);
  assert.equal(seen[0].token, "tok-1");
});

await check("stopping at the prompt returns the refusal and keeps no session", async () => {
  stubFetch([LOCKED]);
  const off = vs.registerVaultAsker(async () => "");
  const res = await vs.vaultFetch("/api/pai/keys/x", { method: "POST" });
  off();
  assert.equal(res.status, 409);
  assert.deepEqual(await res.json(), { ok: false, error: "setup_complete" });
  assert.equal(vs.hasVaultSession(), false);
});

await check("another 409 is not a locked Vault and asks nothing", async () => {
  stubFetch([() => new Response(JSON.stringify({ ok: false, error: "busy" }), { status: 409 })]);
  let asked = 0;
  const off = vs.registerVaultAsker(async () => { asked++; return "t"; });
  const res = await vs.vaultFetch("/api/pai/build/yes", { method: "POST" });
  off();
  assert.equal(res.status, 409);
  assert.equal(asked, 0);
});

await check("with no prompt mounted the refusal comes back as it is", async () => {
  stubFetch([LOCKED]);
  const res = await vs.vaultFetch("/api/pai/setup/ha", { method: "POST" });
  assert.equal(res.status, 409);
});

await check("two changes at once share one PIN prompt", async () => {
  vs.setVaultSession("");
  stubFetch([LOCKED, LOCKED, OK, OK]);
  let asked = 0;
  const off = vs.registerVaultAsker(() => { asked++; return new Promise((r) => setTimeout(() => r("tok-2"), 10)); });
  const [a, b] = await Promise.all([
    vs.vaultFetch("/api/pai/keys/a", { method: "POST" }),
    vs.vaultFetch("/api/pai/keys/b", { method: "POST" }),
  ]);
  off();
  assert.equal(asked, 1);
  assert.equal(a.status, 200);
  assert.equal(b.status, 200);
});

console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
