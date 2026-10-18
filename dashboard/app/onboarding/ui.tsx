"use client";

/**
 * Onboarding primitives, drawn in the dashboard's own language: CSS custom
 * properties from css/_theme.css (--card, --line, --text/2/3, --pink, --mint,
 * --lemon), Nunito for prose, JetBrains Mono for the micro-labels. Dense
 * editorial — a label, a value, a single sentence — never an airy wizard with
 * one question floating in the middle of a white page.
 */

import type { CSSProperties, ReactNode } from "react";
import { useEffect, useState } from "react";
import { checkHealth } from "../_lib/api";

export const microLabel: CSSProperties = {
  fontFamily: "var(--font-jbmono), monospace",
  fontWeight: 700,
  fontSize: 11,
  letterSpacing: ".14em",
  textTransform: "uppercase",
  color: "var(--text3)",
};

export const inputStyle: CSSProperties = {
  width: "100%",
  background: "var(--card2)",
  border: "1px solid var(--line)",
  borderRadius: 12,
  padding: "10px 12px",
  color: "var(--text)",
  fontFamily: "var(--font-nunito), sans-serif",
  fontSize: 15,
};

export function Card({
  children,
  className,
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      className={className}
      style={{
        background: "var(--card)",
        border: "1px solid var(--line)",
        borderRadius: 18,
        padding: 18,
        ...style,
      }}
    >
      {children}
    </div>
  );
}

/** Two-column definition row — the densest way to show a detected fact. */
export function FactRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div
      className="flex items-baseline justify-between gap-4"
      style={{ padding: "8px 0", borderTop: "1px solid var(--line)" }}
    >
      <span style={microLabel}>{label}</span>
      <span style={{ color: "var(--text)", fontSize: 15, textAlign: "right" }}>{value}</span>
    </div>
  );
}

export function Field({
  id,
  label,
  hint,
  error,
  children,
}: {
  id: string;
  label: string;
  hint?: ReactNode;
  error?: string;
  children: ReactNode;
}) {
  return (
    <div style={{ marginTop: 14 }}>
      <label htmlFor={id} style={{ ...microLabel, display: "block", marginBottom: 6 }}>
        {label}
      </label>
      {children}
      {hint ? (
        <div style={{ color: "var(--text3)", fontSize: 13, marginTop: 4 }}>{hint}</div>
      ) : null}
      {error ? (
        <div
          id={`${id}-error`}
          role="alert"
          style={{ color: "var(--puff-red)", fontSize: 13, marginTop: 4 }}
        >
          {error}
        </div>
      ) : null}
    </div>
  );
}

export function TextInput({
  id,
  value,
  onChange,
  type = "text",
  placeholder,
  invalid,
  autoComplete = "off",
}: {
  id: string;
  value: string;
  onChange: (next: string) => void;
  type?: "text" | "password";
  placeholder?: string;
  invalid?: boolean;
  autoComplete?: string;
}) {
  return (
    <input
      id={id}
      type={type}
      value={value}
      placeholder={placeholder}
      autoComplete={autoComplete}
      aria-invalid={invalid || undefined}
      aria-describedby={invalid ? `${id}-error` : undefined}
      onChange={(e) => onChange(e.target.value)}
      style={{
        ...inputStyle,
        borderColor: invalid ? "var(--puff-red)" : "var(--line)",
      }}
    />
  );
}

/** A radio in a bordered row with its one explanatory sentence. */
export function Choice({
  name,
  value,
  checked,
  onSelect,
  title,
  description,
  note,
  disabled,
}: {
  name: string;
  value: string;
  checked: boolean;
  onSelect: (value: string) => void;
  title: string;
  description?: ReactNode;
  note?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label
      className="flex items-start gap-3"
      style={{
        padding: "12px 14px",
        marginTop: 8,
        borderRadius: 14,
        border: `1px solid ${checked ? "var(--pink)" : "var(--line)"}`,
        background: checked ? "var(--chip)" : "var(--card2)",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={() => onSelect(value)}
        style={{ marginTop: 4 }}
      />
      <span>
        <span style={{ display: "block", color: "var(--text)", fontWeight: 700, fontSize: 15 }}>
          {title}
        </span>
        {description ? (
          <span style={{ display: "block", color: "var(--text2)", fontSize: 14, marginTop: 2 }}>
            {description}
          </span>
        ) : null}
        {note ? (
          <span style={{ display: "block", color: "var(--text3)", fontSize: 13, marginTop: 4 }}>
            {note}
          </span>
        ) : null}
      </span>
    </label>
  );
}

/** A checkbox in the same row shape — used for surfaces and egress doors. */
export function Toggle({
  id,
  checked,
  onChange,
  title,
  description,
  disabled,
}: {
  id: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  title: string;
  description?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label
      htmlFor={id}
      className="flex items-start gap-3"
      style={{
        padding: "10px 14px",
        marginTop: 8,
        borderRadius: 14,
        border: `1px solid ${checked ? "var(--pink)" : "var(--line)"}`,
        background: checked ? "var(--chip)" : "var(--card2)",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        style={{ marginTop: 4 }}
      />
      <span>
        <span style={{ display: "block", color: "var(--text)", fontWeight: 700, fontSize: 15 }}>
          {title}
        </span>
        {description ? (
          <span style={{ display: "block", color: "var(--text2)", fontSize: 14, marginTop: 2 }}>
            {description}
          </span>
        ) : null}
      </span>
    </label>
  );
}

export function Notice({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn" | "ok";
  children: ReactNode;
}) {
  const colour =
    tone === "ok" ? "var(--mint)" : tone === "warn" ? "var(--lemon)" : "var(--sky)";
  return (
    <div
      style={{
        marginTop: 12,
        padding: "10px 14px",
        borderRadius: 14,
        border: "1px solid var(--line)",
        borderLeft: `3px solid ${colour}`,
        background: "var(--card2)",
        color: "var(--text2)",
        fontSize: 14,
        lineHeight: 1.5,
      }}
    >
      {children}
    </div>
  );
}

export function PuffButton({
  label,
  onClick,
  kind = "ghost",
  disabled,
  type = "button",
}: {
  label: string;
  onClick?: () => void;
  kind?: "primary" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  const primary = kind === "primary";
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "10px 18px",
        borderRadius: 999,
        fontFamily: "var(--font-nunito), sans-serif",
        fontWeight: 700,
        fontSize: 15,
        color: primary ? "#fff" : "var(--text2)",
        background: primary ? "var(--magenta)" : "var(--card2)",
        border: `1px solid ${primary ? "var(--magenta)" : "var(--line)"}`,
        opacity: disabled ? 0.45 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      {label}
    </button>
  );
}

/** Micro-label above a block of content. */
export function Eyebrow({ children }: { children: ReactNode }) {
  return <div style={microLabel}>{children}</div>;
}

export function Lede({ children }: { children: ReactNode }) {
  return (
    <p style={{ color: "var(--text2)", fontSize: 15, lineHeight: 1.5, margin: "6px 0 0" }}>
      {children}
    </p>
  );
}

/** Numbered rail of steps — current, done, to come. */
export function Stepper({ labels, current }: { labels: readonly string[]; current: number }) {
  return (
    <ol className="flex flex-wrap items-center gap-x-3 gap-y-1" style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {labels.map((label, i) => {
        const active = i === current;
        const done = i < current;
        return (
          <li
            key={label}
            aria-current={active ? "step" : undefined}
            style={{
              ...microLabel,
              fontSize: 10,
              color: active ? "var(--pink)" : done ? "var(--text2)" : "var(--text3)",
            }}
          >
            {String(i + 1).padStart(2, "0")} {label}
          </li>
        );
      })}
    </ol>
  );
}

/**
 * One line above the wizard, only when the bot behind it does not answer.
 * It never names the backend or its address: this dashboard keeps that off
 * the screen. A missing answer to /health is the only signal it reads.
 */
export function ConnectedTo() {
  const [reachable, setReachable] = useState<boolean | null>(null);
  useEffect(() => {
    let cancelled = false;
    void checkHealth().then((ok) => {
      if (!cancelled) setReachable(ok);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  if (reachable !== false) return null;
  return (
    <p
      style={{
        ...microLabel,
        letterSpacing: ".1em",
        margin: "0 0 12px",
        color: "var(--lemon)",
      }}
    >
      {NOT_ANSWERING}
    </p>
  );
}

/** The one line above the wizard, when the bot does not answer. */
export const NOT_ANSWERING = "Your bot is not answering right now, so nothing here can be saved yet.";
