/**
 * One speaker in the room.
 *
 * On the box, the voice service (Piper through the room speakers) owns the
 * room's voice. The kiosk browser plays through the SAME speakers, so if it
 * also says something the owner hears two voices at once (the voice service
 * mid-answer while the kiosk reads a zigbee pairing announcement out loud,
 * and then the voice service reading it again).
 *
 * This is the pure decision every speaking surface asks before it makes a
 * sound. No DOM, no timers, so it is testable under plain Node.
 *
 * Off the kiosk (a phone or laptop in another room) nothing changes: its
 * speakers are not the room's, so it always speaks as it did before. Voice is
 * optional: a kiosk on a machine with no voice unit has nobody to defer to, so
 * it speaks like any other screen.
 */

/** Where the words came from. */
export type SpeechSource =
  /** /announcements entries: the voice service announces these itself. */
  | "announcement"
  /** A ringing alarm or timer: the box scheduler announces these itself. */
  | "alarm"
  /** A wake-word turn picked up from /voice/live: the voice service said it. */
  | "voice-turn"
  /** A reply replayed from chat history: already said, or not this room's. */
  | "history"
  /** A reply to something done ON this screen (typed chat, tap-mic, a script). */
  | "local-reply"
  /** A proactive suggestion only the dashboard knows about. */
  | "suggestion";

export type SpeechDecision = "speak" | "wait" | "skip";

/** A queued line older than this is skipped, not spoken late. */
export const STALE_MS = 15_000;

/** Sources the voice service speaks itself. The kiosk never voices these. */
export const VOICE_SERVICE_SOURCES: ReadonlySet<SpeechSource> = new Set<SpeechSource>([
  "announcement",
  "alarm",
  "voice-turn",
  "history",
]);

export interface SpeechQuestion {
  /** True on the box's own screen (html.puff-kiosk). */
  kiosk: boolean;
  /** False when the machine has no voice unit. Omitted = it has one. */
  voiceService?: boolean;
  source: SpeechSource;
  /** Someone is already talking through the room speakers. */
  roomSpeaking: boolean;
  /** False when /voice/state errored or has not answered yet. On the kiosk an
   *  unknown room counts as busy (wait, then skip once stale), so the screen
   *  never talks over a box it cannot hear. Omitted = known. */
  roomStateKnown?: boolean;
  /** When the words became ready to say (epoch ms). */
  createdAtMs: number;
  nowMs: number;
}

/**
 * Should this surface speak now?
 *   speak: go ahead.
 *   wait:  someone else has the room; ask again after the next voice state.
 *   skip:  stay silent (show the text only).
 */
export function decideSpeech(q: SpeechQuestion): SpeechDecision {
  if (!q.kiosk || q.voiceService === false) return "speak";
  if (VOICE_SERVICE_SOURCES.has(q.source)) return "skip";
  if (q.nowMs - q.createdAtMs > STALE_MS) return "skip";
  return q.roomSpeaking || q.roomStateKnown === false ? "wait" : "speak";
}
