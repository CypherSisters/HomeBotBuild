/**
 * POST /api/pair/devices/rename — relabel a paired device.
 *
 * Request:    { "id": "<deviceId>", "name": "Kitchen wall" }
 * Success:    200 { "ok": true }
 * Unknown id: 404 { "error": "not_found" }
 * Unpaired:   401 { "error": "pairing_required" }
 * Cross-site: 403 { "error": "cross_origin" }
 * Bad body:   400 { "error": "bad_request" }
 *
 * WHY THE ID TRAVELS IN THE BODY, NOT THE PATH
 * --------------------------------------------
 * A dynamic segment (/api/pair/devices/[id]/rename) would put attacker-supplied
 * bytes into the URL that the middleware matcher and the gate's path exemptions
 * both reason about. /api/pair/* is deliberately exempt from the pairing gate,
 * and every extra shape under that prefix is another chance for an encoded
 * separator or traversal to be read differently by the matcher than by the
 * router. Keeping ids in the body means the routing surface stays exactly the
 * two static paths it is today, and the id is validated once, here, against
 * DEVICE_ID_RE. It also keeps device ids out of access logs and Referer
 * headers.
 *
 * Same-origin is required: this is a state change, and SameSite=Lax alone does
 * not cover every browser/embedding case. The name is sanitised server-side
 * (control chars stripped, whitespace collapsed, 64-char cap) — it is rendered
 * back into the Settings screen, so it is never trusted as it arrives.
 */
import { NextRequest, NextResponse } from "next/server";
import {
  isPaired,
  isSameOrigin,
  renamePairedDevice,
} from "@/app/_lib/server/pairing";

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
  let name = "";
  try {
    const body = (await req.json()) as { id?: unknown; name?: unknown };
    if (typeof body?.id === "string") id = body.id;
    if (typeof body?.name === "string") name = body.name;
  } catch {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }
  if (!id) {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }
  // A name that sanitises to nothing (whitespace, control characters only) is a
  // 400 rather than a silent blank: an unnamed row in the device list is worse
  // than a rejected rename, because it is indistinguishable from a bug.
  const cleaned = name.replace(/[\x00-\x1f\x7f]/g, " ").trim();
  if (!cleaned) {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }

  try {
    // renamePairedDevice re-validates the id shape and re-sanitises the name;
    // the checks above are the trust boundary, that one is the invariant.
    if (!renamePairedDevice(id, name)) {
      return NextResponse.json(
        { error: "not_found" },
        { status: 404, headers: { "Cache-Control": "no-store" } },
      );
    }
    return NextResponse.json(
      { ok: true },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json({ error: "pairing_unavailable" }, { status: 500 });
  }
}
