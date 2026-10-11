/**
 * Approve card: inline vault PIN when the server says needs_pin.
 * Source-level checks of the handling rules:
 * masked, numeric-friendly, never stored or logged client-side, cleared
 * before the request goes out, sent only as the route's `pin` field, and the
 * server's message shown. Run: node app/_components/Consent/__tests__/pin-entry.test.mjs
 *
 * puff puff pass
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const card = readFileSync(join(here, "..", "ConsentRequests.tsx"), "utf8");
const service = readFileSync(
  join(here, "..", "..", "..", "_lib", "services", "consentService.ts"), "utf8");

const start = card.indexOf("export function PinEntry(");
const end = card.indexOf("export function WaitingPinList(");
const pinEntry = card.slice(start, end);

let pass = 0;
let fail = 0;
function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    fail += 1;
    console.error(`  FAIL  ${name}\n${err.message}`);
  }
}

check("PinEntry exists", () => assert.ok(start > 0 && end > start));
check("the field is masked, numeric-friendly, no autofill", () => {
  assert.match(pinEntry, /type="password"/);
  assert.match(pinEntry, /inputMode="numeric"/);
  assert.match(pinEntry, /autoComplete="off"/);
});
check("the PIN is cleared before the request is sent", () => {
  const cleared = pinEntry.indexOf('setPin("")');
  const sent = pinEntry.indexOf("resolveConsentRequest(");
  assert.ok(cleared > 0 && sent > cleared);
});
check("never logged or stored client-side", () => {
  for (const bad of ["console.", "localStorage", "sessionStorage", "document.cookie",
                     "indexedDB"]) {
    assert.ok(!pinEntry.includes(bad), bad);
  }
});
check("the server's own message is shown and needs_pin decides when to stop", () => {
  assert.match(pinEntry, /setMessage\(text\)/);
  assert.match(pinEntry, /if \(!res\.needs_pin\)/);
});
check("a needs_pin answer on Approve opens the PIN field on that card", () => {
  assert.match(card, /if \(approve && res\.needs_pin\) \{\s*setPinPrompt\(message\);/);
  assert.match(card, /<PinEntry\s+entityId=\{request\.entity_id\}/);
});
check("the firewall page's Allow opens the same PIN field on needs_pin", () => {
  const fw = readFileSync(
    join(here, "..", "..", "..", "dashboard", "firewall", "page.tsx"), "utf8");
  assert.match(fw, /import \{ PinEntry \} from "\.\.\/\.\.\/_components\/Consent\/ConsentRequests"/);
  assert.match(fw, /resolveConsentRequest\(entity_id, approve\)/);
  assert.match(fw, /if \(approve && res\.needs_pin\) \{\s*(\/\/[^\n]*\s*)*setPinPrompts\(/);
  assert.match(fw, /<PinEntry\s+entityId=\{id\}/);
  assert.match(fw, /tone="theme"/);
  assert.match(pinEntry, /tone = "card"/, "the consent card keeps its look by default");
});
check("the service sends pin only when given, on the existing route", () => {
  assert.match(service, /if \(pin\) body\.pin = pin;/);
  assert.match(service, /`\/sentinel\/consent\/requests\/\$\{encodeURIComponent\(entityId\)\}`/);
});
check("waiting approvals show after a reload too", () => {
  assert.match(card, /<WaitingPinList \/>/);
  assert.match(service, /waiting_pin/);
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
