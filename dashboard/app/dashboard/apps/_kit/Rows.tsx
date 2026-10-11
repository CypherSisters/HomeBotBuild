"use client";

import React, { ReactNode } from "react";
import Icon from "@/app/_components/Icon";

/* ------------------------------------------------------------------ */
/*  StatRow                                                            */
/* ------------------------------------------------------------------ */

export type Stat = {
  /** Mono caption. Kept short — it is a label, not a sentence. */
  label: string;
  /** The number. Rendered in JetBrains Mono with tabular numerals. */
  value: ReactNode;
  /** Optional second line under the value. */
  sub?: ReactNode;
  /** Optional mdi path. */
  icon?: string;
  /** Token colour for the value, e.g. `var(--mint)`. Defaults to --text. */
  tone?: string;
};

/**
 * StatRow — a row of counts. Mono, tabular, no progress bars and no
 * celebration states.
 */
export function StatRow({ stats }: { stats: Stat[] }) {
  return (
    <div className="puff-stats">
      {stats.map((s, i) => (
        <div className="puff-stat" key={`${s.label}-${i}`}>
          <span className="puff-stat-label">
            {s.icon ? <Icon path={s.icon} size="14" className="mr-1 align-[-2px]" w="w-4" h="h-4" /> : null}
            {s.label}
          </span>
          <span className="puff-stat-value" style={s.tone ? { color: s.tone } : undefined}>
            {s.value}
          </span>
          {s.sub ? <span className="puff-stat-label">{s.sub}</span> : null}
        </div>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  ListRow                                                            */
/* ------------------------------------------------------------------ */

type ListRowProps = {
  /**
   * Person colour token for the 5px left stripe, e.g. `var(--sky)`.
   * Omit it and no stripe renders.
   *
   * Never pass one for a health row: a person-tagged medication row
   * publishes a health-to-identity link on a screen the whole room can see
   * (the backend classes meds[] as health.condition/P4).
   */
  stripe?: string;
  /** >=16px mono initial — the identity of record, not the hue. */
  initial?: string;
  /** Leading mono field, usually a time or a count. */
  lead?: ReactNode;
  /** The row's plain-weight label. */
  label: ReactNode;
  /** Trailing mono metadata. */
  meta?: ReactNode;
  /** Trailing controls. Wrap icon-only buttons in `.puff-tap`. */
  actions?: ReactNode;
  className?: string;
  onClick?: () => void;
  title?: string;
};

/**
 * ListRow — the hairline row every app's list is built from. Radius 16,
 * 1px var(--line), optional person stripe. Not a gradient card.
 */
export function ListRow({
  stripe,
  initial,
  lead,
  label,
  meta,
  actions,
  className = "",
  onClick,
  title,
}: ListRowProps) {
  return (
    <div
      className={`puff-row ${stripe ? "puff-row-stripe" : ""} ${className}`}
      style={stripe ? ({ ["--pc" as string]: stripe } as React.CSSProperties) : undefined}
      onClick={onClick}
      title={title}
    >
      {initial ? <span className="puff-row-initial">{initial}</span> : null}
      {lead ? <span className="puff-row-meta">{lead}</span> : null}
      <span className="puff-row-label">{label}</span>
      {meta ? <span className="puff-row-meta">{meta}</span> : null}
      {actions ? <span className="flex items-center gap-1.5">{actions}</span> : null}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Small shared bits                                                  */
/* ------------------------------------------------------------------ */

/**
 * SampleBadge — the one honesty control on the wall, so it is the opposite of
 * subtle: `--lemon`, a solid border and a real size (>=14px at 2560).
 *
 * Rendered only when the payload actually says it is sample data. Nothing in
 * the app routes seeds anything, so this never appears on a household's own
 * file — it is the marker for a demo or fixture payload.
 */
export function SampleBadge({ label = "SAMPLE DATA" }: { label?: string }) {
  return <span className="puff-sample">{label}</span>;
}

/** Section caption above a block. Mono, uppercase, --text2 floor. */
export function Caption({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`puff-cap ${className}`}>{children}</div>;
}

/** A tinted status chip. `tone` is a token, `tint` a color-mix wash of it. */
export function Chip({
  children,
  tone,
  tintBg,
  className = "",
}: {
  children: ReactNode;
  tone?: string;
  tintBg?: string;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${className}`}
      style={{
        color: tone ?? "var(--text2)",
        background: tintBg ?? "var(--chip)",
      }}
    >
      {children}
    </span>
  );
}
