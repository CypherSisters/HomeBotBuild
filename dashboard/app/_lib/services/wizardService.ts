// The wizard as cards: the browser half.
//
// The CLI and this dashboard are two renderers of ONE card set. No question is
// written here: a question lives in the skill that proposed it, and these types
// mirror `wizard_card/v1` (src/puffpai/wizard/cards.py) field for field so a
// renderer that has drifted from the card fails at the type level rather than
// in somebody's build.
//
// Three routes, same-origin through /api/pai, which injects this box's API key
// server-side; nothing key-shaped ships to the browser. That proxy
// (app/api/pai/[...path]/route.ts) forwards any backend path behind the pairing
// gate, so there is no per-route allowlist to add `wizard` to here.
//
// SECRETS. A card never carries a value for a `secret_entry` field (the backend
// refuses one that does), and an answer to one is redacted before it is logged
// or returned. Nothing in this file stores an answer.

import { setupCodeHeaders } from "./setupService";

const BASE = "/api/pai";

export const FIELD_KINDS = [
  "choice",
  "multi_choice",
  "text",
  "toggle",
  "number",
  "secret_entry",
  "info",
] as const;
export type FieldKind = (typeof FIELD_KINDS)[number];

/** Every status a row on the review screen may be in (cards.py STATUSES). */
export type CardStatus = "ready" | "needs_you" | "gap" | "blocked" | "not_built";

/** How honestly a cost is known. `unknown` is never summed into a total. */
export type EstimateBasis = "measured" | "calibrated" | "unknown";

/** The four things that make a row consequential. */
export type ConsequentialReason =
  | "heavy_bytes"
  | "destructive_rerun"
  | "runtime_egress"
  | "vault_pin_unset";

export interface CardOption {
  value: string;
  label: string;
}

/** One field of a card. Which optional keys are present depends on `kind`:
 *  choice/multi_choice carry options, free_text and pattern; text carries
 *  max_length and pattern; number carries min/max; secret_entry carries
 *  max_length and empty strings for current/proposed; info carries text only. */
export interface CardField {
  id: string;
  kind: FieldKind;
  label: string;
  help: string;
  current?: string | string[] | boolean | number | null;
  proposed?: string | string[] | boolean | number | null;
  options?: CardOption[];
  pattern?: string;
  max_length?: number;
  min?: number;
  max?: number;
  free_text?: boolean;
}

export interface CardEstimate {
  bytes: number | null;
  seconds: number | null;
  basis: EstimateBasis;
}

/** A gap record. `fix_command` is rendered by the backend from a
 *  registered command id — a command string is NEVER assembled in the browser. */
export interface CardGap {
  skill: string;
  what: string;
  why: string;
  impact: string;
  fix: Record<string, unknown>;
  fix_command: string;
  blocking: boolean;
  recorded_at: string;
}

export interface WizardCard {
  schema: string;
  skill: string;
  section: string;
  /** Which question of a skill that asks more than one (Secure: posture,
   *  vault, bridge). Empty for a skill with a single card. */
  part: string;
  title: string;
  decided: string;
  status: CardStatus;
  consequential: boolean;
  consequential_reasons: ConsequentialReason[];
  estimate: CardEstimate;
  fields: CardField[];
  gaps: CardGap[];
}

/** A skill with no card at all. The backend names it and gives it the owner's
 *  words; this file does not keep its own list of the twelve. */
export interface PlainSkill {
  skill: string;
  title: string;
}

export interface CardsReply {
  cards: WizardCard[];
  /** The card ids in the order the wizard runs them — `secure:vault` for a
   *  skill that asks more than one question. THE source of truth for order:
   *  the browser keeps no copy of the skill list, which is the drift (14 steps
   *  against 8) this whole port exists against. */
  order: string[];
  /** Skills that have no card yet. Listed, never hidden. */
  not_converted: PlainSkill[];
  /** A skill that could not propose, in the backend's own words. The page
   *  stays up; the refusal is shown. */
  refusals: string[];
  /** The card ids the owner has already settled, so the screen can say how
   *  much of the build has been decided instead of "nothing has been
   *  applied" forever. */
  applied?: string[];
  total: CardEstimate;
  stale: string[];
}

/** One row of what POST /wizard/run did. `applied` false with a `why` is the
 *  server refusing to accept something on the owner's behalf. */
export interface RunStep {
  card: string;
  outcome: string;
  applied: boolean;
  why: string;
  verified?: Record<string, unknown>;
}

export interface RunReply {
  success?: boolean;
  message?: string;
  results?: RunStep[];
  refusals?: string[];
  gaps?: CardGap[];
  stale?: string[];
}

export interface ApplyReply {
  success?: boolean;
  /** The backend's message on a refusal (400/404). Shown beside the field. */
  message?: string;
  /** Which field the refusal was about, decided by the backend against the
   *  card it proposed. `null` means the message is about the card as a whole. */
  field?: string | null;
  skill?: string;
  outcome?: string;
  card?: WizardCard;
  changed?: Record<string, unknown>;
  stale?: string[];
  verified?: Record<string, unknown>;
  gaps?: CardGap[];
}

/** One card a stopped run already settled, as the record remembers it. */
export interface ResumeDecided {
  entry?: string;
  card?: string;
  title?: string;
}

/** A setup that stopped part-way and is still worth continuing.
 *
 *  `offer: false` carries `why` — `""` for "there is no record at all", or the
 *  backend's word for a record it set aside (`unreadable`, `finished`,
 *  `too_old`, `nothing_done`). Read-only: nothing is verified and nothing is
 *  written until the owner answers. */
export interface ResumeOffer {
  offer?: boolean;
  why?: string;
  started_at?: number;
  age_s?: number;
  decided?: ResumeDecided[];
  stopped_entry?: string;
  stopped_card?: string;
  stopped_title?: string;
}

export interface ResumeReply {
  success?: boolean;
  message?: string;
  choice?: string;
  resumed?: boolean;
  archived?: boolean;
  /** Blocking cards whose verify did not hold against this machine, so they
   *  will be asked again. Continuing is never "trust the file". */
  reasked?: string[];
  why?: string;
}

export interface StateReply {
  schema?: string;
  found?: boolean;
  started_at?: string | null;
  /** Fixed skill order, carried explicitly: JSON key order is no promise. */
  order?: string[];
  skills?: Record<string, unknown>;
  entries?: Record<string, unknown>;
  log?: Record<string, unknown>[];
  gaps?: CardGap[];
  stale?: string[];
  next_entry?: string | null;
  /** Whether a stopped setup is there to continue. Rides on this route rather
   *  than one of its own: it is the same question the log answers. */
  resume?: ResumeOffer;
}

export interface Reply<T> {
  ok: boolean;
  status: number;
  data: T;
}

async function req<T>(path: string, method = "GET", body?: unknown): Promise<Reply<T>> {
  try {
    const res = await fetch(`${BASE}${path}`, {
      method,
      // X-Setup-Code on every wizard call, reads included: while first-run
      // setup is open the backend refuses this whole family from a client that
      // is not on the machine itself (api_server._enforce_setup_code), exactly
      // as it refuses /setup/*. The code lives in setupService's module memory
      // — never in storage, a cookie or a URL — and is "" once setup completes,
      // at which point the header is simply absent again.
      headers: { "Content-Type": "application/json", ...setupCodeHeaders() },
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
    let data: unknown = {};
    try {
      data = await res.json();
    } catch {
      /* empty / non-JSON body */
    }
    return { ok: res.ok, status: res.status, data: data as T };
  } catch {
    // Backend down before the proxy even answered. A 0 so callers can say so
    // rather than hang.
    return { ok: false, status: 0, data: {} as T };
  }
}

/** Every skill's card, in the fixed skill order. Read-only on the backend. */
export function getWizardCards(): Promise<Reply<CardsReply>> {
  return req<CardsReply>("/wizard/cards");
}

/** Apply one card's answers. An empty `answers` means "accept the proposal".
 *  `card` is the id from `order` — `discover`, or `secure:vault`. */
export function applyWizardCard(
  card: string,
  answers: Record<string, unknown> = {},
): Promise<Reply<ApplyReply>> {
  return req<ApplyReply>("/wizard/apply", "POST", { skill: card, answers });
}

/** [Build it]. The SERVER applies every card the owner has not answered, in
 *  its own order, and refuses on its own account to accept a consequential row
 *  that is not in `acknowledged` or any row that asks for a secret. The list
 *  below is the browser saying which heavy rows the owner opened; it is a
 *  claim, and the backend checks it against the real card set. */
export function runWizard(acknowledged: string[]): Promise<Reply<RunReply>> {
  return req<RunReply>("/wizard/run", "POST", { acknowledged });
}

/** The run log, summarised: one row per skill, the gaps, what is stale — and
 *  whether a stopped setup is there to continue. */
export function getWizardState(): Promise<Reply<StateReply>> {
  return req<StateReply>("/wizard/state");
}

/** Continue a stopped setup, or set it aside and start over.
 *
 *  The SERVER decides what continuing may skip: every blocking card the record
 *  calls done is verified against the machine again, and the vault is checked
 *  on disk whatever the record says. What comes back in `reasked` is which
 *  cards did not hold and will be asked again. */
export function resumeWizard(choice: "continue" | "restart"): Promise<Reply<ResumeReply>> {
  return req<ResumeReply>("/wizard/resume", "POST", { choice });
}
