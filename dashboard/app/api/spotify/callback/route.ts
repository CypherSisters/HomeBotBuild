/**
 * /api/spotify/callback — OAuth redirect target for "Connect Spotify".
 *
 * The Spotify developer app registers its redirect URI as exactly
 *   http://127.0.0.1:3003/api/spotify/callback
 * which is THIS dashboard (Next.js on :3003), not the Flask backend (:8000).
 * When the user authorizes on the appliance's own screen, Spotify sends the
 * browser here with ?code=…&state=…; this handler exchanges the code for a
 * session by calling the backend directly (server-side, so the same-origin
 * /api/pai proxy isn't reachable — we hit the backend origin like the other
 * app/api server routes do, e.g. app/api/media/status/route.ts), then bounces
 * the browser back to Settings with a flag the ConnectSpotifyCard reads.
 *
 * Defensive by construction: any failure ends in a redirect to Settings with
 * ?spotify=error, never a stack trace or a hung page.
 *
 * puff puff pass
 */
import { NextRequest, NextResponse } from "next/server";
import { requirePairing } from "@/app/_lib/server/pairing";

// Same base + key resolution as app/api/pai/[...path]/route.ts.
const BACKEND =
  process.env.PUFFPAI_BACKEND_URL ??
  process.env.PUFFPAI_API_URL ??
  "http://localhost:8000";
const API_KEY = process.env.PUFFPAI_API_KEY ?? "";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function settingsRedirect(req: NextRequest, query: string) {
  // Relative to the incoming origin (127.0.0.1:3003 on the appliance, or the
  // LAN host if a phone somehow lands here) so the bounce stays same-origin.
  return NextResponse.redirect(new URL(`/dashboard/settings?${query}`, req.url));
}

export async function GET(req: NextRequest) {
  // Household pairing gate (second layer behind middleware.ts). This route
  // carries the appliance's own PUFFPAI_API_KEY to the backend, so an unpaired
  // client must not be able to make it fire — 401 before `code` is even read.
  // Spotify's redirect here is a top-level GET navigation, which SameSite=Lax
  // still sends the device cookie on, so a paired browser passes the gate.
  const denied = requirePairing(req);
  if (denied) return denied;

  const params = req.nextUrl.searchParams;
  const code = params.get("code");
  const state = params.get("state") ?? undefined;
  const oauthError = params.get("error");

  // Spotify denied / user cancelled → surface its reason, don't call exchange.
  if (oauthError) {
    return settingsRedirect(
      req,
      `spotify=error&reason=${encodeURIComponent(oauthError)}`,
    );
  }

  if (!code) {
    return settingsRedirect(req, "spotify=error&reason=missing_code");
  }

  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (API_KEY) headers["Authorization"] = `Bearer ${API_KEY}`;

    const upstream = await fetch(`${BACKEND}/api/spotify/exchange`, {
      method: "POST",
      headers,
      cache: "no-store",
      body: JSON.stringify({ code, state }),
      signal: AbortSignal.timeout(15000),
    });

    let data: { ok?: boolean; error?: string } | null = null;
    try {
      data = await upstream.json();
    } catch {
      data = null;
    }

    if (upstream.ok && data?.ok) {
      return settingsRedirect(req, "spotify=connected");
    }

    const reason = data?.error ?? `exchange_failed_${upstream.status}`;
    return settingsRedirect(
      req,
      `spotify=error&reason=${encodeURIComponent(reason)}`,
    );
  } catch {
    // Backend unreachable / timeout — still land the user somewhere sane.
    return settingsRedirect(req, "spotify=error&reason=backend_unreachable");
  }
}
