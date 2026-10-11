/**
 * POST /api/pair/device-token/revoke — revoke a device token by id. Paired,
 * same-origin only.
 *
 *   body: { "id": "<token id>" }
 *   200 { revoked: boolean }        ← false if no such id (idempotent)
 *   400 { error: "bad_request" }
 *   401 { error: "pairing_required" }
 *   403 { error: "cross_origin" }
 *
 * Revocation deletes the stored hash, so the raw token stops authorising
 * uploads immediately (verifyDeviceToken reads the file fresh every call).
 */
import { NextRequest, NextResponse } from "next/server";
import { isPaired, isSameOrigin } from "@/app/_lib/server/pairing";
import { revokeDeviceToken } from "@/app/_lib/server/deviceToken";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  if (!isSameOrigin(req)) {
    return NextResponse.json({ error: "cross_origin" }, { status: 403 });
  }
  if (!isPaired(req)) {
    return NextResponse.json(
      { error: "pairing_required" },
      { status: 401, headers: { "Cache-Control": "no-store" } },
    );
  }

  let id = "";
  try {
    const body = (await req.json()) as { id?: unknown };
    if (typeof body?.id === "string") id = body.id;
  } catch {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }
  if (!id) {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }

  try {
    return NextResponse.json(
      { revoked: revokeDeviceToken(id) },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json({ error: "unavailable" }, { status: 500 });
  }
}
