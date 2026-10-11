"use client";

/**
 * "You keep telling me she will stop when I say but she doesn't."
 *
 * This hook is the eye and the hand of TAP TO STOP: it knows whether the
 * appliance is speaking right now, and it carries the one call that shuts her
 * up. Every surface that offers a Stop control (the kiosk bar, the NavBar
 * pill, Puff's own bubble) reads from here, so they agree and so the stop is
 * the same stop everywhere.
 *
 * ONE POLLER, NOT ONE PER MOUNT
 * -----------------------------
 * Three surfaces mount this at once and it polls twice a second. Per-mount
 * intervals would be 6 requests/second at the box for a two-field answer.
 * The poller below is module-level with a subscriber set: it starts on the
 * first mount, stops on the last unmount, and every subscriber sees the same
 * snapshot in the same tick.
 *
 * It PAUSES WHEN THE TAB IS HIDDEN (visibilitychange) and resumes with an
 * immediate read, so a backgrounded phone is not a permanent 2 Hz load on the
 * appliance — and so the first thing you see on returning is current.
 *
 * THE OPTIMISTIC HIDE, AND WHY IT CANNOT STRAND YOU
 * -------------------------------------------------
 * A tap hides the control immediately; the backend's `speaking` may still be
 * true for the poll or two it takes to tear the audio down, and a control that
 * flickered back would read as "it didn't work". So after a stop we suppress
 * `speaking` — but only for the utterance that was running when we tapped:
 *
 *   • a poll answering `speaking: false` clears it (the stop landed), and
 *   • a poll answering `speaking: true` with a `since` LATER than our tap
 *     clears it too — that is a NEW sentence, which must be stoppable at once.
 *   • A 2.5 s ceiling clears it regardless, so a backend that answers
 *     `since: null` forever can never leave the owner without the control.
 *
 * A failed stop clears the suppression straight away: the control comes back
 * with the error beside it rather than pretending.
 */

import { useCallback, useEffect, useState } from "react";
import {
  getVoiceState,
  stopVoice,
  type VoiceSpeakingState,
} from "../services/voiceService";

const POLL_MS = 500;
/** Longest an optimistic hide may outlive the truth. */
const SUPPRESS_MAX_MS = 2_500;

type Snapshot = VoiceSpeakingState & { error: string | null };

let raw: VoiceSpeakingState = { speaking: false, since: null, voiceService: true };
let error: string | null = null;
/** False until a poll answers, and after any poll that errors. */
let rawKnown = false;
/** Epoch ms of the last tap, or 0 when nothing is being suppressed. */
let suppressedAt = 0;
/** `since` (epoch seconds) of the utterance that tap was aimed at. */
let suppressedSince: number | null = null;

const subscribers = new Set<(s: Snapshot) => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let inFlight = false;

function snapshot(): Snapshot {
  const speaking = raw.speaking && suppressedAt === 0;
  return { speaking, since: speaking ? raw.since : null, voiceService: raw.voiceService, error };
}

function publish() {
  const s = snapshot();
  subscribers.forEach((fn) => fn(s));
}

function clearSuppressionIfStale() {
  if (suppressedAt === 0) return;
  if (Date.now() - suppressedAt > SUPPRESS_MAX_MS) {
    suppressedAt = 0;
    suppressedSince = null;
    return;
  }
  if (!raw.speaking) {
    suppressedAt = 0;
    suppressedSince = null;
    return;
  }
  // Still speaking — but is it the same sentence? A later `since` is a new one.
  if (
    typeof raw.since === "number" &&
    (suppressedSince === null || raw.since > suppressedSince)
  ) {
    suppressedAt = 0;
    suppressedSince = null;
  }
}

async function poll() {
  if (inFlight) return;
  inFlight = true;
  try {
    raw = await getVoiceState();
    rawKnown = true;
  } catch {
    rawKnown = false;
    // Backend unreachable or the route missing: treat as silent.
    // A Stop bar that latches on because the box went away would be worse than
    // no bar at all — it would be a button that can never succeed.
    raw = { speaking: false, since: null, voiceService: true };
  } finally {
    inFlight = false;
  }
  clearSuppressionIfStale();
  publish();
}

function running() {
  return timer !== null;
}

function start() {
  if (running()) return;
  if (typeof document !== "undefined" && document.visibilityState === "hidden")
    return;
  void poll();
  timer = setInterval(() => void poll(), POLL_MS);
}

function stop() {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

function onVisibility() {
  if (subscribers.size === 0) return;
  if (document.visibilityState === "hidden") stop();
  else start();
}

/* ── Read access for the one-speaker gate (lib/services/roomSpeaker) ──
   No timer of its own: it reads the shared poller's last answer, and only
   when nobody is subscribed does it start the same poller for one read. */

/** True while the shared poller is running (its `raw` is at most 500 ms old). */
export function voiceStateIsLive(): boolean {
  return running();
}

/** What the box's mouth was doing at the last poll (ignores optimistic stops). */
export function roomSpeakingNow(): boolean {
  return raw.speaking;
}

/** False only when the backend said this machine has no voice unit. */
export function voiceServiceNow(): boolean {
  return raw.voiceService;
}

/** False when the last poll errored (or none has answered yet): the room's
 *  state is unknown, which the one-speaker gate treats as busy on the kiosk. */
export function roomStateKnownNow(): boolean {
  return rawKnown;
}

/** Resolves after the next poll answer (or `maxMs`, so a hidden tab can't hang it). */
export function nextVoiceState(maxMs = 1_000): Promise<void> {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      clearTimeout(cap);
      subscribers.delete(fn);
      if (subscribers.size === 0) stop();
      resolve();
    };
    const fn = () => finish();
    const cap = setTimeout(finish, maxMs);
    subscribers.add(fn);
    start();
  });
}

export interface VoiceStateController {
  /** True only while she is audibly speaking AND no tap is being honoured. */
  speaking: boolean;
  /** Epoch seconds the current utterance began, when the backend reports it. */
  since: number | null;
  /** Last stop failure, for the surface to show beside its control. */
  error: string | null;
  /** Tap to stop. `music: true` is "Stop everything" (radio as well). */
  stop: (music?: boolean) => Promise<void>;
}

export function useVoiceState(): VoiceStateController {
  const [state, setState] = useState<Snapshot>(() => snapshot());

  useEffect(() => {
    const fn = (s: Snapshot) => setState(s);
    subscribers.add(fn);
    if (subscribers.size === 1) {
      document.addEventListener("visibilitychange", onVisibility);
    }
    start();
    return () => {
      subscribers.delete(fn);
      if (subscribers.size === 0) {
        document.removeEventListener("visibilitychange", onVisibility);
        stop();
      }
    };
  }, []);

  const doStop = useCallback(async (music = false) => {
    suppressedAt = Date.now();
    suppressedSince = raw.since;
    error = null;
    publish();
    try {
      await stopVoice(music);
      // Don't wait a whole tick to confirm: read back immediately.
      void poll();
    } catch (e) {
      suppressedAt = 0;
      suppressedSince = null;
      error = e instanceof Error ? e.message : "Stop failed";
      publish();
    }
  }, []);

  return {
    speaking: state.speaking,
    since: state.since,
    error: state.error,
    stop: doStop,
  };
}
