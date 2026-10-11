/**
 * The BUILD surface is gated at the shipped default, not only at "all".
 *
 *   npm run test:pairing   (or: node app/_lib/server/__tests__/wizard-gate.test.mjs)
 *
 * WHAT THIS PINS
 * --------------
 * `enforceMode()` defaults to "knowledge". If that stage gated
 * KNOWLEDGE_PREFIXES only, `/api/pai/wizard/run` and `/api/pai/build/prove`
 * would NOT be gated on a default install, and the catch-all proxy
 * (app/api/pai/[...path]/route.ts) would forward them upstream with the
 * appliance's API key attached: an unpaired LAN client could drive the whole
 * wizard (apply cards, run the build, scaffold personas) on the appliance.
 *
 * Same plain-Node pattern as pairing-gate.test.mjs: pathRequiresPairing() is
 * pure path/method logic, nothing is written and no request is made. Node
 * >=22.6 for native TypeScript type stripping. Exits non-zero on any failure.
 */
import assert from "node:assert/strict";

const { pathRequiresPairing, enforceMode } = await import("../pairing.ts");

let pass = 0,
  fail = 0;

function check(mode, path, method, expected, label) {
  process.env.PUFF_PAIRING_ENFORCE = mode;
  const got = pathRequiresPairing(path, method, { sameOrigin: true });
  const ok = got === expected;
  if (ok) pass++;
  else fail++;
  console.log(
    `${ok ? "PASS" : "FAIL"}  [${(mode || "<unset>").padEnd(9)}] ${method.padEnd(6)} ${path}` +
      `  -> gated=${got} (want ${expected})   ${label}`,
  );
}

/** The shipped default: PUFF_PAIRING_ENFORCE absent from the environment. */
function checkDefault(path, method, expected, label) {
  delete process.env.PUFF_PAIRING_ENFORCE;
  const got = pathRequiresPairing(path, method, { sameOrigin: true });
  const ok = got === expected;
  if (ok) pass++;
  else fail++;
  console.log(
    `${ok ? "PASS" : "FAIL"}  [default  ] ${method.padEnd(6)} ${path}` +
      `  -> gated=${got} (want ${expected})   ${label}`,
  );
}

console.log("\n--- the shipped default IS the knowledge stage ---");
delete process.env.PUFF_PAIRING_ENFORCE;
assert.equal(enforceMode(), "knowledge", "unset must mean knowledge");
process.env.PUFF_PAIRING_ENFORCE = "";
assert.equal(enforceMode(), "knowledge", "empty must mean knowledge");
pass += 2;
console.log("PASS  enforceMode() with no/blank env is 'knowledge'");

console.log("\n--- default install: the wizard family is gated, reads included ---");
for (const [path, method] of [
  ["/api/pai/wizard", "GET"],
  ["/api/pai/wizard/cards", "GET"],
  ["/api/pai/wizard/state", "GET"],
  ["/api/pai/wizard/apply", "POST"],
  ["/api/pai/wizard/run", "POST"],
  ["/api/pai/wizard/resume", "POST"],
  ["/api/pai/build/prove", "GET"],
  ["/api/pai/build/prove/static", "POST"],
  ["/api/pai/build/prove/live", "POST"],
  ["/api/pai/build/prove/owner", "POST"],
  ["/api/pai/build/self", "POST"],
]) {
  checkDefault(path, method, true, "build surface");
  check("knowledge", path, method, true, "build surface");
  check("all", path, method, true, "build surface");
}

console.log("\n--- encoded and cased spellings cannot dodge it ---");
checkDefault("/api/pai/wizard%2frun", "POST", true, "encoded separator");
checkDefault("/api/pai/WIZARD/run", "POST", true, "upper case");
checkDefault("/api/pai/build%2Fprove%2Flive", "POST", true, "encoded prove");
checkDefault("/api/pai/wizard/run%00", "POST", true, "NUL fails closed");
checkDefault("/api/pai/wizard/%zz", "POST", true, "malformed escape fails closed");
checkDefault("/api/pai/wizard", "", true, "missing method fails closed");

console.log("\n--- persona: writes gated, list stays open for the kiosk nav ---");
checkDefault("/api/pai/persona/scaffold", "POST", true, "scaffold writes a folder");
checkDefault("/api/pai/persona/preview", "POST", true, "preview is a POST write-shaped route");
checkDefault("/api/pai/persona/stargazer", "DELETE", true, "delete gated");
checkDefault("/api/pai/persona/list", "GET", false, "nav read stays open");
checkDefault("/api/pai/persona/list", "HEAD", false, "read stays open");

console.log("\n--- /api/pai/setup is UNCHANGED (the backend's setup code gates it) ---");
checkDefault("/api/pai/setup/status", "GET", false, "setup read open as before");
checkDefault("/api/pai/setup/identity", "POST", false, "setup write open as before");
checkDefault("/api/pai/setup/vault", "POST", false, "setup write open as before");
check("all", "/api/pai/setup/identity", "POST", true, "at 'all' everything /api/pai is gated");

console.log("\n--- unrelated surfaces are not newly gated ---");
checkDefault("/api/pai/chat", "POST", false, "chat unchanged at this stage");
checkDefault("/api/pai/knowledge/search", "GET", false, "knowledge read unchanged");
checkDefault("/api/pai/knowledge/upload", "POST", true, "knowledge write unchanged");
checkDefault("/api/pair/status", "GET", false, "pairing endpoints always reachable");
checkDefault("/api/pai/wizardry/run", "POST", false, "prefix match is segment-anchored");
checkDefault("/api/pai/buildings", "GET", false, "prefix match is segment-anchored");

console.log("\n--- 'off' still means off (dev switch), and says so on the console ---");
check("off", "/api/pai/wizard/run", "POST", false, "off disables the gate by design");
check("off", "/api/pai/build/prove/live", "POST", false, "off disables the gate by design");
check("bogus", "/api/pai/wizard/run", "POST", true, "an unrecognised value falls back to 'all'");

console.log(`\n${pass} passed, ${fail} failed`);
delete process.env.PUFF_PAIRING_ENFORCE;
if (fail) process.exit(1);
