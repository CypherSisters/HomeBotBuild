"use client";

/**
 * A scroll region that follows the bottom while text streams in — unless the
 * reader has scrolled up to re-read something, in which case it holds still
 * until they come back down.
 *
 * Used by every surface that shows a reply as it is being written (transcript
 * tile, Talk page, the thinking block), so they all behave the same way.
 *
 * `dep` is whatever changes when new text lands (the text itself is fine).
 */

import { useCallback, useEffect, useRef } from "react";

/** Within this many px of the bottom still counts as "at the bottom". */
const BOTTOM_SLACK_PX = 24;

export function useAutoFollow<T extends HTMLElement>(dep: unknown) {
  const ref = useRef<T | null>(null);
  const following = useRef(true);

  useEffect(() => {
    const el = ref.current;
    if (!el || !following.current) return;
    el.scrollTop = el.scrollHeight;
  }, [dep]);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    following.current = distance <= BOTTOM_SLACK_PX;
  }, []);

  return { ref, onScroll };
}
