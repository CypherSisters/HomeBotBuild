/**
 * /api/media/status — health pre-flight for the embedded Jellyfin player.
 *
 * Server-side pings the loopback media-proxy (instrumentation.ts, localhost:8096
 * → Pi). The proxy silently destroys sockets when the Pi is unreachable, so a
 * browser-side fetch would hang; doing it here with a short AbortSignal timeout
 * lets us fail fast and drive the media-page error UI.
 *
 * /System/Info/Public is Jellyfin's unauthenticated liveness endpoint.
 *
 * ?service=books asks the same question of the books proxy (localhost:8083 →
 * Calibre-web). Any answer below 500 (Calibre-web redirects to /login) means
 * something is there; a refused or timed-out connect means "not connected",
 * which is what a box without Calibre-web must show instead of a blank frame.
 *
 * puff puff pass
 */
import { NextResponse } from "next/server";

const JELLYFIN_URL =
  process.env.NEXT_PUBLIC_JELLYFIN_URL ?? "http://localhost:8096";

const CALIBRE_URL =
  process.env.NEXT_PUBLIC_CALIBRE_URL ?? "http://localhost:8083";

export async function GET(req: Request) {
  if (new URL(req.url).searchParams.get("service") === "books") {
    try {
      const res = await fetch(`${CALIBRE_URL}/`, {
        signal: AbortSignal.timeout(3000),
        cache: "no-store",
        redirect: "manual",
      });
      return NextResponse.json({ ok: res.status < 500 });
    } catch {
      return NextResponse.json({ ok: false });
    }
  }
  try {
    const res = await fetch(`${JELLYFIN_URL}/System/Info/Public`, {
      signal: AbortSignal.timeout(3000),
      cache: "no-store",
    });

    if (!res.ok) {
      return NextResponse.json({ ok: false });
    }

    const data = await res.json();
    return NextResponse.json({
      ok: true,
      serverName: data?.ServerName,
      version: data?.Version,
    });
  } catch {
    return NextResponse.json({ ok: false });
  }
}
