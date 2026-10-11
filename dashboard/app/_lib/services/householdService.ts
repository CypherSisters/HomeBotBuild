/**
 * Household — the one identity source the dashboard reads.
 *
 * Before this existed the UI had three answers to "who is this": a Redux
 * default ("PuffPAI User"), a page-local fallback ("Operator") and whatever
 * GET /profile happened to return. The owner saved a name and still
 * saw two of the three placeholders. Everything identity-shaped now comes from
 * GET /household (the backend's roster, owner included) via useHousehold().
 *
 * All calls go through the same-origin /api/pai proxy — the browser never
 * holds the backend key. Avatar uploads are multipart and must NOT set a
 * Content-Type by hand: the browser writes the boundary, and the proxy relays
 * the body as a stream, untouched.
 *
 * puff puff pass
 */
import { API_URL, apiGet, apiPost, apiPut, apiDelete } from "../api";

export type HouseholdRole = "owner" | "adult" | "child" | "pet";

export interface HouseholdMember {
  id: string;
  display_name: string;
  role: HouseholdRole;
  emoji?: string | null;
  color?: string | null;
  /** Backend-relative, e.g. "/household/<id>/avatar", or null when unset. */
  avatar_url?: string | null;
  created?: string | null;
  last_active?: string | null;
  /**
   * Free-form per-member settings the backend merges rather than replaces.
   * `{}` when the member has none. The owner's already holds `workout_level`,
   * so every write here must be a patch of the keys it means to change.
   */
  preferences?: Record<string, unknown>;
}

export interface HouseholdPayload {
  members: HouseholdMember[];
  owner_id: string | null;
  /** Whose profile this screen is showing. Falls back to the owner. */
  active_id: string | null;
}

/** An image the owner has already sent to Puff (Send to Puff / documents). */
export interface IngestImage {
  upload_id: string;
  filename: string;
  /** Puff's label for the picture, when there is one. */
  title?: string;
  mime: string;
  size: number;
  uploaded_at?: string | null;
  /** Backend-relative, e.g. "/ingest/images/<id>/thumb". */
  thumb_url: string;
}

/** Roles the UI offers when adding or editing. "owner" is not assignable. */
export const ASSIGNABLE_ROLES: readonly HouseholdRole[] = ["adult", "child", "pet"];

export const MAX_AVATAR_BYTES = 8 * 1024 * 1024;
export const AVATAR_MIME = ["image/jpeg", "image/png", "image/webp"];

/**
 * Tell the rest of the client the roster changed. useAppData listens for this
 * on the calendar and fitfam stores, which render the household as `family`.
 */
function announceChange(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent("puffpai:household-changed"));
}

export async function getHousehold(): Promise<HouseholdPayload> {
  const res = await apiGet<HouseholdPayload>("/household");
  return {
    members: res?.members ?? [],
    owner_id: res?.owner_id ?? null,
    active_id: res?.active_id ?? null,
  };
}

/**
 * Patch one member's preferences. The backend MERGES the object (null on a key
 * removes it), which is the only safe shape here: the owner's preferences also
 * carry workout_level, and a whole-object PUT would drop it.
 */
export async function setMemberPreferences(
  id: string,
  preferences: Record<string, unknown>,
): Promise<void> {
  // The backend answers {member, success} here, not a bare member. Callers
  // refresh() rather than read the reply, so the shape is not modelled.
  await apiPut(`/household/${encodeURIComponent(id)}`, { preferences });
  announceChange();
}

/**
 * Whose profile this screen shows. The kiosk has no login, so "active" is how
 * a wall display knows which household member it belongs to right now - and,
 * through preferences.theme, which palette it wears.
 */
export async function setActiveMember(id: string): Promise<unknown> {
  const res = await apiPost("/household/active", { id });
  announceChange();
  return res;
}

export async function addMember(body: {
  display_name: string;
  role?: HouseholdRole;
  emoji?: string | null;
  color?: string | null;
}): Promise<HouseholdMember> {
  const m = await apiPost<HouseholdMember>("/household", body);
  announceChange();
  return m;
}

export async function updateMember(
  id: string,
  body: Partial<Pick<HouseholdMember, "display_name" | "role" | "emoji" | "color">>,
): Promise<HouseholdMember> {
  const m = await apiPut<HouseholdMember>(
    `/household/${encodeURIComponent(id)}`,
    body,
  );
  announceChange();
  return m;
}

export async function removeMember(id: string): Promise<unknown> {
  const res = await apiDelete(`/household/${encodeURIComponent(id)}`);
  announceChange();
  return res;
}

/**
 * Avatar endpoints. `target` is a member id, or "profile" for the owner alias
 * (/profile/avatar) — same shapes either way.
 */
function avatarBase(target: string): string {
  return target === "profile"
    ? `${API_URL}/profile/avatar`
    : `${API_URL}/household/${encodeURIComponent(target)}/avatar`;
}

/** Display URL for a member's picture, cache-busted by `bust`. */
export function avatarSrc(member: HouseholdMember, bust: number): string | null {
  if (!member.avatar_url) return null;
  return `${API_URL}${member.avatar_url}?v=${bust}`;
}

export function thumbSrc(image: IngestImage): string {
  return `${API_URL}${image.thumb_url}`;
}

/** Local guard so an 8 MB+ or wrong-type file never leaves the browser. */
export function checkAvatarFile(file: File): string | null {
  if (file.size > MAX_AVATAR_BYTES) return "That picture is larger than 8 MB.";
  if (file.type && !AVATAR_MIME.includes(file.type)) {
    return "Pictures must be JPEG, PNG or WebP.";
  }
  return null;
}

async function orThrow(res: Response): Promise<any> {
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.error || data?.message || res.statusText);
  return data;
}

export async function uploadAvatarFile(target: string, file: File): Promise<unknown> {
  const form = new FormData();
  form.append("file", file);
  // No Content-Type header on purpose — the browser writes the multipart
  // boundary, and the proxy forwards whatever Content-Type arrives.
  const res = await fetch(avatarBase(target), { method: "POST", body: form });
  return orThrow(res);
}

export async function setAvatarFromUpload(
  target: string,
  uploadId: string,
): Promise<unknown> {
  const res = await fetch(`${avatarBase(target)}/from-upload`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ upload_id: uploadId }),
  });
  return orThrow(res);
}

export async function clearAvatar(target: string): Promise<unknown> {
  const res = await fetch(avatarBase(target), { method: "DELETE" });
  return orThrow(res);
}

/** Images already uploaded to Puff, for the "choose from Puff's files" grid. */
export async function getIngestImages(): Promise<IngestImage[]> {
  const res = await apiGet<IngestImage[] | { images?: IngestImage[] }>("/ingest/images");
  if (Array.isArray(res)) return res;
  return res?.images ?? [];
}
