/**
 * GET /api/pair/code — reveal the current pairing code so an already-paired
 * device (the Settings screen, or the kiosk) can display it / render a QR for
 * pairing another household device.
 *
 * Paired:   200 { "code": "7XK4-P2M9", "rotatedAt": "<iso8601>",
 *                 "expiresAt": "<iso8601>" }
 * Unpaired: 401 { "error": "pairing_required" }
 *
 * Note this is gated on the *cookie*, not on the enforcement stage — the code
 * is a credential and must never be readable by an unpaired LAN client even
 * while PUFF_PAIRING_ENFORCE is "off" or "knowledge".
 *
 * Codes now expire (PAIR_CODE_TTL_SEC). getPairingCodeInfo() LAZILY ROTATES an
 * expired one before returning, so whatever this hands the UI is always
 * redeemable — a QR on the wall never goes stale under the household's nose.
 * That rotation is reachable only behind the isPaired gate below, so a stranger
 * cannot spin the code by hammering this endpoint.
 *
 * `expiresAt` is not sensitive (it is a timestamp, not a credential) and lets
 * the card re-fetch itself the moment the displayed code dies.
 */
import { NextRequest, NextResponse } from "next/server";
import { getPairingCodeInfo, isPaired } from "@/app/_lib/server/pairing";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!isPaired(req)) {
    return NextResponse.json(
      { error: "pairing_required" },
      { status: 401, headers: { "Cache-Control": "no-store" } },
    );
  }
  try {
    const info = getPairingCodeInfo();
    return NextResponse.json(
      {
        code: info.code,
        rotatedAt: info.rotatedAt,
        expiresAt: info.expiresAt,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json(
      { error: "pairing_unavailable" },
      { status: 500 },
    );
  }
}
