"use client";

/**
 * The Hatch — first boot's final step, replayable forever.
 *
 * Flow: pre-flight (REAL probes via GET /hatch/preflight — never the lying
 * /health) → the avatar wakes and asks "what's my name?" → the owner types
 * (or speaks) a name → POST /hatch → the Puff speaks her scripted
 * introduction BY NAME through Piper with the talking-mouth avatar, then
 * introduces the crew (display names from the active preset) → done.
 *
 * On an already-hatched build the same page replays the ritual with the
 * existing name; renaming is a separate, explicit control (replays never
 * rename by accident — the backend enforces it too). On pre-flight failure
 * the page REFUSES and says plainly what is broken.
 *
 * Every spoken line is scripted server-side (single utterances, no LLM, no
 * multi-step routing). There are deliberately no share/film prompts here:
 * the moment belongs to the household.
 *
 * Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import MsIcon from "../../_components/MsIcon";
import AvatarStage from "../../_components/Avatar/AvatarStage";
import useAvatarCharacter from "../../_components/Avatar/useAvatarCharacter";
import type { AvatarEngineHandle } from "../../_components/Avatar/engines/types";
import { useAppSelector } from "@/app/_stores/hooks";
import { apiGet, apiPostRaw } from "@/app/_lib/api";
import {
  synthesize,
  playPcmAudio,
  browserSpeak,
  browserSpeakStop,
} from "@/app/_lib/services/voiceService";
import { withRoomSpeaker } from "@/app/_lib/services/roomSpeaker";
import { unlockAudio } from "@/app/_lib/audioUnlock";

/* ── Backend contracts ──────────────────────────────────────── */

interface HatchStatus {
  hatched: boolean;
  name: string | null;
  hatched_at: string | null;
  hatch_date: string | null;
  replays: number;
  ask_line: string;
  certificate_formats: string[];
}

interface Check {
  id: string;
  label: string;
  status: "ok" | "warn" | "fail";
  detail: string;
  blocking: boolean;
}

interface Preflight {
  ok: boolean;
  blocking: Check[];
  checks: Check[];
}

interface ScriptLine {
  id: string;
  text: string;
}

interface HatchResult {
  success?: boolean;
  refused?: boolean;
  message?: string;
  blocking?: Check[];
  replay?: boolean;
  name?: string;
  hatched_at?: string;
  hatch_date?: string;
  replays?: number;
  script?: ScriptLine[];
  note?: string;
}

type Phase =
  | "checking" // running pre-flight + status
  | "refused" // pre-flight found blocking faults
  | "ready" // unhatched, checks pass — waiting for "begin"
  | "ask" // she is awake and has asked for her name
  | "speaking" // the ritual script is being spoken
  | "done" // ritual finished this visit
  | "idle-hatched"; // arrived on an already-hatched build

const API_BASE = "/api/pai";

/* ── Shared style atoms (settings-page idiom) ───────────────── */

const microLabel: React.CSSProperties = {
  fontFamily: "var(--font-jbmono), monospace",
  fontWeight: 700,
  fontSize: 11,
  letterSpacing: ".14em",
  textTransform: "uppercase",
  color: "var(--text3)",
};

const cardStyle: React.CSSProperties = {
  borderRadius: 30,
  border: "1px solid var(--line)",
  background: "var(--card)",
  backdropFilter: "blur(18px)",
  WebkitBackdropFilter: "blur(18px)",
  padding: "26px 28px",
  boxShadow: "var(--shadow)",
};

const primaryButton: React.CSSProperties = {
  height: 52,
  padding: "0 30px",
  borderRadius: 99,
  border: "none",
  cursor: "pointer",
  background: "linear-gradient(140deg,var(--pink),var(--magenta))",
  color: "#fff",
  fontFamily: "var(--font-nunito), sans-serif",
  fontWeight: 700,
  fontSize: 16,
};

const quietButton: React.CSSProperties = {
  height: 52,
  padding: "0 24px",
  borderRadius: 99,
  border: "1px solid var(--line)",
  cursor: "pointer",
  background: "var(--card2)",
  color: "var(--text2)",
  fontFamily: "var(--font-nunito), sans-serif",
  fontWeight: 700,
  fontSize: 15,
};

function CheckRow({ check }: { check: Check }) {
  const color =
    check.status === "ok"
      ? "var(--mint)"
      : check.status === "warn"
        ? "var(--lemon)"
        : "var(--pink)";
  const icon =
    check.status === "ok"
      ? "check_circle"
      : check.status === "warn"
        ? "error"
        : "cancel";
  return (
    <div
      className="flex items-start gap-3"
      style={{ padding: "12px 0", borderTop: "1px solid var(--line)" }}
    >
      <MsIcon name={icon} size={20} style={{ color, marginTop: 2 }} />
      <div className="min-w-0">
        <div
          style={{
            fontFamily: "var(--font-nunito), sans-serif",
            fontWeight: 700,
            fontSize: 15,
            color: "var(--text)",
          }}
        >
          {check.label}
        </div>
        <div
          style={{
            fontFamily: "var(--font-nunito), sans-serif",
            fontWeight: 500,
            fontSize: 13.5,
            lineHeight: 1.4,
            color: "var(--text3)",
            overflowWrap: "anywhere",
          }}
        >
          {check.detail}
        </div>
      </div>
    </div>
  );
}

/* ── Page ───────────────────────────────────────────────────── */

export default function HatchPage() {
  const avatarRef = useRef<AvatarEngineHandle>(null);
  const mountedRef = useRef(true);
  const character = useAvatarCharacter();
  const { avatarUrl, mood } = useAppSelector((state) => state.avatar);

  const [phase, setPhase] = useState<Phase>("checking");
  const [status, setStatus] = useState<HatchStatus | null>(null);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [allowBrowserVoice, setAllowBrowserVoice] = useState(false);
  const [name, setName] = useState("");
  const [caption, setCaption] = useState<string | null>(null);
  const [lineIndex, setLineIndex] = useState(0);
  const [lineTotal, setLineTotal] = useState(0);
  const [result, setResult] = useState<HatchResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showRename, setShowRename] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      browserSpeakStop();
      avatarRef.current?.stopSpeaking();
    };
  }, []);

  /* ── Pre-flight + status ─────────────────────────────────── */

  const runChecks = useCallback(
    async (browserVoiceOk: boolean) => {
      setPhase("checking");
      setError(null);
      try {
        const [st, pf] = await Promise.all([
          apiGet<HatchStatus>("/hatch/status"),
          apiGet<Preflight>(
            `/hatch/preflight?require_voice=${browserVoiceOk ? "0" : "1"}`
          ),
        ]);
        if (!mountedRef.current) return;
        setStatus(st);
        setPreflight(pf);
        if (!pf.ok) {
          setPhase("refused");
        } else if (st.hatched) {
          setPhase("idle-hatched");
        } else {
          setPhase("ready");
        }
      } catch (e) {
        if (!mountedRef.current) return;
        setPreflight(null);
        setError(
          e instanceof Error ? e.message : "The backend is unreachable."
        );
        setPhase("refused");
      }
    },
    []
  );

  useEffect(() => {
    void runChecks(false);
  }, [runChecks]);

  /* ── Speech plumbing ─────────────────────────────────────── */

  const speakOne = useCallback(async (text: string) => {
    // On the kiosk: only when the room speakers are free (lib/roomSpeech).
    await withRoomSpeaker("local-reply", async () => {
      try {
        const tts = await synthesize(text);
        if (tts.success && tts.audio_base64) {
          const rate = tts.sample_rate || 22050;
          if (avatarRef.current?.isReady()) {
            await avatarRef.current.speakPcm(tts.audio_base64, rate, text);
            return;
          }
          await playPcmAudio(tts.audio_base64, rate);
          return;
        }
      } catch {
        /* fall through to browser voice */
      }
      await new Promise<void>((resolve) => {
        browserSpeak(text, resolve);
        setTimeout(resolve, 20000); // safety net if browser TTS dies silently
      });
    });
  }, []);

  const speakScript = useCallback(
    async (lines: ScriptLine[]) => {
      setLineTotal(lines.length);
      for (let i = 0; i < lines.length; i++) {
        if (!mountedRef.current) return;
        setLineIndex(i);
        setCaption(lines[i].text);
        await speakOne(lines[i].text);
        // A breath between beats — the crew intro should land name by name.
        await new Promise((r) => setTimeout(r, 260));
      }
    },
    [speakOne]
  );

  /* ── Ritual actions ──────────────────────────────────────── */

  const begin = useCallback(async () => {
    unlockAudio(); // user gesture — required before any autoplay audio
    setPhase("ask");
    const ask = status?.ask_line || "I'm awake. What's my name?";
    setCaption(ask);
    await speakOne(ask);
  }, [status, speakOne]);

  const performHatch = useCallback(
    async (body: { name?: string; rename?: boolean }) => {
      unlockAudio();
      setBusy(true);
      setError(null);
      try {
        const resp = await apiPostRaw<HatchResult>("/hatch", {
          ...body,
          require_voice: !allowBrowserVoice,
        });
        if (!mountedRef.current) return;
        if (resp.status === 409 && resp.data?.refused) {
          setPreflight({
            ok: false,
            blocking: resp.data.blocking || [],
            checks: resp.data.blocking || [],
          });
          setPhase("refused");
          return;
        }
        if (!resp.ok || !resp.data?.script) {
          setError(resp.data?.message || "The hatch failed — try again.");
          return;
        }
        setResult(resp.data);
        setPhase("speaking");
        await speakScript(resp.data.script);
        if (mountedRef.current) setPhase("done");
      } catch (e) {
        // A dropped connection mid-ritual must say so, not fail silently.
        if (mountedRef.current) {
          setError(
            e instanceof Error
              ? `The backend dropped mid-hatch: ${e.message}`
              : "The backend dropped mid-hatch — check the box and retry."
          );
          // If the script was already fetched the hatch itself landed —
          // settle into "done" (which renders the error); otherwise stay
          // in the phase whose card shows the message.
          setPhase((p) => (p === "speaking" ? "done" : p));
        }
      } finally {
        if (mountedRef.current) setBusy(false);
      }
    },
    [allowBrowserVoice, speakScript]
  );

  /* ── Derived bits ────────────────────────────────────────── */

  const warnings = (preflight?.checks || []).filter(
    (c) => c.status === "warn"
  );
  const voiceOnlyBlocked =
    preflight !== null &&
    !preflight.ok &&
    preflight.blocking.every((c) => c.id === "voice_tts");
  const pngAvailable = (status?.certificate_formats || []).includes("png");
  const certHref = (fmt: "svg" | "png") =>
    `${API_BASE}/hatch/certificate?format=${fmt}&download=1`;
  const puffName = result?.name || status?.name || "";

  /* ── Render ──────────────────────────────────────────────── */

  return (
    <section
      style={{
        padding:
          "clamp(16px,2.4vh,26px) clamp(18px,2vw,36px) clamp(18px,2.8vh,30px)",
        animation: "slide-up 0.3s ease-out",
        maxWidth: 1060,
        margin: "0 auto",
      }}
    >
      <div style={{ ...microLabel, marginBottom: 8 }}>
        {status?.hatched ? "The Hatch · Replay" : "First light · The Hatch"}
      </div>
      <h1
        style={{
          margin: 0,
          fontFamily: "var(--font-fredoka), sans-serif",
          fontWeight: 600,
          fontSize: "clamp(28px,2.6vw,44px)",
          letterSpacing: "-.8px",
          color: "var(--text)",
        }}
      >
        {phase === "done"
          ? `${puffName} lives here now.`
          : status?.hatched
            ? `Hatch day, again`
            : "Every Puff hatches once"}
      </h1>
      <p
        style={{
          margin: "6px 0 22px",
          fontFamily: "var(--font-nunito), sans-serif",
          fontWeight: 500,
          fontSize: 18,
          color: "var(--text2)",
        }}
      >
        {phase === "refused"
          ? "The box has to be honest before it makes promises."
          : "A name, a voice, a household. None of it leaves this box without your consent."}
      </p>

      <div
        className="grid grid-cols-1 items-start lg:grid-cols-[1.1fr_1fr]"
        style={{ gap: "clamp(14px,1.6vw,24px)" }}
      >
        {/* ── Left: the avatar ─────────────────────────────── */}
        <div style={{ ...cardStyle, padding: 0, overflow: "hidden" }}>
          <div style={{ position: "relative", height: 380 }}>
            <AvatarStage
              engineId={character}
              ref={avatarRef}
              avatarUrl={avatarUrl}
              mood={mood}
              view="head"
              className="h-full w-full"
              style={{ height: "380px", minHeight: "380px" }}
            />
            {phase === "speaking" && lineTotal > 0 && (
              <div
                className="absolute left-4 top-4 flex items-center gap-1.5"
                style={{ ...microLabel, color: "var(--text2)" }}
              >
                {Array.from({ length: lineTotal }).map((_, i) => (
                  <span
                    key={i}
                    style={{
                      width: 7,
                      height: 7,
                      borderRadius: "50%",
                      background:
                        i <= lineIndex ? "var(--pink)" : "var(--chip)",
                      transition: "background .2s ease",
                    }}
                  />
                ))}
              </div>
            )}
          </div>
          {caption && (
            <div
              style={{
                padding: "18px 24px 22px",
                borderTop: "1px solid var(--line)",
                fontFamily: "var(--font-nunito), sans-serif",
                fontWeight: 600,
                fontSize: 17,
                lineHeight: 1.5,
                color: "var(--text)",
                minHeight: 64,
              }}
            >
              {caption}
            </div>
          )}
        </div>

        {/* ── Right: state panel ───────────────────────────── */}
        <div className="flex min-w-0 flex-col gap-5">
          {phase === "checking" && (
            <div style={cardStyle}>
              <div style={{ ...microLabel, marginBottom: 12 }}>
                Pre-flight
              </div>
              <div
                style={{
                  fontFamily: "var(--font-nunito), sans-serif",
                  fontWeight: 600,
                  fontSize: 17,
                  color: "var(--text2)",
                }}
              >
                Checking the box — real probes only…
              </div>
            </div>
          )}

          {phase === "refused" && (
            <div style={cardStyle}>
              <div style={{ ...microLabel, marginBottom: 6 }}>
                Pre-flight · refused
              </div>
              <div
                style={{
                  fontFamily: "var(--font-fredoka), sans-serif",
                  fontWeight: 600,
                  fontSize: 24,
                  color: "var(--text)",
                  marginBottom: 6,
                }}
              >
                Not hatching like this
              </div>
              <p
                style={{
                  margin: "0 0 14px",
                  fontFamily: "var(--font-nunito), sans-serif",
                  fontWeight: 500,
                  fontSize: 15,
                  lineHeight: 1.5,
                  color: "var(--text2)",
                }}
              >
                The first thing this Puff says is &ldquo;everything you tell
                me stays in this box.&rdquo; She won&rsquo;t say it while part
                of the box is down. Broken right now:
              </p>
              {error && (
                <CheckRow
                  check={{
                    id: "backend",
                    label: "Backend",
                    status: "fail",
                    detail: error,
                    blocking: true,
                  }}
                />
              )}
              {(preflight?.blocking || []).map((c) => (
                <CheckRow key={c.id} check={c} />
              ))}
              <div className="mt-5 flex flex-wrap gap-3">
                <button
                  type="button"
                  style={primaryButton}
                  onClick={() => void runChecks(allowBrowserVoice)}
                >
                  Run the checks again
                </button>
                {voiceOnlyBlocked && !allowBrowserVoice && (
                  <button
                    type="button"
                    style={quietButton}
                    onClick={() => {
                      setAllowBrowserVoice(true);
                      void runChecks(true);
                    }}
                  >
                    Continue with the browser&rsquo;s voice
                  </button>
                )}
              </div>
            </div>
          )}

          {phase === "ready" && (
            <div style={cardStyle}>
              <div style={{ ...microLabel, marginBottom: 6 }}>
                Pre-flight · clear
              </div>
              <div
                style={{
                  fontFamily: "var(--font-fredoka), sans-serif",
                  fontWeight: 600,
                  fontSize: 24,
                  color: "var(--text)",
                  marginBottom: 6,
                }}
              >
                The box checks out
              </div>
              <p
                style={{
                  margin: "0 0 16px",
                  fontFamily: "var(--font-nunito), sans-serif",
                  fontWeight: 500,
                  fontSize: 15,
                  lineHeight: 1.5,
                  color: "var(--text2)",
                }}
              >
                Voice loaded, crew present, config writable. She&rsquo;s ready
                to wake up and ask you one question.
              </p>
              {allowBrowserVoice && (
                <p
                  style={{
                    margin: "0 0 16px",
                    fontFamily: "var(--font-nunito), sans-serif",
                    fontWeight: 600,
                    fontSize: 14,
                    color: "var(--lemon)",
                  }}
                >
                  Piper isn&rsquo;t loaded — she&rsquo;ll borrow the
                  browser&rsquo;s voice for now.
                </p>
              )}
              <button type="button" style={primaryButton} onClick={begin}>
                Wake her up
              </button>
            </div>
          )}

          {phase === "ask" && (
            <div style={cardStyle}>
              <div style={{ ...microLabel, marginBottom: 12 }}>
                She&rsquo;s waiting
              </div>
              <label
                htmlFor="puff-name"
                style={{
                  display: "block",
                  fontFamily: "var(--font-fredoka), sans-serif",
                  fontWeight: 600,
                  fontSize: 24,
                  color: "var(--text)",
                  marginBottom: 14,
                }}
              >
                Name your Puff
              </label>
              <input
                id="puff-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && name.trim() && !busy) {
                    void performHatch({ name: name.trim() });
                  }
                }}
                maxLength={40}
                autoFocus
                placeholder="Nimbus, Stratus, Beatrix…"
                style={{
                  width: "100%",
                  height: 56,
                  padding: "0 20px",
                  borderRadius: 18,
                  border: "1px solid var(--line)",
                  background: "var(--card2)",
                  color: "var(--text)",
                  fontFamily: "var(--font-fredoka), sans-serif",
                  fontWeight: 500,
                  fontSize: 22,
                  outline: "none",
                  marginBottom: 16,
                }}
              />
              <button
                type="button"
                style={{
                  ...primaryButton,
                  opacity: name.trim() && !busy ? 1 : 0.5,
                  cursor: name.trim() && !busy ? "pointer" : "not-allowed",
                }}
                disabled={!name.trim() || busy}
                onClick={() => void performHatch({ name: name.trim() })}
              >
                {busy ? "Hatching…" : "Hatch"}
              </button>
              {error && (
                <p
                  style={{
                    margin: "12px 0 0",
                    fontFamily: "var(--font-nunito), sans-serif",
                    fontWeight: 600,
                    fontSize: 14,
                    color: "var(--pink)",
                  }}
                >
                  {error}
                </p>
              )}
            </div>
          )}

          {phase === "speaking" && (
            <div style={cardStyle}>
              <div style={{ ...microLabel, marginBottom: 12 }}>
                {result?.replay ? "Replaying the ritual" : "Hatching"}
              </div>
              <div
                style={{
                  fontFamily: "var(--font-nunito), sans-serif",
                  fontWeight: 600,
                  fontSize: 16,
                  lineHeight: 1.5,
                  color: "var(--text2)",
                }}
              >
                Line {Math.min(lineIndex + 1, lineTotal)} of {lineTotal} — the
                whole script is written, not generated. She can&rsquo;t get
                her own introduction wrong.
              </div>
            </div>
          )}

          {phase === "done" && result && (
            <div style={cardStyle}>
              <div style={{ ...microLabel, marginBottom: 6 }}>
                {result.replay ? "Replay complete" : "Hatched"}
              </div>
              <div
                style={{
                  fontFamily: "var(--font-fredoka), sans-serif",
                  fontWeight: 600,
                  fontSize: 30,
                  color: "var(--text)",
                  marginBottom: 4,
                }}
              >
                {result.name}
              </div>
              <div
                style={{
                  fontFamily: "var(--font-jbmono), monospace",
                  fontSize: 13,
                  color: "var(--text3)",
                  marginBottom: 18,
                }}
              >
                HATCHED {result.hatch_date}
                {result.replay ? ` · REPLAY #${result.replays}` : ""}
              </div>
              {result.note && (
                <p
                  style={{
                    margin: "0 0 14px",
                    fontFamily: "var(--font-nunito), sans-serif",
                    fontWeight: 600,
                    fontSize: 14,
                    color: "var(--lemon)",
                  }}
                >
                  {result.note}
                </p>
              )}
              {error && (
                <p
                  style={{
                    margin: "0 0 14px",
                    fontFamily: "var(--font-nunito), sans-serif",
                    fontWeight: 600,
                    fontSize: 14,
                    color: "var(--pink)",
                  }}
                >
                  {error}
                </p>
              )}
              <div className="flex flex-wrap gap-3">
                <a
                  href={certHref("svg")}
                  style={{
                    ...primaryButton,
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 8,
                    textDecoration: "none",
                  }}
                >
                  <MsIcon name="workspace_premium" size={20} />
                  Adoption certificate
                </a>
                {pngAvailable && (
                  <a
                    href={certHref("png")}
                    style={{
                      ...quietButton,
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 8,
                      textDecoration: "none",
                    }}
                  >
                    PNG copy
                  </a>
                )}
                <Link href="/dashboard" style={{ ...quietButton, display: "inline-flex", alignItems: "center", textDecoration: "none" }}>
                  To the dashboard
                </Link>
              </div>
            </div>
          )}

          {phase === "idle-hatched" && status && (
            <div style={cardStyle}>
              <div style={{ ...microLabel, marginBottom: 6 }}>
                Already home
              </div>
              <div
                style={{
                  fontFamily: "var(--font-fredoka), sans-serif",
                  fontWeight: 600,
                  fontSize: 30,
                  color: "var(--text)",
                  marginBottom: 4,
                }}
              >
                {status.name}
              </div>
              <div
                style={{
                  fontFamily: "var(--font-jbmono), monospace",
                  fontSize: 13,
                  color: "var(--text3)",
                  marginBottom: 18,
                }}
              >
                HATCHED {status.hatch_date}
                {status.replays > 0 ? ` · ${status.replays} REPLAYS` : ""}
              </div>
              <div className="flex flex-wrap gap-3">
                <button
                  type="button"
                  style={primaryButton}
                  disabled={busy}
                  onClick={() => void performHatch({})}
                >
                  {busy ? "Replaying…" : "Replay the ritual"}
                </button>
                <a
                  href={certHref("svg")}
                  style={{
                    ...quietButton,
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 8,
                    textDecoration: "none",
                  }}
                >
                  <MsIcon name="workspace_premium" size={20} />
                  Certificate
                </a>
                {pngAvailable && (
                  <a
                    href={certHref("png")}
                    style={{
                      ...quietButton,
                      display: "inline-flex",
                      alignItems: "center",
                      textDecoration: "none",
                    }}
                  >
                    PNG
                  </a>
                )}
              </div>
              <div style={{ marginTop: 20, borderTop: "1px solid var(--line)", paddingTop: 16 }}>
                {!showRename ? (
                  <button
                    type="button"
                    onClick={() => setShowRename(true)}
                    style={{
                      ...microLabel,
                      background: "none",
                      border: "none",
                      cursor: "pointer",
                      padding: 0,
                    }}
                  >
                    Rename this Puff…
                  </button>
                ) : (
                  <div>
                    <p
                      style={{
                        margin: "0 0 10px",
                        fontFamily: "var(--font-nunito), sans-serif",
                        fontWeight: 500,
                        fontSize: 14,
                        lineHeight: 1.5,
                        color: "var(--text2)",
                      }}
                    >
                      Renaming is deliberate — a replay alone never changes
                      the name. The hatch date stays.
                    </p>
                    <div className="flex flex-wrap gap-3">
                      <input
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        maxLength={40}
                        placeholder={status.name || "New name"}
                        style={{
                          flex: 1,
                          minWidth: 160,
                          height: 48,
                          padding: "0 16px",
                          borderRadius: 14,
                          border: "1px solid var(--line)",
                          background: "var(--card2)",
                          color: "var(--text)",
                          fontFamily: "var(--font-fredoka), sans-serif",
                          fontWeight: 500,
                          fontSize: 18,
                          outline: "none",
                        }}
                      />
                      <button
                        type="button"
                        style={{
                          ...quietButton,
                          opacity: name.trim() && !busy ? 1 : 0.5,
                        }}
                        disabled={!name.trim() || busy}
                        onClick={() =>
                          void performHatch({
                            name: name.trim(),
                            rename: true,
                          })
                        }
                      >
                        Rename and replay
                      </button>
                    </div>
                  </div>
                )}
              </div>
              {error && (
                <p
                  style={{
                    margin: "12px 0 0",
                    fontFamily: "var(--font-nunito), sans-serif",
                    fontWeight: 600,
                    fontSize: 14,
                    color: "var(--pink)",
                  }}
                >
                  {error}
                </p>
              )}
            </div>
          )}

          {/* Non-blocking warnings — honest, quiet */}
          {warnings.length > 0 && phase !== "checking" && (
            <div style={{ ...cardStyle, padding: "18px 24px" }}>
              <div style={{ ...microLabel, marginBottom: 4 }}>
                Worth knowing
              </div>
              {warnings.map((c) => (
                <CheckRow key={c.id} check={c} />
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
