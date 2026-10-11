/**
 * Which apps this box actually has installed.
 *
 * The dashboard ships hand-written pages for the five household apps, but an app
 * is really just a folder under `apps/` with an `app.json` in it — the owner can
 * add one, and the backend will serve its data through the generic
 * `/apps/<id>/data` endpoints. Nothing in the browser knew about those, so an
 * installed app was invisible: no index entry, and following a nav link built
 * from its manifest route was a 404.
 *
 * TWO ENDPOINTS, ON PURPOSE
 * -------------------------
 * `GET /apps/installed` is the one to use. On a backend that does not serve it
 * the request 404s, and the fallback is
 * `POST /apps/rescan`, which re-reads the `apps/` directory and answers with the
 * same list. The fallback is a POST that CHANGES something (it refreshes the
 * registry), which is why it is the fallback and not the primary: it is only
 * reached when the read-only door does not exist.
 *
 * Every failure path returns `null`, never `[]`. "I could not find out" and
 * "there are none" are different answers, and the UI treats null as unknown and
 * shows the built-in apps rather than claiming the box is empty.
 */
import { apiGet, apiPost } from "../api";

/** One app, as the backend describes it to a browser. */
export interface InstalledApp {
  id: string;
  name?: string;
  blurb?: string;
  route?: string;
  icon?: string;
  surfaces?: string[];
  /** Installed but cannot fully work here; `degraded_reason` says why. */
  degraded?: boolean;
  degraded_reason?: string;
  requires?: string[];
}

interface AppsAnswer {
  apps?: InstalledApp[];
  installed?: InstalledApp[];
}

/** Both endpoints may name the list differently; accept either spelling. */
function listOf(data: AppsAnswer | null): InstalledApp[] | null {
  const raw = data?.apps ?? data?.installed;
  if (!Array.isArray(raw)) return null;
  return raw.filter((app) => app && typeof app.id === "string");
}

/**
 * The installed apps, or null when the box cannot say.
 *
 * Callers must treat null as "unknown" and fail open. An app list is used to
 * decide what to SHOW, so guessing "none" hides working apps.
 */
export async function getInstalledApps(): Promise<InstalledApp[] | null> {
  try {
    return listOf(await apiGet<AppsAnswer>("/apps/installed"));
  } catch (err) {
    // apiGet throws the backend's message; a 404 is "this build has no such
    // endpoint", which is the one case worth a second attempt.
    const message = String(err instanceof Error ? err.message : err).toLowerCase();
    const missing =
      message.includes("not found") || message.includes("404") || message.includes("no route");
    if (!missing) return null;
    try {
      return listOf(await apiPost<AppsAnswer>("/apps/rescan"));
    } catch {
      return null;
    }
  }
}
