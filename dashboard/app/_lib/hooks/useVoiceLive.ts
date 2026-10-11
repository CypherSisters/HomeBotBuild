"use client";

/**
 * Puts the turn happening AT THE BOX into the store.
 *
 * A wake-word turn never touches this browser: the wall hears "Hey Puff",
 * answers out loud, and the screen in front of you knows nothing about it
 * until the exchange lands in chat history minutes later. This hook closes
 * that gap by polling /voice/live and writing it into liveTurnSlice, which is
 * what the transcript tile and the Talk page render.
 *
 * COST DISCIPLINE. The kiosk runs this 24/7 on an appliance that also holds
 * an LLM resident, so:
 *   • 700 ms while a turn is live, 2.5 s when nothing is happening,
 *   • nothing at all while the tab is hidden (resumes with an immediate read),
 *   • exponential backoff to 30 s on any non-200 — a box without the route
 *     must not be asked about it forever,
 *   • self-scheduling setTimeout, never an interval, so a slow answer cannot
 *     stack requests.
 *
 * ONE POLLER, REF-COUNTED. The poller lives at module level, like
 * useVoiceState's: it starts on the first mount and stops on the LAST unmount.
 * A per-mount guard would have been wrong in the way that is hardest to
 * notice — if the mount that owns the poller goes away while another is still
 * on screen, the survivor is left with no poller at all and a transcript that
 * silently stops updating.
 */

import { useEffect } from "react";
import { useAppDispatch } from "@/app/_stores/hooks";
import { liveTurnCleared, liveTurnSnapshot } from "@/app/_stores/liveTurnSlice";
import { getVoiceLive } from "../services/voiceService";

const ACTIVE_MS = 700;
const IDLE_MS = 2_500;
const BACKOFF_MAX_MS = 30_000;

type Dispatch = ReturnType<typeof useAppDispatch>;

let subscribers = 0;
/** Whoever mounted most recently; every dispatch reaches the same store. */
let dispatchFn: Dispatch | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let backoff = 0;

function schedule(ms: number) {
  if (subscribers === 0) return;
  timer = setTimeout(() => void tick(), ms);
}

async function tick() {
  timer = null;
  if (subscribers === 0) return;
  if (typeof document !== "undefined" && document.hidden) return; // resumed by visibilitychange

  const { status, turn } = await getVoiceLive();
  const dispatch = dispatchFn;
  if (subscribers === 0 || !dispatch) return;

  if (status !== 200) {
    // Route missing or box unreachable: say nothing, ask less often.
    backoff = backoff === 0 ? IDLE_MS : Math.min(backoff * 2, BACKOFF_MAX_MS);
    dispatch(liveTurnCleared({ only: "voice" }));
    schedule(backoff);
    return;
  }
  backoff = 0;

  if (!turn || !turn.turnId) {
    dispatch(liveTurnCleared({ only: "voice" }));
    schedule(IDLE_MS);
    return;
  }
  dispatch(
    liveTurnSnapshot({
      user: turn.heard,
      thinking: turn.thinking,
      reply: turn.reply,
      streaming: turn.active && !turn.done,
      turnId: turn.turnId,
    }),
  );
  schedule(turn.active && !turn.done ? ACTIVE_MS : IDLE_MS);
}

function stopPolling() {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}

function onVisibility() {
  if (subscribers === 0) return;
  if (document.hidden) {
    stopPolling();
    return;
  }
  if (timer === null) void tick();
}

export function useVoiceLive(): void {
  const dispatch = useAppDispatch();

  useEffect(() => {
    dispatchFn = dispatch;
    subscribers += 1;
    if (subscribers === 1) {
      document.addEventListener("visibilitychange", onVisibility);
      backoff = 0;
      void tick();
    }
    return () => {
      subscribers -= 1;
      if (subscribers === 0) {
        document.removeEventListener("visibilitychange", onVisibility);
        stopPolling();
        dispatchFn = null;
      }
    };
  }, [dispatch]);
}

/** Renders nothing — a mount point for the poller in the dashboard layout. */
export default function VoiceLiveSync() {
  useVoiceLive();
  return null;
}
