"use client";

/**
 * Home point: where the house is, set once by the owner, kept on the box.
 *
 * No build ships a point and the code
 * has no default town, so a fresh box has no idea where it is until the owner
 * pastes the numbers in here. The weather card, the severe-weather alerts and (later) directions all
 * read the same saved point.
 *
 * Backend contract (all via the same-origin /api/pai proxy, which strips the
 * /api/pai prefix, hence the double /api; "api" is already on the proxy
 * allowlist, see app/_lib/server/proxyAllowlist.ts):
 *   GET    /api/pai/api/home-point  → {set, set_at, source}
 *   POST   /api/pai/api/home-point  {point: "lat, lon"} → same shape | {error}
 *   DELETE /api/pai/api/home-point  → same shape
 *
 * WRITE-ONLY. The backend never returns the numbers, and this card never shows
 * them back: the field is emptied the moment a save succeeds, and nothing here
 * logs or stores what was typed.
 */

import { useCallback, useEffect, useRef, useState } from "react";

const FREDOKA = "var(--font-fredoka), sans-serif";
const NUNITO = "var(--font-nunito), sans-serif";
const MONO = "var(--font-jbmono), monospace";

const ENDPOINT = "/api/pai/api/home-point";

const cardStyle: React.CSSProperties = {
  borderRadius: 30,
  border: "1px solid var(--line)",
  background: "var(--card)",
  backdropFilter: "blur(18px)",
  WebkitBackdropFilter: "blur(18px)",
  padding: "26px 28px",
  boxShadow: "var(--shadow)",
};

const microLabel: React.CSSProperties = {
  fontFamily: MONO,
  fontWeight: 700,
  fontSize: 11,
  letterSpacing: ".14em",
  textTransform: "uppercase",
  color: "var(--text3)",
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  height: 48,
  padding: "0 16px",
  borderRadius: 14,
  border: "1px solid var(--line)",
  background: "var(--card2)",
  color: "var(--text)",
  fontFamily: NUNITO,
  fontWeight: 600,
  fontSize: 15,
  outline: "none",
};

const primaryBtn: React.CSSProperties = {
  height: 44,
  padding: "0 22px",
  borderRadius: 99,
  border: "1px solid transparent",
  background: "linear-gradient(140deg,var(--pink),var(--magenta))",
  color: "#fff",
  fontFamily: NUNITO,
  fontWeight: 700,
  fontSize: 14,
  cursor: "pointer",
};

const secondaryBtn: React.CSSProperties = {
  height: 44,
  padding: "0 18px",
  borderRadius: 99,
  border: "1px solid var(--line)",
  background: "var(--card2)",
  color: "var(--text2)",
  fontFamily: NUNITO,
  fontWeight: 700,
  fontSize: 14,
  cursor: "pointer",
};

interface HomePointStatus {
  set: boolean;
  set_at: string | null;
  source: "owner" | "config" | null;
}

type Note = { kind: "ok" | "err"; text: string } | null;

/** "3 Oct 2026" from an ISO stamp; null when missing or unreadable. */
function dateText(ts: string | null): string | null {
  if (!ts) return null;
  const t = new Date(ts);
  if (Number.isNaN(t.getTime())) return null;
  return t.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** Only the three fields the contract allows; anything else is dropped. */
function toStatus(d: unknown): HomePointStatus | null {
  if (!d || typeof d !== "object") return null;
  const o = d as Record<string, unknown>;
  if (typeof o.set !== "boolean") return null;
  return {
    set: o.set,
    set_at: typeof o.set_at === "string" ? o.set_at : null,
    source: o.source === "owner" || o.source === "config" ? o.source : null,
  };
}

export default function HomePointCard() {
  const [status, setStatus] = useState<HomePointStatus | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note>(null);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    try {
      const r = await fetch(ENDPOINT, {
        cache: "no-store",
        signal: AbortSignal.timeout(8000),
      });
      const s = r.ok ? toStatus(await r.json().catch(() => null)) : null;
      if (!alive.current) return;
      if (s) {
        setStatus(s);
        setLoadFailed(false);
      } else {
        setLoadFailed(true);
      }
    } catch {
      if (alive.current) setLoadFailed(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const send = useCallback(
    async (method: "POST" | "DELETE") => {
      if (busy) return;
      if (method === "POST" && !text.trim()) return;
      setBusy(true);
      setNote(null);
      try {
        const r = await fetch(ENDPOINT, {
          method,
          cache: "no-store",
          headers:
            method === "POST" ? { "Content-Type": "application/json" } : {},
          body:
            method === "POST" ? JSON.stringify({ point: text.trim() }) : undefined,
          signal: AbortSignal.timeout(12000),
        });
        const d = (await r.json().catch(() => null)) as
          | (HomePointStatus & { error?: string })
          | null;
        if (!alive.current) return;
        const s = r.ok ? toStatus(d) : null;
        if (s) {
          setStatus(s);
          // Write-only: the numbers leave the screen once they are saved.
          setText("");
          setNote({
            kind: "ok",
            text: method === "POST" ? "Saved." : "Cleared.",
          });
        } else {
          setNote({
            kind: "err",
            text:
              d?.error ||
              (method === "POST"
                ? "That didn't save. Try again."
                : "That didn't clear. Try again."),
          });
        }
      } catch {
        if (alive.current) {
          setNote({ kind: "err", text: "Couldn't reach the box. Try again." });
        }
      } finally {
        if (alive.current) setBusy(false);
      }
    },
    [busy, text]
  );

  const when = dateText(status?.set_at ?? null);
  let stateText = "Not set";
  if (status?.set) {
    stateText = when ? `Set ${when}` : "Set";
    if (status.source === "config") stateText += ", in the box's config file";
  }

  return (
    <div id="home-point" style={cardStyle} className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between gap-3">
        <div
          style={{
            fontFamily: FREDOKA,
            fontWeight: 600,
            fontSize: 24,
            color: "var(--text)",
          }}
        >
          Home point
        </div>
        <div
          style={{
            ...microLabel,
            color: status?.set ? "var(--mint)" : "var(--text3)",
          }}
        >
          {status ? stateText : loadFailed ? "Unavailable" : "Reading"}
        </div>
      </div>

      <div>
        <label
          htmlFor="home-point-input"
          style={{
            display: "block",
            fontFamily: NUNITO,
            fontWeight: 700,
            fontSize: 13,
            color: "var(--text2)",
            marginBottom: 6,
          }}
        >
          Paste your home point (latitude, longitude)
        </label>
        <input
          id="home-point-input"
          type="text"
          inputMode="decimal"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void send("POST");
          }}
          maxLength={64}
          style={inputStyle}
          autoComplete="off"
          spellCheck={false}
        />
        <div
          style={{
            marginTop: 6,
            fontFamily: NUNITO,
            fontWeight: 500,
            fontSize: 13,
            lineHeight: 1.4,
            color: "var(--text3)",
          }}
        >
          In a phone maps app, long-press your home and copy the numbers.
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void send("POST")}
          disabled={busy || !text.trim()}
          style={{ ...primaryBtn, opacity: busy || !text.trim() ? 0.5 : 1 }}
        >
          Save
        </button>
        <button
          type="button"
          onClick={() => void send("DELETE")}
          disabled={busy || status?.source !== "owner"}
          title={
            status?.source === "config"
              ? "This point is in the box's config file, not saved here."
              : undefined
          }
          style={{
            ...secondaryBtn,
            opacity: busy || status?.source !== "owner" ? 0.5 : 1,
          }}
        >
          Clear
        </button>
      </div>

      {note && (
        <div
          role="status"
          style={{
            fontFamily: NUNITO,
            fontWeight: 600,
            fontSize: 13,
            lineHeight: 1.4,
            color: note.kind === "ok" ? "var(--mint)" : "var(--pink)",
          }}
        >
          {note.text}
        </div>
      )}

      <div
        style={{
          fontFamily: NUNITO,
          fontWeight: 600,
          fontSize: 13,
          lineHeight: 1.45,
          color: "var(--text3)",
        }}
      >
        Saved on this box only. Puff uses it for weather and directions. It&apos;s
        never written to logs.
      </div>
    </div>
  );
}
