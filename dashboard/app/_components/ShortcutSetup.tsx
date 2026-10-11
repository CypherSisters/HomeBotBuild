"use client";

/**
 * "Set up phone sharing" — generate an upload-only device token and show the
 * iOS Apple Shortcut steps that use it.
 *
 * iOS has no Web Share Target, so "Share → Puff" from an iPhone is an Apple
 * Shortcut that POSTs the shared file to /api/pai/knowledge/upload with the
 * token as a Bearer header. This panel mints the token (shown ONCE), lists
 * existing tokens with a revoke button, and renders the manual setup steps.
 *
 * Gated to paired devices: the list endpoint 401s an unpaired client, and this
 * component then renders nothing. Everything here talks to /api/pair/* (never
 * gated) and never sees the HttpOnly pairing cookie.
 */

import { useCallback, useEffect, useState } from "react";
import MsIcon from "./MsIcon";

interface TokenMeta {
  id: string;
  label: string;
  createdAt: string;
  expiresAt: string;
  lastUsedAt: string | null;
  expired: boolean;
}

/** The upload URL the Shortcut posts to — built from this appliance's origin. */
function uploadUrl(): string {
  if (typeof window === "undefined") return "/api/pai/knowledge/upload";
  return `${window.location.origin}/api/pai/knowledge/upload`;
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function ShortcutSetup() {
  const [ready, setReady] = useState(false); // paired + list loaded
  const [hidden, setHidden] = useState(false); // unpaired → render nothing
  const [tokens, setTokens] = useState<TokenMeta[]>([]);
  const [open, setOpen] = useState(false); // steps panel expanded
  const [label, setLabel] = useState("");
  const [minting, setMinting] = useState(false);
  const [fresh, setFresh] = useState<string | null>(null); // raw token, shown once
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/pair/device-token", { cache: "no-store" });
      if (res.status === 401) {
        setHidden(true);
        return;
      }
      if (!res.ok) {
        setReady(true);
        return;
      }
      const data = (await res.json()) as { tokens?: TokenMeta[] };
      setTokens(Array.isArray(data.tokens) ? data.tokens : []);
      setReady(true);
    } catch {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const mint = useCallback(async () => {
    if (minting) return;
    setMinting(true);
    setError(null);
    setFresh(null);
    setCopied(false);
    try {
      const res = await fetch("/api/pair/device-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: label.trim() }),
      });
      if (res.status === 429) {
        setError("Too many just now — wait a minute.");
        return;
      }
      if (res.status === 401) {
        setHidden(true);
        return;
      }
      if (!res.ok) {
        setError("Couldn't create a token.");
        return;
      }
      const data = (await res.json()) as { token?: string };
      if (data.token) {
        setFresh(data.token);
        setOpen(true);
        setLabel("");
        void load();
      }
    } catch {
      setError("Can't reach Puff.");
    } finally {
      setMinting(false);
    }
  }, [label, minting, load]);

  const revoke = useCallback(
    async (id: string) => {
      try {
        await fetch("/api/pair/device-token/revoke", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        });
        void load();
      } catch {
        /* leave the row; a reload will reconcile */
      }
    },
    [load],
  );

  const copy = useCallback(async () => {
    if (!fresh) return;
    try {
      await navigator.clipboard.writeText(fresh);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard blocked — the token is visible to select manually */
    }
  }, [fresh]);

  if (hidden || !ready) return null;

  const card: React.CSSProperties = {
    background: "var(--card)",
    border: "1px solid var(--line)",
    borderRadius: 24,
    boxShadow: "var(--shadow)",
    backdropFilter: "blur(18px)",
    WebkitBackdropFilter: "blur(18px)",
  };

  const num: React.CSSProperties = {
    fontFamily: "var(--font-jbmono)",
    color: "var(--pink)",
    fontSize: 12,
    fontWeight: 700,
    minWidth: 16,
  };

  return (
    <section style={card} className="mt-3 p-5">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 text-left"
        aria-expanded={open}
      >
        <MsIcon name="ios_share" size={22} style={{ color: "var(--pink)" }} />
        <span
          className="flex-1"
          style={{ fontFamily: "var(--font-fredoka)", fontSize: 16, fontWeight: 600, color: "var(--text)" }}
        >
          Set up phone sharing
        </span>
        <MsIcon
          name={open ? "expand_less" : "expand_more"}
          size={22}
          style={{ color: "var(--text3)" }}
        />
      </button>

      {open && (
        <div className="mt-4">
          <p className="text-xs" style={{ color: "var(--text2)" }}>
            Add "Send to Puff" to your iPhone Share Sheet. Create an upload-only
            token, then build a one-time Apple Shortcut with it.
          </p>

          {/* Mint */}
          <div className="mt-3 flex gap-2">
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Name this phone (optional)"
              maxLength={64}
              className="min-w-0 flex-1 outline-none"
              style={{
                fontSize: 13,
                padding: "10px 12px",
                borderRadius: 14,
                background: "var(--card2)",
                color: "var(--text)",
                border: "1px solid var(--line)",
              }}
            />
            <button
              onClick={() => void mint()}
              disabled={minting}
              className="shrink-0 transition disabled:opacity-45"
              style={{
                padding: "10px 16px",
                borderRadius: 14,
                border: "1px solid transparent",
                background: "linear-gradient(135deg, var(--pink), var(--magenta))",
                color: "#fff",
                fontFamily: "var(--font-fredoka)",
                fontSize: 14,
                fontWeight: 600,
              }}
            >
              {minting ? "Creating…" : "Create"}
            </button>
          </div>

          {error && (
            <p role="alert" className="mt-2 text-xs" style={{ color: "var(--puff-red)" }}>
              {error}
            </p>
          )}

          {/* Freshly minted token — shown once */}
          {fresh && (
            <div
              className="mt-3 p-3"
              style={{ background: "var(--card2)", border: "1px solid var(--line)", borderRadius: 16 }}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] uppercase tracking-[0.14em]" style={{ color: "var(--text3)", fontFamily: "var(--font-jbmono)" }}>
                  Copy now — shown once
                </span>
                <button
                  onClick={() => void copy()}
                  style={{
                    padding: "5px 12px",
                    borderRadius: 10,
                    border: "1px solid var(--line)",
                    background: "var(--card)",
                    color: copied ? "var(--mint)" : "var(--text)",
                    fontSize: 12,
                    fontWeight: 600,
                  }}
                >
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
              <div
                className="mt-2 break-all"
                style={{ fontFamily: "var(--font-jbmono)", fontSize: 12, color: "var(--pink)" }}
              >
                {fresh}
              </div>
            </div>
          )}

          {/* Steps */}
          <ol className="mt-4 space-y-2">
            {[
              <>Shortcuts app → <b>+</b> → set <b>Show in Share Sheet</b>, Accept <b>Images</b> and <b>Files</b>.</>,
              <>Add <b>Get Contents of URL</b>. URL:{" "}
                <code style={{ fontFamily: "var(--font-jbmono)", fontSize: 11, color: "var(--text)" }}>{uploadUrl()}</code></>,
              <>Method <b>POST</b>. Header <b>Authorization</b> ={" "}
                <code style={{ fontFamily: "var(--font-jbmono)", fontSize: 11, color: "var(--text)" }}>Bearer pstk_…</code> (your token).</>,
              <>Request Body <b>Form</b>: field <b>file</b> = Shortcut Input, field <b>zone</b> = <b>2</b>.</>,
              <>Name it <b>Send to Puff</b> and enable <b>Show in Share Sheet</b>.</>,
            ].map((step, i) => (
              <li key={i} className="flex gap-2.5">
                <span style={num}>{i + 1}</span>
                <span className="text-xs" style={{ color: "var(--text2)", lineHeight: 1.5 }}>
                  {step}
                </span>
              </li>
            ))}
          </ol>

          {/* Existing tokens */}
          {tokens.length > 0 && (
            <div className="mt-4">
              <div className="text-[11px] uppercase tracking-[0.14em]" style={{ color: "var(--text3)", fontFamily: "var(--font-jbmono)" }}>
                Tokens
              </div>
              <ul className="mt-2 space-y-1.5">
                {tokens.map((t) => (
                  <li
                    key={t.id}
                    className="flex items-center gap-3 p-2.5"
                    style={{ background: "var(--card2)", border: "1px solid var(--line)", borderRadius: 12 }}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm" style={{ color: "var(--text)" }}>
                        {t.label || "Unnamed phone"}
                      </div>
                      <div className="text-[11px]" style={{ color: "var(--text3)" }}>
                        {t.expired ? "expired" : `expires ${fmtDate(t.expiresAt)}`}
                        {t.lastUsedAt ? ` · used ${fmtDate(t.lastUsedAt)}` : " · never used"}
                      </div>
                    </div>
                    <button
                      onClick={() => void revoke(t.id)}
                      className="shrink-0"
                      style={{
                        padding: "6px 12px",
                        borderRadius: 10,
                        border: "1px solid var(--line)",
                        background: "var(--card)",
                        color: "var(--puff-red)",
                        fontSize: 12,
                        fontWeight: 600,
                      }}
                    >
                      Revoke
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
