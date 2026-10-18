/**
 * The owner's own apps — made, previewed, listed and deleted from the browser.
 *
 * `puffpai persona new` has written a persona from the terminal since the
 * scaffold landed. These four calls are the same thing for somebody who never
 * opens one, and every one of them goes to the SAME backend module, so the two
 * paths cannot end up validating different names or writing different files.
 *
 * WHY THE PREVIEW IS A FETCH AND NOT A FUNCTION
 * --------------------------------------------
 * The builder page shows the words the bot will understand before the owner
 * commits to them. Deriving that list here — lowercasing the name, splitting
 * the goal, dropping stopwords — would be a second implementation of rules that
 * live in Python, and the first time the two drifted the page would be showing
 * words the installed manifest does not carry. One truth, one round trip.
 *
 * The API key never reaches the browser: every call goes through /api/pai,
 * which attaches it server-side. This dashboard's proxy has no path allowlist,
 * so nothing had to be added there — but the backend requires the key on all
 * four of these, from every peer, loopback included.
 *
 * The same contract as BuildBot's personaService; `pack_taken` and
 * `suggested_id` are this build's additions (a shipped PACK can hold an id no
 * app holds).
 *
 * Copyright (c) Cypher Sisters. Apache-2.0 License.
 */

import { apiDelete, apiGet, apiPost } from "../api";

/** What `POST /persona/preview` and the `persona` block of a scaffold reply
 *  both carry. The paths are absolute, on the box, and are shown so an owner
 *  knows which two files are theirs to edit afterwards. */
export interface PersonaPreview {
  id: string;
  name: string;
  goal: string;
  blurb: string;
  /** Data keys: `harvest log` typed becomes `harvest_log` stored. */
  lists: string[];
  /** The same keys as the owner said them, for anything user-facing. */
  list_labels: string[];
  /** The words the router will match on once this is installed. */
  keywords: string[];
  examples: string[];
  says: string[];
  route: string;
  surfaces: string[];
  app_path: string;
  pack_path: string;
  /** An APP is already installed under this id. Not a refusal on its own — the
   *  page says so and stops offering Create. */
  taken: boolean;
  /** A PACK already claims this id. This build ships a pack called "homebot"
   *  that no app holds, so checking only `taken` would have let the owner press
   *  Create and be refused at the last step. */
  pack_taken: boolean;
  /** A free id the box would offer instead. */
  suggested_id: string;
}

/** One app the owner added, with what can be said to it. */
export interface OwnerApp {
  id: string;
  name: string;
  blurb: string;
  route: string;
  lists: string[];
  list_labels: string[];
  /** What the owner said this bot is FOR, when the backend records one.
   *  Absent on an older box — `personaGoal` falls back to the blurb. */
  goal?: string;
  /** One sentence that demonstrably works, e.g. "add tomatoes to the plantings
   *  list". Empty when the app keeps no lists. */
  try: string;
  /** True for a scaffolded persona with no list and no saved row. The Apps
   *  page and the home launcher leave it out. Absent on older backends. */
  empty?: boolean;
  /** True for the bot's own page (the place the build made for itself), not
   *  an app the owner added: it offers no "Remove this app". */
  own?: boolean;
}

export interface PersonaRecord {
  id: string;
  display_name: string;
  blurb: string;
  apps: string[];
  missing_apps: string[];
}

export interface PersonaListReply {
  personas: PersonaRecord[];
  apps: OwnerApp[];
  user_apps_dir: string;
  user_packs_dir: string;
}

export interface ScaffoldRequest {
  /** Optional: derived from the name when absent. */
  id?: string;
  name: string;
  goal?: string;
  lists?: string[];
}

export interface ScaffoldReply {
  written: boolean;
  persona: PersonaPreview & { installed: boolean; pack_installed: boolean };
  apps: string[];
  rescanned: boolean;
}

/** What went wrong, in the backend's own words. The refusals here are written
 *  for the owner ("That is 9 lists; 8 is as many as one scaffolded app
 *  carries"), so they are shown verbatim rather than translated. */
export function personaProblem(err: unknown): string {
  // After setup the backend refuses build changes without an unlocked vault
  // (409, error code "setup_complete"). The dashboard cannot pass the vault
  // session on this call yet, so point at the terminal, which is not behind it.
  if (err instanceof Error && err.message === "setup_complete") {
    return "Setup is finished, so personas are changed from the terminal on this " +
      "machine for now: run `puffpai persona new` (see `puffpai persona --help`).";
  }
  if (err instanceof Error && err.message) return err.message;
  return "The box did not answer, so nothing was written.";
}

/** The manifest that WOULD be written. Writes nothing. */
export function previewPersona(req: ScaffoldRequest): Promise<PersonaPreview> {
  return apiPost<PersonaPreview>("/persona/preview", req);
}

/** Write it, and rescan so it is installed before this promise resolves. */
export function scaffoldPersona(req: ScaffoldRequest): Promise<ScaffoldReply> {
  return apiPost<ScaffoldReply>("/persona/scaffold", req);
}

export function listPersonas(): Promise<PersonaListReply> {
  return apiGet<PersonaListReply>("/persona/list");
}

/**
 * The owner's own apps, or null when the box could not say.
 *
 * null is "I do not know", never "there are none": the callers use this to
 * decide what to SHOW, and a failed call must not make a persona vanish off the
 * home board as though it had been deleted.
 */
export async function getOwnerApps(): Promise<OwnerApp[] | null> {
  try {
    const reply = await listPersonas();
    return Array.isArray(reply.apps) ? reply.apps : null;
  } catch {
    return null;
  }
}

/**
 * Delete one persona the owner made, its pack and its store.
 *
 * The id is sent twice on purpose: once in the path as the target, once in the
 * body as the confirmation the owner typed. The backend refuses when they do
 * not match, and refuses again for anything that is not in the owner's own
 * apps directory — a shipped app is removed by changing the build.
 */
export function deletePersona(
  id: string,
  confirm: string,
): Promise<{ removed: boolean }> {
  return apiDelete<{ removed: boolean }>(
    `/persona/${encodeURIComponent(id)}`,
    { confirm },
  );
}
