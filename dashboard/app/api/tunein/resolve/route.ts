/**
 * /api/tunein/resolve — turn a TuneIn guide_id into a playable stream URL.
 *
 * radio-browser hands us url_resolved directly; TuneIn does not. A station
 * resolves via Tune.ashx to one or more stream options, and those are MORE
 * often a PLAYLIST (.pls/.m3u) than a direct stream. An <audio> element can't
 * play a .pls (it's a text file listing the real URL), so we:
 *   1. Tune.ashx?id=<guide_id> → candidate streams (url + is_direct).
 *   2. is_direct:true  → use url as-is.
 *   3. is_direct:false → fetch the playlist and extract the first stream URL
 *      (File1=… in .pls, first non-# line in .m3u).
 * HLS (.m3u8) is skipped everywhere — we ship a plain HTMLAudioElement with no
 * hls.js, exactly like World Radio.
 *
 * The Bridge: every resolve asks the backend whether the `tunein` switch is
 * open (app/_lib/server/bridgeSwitch.ts) before the cache and before any name
 * is looked up. Closed, or no clear answer, means a JSON refusal and nothing
 * sent.
 *
 * SSRF posture: the only user input is `id`, gated to ^s[0-9]+$, and the
 * Tune.ashx host is fixed. A playlist URL in TuneIn's answer is fetched only
 * when it is https on a host the `tunein` switch declares. A playlist on the
 * broadcaster's own server (the usual case) is skipped, not fetched: that
 * server is chosen by TuneIn's answer, is not on any switch, and could name an
 * address inside the house. The backend's TuneIn catalog skips it the same
 * way. The read is byte-capped so a mislabeled live stream can't pour into us.
 *
 * puff puff pass
 */
import { NextRequest, NextResponse } from "next/server";
import {
  bridgeRefusalResponse,
  checkBridgeSwitch,
} from "@/app/_lib/server/bridgeSwitch";

export const runtime = "nodejs";

const HOST = "opml.radiotime.com";
const TIMEOUT_MS = 8_000;
const PLAYLIST_TIMEOUT_MS = 6_000;
/** Playlists are a few hundred bytes; cap the read well above that. */
const PLAYLIST_MAX_BYTES = 64 * 1_024;
/** Resolutions are stable for a while, and re-resolving costs two round-trips. */
const CACHE_TTL_MS = 15 * 60 * 1_000;
const MAX_CACHE_ENTRIES = 128;

const GUIDE_ID = /^s[0-9]+$/;

const str = (v: unknown) => (typeof v === "string" ? v : "");
/** radiotime sends is_direct as a real boolean OR the string "true". */
const asBool = (v: unknown) => v === true || v === "true";
const isHttp = (u: string) => /^https?:\/\//i.test(u);
const isHls = (u: string) => /\.m3u8(\?|$)/i.test(u);
/** TuneIn serves a "not available in your region" clip for blocked stations. */
const isBlocked = (u: string) => /notcompatible/i.test(u);

/** True only for an https URL on one of `hosts`, on the default port. */
function onDeclaredHost(u: string, hosts: readonly string[]): boolean {
  try {
    const url = new URL(u);
    return (
      url.protocol === "https:" &&
      url.port === "" &&
      !url.username &&
      !url.password &&
      hosts.includes(url.hostname.toLowerCase())
    );
  } catch {
    return false;
  }
}

interface RawStream {
  url?: unknown;
  is_direct?: unknown;
  media_type?: unknown;
}

function pluckArray(body: unknown): unknown[] {
  if (Array.isArray(body)) return body;
  if (body && typeof body === "object") {
    const b = (body as { body?: unknown }).body;
    if (Array.isArray(b)) return b;
  }
  return [];
}

const cache = new Map<string, { at: number; url: string }>();

function cacheGet(id: string): string | null {
  const hit = cache.get(id);
  if (!hit) return null;
  if (Date.now() - hit.at > CACHE_TTL_MS) {
    cache.delete(id);
    return null;
  }
  return hit.url;
}

function cacheSet(id: string, url: string) {
  if (cache.size >= MAX_CACHE_ENTRIES) {
    const oldest = cache.keys().next();
    if (!oldest.done) cache.delete(oldest.value);
  }
  cache.set(id, { at: Date.now(), url });
}

/** Extract the first real stream URL from a .pls or .m3u playlist body. */
function parsePlaylist(text: string): string {
  const lines = text.split(/\r?\n/);
  // .pls → File1=http://…, File2=… — first playable File entry wins.
  const fileEntry = /^\s*File\d+\s*=\s*(\S+)\s*$/i;
  for (const line of lines) {
    const m = fileEntry.exec(line);
    if (m) {
      const u = m[1];
      if (isHttp(u) && !isHls(u) && !isBlocked(u)) return u;
    }
  }
  // .m3u → first non-comment, non-blank http(s) line.
  for (const line of lines) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    if (isHttp(t) && !isHls(t) && !isBlocked(t)) return t;
  }
  return "";
}

/**
 * Fetch a playlist body, reading at most PLAYLIST_MAX_BYTES. Redirects are not
 * followed: a hop off the declared host would be a host the switch never named.
 */
async function fetchPlaylist(url: string): Promise<string> {
  const res = await fetch(url, {
    headers: { "User-Agent": "PuffPAI/1.0" },
    signal: AbortSignal.timeout(PLAYLIST_TIMEOUT_MS),
    cache: "no-store",
    redirect: "error",
  });
  if (!res.ok || !res.body) return "";

  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  // Cap the read: if the URL was actually a live stream (no EOF), stop pulling
  // once we have enough bytes to have found any playlist entry.
  while (total < PLAYLIST_MAX_BYTES) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) {
      chunks.push(value);
      total += value.length;
    }
  }
  await reader.cancel().catch(() => {});

  const cap = Math.min(total, PLAYLIST_MAX_BYTES);
  const buf = new Uint8Array(cap);
  let off = 0;
  for (const c of chunks) {
    if (off >= cap) break;
    const slice = c.subarray(0, cap - off);
    buf.set(slice, off);
    off += slice.length;
  }
  return new TextDecoder().decode(buf);
}

export async function GET(req: NextRequest) {
  const id = (req.nextUrl.searchParams.get("id") ?? "").trim();
  if (!GUIDE_ID.test(id)) {
    return NextResponse.json({ error: "bad station id" }, { status: 400 });
  }

  const bridge = await checkBridgeSwitch("tunein");
  if (!bridge.open) return bridgeRefusalResponse(bridge);
  if (!bridge.hosts.includes(HOST)) {
    return NextResponse.json(
      { error: "station unreachable", detail: "host not on the tunein switch" },
      { status: 502 }
    );
  }

  const cached = cacheGet(id);
  if (cached) return NextResponse.json({ url_resolved: cached, cached: true });

  let candidates: RawStream[];
  try {
    const res = await fetch(
      `https://${HOST}/Tune.ashx?id=${encodeURIComponent(id)}&render=json`,
      {
        headers: { "User-Agent": "PuffPAI/1.0", Accept: "application/json" },
        signal: AbortSignal.timeout(TIMEOUT_MS),
        cache: "no-store",
      }
    );
    if (!res.ok) {
      return NextResponse.json(
        { error: "station unreachable", detail: `HTTP ${res.status}` },
        { status: 502 }
      );
    }
    const body: unknown = await res.json();
    candidates = pluckArray(body).filter(
      (c): c is RawStream => !!c && typeof c === "object"
    );
  } catch (err) {
    return NextResponse.json(
      {
        error: "station unreachable",
        detail: err instanceof Error ? err.message : "failed",
      },
      { status: 502 }
    );
  }

  // Prefer direct streams (no second round-trip); keep TuneIn's own order
  // within each group — it lists the most-reliable stream first.
  const ordered = [
    ...candidates.filter((c) => asBool(c.is_direct)),
    ...candidates.filter((c) => !asBool(c.is_direct)),
  ];

  for (const c of ordered) {
    const url = str(c.url);
    if (
      !isHttp(url) ||
      isHls(url) ||
      isBlocked(url) ||
      str(c.media_type).toLowerCase() === "hls"
    ) {
      continue;
    }
    if (asBool(c.is_direct)) {
      cacheSet(id, url);
      return NextResponse.json({ url_resolved: url, cached: false });
    }
    // Indirect → the url points at a .pls/.m3u. Unwrap it only when it sits
    // on a host the tunein switch declares; otherwise skip the candidate.
    if (!onDeclaredHost(url, bridge.hosts)) continue;
    const stream = parsePlaylist(await fetchPlaylist(url).catch(() => ""));
    if (stream) {
      cacheSet(id, stream);
      return NextResponse.json({ url_resolved: stream, cached: false });
    }
  }

  return NextResponse.json(
    { error: "no playable stream for this station" },
    { status: 502 }
  );
}
