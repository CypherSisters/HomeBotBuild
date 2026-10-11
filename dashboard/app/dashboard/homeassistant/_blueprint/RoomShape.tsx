"use client";

/**
 * A room: hairline outline, small-caps name, dimension ticks, and a warm tint
 * scaled by how much light is on inside it.
 *
 * The tint is the only fill a room ever gets. It is what makes the sheet
 * readable from across the room on the kiosk: dark plan, warm rooms are the
 * ones with lights on.
 */

import { memo } from "react";
import { mdiPencil } from "@mdi/js";
import type { HomeLayoutRoom } from "../../../_lib/services/homeService";
import { ON_TONE } from "./glyphs";
import { SCALE } from "./view";

function RoomShapeInner({
  room,
  tint,
  deviceCount,
  selected,
  editMode,
  overlapping,
  onSelect,
  onMoveStart,
  onResizeStart,
  onRenameRequest,
}: {
  room: HomeLayoutRoom;
  /** 0 to 1, summed light level in the room. */
  tint: number;
  deviceCount: number;
  selected: boolean;
  editMode: boolean;
  overlapping: boolean;
  onSelect: (areaId: string) => void;
  onMoveStart: (areaId: string, e: React.PointerEvent) => void;
  onResizeStart: (areaId: string, e: React.PointerEvent) => void;
  /** Edit mode only: the name itself was tapped. */
  onRenameRequest: (areaId: string) => void;
}) {
  const x = room.x * SCALE;
  const y = room.y * SCALE;
  const w = room.w * SCALE;
  const h = room.h * SCALE;

  // Ticks every grid unit along the top and left edges, longer every 5.
  const ticksX: number[] = [];
  for (let i = 1; i < room.w; i += 1) ticksX.push(i);
  const ticksY: number[] = [];
  for (let i = 1; i < room.h; i += 1) ticksY.push(i);

  // Rough advance width for the small-caps label. There is no way to measure
  // SVG text before it is laid out, and the underline only has to look like it
  // belongs to the word, so an estimate is the right amount of machinery.
  const nameW = Math.min(w - 14, Math.max(16, room.name.length * 4.3));

  return (
    <g
      onPointerDown={(e) => {
        if (editMode) onMoveStart(room.area_id, e);
      }}
      onClick={(e) => {
        e.stopPropagation();
        onSelect(room.area_id);
      }}
      className={editMode ? "cursor-move" : "cursor-pointer"}
    >
      <rect
        x={x}
        y={y}
        width={w}
        height={h}
        fill={overlapping ? "var(--puff-red)" : ON_TONE}
        opacity={overlapping ? 0.1 : tint * 0.13}
      />
      <rect
        x={x}
        y={y}
        width={w}
        height={h}
        fill="none"
        stroke={selected ? "var(--pink)" : "var(--text2)"}
        strokeWidth={selected ? 1.4 : 0.9}
        opacity={selected ? 1 : 0.75}
      />

      {/* Dimension ticks: a drafting sheet counts its feet. */}
      <g stroke="var(--text3)" strokeWidth={0.4} opacity={0.75}>
        {ticksX.map((i) => (
          <line
            key={`x${i}`}
            x1={x + i * SCALE}
            y1={y}
            x2={x + i * SCALE}
            y2={y + (i % 5 === 0 ? 5 : 2.5)}
          />
        ))}
        {ticksY.map((i) => (
          <line
            key={`y${i}`}
            x1={x}
            y1={y + i * SCALE}
            x2={x + (i % 5 === 0 ? 5 : 2.5)}
            y2={y + i * SCALE}
          />
        ))}
      </g>

      <text
        x={x + 5}
        y={y + 12}
        fontSize={7.5}
        fill="var(--text)"
        style={{
          fontVariant: "small-caps",
          letterSpacing: "0.14em",
          textTransform: "lowercase",
        }}
      >
        {room.name}
      </text>

      {/* In edit mode the name is the rename control: underlined, with a
          pencil, and a hit target far bigger than the letters. The owner
          tapped the name and expected this, which is the whole argument. It
          swallows its own pointerdown so tapping it never starts a room drag. */}
      {editMode && (
        <g
          className="cursor-text"
          onPointerDown={(e) => {
            e.stopPropagation();
          }}
          onClick={(e) => {
            e.stopPropagation();
            onRenameRequest(room.area_id);
          }}
        >
          <line
            x1={x + 5}
            y1={y + 14.5}
            x2={x + 5 + nameW}
            y2={y + 14.5}
            stroke="var(--pink)"
            strokeWidth={0.6}
            opacity={0.85}
          />
          <g transform={`translate(${x + 7 + nameW} ${y + 4}) scale(0.4)`} opacity={0.9}>
            <path d={mdiPencil} fill="var(--pink)" />
          </g>
          <rect
            x={x + 1}
            y={y + 1}
            width={nameW + 18}
            height={19}
            fill="transparent"
          />
        </g>
      )}

      <text
        x={x + w - 4}
        y={y + h - 4}
        fontSize={6}
        textAnchor="end"
        fill="var(--text3)"
        style={{ fontFamily: "var(--font-mono, ui-monospace, monospace)" }}
      >
        {room.w}x{room.h} · {deviceCount}
      </text>

      {editMode && (
        <rect
          x={x + w - 9}
          y={y + h - 9}
          width={9}
          height={9}
          fill="var(--card)"
          stroke="var(--pink)"
          strokeWidth={0.8}
          className="cursor-se-resize"
          onPointerDown={(e) => {
            e.stopPropagation();
            onResizeStart(room.area_id, e);
          }}
        />
      )}
    </g>
  );
}

export default memo(RoomShapeInner);
