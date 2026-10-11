"use client";

/**
 * The Bridge's shared vocabulary: the chip, the stat cell, the status strip.
 *
 * These started life inside `app/dashboard/bridge/page.tsx`. The Firewall page
 * is the same instrument read from the other side — Drawbridge says what the
 * house is saying, Firewall says what the house is allowed to say — so the two
 * must look like one panel, not two panels about the same subject. Copying the
 * atoms would have guaranteed they drifted the first time either page was
 * touched.
 *
 * Deliberately unstyled beyond the tokens: colour comes from `var(--*)` so both
 * pages inherit the dashboard theme rather than pinning their own palette.
 *
 * puff puff pass
 */

import type { ReactNode } from "react";

/** Small uppercase tag. Tone is a CSS variable, never a literal colour. */
export function Chip({
  label,
  tone,
  title,
  dim,
}: {
  label: string;
  tone: string;
  title?: string;
  dim?: boolean;
}) {
  return (
    <span
      title={title}
      className="rounded px-1.5 py-px text-[10px] font-semibold tracking-wider uppercase"
      style={{ color: tone, background: "var(--chip)", opacity: dim ? 0.45 : 1 }}
    >
      {label}
    </span>
  );
}

/**
 * One cell of a status strip: a dim label over a monospace figure.
 *
 * `value` is a string or number, never a component, so a strip cannot quietly
 * grow a control. A strip states facts; the things that change the house live
 * in their own section, where they can carry the words that explain them.
 */
export function Stat({
  label,
  value,
  tone,
  title,
}: {
  label: string;
  value: string | number;
  tone?: string;
  title?: string;
}) {
  return (
    <div
      className="flex min-w-0 flex-col gap-0.5 px-4 py-2.5 first:pl-0"
      title={title}
    >
      <span
        className="text-[10px] font-semibold tracking-[0.14em] uppercase"
        style={{ color: "var(--text2)" }}
      >
        {label}
      </span>
      <span
        className="truncate font-mono text-base leading-none tabular-nums"
        style={{ color: tone ?? "var(--text)" }}
      >
        {value}
      </span>
    </div>
  );
}

/** The hairline-divided row the Stat cells sit in. */
export function StatusStrip({ children }: { children: ReactNode }) {
  return (
    <div
      className="mb-5 flex flex-wrap divide-x rounded border"
      style={{ borderColor: "var(--line)", background: "var(--card2)" }}
    >
      {children}
    </div>
  );
}

/** A section rule: small caps heading with a one-line gloss beside it. */
export function SectionRule({
  title,
  note,
  right,
}: {
  title: string;
  note?: string;
  right?: ReactNode;
}) {
  return (
    <div
      className="mt-7 mb-2.5 flex flex-wrap items-baseline justify-between gap-3 border-b pb-1.5"
      style={{ borderColor: "var(--line)" }}
    >
      <div className="flex flex-wrap items-baseline gap-3">
        <h2
          className="text-[11px] font-bold tracking-[0.18em] uppercase"
          style={{ color: "var(--text)" }}
        >
          {title}
        </h2>
        {note && (
          <span className="text-[11px]" style={{ color: "var(--text2)" }}>
            {note}
          </span>
        )}
      </div>
      {right}
    </div>
  );
}

/** A refusal or a fault, stated in one line without shouting. */
export function Notice({
  children,
  tone = "var(--pink)",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return (
    <div
      className="mb-4 rounded border-l-2 px-3 py-2 text-xs"
      style={{
        borderColor: tone,
        background: "var(--card2)",
        color: "var(--text2)",
      }}
    >
      {children}
    </div>
  );
}

/**
 * The bordered box a table lives in, with its own horizontal scroll.
 *
 * The `<table>` inside MUST carry `className="puff-bridge-table"`. The
 * dashboard template ships blanket `.dark table td { color: slate-200
 * !important }` and slate row stripes in `css/main.css` / `css/_table.css`;
 * being !important they outranked the inline palette on every cell, which is
 * exactly the "all grey with white writing" this page was reported for. The
 * class is how a table says it dresses itself.
 */
export function TableFrame({ children }: { children: ReactNode }) {
  return (
    <div
      className="overflow-x-auto rounded border"
      style={{ borderColor: "var(--line)", background: "var(--card)" }}
    >
      {children}
    </div>
  );
}

/** Column headers in the strip's small-caps voice. */
export function Thead({ columns }: { columns: string[] }) {
  return (
    <thead>
      <tr className="border-b" style={{ borderColor: "var(--line)", color: "var(--text2)" }}>
        {columns.map((h) => (
          <th
            key={h}
            className="px-3 py-2 text-[10px] font-semibold tracking-[0.12em] uppercase"
          >
            {h}
          </th>
        ))}
      </tr>
    </thead>
  );
}

/* ── the tier vocabulary ──────────────────────────────────────────────────
 *
 * Lives here, not on either page, because Firewall and Drawbridge must call
 * the same integration the same thing. Tones are palette tokens: amber for
 * the tier that leaves the house, the theme's green for the tier that does
 * not, violet for the open question, and plain secondary text for Home
 * Assistant's own furniture — muted, because it is furniture, but never
 * `--text3`, which does not clear AA on the night card.
 */

export type Tier = "cloud" | "wifi_direct" | "local" | "unknown" | "system";

/** Cloud first: it is the tier that answers the question the user asked. */
export const TIER_ORDER: Tier[] = [
  "cloud",
  "wifi_direct",
  "local",
  "unknown",
  "system",
];

export const TIER_TONE: Record<string, string> = {
  cloud: "var(--lemon)",
  wifi_direct: "var(--sky)",
  local: "var(--mint)",
  unknown: "var(--lav)",
  system: "var(--text2)",
};

/** The chip's word. Short: it sits inside a table cell beside a name. */
export const TIER_LABEL: Record<string, string> = {
  cloud: "cloud",
  wifi_direct: "wifi",
  local: "local",
  unknown: "unclassified",
  system: "system",
};

/** The section heading, in the owner's words rather than the schema's. */
export const TIER_HEADING: Record<string, string> = {
  cloud: "Cloud-backed",
  wifi_direct: "On the network",
  local: "Local",
  unknown: "Unknown",
  system: "System",
};

/** What the tier actually means, on hover and under each heading. */
export const TIER_NOTE: Record<string, string> = {
  cloud: "The vendor's servers are in the control path.",
  wifi_direct: "Has its own address — also a row on the network roll.",
  local: "Speaks only to this house.",
  unknown: "PuffPAI has no opinion on this integration yet.",
  system: "Home Assistant's own internals. Never offered a policy.",
};

/** Sort key for a bucket row: tier band first, then the caller's own order. */
export function tierRank(tier: string): number {
  const at = TIER_ORDER.indexOf(tier as Tier);
  return at === -1 ? TIER_ORDER.length : at;
}
