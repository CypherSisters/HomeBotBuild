// What a screen that is not paired yet is told, and how to tell that a reply
// was refused for that reason.
//
// The build engine's pages (Developer, Find more, Sign-up keys, GitHub) are for
// paired screens only. The gate itself lives in app/_lib/server/pairing.ts and
// the backend's paired-device checks; nothing here changes who may pass. This
// only makes the refusal say what it is, and how a person at the bot's own
// computer gets the code (the `puffpai pair` command, which reads the same
// pairing file as scripts/show-pairing-code.js).
//
// Plain JavaScript inside a .ts file (Node runs it with type stripping), no
// imports, so the test can load it without a build.

export const PAIR_PAGE_LINE = "Pair this screen to use this page.";
export const PAIR_HOW_HERE = "On the computer running your bot, run puffpai pair to see the code.";
export const PAIR_HOW_PAIRED = "Or, on a screen that is already paired, open Settings, then Paired devices.";
export const PAIR_HOW_LINES: string[] = [PAIR_HOW_HERE, PAIR_HOW_PAIRED];

/** The page line and the how, as one string, for places that show one line. */
export const PAIR_PAGE_WORDS = [PAIR_PAGE_LINE, ...PAIR_HOW_LINES].join(" ");

/** A tile's status line while this screen is not paired. */
export const PAIR_TILE_LINE = "Pair this screen to see this";

/** Fired when the pairing gate answers 401 pairing_required (PairPrompt's fetch hook). */
export const PAIRING_REQUIRED_EVENT = "puffpai:pairing-required";

/** Opens the pairing box on purpose, even after "Not now". */
export const PAIR_OPEN_EVENT = "puffpai:pair-open";

/**
 * True when a reply was refused because this screen is not paired: the
 * dashboard's gate (401 {"error": "pairing_required"}) or a backend
 * paired-device check (403 {"code": "not_paired"}). Any other 401 or 403 (a
 * wrong PIN, a refused change) is not this, and keeps its own words.
 */
export function isPairingReply(status: number, body: unknown): boolean {
  if (!body || typeof body !== "object") return false;
  const b = body as { error?: unknown; code?: unknown };
  if (status === 401) return b.error === "pairing_required";
  if (status === 403) return b.code === "not_paired" || b.error === "not_paired";
  return false;
}
