"use client";

/**
 * World Radio favorites — the HOUSEHOLD's list, not this browser's.
 *
 * Favorites in localStorage alone would mean "PuffPAI, save this station"
 * and the star on this page keep two different lists. The backend
 * owns ONE list (mediahub_favorites_*), shared with voice, and localStorage is
 * demoted to an offline cache: it is what the appliance renders before the
 * first response lands, and what it keeps rendering when the backend is down.
 *
 * Everything here is pure logic plus a thin fetch, so the sync rules are
 * testable without React — see __tests__/radio-favorites.test.mjs.
 *
 * Two shapes meet in this file:
 *
 *   wire   what the backend stores: stationuuid "tunein:s12345" for TuneIn,
 *          a real uuid for radio-browser, plus `source` and `favicon`.
 *   local  what ./audio.ts plays: TuneIn rows keep their bare guide_id
 *          ("s12345"), which is how index.tsx tells the two catalogs apart.
 *
 * toWire()/fromWire() are the only places that translation happens.
 *
 * A TuneIn favorite with no resolved stream URL cannot be stored: the backend
 * refuses a row it could not play for voice. Such a row stays local-only and is
 * offered to the backend again on a later load, once its stream resolves.
 *
 * puff puff pass
 */

import type { Station } from "./audio";

export const FAVS_KEY = "puffpai_radio_favs";
/** Set once per browser, after its pre-backend favorites have been handed over. */
export const SYNCED_KEY = "puffpai_radio_favs_synced_v1";
export const FAVS_MAX = 40;

const ACTION_URL = "/api/pai/apps/mediahub/action";
const TIMEOUT_MS = 8000;

/* ── TuneIn id adapter ──────────────────────────────────────────────────── */

/** A bare TuneIn guide_id as the dashboard carries it: "s328971". */
export const isTuneInLocalId = (id: string): boolean => /^s\d+$/.test(id);
/** The backend's namespaced spelling of the same station. */
export const isTuneInWireId = (id: string): boolean => /^tunein:s\d+$/.test(id);

export function toWireId(id: string): string {
  return isTuneInLocalId(id) ? `tunein:${id}` : id;
}

export function toLocalId(id: string): string {
  return isTuneInWireId(id) ? id.slice("tunein:".length) : id;
}

/* ── station shapes ─────────────────────────────────────────────────────── */

/** Exactly the keys the backend accepts — an unlisted one is a 400. */
export interface WireStation {
  stationuuid: string;
  name: string;
  url_resolved: string;
  country: string;
  countrycode: string;
  codec: string;
  bitrate: number;
  tags: string;
  homepage: string;
  favicon: string;
  source: string;
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

/** True once a row has a stream the backend (and voice) could actually play. */
export function isStorable(s: Station): boolean {
  return /^https?:\/\//i.test(s.url_resolved || "");
}

/** Field whitelist + id/source mapping. Client-only keys (votes) are dropped. */
export function toWire(s: Station): WireStation {
  const extra = s as Station & { favicon?: unknown; source?: unknown };
  const tunein = isTuneInLocalId(s.stationuuid) || isTuneInWireId(s.stationuuid);
  return {
    stationuuid: toWireId(str(s.stationuuid)),
    name: str(s.name),
    url_resolved: str(s.url_resolved),
    country: str(s.country),
    countrycode: str(s.countrycode),
    codec: str(s.codec),
    bitrate: num(s.bitrate),
    tags: str(s.tags),
    homepage: str(s.homepage),
    favicon: str(extra.favicon),
    source: tunein ? "tunein" : str(extra.source) || "radio-browser",
  };
}

/**
 * A backend row as the player wants it. `votes` is display-only and unstored.
 *
 * The id is OPAQUE beyond the TuneIn prefix: a row the catalog gave no id of
 * its own comes back keyed "url:<16 hex>", and that is a perfectly good
 * favorite — it is kept as-is and handed back verbatim on remove.
 */
export function fromWire(row: unknown): Station | null {
  if (!row || typeof row !== "object") return null;
  const r = row as Record<string, unknown>;
  const id = toLocalId(str(r.stationuuid));
  const url = str(r.url_resolved);
  if (!id || !url) return null;
  return {
    stationuuid: id,
    name: str(r.name),
    url_resolved: url,
    country: str(r.country),
    countrycode: str(r.countrycode),
    codec: str(r.codec),
    bitrate: num(r.bitrate),
    tags: str(r.tags),
    votes: 0,
    homepage: str(r.homepage),
  };
}

/** Same station? Identity is the id the backend keys on, or the stream URL. */
export function sameStation(a: Station, b: Station): boolean {
  if (a.stationuuid && b.stationuuid) {
    return toWireId(a.stationuuid) === toWireId(b.stationuuid);
  }
  return !!a.url_resolved && a.url_resolved === b.url_resolved;
}

/* ── cache ──────────────────────────────────────────────────────────────── */

/**
 * The cached shape carries two private book-keeping fields the backend never
 * sees (toWire's whitelist drops both):
 *
 *   _syncAttempts  how often this row has been offered to the household list
 *                  and refused. Three strikes and we stop asking — a station
 *                  whose stream will not resolve must not cost a resolve plus
 *                  an add on every tick for the life of the appliance.
 *   _localOnly     this row was NEVER handed over (the household list was
 *                  full), so a later pass must keep it instead of treating its
 *                  absence from the backend as a removal.
 */
export interface CachedStation extends Station {
  _syncAttempts?: number;
  _localOnly?: boolean;
}

export const MAX_SYNC_ATTEMPTS = 3;

export function attemptsOf(s: Station): number {
  const n = (s as CachedStation)._syncAttempts;
  return typeof n === "number" && Number.isFinite(n) ? n : 0;
}

export function withAttempts(s: Station, n: number): CachedStation {
  return { ...s, _syncAttempts: n };
}

export const isLocalOnly = (s: Station): boolean =>
  (s as CachedStation)._localOnly === true;

export function loadCached(): Station[] {
  try {
    const raw = globalThis.localStorage.getItem(FAVS_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (s): s is Station =>
        !!s &&
        typeof s === "object" &&
        typeof (s as Station).stationuuid === "string" &&
        typeof (s as Station).url_resolved === "string"
    );
  } catch {
    return [];
  }
}

export function saveCached(favs: Station[]): void {
  try {
    globalThis.localStorage.setItem(FAVS_KEY, JSON.stringify(favs));
  } catch {
    /* private mode — favorites just won't persist */
  }
}

export function isFirstSyncDone(): boolean {
  try {
    return globalThis.localStorage.getItem(SYNCED_KEY) === "1";
  } catch {
    return false;
  }
}

export function markFirstSyncDone(): void {
  try {
    globalThis.localStorage.setItem(SYNCED_KEY, "1");
  } catch {
    /* nothing persists in private mode; the import just repeats */
  }
}

/* ── backend ────────────────────────────────────────────────────────────── */

export type RemoteResult =
  | {
      ok: true;
      favorites: Station[];
      max?: number;
      added?: number;
      skipped?: number;
      /** Import only: rows the backend declined to store at all. */
      ignored?: number;
      /** Import only: the cap was hit, so some rows were left behind. */
      full?: boolean;
    }
  | { ok: false; error: string; max?: number };

async function call(
  action: string,
  params: Record<string, unknown>
): Promise<RemoteResult> {
  let data: unknown = null;
  try {
    const res = await fetch(ACTION_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, params }),
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!res.ok) return { ok: false, error: "unreachable" };
    data = await res.json();
  } catch {
    return { ok: false, error: "unreachable" };
  }
  const env = data as { success?: boolean; result?: unknown } | null;
  if (!env || env.success === false) return { ok: false, error: "unreachable" };
  const result = (env.result ?? null) as Record<string, unknown> | null;
  if (!result || typeof result !== "object") {
    return { ok: false, error: "unreachable" };
  }
  if (result.ok !== true) {
    return {
      ok: false,
      error: str(result.error) || "refused",
      max: typeof result.max === "number" ? result.max : undefined,
    };
  }
  const rows = Array.isArray(result.favorites) ? result.favorites : [];
  const count = (v: unknown): number | undefined =>
    typeof v === "number" && Number.isFinite(v) ? v : undefined;
  return {
    ok: true,
    favorites: rows.map(fromWire).filter((s): s is Station => s !== null),
    max: count(result.max),
    added: count(result.added),
    skipped: count(result.skipped),
    ignored: count(result.ignored),
    full: result.full === true,
  };
}

export function fetchRemote(): Promise<RemoteResult> {
  return call("mediahub_favorites_list", {});
}

export function addRemote(station: Station): Promise<RemoteResult> {
  return call("mediahub_favorites_add", { station: toWire(station) });
}

/** By id when we have one, by stream URL otherwise — the backend accepts both. */
export function removeRemote(station: Station): Promise<RemoteResult> {
  const id = toWireId(str(station.stationuuid));
  return call(
    "mediahub_favorites_remove",
    id ? { id } : { url: str(station.url_resolved) }
  );
}

export function importLocal(stations: Station[]): Promise<RemoteResult> {
  return call("mediahub_favorites_import", { stations: stations.map(toWire) });
}

/** Resolve a TuneIn guide_id to a stream, so the row can be stored at all. */
export async function resolveTuneIn(station: Station): Promise<Station | null> {
  try {
    const res = await fetch(
      `/api/tunein/resolve?id=${encodeURIComponent(station.stationuuid)}`,
      { cache: "no-store", signal: AbortSignal.timeout(TIMEOUT_MS) }
    );
    const data = (await res.json().catch(() => null)) as {
      url_resolved?: unknown;
    } | null;
    const url = str(data?.url_resolved);
    if (!res.ok || !url) return null;
    return { ...station, url_resolved: url };
  } catch {
    return null;
  }
}

/* ── sync ───────────────────────────────────────────────────────────────── */

/**
 * The one-time handover: which of this browser's own favorites the household
 * list has never seen. Order is preserved, duplicates within `local` collapse,
 * and rows with no playable stream are left behind for the caller to resolve.
 */
export function mergeForFirstSync(local: Station[], remote: Station[]): Station[] {
  const seen = new Set(remote.map((s) => toWireId(s.stationuuid)));
  const out: Station[] = [];
  for (const s of local) {
    if (!isStorable(s)) continue;
    const id = toWireId(s.stationuuid);
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(s);
  }
  return out;
}

/**
 * Cached rows the household list does not have and that belong to this device:
 * a TuneIn station with no stream yet, or a row the full list refused to take.
 * Everything else missing from the backend was removed somewhere and goes.
 */
export function pendingLocalOnly(local: Station[], remote: Station[]): Station[] {
  const seen = new Set(remote.map((s) => toWireId(s.stationuuid)));
  return local.filter(
    (s) => !seen.has(toWireId(s.stationuuid)) && (!isStorable(s) || isLocalOnly(s))
  );
}

export interface SyncOutcome {
  /** null means "nothing learned" — the caller keeps the cache it is showing. */
  favorites: Station[] | null;
  /** Favorites that still live only here, appended after the household list. */
  localOnly: Station[];
  /** One line for the caller's inline label, when something was left behind. */
  note?: string;
}

export interface SyncOptions {
  /**
   * Offer pending local-only rows to the backend again. Off for the cheap
   * refresh a returning tab triggers: that pass is a read, not a repair.
   */
  retryPending?: boolean;
}

/**
 * One pass of the household sync, start to finish.
 *
 *  1. read the household list; any failure ends the pass with favorites: null,
 *     the cache untouched and the first-sync flag unset, so the next load
 *     tries again;
 *  2. once per browser, hand this browser's own favorites over (order kept)
 *     and set the flag ONLY after the backend has taken them;
 *  3. from then on the household list wins outright — a station saved by voice
 *     shows up, one removed anywhere disappears;
 *  4. a cached TuneIn favorite with no stream gets one resolve attempt per
 *     pass; if it resolves it joins the household list, otherwise it stays
 *     local-only and is tried again next time.
 */
export async function syncFavorites(
  cached: Station[],
  opts: SyncOptions = {}
): Promise<SyncOutcome> {
  const listed = await fetchRemote();
  if (!listed.ok) return { favorites: null, localOnly: [] };
  let remote = listed.favorites;
  let max = listed.max ?? FAVS_MAX;
  let note: string | undefined;
  const keptBack: Station[] = [];

  if (!isFirstSyncDone()) {
    const missing = mergeForFirstSync(cached, remote);
    if (missing.length > 0) {
      const imported = await importLocal(missing);
      if (!imported.ok) return { favorites: null, localOnly: [] };
      remote = imported.favorites;
      max = imported.max ?? max;
      // A household list already at the cap takes what fits and no more. The
      // rest are not lost and not silently dropped: they stay on this device,
      // marked so a later pass does not read their absence as a removal.
      const short =
        imported.added !== undefined &&
        imported.added < missing.length &&
        remote.length >= max;
      if (imported.full === true || short) {
        for (const s of missing) {
          if (!remote.some((r) => sameStation(r, s))) {
            keptBack.push({ ...s, _localOnly: true } as CachedStation);
          }
        }
        if (keptBack.length > 0) {
          note = `Favorites are full (${max}); some stayed on this device.`;
        }
      }
    }
    markFirstSyncDone();
  }

  const localOnly: Station[] = [...keptBack];
  for (const p of pendingLocalOnly(cached, remote)) {
    const attempts = attemptsOf(p);
    // One resolve + one add per row per pass, three passes at most, and only
    // when the caller asked for a repair pass.
    if (
      !opts.retryPending ||
      attempts >= MAX_SYNC_ATTEMPTS ||
      !isTuneInLocalId(p.stationuuid)
    ) {
      localOnly.push(p);
      continue;
    }
    const resolved = await resolveTuneIn(p);
    if (!resolved) {
      localOnly.push(withAttempts(p, attempts + 1));
      continue;
    }
    const added = await addRemote(resolved);
    if (added.ok) {
      remote = added.favorites;
      continue;
    }
    // A row the backend calls invalid will never become valid — stop asking.
    localOnly.push(
      withAttempts(
        p,
        added.error === "invalid_station" ? MAX_SYNC_ATTEMPTS : attempts + 1
      )
    );
  }

  return { favorites: remote, localOnly, note };
}
