"use client";

/**
 * The model's reasoning, shown while it is still reasoning.
 *
 * Subordinate by design: small mono text at --text3 behind a hairline rule, so
 * it reads as workings-out beside the answer rather than as the answer. The
 * moment real reply tokens start arriving it folds itself into a one-line
 * disclosure — the answer is what the room is waiting for — and a tap opens it
 * again.
 *
 * Renders NOTHING when there is no thinking text. There is never a placeholder
 * and never invented copy: no thinking means no block.
 *
 * Display only. This text is never spoken, never saved to history, never
 * written to localStorage.
 */

import { useState } from "react";
import { useAutoFollow } from "@/app/_lib/hooks/useAutoFollow";

export default function ThinkingBlock({
  text,
  /** True once the reply has started — collapses unless the reader opened it. */
  collapsed,
  /** Cap on the scroll region; ~30% of whatever region this sits in. */
  maxHeight,
  fontSize = 12,
}: {
  text: string;
  collapsed: boolean;
  maxHeight: number | string;
  fontSize?: number;
}) {
  const [opened, setOpened] = useState(false);
  const { ref, onScroll } = useAutoFollow<HTMLDivElement>(text);

  if (!text.trim()) return null;

  const mono = "var(--font-jbmono), monospace";

  if (collapsed && !opened) {
    return (
      <button
        type="button"
        onClick={() => setOpened(true)}
        className="cursor-pointer text-left"
        style={{
          font: `600 ${Math.max(11, fontSize - 1)}px ${mono}`,
          letterSpacing: ".04em",
          color: "var(--text3)",
          background: "none",
          padding: 0,
        }}
      >
        Thought for a moment &#9656;
      </button>
    );
  }

  return (
    <div style={{ borderLeft: "1px solid var(--line)", paddingLeft: 10 }}>
      <div
        style={{
          font: "700 10px var(--font-jbmono), monospace",
          letterSpacing: ".14em",
          textTransform: "uppercase",
          color: "var(--text3)",
          marginBottom: 3,
        }}
      >
        Thinking
      </div>
      <div
        ref={ref}
        onScroll={onScroll}
        tabIndex={0}
        className="puff-scroll"
        style={{
          maxHeight,
          overflowY: "auto",
          font: `400 ${fontSize}px/1.5 ${mono}`,
          fontStyle: "italic",
          color: "var(--text3)",
          whiteSpace: "pre-wrap",
        }}
      >
        {text}
      </div>
    </div>
  );
}
