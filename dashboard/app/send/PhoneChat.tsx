"use client";

/**
 * Talk to Puff from /send: the ONE household conversation on the phone.
 *
 * Typed text goes to POST /api/pai/chat/send with `surface: "phone"` into the
 * household thread, the same thread voice, the wall and the dashboard Chat
 * page use, so the backend's chat rules apply unchanged (an HA PIN typed here
 * is held exactly like chat; a private turn is never stored).
 *
 * A voice memo uses the phone's OWN recorder through a file input
 * (`accept="audio/*" capture`): this page is served over plain-http LAN, where
 * getUserMedia is blocked. The file goes to POST /api/pai/chat/voice-memo; the
 * box transcribes it and handles the words like typed text, stored as
 * "Voice memo: <text>".
 *
 * Both calls ride the pairing-gated catch-all proxy with no client
 * credentials, like the photo upload. A 401 sends the page back to the gate.
 *
 * Refresh is a cheap poll of only the messages newer than the last one seen,
 * every few seconds while the page is visible and nothing is in flight.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import MsIcon from "../_components/MsIcon";
import {
  HOUSEHOLD_THREAD_ID,
  chatThreadHref,
  latestTimestamp,
  newServerMessages,
  withIds,
  type ThreadMessage,
} from "../_lib/householdThread";

const POLL_MS = 5000;
const SHOW_LAST = 12;
const MEMO_MAX_BYTES = 10 * 1024 * 1024; // the backend's cap
const PRIVATE_LINE = "(kept private)";

type Msg = ThreadMessage & { id: string };
type Busy = null | "sending" | "listening";

function threadUrl(since?: string): string {
  const q = `c=${encodeURIComponent(HOUSEHOLD_THREAD_ID)}`;
  return `/api/pai/chat/history?${q}${since ? `&since=${encodeURIComponent(since)}` : ""}`;
}

let localSeq = 0;
function localMsg(role: "user" | "assistant", content: string, surface = "phone"): Msg {
  localSeq += 1;
  return { id: `local-${localSeq}`, role, content, surface, timestamp: "" };
}

export default function PhoneChat({ onUnpaired }: { onUnpaired: () => void }) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);

  const lastSeen = useRef("");
  const busyRef = useRef<Busy>(null);
  const messagesRef = useRef<Msg[]>([]);
  const memoRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);
  useEffect(() => {
    messagesRef.current = messages;
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [messages]);

  /* ── first load + cheap refresh ───────────────────────────────────── */
  const pull = useCallback(
    async (initial: boolean) => {
      try {
        const res = await fetch(threadUrl(initial ? undefined : lastSeen.current || undefined), {
          cache: "no-store",
        });
        if (res.status === 401) {
          onUnpaired();
          return;
        }
        if (!res.ok) return;
        const data = (await res.json()) as {
          conversation?: { messages?: ThreadMessage[] } | null;
        };
        const fetched = data?.conversation?.messages ?? [];
        const newest = latestTimestamp(fetched);
        if (newest > lastSeen.current) lastSeen.current = newest;
        if (initial) {
          setMessages(withIds(fetched.slice(-SHOW_LAST)));
          return;
        }
        if (busyRef.current) return;
        const fresh = newServerMessages(fetched, messagesRef.current);
        if (fresh.length) {
          setMessages((prev) => [...prev, ...withIds(fresh)].slice(-SHOW_LAST * 2));
        }
      } catch {
        /* offline: the next tick tries again */
      } finally {
        if (initial) setLoaded(true);
      }
    },
    [onUnpaired],
  );

  useEffect(() => {
    void pull(true);
    const t = setInterval(() => {
      if (busyRef.current) return;
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      void pull(false);
    }, POLL_MS);
    return () => clearInterval(t);
  }, [pull]);

  /* ── a finished turn, from either route ───────────────────────────── */
  const landTurn = useCallback(
    (userLine: string, data: { response?: string; private?: boolean }) => {
      setMessages((prev) => {
        const out = [...prev];
        // The optimistic line becomes what the box actually kept.
        for (let i = out.length - 1; i >= 0; i--) {
          if (out[i].id.startsWith("local-") && out[i].role === "user") {
            out[i] = { ...out[i], content: data.private ? PRIVATE_LINE : userLine };
            break;
          }
        }
        out.push(localMsg("assistant", data.response || ""));
        return out.slice(-SHOW_LAST * 2);
      });
    },
    [],
  );

  /* ── typed message ────────────────────────────────────────────────── */
  const sendText = useCallback(async () => {
    const text = draft.trim();
    if (!text || busy) return;
    setError(null);
    setBusy("sending");
    setDraft("");
    const pending = localMsg("user", text);
    setMessages((prev) => [...prev, pending]);
    // A failed or refused send takes its line back off the screen (it may be
    // a PIN), and the words are not put back in the box either.
    const dropPending = () => setMessages((prev) => prev.filter((m) => m.id !== pending.id));
    try {
      const res = await fetch("/api/pai/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: text,
          conversation_id: HOUSEHOLD_THREAD_ID,
          surface: "phone",
        }),
      });
      if (res.status === 401) {
        dropPending();
        onUnpaired();
        return;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data?.success === false) {
        dropPending();
        setError(res.status === 502 ? "Puff's brain isn't answering." : "Puff couldn't answer that.");
        return;
      }
      landTurn(text, data);
    } catch {
      dropPending();
      setError("Lost the connection.");
    } finally {
      setBusy(null);
    }
  }, [busy, draft, landTurn, onUnpaired]);

  /* ── voice memo ───────────────────────────────────────────────────── */
  const sendMemo = useCallback(
    (file: File) => {
      setError(null);
      if (file.size > MEMO_MAX_BYTES) {
        setError("That memo is too big, 10 MB max (about 2 minutes).");
        return;
      }
      setBusy("listening");
      setMessages((prev) => [...prev, localMsg("user", "Voice memo…")]);

      const body = new FormData();
      body.append("file", file);
      body.append("conversation_id", HOUSEHOLD_THREAD_ID);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/pai/chat/voice-memo");
      const dropPending = () =>
        setMessages((prev) =>
          prev.filter((m) => !(m.id.startsWith("local-") && m.content === "Voice memo…")),
        );
      xhr.onload = () => {
        setBusy(null);
        if (xhr.status === 401) {
          dropPending();
          onUnpaired();
          return;
        }
        let data: {
          success?: boolean;
          message?: string;
          response?: string;
          private?: boolean;
          transcript?: string | null;
        } = {};
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          /* non-JSON: handled as a failure below */
        }
        if (xhr.status >= 200 && xhr.status < 300 && data.success !== false) {
          landTurn(`Voice memo: ${data.transcript ?? ""}`, data);
          return;
        }
        dropPending();
        setError(
          data.message ||
            (xhr.status === 413
              ? "That memo is too long, 2 minutes max."
              : xhr.status === 502
                ? "Puff's brain isn't answering."
                : `Memo failed (${xhr.status}).`),
        );
      };
      xhr.onerror = () => {
        setBusy(null);
        dropPending();
        setError("Lost the connection.");
      };
      xhr.send(body);
    },
    [landTurn, onUnpaired],
  );

  const pickMemo = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (file) sendMemo(file);
  };

  const card: React.CSSProperties = {
    background: "var(--card)",
    border: "1px solid var(--line)",
    borderRadius: 24,
    boxShadow: "var(--shadow)",
    backdropFilter: "blur(18px)",
    WebkitBackdropFilter: "blur(18px)",
  };

  return (
    <section style={card} className="mb-3 overflow-hidden" aria-label="Talk to Puff">
      <div className="flex items-baseline justify-between px-5 pt-4">
        <span
          style={{ fontFamily: "var(--font-fredoka)", fontSize: 17, fontWeight: 600, color: "var(--text)" }}
        >
          Talk to Puff
        </span>
        <a
          href={chatThreadHref(HOUSEHOLD_THREAD_ID)}
          className="flex items-center gap-1 text-xs"
          style={{ color: "var(--sky)" }}
        >
          Open full chat
          <MsIcon name="open_in_new" size={14} />
        </a>
      </div>

      {/* Recent turns of the household conversation */}
      <div className="max-h-72 overflow-y-auto px-5 py-3" aria-live="polite">
        {!loaded && <div className="h-10 animate-pulse rounded-xl" style={{ background: "var(--chip)" }} />}
        {loaded && messages.length === 0 && (
          <p className="text-sm" style={{ color: "var(--text3)" }}>
            Ask Puff anything. Voice, the wall and the dashboard all see this conversation.
          </p>
        )}
        {messages.map((m) => {
          const mine = m.role === "user";
          return (
            <div key={m.id} className={`mb-2 flex ${mine ? "justify-end" : "justify-start"}`}>
              <div
                className="max-w-[85%] whitespace-pre-wrap text-sm"
                style={{
                  padding: "8px 12px",
                  borderRadius: 16,
                  background: mine
                    ? "color-mix(in srgb, var(--pink) 18%, transparent)"
                    : "var(--card2)",
                  border: "1px solid var(--line)",
                  color: "var(--text)",
                }}
              >
                {m.content}
                {mine && m.surface && m.surface !== "phone" && (
                  <span
                    className="ml-2 uppercase"
                    style={{ color: "var(--text3)", fontFamily: "var(--font-jbmono)", fontSize: 9 }}
                  >
                    {m.surface}
                  </span>
                )}
              </div>
            </div>
          );
        })}
        {busy && (
          <p className="animate-pulse text-xs" style={{ color: "var(--text3)" }}>
            {busy === "listening" ? "Puff is listening to your memo…" : "Puff is thinking…"}
          </p>
        )}
        <div ref={endRef} />
      </div>

      {error && (
        <p role="alert" className="px-5 pb-2 text-xs" style={{ color: "var(--puff-red)" }}>
          {error}
        </p>
      )}

      {/* Message box + voice memo */}
      <div className="flex items-center gap-2 px-3 pb-3" style={{ borderTop: "1px solid var(--line)", paddingTop: 10 }}>
        <button
          onClick={() => memoRef.current?.click()}
          disabled={!!busy}
          aria-label="Record a voice memo"
          className="flex-none transition disabled:opacity-40"
          style={{
            width: 44,
            height: 44,
            borderRadius: 14,
            border: "1px solid var(--line)",
            background: "var(--card2)",
            color: "var(--pink)",
          }}
        >
          <MsIcon name="mic" size={22} />
        </button>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value.slice(0, 2000))}
          onKeyDown={(e) => {
            if (e.key === "Enter") void sendText();
          }}
          disabled={!!busy}
          placeholder="Message Puff"
          aria-label="Message Puff"
          enterKeyHint="send"
          className="min-w-0 flex-1 outline-none disabled:opacity-40"
          style={{
            padding: "11px 14px",
            borderRadius: 14,
            background: "var(--card2)",
            border: "1px solid var(--line)",
            color: "var(--text)",
            fontSize: 16, // 16px stops iOS zooming into the field
          }}
        />
        <button
          onClick={() => void sendText()}
          disabled={!!busy || !draft.trim()}
          aria-label="Send"
          className="flex-none transition disabled:opacity-40"
          style={{
            width: 44,
            height: 44,
            borderRadius: 14,
            border: "1px solid transparent",
            background: "linear-gradient(135deg, var(--pink), var(--magenta))",
            color: "#fff",
          }}
        >
          <MsIcon name="send" size={20} />
        </button>
      </div>

      {/* The phone's own recorder. Plain-http LAN blocks getUserMedia. */}
      <input
        ref={memoRef}
        type="file"
        accept="audio/*"
        capture
        className="hidden"
        onChange={pickMemo}
      />
    </section>
  );
}
