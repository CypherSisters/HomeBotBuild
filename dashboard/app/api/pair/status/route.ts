/**
 * GET /api/pair/status — public. Tells a client whether it is already paired
 * and whether pairing is currently being enforced, so the UI can decide
 * between showing the app and showing the pairing prompt.
 *
 * Response: 200 {
 *   "paired": boolean,
 *   "enforce": "off" | "knowledge" | "all"
 * }
 *
 * No secrets: never returns the code or anything derived from the HMAC secret.
 */
import { NextRequest, NextResponse } from "next/server";
import { enforceMode, isPaired } from "@/app/_lib/server/pairing";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  return NextResponse.json(
    { paired: isPaired(req), enforce: enforceMode() },
    { headers: { "Cache-Control": "no-store" } },
  );
}
