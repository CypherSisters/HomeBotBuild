"use client";

/**
 * The tray: everything Home Assistant knows about but cannot place.
 *
 * Most of this house's devices have no HA area, and the HA user stays
 * non-admin on purpose, so the owner places them here and PuffPAI stores it.
 * Drag a chip onto a room to file it; a click with no drag just opens it in
 * the inspector.
 */

import { glyphFor, TIER_TONE } from "./glyphs";
import type { HomeDevice } from "../../../_lib/services/homeService";
import { isUnavailable } from "./view";

export default function UnplacedTray({
  devices,
  selectedId,
  dragId,
  onSelect,
  onDragStart,
}: {
  devices: HomeDevice[];
  selectedId: string | null;
  dragId: string | null;
  onSelect: (deviceId: string) => void;
  onDragStart: (deviceId: string, e: React.PointerEvent) => void;
}) {
  if (devices.length === 0) return null;

  return (
    <div
      className="flex items-center gap-2 overflow-x-auto border-t px-3 py-2"
      style={{ borderColor: "var(--line)", background: "var(--card2)" }}
    >
      <span
        className="shrink-0 text-[10px] font-semibold tracking-[0.16em] uppercase"
        style={{ color: "var(--text3)" }}
      >
        Unplaced {devices.length}
      </span>
      {devices.map((d) => (
        <button
          key={d.device_id}
          type="button"
          onPointerDown={(e) => onDragStart(d.device_id, e)}
          onClick={() => onSelect(d.device_id)}
          className="flex shrink-0 touch-none items-center gap-1.5 rounded-sm border px-2 py-1 text-left"
          style={{
            borderColor: selectedId === d.device_id ? "var(--pink)" : "var(--line)",
            background: "var(--chip)",
            opacity: dragId === d.device_id ? 0.4 : 1,
          }}
          title={d.primary_entity_id ?? d.device_id}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
            <path
              d={glyphFor(d.kind)}
              fill={isUnavailable(d) ? "var(--text3)" : "var(--text2)"}
            />
          </svg>
          <span className="max-w-[10rem] truncate text-[11px]" style={{ color: "var(--text)" }}>
            {d.name}
          </span>
          <span
            className="h-0.5 w-3 shrink-0"
            style={{ background: TIER_TONE[d.tier] ?? "var(--text3)" }}
          />
        </button>
      ))}
    </div>
  );
}
