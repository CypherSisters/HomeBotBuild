/**
 * Directions page helpers: the pure half of
 * app/dashboard/directions. Everything the page shows is built here, so the
 * privacy rule and the plain-words copy are testable without a browser.
 *
 * Contract: docs/MAPS_ROUTE_API.md in the backend repository.
 * A route record arrives from GET /maps/routes/<id|latest> through /api/pai.
 *
 * PRIVACY RULE: no coordinates on the page, ever. The home point is never in a
 * record. The destination's lat/lon are in the record, but normalizeRoute()
 * drops them on the way in, so they never reach React state; the only place
 * they survive is inside omaps_url, which is only ever drawn as a QR code.
 * As a second fence, every text field passes through safeText(), which strips
 * anything shaped like a coordinate.
 *
 * Pure module: no React, no next/*, so plain-node tests import it directly
 * under Node's TypeScript type stripping.
 *
 * puff puff pass
 */

export type TravelMode = "drive" | "walk" | "bike";

export interface RouteStep {
  n: number;
  text: string;
  distance_mi: number;
  duration_s: number;
}

/** A route record with the destination coordinates already dropped. */
export interface MapsRoute {
  id: string;
  created_at: string;
  mode: TravelMode;
  destination: { label: string; address: string };
  distance_mi: number;
  duration_s: number;
  steps: RouteStep[];
  data_date: string | null;
  traffic_note: string | null;
  /** Only ever drawn as a QR code. Null unless it is an omaps.app link. */
  omaps_url: string | null;
}

/** 16 lower-case hex characters, same rule the backend enforces. */
export const ROUTE_ID_RE = /^[0-9a-f]{16}$/;

export const NORMAL_TRAFFIC_NOTE = "Times assume normal traffic.";

/** Backend path for a page id ("latest" or a route id). Null for anything else. */
export function routeApiPath(id: string | null | undefined): string | null {
  if (id === "latest") return "/maps/routes/latest";
  if (typeof id === "string" && ROUTE_ID_RE.test(id)) return `/maps/routes/${id}`;
  return null;
}

/* ── coordinates: never on the page ─────────────────────────────────── */

/*
 * What counts as a coordinate. Tight on purpose, so road text survives:
 * "Exit 1.5,2.5 split", "Hwy 281.1234", "Exit 12.5" and "0.3 mi" are kept.
 *   - degrees with a degree sign, optional minutes / seconds (' ′ " ″) and
 *     hemisphere: 30°7'N, 30° 7.4' N, 30°7′24″N, 30.1234°
 *   - a "lat, lon" pair: 3+ decimals on both parts, |lat| <= 90, |lon| <= 180
 *   - a lone number with 4+ decimals AND a minus sign (- or the unicode
 *     minus), sign included, so no stray sign is left behind
 */
// − is the unicode minus; ° degree; ′/″ prime/double prime;
// ’/” curly quotes standing in for them.
const DMS =
  /(?<![\d.])[-+−]?\d{1,3}(?:\.\d+)?\s*°(?:\s*\d{1,2}(?:\.\d+)?\s*['′’])?(?:\s*\d{1,2}(?:\.\d+)?\s*(?:["″”]|''|′′))?(?:\s*[NSEW](?![A-Za-z]))?/g;
const PAIR =
  /(?<![\d.])([-+−]?\d{1,3}\.\d{3,})\s*,\s*([-+−]?\d{1,3}\.\d{3,})(?!\d|\.\d)/g;
const LONE = /(?<![\d.\w])[-−]\d{1,3}\.\d{4,}(?!\d|\.\d)/g;

function toNum(s: string): number {
  return Number(s.replace("−", "-"));
}

/** Placeholder for a removed coordinate, so the separators around it can go too. */
const CUT = "\u0000";

/** The string with every coordinate-shaped piece replaced by CUT. */
function markCoordinates(s: string): string {
  return s
    .split(CUT)
    .join("")
    .replace(DMS, CUT)
    .replace(PAIR, (m: string, lat: string, lon: string) =>
      Math.abs(toNum(lat)) <= 90 && Math.abs(toNum(lon)) <= 180 ? CUT : m,
    )
    .replace(LONE, CUT);
}

export function hasCoordinateText(s: string): boolean {
  return markCoordinates(s).includes(CUT);
}

/** Trimmed string with anything coordinate-shaped removed. */
export function safeText(v: unknown): string {
  if (typeof v !== "string") return "";
  return markCoordinates(v)
    // A run of removed pieces and the commas/spaces between them is one gap.
    .replace(/[\s,]*\u0000(?:[\s,]*\u0000)*[\s,]*/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\s+([,.])/g, "$1")
    .replace(/^[\s,]+|[\s,]+$/g, "")
    .trim();
}

/* ── normalizing the record ─────────────────────────────────────────── */

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 0;
}

/** Only an https omaps.app link may become a QR code. */
export function safeOmapsUrl(v: unknown): string | null {
  if (typeof v !== "string") return null;
  try {
    const u = new URL(v);
    if (u.protocol === "https:" && u.hostname === "omaps.app") return v;
  } catch {
    /* not a URL */
  }
  return null;
}

/**
 * Shape-check a record from the API. Returns null when it is not a route.
 * Drops destination lat/lon on purpose (see the privacy rule above).
 */
export function normalizeRoute(raw: unknown): MapsRoute | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.id !== "string" || !ROUTE_ID_RE.test(r.id)) return null;
  const mode: TravelMode =
    r.mode === "walk" || r.mode === "bike" ? r.mode : "drive";
  const dest = (r.destination ?? {}) as Record<string, unknown>;
  const steps: RouteStep[] = Array.isArray(r.steps)
    ? r.steps
        .filter((s): s is Record<string, unknown> => !!s && typeof s === "object")
        .map((s, i) => ({
          n: typeof s.n === "number" && s.n > 0 ? Math.floor(s.n) : i + 1,
          text: safeText(s.text),
          distance_mi: num(s.distance_mi),
          duration_s: num(s.duration_s),
        }))
        .filter((s) => s.text)
    : [];
  return {
    id: r.id,
    created_at: typeof r.created_at === "string" ? r.created_at : "",
    mode,
    destination: { label: safeText(dest.label), address: safeText(dest.address) },
    distance_mi: num(r.distance_mi),
    duration_s: num(r.duration_s),
    steps,
    data_date: typeof r.data_date === "string" ? r.data_date : null,
    traffic_note: typeof r.traffic_note === "string" ? safeText(r.traffic_note) : null,
    omaps_url: safeOmapsUrl(r.omaps_url),
  };
}

/* ── formatting ─────────────────────────────────────────────────────── */

/** "4.2 mi", "12 mi", "250 ft". Zero is "" (the arrival step has no distance). */
export function formatDistance(mi: number): string {
  if (!Number.isFinite(mi) || mi <= 0) return "";
  if (mi < 0.1) {
    const ft = Math.max(50, Math.round((mi * 5280) / 50) * 50);
    return `${ft} ft`;
  }
  if (mi >= 10) return `${Math.round(mi)} mi`;
  return `${(Math.round(mi * 10) / 10).toFixed(1)} mi`;
}

/** "under 1 min", "12 min", "1 hr", "1 hr 5 min". */
export function formatDuration(s: number): string {
  if (!Number.isFinite(s) || s <= 0) return "";
  if (s < 60) return "under 1 min";
  const totalMin = Math.round(s / 60);
  if (totalMin < 60) return `${totalMin} min`;
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return m ? `${h} hr ${m} min` : `${h} hr`;
}

const MODE_LABEL: Record<TravelMode, string> = {
  drive: "By car",
  walk: "On foot",
  bike: "By bike",
};

export function modeLabel(mode: TravelMode): string {
  return MODE_LABEL[mode] ?? MODE_LABEL.drive;
}

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** "Map data from September 30, 2026", or "Map data date unknown". Never a guess. */
export function dataDateLabel(d: string | null | undefined): string {
  const m = typeof d === "string" ? d.match(/^(\d{4})-(\d{2})-(\d{2})/) : null;
  if (m) {
    const month = Number(m[2]);
    const day = Number(m[3]);
    if (month >= 1 && month <= 12 && day >= 1 && day <= 31) {
      return `Map data from ${MONTHS[month - 1]} ${day}, ${m[1]}`;
    }
  }
  return "Map data date unknown";
}

/** "Printed October 11, 2026" in the browser's local date. */
export function printedOnLabel(now: Date): string {
  return `Printed ${MONTHS[now.getMonth()]} ${now.getDate()}, ${now.getFullYear()}`;
}

/* ── the view model: every string the page renders ──────────────────── */

export interface DirectionsView {
  title: string;
  destination: string;
  address: string;
  mode: string;
  distance: string;
  duration: string;
  steps: { n: number; text: string; distance: string }[];
  trafficNote: string | null;
  dataDate: string;
  printedOn: string;
  /** For the QR only. Not text on the page. */
  qrUrl: string | null;
}

export function buildDirectionsView(route: MapsRoute, now: Date): DirectionsView {
  const destination =
    route.destination.label || route.destination.address || "your destination";
  const address =
    route.destination.address && route.destination.address !== destination
      ? route.destination.address
      : "";
  return {
    title: `Directions to ${destination}`,
    destination,
    address,
    mode: modeLabel(route.mode),
    distance: formatDistance(route.distance_mi),
    duration: formatDuration(route.duration_s),
    steps: route.steps.map((s) => ({
      n: s.n,
      text: s.text,
      distance: formatDistance(s.distance_mi),
    })),
    trafficNote:
      route.mode === "drive" ? route.traffic_note || NORMAL_TRAFFIC_NOTE : null,
    dataDate: dataDateLabel(route.data_date),
    printedOn: printedOnLabel(now),
    qrUrl: route.omaps_url,
  };
}

/* ── what to show after the fetches ─────────────────────────────────── */

export type LoadResult =
  | { kind: "ready"; route: MapsRoute }
  | { kind: "not-found" }
  | { kind: "no-routes" }
  | { kind: "offline" }
  | { kind: "not-set-up" }
  | { kind: "error" };

export const LOAD_MESSAGES: Record<Exclude<LoadResult["kind"], "ready">, string> = {
  "not-found": "That route isn't saved anymore. Ask Puff again.",
  "no-routes":
    "No directions yet. Ask Puff how to get somewhere, and the steps will show up here.",
  offline: "The offline maps aren't running right now, so there are no directions to show.",
  "not-set-up": "Directions aren't set up on this box yet.",
  error: "Puff couldn't load these directions just now. Try again in a moment.",
};

interface Fetched {
  status: number;
  data: unknown;
}

/**
 * Decide the page state. `route` / `status` are null when the request never
 * got an answer (network error). A saved route always shows, even when the
 * engines are down: the steps are already worked out.
 */
export function classifyLoad(route: Fetched | null, status: Fetched | null): LoadResult {
  const live =
    status && status.status === 200 && status.data && typeof status.data === "object"
      ? (status.data as { live?: unknown }).live
      : undefined;
  const offline = live === false;

  if (route && route.status === 200) {
    const r = normalizeRoute(route.data);
    return r ? { kind: "ready", route: r } : { kind: "error" };
  }
  if (route && route.status === 400) return { kind: "not-found" };
  if (route && route.status === 404) {
    const err =
      route.data && typeof route.data === "object"
        ? (route.data as { error?: unknown }).error
        : undefined;
    if (err === "No such route.") return { kind: "not-found" };
    if (err === "No routes yet.") return offline ? { kind: "offline" } : { kind: "no-routes" };
    // Any other 404 is the proxy or an older backend: no /maps routes here.
    return { kind: "not-set-up" };
  }
  return offline ? { kind: "offline" } : { kind: "error" };
}

/* ── fitting the Organic Maps link into a QR ────────────────────────── */

export const QR_TOO_LONG_MESSAGE =
  "This route can't fit in a QR code. Open Directions on your phone to get the link.";

/**
 * The omaps link as it is if `fits` says so; otherwise the same link with
 * destination_name trimmed a word at a time, then with destination_name
 * dropped (the destination coordinates alone are enough). Null when even that
 * does not fit. Only the destination_name pair is rewritten: every other
 * parameter keeps its exact bytes and order, so the v2/nav shape is unchanged.
 */
export function fitOmapsUrl(url: string, fits: (candidate: string) => boolean): string | null {
  if (fits(url)) return url;
  const q = url.indexOf("?");
  if (q < 0) return null;
  const base = url.slice(0, q);
  const pairs = url.slice(q + 1).split("&");
  const at = pairs.findIndex((p) => p.startsWith("destination_name="));
  const rebuild = (name: string | null) =>
    `${base}?${pairs
      .flatMap((p, i) =>
        i !== at ? [p] : name === null ? [] : [`destination_name=${encodeURIComponent(name)}`],
      )
      .join("&")}`;
  if (at >= 0) {
    let name = "";
    try {
      name = decodeURIComponent(pairs[at].slice("destination_name=".length).replace(/\+/g, " "));
    } catch {
      name = "";
    }
    const words = name.trim().split(/\s+/).filter(Boolean);
    for (let k = words.length - 1; k >= 1; k--) {
      const candidate = rebuild(words.slice(0, k).join(" "));
      if (fits(candidate)) return candidate;
    }
    const bare = rebuild(null);
    if (fits(bare)) return bare;
  }
  return null;
}

/* ── the kiosk's show-me display ────────────────────────────────────── */

const DIRECTIONS_URL_RE = /^\/dashboard\/directions\/[0-9a-f]{16}\/print$/;

/**
 * Where the show-me overlay frames a "directions" item. Built from
 * meta.route_id when it is a real id; otherwise the backend's url if it is
 * exactly the print path; otherwise the latest route. Never an arbitrary URL.
 */
export function directionsFrameSrc(item: {
  url?: unknown;
  meta?: unknown;
}): string {
  const meta = item.meta && typeof item.meta === "object" ? (item.meta as Record<string, unknown>) : {};
  const id = meta.route_id;
  if (typeof id === "string" && ROUTE_ID_RE.test(id)) return `/dashboard/directions/${id}/print`;
  if (typeof item.url === "string" && DIRECTIONS_URL_RE.test(item.url)) return item.url;
  return "/dashboard/directions";
}

/**
 * The show-me overlay's frame target for a "directions" item, or null for any
 * other kind. Checked BEFORE the vault path: a directions item never takes the
 * secure branch, whatever flags it carries, so `secure: true` plus a hostile
 * url still lands on /dashboard/directions/*.
 */
export function directionsFrameUrl(item: {
  kind?: unknown;
  url?: unknown;
  meta?: unknown;
} | null): string | null {
  if (!item || item.kind !== "directions") return null;
  return directionsFrameSrc(item);
}
