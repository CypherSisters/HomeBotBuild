/**
 * /api/ha/status — proxies to PuffPAI API /ha/status
 *
 * The dashboard frontend calls PuffPAI directly via haService.ts,
 * but this route exists for server-side rendering and backward compat.
 *
 * puff puff pass
 */
import { NextRequest, NextResponse } from "next/server";
import { requirePairing } from "@/app/_lib/server/pairing";

export const runtime = "nodejs";

const PUFFPAI_API =
  process.env.PUFFPAI_BACKEND_URL ??
  process.env.PUFFPAI_API_URL ??
  "http://localhost:8000";
const API_KEY = process.env.PUFFPAI_API_KEY ?? "";

export async function GET(req: NextRequest) {
  // Household pairing gate (second layer behind middleware.ts).
  const denied = requirePairing(req);
  if (denied) return denied;

  try {
    const headers: Record<string, string> = {};
    if (API_KEY) headers["Authorization"] = `Bearer ${API_KEY}`;

    const res = await fetch(`${PUFFPAI_API}/ha/status`, {
      headers,
      signal: AbortSignal.timeout(5000),
      cache: "no-store",
    });

    const data = await res.json();
    return NextResponse.json(data, { status: res.status });
  } catch {
    return NextResponse.json({ connected: false, error: "PuffPAI API unreachable" });
  }
}
