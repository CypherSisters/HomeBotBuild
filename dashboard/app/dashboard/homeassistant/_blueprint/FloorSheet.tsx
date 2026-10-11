"use client";

/**
 * The sheet itself: one inline SVG, drawn like a drafting plan.
 *
 * Fine grid at one unit with a heavier line every five, hairline rooms, device
 * glyphs, and a title block in the corner carrying house, floor, count, as-of
 * and the tier legend. The viewBox auto-fits whatever rooms exist, so a two
 * room flat and a twelve room house both fill the frame.
 *
 * All pointer handling is forwarded up to Blueprint, which owns the drag
 * state. This component draws and reports; it decides nothing.
 */

import type { RefObject } from "react";
import type { HomeLayoutRoom, HomeTraffic } from "../../../_lib/services/homeService";
import DeviceGlyph from "./DeviceGlyph";
import RoomShape from "./RoomShape";
import TrafficOverlay, { TRAFFIC_GUTTER, TrafficMarks } from "./TrafficOverlay";
import { TIER_LABEL, TIER_LEGEND, TIER_TONE } from "./glyphs";
import type { Bounds } from "./seedLayout";
import { SCALE, type LayerKey, type Placed, type Selection } from "./view";

const SHEET_CSS = `
.puff-bp-pulse { animation: puffBpPulse 1.8s ease-in-out infinite;
  animation-play-state: var(--puff-live-play, running); }
@keyframes puffBpPulse { 0%,100% { opacity: .15 } 50% { opacity: .9 } }
.puff-bp-level { animation: puffBpLevel 1.1s ease-in-out infinite;
  animation-play-state: var(--puff-ambient-play, running); }
@keyframes puffBpLevel { 0%,100% { transform: scaleY(.35) } 50% { transform: scaleY(1) } }
.puff-bp-glow { filter: blur(2px) }
/* Traffic: dashes travel along the path in its drawn direction, device to
   gate, which is decreasing dashoffset. Ambient, so the idle guard stops it. */
.puff-bp-flow { animation: puffBpFlow 3.2s linear infinite;
  animation-play-state: var(--puff-ambient-play, running); }
@keyframes puffBpFlow { from { stroke-dashoffset: 16 } to { stroke-dashoffset: 0 } }
@media (prefers-reduced-motion: reduce) {
  .puff-bp-pulse, .puff-bp-level, .puff-bp-flow { animation: none }
}
/* While a drag is live the sheet drops everything expensive: the blur behind
   every lit light, the two animations, and any transition. The kiosk is a
   power-capped Jetson and this is the difference between a tile that follows
   the finger and one that lags it. */
.is-dragging .puff-bp-glow { filter: none }
.is-dragging .puff-bp-pulse, .is-dragging .puff-bp-level,
.is-dragging .puff-bp-flow { animation: none }
.is-dragging * { transition: none !important }
`;

export default function FloorSheet({
  svgRef,
  rooms,
  placed,
  bounds,
  selection,
  layer,
  editMode,
  overlapping,
  roomTint,
  roomCounts,
  dragging,
  houseName,
  floorName,
  deviceCount,
  asOf,
  hiddenNote,
  traffic,
  onSelectRoom,
  onSelectDevice,
  onClearSelection,
  onRoomMoveStart,
  onRoomResizeStart,
  onDeviceDragStart,
  onRenameRoom,
  onPointerMove,
  onPointerUp,
}: {
  svgRef: RefObject<SVGSVGElement | null>;
  rooms: HomeLayoutRoom[];
  placed: Placed[];
  bounds: Bounds;
  selection: Selection;
  layer: LayerKey;
  editMode: boolean;
  overlapping: Set<string>;
  roomTint: Record<string, number>;
  roomCounts: Record<string, number>;
  /** A room or glyph is being dragged right now. */
  dragging: boolean;
  houseName: string;
  floorName: string;
  deviceCount: number;
  asOf: string;
  /** `18 virtual` and the like. Empty when the resolver hid nothing. */
  hiddenNote: string;
  traffic: HomeTraffic | null;
  onSelectRoom: (areaId: string) => void;
  onSelectDevice: (deviceId: string) => void;
  onClearSelection: () => void;
  onRoomMoveStart: (areaId: string, e: React.PointerEvent) => void;
  onRoomResizeStart: (areaId: string, e: React.PointerEvent) => void;
  onDeviceDragStart: (deviceId: string, e: React.PointerEvent) => void;
  /** Edit mode only: the room name was tapped and wants the rename field. */
  onRenameRoom: (areaId: string) => void;
  onPointerMove: (e: React.PointerEvent) => void;
  onPointerUp: (e: React.PointerEvent) => void;
}) {
  const trafficLayer = layer === "traffic";
  // The traffic layer claims a right-hand gutter for the Drawbridge, so the
  // gate never sits on top of a room.
  const gutter = trafficLayer ? TRAFFIC_GUTTER : 0;
  const vx = bounds.x * SCALE;
  const vy = bounds.y * SCALE;
  const vw = bounds.w * SCALE + gutter;
  const vh = bounds.h * SCALE;

  // Title block sits inside the bottom right padding of the plan, in sheet
  // units. The gutter is the gate's, so the block stays left of it.
  const tbW = 116;
  const tbH = 54 + (hiddenNote ? 9 : 0);
  const tbX = vx + vw - gutter - tbW - 6;
  const tbY = vy + vh - tbH - 6;

  return (
    <svg
      ref={svgRef}
      viewBox={`${vx} ${vy} ${vw} ${vh}`}
      preserveAspectRatio="xMidYMid meet"
      className={`block h-full w-full touch-none select-none${dragging ? " is-dragging" : ""}`}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onClick={onClearSelection}
    >
      <style>{SHEET_CSS}</style>
      <defs>
        <pattern id="puffBpMinor" width={SCALE} height={SCALE} patternUnits="userSpaceOnUse">
          <path
            d={`M ${SCALE} 0 L 0 0 0 ${SCALE}`}
            fill="none"
            stroke="var(--text3)"
            strokeWidth={0.3}
            opacity={0.35}
          />
        </pattern>
        <pattern
          id="puffBpMajor"
          width={SCALE * 5}
          height={SCALE * 5}
          patternUnits="userSpaceOnUse"
        >
          <rect width={SCALE * 5} height={SCALE * 5} fill="url(#puffBpMinor)" />
          <path
            d={`M ${SCALE * 5} 0 L 0 0 0 ${SCALE * 5}`}
            fill="none"
            stroke="var(--text3)"
            strokeWidth={0.6}
            opacity={0.55}
          />
        </pattern>
      </defs>

      <rect x={vx} y={vy} width={vw} height={vh} fill="url(#puffBpMajor)" />

      {/* Under the traffic layer the plan steps back so the lines read. */}
      <g opacity={trafficLayer ? 0.55 : 1}>
        {rooms.map((room) => (
          <RoomShape
            key={room.area_id}
            room={room}
            tint={trafficLayer ? 0 : roomTint[room.area_id] ?? 0}
            deviceCount={roomCounts[room.area_id] ?? 0}
            selected={selection?.type === "room" && selection.id === room.area_id}
            editMode={editMode}
            overlapping={overlapping.has(room.area_id)}
            onSelect={onSelectRoom}
            onMoveStart={onRoomMoveStart}
            onResizeStart={onRoomResizeStart}
            onRenameRequest={onRenameRoom}
          />
        ))}
      </g>

      {trafficLayer && (
        <TrafficOverlay
          placed={placed}
          bounds={bounds}
          traffic={traffic}
          selection={selection}
        />
      )}

      {placed.map((p) => (
        // Primitives, not the Placed object: `placed` is rebuilt on every
        // drag frame, and a fresh object for each device would defeat the memo
        // on every glyph in the house rather than the ones that moved.
        <DeviceGlyph
          key={p.device.device_id}
          device={p.device}
          gx={p.gx}
          gy={p.gy}
          selected={selection?.type === "device" && selection.id === p.device.device_id}
          layer={layer}
          onSelect={onSelectDevice}
          onDragStart={onDeviceDragStart}
        />
      ))}

      {trafficLayer && <TrafficMarks placed={placed} traffic={traffic} />}

      {trafficLayer && (
        <text
          x={vx + 6}
          y={vy + vh - 6}
          fontSize={5.5}
          fill="var(--text3)"
          style={{ fontFamily: "var(--font-mono, ui-monospace, monospace)" }}
        >
          line = lookups {traffic ? traffic.hours : 24}h · dot = telemetry/ads/tracking seen
        </text>
      )}

      {/* Title block. */}
      <g>
        <rect
          x={tbX}
          y={tbY}
          width={tbW}
          height={tbH}
          fill="var(--card)"
          stroke="var(--text2)"
          strokeWidth={0.7}
          opacity={0.96}
        />
        <line
          x1={tbX}
          y1={tbY + 13}
          x2={tbX + tbW}
          y2={tbY + 13}
          stroke="var(--text2)"
          strokeWidth={0.5}
          opacity={0.6}
        />
        <text
          x={tbX + 5}
          y={tbY + 9.5}
          fontSize={7}
          fill="var(--text)"
          style={{ letterSpacing: "0.18em", textTransform: "uppercase" }}
        >
          {houseName}
        </text>
        <g
          fontSize={6}
          fill="var(--text2)"
          style={{ fontFamily: "var(--font-mono, ui-monospace, monospace)" }}
        >
          <text x={tbX + 5} y={tbY + 23}>
            FLOOR {floorName}
          </text>
          <text x={tbX + 5} y={tbY + 32}>
            DEVICES {deviceCount}
          </text>
          <text x={tbX + 5} y={tbY + 41}>
            AS OF {asOf}
          </text>
          {hiddenNote && (
            <text x={tbX + 5} y={tbY + 50} fill="var(--text3)">
              HIDDEN {hiddenNote}
            </text>
          )}
          {TIER_LEGEND.map((tier, i) => (
            <g key={tier} transform={`translate(${tbX + 62} ${tbY + 19 + i * 9})`}>
              <rect x={0} y={0} width={10} height={2} fill={TIER_TONE[tier]} />
              <text x={14} y={3}>
                {TIER_LABEL[tier]}
              </text>
            </g>
          ))}
        </g>
      </g>
    </svg>
  );
}
