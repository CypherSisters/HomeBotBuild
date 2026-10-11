/**
 * The bot the owner built, in the rail — and the words the home card says
 * about it.
 *
 * A persona's only other home is the Apps submenu, which this rail does not
 * render, so without this it cannot be found in the dashboard. The persona
 * gets a rail entry of its own, named after the bot, straight after
 * Dashboard — the second thing in the rail, not the last.
 *
 * Everything here is the OWNER'S OWN DATA out of the owner's own manifest, via
 * GET /persona/list: the name, the goal, the list labels. Rendered as text and
 * escaped by React like any other string; the prompts are derived from those
 * same labels, so a suggestion can never name a list the app does not keep.
 *
 * Which personas a trimmed build LOADS is decided server-side (the
 * `persona.<id>` surface is on the backend's own allowlist), so nothing here
 * re-decides it — this file only renders what /persona/list served.
 *
 * Pure: no React, no fetch, so it can be read and reasoned about on its own.
 *
 * Copyright (c) Cypher Sisters. Apache-2.0 License.
 */

import { mdiStarFourPointsOutline } from "@mdi/js";
import type { MenuAsideItem } from "../_interfaces";
import type { OwnerApp } from "./services/personaService";

const APP_ROUTE_PREFIX = "/dashboard/apps/";
const MAX_APP_LABEL = 40;
/** What `persona scaffold` writes into a blurb when it had a goal to record. */
const GOAL_BLURB_PREFIX = "Scaffolded from your goal: ";
const MAX_GOAL_CHARS = 180;

/** Where this persona's own page lives. */
export function personaHref(app: OwnerApp): string {
  return app.route || `${APP_ROUTE_PREFIX}${app.id}`;
}

/** The bot's name as the rail should show it, bounded like any app label. */
export function personaLabel(app: OwnerApp): string {
  const name = (app.name || app.id).trim();
  return name.length > MAX_APP_LABEL ? name.slice(0, MAX_APP_LABEL) : name;
}

/** What this bot is FOR, in the owner's own words: the goal when the backend
 *  serves one, otherwise whatever the scaffolded blurb recorded. */
export function personaGoal(app: OwnerApp): string {
  const sent = (app.goal || "").trim();
  const blurb = (app.blurb || "").trim();
  const goal = sent
    ? sent
    : blurb.startsWith(GOAL_BLURB_PREFIX)
      ? blurb.slice(GOAL_BLURB_PREFIX.length).trim()
      : blurb;
  if (goal.length <= MAX_GOAL_CHARS) return goal;
  return `${goal.slice(0, MAX_GOAL_CHARS).replace(/\s+\S*$/, "")}…`;
}

/**
 * Three sentences worth saying to this bot.
 *
 * Built from the app's own list labels when it keeps any — those are the
 * owner's keys out of the owner's manifest, and they match the actions the
 * scaffold wrote, so every one of them really works. A persona with no lists
 * gets three generic openers instead of a suggestion about a list nobody
 * keeps: a "try saying" line that fails teaches people to distrust the rest.
 */
export function personaPrompts(app: OwnerApp): string[] {
  const labels = (app.list_labels?.length ? app.list_labels : app.lists) ?? [];
  const named = labels.map((l) => (l || "").trim()).filter(Boolean);
  if (named.length >= 3) {
    return named.slice(0, 3).map((l) => `what is on the ${l} list`);
  }
  if (named.length > 0) {
    const first = named[0];
    const out = [`what is on the ${first} list`, `add something to the ${first} list`];
    out.push(
      named.length > 1
        ? `what is on the ${named[1]} list`
        : `remove something from the ${first} list`,
    );
    return out;
  }
  return ["what are you for", "what can you help me with", "what do you know"];
}

/** The rail entries for the owner's personas, in the order served. */
export function personaNavItems(apps: OwnerApp[] | null): MenuAsideItem[] {
  if (!apps) return [];
  return apps.map((app) => ({
    label: personaLabel(app),
    icon: mdiStarFourPointsOutline,
    msIcon: "auto_awesome",
    href: personaHref(app),
    railGroup: "primary" as const,
  }));
}

/**
 * This dashboard's menu with the owner's personas added to the primary rail
 * group, straight after Dashboard.
 *
 * Deduplicated against entries that already RENDER in the rail (`railGroup`
 * set). The Apps submenu's children carry no railGroup — they are indexed by
 * QuickSearch and never drawn — so a persona listed there is still added here,
 * which is exactly why this exists.
 */
export function withPersonaNav(
  items: MenuAsideItem[],
  apps: OwnerApp[] | null,
): MenuAsideItem[] {
  const shown = new Set<string>();
  const walk = (list: MenuAsideItem[]) => {
    for (const item of list) {
      if (item.href && item.railGroup) shown.add(item.href.split("?")[0]);
      if (item.menu) walk(item.menu);
    }
  };
  walk(items);

  const added = personaNavItems(apps).filter(
    (item) => item.href && !shown.has(item.href.split("?")[0]),
  );
  if (added.length === 0) return items;

  const at = items.findIndex((item) => item.href === "/dashboard") + 1;
  return [...items.slice(0, at), ...added, ...items.slice(at)];
}
