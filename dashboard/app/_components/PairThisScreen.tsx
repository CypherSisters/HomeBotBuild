"use client";

/**
 * "Pair this screen to use this page", with how to get the code, for the
 * pages that only paired screens may use. The button opens the pairing box
 * (PairPrompt), even when it was put away with "Not now". The code is never
 * shown or read here.
 */

import { PAIR_HOW_LINES, PAIR_OPEN_EVENT, PAIR_PAGE_LINE } from "../_lib/pairingWords";

const NUNITO = "var(--font-nunito), sans-serif";

export default function PairThisScreen() {
  return (
    <div
      role="status"
      data-pair-this-screen=""
      style={{
        border: "1px solid var(--line)",
        borderRadius: 18,
        background: "var(--card)",
        padding: "14px 18px",
        display: "grid",
        gap: 6,
        fontFamily: NUNITO,
        color: "var(--text)",
      }}
    >
      <p style={{ margin: 0, fontWeight: 700, fontSize: 16 }}>{PAIR_PAGE_LINE}</p>
      {PAIR_HOW_LINES.map((line) => (
        <p key={line} style={{ margin: 0, fontSize: 14, color: "var(--text2)" }}>
          {line}
        </p>
      ))}
      <div>
        <button
          type="button"
          onClick={() => window.dispatchEvent(new Event(PAIR_OPEN_EVENT))}
          style={{
            marginTop: 4,
            padding: "6px 16px",
            borderRadius: 999,
            border: "1px solid var(--line)",
            background: "var(--card2)",
            color: "var(--text)",
            fontFamily: NUNITO,
            fontWeight: 700,
            fontSize: 14,
            cursor: "pointer",
          }}
        >
          Pair this screen
        </button>
      </div>
    </div>
  );
}
