/**
 * GET /api/pair/devices — list the household's paired devices.
 *
 * Paired:   200 {
 *   "devices": [{ id, name, createdAt, lastSeenAt }],
 *   "currentDeviceId": "<id>" | null
 * }
 * Unpaired: 401 { "error": "pairing_required" }
 *
 * Gated on the *cookie*, not on the enforcement stage, exactly like
 * /api/pair/code: who is in this household is household business, and must not
 * be readable by an unpaired LAN client even while PUFF_PAIRING_ENFORCE is
 * "off" or "knowledge". /api/pair/* is exempt from the middleware gate (so a
 * fresh device can still redeem a code), which is precisely why this route
 * checks isPaired itself.
 *
 * NEVER RETURNS COOKIE MATERIAL. The response carries device ids, which are
 * public handles used to rename and revoke — not credentials. The HMAC secret
 * and every cookie value stay on the server; there is no field here derived
 * from either.
 *
 * `currentDeviceId` is null when the caller holds a legacy v1 cookie, because a
 * v1 cookie genuinely does not identify a device. The UI shows a quiet note in
 * that case rather than guessing.
 */
import { NextRequest, NextResponse } from "next/server";
import {
  isPaired,
  listPairedDevices,
  pairedDeviceIdFor,
} from "@/app/_lib/server/pairing";

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
    return NextResponse.json(
      {
        devices: listPairedDevices(),
        currentDeviceId: pairedDeviceIdFor(req),
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json({ error: "pairing_unavailable" }, { status: 500 });
  }
}
