/**
 * The apps this build carries, derived rather than retyped.
 *
 * The built-in membership list comes from the sidebar's own Apps submenu
 * (`app/dashboard/_lib/menuAside.ts`), so this page and the nav cannot disagree
 * about what exists. Anything the box reports from `GET /apps/installed` that is
 * NOT one of those is folded in as well, using the manifest's own name, blurb and
 * route — that is the whole point: an app the owner dropped into `apps/` shows up
 * here without anybody editing the dashboard.
 *
 * Descriptions come from each manifest's `blurb` when the box answered. The local
 * map below is the fallback for the built-ins, used when the backend has not
 * answered or cannot — the list fails OPEN, because hiding a working app is worse
 * than describing one in slightly stale words.
 */
import menuAside from "../../_lib/menuAside";
import type { InstalledApp } from "../../../_lib/services/installedAppsService";

/** The submenu label this reads. Kept as a constant so a nav rename is one edit. */
export const APPS_MENU_LABEL = "Apps";

export type AppEntry = {
  /** Route segment, e.g. "calendar". */
  id: string;
  label: string;
  href: string;
  icon?: string;
  /** One line, said plainly. Empty when nothing has been written yet. */
  description: string;
  /** True when this app has a hand-written page; false = generic page. */
  builtIn: boolean;
  /** Installed but cannot fully work on this box. */
  degradedReason?: string;
};

/** What each built-in app is for, used when the box has not described it. */
const DESCRIPTIONS: Record<string, string> = {
  alarmflow: "Alarms and timers, with the sound played on this box.",
  calendar: "Your events and to-dos on one month view.",
  freshtable: "Pantry, shopping list, meal plan and the recipes behind them.",
  fitfam: "Workouts per person, with streaks and a history that stays here.",
  trivia: "Question rounds and scores, for when the room needs something to do.",
};

function segmentOf(href: string): string {
  const parts = href.split("?")[0].split("/").filter(Boolean);
  return parts[parts.length - 1] ?? "";
}

/**
 * The apps to list, sidebar order first, then anything extra the box reports.
 *
 * @param installed what `GET /apps/installed` said, or null for "unknown" — in
 *                  which case only the built-ins are listed, described from the
 *                  map above. Never treat null as an empty box.
 */
export function listApps(installed: InstalledApp[] | null = null): AppEntry[] {
  const group = menuAside.find((item) => item.label === APPS_MENU_LABEL);
  const items = group?.menu ?? [];
  const byId = new Map((installed ?? []).map((app) => [app.id, app]));

  const entries: AppEntry[] = items
    .filter((item): item is typeof item & { href: string } => Boolean(item.href))
    .map((item) => {
      const id = segmentOf(item.href!);
      const manifest = byId.get(id);
      return {
        id,
        label: manifest?.name || item.label || id,
        href: item.href!,
        icon: item.icon,
        description: manifest?.blurb || DESCRIPTIONS[id] || "",
        builtIn: true,
        degradedReason: manifest?.degraded ? manifest.degraded_reason : undefined,
      };
    });

  // Apps the owner added. Their route comes from the manifest; when it declares
  // none, the generic page at /dashboard/apps/<id> is where it lives.
  const seen = new Set(entries.map((entry) => entry.id));
  for (const app of installed ?? []) {
    if (seen.has(app.id)) continue;
    seen.add(app.id);
    entries.push({
      id: app.id,
      label: app.name || app.id,
      href: app.route || `/dashboard/apps/${app.id}`,
      icon: undefined,
      description: app.blurb || "",
      builtIn: false,
      degradedReason: app.degraded ? app.degraded_reason : undefined,
    });
  }

  return entries;
}

// -- What the Apps page lists beyond the menu ----------------------------------

/** The prefix a scaffolded persona's stored blurb starts with. Kept on disk
 *  (the backend reads the goal back from it); never shown. */
const SCAFFOLD_PREFIX = "Scaffolded from your goal: ";

/** The one thing the Apps page needs to know about an owner-made app. */
export type PersonaFact = { id: string; empty?: boolean };

/** Something Toolsmith added, as GET /front sends it (see frontService). */
export type AddedApp = {
  id: string;
  name: string;
  line: string;
  href: string;
  ask_folder?: boolean;
};

/** Where an added thing with no page of its own is used: the home page. */
export const HOME_HREF = "/dashboard";
/** Said on an added thing that waits for a pictures folder. */
export const FOLDER_HINT = "Choose a pictures folder for it on the home page.";

/** The apps the Apps page lists.
 *
 *  `listApps(installed)` first, then:
 *  - a scaffolded persona with nothing in it (`empty` from GET /persona/list)
 *    is left out; the bot's own page and rail entry stay;
 *  - a persona's stored "Scaffolded from your goal: ..." blurb is replaced by
 *    a plain line (the goal is the owner's own words and stays off a subtitle);
 *  - every thing Toolsmith added (GET /front `apps`) that is not listed yet
 *    is added, so the Mood board is on this page too. One with no page of its
 *    own opens the home page, where its folder is chosen.
 *
 *  Pure: the page passes what its hooks returned, the proof passes fixtures.
 *  Null persona or front answers change nothing (fail open, as listApps). */
export function appsToList(
  installed: InstalledApp[] | null,
  personas: PersonaFact[] | null = null,
  added: AddedApp[] | null = null,
): AppEntry[] {
  const empty = new Set((personas ?? []).filter((p) => p.empty === true).map((p) => p.id));
  const out: AppEntry[] = listApps(installed)
    .filter((app) => !empty.has(app.id))
    .map((app) => (app.description.startsWith(SCAFFOLD_PREFIX)
      ? { ...app, description: `${app.label}'s own page, with the lists it keeps.` }
      : app));
  const listed = new Set(out.map((app) => app.href.split("?")[0]));
  const seen = new Set<string>();
  for (const a of added ?? []) {
    const name = (a.name || "").trim();
    if (!name) continue;
    const href = a.href || HOME_HREF;
    if (a.href && listed.has(a.href.split("?")[0])) continue;
    // The same thing added twice (same name, same place) is listed once.
    const key = `${name}|${href}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const line = (a.line || "").trim();
    out.push({
      id: a.id,
      label: name,
      href,
      description: !a.href && a.ask_folder ? `${line} ${FOLDER_HINT}`.trim() : line,
      builtIn: false,
    });
  }
  return out;
}
