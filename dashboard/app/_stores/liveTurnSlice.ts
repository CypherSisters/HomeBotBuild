/**
 * The ONE turn Puff is in the middle of, wherever it was started.
 *
 * "The live transcript doesn't show all the way" had two halves: the surfaces
 * clamped the text, and they only ever rendered a FINISHED exchange pulled out
 * of chat history. So a reply being spoken out loud was invisible until it was
 * over, and a voice turn started at the wall (wake word, no browser involved)
 * was never visible at all.
 *
 * This slice is the shared answer: whoever is driving the turn writes here —
 * the Talk page stream, the Chat page stream, or the /voice/live poller for an
 * on-box turn — and every surface reads the same thing.
 *
 * Two write shapes, deliberately:
 *   • liveTurnAppend  — a browser stream, which arrives token by token.
 *   • liveTurnSnapshot — the voice poller, which reports absolute text each
 *     time it asks. It must never clobber a stream this browser is driving
 *     (the wall can be answering one question while a phone asks another), so
 *     the reducer drops a voice snapshot while a local stream is running.
 *
 * `thinking` is DISPLAY ONLY. It is never appended to conversation history,
 * never persisted, never sent to TTS.
 */

import { createSlice, PayloadAction } from '@reduxjs/toolkit';

export type LiveTurnSource = 'typed' | 'talk' | 'voice';

export interface LiveTurnState {
  /** null when nothing is in flight and nothing recent is worth showing. */
  source: LiveTurnSource | null;
  /** What was asked: typed text, or what the box heard. */
  user: string;
  /** Model reasoning, when the backend is sending it. */
  thinking: string;
  reply: string;
  /** True while tokens are still arriving. */
  streaming: boolean;
  /** Backend turn id for a voice turn; null for browser-driven turns. */
  turnId: string | null;
  /** Epoch ms of the last write — how surfaces age a finished turn out. */
  updatedAt: number;
}

/**
 * How long a FINISHED turn stays on screen. Matches the window the backend
 * keeps /voice/live readable for, so the wall and the browser forget a turn at
 * about the same moment instead of one of them showing a ghost.
 */
export const LIVE_TURN_MAX_AGE_MS = 90_000;

const initialState: LiveTurnState = {
  source: null,
  user: '',
  thinking: '',
  reply: '',
  streaming: false,
  turnId: null,
  updatedAt: 0,
};

const liveTurnSlice = createSlice({
  name: 'liveTurn',
  initialState,
  reducers: {
    /** A browser-driven turn begins: replaces whatever was showing. */
    liveTurnStarted: (
      state,
      action: PayloadAction<{ source: LiveTurnSource; user: string }>
    ) => {
      state.source = action.payload.source;
      state.user = action.payload.user;
      state.thinking = '';
      state.reply = '';
      state.streaming = true;
      state.turnId = null;
      state.updatedAt = Date.now();
    },
    /** A token (or a reasoning chunk) from a browser stream. */
    liveTurnAppend: (
      state,
      action: PayloadAction<{ reply?: string; thinking?: string }>
    ) => {
      if (state.source === null) return; // append without a start: ignore
      if (action.payload.reply) state.reply += action.payload.reply;
      if (action.payload.thinking) state.thinking += action.payload.thinking;
      state.updatedAt = Date.now();
    },
    /** Tokens stopped arriving. The text stays readable until it ages out. */
    liveTurnFinished: (state) => {
      state.streaming = false;
      state.updatedAt = Date.now();
    },
    /**
     * Absolute state of the on-box voice turn. Ignored while this browser is
     * streaming its own turn — the local one is what the person in front of
     * this screen is doing.
     */
    liveTurnSnapshot: (
      state,
      action: PayloadAction<{
        user: string;
        thinking: string;
        reply: string;
        streaming: boolean;
        turnId: string | null;
      }>
    ) => {
      if (state.source !== null && state.source !== 'voice' && state.streaming) {
        return;
      }
      state.source = 'voice';
      state.user = action.payload.user;
      state.thinking = action.payload.thinking;
      state.reply = action.payload.reply;
      state.streaming = action.payload.streaming;
      state.turnId = action.payload.turnId;
      state.updatedAt = Date.now();
    },
    /**
     * Forget the current turn. `only` scopes it so the voice poller saying
     * "no turn on the box" cannot wipe a turn the Chat page is driving.
     */
    liveTurnCleared: (
      state,
      action: PayloadAction<{ only?: LiveTurnSource } | undefined>
    ) => {
      const only = action.payload?.only;
      if (only && state.source !== only) return;
      Object.assign(state, initialState);
    },
  },
});

/** True if this turn is worth rendering: streaming, or finished recently. */
export function isLiveTurnVisible(
  turn: LiveTurnState,
  now: number = Date.now()
): boolean {
  if (turn.source === null) return false;
  if (!turn.user && !turn.reply && !turn.thinking) return false;
  if (turn.streaming) return true;
  return now - turn.updatedAt < LIVE_TURN_MAX_AGE_MS;
}

export const {
  liveTurnStarted,
  liveTurnAppend,
  liveTurnFinished,
  liveTurnSnapshot,
  liveTurnCleared,
} = liveTurnSlice.actions;
export default liveTurnSlice.reducer;
