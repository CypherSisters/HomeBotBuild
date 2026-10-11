/**
 * Theme tokens for the five HomeBot app routes.
 *
 * Every colour an app page paints comes from here or from a `var(--token)`
 * class in css/_apps.css. No `dark:` utility and no literal hex: `.dark` is
 * permanently on <html> (app/layout.tsx:59) so `dark:` renders in BOTH
 * palettes, and day mode survives only on the `!important` remap at
 * css/_theme.css:646+. Painting from tokens takes the app pages off that
 * remap entirely — one rule set, both palettes.
 */

/** Canonical palette tokens, defined per palette on html[data-puff-theme]. */
export const T = {
  bg: "var(--bg)",
  card: "var(--card)",
  card2: "var(--card2)",
  line: "var(--line)",
  text: "var(--text)",
  text2: "var(--text2)",
  text3: "var(--text3)",
  sky: "var(--sky)",
  lav: "var(--lav)",
  pink: "var(--pink)",
  magenta: "var(--magenta)",
  mint: "var(--mint)",
  lemon: "var(--lemon)",
  red: "var(--puff-red)",
} as const;

export type ToneName = "sky" | "lav" | "pink" | "magenta" | "mint" | "lemon" | "red" | "text2";

/** A translucent wash of a token, for tinted badges and status chips. */
export function tint(tone: ToneName, pct = 16): string {
  return `color-mix(in srgb, ${T[tone]} ${pct}%, transparent)`;
}

/**
 * Per-person colour order.
 *
 * --mint and --lemon are deliberately skipped: they carry semantic meaning
 * (success / warning) elsewhere, and a person colour must never double as a
 * state colour. Colour reliably separates three people and four is the
 * tested ceiling, so identity of record is the >=16px mono initial that
 * every chip carries (.puff-row-initial), not the hue.
 */
export const PERSON_TOKENS: readonly string[] = [T.sky, T.lav, T.pink, T.magenta];

/** Stable per-person colour from a list position or an id. */
export function personColor(index: number): string {
  return PERSON_TOKENS[((index % PERSON_TOKENS.length) + PERSON_TOKENS.length) % PERSON_TOKENS.length];
}

/** First character of a name, uppercased — the identity of record on a chip. */
export function initialOf(name: string | undefined | null): string {
  const trimmed = (name ?? "").trim();
  return trimmed ? trimmed[0].toUpperCase() : "?";
}

/**
 * Role placeholders used wherever an app has to show a person before the
 * household has entered one. Never invent names, ages or health facts.
 */
export const ROLE_PLACEHOLDERS = ["Adult 1", "Adult 2", "Kid 1", "Kid 2"] as const;
