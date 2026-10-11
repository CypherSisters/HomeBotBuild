"use client";

/**
 * TileGrid — the dashboard's arrangeable board (react-grid-layout v2).
 *
 * Order-only drag (swapping tiles, each carrying its col-span) shreds the
 * rows when a narrow tile lands in a wide slot. Here every tile has a real
 * (x, y, w, h) rect on a 12-column grid: drag anywhere, resize from the
 * edges, layout persists per breakpoint.
 *
 * Tiles are inert until the parent's "Arrange" toggle sets `editing` — so
 * taps on tile content never move anything by accident (the appliance screen
 * is a touchscreen). While editing, a translucent overlay captures the
 * pointer: the whole tile is a drag surface and its content can't be
 * clicked.
 *
 * Breakpoints are CONTAINER widths (the grid sits beside a 240px sidebar,
 * so a 1280px window gives a ~1000px container — that must still be "lg").
 */

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Responsive,
  verticalCompactor,
  type Layout,
  type LayoutItem,
  type ResponsiveLayouts,
} from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

export interface TileDef {
  id: string;
  /** Default rect on the lg (12-col) grid; other breakpoints derive. */
  lg: {
    x: number;
    y: number;
    w: number;
    h: number;
    minW?: number;
    minH?: number;
  };
  node: React.ReactNode;
}

const BREAKPOINTS = { lg: 940, md: 620, sm: 0 };
const COLS = { lg: 12, md: 8, sm: 2 };
const ROW_HEIGHT = 72;
const MARGIN: [number, number] = [10, 10];
/** Below this a fitted row would be unreadable — clamp and let the grid scroll. */
const MIN_FIT_ROW = 44;

// The version suffix is bumped whenever the default layout changes, so stale
// saved rects (for example taller than the kiosk viewport) are not reused.
const storageKeyFor = (key: string) => `puffpai_grid_v4_${key}`;

/** Wipe a board's saved layout (the parent remounts TileGrid afterwards). */
export function clearTileLayout(key: string) {
  try {
    localStorage.removeItem(storageKeyFor(key));
  } catch {
    /* private mode — nothing to clear */
  }
}

function loadSaved(key: string): ResponsiveLayouts | null {
  try {
    const raw = localStorage.getItem(storageKeyFor(key));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function defaultItem(tile: TileDef): LayoutItem {
  return {
    i: tile.id,
    x: tile.lg.x,
    y: tile.lg.y,
    w: tile.lg.w,
    h: tile.lg.h,
    minW: tile.lg.minW ?? 2,
    minH: tile.lg.minH ?? 2,
  };
}

/**
 * Saved layouts win, but tiles come and go (registry widgets): drop rects
 * whose tile is gone, append defaults (bottom of the board) for tiles the
 * save has never seen.
 */
function reconcile(
  tiles: TileDef[],
  saved: ResponsiveLayouts | null
): ResponsiveLayouts {
  const wanted = new Map(tiles.map((t) => [t.id, t]));
  if (!saved || !Array.isArray(saved.lg)) {
    return { lg: tiles.map(defaultItem) };
  }
  const out: ResponsiveLayouts = {};
  for (const [bp, layout] of Object.entries(saved)) {
    if (!Array.isArray(layout)) continue;
    const kept = layout.filter((l) => wanted.has(l.i));
    const seen = new Set(kept.map((l) => l.i));
    for (const tile of tiles) {
      if (!seen.has(tile.id)) {
        kept.push({ ...defaultItem(tile), y: Infinity });
      }
    }
    out[bp] = kept;
  }
  return out;
}

export default function TileGrid({
  storageKey,
  tiles,
  editing,
  onRemoveTile,
  fitHeight = false,
}: {
  storageKey: string;
  tiles: TileDef[];
  editing: boolean;
  /** Kiosk mode: derive rowHeight from the measured container height so the
   *  whole board lands on one screen (see the rowHeight memo below). */
  fitHeight?: boolean;
  /** When set, arrange mode shows a remove button on every tile (design's
   *  magenta close bubble); the parent hides the tile + offers it back in
   *  the arrange tray. */
  onRemoveTile?: (id: string) => void;
}) {
  // Own the width measurement (library's useContainerWidth was seen serving
  // its 1280 initialWidth to the first real layout, overflowing the column).
  // Grid renders only after a real measurement exists — never at a guess.
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState<number | null>(null);
  // Height is measured too (kiosk fit). The grid's own height is driven BY
  // rowHeight, so measuring the container would feed back on itself —
  // measure the parent, which is the flex slot the grid is allowed to fill.
  const [height, setHeight] = useState<number | null>(null);
  useEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const slot = node.parentElement;
    const measure = () => {
      const w = Math.round(node.getBoundingClientRect().width);
      setWidth((prev) => (prev === w ? prev : w));
      if (slot) {
        const h = Math.round(slot.getBoundingClientRect().height);
        setHeight((prev) => (prev === h ? prev : h));
      }
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(node);
    if (slot) ro.observe(slot);
    return () => ro.disconnect();
  }, []);

  // Saved layout is read once on the client (localStorage is unavailable
  // during SSR); live edits then flow through onLayoutChange.
  //
  // `hydrated` gates the grid: RGL fires onLayoutChange as soon as it mounts,
  // and that write persists whatever it was mounted with. Rendering it before
  // the save is in hand let a default layout clobber the real one on reload
  // (resizes silently reverted) — so mount nothing until we've looked.
  const [layouts, setLayouts] = useState<ResponsiveLayouts | null>(null);
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    setLayouts(loadSaved(storageKey));
    setHydrated(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey]);

  /** Grid renders only once it has both a real width and the saved layout. */
  const mounted = width !== null && hydrated;

  const tileIds = tiles.map((t) => t.id).join("|");
  const merged = useMemo(
    () => reconcile(tiles, layouts),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [tileIds, layouts]
  );

  /**
   * Kiosk fit: rowHeight is whatever makes the board's total rows land
   * exactly on the measured slot height. Recomputes on resize AND on layout
   * change — dragging a tile down adds a row, and the board then re-fits
   * (shorter rows) instead of pushing content off-screen.
   *
   * Only above the lg breakpoint; smaller windows keep the fixed row height
   * and their normal scrolling.
   */
  const { rowHeight, fitted } = useMemo(() => {
    const isLg = (width ?? 0) >= BREAKPOINTS.lg;
    if (!fitHeight || !isLg || !height) {
      return { rowHeight: ROW_HEIGHT, fitted: false };
    }
    const rows = (merged.lg ?? []).reduce(
      (max, l) => Math.max(max, l.y + l.h),
      0
    );
    if (rows <= 0) return { rowHeight: ROW_HEIGHT, fitted: false };
    const raw = Math.floor((height - MARGIN[1] * (rows - 1)) / rows);
    if (raw < MIN_FIT_ROW) return { rowHeight: MIN_FIT_ROW, fitted: false };
    return { rowHeight: raw, fitted: true };
  }, [fitHeight, width, height, merged]);

  const handleLayoutChange = useCallback(
    (_current: Layout, all: ResponsiveLayouts) => {
      setLayouts(all);
      try {
        localStorage.setItem(storageKeyFor(storageKey), JSON.stringify(all));
      } catch {
        /* storage full/blocked — layout just won't persist */
      }
    },
    [storageKey]
  );

  return (
    <div
      ref={containerRef}
      className={editing ? "puff-grid-editing" : ""}
      style={
        fitHeight
          ? {
              height: "100%",
              minHeight: 0,
              // Exact fit needs no scroll and must not clip the tile glow;
              // a clamped (too-short) fit falls back to scrolling.
              overflowY: fitted ? "visible" : "auto",
              overflowX: "visible",
            }
          : undefined
      }
    >
      {mounted ? (
        <Responsive
          layouts={merged}
          breakpoints={BREAKPOINTS}
          cols={COLS}
          width={width as number}
          rowHeight={rowHeight}
          margin={MARGIN}
          containerPadding={[0, 0]}
          compactor={verticalCompactor}
          dragConfig={{ enabled: editing }}
          resizeConfig={{ enabled: editing, handles: ["se", "s", "e"] }}
          onLayoutChange={handleLayoutChange}
        >
          {tiles.map((tile) => (
            <div key={tile.id} className="puff-tile-shell relative">
              <div className="h-full min-h-0 [&>*:first-child]:h-full">
                {tile.node}
              </div>
              {editing && (
                <div
                  className="absolute inset-0 z-30 cursor-grab rounded-[28px] active:cursor-grabbing"
                  style={{
                    border:
                      "1px dashed color-mix(in srgb, var(--pink) 60%, transparent)",
                    background:
                      "color-mix(in srgb, var(--pink) 5%, transparent)",
                  }}
                >
                  {/* This overlay owns the pointer while arranging; the resize
                      grips are lifted above it (z 45) in css/_theme.css so a
                      drag on a corner still resizes instead of moving. */}
                  <span
                    className="absolute left-3 top-3 rounded-full border px-2 py-1"
                    style={{
                      fontFamily:
                        "var(--font-jbmono), 'JetBrains Mono', monospace",
                      fontWeight: 700,
                      fontSize: 9,
                      letterSpacing: "0.14em",
                      textTransform: "uppercase",
                      color: "var(--pink)",
                      background: "var(--card)",
                      borderColor: "var(--line)",
                    }}
                  >
                    drag · resize ↘
                  </span>
                </div>
              )}
              {editing && onRemoveTile && (
                <button
                  type="button"
                  aria-label={`Remove ${tile.id} tile`}
                  onClick={() => onRemoveTile(tile.id)}
                  onPointerDown={(e) => e.stopPropagation()}
                  onMouseDown={(e) => e.stopPropagation()}
                  onTouchStart={(e) => e.stopPropagation()}
                  className="absolute right-3 top-3 z-40 grid h-9 w-9 cursor-pointer place-items-center rounded-full border-0"
                  style={{ background: "var(--magenta)", color: "#fff" }}
                >
                  <span className="msr" style={{ fontSize: 20 }}>
                    close
                  </span>
                </button>
              )}
            </div>
          ))}
        </Responsive>
      ) : (
        <div className="grid grid-cols-4 gap-3.5">
          {tiles.slice(0, 8).map((t) => (
            <div
              key={t.id}
              className="h-40 animate-pulse rounded-[28px]"
              style={{ background: "var(--card2)" }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
