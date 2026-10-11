/**
 * The runtime half of the one-speaker rule (pure rule: lib/roomSpeech.ts).
 *
 *   await withRoomSpeaker("local-reply", () => playIt())
 *
 * Off the kiosk this just runs `play` (today's behaviour, no polling). On the
 * kiosk it skips anything the voice service speaks itself, waits while the
 * room is busy (the box talking, another kiosk surface talking, or /voice/state
 * erroring or not yet answered, so the room is unknown), and gives
 * up silently once the line is over STALE_MS old. Waiting rides the existing
 * shared /voice/state poller; it adds no interval of its own.
 */

import { decideSpeech, type SpeechSource } from "../roomSpeech";
import { isKiosk } from "../kioskActivity";
import {
  nextVoiceState,
  roomSpeakingNow,
  roomStateKnownNow,
  voiceServiceNow,
  voiceStateIsLive,
} from "../hooks/useVoiceState";

/** A kiosk surface is mid-sentence: the others queue behind it. */
let localHolder = false;

/** Returns true if `play` ran, false if the line was skipped. */
export async function withRoomSpeaker(
  source: SpeechSource,
  play: () => Promise<void>,
  createdAtMs: number = Date.now(),
): Promise<boolean> {
  if (!isKiosk()) {
    await play();
    return true;
  }
  // An idle poller's last answer may be old: get a fresh one first.
  if (!voiceStateIsLive()) await nextVoiceState();
  for (;;) {
    const decision = decideSpeech({
      kiosk: true,
      voiceService: voiceServiceNow(),
      source,
      roomSpeaking: localHolder || roomSpeakingNow(),
      roomStateKnown: roomStateKnownNow(),
      createdAtMs,
      nowMs: Date.now(),
    });
    if (decision === "skip") return false;
    if (decision === "speak") break;
    await nextVoiceState();
  }
  localHolder = true;
  try {
    await play();
  } finally {
    localHolder = false;
  }
  return true;
}
