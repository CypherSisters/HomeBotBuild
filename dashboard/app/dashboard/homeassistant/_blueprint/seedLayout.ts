/**
 * The schematic a house gets before anyone has drawn one.
 *
 * Pure and deterministic: the same areas and devices always produce the same
 * plan, so a reload does not reshuffle the owner's house. Nothing here is a
 * guess at the real building. It is a drafting stand-in the owner drags into
 * shape once, after which the saved layout wins and this never runs again.
 *
 * Units are the layout contract's grid units (about a foot, integers, 0 to 200).
 */

import type {
  HomeArea,
  HomeDevice,
  HomeFloor,
  HomeLayout,
  HomeLayoutRoom,
} from "../../../_lib/services/homeService";

export const GRID_MIN = 0;
export const GRID_MAX = 200;

/** Rooms wrap at this width, which keeps a 6 room house roughly square. */
const ROW_WIDTH = 34;
const GAP = 1;

export const DEFAULT_FLOOR: HomeFloor = {
  floor_id: "ground",
  name: "Ground",
  level: 0,
};

function clampInt(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, Math.round(v)));
}

/**
 * Room size from device count: a closet with two devices should not be drawn
 * the same size as a living room with eleven. Square-ish, and bounded so one
 * busy room cannot eat the sheet.
 */
function sizeFor(deviceCount: number): { w: number; h: number } {
  const units = 44 + deviceCount * 7;
  const w = clampInt(Math.sqrt(units * 1.35), 7, 20);
  const h = clampInt(units / w, 5, 16);
  return { w, h };
}

/** Deterministic: HA gives no stable ordering guarantee, so we impose one. */
function orderAreas(areas: HomeArea[]): HomeArea[] {
  return [...areas].sort((a, b) => a.area_id.localeCompare(b.area_id));
}

/**
 * Pack one floor's rooms left to right, wrapping at ROW_WIDTH, each row as
 * tall as its tallest room. Rectangle-ish, no overlaps, no gaps to speak of.
 */
function packFloor(areas: HomeArea[], counts: Map<string, number>, floorId: string): HomeLayoutRoom[] {
  const rooms: HomeLayoutRoom[] = [];
  let x = 0;
  let y = 0;
  let rowHeight = 0;
  for (const area of areas) {
    const { w, h } = sizeFor(counts.get(area.area_id) ?? 0);
    if (x > 0 && x + w > ROW_WIDTH) {
      x = 0;
      y += rowHeight + GAP;
      rowHeight = 0;
    }
    rooms.push({
      area_id: area.area_id,
      // The layout store refuses names over 64 characters; an unsliced HA name
      // would make every later Save fail.
      name: (area.name || area.area_id).slice(0, 64),
      floor_id: floorId,
      x: clampInt(x, GRID_MIN, GRID_MAX),
      y: clampInt(y, GRID_MIN, GRID_MAX),
      w,
      h,
      custom: false,
    });
    x += w + GAP;
    rowHeight = Math.max(rowHeight, h);
  }
  return rooms;
}

/**
 * Seed a layout from what `/ha/home` reported.
 *
 * Areas with no floor are put on the first floor, because a room that belongs
 * to no floor cannot be drawn at all and silently dropping it would read as a
 * missing room rather than as missing metadata.
 */
export function seedLayout(
  areas: HomeArea[],
  devices: HomeDevice[],
  floors: HomeFloor[],
): HomeLayout {
  const useFloors = floors.length > 0 ? [...floors] : [DEFAULT_FLOOR];
  const fallbackFloor = useFloors[0].floor_id;

  const counts = new Map<string, number>();
  for (const d of devices) {
    if (!d.area_id) continue;
    counts.set(d.area_id, (counts.get(d.area_id) ?? 0) + 1);
  }

  const rooms: HomeLayoutRoom[] = [];
  for (const floor of useFloors) {
    const mine = orderAreas(
      areas.filter((a) =>
        a.floor_id ? a.floor_id === floor.floor_id : floor.floor_id === fallbackFloor,
      ),
    );
    rooms.push(...packFloor(mine, counts, floor.floor_id));
  }

  return {
    version: 1,
    floors: useFloors,
    rooms,
    placements: {},
    seeded: true,
  };
}

/**
 * Where a device sits when nobody has placed it: a tidy grid inside its room,
 * ordered by device id so the pack never jumps between polls.
 */
export function autoPositions(deviceIds: string[]): Record<string, { x: number; y: number }> {
  const ids = [...deviceIds].sort();
  const cols = Math.max(1, Math.ceil(Math.sqrt(ids.length)));
  const rows = Math.max(1, Math.ceil(ids.length / cols));
  const out: Record<string, { x: number; y: number }> = {};
  ids.forEach((id, i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    out[id] = { x: (col + 0.5) / cols, y: (row + 0.5) / rows };
  });
  return out;
}

export interface Bounds {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** viewBox in grid units, padded, with a floor of 20x14 so one room is not huge. */
export function fitBounds(rooms: HomeLayoutRoom[], pad = 3): Bounds {
  if (rooms.length === 0) return { x: -pad, y: -pad, w: 20, h: 14 };
  const minX = Math.min(...rooms.map((r) => r.x));
  const minY = Math.min(...rooms.map((r) => r.y));
  const maxX = Math.max(...rooms.map((r) => r.x + r.w));
  const maxY = Math.max(...rooms.map((r) => r.y + r.h));
  return {
    x: minX - pad,
    y: minY - pad,
    w: Math.max(20, maxX - minX + pad * 2),
    h: Math.max(14, maxY - minY + pad * 2),
  };
}

export function roomsOverlap(a: HomeLayoutRoom, b: HomeLayoutRoom): boolean {
  return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
}

/** Area ids of every room sharing space with another on the same floor. */
export function overlappingRooms(rooms: HomeLayoutRoom[]): Set<string> {
  const hits = new Set<string>();
  for (let i = 0; i < rooms.length; i += 1) {
    for (let j = i + 1; j < rooms.length; j += 1) {
      if (rooms[i].floor_id !== rooms[j].floor_id) continue;
      if (roomsOverlap(rooms[i], rooms[j])) {
        hits.add(rooms[i].area_id);
        hits.add(rooms[j].area_id);
      }
    }
  }
  return hits;
}

export { clampInt };
