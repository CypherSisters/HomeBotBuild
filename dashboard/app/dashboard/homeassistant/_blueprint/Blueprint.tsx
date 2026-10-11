"use client";

/**
 * Blueprint: the house as a drawing you can command.
 *
 * Owns everything stateful so the SVG pieces stay dumb: the two fetches, the
 * 5 second poll (paused while the tab is hidden, because a wall kiosk left on
 * a background tab would otherwise poll the bridge all night), the layout
 * draft during edit, drag state, optimistic control, and the ticket flow for
 * the safety-critical domains.
 *
 * Two rules worth naming:
 *   - Layout is PuffPAI's, never HA's. Placing a device writes `placements`
 *     here and nothing at all in Home Assistant, whose user stays non-admin.
 *   - "Lights off" is one call per light entity, in sequence. The bridge
 *     refuses area-wide and untargeted calls by design, and routing around
 *     that with a single broad call is exactly what that refusal is for.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  EMPTY_LAYOUT,
  HomeUnavailableError,
  liveDriver,
  type ControlIntent,
  type HomeDevice,
  type HomeDriver,
  type HomeFloor,
  type HomeLayout,
  type HomeLayoutRoom,
  type HomePayload,
  type LanOnlyRow,
  type TrafficResult,
  type TrafficRow,
} from "../../../_lib/services/homeService";
import type { HaTicketProposal } from "../../../_lib/services/haConfirmService";
import EditToolbar from "./EditToolbar";
import FloorSheet from "./FloorSheet";
import Inspector, { ConfirmSheet } from "./Inspector";
import { LanOnlyStrip } from "./TrafficOverlay";
import UnplacedTray from "./UnplacedTray";
import {
  DEFAULT_FLOOR,
  autoPositions,
  clampInt,
  fitBounds,
  overlappingRooms,
  seedLayout,
} from "./seedLayout";
import {
  SCALE,
  brightness,
  litLightEntities,
  primaryEntity,
  shortTime,
  type LayerKey,
  type Placed,
  type Selection,
} from "./view";

const POLL_MS = 5_000;
/**
 * Traffic is a DNS log rollup, not live state. Thirty seconds is already
 * faster than the numbers move, and it only runs while the layer is up.
 */
const TRAFFIC_POLL_MS = 30_000;
const DRAG_SLOP = 4;

type Drag =
  | { kind: "room-move"; areaId: string; dx: number; dy: number }
  | { kind: "room-resize"; areaId: string }
  | { kind: "device"; deviceId: string; moved: boolean; sx: number; sy: number }
  | null;

type Override = { state?: string; attributes?: Record<string, any> };

/** What the map is about to do, held while the confirm sheet is open. */
type Pending = { intent: ControlIntent; ticket: string; proposal: HaTicketProposal };

function useDriver(): HomeDriver | null {
  const [driver, setDriver] = useState<HomeDriver | null>(null);
  useEffect(() => {
    setDriver(liveDriver);
  }, []);
  return driver;
}

export default function Blueprint({
  onUnavailable,
}: {
  onUnavailable: (message: string) => void;
}) {
  const driver = useDriver();
  const svgRef = useRef<SVGSVGElement | null>(null);

  const [payload, setPayload] = useState<HomePayload | null>(null);
  const [layout, setLayout] = useState<HomeLayout>(EMPTY_LAYOUT);
  const [draft, setDraft] = useState<HomeLayout | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [layer, setLayer] = useState<LayerKey>("devices");
  const [floorId, setFloorId] = useState<string>("");
  const [selection, setSelection] = useState<Selection>(null);

  const [trafficHours, setTrafficHours] = useState(24);
  const [trafficResult, setTrafficResult] = useState<TrafficResult | null>(null);
  /** Bumped when a room name is tapped, to open the toolbar's rename field. */
  const [renameSeq, setRenameSeq] = useState(0);

  const [overrides, setOverrides] = useState<Record<string, Override>>({});
  const [busy, setBusy] = useState(false);
  const [ctlError, setCtlError] = useState("");
  const [ctlNote, setCtlNote] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);

  const [drag, setDrag] = useState<Drag>(null);
  const [trayDrag, setTrayDrag] = useState<{ deviceId: string; x: number; y: number; moved: boolean } | null>(null);
  const [saving, setSaving] = useState(false);

  const editMode = draft !== null;
  const working = draft ?? layout;

  /**
   * The layout any write must be based on.
   *
   * State alone is a beat behind: a pointermove, and a toolbar Save that has to
   * commit an open name field first, both need the value produced by the edit
   * immediately before them, and `draft` read from a render closure is still
   * the old one. Every mutation goes through `mutate`, which assigns the ref
   * first and sets state second, so the ref is the truth and `saveEdit` PUTs it.
   */
  const draftRef = useRef<HomeLayout | null>(null);
  const layoutRef = useRef<HomeLayout>(layout);

  const applyLayout = useCallback((next: HomeLayout) => {
    layoutRef.current = next;
    setLayout(next);
  }, []);

  const updateDraft = useCallback((fn: (l: HomeLayout) => HomeLayout) => {
    const cur = draftRef.current;
    if (!cur) return;
    const next = fn(cur);
    draftRef.current = next;
    setDraft(next);
  }, []);

  const mutate = useCallback(
    (fn: (l: HomeLayout) => HomeLayout) => {
      if (draftRef.current) updateDraft(fn);
      else applyLayout(fn(layoutRef.current));
    },
    [updateDraft, applyLayout],
  );

  /* ── data ──────────────────────────────────────────────────────────── */

  const load = useCallback(
    async (withLayout: boolean) => {
      if (!driver) return;
      try {
        const home = await driver.getHome();
        setPayload(home);
        setOverrides({});
        setError("");
        if (withLayout) {
          const saved = await driver.getLayout().catch(() => ({ ...EMPTY_LAYOUT }));
          const next =
            saved.rooms.length > 0
              ? saved
              : seedLayout(home.areas, home.devices, home.floors);
          // Seeded or saved, this is what the next PUT carries: a fresh house
          // has never been written, so the first auto-save must send the seed.
          applyLayout(next);
        }
      } catch (err: any) {
        if (err instanceof HomeUnavailableError) {
          onUnavailable("Blueprint needs the home map service. Showing lists.");
          return;
        }
        setError(err?.message || "Could not read the house.");
      } finally {
        setLoading(false);
      }
    },
    [driver, onUnavailable, applyLayout],
  );

  useEffect(() => {
    if (!driver) return;
    load(true);
  }, [driver, load]);

  useEffect(() => {
    if (!driver) return;
    const tick = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      if (editMode || drag || pending) return;
      load(false);
    };
    const id = setInterval(tick, POLL_MS);
    const onVisible = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [driver, load, editMode, drag, pending]);

  /**
   * Traffic has its own, much slower poll, and only while its layer is up.
   * The last reading is kept when the layer changes, because the inspector
   * shows a device's traffic whichever layer you found it on.
   */
  useEffect(() => {
    if (!driver || layer !== "traffic") return;
    let live = true;
    let inFlight = false;
    let lastAt = 0;
    const tick = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      // One request at a time, and never two within 5 s: a slow box or a
      // burst of tab focus events must not stack reads on the bridge.
      const now = Date.now();
      if (inFlight || now - lastAt < 5_000) return;
      inFlight = true;
      lastAt = now;
      driver
        .getTraffic(trafficHours)
        .then((r) => {
          if (live) setTrafficResult(r);
        })
        .finally(() => {
          inFlight = false;
        });
    };
    tick();
    const id = setInterval(tick, TRAFFIC_POLL_MS);
    const onVisible = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      live = false;
      clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [driver, layer, trafficHours]);

  // A LAN row only exists on the traffic layer, so leaving it drops it.
  useEffect(() => {
    if (layer !== "traffic") {
      setSelection((prev) => (prev?.type === "lan" ? null : prev));
    }
  }, [layer]);

  /* ── derived ───────────────────────────────────────────────────────── */

  /** Live devices with any optimistic edit laid over the polled truth. */
  const devices: HomeDevice[] = useMemo(() => {
    const base = payload?.devices ?? [];
    if (Object.keys(overrides).length === 0) return base;
    return base.map((d) => ({
      ...d,
      entities: d.entities.map((e) => {
        const o = overrides[e.entity_id];
        if (!o) return e;
        return {
          ...e,
          state: o.state ?? e.state,
          attributes: { ...e.attributes, ...(o.attributes ?? {}) },
        };
      }),
    }));
  }, [payload, overrides]);

  const floors: HomeFloor[] = useMemo(() => {
    if (working.floors.length > 0) return working.floors;
    if (payload?.floors?.length) return payload.floors;
    return [DEFAULT_FLOOR];
  }, [working.floors, payload]);

  useEffect(() => {
    if (floors.length === 0) return;
    if (!floors.some((f) => f.floor_id === floorId)) setFloorId(floors[0].floor_id);
  }, [floors, floorId]);

  const areaOf = useCallback(
    (d: HomeDevice): string | null => working.placements[d.device_id]?.area_id ?? d.area_id,
    [working.placements],
  );

  const roomsHere = useMemo(
    () => working.rooms.filter((r) => r.floor_id === floorId),
    [working.rooms, floorId],
  );

  const byArea = useMemo(() => {
    const map = new Map<string, HomeDevice[]>();
    for (const d of devices) {
      const a = areaOf(d);
      if (!a) continue;
      const list = map.get(a);
      if (list) list.push(d);
      else map.set(a, [d]);
    }
    return map;
  }, [devices, areaOf]);

  const unplaced = useMemo(() => devices.filter((d) => !areaOf(d)), [devices, areaOf]);

  const placed: Placed[] = useMemo(() => {
    const out: Placed[] = [];
    for (const room of roomsHere) {
      const list = byArea.get(room.area_id) ?? [];
      const auto = autoPositions(list.map((d) => d.device_id));
      for (const d of list) {
        const saved = working.placements[d.device_id];
        const fx = saved && typeof saved.x === "number" ? saved.x : auto[d.device_id].x;
        const fy = saved && typeof saved.y === "number" ? saved.y : auto[d.device_id].y;
        out.push({
          device: d,
          areaId: room.area_id,
          fx,
          fy,
          gx: room.x + fx * room.w,
          gy: room.y + fy * room.h,
        });
      }
    }
    return out;
  }, [roomsHere, byArea, working.placements]);

  const roomTint = useMemo(() => {
    const out: Record<string, number> = {};
    for (const room of roomsHere) {
      const list = byArea.get(room.area_id) ?? [];
      out[room.area_id] = list.reduce((m, d) => Math.max(m, brightness(d)), 0);
    }
    return out;
  }, [roomsHere, byArea]);

  const roomCounts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const room of roomsHere) out[room.area_id] = (byArea.get(room.area_id) ?? []).length;
    return out;
  }, [roomsHere, byArea]);

  const bounds = useMemo(() => fitBounds(roomsHere), [roomsHere]);
  const overlaps = useMemo(() => overlappingRooms(roomsHere), [roomsHere]);

  const selectedDevice = useMemo(
    () => (selection?.type === "device" ? devices.find((d) => d.device_id === selection.id) ?? null : null),
    [selection, devices],
  );
  const selectedRoom = useMemo(
    () => (selection?.type === "room" ? working.rooms.find((r) => r.area_id === selection.id) ?? null : null),
    [selection, working.rooms],
  );

  const traffic = trafficResult?.status === "ok" ? trafficResult.traffic : null;
  const trafficNote =
    trafficResult && trafficResult.status !== "ok" ? trafficResult.message : "";
  const seeing = traffic?.visibility.seeing_traffic ?? false;

  const selectedLan: LanOnlyRow | null = useMemo(() => {
    if (selection?.type !== "lan" || !traffic) return null;
    return traffic.lan_only.find((r) => r.key === selection.id) ?? null;
  }, [selection, traffic]);

  const selectedTraffic: TrafficRow | null = useMemo(() => {
    if (!selectedDevice || !traffic) return null;
    return traffic.devices[selectedDevice.device_id] ?? null;
  }, [selectedDevice, traffic]);

  /** `18 virtual · 6 system`, or empty when the resolver hid nothing. */
  const hiddenNote = useMemo(() => {
    const h = payload?.hidden;
    if (!h) return "";
    const parts: string[] = [];
    if (h.non_physical > 0) parts.push(`${h.non_physical} virtual`);
    if (h.system > 0) parts.push(`${h.system} system`);
    return parts.join(" · ");
  }, [payload]);

  const areaName = useCallback(
    (areaId: string | null) => {
      if (!areaId) return "Unplaced";
      return (
        working.rooms.find((r) => r.area_id === areaId)?.name ??
        payload?.areas.find((a) => a.area_id === areaId)?.name ??
        areaId
      );
    },
    [working.rooms, payload],
  );

  /* ── pointer geometry ──────────────────────────────────────────────── */

  const toUnits = useCallback((clientX: number, clientY: number) => {
    const svg = svgRef.current;
    if (!svg) return null;
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    const p = new DOMPoint(clientX, clientY).matrixTransform(ctm.inverse());
    return { x: p.x / SCALE, y: p.y / SCALE };
  }, []);

  /**
   * Rooms on the shown floor read from the refs, so every drag handler below
   * keeps one identity for the life of the component. Handlers that change on
   * each render would defeat the memo on RoomShape and DeviceGlyph, which is
   * the whole point of memoising them.
   */
  const floorIdRef = useRef(floorId);
  useEffect(() => {
    floorIdRef.current = floorId;
  }, [floorId]);

  const currentRooms = useCallback((): HomeLayoutRoom[] => {
    const l = draftRef.current ?? layoutRef.current;
    const fid = floorIdRef.current;
    return l.rooms.filter((r) => r.floor_id === fid);
  }, []);

  const roomAt = useCallback(
    (ux: number, uy: number): HomeLayoutRoom | null =>
      currentRooms().find((r) => ux >= r.x && ux <= r.x + r.w && uy >= r.y && uy <= r.y + r.h) ??
      null,
    [currentRooms],
  );

  const placeDevice = useCallback(
    (deviceId: string, room: HomeLayoutRoom, ux: number, uy: number) => {
      const fx = Number(Math.max(0.04, Math.min(0.96, (ux - room.x) / room.w)).toFixed(3));
      const fy = Number(Math.max(0.04, Math.min(0.96, (uy - room.y) / room.h)).toFixed(3));
      const cur = (draftRef.current ?? layoutRef.current).placements[deviceId];
      if (cur && cur.area_id === room.area_id && cur.x === fx && cur.y === fy) return;
      mutate((l) => ({
        ...l,
        placements: { ...l.placements, [deviceId]: { area_id: room.area_id, x: fx, y: fy } },
      }));
    },
    [mutate],
  );

  /* ── sheet drags ───────────────────────────────────────────────────── */

  const dragRef = useRef<Drag>(null);
  const applyDrag = useCallback((next: Drag) => {
    dragRef.current = next;
    setDrag(next);
  }, []);

  const onRoomMoveStart = useCallback(
    (areaId: string, e: React.PointerEvent) => {
      const room = currentRooms().find((r) => r.area_id === areaId);
      const u = toUnits(e.clientX, e.clientY);
      if (!room || !u) return;
      try {
        svgRef.current?.setPointerCapture(e.pointerId);
      } catch {
        /* no capture available: the drag still tracks on the sheet */
      }
      applyDrag({ kind: "room-move", areaId, dx: u.x - room.x, dy: u.y - room.y });
    },
    [currentRooms, toUnits, applyDrag],
  );

  const onRoomResizeStart = useCallback(
    (areaId: string, e: React.PointerEvent) => {
      try {
        svgRef.current?.setPointerCapture(e.pointerId);
      } catch {
        /* no capture available: the drag still tracks on the sheet */
      }
      applyDrag({ kind: "room-resize", areaId });
    },
    [applyDrag],
  );

  // Pointer capture retargets the follow-up click to the sheet, so the pressed
  // glyph or room never sees it and the sheet reads it as "clear selection".
  // A press that did not move is the tap: select on pointer-up and swallow
  // that one sheet click.
  const suppressClearRef = useRef(false);
  const selectFromTap = useCallback((next: { type: "device" | "room"; id: string }) => {
    setSelection(next);
    suppressClearRef.current = true;
    window.setTimeout(() => {
      suppressClearRef.current = false;
    }, 0);
  }, []);

  const selectRoom = useCallback((id: string) => setSelection({ type: "room", id }), []);
  /**
   * The room name was tapped in edit mode. Select the room and ask the toolbar
   * for its rename field in one go: the owner already said which room and that
   * they want to rename it, and a second tap on "Rename" adds nothing.
   */
  const requestRename = useCallback((areaId: string) => {
    setSelection({ type: "room", id: areaId });
    setRenameSeq((n) => n + 1);
  }, []);
  const selectDevice = useCallback((id: string) => setSelection({ type: "device", id }), []);
  const clearSelection = useCallback(() => {
    if (suppressClearRef.current) {
      suppressClearRef.current = false;
      return;
    }
    setSelection(null);
  }, []);

  const onDeviceDragStart = useCallback(
    (deviceId: string, e: React.PointerEvent) => {
      try {
        svgRef.current?.setPointerCapture(e.pointerId);
      } catch {
        /* no capture available: the drag still tracks on the sheet */
      }
      applyDrag({ kind: "device", deviceId, moved: false, sx: e.clientX, sy: e.clientY });
    },
    [applyDrag],
  );

  const pointRef = useRef<{ x: number; y: number } | null>(null);
  const frameRef = useRef<number | null>(null);

  /**
   * At most one layout update per animation frame, and none at all when the
   * snapped position has not changed. Rooms land on whole grid units, so most
   * pointermoves on the kiosk would otherwise re-draw the entire sheet to
   * produce the identical picture.
   */
  const applyPointer = useCallback(() => {
    frameRef.current = null;
    const pt = pointRef.current;
    const d = dragRef.current;
    if (!pt || !d) return;
    const u = toUnits(pt.x, pt.y);
    if (!u) return;
    if (d.kind === "room-move") {
      const room = currentRooms().find((r) => r.area_id === d.areaId);
      if (!room) return;
      const nx = clampInt(u.x - d.dx, 0, 200);
      const ny = clampInt(u.y - d.dy, 0, 200);
      if (room.x === nx && room.y === ny) return;
      mutate((l) => ({
        ...l,
        rooms: l.rooms.map((r) => (r.area_id === d.areaId ? { ...r, x: nx, y: ny } : r)),
      }));
    } else if (d.kind === "room-resize") {
      const room = currentRooms().find((r) => r.area_id === d.areaId);
      if (!room) return;
      const nw = clampInt(Math.max(2, u.x - room.x), 2, 200 - room.x);
      const nh = clampInt(Math.max(2, u.y - room.y), 2, 200 - room.y);
      if (room.w === nw && room.h === nh) return;
      mutate((l) => ({
        ...l,
        rooms: l.rooms.map((r) => (r.area_id === d.areaId ? { ...r, w: nw, h: nh } : r)),
      }));
    } else {
      const far = Math.abs(pt.x - d.sx) > DRAG_SLOP || Math.abs(pt.y - d.sy) > DRAG_SLOP;
      if (!far) return;
      if (!d.moved) applyDrag({ ...d, moved: true });
      const room = roomAt(u.x, u.y);
      if (room) placeDevice(d.deviceId, room, u.x, u.y);
    }
  }, [toUnits, currentRooms, mutate, roomAt, placeDevice, applyDrag]);

  const onSheetPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!dragRef.current) return;
      pointRef.current = { x: e.clientX, y: e.clientY };
      if (frameRef.current !== null) return;
      frameRef.current = window.requestAnimationFrame(applyPointer);
    },
    [applyPointer],
  );

  /**
   * Placement saves are requested, not called. A drop mutates the layout and
   * asks for a save in the same handler, where `layout` is still the value
   * from before the drop: saving from an effect means the PUT carries the
   * placement that was just made rather than the one before it.
   */
  const [saveWanted, setSaveWanted] = useState(false);

  useEffect(() => {
    if (!saveWanted) return;
    setSaveWanted(false);
    if (!driver || draftRef.current) return;
    let live = true;
    setSaving(true);
    driver
      // The ref, not the state: on a fresh house this is the seeded plan, and
      // the placement just dropped is already folded into it.
      .saveLayout(layoutRef.current)
      .then(() => {
        if (live) setError("");
      })
      .catch((err: any) => {
        if (live) setError(err?.message || "Could not save the floorplan.");
      })
      .finally(() => {
        if (live) setSaving(false);
      });
    return () => {
      live = false;
    };
  }, [saveWanted, driver]);

  const onSheetPointerUp = useCallback(
    (e: React.PointerEvent) => {
      if (!dragRef.current) return;
      // The last frame may still be queued: land it before the drag ends.
      if (frameRef.current !== null) {
        window.cancelAnimationFrame(frameRef.current);
        applyPointer();
      }
      pointRef.current = null;
      const d = dragRef.current;
      if (!d) return;
      try {
        svgRef.current?.releasePointerCapture(e.pointerId);
      } catch {
        /* capture already gone */
      }
      const moved = d.kind === "device" && d.moved;
      if (d.kind === "device" && !d.moved) {
        selectFromTap({ type: "device", id: d.deviceId });
      } else if (d.kind === "room-move" || d.kind === "room-resize") {
        // Edit mode: the room just pressed or moved becomes the selection.
        selectFromTap({ type: "room", id: d.areaId });
      }
      applyDrag(null);
      if (moved && !draftRef.current) setSaveWanted(true);
    },
    [applyPointer, applyDrag, selectFromTap],
  );

  /* ── tray drag ─────────────────────────────────────────────────────── */

  const onTrayDragStart = useCallback((deviceId: string, e: React.PointerEvent) => {
    setTrayDrag({ deviceId, x: e.clientX, y: e.clientY, moved: false });
  }, []);

  useEffect(() => {
    if (!trayDrag) return;
    const move = (e: PointerEvent) => {
      setTrayDrag((prev) => (prev ? { ...prev, x: e.clientX, y: e.clientY, moved: true } : prev));
    };
    const up = (e: PointerEvent) => {
      setTrayDrag((prev) => {
        if (prev?.moved) {
          const u = toUnits(e.clientX, e.clientY);
          const room = u ? roomAt(u.x, u.y) : null;
          if (u && room) {
            placeDevice(prev.deviceId, room, u.x, u.y);
            if (!draftRef.current) setSaveWanted(true);
          }
        }
        return null;
      });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [trayDrag, toUnits, roomAt, placeDevice]);

  /* ── edit mode ─────────────────────────────────────────────────────── */

  const startEdit = () => {
    const base = layoutRef.current;
    const copy: HomeLayout = {
      ...base,
      rooms: base.rooms.map((r) => ({ ...r })),
      floors: [...floors],
      placements: { ...base.placements },
    };
    draftRef.current = copy;
    setDraft(copy);
  };

  const savingRef = useRef(false);

  const cancelEdit = () => {
    // A PUT already on the wire cannot be recalled; cancelling under it would
    // say "discarded" and then keep the edits when it lands.
    if (savingRef.current) return;
    draftRef.current = null;
    setDraft(null);
    applyDrag(null);
  };

  /** Returns true when the house was written, so the toolbar can say so. */
  const saveEdit = async (): Promise<boolean> => {
    const next = draftRef.current;
    if (!driver || !next || savingRef.current) return false;
    savingRef.current = true;
    setSaving(true);
    try {
      await driver.saveLayout(next);
      applyLayout(next);
      draftRef.current = null;
      setDraft(null);
      setError("");
      return true;
    } catch (err: any) {
      setError(err?.message || "Could not save the floorplan.");
      return false;
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  const addRoom = (name: string) => {
    const slug = name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 40);
    if (!slug) return "name already used";
    const cur = draftRef.current;
    if (!cur) return "not editing";
    const here = cur.rooms.filter((r) => r.floor_id === floorId);
    const id = `puff_${slug}`;
    if (cur.rooms.some((r) => r.area_id === id)) return "name already used";
    if (here.some((r) => r.name.toLowerCase() === name.toLowerCase())) return "name already used";
    const maxY = here.reduce((m, r) => Math.max(m, r.y + r.h), 0);
    updateDraft((prev) => ({
      ...prev,
      rooms: [
        ...prev.rooms,
        { area_id: id, name: name.slice(0, 64), floor_id: floorId, x: 0, y: Math.min(194, maxY + 1), w: 8, h: 6, custom: true },
      ],
    }));
    return null;
  };

  const addFloor = (name: string) => {
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40);
    if (!slug) return "name already used";
    const cur = draftRef.current;
    if (!cur) return "not editing";
    const base = cur.floors.length > 0 ? cur.floors : floors;
    if (base.some((f) => f.floor_id === slug || f.name.toLowerCase() === name.toLowerCase()))
      return "name already used";
    updateDraft((prev) => {
      const list = prev.floors.length > 0 ? prev.floors : floors;
      return {
        ...prev,
        floors: [...list, { floor_id: slug, name: name.slice(0, 64), level: list.length }],
      };
    });
    setFloorId(slug);
    return null;
  };

  const renameRoom = (name: string) => {
    if (!selectedRoom) return "no room selected";
    const cur = draftRef.current;
    if (!cur) return "not editing";
    if (
      cur.rooms.some(
        (r) =>
          r.floor_id === selectedRoom.floor_id &&
          r.area_id !== selectedRoom.area_id &&
          r.name.toLowerCase() === name.toLowerCase(),
      )
    )
      return "name already used";
    updateDraft((prev) => ({
      ...prev,
      rooms: prev.rooms.map((r) =>
        r.area_id === selectedRoom.area_id ? { ...r, name: name.slice(0, 64) } : r,
      ),
    }));
    return null;
  };

  /* ── control ───────────────────────────────────────────────────────── */

  const optimistic = useCallback((intent: ControlIntent, current: HomeDevice | null) => {
    const ent = current ? primaryEntity(current) : null;
    const patch: Override = {};
    if (intent.kind === "toggle") {
      const on = intent.action === "turn_on";
      if (ent?.domain === "lock") patch.state = on ? "locked" : "unlocked";
      else if (ent?.domain === "cover") patch.state = on ? "open" : "closed";
      else patch.state = on ? "on" : "off";
    } else if (intent.kind === "brightness") {
      patch.state = "on";
      patch.attributes = { brightness: Math.round((intent.pct / 100) * 255) };
    } else if (intent.kind === "climate") {
      patch.attributes = {};
      if (intent.temperature !== undefined) patch.attributes.temperature = intent.temperature;
      if (intent.hvac_mode !== undefined) patch.state = intent.hvac_mode;
    } else if (intent.kind === "service") {
      if (intent.service === "media_play") patch.state = "playing";
      if (intent.service === "media_pause") patch.state = "paused";
      if (intent.service === "lock") patch.state = "locked";
      if (intent.service === "unlock") patch.state = "unlocked";
      if (intent.service === "open_cover") patch.state = "open";
      if (intent.service === "close_cover") patch.state = "closed";
      if (intent.service === "volume_set")
        patch.attributes = { volume_level: intent.data?.volume_level };
    }
    setOverrides((prev) => ({
      ...prev,
      [intent.entityId]: {
        state: patch.state ?? prev[intent.entityId]?.state,
        attributes: { ...(prev[intent.entityId]?.attributes ?? {}), ...(patch.attributes ?? {}) },
      },
    }));
  }, []);

  const dropOverride = useCallback((entityId: string) => {
    setOverrides((prev) => {
      const next = { ...prev };
      delete next[entityId];
      return next;
    });
  }, []);

  const openConfirm = useCallback(
    async (intent: ControlIntent) => {
      if (!driver) return;
      setBusy(true);
      setCtlError("");
      const minted = await driver.requestTicket(intent);
      setBusy(false);
      if (!minted.ok) {
        setCtlError(minted.message);
        return;
      }
      setPending({ intent, ticket: minted.ticket, proposal: minted.proposal });
    },
    [driver],
  );

  const runControl = useCallback(
    async (intent: ControlIntent) => {
      if (!driver) return;
      const device = devices.find((d) => d.entities.some((e) => e.entity_id === intent.entityId)) ?? null;
      const critical = device?.entities.some(
        (e) => e.entity_id === intent.entityId && e.safety_critical,
      );
      setCtlError("");
      setCtlNote("");
      if (critical) {
        await openConfirm(intent);
        return;
      }
      optimistic(intent, device);
      setBusy(true);
      const result = await driver.control(intent);
      setBusy(false);
      if (result.status === "ok") return;
      dropOverride(intent.entityId);
      if (result.status === "needs_confirmation") {
        setCtlNote(result.message);
        await openConfirm(intent);
        return;
      }
      setCtlError(result.message);
    },
    [driver, devices, openConfirm, optimistic, dropOverride],
  );

  const confirmPending = useCallback(async () => {
    if (!driver || !pending) return;
    setBusy(true);
    const result = await driver.control(pending.intent, pending.ticket);
    setBusy(false);
    setPending(null);
    if (result.status === "ok") {
      setCtlNote("");
      void load(false);
      return;
    }
    setCtlError(result.status === "error" ? result.message : result.message);
  }, [driver, pending, load]);

  /** Per-entity, in sequence. Never an area call. */
  const lightsOff = useCallback(async () => {
    if (!driver || !selectedRoom) return;
    const list = litLightEntities(byArea.get(selectedRoom.area_id) ?? []);
    if (list.length === 0) return;
    setBusy(true);
    setCtlError("");
    let failed = 0;
    let lastMessage = "";
    for (const e of list) {
      const result = await driver.control({
        kind: "toggle",
        entityId: e.entity_id,
        action: "turn_off",
      });
      if (result.status === "ok") {
        optimistic({ kind: "toggle", entityId: e.entity_id, action: "turn_off" }, null);
      } else {
        failed += 1;
        lastMessage = result.message;
      }
    }
    setBusy(false);
    if (failed > 0) setCtlError(`${failed} of ${list.length} refused. ${lastMessage}`);
    else setCtlNote(`${list.length} off.`);
  }, [driver, selectedRoom, byArea, optimistic]);

  /* ── render ────────────────────────────────────────────────────────── */

  const inspector = (
    <Inspector
      device={selectedDevice}
      room={selectedRoom}
      roomDevices={selectedRoom ? byArea.get(selectedRoom.area_id) ?? [] : []}
      areaName={areaName}
      busy={busy}
      error={ctlError}
      note={ctlNote}
      trafficRow={selectedTraffic}
      lanRow={selectedLan}
      trafficKnown={!!traffic}
      onControl={runControl}
      onLightsOff={lightsOff}
      onClose={() => setSelection(null)}
    />
  );

  return (
    <div>
      {error && (
        <div
          className="mb-2 rounded-sm border-l-2 px-3 py-2 text-xs"
          style={{ borderColor: "var(--puff-red)", background: "var(--card2)", color: "var(--text2)" }}
        >
          {error}
        </div>
      )}

      <div
        className="rounded border"
        style={{ borderColor: "var(--line)", background: "var(--card)" }}
      >
        <EditToolbar
          layer={layer}
          onLayer={setLayer}
          floors={floors}
          floorId={floorId}
          onFloor={setFloorId}
          editMode={editMode}
          dirty={editMode}
          saving={saving}
          onEdit={startEdit}
          onSave={saveEdit}
          onCancel={cancelEdit}
          onAddRoom={addRoom}
          onAddFloor={addFloor}
          onRenameRoom={renameRoom}
          canRename={!!selectedRoom}
          roomName={selectedRoom?.name ?? ""}
          hours={trafficHours}
          onHours={setTrafficHours}
          renameRequest={renameSeq}
        />

        {/* What the traffic layer can and cannot see, said plainly. The real
            house has no DNS visibility until the router points at Puff, and
            drawing anything at all in that state would be a fiction. */}
        {layer === "traffic" && trafficNote && (
          <div
            className="border-b px-3 py-1.5 text-[11px]"
            style={{ borderColor: "var(--line)", color: "var(--text2)" }}
          >
            {trafficNote}
          </div>
        )}
        {layer === "traffic" && traffic && !seeing && (
          <div className="border-b px-3 py-1.5" style={{ borderColor: "var(--line)" }}>
            <div className="text-[11px]" style={{ color: "var(--text2)" }}>
              No DNS visibility yet. Devices are not using Puff&apos;s DNS, so there is nothing to
              draw.
            </div>
            <div className="font-mono text-[10px]" style={{ color: "var(--text3)" }}>
              adguard {traffic.visibility.adguard_reachable ? "reachable" : "unreachable"} · queries
              today {traffic.visibility.queries_today}
            </div>
          </div>
        )}

        <div className="flex flex-col lg:flex-row">
          <div className="min-w-0 flex-1">
            <div className="h-[56vh] min-h-[360px] lg:h-[calc(100vh-19rem)] lg:min-h-[460px]">
              {loading && !payload ? (
                <div
                  className="flex h-full items-center justify-center text-xs tracking-wider uppercase"
                  style={{ color: "var(--text3)" }}
                >
                  Drawing the house
                </div>
              ) : (
                <FloorSheet
                  svgRef={svgRef}
                  rooms={roomsHere}
                  placed={placed}
                  bounds={bounds}
                  selection={selection}
                  layer={layer}
                  editMode={editMode}
                  overlapping={overlaps}
                  roomTint={roomTint}
                  roomCounts={roomCounts}
                  houseName="Home"
                  floorName={floors.find((f) => f.floor_id === floorId)?.name ?? "Ground"}
                  deviceCount={devices.length}
                  asOf={shortTime(payload?.generated_at)}
                  hiddenNote={hiddenNote}
                  traffic={traffic}
                  dragging={drag !== null || !!trayDrag?.moved}
                  onSelectRoom={selectRoom}
                  onSelectDevice={selectDevice}
                  onClearSelection={clearSelection}
                  onRoomMoveStart={onRoomMoveStart}
                  onRoomResizeStart={onRoomResizeStart}
                  onDeviceDragStart={onDeviceDragStart}
                  onRenameRoom={requestRename}
                  onPointerMove={onSheetPointerMove}
                  onPointerUp={onSheetPointerUp}
                />
              )}
            </div>
            {layer === "traffic" && traffic && (
              <LanOnlyStrip
                rows={traffic.lan_only}
                selectedKey={selection?.type === "lan" ? selection.id : null}
                onSelect={(key) => setSelection({ type: "lan", id: key })}
              />
            )}
            <UnplacedTray
              devices={unplaced}
              selectedId={selection?.type === "device" ? selection.id : null}
              dragId={trayDrag?.moved ? trayDrag.deviceId : null}
              onSelect={(id) => setSelection({ type: "device", id })}
              onDragStart={onTrayDragStart}
            />
          </div>

          {/* Rail on desktop. */}
          <aside
            className="hidden w-[20rem] shrink-0 border-l lg:block"
            style={{ borderColor: "var(--line)" }}
          >
            {inspector}
          </aside>
        </div>
      </div>

      {/* Bottom sheet on narrow screens. */}
      {selection && (
        <div
          className="fixed inset-x-0 bottom-0 z-40 max-h-[48vh] overflow-y-auto border-t lg:hidden"
          style={{ borderColor: "var(--line)", background: "var(--puff-surface-deep)" }}
        >
          {inspector}
        </div>
      )}

      {trayDrag?.moved && (
        <div
          className="pointer-events-none fixed z-50 rounded-sm border px-2 py-1 text-[11px]"
          style={{
            left: trayDrag.x + 10,
            top: trayDrag.y + 10,
            borderColor: "var(--pink)",
            background: "var(--puff-surface-deep)",
            color: "var(--text)",
          }}
        >
          {devices.find((d) => d.device_id === trayDrag.deviceId)?.name ?? trayDrag.deviceId}
        </div>
      )}

      {pending && (
        <ConfirmSheet
          proposal={pending.proposal}
          busy={busy}
          onConfirm={confirmPending}
          onCancel={() => setPending(null)}
        />
      )}
    </div>
  );
}
