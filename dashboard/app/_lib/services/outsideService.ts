/**
 * The build engine's own outside services (Code downloads, Code search, the
 * book library, the skill shelves...), for the Bridge switches card in
 * Settings. Backend: src/puffpai/outside_api.py, through the /api/pai proxy
 * (paired screens only, at every stage).
 *
 *   GET  /outside                the list, in the backend's own words
 *   POST /outside/<id>/off       switch one off: nothing else is asked
 *   POST /outside/<id>/confirm   the backend's sentence + a one-time yes
 *   POST /outside/<id>/on        {"ticket"} switch it on with that yes
 *
 * ON needs this paired screen's yes (a consent ticket for exactly that switch,
 * used once, within ten minutes) and, after setup, the unlocked Vault: the
 * calls go through vaultFetch (apiPostRaw), which asks for the PIN when the
 * backend says the Vault is locked. OFF always works.
 *
 * No sentence about a service is written here: every line shown comes from
 * the backend.
 */

import { apiGetRaw, apiPostRaw } from "../api";
import { isPairingReply, PAIR_PAGE_WORDS } from "../pairingWords";
import { isVaultLockedReply, VAULT_NEEDED_LINE } from "../vaultSession";

/** Switch ids as the backend makes them. Stricter here on purpose. */
const SID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

export type OutsideService = {
  id: string;
  name: string;
  switch_name?: string;
  for: string;
  what_leaves: string;
  websites: string[];
  state: string;
  state_line: string;
  can_switch: boolean;
  switch_to: "on" | "off" | null;
  why_cannot_switch: string;
  turned_on_line?: string;
  kind: string;
};

export type OutsideList =
  | { ok: true; services: OutsideService[] }
  | { ok: false; message: string; pair: boolean };

export type SwitchResult = { ok: true } | { ok: false; message: string };

export type SwitchSentence =
  | { ok: true; sentence: string; ticket: string }
  | { ok: false; message: string };

function words(data: unknown, fallback: string): string {
  const d = (data && typeof data === "object" ? data : {}) as {
    message?: unknown;
    error?: unknown;
  };
  if (typeof d.message === "string" && d.message) return d.message;
  if (typeof d.error === "string" && d.error && d.error !== "pairing_required") return d.error;
  return fallback;
}

function text(v: unknown, max: number): string {
  return typeof v === "string" ? v.slice(0, max) : "";
}

function clean(raw: unknown): OutsideService | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.id !== "string" || !SID.test(r.id) || typeof r.name !== "string") return null;
  return {
    id: r.id,
    name: text(r.name, 120),
    switch_name: text(r.switch_name, 80) || undefined,
    for: text(r.for, 300),
    what_leaves: text(r.what_leaves, 400),
    websites: Array.isArray(r.websites)
      ? r.websites
          .filter((w): w is string => typeof w === "string")
          .slice(0, 8)
          .map((w) => w.slice(0, 120))
      : [],
    state: text(r.state, 20),
    state_line: text(r.state_line, 200),
    can_switch: r.can_switch === true,
    switch_to: r.switch_to === "on" || r.switch_to === "off" ? r.switch_to : null,
    why_cannot_switch: text(r.why_cannot_switch, 300),
    turned_on_line: text(r.turned_on_line, 120) || undefined,
    kind: text(r.kind, 20),
  };
}

export async function getOutsideServices(): Promise<OutsideList> {
  try {
    const res = await apiGetRaw<{ services?: unknown[] }>("/outside");
    if (!res.ok) {
      if (isPairingReply(res.status, res.data)) {
        return { ok: false, message: PAIR_PAGE_WORDS, pair: true };
      }
      return { ok: false, message: words(res.data, "Your bot did not list them."), pair: false };
    }
    const services = (Array.isArray(res.data?.services) ? res.data.services : [])
      .map(clean)
      .filter((s): s is OutsideService => s !== null);
    return { ok: true, services };
  } catch {
    return { ok: false, message: "Your bot did not answer. Try again in a moment.", pair: false };
  }
}

async function post(path: string, body: unknown, fallback: string) {
  try {
    const res = await apiPostRaw<Record<string, unknown>>(path, body);
    if (res.ok) return { ok: true as const, data: res.data };
    if (isPairingReply(res.status, res.data)) return { ok: false as const, message: PAIR_PAGE_WORDS };
    if (isVaultLockedReply(res.status, res.data)) return { ok: false as const, message: VAULT_NEEDED_LINE };
    return { ok: false as const, message: words(res.data, fallback) };
  } catch {
    return { ok: false as const, message: "Your bot did not answer. Try again in a moment." };
  }
}

/** Switch one off. Never asks anything first. */
export async function switchOutsideOff(id: string): Promise<SwitchResult> {
  if (!SID.test(id)) return { ok: false, message: "There is no outside service by that name." };
  const got = await post(`/outside/${id}/off`, undefined, "It could not be switched off. Try again.");
  return got.ok ? { ok: true } : got;
}

/** The backend's own sentence for switching one on, and the yes it hands out. */
export async function askToSwitchOn(id: string): Promise<SwitchSentence> {
  if (!SID.test(id)) return { ok: false, message: "There is no outside service by that name." };
  const got = await post(`/outside/${id}/confirm`, undefined, "It could not be switched on. Try again.");
  if (!got.ok) return got;
  const sentence = text(got.data?.sentence, 1200);
  const ticket = text(got.data?.ticket, 200);
  if (!sentence || !ticket) return { ok: false, message: "It could not be switched on. Try again." };
  return { ok: true, sentence, ticket };
}

/** Switch one on with the yes from askToSwitchOn, on this same screen. */
export async function switchOutsideOn(id: string, ticket: string): Promise<SwitchResult> {
  if (!SID.test(id)) return { ok: false, message: "There is no outside service by that name." };
  const got = await post(`/outside/${id}/on`, { ticket }, "It could not be switched on. Try again.");
  return got.ok ? { ok: true } : got;
}
