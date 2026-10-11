"use client";

/**
 * The traffic layer: which devices are phoning home, drawn on the plan.
 *
 * A line means lookups left that device in the window. Its weight is how many,
 * its colour is the device's dominant verdict, and its direction is always
 * device to gate. Nothing here is per domain: AdGuard reports allowed,
 * monitored and blocked per client only, so a domain gets a category and never
 * a verdict, and the drawing says exactly that and no more.
 *
 * Lines gather on one vertical bus a short way in front of the Drawbridge
 * rather than fanning at it, because twenty straight lines to one point is a
 * starburst nobody can read. Each device keeps its own entry height on the
 * wall so its blocked marker and its line stay findable.
 *
 * Every string on this layer came off the network. A device can call itself
 * anything, so all of it is rendered as text children and title attributes,
 * never markup, never a link, never a path.
 */

import { useState } from "react";
import type {
  HomeTraffic,
  LanOnlyRow,
  TrafficRow,
} from "../../../_lib/services/homeService";
import type { Bounds } from "./seedLayout";
import DrawbridgeGate from "./DrawbridgeGate";
import {
  SCALE,
  VERDICT_TONE,
  compactCount,
  dominantVerdict,
  hasLoudDomain,
  isLoudCategory,
  middleTruncate,
  shortTime,
  type Placed,
  type Selection,
} from "./view";

/** Sheet units of right-hand margin the layer claims for the gate. */
export const TRAFFIC_GUTTER = 76;

interface Geometry {
  busX: number;
  gateX: number;
  gateTop: number;
  gateBottom: number;
  textRight: number;
  textTop: number;
}

export function gateGeometry(bounds: Bounds): Geometry {
  const vx = bounds.x * SCALE;
  const vy = bounds.y * SCALE;
  const vw = bounds.w * SCALE + TRAFFIC_GUTTER;
  const vh = bounds.h * SCALE;
  const right = vx + vw;
  return {
    busX: right - 58,
    gateX: right - 18,
    gateTop: vy + 44,
    // Clear of the title block, which lives in the bottom right padding.
    gateBottom: vy + vh - 66,
    textRight: right - 4,
    textTop: vy + 12,
  };
}

/** Orthogonal with eased corners: glyph, out, along the bus, into the wall. */
function routePath(cx: number, cy: number, busX: number, entryY: number, gateX: number): string {
  const r = 5;
  const startX = cx + 10;
  const dir = entryY >= cy ? 1 : -1;
  if (Math.abs(entryY - cy) < r * 2 + 1 || startX > busX - r * 2) {
    return `M ${startX} ${cy} L ${busX} ${cy} L ${busX} ${entryY} L ${gateX} ${entryY}`;
  }
  return (
    `M ${startX} ${cy} H ${busX - r} Q ${busX} ${cy} ${busX} ${cy + dir * r} ` +
    `V ${entryY - dir * r} Q ${busX} ${entryY} ${busX + r} ${entryY} H ${gateX}`
  );
}

/** 0.6 at one lookup, 3 at a thousand. Anything louder just stays at 3. */
function weightFor(total: number): number {
  const w = 0.6 + Math.log10(Math.max(1, total)) * 0.8;
  return Math.max(0.6, Math.min(3, Number(w.toFixed(2))));
}

interface Line {
  id: string;
  row: TrafficRow;
  cx: number;
  cy: number;
  entryY: number;
}

function linesFor(placed: Placed[], traffic: HomeTraffic | null, geo: Geometry): Line[] {
  if (!traffic || !traffic.visibility.seeing_traffic) return [];
  const talking = placed
    .map((p) => ({ p, row: traffic.devices[p.device.device_id] }))
    .filter((x): x is { p: Placed; row: TrafficRow } => !!x.row && x.row.total > 0)
    .sort((a, b) => a.p.gy - b.p.gy || a.p.gx - b.p.gx);
  const span = geo.gateBottom - geo.gateTop;
  return talking.map((x, i) => ({
    id: x.p.device.device_id,
    row: x.row,
    cx: x.p.gx * SCALE,
    cy: x.p.gy * SCALE,
    entryY: geo.gateTop + (span * (i + 1)) / (talking.length + 1),
  }));
}

/* ── the lines and the gate, drawn under the glyphs ──────────────────── */

export default function TrafficOverlay({
  placed,
  bounds,
  traffic,
  selection,
}: {
  placed: Placed[];
  bounds: Bounds;
  traffic: HomeTraffic | null;
  selection: Selection;
}) {
  const geo = gateGeometry(bounds);
  const lines = linesFor(placed, traffic, geo);
  const selectedId = selection?.type === "device" ? selection.id : null;

  return (
    <g>
      <DrawbridgeGate
        x={geo.gateX}
        top={geo.gateTop}
        bottom={geo.gateBottom}
        textRight={geo.textRight}
        textTop={geo.textTop}
        visibility={traffic?.visibility ?? null}
      />

      {lines.map((l) => {
        const tone = VERDICT_TONE[dominantVerdict(l.row)];
        const w = weightFor(l.row.total);
        const d = routePath(l.cx, l.cy, geo.busX, l.entryY, geo.gateX);
        const hot = selectedId === l.id;
        return (
          <g key={l.id}>
            <path
              d={d}
              fill="none"
              stroke={tone}
              strokeWidth={w}
              strokeLinecap="round"
              opacity={hot ? 0.85 : 0.38}
            />
            <path
              className="puff-bp-flow"
              d={d}
              fill="none"
              stroke={tone}
              strokeWidth={w}
              strokeLinecap="round"
              strokeDasharray="3 13"
              opacity={hot ? 1 : 0.8}
            />
            {l.row.blocked > 0 && (
              <line
                x1={geo.busX + 7}
                y1={l.entryY}
                x2={geo.gateX - 1}
                y2={l.entryY}
                stroke={VERDICT_TONE.blocked}
                strokeWidth={Math.max(1.2, w)}
                strokeDasharray="1.6 1.6"
              />
            )}
          </g>
        );
      })}
    </g>
  );
}

/* ── per-glyph marks, drawn over the glyphs ──────────────────────────── */

export function TrafficMarks({
  placed,
  traffic,
}: {
  placed: Placed[];
  traffic: HomeTraffic | null;
}) {
  if (!traffic) return null;
  const seeing = traffic.visibility.seeing_traffic;
  return (
    <g pointerEvents="none">
      {placed.map((p) => {
        const row = traffic.devices[p.device.device_id];
        if (!row) return null;
        const cx = p.gx * SCALE;
        const cy = p.gy * SCALE;
        const talking = seeing && row.total > 0;
        return (
          <g key={p.device.device_id} transform={`translate(${cx} ${cy})`}>
            {talking ? (
              <text
                x={0}
                y={-11}
                fontSize={6}
                textAnchor="middle"
                fill="var(--text2)"
                style={{ fontFamily: "var(--font-mono, ui-monospace, monospace)" }}
              >
                {compactCount(row.total)}
              </text>
            ) : (
              // Has a LAN address and never uses it. The tick says linked, and
              // deliberately says nothing more than that.
              <circle
                cx={9}
                cy={-9}
                r={2}
                fill="none"
                stroke="var(--text3)"
                strokeWidth={0.7}
              />
            )}
            {talking && hasLoudDomain(row) && (
              <circle cx={10} cy={-9} r={1.5} fill="var(--magenta)" />
            )}
          </g>
        );
      })}
    </g>
  );
}

/* ── LAN-only strip ──────────────────────────────────────────────────── */

const VISIBLE_LAN_ROWS = 12;

function Bar({ row }: { row: TrafficRow }) {
  const total = Math.max(1, row.total);
  const seg = (n: number, tone: string) =>
    n > 0 ? <span style={{ width: `${(n / total) * 100}%`, background: tone }} /> : null;
  return (
    <span className="flex h-[3px] w-14 overflow-hidden rounded-[1px]" style={{ background: "var(--chip)" }}>
      {seg(row.allowed, VERDICT_TONE.allowed)}
      {seg(row.monitored, VERDICT_TONE.monitored)}
      {seg(row.blocked, VERDICT_TONE.blocked)}
    </span>
  );
}

export function LanOnlyStrip({
  rows,
  selectedKey,
  onSelect,
}: {
  rows: LanOnlyRow[];
  selectedKey: string | null;
  onSelect: (key: string) => void;
}) {
  const sorted = [...rows].sort((a, b) => {
    const rank = (r: LanOnlyRow) => (r.is_gateway ? 0 : r.is_self ? 1 : 2);
    return rank(a) - rank(b) || b.total - a.total;
  });
  const [open, setOpen] = useState(false);
  if (sorted.length === 0) return null;
  const shown = open ? sorted : sorted.slice(0, VISIBLE_LAN_ROWS);
  const rest = sorted.length - shown.length;
  return (
    <div className="border-t px-3 py-2" style={{ borderColor: "var(--line)" }}>
      <div
        className="text-[10px] font-semibold tracking-[0.16em] uppercase"
        style={{ color: "var(--text3)" }}
      >
        On the LAN, not in the house
      </div>
      <div className="mt-1.5 space-y-px">
        {shown.map((r) => {
          const tag = r.is_gateway ? "GATEWAY" : r.is_self ? "THIS BOX" : "";
          const active = selectedKey === r.key;
          return (
            <button
              key={r.key}
              type="button"
              onClick={() => onSelect(r.key)}
              className="flex w-full items-center gap-2 px-1 py-1 text-left font-mono text-[11px]"
              style={{ background: active ? "var(--chip)" : "transparent", color: "var(--text2)" }}
            >
              <span
                className="min-w-0 flex-1 truncate"
                style={{ color: "var(--text)" }}
                title={r.display_name}
              >
                {r.display_name}
              </span>
              {tag && (
                <span
                  className="shrink-0 rounded-sm px-1 text-[9px] tracking-wider"
                  style={{ background: "var(--chip)", color: "var(--lav)" }}
                >
                  {tag}
                </span>
              )}
              <span className="hidden w-20 shrink-0 truncate sm:block" title={r.category}>
                {r.category}
              </span>
              <span
                className="hidden w-24 shrink-0 truncate md:block"
                style={{ color: "var(--text3)" }}
                title={r.manufacturer}
              >
                {r.manufacturer || "unknown"}
              </span>
              <span className="w-28 shrink-0 truncate" style={{ color: "var(--text3)" }} title={r.ip}>
                {r.ip}
              </span>
              <span className="w-12 shrink-0 text-right" style={{ color: "var(--text)" }}>
                {compactCount(r.total)}
              </span>
              <Bar row={r} />
            </button>
          );
        })}
      </div>
      {rest > 0 && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="mt-1 text-[10px] font-semibold tracking-[0.16em] uppercase"
          style={{ color: "var(--text3)" }}
        >
          +{rest} more
        </button>
      )}
    </div>
  );
}

/* ── traffic detail, reused by the inspector ─────────────────────────── */

export function TrafficDetail({ row }: { row: TrafficRow }) {
  return (
    <div className="space-y-1.5">
      <div className="font-mono text-[11px]" style={{ color: "var(--text2)" }}>
        <span style={{ color: VERDICT_TONE.allowed }}>allowed {row.allowed}</span>
        {" · "}
        <span style={{ color: VERDICT_TONE.monitored }}>monitored {row.monitored}</span>
        {" · "}
        <span style={{ color: VERDICT_TONE.blocked }}>blocked {row.blocked}</span>
      </div>
      <div className="font-mono text-[10px]" style={{ color: "var(--text3)" }}>
        {row.ip} · last seen {shortTime(row.last_seen)}
      </div>
      {row.top_domains.length > 0 && (
        <div className="space-y-px pt-0.5">
          {row.top_domains.map((d) => (
            <div key={d.domain} className="flex items-baseline gap-2 font-mono text-[11px]">
              <span className="w-10 shrink-0 text-right" style={{ color: "var(--text)" }}>
                {compactCount(d.count)}
              </span>
              <span className="min-w-0 flex-1 truncate" style={{ color: "var(--text2)" }} title={d.domain}>
                {middleTruncate(d.domain, 30)}
              </span>
              <span
                className="shrink-0 rounded-sm px-1 text-[9px] tracking-wider uppercase"
                style={{
                  background: "var(--chip)",
                  color: isLoudCategory(d.category) ? "var(--magenta)" : "var(--text3)",
                }}
              >
                {d.category}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
