/**
 * /api/radio/search — server-side proxy for the radio-browser.info station
 * catalog (the same public directory Home Assistant's "Radio Browser"
 * integration uses).
 *
 * Why a proxy at all: the dashboard's CSP keeps connect-src at 'self', so the
 * browser is never allowed to talk to a third-party API directly. Only the
 * AUDIO stream leaves the origin (media-src, see middleware.ts) — catalog
 * lookups come through here. It also lets us send the User-Agent that the
 * radio-browser API etiquette asks for, which a browser fetch cannot set.
 *
 * The Bridge: every request first asks the backend whether the
 * `radio_browser` switch is open (app/_lib/server/bridgeSwitch.ts), before the
 * cache and before any name is looked up. Closed, or no clear answer, means a
 * JSON refusal and nothing sent. The route uses the appliance key only for
 * that one fixed question and forwards nothing from the caller, so it stays
 * outside the middleware pairing matcher.
 *
 * Resilience: radio-browser is a volunteer mirror pool and individual mirrors
 * go dark regularly, so we walk a fallback list with a short timeout each and
 * memoise successful answers for 5 minutes (the catalog changes on the order
 * of days; the appliance re-asks the same handful of genre queries all day).
 * Only mirrors the `radio_browser` switch declares are contacted.
 *
 * puff puff pass
 */
import { NextRequest, NextResponse } from "next/server";
import {
  bridgeRefusalResponse,
  checkBridgeSwitch,
} from "@/app/_lib/server/bridgeSwitch";

export const runtime = "nodejs";

/**
 * Tried in order; first mirror to answer wins. A mirror is contacted only if
 * the `radio_browser` switch also names it, so this list can narrow the
 * registry's host list but never widen it.
 */
const MIRRORS = ["all.api.radio-browser.info", "de1.api.radio-browser.info"];

const TIMEOUT_MS = 6_000;
const CACHE_TTL_MS = 5 * 60 * 1_000;
const MAX_LIMIT = 50;
const DEFAULT_LIMIT = 30;
/** Bounded so a long uptime can't grow this without limit. */
const MAX_CACHE_ENTRIES = 64;

/** radio-browser's documented sort keys; anything else falls back to votes. */
const ORDERS = new Set([
  "votes",
  "clickcount",
  "clicktrend",
  "name",
  "bitrate",
  "random",
]);

/** The only fields the player needs — everything else is dropped at the edge. */
export interface Station {
  stationuuid: string;
  name: string;
  url_resolved: string;
  country: string;
  countrycode: string;
  codec: string;
  bitrate: number;
  tags: string;
  votes: number;
  homepage: string;
}

interface RawStation {
  stationuuid?: unknown;
  name?: unknown;
  url_resolved?: unknown;
  country?: unknown;
  countrycode?: unknown;
  codec?: unknown;
  bitrate?: unknown;
  tags?: unknown;
  votes?: unknown;
  homepage?: unknown;
  hls?: unknown;
}

const str = (v: unknown) => (typeof v === "string" ? v : "");
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : 0);

function trim(raw: RawStation): Station | null {
  const stationuuid = str(raw.stationuuid);
  const url_resolved = str(raw.url_resolved);
  // hls===1 needs hls.js to play; we ship plain HTMLAudioElement only, so an
  // HLS station would present as a station that simply never starts.
  if (!stationuuid || !url_resolved || num(raw.hls) === 1) return null;
  return {
    stationuuid,
    name: str(raw.name).trim() || "Unnamed station",
    url_resolved,
    country: str(raw.country),
    countrycode: str(raw.countrycode),
    codec: str(raw.codec),
    bitrate: num(raw.bitrate),
    tags: str(raw.tags),
    votes: num(raw.votes),
    homepage: str(raw.homepage),
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
  // Before the cache too: a closed switch serves nothing, not even rows it
  // fetched while it was open.
  const bridge = await checkBridgeSwitch("radio_browser");
  if (!bridge.open) return bridgeRefusalResponse(bridge);
  const mirrors = MIRRORS.filter((h) => bridge.hosts.includes(h));

  const sp = req.nextUrl.searchParams;

  const name = (sp.get("name") ?? "").trim().slice(0, 100);
  const tag = (sp.get("tag") ?? "").trim().toLowerCase().slice(0, 40);
  const orderParam = (sp.get("order") ?? "").trim().toLowerCase();
  const order = ORDERS.has(orderParam) ? orderParam : "votes";

  const limitParam = Number.parseInt(sp.get("limit") ?? "", 10);
  const limit = Number.isFinite(limitParam)
    ? Math.min(Math.max(limitParam, 1), MAX_LIMIT)
    : DEFAULT_LIMIT;

  const key = `${name.toLowerCase()}|${tag}|${order}|${limit}`;
  const cached = cacheGet(key);
  if (cached) {
    return NextResponse.json({ stations: cached, cached: true });
  }

  const query = new URLSearchParams({
    hidebroken: "true",
    order,
    reverse: "true",
    // Ask for extra headroom: the hls/url filter below drops rows locally.
    limit: String(Math.min(limit * 2, 100)),
  });
  if (name) query.set("name", name);
  if (tag) query.set("tag", tag);

  const errors: string[] = [];

  for (const host of mirrors) {
    try {
      const res = await fetch(
        `https://${host}/json/stations/search?${query.toString()}`,
        {
          headers: {
            // radio-browser asks every client to identify itself.
            "User-Agent": "PuffPAI/1.0",
            Accept: "application/json",
          },
          signal: AbortSignal.timeout(TIMEOUT_MS),
          cache: "no-store",
        }
      );
      if (!res.ok) {
        errors.push(`${host}: HTTP ${res.status}`);
        continue;
      }
      const body: unknown = await res.json();
      if (!Array.isArray(body)) {
        errors.push(`${host}: unexpected payload`);
        continue;
      }
      const stations = body
        .map((raw) => trim(raw as RawStation))
        .filter((s): s is Station => s !== null)
        .slice(0, limit);

      // A 200 whose rows ALL fail the trim filter is indistinguishable from a
      // broken mirror — don't cache that emptiness for 5 minutes. A genuinely
      // empty answer (zero raw rows) is a real result and caches fine.
      if (stations.length > 0 || body.length === 0) {
        cacheSet(key, stations);
      }
      return NextResponse.json({ stations, cached: false });
    } catch (err) {
      errors.push(`${host}: ${err instanceof Error ? err.message : "failed"}`);
    }
  }

  // Every mirror failed. Say so plainly rather than returning an empty list,
  // which the UI would otherwise render as "no stations match".
  return NextResponse.json(
    { error: "radio catalog unreachable", detail: errors },
    { status: 502 }
  );
}
