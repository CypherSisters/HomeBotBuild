/**
 * Unit test for the Workflows page's engine-status classifier. Plain Node -
 * this repo has no component test runner, and classifyEngineStatus is pure.
 *
 *   npm run test:enginestatus
 *   (or: node app/_lib/services/__tests__/engine-status.test.mjs)
 *
 * Requires Node >=22.6 for native TypeScript type stripping, like the other
 * tests here. Exits non-zero on any failure.
 *
 * What is being guarded: the page printed "Offline" for every failure, so a
 * restarting dashboard and an unpaired browser both read as "Node-RED is
 * down" while Node-RED was up and running flows on schedule. Only a 200 that
 * actually says connected:false may blame the engine.
 */
import assert from "node:assert/strict";

const { classifyEngineStatus } = await import("../engineStatus.ts");

let pass = 0;
let fail = 0;

function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    fail += 1;
    console.error(`  FAIL  ${name}`);
    console.error(`        ${err && err.message}`);
  }
}

const ok = (data) => ({ status: 200, ok: true, data });

check("200 + connected:true -> connected, body kept", () => {
  const r = classifyEngineStatus(
    ok({
      success: true,
      connected: true,
      base_url: "http://localhost:1880",
      webhooks: { network_scan: "/webhook/network_scan" },
    })
  );
  assert.equal(r.state, "connected");
  assert.equal(r.status.base_url, "http://localhost:1880");
  assert.equal(r.status.webhooks.network_scan, "/webhook/network_scan");
});

check("200 + connected:false -> engine_offline, with the backend reason", () => {
  const r = classifyEngineStatus(
    ok({ success: false, connected: false, error: "connection refused" })
  );
  assert.equal(r.state, "engine_offline");
  assert.equal(r.detail, "connection refused");
});

check("401 pairing_required -> pairing_required, never engine_offline", () => {
  const r = classifyEngineStatus({
    status: 401,
    ok: false,
    data: { error: "pairing_required" },
  });
  assert.equal(r.state, "pairing_required");
});

check("401 without a pairing reason -> unreachable", () => {
  const r = classifyEngineStatus({
    status: 401,
    ok: false,
    data: { error: "unauthorized" },
  });
  assert.equal(r.state, "unreachable");
});

check("null (network error / timeout / service restarting) -> unreachable", () => {
  assert.equal(classifyEngineStatus(null).state, "unreachable");
});

check("5xx -> unreachable, not engine_offline", () => {
  const r = classifyEngineStatus({ status: 502, ok: false, data: null });
  assert.equal(r.state, "unreachable");
  assert.equal(r.detail, "HTTP 502");
});

check("200 with a non-JSON / wrong-shaped body -> unreachable", () => {
  assert.equal(classifyEngineStatus(ok(null)).state, "unreachable");
  assert.equal(classifyEngineStatus(ok("<html>login</html>")).state, "unreachable");
  assert.equal(classifyEngineStatus(ok({ success: true })).state, "unreachable");
});

check("no failure mode other than engine_offline blames Node-RED", () => {
  const blamed = [
    null,
    { status: 401, ok: false, data: { error: "pairing_required" } },
    { status: 502, ok: false, data: null },
    ok(null),
  ].filter((raw) => classifyEngineStatus(raw).state === "engine_offline");
  assert.deepEqual(blamed, []);
});

console.log(`\nengine-status: ${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
