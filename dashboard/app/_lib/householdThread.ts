/**
 * One brain, one conversation.
 *
 * Voice at the box, Home Assistant's voice assistant, this Chat page and the
 * Talk page all share ONE household thread on the appliance: `home-<principal>`
 * (today `home-owner`; the box cannot tell voices apart yet, so everyone in
 * the room shares it). The Chat page opens on it, "New chat" makes a side
 * topic with its own id, and older threads are listed under "Earlier".
 *
 * Pure (no imports), so app/_lib/__tests__/household-thread.test.mjs can load
 * it under Node's type stripping. The backend twin of `isListedThread` is
 * `_is_listed_thread` in api_server.py; keep the two in step.
 */

export const HOUSEHOLD_THREAD_ID = "home-owner";

export interface ThreadMessage {
  id?: string;
  role: string;
  content: string;
  timestamp?: string;
  surface?: string;
}

export interface ThreadLike {
  id: string;
  created_at?: string;
  messages?: ThreadMessage[];
}

const LISTED =
  /^(?:home-.+|voice-.+|[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;

export function isHouseholdThread(id: string | null | undefined): boolean {
  return !!id && id.startsWith("home-");
}

/** False for test-run threads (such as `*-smoke`): kept on disk, not listed. */
export function isListedThread(id: string | null | undefined): boolean {
  return !!id && LISTED.test(id);
}

/** The household thread first, then everything else listed, in the order given (newest first). */
export function groupThreads<T extends ThreadLike>(
  conversations: T[],
  householdId: string = HOUSEHOLD_THREAD_ID
): { household: T | null; earlier: T[] } {
  let household: T | null = null;
  const earlier: T[] = [];
  for (const c of conversations) {
    if (!isListedThread(c.id)) continue;
    if (c.id === householdId) household = c;
    else earlier.push(c);
  }
  return { household, earlier };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "voice-20261008" (or "voice-guest-20261008") -> "Voice, Oct 8"; anything else -> null. */
export function voiceDayLabel(id: string): string | null {
  const m = /^voice-(?:.+-)?(\d{4})(\d{2})(\d{2})$/.exec(id);
  if (!m) return null;
  const month = MONTHS[Number(m[2]) - 1];
  if (!month) return null;
  return `Voice, ${month} ${Number(m[3])}`;
}

/** Where the transcript tile's "Continue in chat" goes. */
export function chatThreadHref(id: string = HOUSEHOLD_THREAD_ID): string {
  return `/dashboard/chat?c=${encodeURIComponent(id)}`;
}

/** Newest message timestamp in a list, or "" when none carries one. */
export function latestTimestamp(messages: ThreadMessage[]): string {
  let best = "";
  for (const m of messages) {
    const t = m.timestamp ?? "";
    if (t > best) best = t;
  }
  return best;
}

/**
 * Of the messages the poll fetched (already newer than the last one seen),
 * the ones to append. A turn typed on THIS page is already on screen and the
 * server echoes it back; it is skipped by its user line, together with the
 * reply that follows it (whose stored copy may be scrubbed, so it cannot be
 * matched by text).
 */
export function newServerMessages<T extends ThreadMessage>(
  fetched: T[],
  local: ThreadMessage[],
  window = 12
): T[] {
  const recentUser = new Set(
    local
      .slice(-window)
      .filter((m) => m.role === "user")
      .map((m) => m.content.trim())
  );
  const out: T[] = [];
  let skipReply = false;
  for (const m of fetched) {
    if (m.role === "user" && recentUser.has(m.content.trim())) {
      recentUser.delete(m.content.trim());
      skipReply = true;
      continue;
    }
    if (m.role === "assistant" && skipReply) {
      skipReply = false;
      continue;
    }
    skipReply = false;
    out.push(m);
  }
  return out;
}

/** Give server messages stable React keys (the store does not always carry ids). */
export function withIds<T extends ThreadMessage>(messages: T[]): (T & { id: string })[] {
  return messages.map((m, i) => ({
    ...m,
    id: m.id || `srv-${m.timestamp ?? ""}-${m.role}-${i}`,
  }));
}
