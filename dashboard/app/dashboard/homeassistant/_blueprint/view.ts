/**
 * Shared vocabulary for the sheet: one grid unit, one selection, and the
 * handful of state questions every component asks a device.
 *
 * These live apart from the container so the SVG pieces do not have to import
 * Blueprint.tsx back for a type.
 */

import type { HomeDevice, HomeEntity, TrafficRow } from "../../../_lib/services/homeService";

/** SVG user units per grid unit. Everything on the sheet is drawn in these. */
export const SCALE = 16;

export type LayerKey = "devices" | "state" | "traffic";

export type Selection =
  | { type: "device"; id: string }
  | { type: "room"; id: string }
  /** A LAN address with traffic that is not one of the house's devices. */
  | { type: "lan"; id: string }
  | null;

/** The traffic windows the toolbar offers, in hours. */
export const TRAFFIC_WINDOWS: { hours: number; label: string }[] = [
  { hours: 1, label: "1H" },
  { hours: 24, label: "24H" },
  { hours: 168, label: "7D" },
];

/** Categories the owner is meant to notice. Everything else is plumbing. */
const LOUD_CATEGORIES = new Set(["TELEMETRY", "ADS", "TRACKING"]);

export function isLoudCategory(category: string): boolean {
  return LOUD_CATEGORIES.has(category.toUpperCase());
}

export function hasLoudDomain(row: TrafficRow): boolean {
  return row.top_domains.some((d) => isLoudCategory(d.category));
}

/**
 * The one verdict a line is drawn in.
 *
 * Whichever bucket holds the most lookups. There is no per-domain verdict to
 * mix in here, only per-device counts, and the line says exactly that.
 */
export type Verdict = "allowed" | "monitored" | "blocked";

export function dominantVerdict(row: TrafficRow): Verdict {
  if (row.blocked >= row.allowed && row.blocked >= row.monitored && row.blocked > 0) return "blocked";
  if (row.monitored > row.allowed) return "monitored";
  return "allowed";
}

export const VERDICT_TONE: Record<Verdict, string> = {
  allowed: "var(--sky)",
  monitored: "var(--lemon)",
  blocked: "var(--magenta)",
};

/** 1240 reads as 1.2k. The badge has room for four characters and no more. */
export function compactCount(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 1000) return String(Math.round(n));
  if (n < 10_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  if (n < 1_000_000) return `${Math.round(n / 1000)}k`;
  return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}m`;
}

/**
 * Middle truncation, because the interesting half of a hostile domain name is
 * the end. `a1.tuya-analytics-collector-eu-central.example` truncated at the
 * right would read as a Tuya domain and nothing else.
 */
export function middleTruncate(text: string, max: number): string {
  if (text.length <= max) return text;
  const keep = max - 1;
  const head = Math.ceil(keep / 2);
  const tail = Math.floor(keep / 2);
  return `${text.slice(0, head)}…${text.slice(text.length - tail)}`;
}

/** A device with its resolved position, in grid units, on the sheet. */
export interface Placed {
  device: HomeDevice;
  areaId: string;
  /** 0 to 1 inside the room. */
  fx: number;
  fy: number;
  /** Absolute grid units. */
  gx: number;
  gy: number;
}

export function primaryEntity(d: HomeDevice): HomeEntity | null {
  if (!d.entities || d.entities.length === 0) return null;
  return d.entities.find((e) => e.entity_id === d.primary_entity_id) ?? d.entities[0];
}

export function stateOf(d: HomeDevice): string {
  return primaryEntity(d)?.state ?? "unknown";
}

export function isUnavailable(d: HomeDevice): boolean {
  const s = stateOf(d);
  return s === "unavailable" || s === "unknown" || s === "";
}

export function isSafetyCritical(d: HomeDevice): boolean {
  return d.entities.some((e) => e.safety_critical);
}

/** "Lit" for the map: a light or switch that is on. Nothing else glows. */
export function isLit(d: HomeDevice): boolean {
  if (d.kind !== "light" && d.kind !== "switch") return false;
  return stateOf(d) === "on";
}

/** 0 to 1. A light with no brightness attribute reads as full. */
export function brightness(d: HomeDevice): number {
  if (!isLit(d)) return 0;
  const raw = primaryEntity(d)?.attributes?.brightness;
  if (raw === null || raw === undefined || Number.isNaN(Number(raw))) return 1;
  return Math.max(0, Math.min(1, Number(raw) / 255));
}

export function brightnessPct(d: HomeDevice): number {
  return Math.round(brightness(d) * 100);
}

export function motionActive(d: HomeDevice): boolean {
  const e = primaryEntity(d);
  if (!e || e.domain !== "binary_sensor") return false;
  return e.state === "on";
}

export function mediaPlaying(d: HomeDevice): boolean {
  const e = primaryEntity(d);
  return !!e && e.domain === "media_player" && e.state === "playing";
}

export function climateTemp(d: HomeDevice): number | null {
  const e = primaryEntity(d);
  if (!e || e.domain !== "climate") return null;
  const t = e.attributes?.current_temperature;
  return typeof t === "number" ? t : null;
}

/** Every light entity in a room that is currently on, for "Lights off". */
export function litLightEntities(devices: HomeDevice[]): HomeEntity[] {
  const out: HomeEntity[] = [];
  for (const d of devices) {
    for (const e of d.entities) {
      if (e.domain === "light" && e.state === "on") out.push(e);
    }
  }
  return out;
}

export function shortTime(iso: string | undefined): string {
  if (!iso) return "--:--";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "--:--";
  return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
