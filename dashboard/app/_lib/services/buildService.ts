// Proof it works — what this build's own checks found.
//
// /system/status reports runtime health and knows nothing about whether the
// build was ever proved. GET /build/prove answers the other question: what the
// wizard's static checks found, and what the running bot actually answered when
// `puffpai prove` asked it. Read-only.
//
// Every call goes through the same-origin proxy at /api/pai, which attaches
// this box's backend key server-side behind the pairing gate; nothing
// key-shaped ships to the browser. The proxy forwards any backend path, so
// there is no allowlist entry to add for /build/prove.
//
// "The build changed" lives in ./buildEvents, which this dashboard already had.
//
// Copyright (c) Cypher Sisters. Apache-2.0 License.

import { setupCodeHeaders } from "./setupService";

/** Same-origin proxy. Relative by design: no backend host in the bundle. */
const BASE = "/api/pai";

export interface ProveCheck {
  id: string;
  title: string;
  /** "pass" | "fail" | "gap" | "not_applicable" */
  result: string;
  reason: string;
}

export interface ProveGap {
  what: string;
  why: string;
  impact: string;
  /** Rendered by the backend from a registered command id — never free text
   *  supplied by a package, and never assembled in the browser. */
  fix_command: string;
}

export interface ProveLive {
  ran: boolean;
  at: string | null;
  note: string;
  /** "owner_confirmed" | "owner_refuted" | "owner_unproven" */
  owner: string;
  dashboard_url: string;
  checks: ProveCheck[];
  transcript: { question: string; reply: string; verdict: string }[];
}

export interface ProveRecord {
  ran: boolean;
  verdict: string;
  static_at?: string | null;
  bot_name?: string;
  checks: ProveCheck[];
  gaps: ProveGap[];
  live: ProveLive | null;
  message?: string;
}

/** The last proof, or null when the backend did not answer. Null is not a
 *  pass: callers render nothing rather than claiming anything. */
export async function getBuildProve(): Promise<ProveRecord | null> {
  try {
    const res = await fetch(`${BASE}/build/prove`, {
      method: "GET",
      // While first-run setup is open the backend covers /build/prove* with the
      // console-only setup code, the same way it covers /setup/*; absent once
      // setup completes. See setupService.setupCodeHeaders().
      headers: { "Content-Type": "application/json", ...setupCodeHeaders() },
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as ProveRecord;
  } catch {
    return null;
  }
}
