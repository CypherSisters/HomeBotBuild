/**
 * /api/tunein/search — server-side proxy for TuneIn's open OPML station
 * directory (opml.radiotime.com), the same catalog Sonos and countless other
 * appliances query. It sits alongside /api/radio/search (radio-browser) and
 * returns the SAME trimmed Station shape, so the World Radio player can treat
 * either source identically.
 *
 * Why a proxy: the dashboard CSP keeps connect-src at 'self', so the browser
 * can't call radiotime directly; only the resolved AUDIO stream leaves the
 * origin (media-src, see middleware.ts). The proxy also sends the User-Agent
 * radiotime expects and — critically — caches: TuneIn rate-limits keyless
 * callers, so caching is REQUIRED here, not a nicety.
 *
 * One difference from radio-browser: a TuneIn search row is NOT yet playable.
 * It carries a `guide_id` that must be resolved to a stream on play (see
 * /api/tunein/resolve). Every Station we emit therefore has url_resolved:"",
 * and the player resolves it lazily — only for the row the user clicks.
 *
 * The Bridge: TuneIn has its own switch, `tunein`, separate from
 * radio-browser's. Every search asks the backend whether it is open
 * (app/_lib/server/bridgeSwitch.ts) before the cache and before any name is
 * looked up. Closed, or no clear answer, means a JSON refusal and nothing
 * sent, and the host must be one that switch declares.
 *
 * puff puff pass
 */
import { NextRequest, NextResponse } from "next/server";
import type { Station } from "@/app/dashboard/_components/WorldRadio/audio";
import {
  bridgeRefusalResponse,
  checkBridgeSwitch,
} from "@/app/_lib/server/bridgeSwitch";

export const runtime = "nodejs";

const HOST = "opml.radiotime.com";
const TIMEOUT_MS = 8_000;
const CACHE_TTL_MS = 5 * 60 * 1_000;
const MAX_LIMIT = 50;
const DEFAULT_LIMIT = 30;
/** Bounded so a long uptime can't grow this without limit. */
const MAX_CACHE_ENTRIES = 64;

/** A TuneIn station guide_id: the letter s followed by digits (e.g. s328971). */
const GUIDE_ID = /^s[0-9]+$/;

/** radiotime returns numbers as strings ("128"); coerce both forms. */
const num = (v: unknown) => {
  if (typeof v === "number") return Number.isFinite(v) ? v : 0;
  if (typeof v === "string") {
    const n = Number.parseInt(v, 10);
    return Number.isFinite(n) ? n : 0;
  }
  return 0;
};
const str = (v: unknown) => (typeof v === "string" ? v : "");

interface RawOutline {
  type?: unknown;
  text?: unknown;
  guide_id?: unknown;
  bitrate?: unknown;
  subtext?: unknown;
  formats?: unknown;
  children?: unknown;
}

/** Pull the outline list out of radiotime's {head, body:[…]} envelope. */
function pluckArray(body: unknown): unknown[] {
  if (Array.isArray(body)) return body;
  if (body && typeof body === "object") {
    const b = (body as { body?: unknown }).body;
    if (Array.isArray(b)) return b;
  }
  return [];
}

/**
 * radiotime sometimes nests playable rows under a group outline (a "Stations"
 * header with `children`), and returns them flat other times. Walk either shape
 * and collect only the leaf outlines.
 */
function collectOutlines(node: unknown): RawOutline[] {
  if (Array.isArray(node)) return node.flatMap(collectOutlines);
  if (node && typeof node === "object") {
    const o = node as RawOutline;
    if (Array.isArray(o.children)) return o.children.flatMap(collectOutlines);
    return [o];
  }
  return [];
}

function trim(raw: RawOutline): Station | null {
  // Only type:"audio" rows are stations; type:"link" rows are artist/genre
  // drill-downs (guide_id m…/g…) that don't play — drop them.
  if (str(raw.type) !== "audio") return null;
  const guide_id = str(raw.guide_id);
  if (!GUIDE_ID.test(guide_id)) return null;
  return {
    stationuuid: guide_id,
    name: str(raw.text).trim() || "Unnamed station",
    // Resolved lazily on play via /api/tunein/resolve — a search row is a
    // pointer, not a stream.
    url_resolved: "",
    country: "",
    countrycode: "",
    codec: str(raw.formats),
    bitrate: num(raw.bitrate),
    // The station's tagline / now-playing blurb; kept for future display.
    tags: str(raw.subtext),
    votes: 0,
    homepage: "",
  };
}

const cache = new Map<string, { at: number; stations: Station[] }>();

function cacheGet(key: string): Station[] | null {
  const hit = cache.get(key);
  if (!hit) return null;
  if (Date.now() - hit.at > CACHE_TTL_MS) {
    cache.delete(key);
    return null;
  }
  return hit.stations;
}

function cacheSet(key: string, stations: Station[]) {
  // Map preserves insertion order, so the first key is the oldest write.
  if (cache.size >= MAX_CACHE_ENTRIES) {
    const oldest = cache.keys().next();
    if (!oldest.done) cache.delete(oldest.value);
  }
  cache.set(key, { at: Date.now(), stations });
}

export async function GET(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  const q = (sp.get("q") ?? "").trim().slice(0, 100);

  const limitParam = Number.parseInt(sp.get("limit") ?? "", 10);
  const limit = Number.isFinite(limitParam)
    ? Math.min(Math.max(limitParam, 1), MAX_LIMIT)
    : DEFAULT_LIMIT;

  // TuneIn has no "top stations" query — an empty search means nothing to show.
  // The client already guards this; guard again so the route is safe alone.
  if (!q) return NextResponse.json({ stations: [] });

  const bridge = await checkBridgeSwitch("tunein");
  if (!bridge.open) return bridgeRefusalResponse(bridge);
  if (!bridge.hosts.includes(HOST)) {
    return NextResponse.json(
      { error: "radio catalog unreachable", detail: "host not on the tunein switch" },
      { status: 502 }
    );
  }

  const key = `${q.toLowerCase()}|${limit}`;
  const cached = cacheGet(key);
  if (cached) return NextResponse.json({ stations: cached, cached: true });

  const query = new URLSearchParams({
    query: q,
    types: "station",
    render: "json",
  });

  try {
    const res = await fetch(`https://${HOST}/Search.ashx?${query.toString()}`, {
      headers: {
        // radiotime asks callers to identify themselves; a browser fetch can't
        // set this, which is half the reason this proxy exists.
        "User-Agent": "PuffPAI/1.0",
        Accept: "application/json",
      },
      signal: AbortSignal.timeout(TIMEOUT_MS),
      cache: "no-store",
    });
    if (!res.ok) {
      return NextResponse.json(
        { error: "radio catalog unreachable", detail: `HTTP ${res.status}` },
        { status: 502 }
      );
    }

    const body: unknown = await res.json();
    const outlines = collectOutlines(pluckArray(body));

    // Dedup by guide_id — a search can list the same station twice (station +
    // its now-playing echo).
    const seen = new Set<string>();
    const stations: Station[] = [];
    for (const o of outlines) {
      const s = trim(o);
      if (!s || seen.has(s.stationuuid)) continue;
      seen.add(s.stationuuid);
      stations.push(s);
      if (stations.length >= limit) break;
    }

    cacheSet(key, stations);
    return NextResponse.json({ stations, cached: false });
  } catch (err) {
    // Legacy endpoint: fail loudly rather than as an empty result, so the UI
    // shows "catalog unreachable" instead of "no stations match".
    return NextResponse.json(
      {
        error: "radio catalog unreachable",
        detail: err instanceof Error ? err.message : "failed",
      },
      { status: 502 }
    );
  }
}
