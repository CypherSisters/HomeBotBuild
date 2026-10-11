/**
 * One speaker in the room: "should this surface speak now?"
 *
 *   npm run test:roomspeech   (or: node app/_lib/__tests__/room-speech.test.mjs)
 *
 * 1. Real unit tests of app/_lib/roomSpeech.ts (pure, importable).
 * 2. Source tripwires on the surfaces that used to talk over the voice
 *    service (they are not importable under Node's type stripping).
 *
 * Requires Node >=22.6 for native TypeScript type stripping.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const rs = await import("../roomSpeech.ts");
const here = dirname(fileURLToPath(import.meta.url));
const app = join(here, "..", "..");
const read = (p) => readFileSync(join(app, p), "utf8");

let failed = 0;
let passed = 0;
const test = (name, fn) => {
  try {
    fn();
    passed++;
    console.log(`ok   ${name}`);
  } catch (e) {
    failed++;
    console.error(`FAIL ${name}\n     ${e.message}`);
  }
};

const NOW = 1_800_000_000_000;
const ask = (over) =>
  rs.decideSpeech({
    kiosk: true,
    source: "local-reply",
    roomSpeaking: false,
    createdAtMs: NOW,
    nowMs: NOW,
    ...over,
  });
const ALL = ["announcement", "alarm", "voice-turn", "history", "local-reply", "suggestion"];

test("off the kiosk every source speaks, even while the box talks, even stale", () => {
  for (const source of ALL)
    for (const roomSpeaking of [false, true])
      assert.equal(
        ask({ kiosk: false, source, roomSpeaking, createdAtMs: NOW - 60_000 }),
        "speak",
        `${source} roomSpeaking=${roomSpeaking}`,
      );
});

test("a kiosk with no voice unit speaks every source itself", () => {
  for (const source of ALL)
    assert.equal(ask({ source, voiceService: false, roomSpeaking: true }), "speak", source);
});

test("a kiosk that has a voice unit, or does not say, still defers to it", () => {
  for (const voiceService of [true, undefined])
    assert.equal(ask({ source: "announcement", voiceService }), "skip", String(voiceService));
});

test("on the kiosk, what the voice service says is never spoken again", () => {
  for (const source of ["announcement", "alarm", "voice-turn", "history"]) {
    assert.equal(ask({ source }), "skip", `${source} idle room`);
    assert.equal(ask({ source, roomSpeaking: true }), "skip", `${source} busy room`);
  }
});

test("the 23:36:44 case: an announcement while the box is mid-answer stays silent", () => {
  assert.equal(ask({ source: "announcement", roomSpeaking: true }), "skip");
});

test("on the kiosk, a local reply speaks when the room is quiet", () => {
  assert.equal(ask({ source: "local-reply" }), "speak");
  assert.equal(ask({ source: "suggestion" }), "speak");
});

test("on the kiosk, a local reply waits while anyone is speaking", () => {
  assert.equal(ask({ source: "local-reply", roomSpeaking: true }), "wait");
  assert.equal(ask({ source: "suggestion", roomSpeaking: true }), "wait");
});

test("on the kiosk, a line over 15 s old is skipped, at 15 s it still counts", () => {
  assert.equal(rs.STALE_MS, 15_000);
  assert.equal(ask({ createdAtMs: NOW - 15_000 }), "speak");
  assert.equal(ask({ createdAtMs: NOW - 15_000, roomSpeaking: true }), "wait");
  assert.equal(ask({ createdAtMs: NOW - 15_001 }), "skip");
  assert.equal(ask({ createdAtMs: NOW - 15_001, roomSpeaking: true }), "skip");
});

test("on the kiosk, an unknown or erroring room state counts as busy, then drops at 15 s", () => {
  assert.equal(ask({ roomStateKnown: false }), "wait");
  assert.equal(ask({ source: "suggestion", roomStateKnown: false }), "wait");
  assert.equal(ask({ roomStateKnown: false, createdAtMs: NOW - 15_000 }), "wait");
  assert.equal(ask({ roomStateKnown: false, createdAtMs: NOW - 15_001 }), "skip");
  assert.equal(ask({ roomStateKnown: true }), "speak");
  assert.equal(ask({}), "speak", "omitted = known");
});

test("off the kiosk an unknown room state changes nothing", () => {
  for (const source of ALL)
    assert.equal(ask({ kiosk: false, source, roomStateKnown: false }), "speak", source);
});

test("the voice-service source list is exactly the four it speaks itself", () => {
  assert.deepEqual(
    [...rs.VOICE_SERVICE_SOURCES].sort(),
    ["alarm", "announcement", "history", "voice-turn"],
  );
});

/* ── Tripwires: every kiosk speaking surface goes through the gate ── */

test("AnnouncementToast asks the rule and marks kiosk cards as the house's", () => {
  const src = read("_components/AnnouncementToast.tsx");
  assert.match(src, /decideSpeech\(\{[\s\S]*?source: "announcement"/);
  assert.match(src, /houseSays \? \{ \.\.\.raw, spoken: true \}/);
  assert.match(src, /if \(!a\.spoken\) enqueueSpeech\(a\)/);
});

test("FloatingPuff gates alarms and suggestions, and lip-syncs to the box on the kiosk", () => {
  const src = read("dashboard/_components/FloatingPuff.tsx");
  assert.match(src, /withRoomSpeaker\("alarm"/);
  assert.match(src, /withRoomSpeaker\("suggestion"/);
  assert.doesNotMatch(src, /^\s*void bubbleRef\.current\?\.speakText/m);
  assert.match(src, /const talking = speaking \|\| \(voice\.speaking && isKiosk\(\)\)/);
});

test("tap-mic, Talk, Hatch and typed chat speak only through the gate", () => {
  for (const p of [
    "_components/Avatar/AvatarTile.tsx",
    "dashboard/talk/page.tsx",
    "dashboard/hatch/page.tsx",
    "dashboard/chat/page.tsx",
  ]) {
    const src = read(p);
    assert.match(src, /withRoomSpeaker\("local-reply"/, p);
    const calls = src.match(/await synthesize\(|await speak\(|\bspeak\(\s*\n?\s*text,/g) || [];
    const gated = src.match(/withRoomSpeaker\("local-reply"[\s\S]{0,200}?(synthesize\(|speak\(|speakOneNow\()/g) || [];
    assert.ok(gated.length >= 1, `${p}: speech call not inside the gate`);
    assert.ok(calls.length <= gated.length + 0, `${p}: ${calls.length} speech calls, ${gated.length} gated`);
  }
});

test("the gate adds no interval of its own", () => {
  const src = read("_lib/services/roomSpeaker.ts");
  assert.doesNotMatch(src, /setInterval|setTimeout/);
  assert.match(src, /if \(!isKiosk\(\)\) \{\s*await play\(\);\s*return true;/);
});

test("the gate passes the poller's known/unknown state, and errors mark it unknown", () => {
  assert.match(read("_lib/services/roomSpeaker.ts"), /roomStateKnown: roomStateKnownNow\(\)/);
  const hook = read("_lib/hooks/useVoiceState.ts");
  assert.match(hook, /raw = await getVoiceState\(\);\s*rawKnown = true;\s*\} catch \{\s*rawKnown = false;/);
});

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
