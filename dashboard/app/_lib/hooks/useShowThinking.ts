"use client";

/**
 * "Show Puff's thinking" (Settings), default ON.
 *
 * Same contract as the other display flags: localStorage '1'/'0', and the
 * settings page fires CustomEvent('puffpai-setting-changed') on every write so
 * an open dashboard follows the toggle without a reload.
 */

import { useEffect, useState } from "react";

export const SHOW_THINKING_KEY = "puffpai_show_thinking";

export function useShowThinking(): boolean {
  // Starts ON, which is also the SSR value: a stored '0' lands on the first
  // effect, before anything has had time to stream.
  const [on, setOn] = useState(true);

  useEffect(() => {
    const read = () => {
      try {
        setOn(localStorage.getItem(SHOW_THINKING_KEY) !== "0");
      } catch {
        /* private mode — the default stands */
      }
    };
    read();
    window.addEventListener("storage", read);
    window.addEventListener("puffpai-setting-changed", read as EventListener);
    return () => {
      window.removeEventListener("storage", read);
      window.removeEventListener(
        "puffpai-setting-changed",
        read as EventListener,
      );
    };
  }, []);

  return on;
}
