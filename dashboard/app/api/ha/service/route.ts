/**
 * /api/ha/service — proxies to PuffPAI API /ha/control or /ha/service
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

export async function POST(req: NextRequest) {
  // Household pairing gate (second layer behind middleware.ts). This is the
  // route that actuates the house — highest-value target of the three.
  const denied = requirePairing(req);
  if (denied) return denied;

  try {
    const body = await req.json();
    const { domain, service, data } = body;

    if (!domain || !service) {
      return NextResponse.json({ error: "domain and service are required" }, { status: 400 });
    }

    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (API_KEY) headers["Authorization"] = `Bearer ${API_KEY}`;

    const res = await fetch(`${PUFFPAI_API}/ha/service/${domain}/${service}`, {
      method: "POST",
      headers,
      body: JSON.stringify(data || {}),
      signal: AbortSignal.timeout(10000),
    });

    const result = await res.json();
    return NextResponse.json(result, { status: res.status });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
