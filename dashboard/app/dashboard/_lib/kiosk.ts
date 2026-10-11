"use client";

/**
 * Kiosk (wall display) detection.
 *
 * The wall display is a public surface: kiosk-gated tiles must not offer a way
 * to navigate off the board or show private controls. Every such rule keys off
 * the same check, so it lives in one place rather than being re-implemented
 * per component.
 *
 * It FAILS CLOSED: if any of the three signals is unreadable we assume "wall
 * display", and the hook starts at `true` so the first paint (and any SSR
 * output) is the safe one.
 */

import { useEffect, useState } from "react";

export function detectKiosk(): boolean {
  if (typeof window === "undefined") return false;
  try {
    const q = new URLSearchParams(window.location.search);
    if (q.get("puffMode") === "kiosk") return true;
    if (document.documentElement.classList.contains("puff-kiosk")) return true;
    if (window.localStorage.getItem("puffpai_display_mode") === "kiosk") return true;
  } catch {
    /* fail closed toward "wall display" if any of these are unreadable */
    return true;
  }
  return false;
}

/** `true` until measured on the client, so nothing kiosk-gated flashes first. */
export function useIsKiosk(): boolean {
  const [isKiosk, setIsKiosk] = useState(true);
  useEffect(() => setIsKiosk(detectKiosk()), []);
  return isKiosk;
}
