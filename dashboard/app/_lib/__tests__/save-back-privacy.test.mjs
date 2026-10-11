/**
 * The save-back privacy contract, client side.
 *
 *   npm run test:saveback   (or: node app/_lib/__tests__/save-back-privacy.test.mjs)
 *
 * WHAT THIS GUARDS. The Chat page streams a turn and then posts the finished
 * pair to /chat/save so it shows up in history. For a vault turn that was the
 * leak: the backend deliberately did NOT store the PIN the household typed or
 * the answer read out of the vault, and the save-back put both on disk a
 * second later. The backend now says so on the wire, and the client stops
 * posting.
 *
 * THE WIRE CONTRACT, which the backend half must match exactly (see
 * tests/test_vault_save_back_private.py in the appliance repo):
 *   /chat/stream          terminal event: {"done":true, ..., "private":bool}
 *   /v1/chat/completions  final chunk:    {"puffpai_meta":{...,"private":bool}}
 *                         then the separate "[DONE]" sentinel line
 *   /chat/send            JSON response:  {..., "private":bool}
 * `private` is always present on a current backend; ABSENT means the backend
 * predates the field, and the client keeps today's behaviour (it saves).
 *
 * Two kinds of check below, labelled as such:
 *   1. real unit tests of parseStreamEvent, which is pure and importable;
 *   2. source assertions on chatService.ts / chat/page.tsx. This repo has no
 *      test runner and neither module is importable under Node's type
 *      stripping (chatService imports '../api' without a file extension), so
 *      the decision logic is pinned by reading it, the same way scripts/check-*
 *      pin other invariants here. They are regression tripwires, not proof the
 *      page runs.
 *
 * Requires Node >=22.6 for native TypeScript type stripping. Exits non-zero on
 * any failure.
 */
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const { parseStreamEvent } = await import("../chatStream.ts");

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = join(HERE, "..", "..");

let pass = 0;
let fail = 0;

function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    fail += 1;
    console.error(`FAIL  ${name}\n      ${err.message}`);
  }
}

/* ── 1. parseStreamEvent: both dialects carry `private` ─────────────────── */

check("/chat/stream terminal event carries private:true", () => {
  const ev = parseStreamEvent(
    '{"conversation_id":"c","delta":"","done":true,"full_response":"x","private":true}'
  );
  assert.equal(ev.private, true);
  assert.equal(ev.done, true);
});

check("/chat/stream terminal event with private:false is not private", () => {
  const ev = parseStreamEvent(
    '{"conversation_id":"c","delta":"","done":true,"full_response":"x","private":false}'
  );
  assert.equal(ev.private, undefined);
  assert.equal(ev.done, true);
});

check("OpenAI final chunk carries private inside puffpai_meta", () => {
  const ev = parseStreamEvent(
    '{"choices":[{"delta":{}}],"puffpai_meta":{"sources":[],"suggestions":[],"private":true}}'
  );
  assert.equal(ev.private, true);
  assert.deepEqual(ev.meta.sources, []);
});

check("OpenAI final chunk with private:false is not private", () => {
  const ev = parseStreamEvent(
    '{"choices":[{"delta":{}}],"puffpai_meta":{"sources":[],"suggestions":[],"private":false}}'
  );
  assert.equal(ev.private, undefined);
});

check("a backend too old to say never claims a turn is private", () => {
  // Absent is NOT private: it means "this box predates the field". The client
  // keeps saving, exactly as before, and the leak is closed by upgrading the
  // backend. Claiming private here would silently stop history on every old
  // box instead.
  const ev = parseStreamEvent(
    '{"conversation_id":"c","delta":"","done":true,"full_response":"x"}'
  );
  assert.equal(ev.private, undefined);
});

check("a junk `private` value is never read as true", () => {
  for (const raw of [
    '{"done":true,"private":"yes"}',
    '{"done":true,"private":1}',
    '{"done":true,"private":null}',
    '{"choices":[{"delta":{}}],"puffpai_meta":{"sources":[],"private":"true"}}',
  ]) {
    const ev = parseStreamEvent(raw);
    assert.notEqual(ev?.private, true, raw);
  }
});

check("private alone still produces an event", () => {
  // Defensive: a backend that sends the flag on a bare line must not have it
  // dropped as "nothing happened".
  assert.equal(parseStreamEvent('{"private":true}').private, true);
});

/* ── 2. source assertions: the decision logic ───────────────────────────── */

const service = readFileSync(join(APP, "_lib/services/chatService.ts"), "utf8");
const chatPage = readFileSync(join(APP, "dashboard/chat/page.tsx"), "utf8");

check("streamMessage tracks private and the terminal event stickily", () => {
  assert.match(service, /if \(ev\.private\) isPrivate = true;/);
  assert.match(service, /if \(ev\.done\) sawTerminal = true;/);
  assert.match(service, /private: isPrivate,/);
  assert.match(service, /complete: sawTerminal,/);
});

check("sendMessage surfaces the /chat/send private flag", () => {
  assert.match(service, /private: raw\.private === true,/);
});

check("the Chat page saves back only a complete, non-private turn", () => {
  assert.match(chatPage, /if \(!turnIsPrivate && turnComplete\) \{\s*\n\s*saveChat\(/);
  assert.match(chatPage, /turnIsPrivate = meta\?\.private === true;/);
  assert.match(chatPage, /turnComplete = meta\?\.complete === true;/);
});

check("the Chat page has exactly one saveChat call", () => {
  const hits = chatPage.match(/saveChat\(/g) ?? [];
  assert.equal(hits.length, 1, `saveChat called ${hits.length} times`);
});

check("a private turn is dropped from the shared live-turn store", () => {
  // The store is in-memory, but the transcript tile offers what it is showing
  // as a chat prefill and writes THAT to localStorage.
  assert.match(chatPage, /liveTurnCleared\(\{ only: "typed" \}\)/);
  const talk = readFileSync(join(APP, "dashboard/talk/page.tsx"), "utf8");
  assert.match(talk, /meta\?\.private.*liveTurnCleared\(\{ only: "talk" \}\)/s);
});

check("nothing else in the dashboard posts to /chat/save", () => {
  const offenders = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) {
        if (name === "node_modules" || name === "__tests__") continue;
        walk(full);
        continue;
      }
      if (!/\.(ts|tsx|mjs|js)$/.test(name)) continue;
      const src = readFileSync(full, "utf8");
      // The definition lives in chatService; the one caller is the Chat page.
      if (/\/chat\/save/.test(src) && !full.endsWith(join("services", "chatService.ts"))) {
        offenders.push(full);
      }
      if (/\bsaveChat\(/.test(src) &&
          !full.endsWith(join("dashboard", "chat", "page.tsx")) &&
          !full.endsWith(join("services", "chatService.ts"))) {
        offenders.push(full);
      }
    }
  };
  walk(APP);
  assert.deepEqual(offenders, [],
    `a new save-back path needs the same private-turn guard: ${offenders}`);
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
