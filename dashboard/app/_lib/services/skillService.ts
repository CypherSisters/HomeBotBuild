/**
 * Skills — the sandboxed adapters the owner approves one at a time.
 *
 * A skill is a tiny package (manifest + `skill.py` + canned test fixtures)
 * that runs in the same bubblewrap jail as `workspace_exec` and reaches
 * exactly one approved host set through one egress door. Nothing here decides
 * anything: install happens only through the Sentinel consent queue, and this
 * module reads the result and carries the owner's off/remove verdicts.
 *
 * THE BYTES ARE THE POINT. `GET /skills/<id>/draft` reads each file and hashes
 * what it read, in the same breath, so the text shown and the hash beside it
 * cannot be two different reads. Anything rendering that payload must render
 * it as TEXT — never as markdown, never through a highlighter that could drop
 * or fold a character — or the approval stops being an approval of bytes.
 *
 * puff puff pass
 */

import { apiGet, apiPost, apiDelete } from "../api";

// ---------------------------------------------------------------------------
// Types — mirror of puffpai.skills.dispatch.listing() / install.draft_bytes()
// ---------------------------------------------------------------------------

export interface SkillPermissions {
  network_hosts: string[];
  files: string[];
  ha_domains: string[];
  secrets: string[];
}

export interface SkillManifest {
  schema: string;
  id: string;
  name: string;
  description: string;
  version: number;
  author: string;
  triggers: string[];
  permissions: SkillPermissions;
  limits: { timeout_s: number; max_requests: number; max_response_bytes: number };
  source_urls?: string[];
}

/** One installed skill, as `GET /skills` reports it. */
export interface SkillRow {
  id: string;
  name: string;
  description: string;
  triggers: string[];
  hosts: string[];
  domains: string[];
  enabled: boolean;
  /** False when the installed files no longer hash to what was approved. */
  intact: boolean;
  runs: number;
  last_error: string;
  last_refusal: string;
  tested: boolean;
  author: string;
  version: number;
  /** Not in every build; rendered only when the backend sends it. */
  installed_at?: string;
  sha256?: Record<string, string>;
}

export interface SkillsListing {
  skills: SkillRow[];
  /** Whether skills can run at all here (bubblewrap present, not env-disabled). */
  enabled: boolean;
  /** Why not, when `enabled` is false. */
  reason: string;
}

/** One file of a package, with the hash of the bytes that were just read. */
export interface SkillFile {
  name: string;
  bytes: number;
  sha256: string;
  text: string;
}

export interface SkillDraft {
  ok: boolean;
  id: string;
  manifest: SkillManifest;
  files: SkillFile[];
  lint: string[];
  hosts: string[];
  domains: string[];
  state: string;
}

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

export async function getSkills(): Promise<SkillsListing> {
  const data = await apiGet<{ skills?: SkillRow[]; enabled?: boolean; reason?: string }>(
    "/skills",
    { cache: "no-store" }
  );
  return {
    skills: Array.isArray(data?.skills) ? data.skills : [],
    enabled: data?.enabled !== false,
    reason: data?.reason || "",
  };
}

/**
 * The exact hashed bytes of a package.
 *
 * The backend serves this out of the DRAFTS tree, so it answers for a skill
 * awaiting approval and 404s once that skill is installed and its folder has
 * moved. Callers must handle "no code view" rather than treating it as an
 * error — see `SkillCodeModal`, which says so in words.
 */
export async function getSkillDraft(skillId: string): Promise<SkillDraft> {
  return apiGet<SkillDraft>(`/skills/${encodeURIComponent(skillId)}/draft`, {
    cache: "no-store",
  });
}

// ---------------------------------------------------------------------------
// Verdicts
// ---------------------------------------------------------------------------

/**
 * Switch a skill off, or back on.
 *
 * Off is `POST /skills/<id>/disable`: the skill stays installed and its egress
 * door stays registered and shut. On posts to `/skills/<id>/enable`, which is
 * what the appliance's own spoken copy tells the owner to do here ("say enable
 * it on the dashboard"); a build without that route reports its refusal
 * inline rather than pretending the flip happened.
 */
export async function setSkillEnabled(
  skillId: string,
  enabled: boolean
): Promise<{ ok: boolean; id?: string; enabled?: boolean }> {
  const verb = enabled ? "enable" : "disable";
  return apiPost(`/skills/${encodeURIComponent(skillId)}/${verb}`);
}

/** Remove a skill: the row goes, the door goes, the code is set aside. */
export async function removeSkill(
  skillId: string
): Promise<{ ok: boolean; id?: string; removed?: boolean }> {
  return apiDelete(`/skills/${encodeURIComponent(skillId)}`);
}

// ---------------------------------------------------------------------------
// Naming
// ---------------------------------------------------------------------------

/**
 * Suffixes that are one label too short to be the registrable domain. Same
 * list as `puffpai.skills.install._MULTI_SUFFIXES` — kept in step deliberately
 * so the domain the owner reads on the card is the domain the backend names.
 */
const MULTI_SUFFIXES = new Set([
  "co.uk", "org.uk", "ac.uk", "gov.uk", "co.jp", "com.au", "net.au",
  "org.au", "co.nz", "com.br", "co.in", "com.mx", "co.za",
]);

/** `api.wheretheiss.at` -> `wheretheiss.at`: the part a person recognises. */
export function registrableDomain(host: string): string {
  const labels = String(host || "")
    .trim()
    .toLowerCase()
    .replace(/^\.+|\.+$/g, "")
    .split(".")
    .filter(Boolean);
  if (labels.length < 2) return labels.join(".");
  if (labels.length >= 3 && MULTI_SUFFIXES.has(labels.slice(-2).join("."))) {
    return labels.slice(-3).join(".");
  }
  return labels.slice(-2).join(".");
}

/** `api.wheretheiss.at` split into the muted prefix and the bold domain. */
export function splitHost(host: string): { prefix: string; domain: string } {
  const domain = registrableDomain(host);
  const h = String(host || "").trim().toLowerCase();
  const prefix = h.endsWith(domain) ? h.slice(0, h.length - domain.length) : "";
  return { prefix, domain };
}

/** First 12 hex characters — enough to compare by eye, honest about being short. */
export function shortSha(sha: string): string {
  return String(sha || "").slice(0, 12);
}
