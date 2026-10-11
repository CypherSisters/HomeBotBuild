/**
 * Kiosk cadence helpers: is this the wall display, and has anyone touched it
 * lately?
 *
 * Kiosk-ness is the same signal everything else uses: html.puff-kiosk, set
 * pre-hydration by the app/layout.tsx boot script from ?puffMode=kiosk (or the
 * Settings toggle). It is read at call time, so flipping the toggle takes
 * effect on the next scheduled refresh without a reload.
 *
 * The kiosk has no GPU (software compositing), so every repaint costs real
 * CPU. Pollers that change pixels (the camera snapshot) slow down at rest and
 * speed back up for a while after any pointer or keyboard input.
 */

/** Camera snapshot cadence everywhere except an idle kiosk. */
export const CAMERA_POLL_MS = 5_000;
/** Camera snapshot cadence on the kiosk when nobody has touched it lately. */
export const KIOSK_CAMERA_IDLE_POLL_MS = 15_000;
/** How long after the last input the kiosk keeps the fast camera cadence. */
export const KIOSK_ACTIVE_WINDOW_MS = 120_000;

/** Same input set IdleMotionGuard counts as presence. */
const ACTIVITY_EVENTS = [
  "pointerdown",
  "pointermove",
  "keydown",
  "wheel",
  "touchstart",
] as const;

let lastInputMs = 0;
let attached = false;
const wakeListeners = new Set<() => void>();

function onInput() {
  const now = Date.now();
  const wasActive = now - lastInputMs < KIOSK_ACTIVE_WINDOW_MS;
  lastInputMs = now;
  // Only the idle -> active edge notifies, so pointermove spam stays a
  // timestamp write.
  if (!wasActive) wakeListeners.forEach((fn) => fn());
}

function attach() {
  if (attached || typeof window === "undefined") return;
  attached = true;
  for (const ev of ACTIVITY_EVENTS)
    window.addEventListener(ev, onInput, { capture: true, passive: true });
}

export function isKiosk(): boolean {
  return (
    typeof document !== "undefined" &&
    document.documentElement.classList.contains("puff-kiosk")
  );
}

/** Snapshot interval to use right now. */
export function cameraPollMs(): number {
  attach();
  if (!isKiosk()) return CAMERA_POLL_MS;
  return Date.now() - lastInputMs < KIOSK_ACTIVE_WINDOW_MS
    ? CAMERA_POLL_MS
    : KIOSK_CAMERA_IDLE_POLL_MS;
}

/** Called once when input arrives after a quiet spell. Returns unsubscribe. */
export function onInputAfterQuiet(fn: () => void): () => void {
  attach();
  wakeListeners.add(fn);
  return () => {
    wakeListeners.delete(fn);
  };
}
