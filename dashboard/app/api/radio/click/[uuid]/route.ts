/**
 * /api/radio/click/<stationuuid> — station click counter.
 *
 * radio-browser's ranking (and the "votes"/"clicktrend" ordering the search
 * route sorts by) is fed by clients reporting what they actually listened to.
 * Reporting back is the etiquette price of using a free volunteer catalog, so
 * the player fires this once per station start.
 *
 * Fire-and-forget by design: once the Bridge allows it we answer 204
 * immediately and let the upstream call finish (or fail) on its own. Nothing
 * about playback depends on it, so a dead mirror must never surface as a
 * player error.
 *
 * The Bridge: the report tells radio-browser which station this house is
 * playing, so it waits for the `radio_browser` switch like a search does
 * (app/_lib/server/bridgeSwitch.ts). Closed, or no clear answer, means a JSON
 * refusal and nothing sent. The appliance key is used only for that one fixed
 * question, so the route stays outside the middleware pairing matcher.
 *
 * puff puff pass
 */
import { NextResponse } from "next/server";
import {
  bridgeRefusalResponse,
  checkBridgeSwitch,
} from "@/app/_lib/server/bridgeSwitch";

export const runtime = "nodejs";

/** Contacted only if the `radio_browser` switch also names the host. */
const MIRRORS = ["all.api.radio-browser.info", "de1.api.radio-browser.info"];

const TIMEOUT_MS = 6_000;

/** radio-browser station uuids are plain hex-and-dash; nothing else is sent. */
const UUID_RE = /^[0-9a-fA-F-]{8,64}$/;

export async function POST(
  _req: Request,
  { params }: { params: Promise<{ uuid: string }> }
) {
  const { uuid } = await params;

  if (!UUID_RE.test(uuid)) {
    return new NextResponse(null, { status: 400 });
  }

  const bridge = await checkBridgeSwitch("radio_browser");
  if (!bridge.open) return bridgeRefusalResponse(bridge);
  const mirrors = MIRRORS.filter((h) => bridge.hosts.includes(h));

  // Not awaited: the response goes out now, the report lands when it lands.
  void (async () => {
    for (const host of mirrors) {
      try {
        const res = await fetch(`https://${host}/json/url/${uuid}`, {
          headers: { "User-Agent": "PuffPAI/1.0", Accept: "application/json" },
          signal: AbortSignal.timeout(TIMEOUT_MS),
          cache: "no-store",
        });
        // The body is never read, but an unconsumed response can pin its
        // undici connection until GC — drain it explicitly.
        void res.body?.cancel().catch(() => {});
        if (res.ok) return;
      } catch {
        /* mirror down — try the next, then give up silently */
      }
    }
  })();

  return new NextResponse(null, { status: 204 });
}
