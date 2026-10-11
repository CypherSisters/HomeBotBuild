/**
 * POST /api/pair — redeem the household pairing code for a device cookie.
 *
 * Request:  { "code": "7XK4-P2M9" }   (dashes/spaces/case optional)
 * Success:  200 { "paired": true }    + Set-Cookie: puff_device=...
 * Failure:  401 { "paired": false, "error": "invalid_code" }
 *           429 { "paired": false, "error": "rate_limited" }
 *           403 { "paired": false, "error": "cross_origin" }
 *           400 { "paired": false, "error": "bad_request" }
 *
 * There is deliberately NO `GET /api/pair/redeem?code=...` variant. A GET that
 * mints an auth cookie from a query parameter is a state-changing GET: the
 * code would land in browser history, Referer headers and any access log, and
 * it would be reachable by cross-site top-level navigation. The QR deep-link
 * should open a page that POSTs here instead.
 */
import { NextRequest, NextResponse } from "next/server";
import {
  addPairedDevice,
  deviceCookieOptions,
  isRateLimited,
  isSameOrigin,
  issueDeviceCookieValueV2,
  clearPairFailures,
  recordPairFailure,
  verifyCode,
} from "@/app/_lib/server/pairing";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  if (!isSameOrigin(req)) {
    return NextResponse.json(
      { paired: false, error: "cross_origin" },
      { status: 403 },
    );
  }

  // Rate-limit BEFORE doing any comparison so a limited client learns nothing.
  if (isRateLimited(req)) {
    return NextResponse.json(
      { paired: false, error: "rate_limited" },
      { status: 429, headers: { "Retry-After": "60" } },
    );
  }

  let code = "";
  try {
    const body = (await req.json()) as { code?: unknown };
    if (typeof body?.code === "string") code = body.code;
  } catch {
    return NextResponse.json(
      { paired: false, error: "bad_request" },
      { status: 400 },
    );
  }
  if (!code) {
    return NextResponse.json(
      { paired: false, error: "bad_request" },
      { status: 400 },
    );
  }

  let ok = false;
  try {
    ok = verifyCode(code);
  } catch {
    // State unreadable/unwritable — fail closed rather than pair anyone.
    return NextResponse.json(
      { paired: false, error: "pairing_unavailable" },
      { status: 500 },
    );
  }

  if (!ok) {
    recordPairFailure(req);
    return NextResponse.json(
      { paired: false, error: "invalid_code" },
      { status: 401 },
    );
  }

  clearPairFailures(req);

  // Mint the registry row FIRST, then sign a v2 cookie over its id. Order
  // matters: a cookie whose device id is not in the registry fails membership
  // on its very next use, so a device would appear to pair and then 401 on
  // everything. If the registry cannot be written we fail the pairing outright
  // rather than hand out a credential we know is already dead.
  //
  // The default name is deliberately generic — it is renameable from Settings,
  // and guessing at an identity from the User-Agent would be both unreliable
  // and a small piece of fingerprinting we have no reason to store.
  let cookieValue: string;
  try {
    const device = addPairedDevice("New device");
    cookieValue = issueDeviceCookieValueV2(device.id);
  } catch {
    return NextResponse.json(
      { paired: false, error: "pairing_unavailable" },
      { status: 500 },
    );
  }

  const res = NextResponse.json(
    { paired: true },
    { status: 200, headers: { "Cache-Control": "no-store" } },
  );
  res.cookies.set(deviceCookieOptions(cookieValue));
  return res;
}
