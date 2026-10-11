"use client";

import { useEffect, useState } from "react";
import { fetchPendingSuggestions, type Suggestion } from "../suggestions";

/** Normal cadence — classification lands seconds after an upload. */
const FAST_MS = 5000;
/** Unpaired (401) or a sulking backend: stop asking so often. */
const SLOW_MS = 60000;

/**
 * usePendingSuggestions — polls the pending queue, oldest first.
 *
 * Self-scheduling rather than setInterval so the 401 backoff is one line.
 * Skips the request while the tab is hidden (same contract as useAppData) and
 * goes completely silent on an unpaired device — no error state, no banner.
 */
export function usePendingSuggestions(enabled = true): {
  suggestions: Suggestion[];
  unpaired: boolean;
} {
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [unpaired, setUnpaired] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const run = async () => {
      if (cancelled) return;
      if (typeof document !== "undefined" && document.hidden) {
        timer = setTimeout(run, FAST_MS);
        return;
      }
      const res = await fetchPendingSuggestions();
      if (cancelled) return;
      if (res.status === "ok") {
        setUnpaired(false);
        // Backend answers newest-first; queue them oldest-first.
        setSuggestions([...res.suggestions].reverse());
        timer = setTimeout(run, FAST_MS);
      } else {
        setUnpaired(res.status === "unpaired");
        setSuggestions([]);
        timer = setTimeout(run, SLOW_MS);
      }
    };

    void run();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [enabled]);

  return { suggestions, unpaired };
}
