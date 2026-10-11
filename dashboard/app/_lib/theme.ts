/**
 * Theme — one resolver for "which palette is this screen wearing".
 *
 * Three themes ship: Cloud Pink (the original day
 * look), Deep Plum (calm dark, only the tile halos carry colour)
 * and Soft Light (near-white with a coral breath). More later; the picker
 * reads THEMES, so adding a fourth is a palette in css/_theme.css plus a row
 * here.
 *
 * SOURCE OF TRUTH, strictest first:
 *   1. preferences.theme on the ACTIVE household profile (the kiosk follows
 *      whoever is active, which is what "use on this screen" sets)
 *   2. localStorage['puff-theme-override'] — the per-device choice, and also
 *      the cache of (1) so the next boot paints the right palette with no
 *      flash before the roster arrives
 *   3. "auto": Cloud Pink 07:00-18:59, Deep Plum otherwise
 *
 * "night" is the retired fourth palette. It stays defined in the CSS so old
 * bookmarks and ?puffTheme=night keep rendering something sane, but every
 * stored value normalises to "plum" on the way in.
 *
 * Pure module apart from the two DOM helpers: the inline boot script in
 * app/layout.tsx has to duplicate this logic (it runs before any bundle), so
 * keep the two in step - clockTheme() and the normalise table are the parts
 * that matter.
 *
 * puff puff pass
 */

export type PuffTheme = "day" | "plum" | "light";
/** What a human picks: a palette, or "follow the clock". */
export type ThemeChoice = PuffTheme | "auto";

export const THEME_KEY = "puff-theme-override";

export interface ThemeOption {
  id: PuffTheme;
  label: string;
  /** One line, shown under the swatch. Cut copy hard: this is the whole blurb. */
  hint: string;
  /** Mini-preview: the ground wash, the tile face, its halo and its ink. */
  ground: string;
  card: string;
  halo: string;
  ink: string;
}

export const THEMES: readonly ThemeOption[] = [
  {
    id: "day",
    label: "Cloud Pink",
    hint: "Blue sky, cotton candy",
    ground: "linear-gradient(150deg,#CFE9F9,#FBDDEE)",
    card: "rgba(255,255,255,0.72)",
    halo: "rgba(201,63,168,0.26)",
    ink: "#2E2140",
  },
  {
    id: "plum",
    label: "Deep Plum",
    hint: "Dark and calm, glow only",
    ground: "linear-gradient(150deg,#24143D,#1A0F2E)",
    card: "rgba(255,255,255,0.055)",
    halo: "rgba(226,155,200,0.45)",
    ink: "#F4EAF2",
  },
  {
    id: "light",
    label: "Soft Light",
    hint: "Almost white, warm",
    ground: "linear-gradient(150deg,#FFFBFA,#FFF3F0)",
    card: "rgba(255,255,255,0.9)",
    halo: "rgba(255,143,163,0.28)",
    ink: "#3A2F40",
  },
];

/** Accepts anything (backend JSON, localStorage, a query string). */
export function normalizeChoice(value: unknown): ThemeChoice | null {
  if (typeof value !== "string") return null;
  const v = value.trim().toLowerCase();
  if (v === "day" || v === "plum" || v === "light" || v === "auto") return v;
  if (v === "night") return "plum"; // retired palette
  return null;
}

/** Auto by the local clock: Cloud Pink 07:00-18:59, Deep Plum otherwise. */
export function clockTheme(): PuffTheme {
  const h = new Date().getHours();
  return h >= 7 && h < 19 ? "day" : "plum";
}

export function resolveTheme(choice: ThemeChoice | null | undefined): PuffTheme {
  if (!choice || choice === "auto") return clockTheme();
  return choice;
}

export function readStoredChoice(): ThemeChoice | null {
  try {
    return normalizeChoice(localStorage.getItem(THEME_KEY));
  } catch {
    return null; // private mode / storage disabled
  }
}

/** null clears the key, which reads back as "auto". */
export function writeStoredChoice(choice: ThemeChoice | null): void {
  try {
    if (choice === null) localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, choice);
  } catch {
    /* storage unavailable - the attribute below still flips for this session */
  }
}

/** Put the palette on <html>. No-op when it is already there. */
export function applyTheme(theme: PuffTheme): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  if (root.getAttribute("data-puff-theme") !== theme) {
    root.setAttribute("data-puff-theme", theme);
  }
}

/** Pick + persist + paint, in one call. Used by every picker. */
export function chooseTheme(choice: ThemeChoice): void {
  writeStoredChoice(choice === "auto" ? "auto" : choice);
  applyTheme(resolveTheme(choice));
}
