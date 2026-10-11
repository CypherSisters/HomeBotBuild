"use client";

/**
 * One device on the sheet.
 *
 * Drawn as an instrument symbol, not an icon in a card: a 24px mdi path, a 2px
 * tier rule under it, and state carried by the drawing itself (a lit light
 * glows, a dead device is hollow and dashed, motion pulses, a safety-critical
 * device wears a dotted ring). The hit target is a transparent circle far
 * larger than the mark so the 1920x1080 touch kiosk can land on it.
 */

import { memo } from "react";
import type { HomeDevice } from "../../../_lib/services/homeService";
import { glyphFor, TIER_TONE, ON_TONE } from "./glyphs";
import {
  SCALE,
  brightness,
  climateTemp,
  isLit,
  isSafetyCritical,
  isUnavailable,
  mediaPlaying,
  motionActive,
  type LayerKey,
} from "./view";

export const HIT_RADIUS = 15;

function DeviceGlyphInner({
  device,
  gx,
  gy,
  selected,
  layer,
  onSelect,
  onDragStart,
}: {
  device: HomeDevice;
  /** Sheet position in grid units. */
  gx: number;
  gy: number;
  selected: boolean;
  layer: LayerKey;
  onSelect: (deviceId: string) => void;
  onDragStart: (deviceId: string, e: React.PointerEvent) => void;
}) {
  const d = device;
  const cx = gx * SCALE;
  const cy = gy * SCALE;
  const dead = isUnavailable(d);
  const lit = isLit(d);
  const level = brightness(d);
  const temp = climateTemp(d);
  const tone = dead ? "var(--text3)" : lit ? ON_TONE : "var(--text)";
  const showState = layer === "state";

  return (
    <g
      transform={`translate(${cx} ${cy})`}
      className="cursor-pointer"
      onPointerDown={(e) => onDragStart(d.device_id, e)}
      onClick={(e) => {
        e.stopPropagation();
        onSelect(d.device_id);
      }}
    >
      {/* Touch target. Invisible, and always the biggest thing here. */}
      <circle r={HIT_RADIUS} fill="transparent" />

      {/* No glow under the traffic layer: the lines are what is being read
          there, and a warm halo behind them just muddies the colour. */}
      {lit && layer !== "traffic" && (
        // The blur lives in the sheet stylesheet, not inline, so a drag can
        // switch it off with one class on the svg.
        <circle className="puff-bp-glow" r={13} fill={ON_TONE} opacity={0.1 + level * 0.28} />
      )}

      {selected && (
        <circle r={13} fill="none" stroke="var(--pink)" strokeWidth={1.2} />
      )}

      {motionActive(d) && (
        <circle
          className="puff-live puff-bp-pulse"
          r={12}
          fill="none"
          stroke="var(--pink)"
          strokeWidth={1}
        />
      )}

      {isSafetyCritical(d) && (
        <circle
          r={11}
          fill="none"
          stroke="var(--lav)"
          strokeWidth={1}
          strokeDasharray="1 3"
          opacity={0.9}
        />
      )}

      {dead && (
        <circle
          r={10}
          fill="none"
          stroke="var(--text3)"
          strokeWidth={0.8}
          strokeDasharray="2 2"
        />
      )}

      <g transform="translate(-7 -8) scale(0.58)" opacity={dead ? 0.45 : 1}>
        <path d={glyphFor(d.kind)} fill={tone} />
      </g>

      {/* Tier rule: where this device takes its orders from. */}
      <rect
        x={-7}
        y={7}
        width={14}
        height={2}
        fill={TIER_TONE[d.tier] ?? "var(--text3)"}
        opacity={dead ? 0.4 : 0.95}
      />

      {temp !== null && (
        <text
          x={11}
          y={2}
          fontSize={7}
          fill="var(--text2)"
          style={{ fontFamily: "var(--font-mono, ui-monospace, monospace)" }}
        >
          {temp}
        </text>
      )}

      {mediaPlaying(d) && (
        <g transform="translate(9 -9)">
          {[0, 1, 2].map((i) => (
            <rect
              key={i}
              className="puff-bp-level"
              x={i * 2.4}
              y={0}
              width={1.4}
              height={5}
              fill="var(--sky)"
              style={{ animationDelay: `${i * 0.16}s`, transformOrigin: `${i * 2.4 + 0.7}px 5px` }}
            />
          ))}
        </g>
      )}

      {showState && lit && (
        <text
          x={0}
          y={17}
          fontSize={6}
          textAnchor="middle"
          fill="var(--text3)"
          style={{ fontFamily: "var(--font-mono, ui-monospace, monospace)" }}
        >
          {Math.round(level * 100)}%
        </text>
      )}
    </g>
  );
}

export default memo(DeviceGlyphInner);
