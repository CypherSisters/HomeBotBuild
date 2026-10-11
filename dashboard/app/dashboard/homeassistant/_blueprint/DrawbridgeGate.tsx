"use client";

/**
 * The house's one way out, drawn on the right edge of the sheet.
 *
 * Everything that leaves the LAN leaves through here, so the traffic layer
 * gives it a wall rather than a node: a hairline bar with rungs, a raised
 * bridge, and the three numbers that say whether the gate is actually doing
 * anything. Monitor mode with enforcing off is the honest common case and it
 * is printed, not hidden.
 */

import type { TrafficVisibility } from "../../../_lib/services/homeService";
import { compactCount } from "./view";

const MONO = { fontFamily: "var(--font-mono, ui-monospace, monospace)" } as const;

export default function DrawbridgeGate({
  x,
  top,
  bottom,
  textRight,
  textTop,
  visibility,
}: {
  /** Sheet x of the wall. Lines arrive here. */
  x: number;
  top: number;
  bottom: number;
  /** Right edge the stats stack is anchored to. */
  textRight: number;
  textTop: number;
  visibility: TrafficVisibility | null;
}) {
  const rungs: number[] = [];
  for (let y = top + 6; y < bottom; y += 7) rungs.push(y);
  const mid = (top + bottom) / 2;

  return (
    <g>
      {/* Label block, above the wall. */}
      <text
        x={textRight}
        y={textTop}
        fontSize={6.5}
        textAnchor="end"
        fill="var(--text)"
        style={{ letterSpacing: "0.2em", textTransform: "uppercase" }}
      >
        Drawbridge
      </text>
      <line
        x1={textRight - 56}
        y1={textTop + 3.5}
        x2={textRight}
        y2={textTop + 3.5}
        stroke="var(--text2)"
        strokeWidth={0.5}
        opacity={0.7}
      />
      <g fontSize={5.5} textAnchor="end" fill="var(--text2)" style={MONO}>
        <text x={textRight} y={textTop + 11}>
          {visibility ? visibility.mode || "unknown" : "no reading"}
        </text>
        <text x={textRight} y={textTop + 18}>
          queries today {visibility ? compactCount(visibility.queries_today) : "--"}
        </text>
        <text
          x={textRight}
          y={textTop + 25}
          fill={visibility?.enforcing ? "var(--mint)" : "var(--text3)"}
        >
          enforcing {visibility ? (visibility.enforcing ? "yes" : "no") : "--"}
        </text>
      </g>

      {/* The wall. */}
      <line x1={x} y1={top} x2={x} y2={bottom} stroke="var(--text2)" strokeWidth={1.3} />
      <line
        x1={x + 3.5}
        y1={top}
        x2={x + 3.5}
        y2={bottom}
        stroke="var(--text3)"
        strokeWidth={0.6}
      />
      <g stroke="var(--text3)" strokeWidth={0.5} opacity={0.8}>
        {rungs.map((y) => (
          <line key={y} x1={x} y1={y} x2={x + 3.5} y2={y} />
        ))}
      </g>

      {/* The bridge itself, raised. */}
      <path
        d={`M ${x} ${mid - 7} L ${x - 9} ${mid - 13} M ${x} ${mid - 7} L ${x} ${mid + 7}`}
        fill="none"
        stroke="var(--text2)"
        strokeWidth={0.9}
      />
      <line
        x1={x}
        y1={mid - 7}
        x2={x - 9}
        y2={mid - 13}
        stroke="var(--text3)"
        strokeWidth={0.5}
        strokeDasharray="1 2"
      />
    </g>
  );
}
