/**
 * Shared timer/alarm time math (the dashboard, AlarmFlow page, widget, and
 * FloatingPuff all need the same answer to "how long until this fires?").
 */

export interface AlarmishItem {
  id: string;
  mode: "timer" | "alarm";
  desc: string;
  status: string;
  sound?: string;
  ts?: number | null;
  tot?: number;
  rem?: number;
  targetMs?: number | null;
}

/** Live seconds remaining; negative once it has fired. */
export function computeLiveRemaining(item: AlarmishItem, nowMs: number): number {
  if (!item) return 0;
  // Paused timers freeze: the backend pins `rem` and nulls `ts`.
  if (item.status === "paused") return item.rem || 0;
  if (item.mode === "alarm" && item.targetMs) {
    return Math.floor((item.targetMs - nowMs) / 1000);
  }
  if (item.mode === "timer" && item.ts && item.tot) {
    return Math.floor((item.ts + item.tot * 1000 - nowMs) / 1000);
  }
  return item.rem || 0;
}

/** Items that are ringing right now (fired within the last 5 minutes and
 * not dismissed) — the same window the dashboard treats as "still going". */
export function ringingItems(items: AlarmishItem[], nowMs: number): AlarmishItem[] {
  return (items || []).filter((i) => {
    if (i.status === "stopped" || i.status === "paused") return false;
    const rem = computeLiveRemaining(i, nowMs);
    return rem <= 0 && rem > -300;
  });
}

/**
 * Milliseconds until ringingItems(items, t) can next change on time alone
 * (an item starts ringing, or ages out 5 minutes past due), or null when no
 * time boundary is ahead. Lets a caller sleep to that moment instead of
 * re-checking every second. Add a small margin before re-checking.
 */
export function msUntilRingingChange(
  items: AlarmishItem[],
  nowMs: number
): number | null {
  let next = Infinity;
  for (const i of items || []) {
    if (i.status === "stopped" || i.status === "paused") continue;
    let dueMs: number | null = null;
    if (i.mode === "alarm" && i.targetMs) dueMs = i.targetMs;
    else if (i.mode === "timer" && i.ts && i.tot) dueMs = i.ts + i.tot * 1000;
    if (dueMs == null) continue;
    // computeLiveRemaining floors to seconds: rem <= 0 from just after
    // due-1000, rem > -300 until due+299000.
    for (const b of [dueMs - 1000, dueMs + 299_000]) {
      if (b >= nowMs && b < next) next = b;
    }
  }
  return Number.isFinite(next) ? next - nowMs : null;
}

/**
 * Statuses the SERVER sets when it has fired an item.
 *
 * Firing is server-side: puffpai.scheduler marks a due item
 * `fired` (or `missed`, when the appliance was off at the time) and announces
 * it on the appliance speaker, so an alarm rings whether or not any screen is
 * open. Every client already polls `/apps/alarmflow/updated`, so the status
 * change reaches all of them; the browser's job is to SHOW the ring, not to
 * decide it.
 */
export const SERVER_FIRED = "fired";
export const SERVER_MISSED = "missed";

/** Has the server rung this item? The one question a client should ask. */
export function isServerFired(item: AlarmishItem): boolean {
  return item?.status === SERVER_FIRED;
}

/**
 * Should this client make a noise for *item*?
 *
 * True only for an item the server says it fired. Deliberately NOT a due-time
 * comparison: components that compute their own due times each ring only
 * while they are the mounted screen, which is exactly how an alarm ends up
 * ringing nowhere. A `missed` item is shown, never chimed — the
 * moment has passed and the speaker already said so.
 */
export function shouldChime(item: AlarmishItem): boolean {
  return isServerFired(item);
}
