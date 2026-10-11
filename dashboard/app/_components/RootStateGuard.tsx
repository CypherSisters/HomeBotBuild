"use client";

import { useEffect } from "react";

import { applyTheme, readStoredChoice, resolveTheme } from "../_lib/theme";

/**
 * Keeps the <html> theme attribute and display-mode classes alive.
 *
 * The inline boot script in app/layout.tsx sets data-puff-theme (and the
 * puff-kiosk / puff-big-touch / puff-no-motion classes) before first paint so
 * the page never flashes the wrong palette. React then hydrates <html> — and
 * because neither the attribute nor those classes exist in the server-rendered
 * tree, hydration STRIPS them. suppressHydrationWarning silences the warning
 * but does not stop the reconciliation.
 *
 * Without this, every daytime load would render the night palette a few
 * seconds in, because css/_theme.css maps the attribute-less fallback
 * `html:not([data-puff-theme])` onto night. The kiosk motion governor would
 * lose its always-static branch the same way.
 *
 * So: re-apply after mount, and keep re-applying — a MutationObserver restores
 * the state whenever anything removes it, and a 60s tick moves the auto theme
 * across the 07:00 / 19:00 boundaries without a reload.
 */

const MODE_KEY = "puffpai_display_mode";
const BIG_TOUCH_KEY = "puffpai_big_touch";
const AMBIENT_KEY = "puffpai_ambient_motion";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null; // private mode / storage disabled
  }
}

export default function RootStateGuard() {
  useEffect(() => {
    const root = document.documentElement;

    const apply = () => {
      // The cached choice, which ThemeSync keeps equal to the active
      // profile's preference whenever there is one.
      applyTheme(resolveTheme(readStoredChoice()));

      const want: Record<string, boolean> = {
        "puff-kiosk": read(MODE_KEY) === "kiosk",
        "puff-big-touch": read(BIG_TOUCH_KEY) === "1",
        "puff-no-motion": read(AMBIENT_KEY) === "0",
      };
      for (const [cls, on] of Object.entries(want)) {
        if (root.classList.contains(cls) !== on) root.classList.toggle(cls, on);
      }
    };

    apply();

    // Restore whatever strips it (hydration, a stray className write).
    // apply() only touches the DOM when a value is actually wrong, so the
    // observer cannot drive itself in a loop.
    const mo = new MutationObserver(apply);
    mo.observe(root, {
      attributes: true,
      attributeFilter: ["data-puff-theme", "class"],
    });

    const tick = window.setInterval(apply, 60_000);
    window.addEventListener("focus", apply);
    document.addEventListener("visibilitychange", apply);

    return () => {
      mo.disconnect();
      window.clearInterval(tick);
      window.removeEventListener("focus", apply);
      document.removeEventListener("visibilitychange", apply);
    };
  }, []);

  return null;
}
