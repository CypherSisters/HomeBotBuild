/**
 * One brain, one conversation: the dashboard half.
 *
 *   npm run test:household   (or: node app/_lib/__tests__/household-thread.test.mjs)
 *
 * 1. Real unit tests of app/_lib/householdThread.ts (pure, importable).
 * 2. Source tripwires on the Chat page, the chat service, the Talk page and
 *    the transcript tile, the same way save-back-privacy.test.mjs pins them:
 *    those modules are not importable under Node's type stripping.
 *
 * Requires Node >=22.6 for native TypeScript type stripping.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ht = await import("../householdThread.ts");
const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, "..", "..");
const read = (p) => readFileSync(join(app, p), "utf8");

let failed = 0;
const test = (name, fn) => {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed++;
    console.error(`FAIL ${name}\n     ${e.message}`);
  }
};

test("the household thread id", () => {
  assert.equal(ht.HOUSEHOLD_THREAD_ID, "home-owner");
  assert.ok(ht.isHouseholdThread("home-owner"));
  assert.ok(!ht.isHouseholdThread("voice-20261008"));
});

test("test threads are hidden from Recent, real ones are listed", () => {
  for (const id of ["home-owner", "voice-20261008", "voice-guest-20261008",
                    "0123456789abcdef0123456789abcdef",
                    "8f14e45f-ceea-467a-9575-0e1d8e0f7a3b"]) {
    assert.ok(ht.isListedThread(id), id);
  }
  for (const id of ["pin-smoke", "check-voice-2", "media-smoke", "probe-smoke", "", "client-ab12"]) {
    assert.ok(!ht.isListedThread(id), id);
  }
});

test("groupThreads: household first, the rest under Earlier, tests dropped", () => {
  const convs = [
    { id: "0123456789abcdef0123456789abcdef" },
    { id: "pin-smoke" },
    { id: "home-owner" },
    { id: "voice-20261008" },
  ];
  const { household, earlier } = ht.groupThreads(convs);
  assert.equal(household.id, "home-owner");
  assert.deepEqual(earlier.map((c) => c.id),
    ["0123456789abcdef0123456789abcdef", "voice-20261008"]);
  assert.equal(ht.groupThreads([{ id: "voice-20261008" }]).household, null);
});

test("old voice threads read as a day", () => {
  assert.equal(ht.voiceDayLabel("voice-20261008"), "Voice, Oct 8");
  assert.equal(ht.voiceDayLabel("voice-owner-20260131"), "Voice, Jan 31");
  assert.equal(ht.voiceDayLabel("0123456789abcdef0123456789abcdef"), null);
  assert.equal(ht.voiceDayLabel("voice-20261399"), null);
});

test("Continue in chat opens the household thread", () => {
  assert.equal(ht.chatThreadHref(), "/dashboard/chat?c=home-owner");
});

test("the poll appends spoken turns and skips this page's own echo", () => {
  const local = [
    { id: "a", role: "user", content: "typed here" },
    { id: "b", role: "assistant", content: "reply with insulin" },
  ];
  const fetched = [
    { role: "user", content: "typed here", timestamp: "2026-10-09T10:00:00+00:00" },
    { role: "assistant", content: "reply with [REDACTED-HEALTH]", timestamp: "2026-10-09T10:00:00+00:00" },
    { role: "user", content: "said at the box", timestamp: "2026-10-09T10:01:00+00:00", surface: "voice" },
    { role: "assistant", content: "spoken reply", timestamp: "2026-10-09T10:01:00+00:00", surface: "voice" },
  ];
  const fresh = ht.newServerMessages(fetched, local);
  assert.deepEqual(fresh.map((m) => m.content), ["said at the box", "spoken reply"]);
  assert.equal(ht.latestTimestamp(fetched), "2026-10-09T10:01:00+00:00");
  assert.deepEqual(ht.newServerMessages([], local), []);
  const ids = ht.withIds(fresh).map((m) => m.id);
  assert.equal(new Set(ids).size, 2);
});

// ── source tripwires ──────────────────────────────────────────────────────
const chat = read("dashboard/chat/page.tsx");
const svc = read("_lib/services/chatService.ts");
const talk = read("dashboard/talk/page.tsx");
const tile = read("_components/Widgets/TranscriptTile.tsx");

test("Chat page opens the household thread and reads ?c=", () => {
  assert.match(chat, /let target = HOUSEHOLD_THREAD_ID;/);
  assert.match(chat, /new URLSearchParams\(window\.location\.search\)\.get\("c"\)/);
  assert.match(chat, /getThread\(target\)/);
});

test("Chat page refreshes the open thread on a timer, visible only", () => {
  assert.match(chat, /setInterval\(tick, 4000\)/);
  assert.match(chat, /document\.visibilityState !== "visible"/);
  assert.match(chat, /getThread\(id, lastSeenTsRef\.current/);
});

test("New chat is still a side topic", () => {
  assert.match(chat, /const handleNewChat = \(\) => \{[\s\S]*?activeIdRef\.current = null;/);
  assert.match(chat, /activeIdRef\.current \?\? newConversationId\(\)/);
});

test("Recent lists the household thread, then Earlier", () => {
  assert.match(chat, /title="Home conversation"/);
  assert.match(chat, />\s*Earlier\s*</);
  assert.match(chat, /groupThreads\(conversations\)/);
});

test("WELCOME says nothing leaves without consent", () => {
  assert.ok(!chat.includes("your data never leaves"));
  assert.match(chat, /nothing leaves your computer without your consent/);
});

test("the OpenAI stream names its conversation and saves client side", () => {
  assert.match(svc, /conversation_id: conversationId \}/);
  assert.match(svc, /client_saves: true/);
});

test("Talk page starts in the household thread, tagged talk", () => {
  assert.match(talk, /useRef<string \| null>\(HOUSEHOLD_THREAD_ID\)/);
  assert.match(talk, /surface: "talk"/);
});

test("the transcript tile continues in the household thread", () => {
  assert.match(tile, /Continue in chat/);
  assert.match(tile, /router\.push\(chatThreadHref\(HOUSEHOLD_THREAD_ID\)\)/);
  assert.match(tile, /c\.id === HOUSEHOLD_THREAD_ID/);
});

const phone = read("send/PhoneChat.tsx");
const sendPage = read("send/page.tsx");

test("Send to Puff types into the household thread as the phone", () => {
  assert.match(phone, /fetch\("\/api\/pai\/chat\/send"/);
  assert.match(phone, /conversation_id: HOUSEHOLD_THREAD_ID/);
  assert.match(phone, /surface: "phone"/);
  // It reads the same thread back, newer-than-last-seen only.
  assert.match(phone, /\/api\/pai\/chat\/history\?/);
  assert.match(phone, /newServerMessages\(/);
  assert.match(phone, /chatThreadHref\(HOUSEHOLD_THREAD_ID\)/);
  assert.match(phone, /Open full chat/);
});

test("Send to Puff voice memo uses the phone's own recorder", () => {
  assert.match(phone, /accept="audio\/\*"/);
  assert.match(phone, /\bcapture\b/);
  assert.doesNotMatch(phone, /getUserMedia\(/);
  assert.match(phone, /"\/api\/pai\/chat\/voice-memo"/);
  // A private turn (a PIN) is never shown back as typed.
  assert.match(phone, /data\.private \? PRIVATE_LINE/);
});

test("Send to Puff takes a failed or refused line back off the screen", () => {
  const send = phone.slice(phone.indexOf("const sendText"), phone.indexOf("const sendMemo"));
  // 401, a refused answer, and a lost connection each drop the pending line.
  assert.equal(send.match(/dropPending\(\);/g)?.length, 3);
  assert.match(send, /status === 401\) \{\s*dropPending\(\);/);
  assert.doesNotMatch(send, /setDraft\(text\)/);
});

test("Send to Puff sends no credential of its own and handles a lost pairing", () => {
  assert.doesNotMatch(phone, /X-API-Key|Authorization/);
  assert.match(phone, /status === 401/);
  assert.match(sendPage, /<PhoneChat onUnpaired=\{toPairing\} \/>/);
  // Only rendered behind the pairing gate.
  const ready = sendPage.indexOf('phase === "ready" && (');
  assert.ok(ready > 0 && sendPage.indexOf("<PhoneChat") > ready);
});

if (failed) {
  console.error(`\n${failed} failed`);
  process.exit(1);
}
console.log("\nall passed");
