"use client";

/**
 * TAP TO STOP.
 *
 * A spoken "stop" has to travel through wake, transcription and routing, and
 * any one of them can drop it while she is talking over you. A finger cannot
 * be mis-heard. So while she is speaking there is always a
 * physical target on the screen, and its job is one HTTP POST.
 *
 * Two surfaces, one hook (useVoiceState — one poller, shared):
 *
 *   • KIOSK: a bar pinned across the bottom of the wall display, 96px tall
 *     minimum, the WHOLE bar is the button. It is the last thing above the
 *     bezel, which is where a hand already is.
 *   • DESKTOP/PHONE: a pill in the topbar, sized like the other NavBar
 *     controls so it costs no layout when she is quiet (it is not rendered).
 *
 * Plus Puff's own bubble, which becomes a stop target while she speaks — see
 * FloatingPuff's onPointerUp. Whatever your eye is already on is tappable.
 *
 * "Stop everything" (music too) is the SECOND gesture on purpose. Stopping the
 * sentence is what the owner asks for ten times a day; killing the radio with
 * it would make the control something she has to think about first.
 * Long-press either surface for ~600ms, or hit the small secondary button on
 * the bar.
 *
 * On failure nothing is faked: the optimistic hide is rolled back by the hook
 * and the backend's own message is printed on the control. This tree has no
 * global toast component (the "toasts" are page-local state), so the error
 * goes where the finger already is rather than somewhere else on the screen.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import MsIcon from "../../_components/MsIcon";
import { useVoiceState } from "@/app/_lib/hooks/useVoiceState";
import { useIsKiosk } from "../_lib/kiosk";

/** Hold this long and the tap means "everything", music included. */
const LONG_PRESS_MS = 600;

/**
 * Press handlers shared by the bar and the pill: tap → stop the voice,
 * long-press → stop everything. Pointer events, not click, so the long press
 * is detectable on the touchscreen and the mouse alike.
 */
function useStopPress(stop: (music?: boolean) => Promise<void>) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const firedLong = useRef(false);

  const clear = useCallback(() => {
    if (timer.current !== null) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => clear, [clear]);

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      firedLong.current = false;
      clear();
      timer.current = setTimeout(() => {
        firedLong.current = true;
        timer.current = null;
        void stop(true);
      }, LONG_PRESS_MS);
    },
    [clear, stop]
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      clear();
      if (firedLong.current) return; // the hold already stopped everything
      void stop(false);
    },
    [clear, stop]
  );

  // Pointer left the control mid-press, or the browser stole the gesture:
  // cancel the pending long press rather than firing it blind.
  const onPointerCancel = useCallback(() => {
    clear();
    firedLong.current = false;
  }, [clear]);

  return { onPointerDown, onPointerUp, onPointerCancel, onPointerLeave: onPointerCancel };
}

/**
 * The kiosk bar. Mounted in the dashboard layout; renders nothing unless this
 * screen is the wall display AND she is speaking.
 */
export function VoiceStopBar() {
  const isKiosk = useIsKiosk();
  const { speaking, error, stop } = useVoiceState();
  const press = useStopPress(stop);
  const [everything, setEverything] = useState(false);

  if (!isKiosk || !speaking) return null;

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label="Tap to stop Puff talking. Hold to stop everything."
      {...press}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") void stop(false);
      }}
      className="fixed inset-x-0 bottom-0 z-[90] flex cursor-pointer touch-none select-none items-center justify-center gap-4"
      style={{
        minHeight: 96,
        padding: "0 clamp(16px,3vw,48px)",
        background: "var(--stop)",
        color: "var(--stopText)",
        borderTop: "1px solid rgba(255,255,255,0.35)",
        boxShadow: "0 -14px 40px rgba(0,0,0,0.35)",
      }}
    >
      <MsIcon name="stop_circle" size={44} />
      <div className="min-w-0">
        <div
          style={{
            font: "800 clamp(26px,3vw,40px) var(--font-nunito), sans-serif",
            letterSpacing: "-.01em",
            lineHeight: 1.05,
          }}
        >
          Tap to stop
        </div>
        {error && (
          <div
            className="truncate"
            style={{
              font: "700 13px var(--font-jbmono), monospace",
              opacity: 0.92,
              marginTop: 4,
            }}
          >
            {error}
          </div>
        )}
      </div>
      <button
        type="button"
        // The bar itself is the primary target, so this must not double-fire.
        onPointerDown={(e) => e.stopPropagation()}
        onPointerUp={(e) => {
          e.stopPropagation();
          e.preventDefault();
          setEverything(true);
          void stop(true);
        }}
        aria-label="Stop everything, music included"
        className="flex-none cursor-pointer"
        style={{
          font: "800 15px var(--font-nunito), sans-serif",
          padding: "12px 18px",
          borderRadius: 14,
          border: "2px solid var(--stopText)",
          background: everything ? "var(--stopText)" : "transparent",
          color: everything ? "var(--stop)" : "var(--stopText)",
        }}
      >
        Stop everything
      </button>
    </div>
  );
}

/**
 * The topbar pill. Mounted in NavBar; renders nothing unless she is speaking,
 * and never on the wall display (the bar owns that screen).
 */
export function VoiceStopPill() {
  const isKiosk = useIsKiosk();
  const { speaking, error, stop } = useVoiceState();
  const press = useStopPress(stop);

  if (isKiosk || !speaking) return null;

  return (
    <button
      type="button"
      {...press}
      title={
        error
          ? `Stop failed: ${error}`
          : "Tap to stop Puff talking · hold to stop everything"
      }
      aria-label="Tap to stop Puff talking. Hold to stop everything."
      className="flex flex-none cursor-pointer touch-none select-none items-center"
      style={{
        gap: 9,
        height: 52,
        padding: "0 20px",
        borderRadius: 16,
        border: "1px solid var(--stopText)",
        font: "800 16px var(--font-nunito), sans-serif",
        background: "var(--stop)",
        color: "var(--stopText)",
      }}
    >
      <MsIcon name="stop_circle" size={24} />
      {error ? "Stop failed" : "Tap to stop"}
    </button>
  );
}
