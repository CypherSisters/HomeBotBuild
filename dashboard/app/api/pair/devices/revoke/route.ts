/**
 * POST /api/pair/devices/revoke — un-pair a device.
 *
 * Request:    { "id": "<deviceId>" }
 * Success:    200 { "ok": true, "self": boolean }
 * Unknown id: 404 { "error": "not_found" }
 * Unpaired:   401 { "error": "pairing_required" }
 * Cross-site: 403 { "error": "cross_origin" }
 * Bad body:   400 { "error": "bad_request" }
 *
 * Removing the registry row is the revocation: the device's v2 cookie stops
 * verifying on its very next request, because v2 verification requires registry
 * membership. No restart, no secret change, no other device disturbed.
 *
 * ANY PAIRED DEVICE MAY REVOKE ANY OTHER, INCLUDING ITSELF
 * --------------------------------------------------------
 * This is the household trust model, and it already applies to POST
 * /api/pair/rotate, which lets any paired device invalidate the pairing code
 * for everyone. Inventing an admin tier here would be a lock with no key: there
 * is no account system, no password, and no second factor to hang it on, so the
 * "admin" would just be whichever cookie happened to exist first. Between
 * devices that all sit inside one home, the honest model is peers.
 *
 * Self-revoke additionally clears the cookie on the response, so the browser
 * stops sending a credential that is already dead. The clearing is a courtesy;
 * the authority is gone with the row whether or not the browser cooperates.
 *
 * A device still carrying a LEGACY v1 cookie cannot be revoked here, because a
 * v1 cookie identifies no device — see the v1 notes in pairing.ts. Middleware
 * upgrades v1 holders to v2 on their next matched request, so this is a
 * one-request window in practice, but it is real and the UI says so.
 *
 * The id travels in the body rather than a path segment for the reasons spelled
 * out in ../rename/route.ts: it keeps /api/pair/* to a fixed set of static
 * paths, so nothing about the gate's exemptions or the middleware matcher has
 * to reason about attacker-supplied URL bytes.
 */
import { NextRequest, NextResponse } from "next/server";
import {
  clearedDeviceCookieOptions,
  isPaired,
  isSameOrigin,
  pairedDeviceIdFor,
  revokePairedDevice,
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
  try {
    const body = (await req.json()) as { id?: unknown };
    if (typeof body?.id === "string") id = body.id;
  } catch {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }
  if (!id) {
    return NextResponse.json({ error: "bad_request" }, { status: 400 });
  }

  // Resolve "is this me?" BEFORE removing the row — afterwards the caller's own
  // cookie no longer verifies, so pairedDeviceIdFor() would report null and we
  // would skip clearing the very cookie we just invalidated.
  const self = pairedDeviceIdFor(req) === id;

  try {
    // revokePairedDevice re-validates the id shape and returns false for an
    // unknown id. Errors are deliberately not swallowed: reporting a revoke
    // that did not persist is the worst lie this endpoint could tell.
    if (!revokePairedDevice(id)) {
      return NextResponse.json(
        { error: "not_found" },
        { status: 404, headers: { "Cache-Control": "no-store" } },
      );
    }
  } catch {
    return NextResponse.json({ error: "pairing_unavailable" }, { status: 500 });
  }

  const res = NextResponse.json(
    { ok: true, self },
    { headers: { "Cache-Control": "no-store" } },
  );
  if (self) res.cookies.set(clearedDeviceCookieOptions());
  return res;
}
