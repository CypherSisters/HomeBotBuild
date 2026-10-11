/**
 * /api/pair/device-token — mint and list upload-only device tokens for an
 * iPhone Apple Shortcut. Paired devices only.
 *
 * POST  (paired, same-origin, rate-limited)
 *   body: { "label"?: string }
 *   200 { token: "pstk_…", id, label, expiresAt }   ← token shown ONCE
 *   401 { error: "pairing_required" }
 *   403 { error: "cross_origin" }
 *   429 { error: "rate_limited" }
 *
 * GET   (paired)
 *   200 { tokens: [ { id, label, scope, createdAt, expiresAt, lastUsedAt,
 *                     expired } ] }   ← metadata only, never raw tokens
 *   401 { error: "pairing_required" }
 *
 * This path lives under /api/pair/*, which the pairing gate always exempts, so
 * these handlers enforce their own cookie check via isPaired(). The minted
 * token authorises ONLY POST /api/pai/knowledge/upload (see deviceToken.ts);
 * it can never actuate the house, read a camera, or read the knowledge base.
 */
import { NextRequest, NextResponse } from "next/server";
import { isPaired, isSameOrigin } from "@/app/_lib/server/pairing";
import {
  isIssueRateLimited,
  issueDeviceToken,
  listDeviceTokens,
} from "@/app/_lib/server/deviceToken";

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
      { tokens: listDeviceTokens() },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json({ error: "unavailable" }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  // Cross-site state change → refuse. The Origin header is browser-set and
  // unforgeable by page JS, so this is a reliable CSRF guard. A tokenless
  // native client (no Origin) is allowed, matching isSameOrigin()'s contract.
  if (!isSameOrigin(req)) {
    return NextResponse.json({ error: "cross_origin" }, { status: 403 });
  }
  // Minting requires an already-paired cookie: a stranger on the LAN can never
  // issue a token.
  if (!isPaired(req)) {
    return NextResponse.json(
      { error: "pairing_required" },
      { status: 401, headers: { "Cache-Control": "no-store" } },
    );
  }
  if (isIssueRateLimited()) {
    return NextResponse.json(
      { error: "rate_limited" },
      { status: 429, headers: { "Retry-After": "60" } },
    );
  }

  let label = "";
  try {
    const body = (await req.json()) as { label?: unknown };
    if (typeof body?.label === "string") label = body.label;
  } catch {
    /* empty/invalid body is fine — label is optional */
  }

  try {
    const { raw, meta } = issueDeviceToken(label);
    // The raw token is returned exactly once and never persisted in clear.
    return NextResponse.json(
      {
        token: raw,
        id: meta.id,
        label: meta.label,
        expiresAt: meta.expiresAt,
      },
      { status: 201, headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return NextResponse.json({ error: "unavailable" }, { status: 500 });
  }
}
