/**
 * Unit test for the chat SSE event parser. Plain Node — this repo has no test
 * runner, and parseStreamEvent is pure.
 *
 *   npm run test:streamparse   (or: node app/_lib/__tests__/stream-parse.test.mjs)
 *
 * Requires Node >=22.6 for native TypeScript type stripping, like the other
 * tests here. Exits non-zero on any failure.
 *
 * What is being guarded: two live backends speak two different SSE dialects —
 * the appliance's /chat/stream ({delta, thinking, done}) and the
 * OpenAI-compatible /v1/chat/completions ({choices[0].delta.content}) — and an
 * older box never sends `thinking` at all. Getting this wrong is how the live
 * transcript goes blank, so the assertions are mostly about tolerance: an
 * unknown shape must be ignored, never thrown on, and thinking must never be
 * mistaken for reply text (it is display-only and must not reach TTS).
 */
import assert from "node:assert/strict";

const { parseStreamEvent, mayResend } = await import("../chatStream.ts");

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

check("/chat/stream content event", () => {
  const ev = parseStreamEvent(
    ' {"conversation_id":"abc","delta":"Hello","done":false}'
  );
  assert.equal(ev.content, "Hello");
  assert.equal(ev.thinking, undefined);
  assert.equal(ev.done, undefined);
  assert.equal(ev.conversationId, "abc");
});

check("/chat/stream thinking event carries no reply text", () => {
  const ev = parseStreamEvent(
    '{"conversation_id":"abc","thinking":"weighing it up","delta":"","done":false}'
  );
  assert.equal(ev.thinking, "weighing it up");
  assert.equal(ev.content, undefined);
});

check("/chat/stream final event", () => {
  const ev = parseStreamEvent(
    '{"conversation_id":"abc","delta":"","done":true,"full_response":"Hello there","timestamp":"2026-09-19T00:00:00Z","pii":null}'
  );
  assert.equal(ev.done, true);
  assert.equal(ev.content, undefined);
  assert.equal(ev.conversationId, "abc");
});

check("OpenAI delta content", () => {
  const ev = parseStreamEvent(
    '{"choices":[{"delta":{"content":" world"},"index":0}]}'
  );
  assert.equal(ev.content, " world");
});

check("OpenAI reasoning_content is thinking, not reply", () => {
  const ev = parseStreamEvent(
    '{"choices":[{"delta":{"reasoning_content":"hmm"}}]}'
  );
  assert.equal(ev.thinking, "hmm");
  assert.equal(ev.content, undefined);
});

check("OpenAI final chunk carries sources and suggestions", () => {
  const ev = parseStreamEvent(
    '{"choices":[{"delta":{}}],"puffpai_meta":{"sources":[{"library":"wikipedia"}],"suggestions":["More"]}}'
  );
  assert.equal(ev.meta.sources.length, 1);
  assert.equal(ev.meta.sources[0].library, "wikipedia");
  assert.deepEqual(ev.meta.suggestions, ["More"]);
});

check("[DONE] sentinel ends the stream", () => {
  assert.deepEqual(parseStreamEvent("[DONE]"), { done: true });
  assert.deepEqual(parseStreamEvent(" [DONE] "), { done: true });
});

check("legacy and junk lines are ignored, never thrown on", () => {
  // Keepalives, blank lines, half-arrived JSON, and a shape from some future
  // backend all mean the same thing: nothing happened on this line.
  assert.equal(parseStreamEvent(""), null);
  assert.equal(parseStreamEvent("   "), null);
  assert.equal(parseStreamEvent("{"), null);
  assert.equal(parseStreamEvent("ping"), null);
  assert.equal(parseStreamEvent("null"), null);
  assert.equal(parseStreamEvent("[1,2,3]"), null);
  assert.equal(parseStreamEvent('{"unknown_field":"x"}'), null);
  // Empty deltas are not content: a thinking event sends delta:"" beside it.
  assert.equal(parseStreamEvent('{"delta":"","done":false}'), null);
  // Non-string delta from a malformed sender must not become "[object Object]".
  assert.equal(parseStreamEvent('{"delta":{"a":1}}'), null);
});

check("an accepted turn is never re-sent", () => {
  // The expensive mistake this guards: a 120 s timeout on a tool-calling turn
  // falling back to /chat/send would run every agent action twice.
  assert.equal(mayResend({ accepted: true }), false);
  // Not accepted: nothing is running upstream, so the fallback is safe.
  assert.equal(mayResend({ accepted: false }), true);
  // Unknown is treated as "never got that far" — the only direction that can
  // duplicate work is claiming acceptance, and nothing here claims it.
  assert.equal(mayResend({}), true);
  assert.equal(mayResend(null), true);
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
