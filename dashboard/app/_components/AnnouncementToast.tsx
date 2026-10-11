"use client";

/**
 * AnnouncementToast
 * -----------------
 * Polls /announcements every 10 seconds (the backend announcer's rolling
 * queue — everything PuffPAI proactively raised its hand about, whether or
 * not the voice satellite was online to say it). Entries newer than this
 * device's persisted cursor slide in as toasts; entries the satellite did
 * NOT already speak (`spoken: false`) are also said out loud here through
 * the same TTS path chat replies use — the avatar's mouth when she's on
 * screen, plain PCM otherwise, browser voice as a last resort.
 *
 * Conventions honored:
 * - polling skips hidden tabs (same as useAppData).
 * - ON THE KIOSK it never speaks (one speaker in the room, lib/roomSpeech.ts):
 *   the voice service announces through the same speakers, so the kiosk only
 *   shows the card. Phones and laptops elsewhere still read it aloud.
 * - the only looping animation is the conditional speaking dot, tagged
 *   `.puff-live` so kiosk/idle never freeze it mid-announcement.
 * - first poll on a fresh device bootstraps the cursor silently — old
 *   queue entries are history, not news (DeviceDiscoveryToast does the
 *   same with its seen-set).
 *
 * Mount once at the dashboard layout level.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import MsIcon from "./MsIcon";
import { apiGet } from "../_lib/api";
import { speak } from "../_lib/services/voiceService";
import { isKiosk } from "../_lib/kioskActivity";
import { decideSpeech } from "../_lib/roomSpeech";
import { voiceServiceNow } from "../_lib/hooks/useVoiceState";
import { useAppDispatch } from "../_stores/hooks";
import { setAvatarSpeaking } from "../_stores/avatarSlice";

interface Announcement {
  ts: string;
  text: string;
  source: string;
  spoken: boolean;
  sink: string | null;
}

interface ToastEntry extends Announcement {
  /** currently being read aloud on THIS device */
  saying: boolean;
}

const POLL_MS = 10_000;
const AUTO_DISMISS_MS = 18_000;
/** re-check cadence when a toast is still speaking at dismiss time */
const DISMISS_RETRY_MS = 6_000;
/** hard cap so a wedged AudioContext can never jam the speech queue */
const SPEAK_TIMEOUT_MS = 45_000;
const MAX_STACK = 3;
const CURSOR_KEY = "puffpai_announce_cursor";

export default function AnnouncementToast() {
  const dispatch = useAppDispatch();
  const [toasts, setToasts] = useState<ToastEntry[]>([]);
  // On /dashboard the hero's Puff dock owns the top-right corner — she is
  // usually the one talking, so drop the stack just below her pocket
  // instead of covering her face. Everywhere else the fallback top applies.
  const [topPx, setTopPx] = useState<number | null>(null);
  const cursorRef = useRef<string | null>(null);
  // Serial speech: announcements never talk over each other.
  const speechChainRef = useRef<Promise<void>>(Promise.resolve());
  const dismissTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map(),
  );

  const removeToast = useCallback((ts: string) => {
    const timer = dismissTimersRef.current.get(ts);
    if (timer) clearTimeout(timer);
    dismissTimersRef.current.delete(ts);
    setToasts((prev) => prev.filter((t) => t.ts !== ts));
  }, []);

  const armDismiss = useCallback(
    (ts: string, delay: number) => {
      const prev = dismissTimersRef.current.get(ts);
      if (prev) clearTimeout(prev);
      dismissTimersRef.current.set(
        ts,
        setTimeout(() => {
          // Still mid-speech? Hold the card until the voice finishes.
          setToasts((current) => {
            const t = current.find((x) => x.ts === ts);
            if (t?.saying) {
              armDismiss(ts, DISMISS_RETRY_MS);
              return current;
            }
            dismissTimersRef.current.delete(ts);
            return current.filter((x) => x.ts !== ts);
          });
        }, delay),
      );
    },
    [],
  );

  const markSaying = useCallback((ts: string, saying: boolean) => {
    setToasts((prev) =>
      prev.map((t) => (t.ts === ts ? { ...t, saying } : t)),
    );
  }, []);

  const speakOne = useCallback(
    async (a: Announcement) => {
      markSaying(a.ts, true);
      // Same dispatch chat's speakResponse does: the avatar lipsyncs and
      // IdleMotionGuard counts the voice as presence.
      dispatch(setAvatarSpeaking(true));
      try {
        await Promise.race([
          speak(a.text),
          new Promise<void>((res) => setTimeout(res, SPEAK_TIMEOUT_MS)),
        ]);
      } catch {
        // speech is a courtesy — the toast already carries the words
      } finally {
        dispatch(setAvatarSpeaking(false));
        markSaying(a.ts, false);
      }
    },
    [dispatch, markSaying],
  );

  const enqueueSpeech = useCallback(
    (a: Announcement) => {
      speechChainRef.current = speechChainRef.current
        .then(() => speakOne(a))
        .catch(() => {});
    },
    [speakOne],
  );

  useEffect(() => {
    let cancelled = false;
    try {
      cursorRef.current = localStorage.getItem(CURSOR_KEY);
    } catch {
      /* private mode — session-only cursor */
    }

    const pollOnce = async () => {
      if (document.hidden) return;
      try {
        const data = await apiGet<{
          ok: boolean;
          announcements: Announcement[];
        }>("/announcements?limit=20");
        if (cancelled || !data?.ok) return;
        const items = (data.announcements || []).filter(
          (a) => a && typeof a.ts === "string" && a.text,
        );
        if (!items.length) return;
        const newest = items[0].ts; // backend returns newest first

        const advance = () => {
          cursorRef.current = newest;
          try {
            localStorage.setItem(CURSOR_KEY, newest);
          } catch {
            /* ignore */
          }
        };

        // First contact on this device: whatever is already queued is
        // history — record where we are and only announce what comes next.
        if (!cursorRef.current) {
          advance();
          return;
        }
        const cursor = cursorRef.current;
        // ISO-8601 UTC strings from one writer: string order = time order.
        const fresh = items.filter((a) => a.ts > cursor).reverse();
        if (!fresh.length) return;
        advance();

        // On the box's own screen the voice service owns the room speakers
        // and announces these itself: show the card, never a second voice.
        // A machine with no voice unit leaves it to this screen.
        const now = Date.now();
        const houseSays =
          decideSpeech({
            kiosk: isKiosk(),
            voiceService: voiceServiceNow(),
            source: "announcement",
            roomSpeaking: false,
            createdAtMs: now,
            nowMs: now,
          }) === "skip";

        for (const raw of fresh) {
          const a = houseSays ? { ...raw, spoken: true } : raw;
          setToasts((prev) => {
            if (prev.some((t) => t.ts === a.ts)) return prev;
            const next = [...prev, { ...a, saying: false }];
            // Oldest card yields when the stack is full.
            for (const dropped of next.slice(0, -MAX_STACK)) {
              const timer = dismissTimersRef.current.get(dropped.ts);
              if (timer) clearTimeout(timer);
              dismissTimersRef.current.delete(dropped.ts);
            }
            return next.slice(-MAX_STACK);
          });
          armDismiss(a.ts, AUTO_DISMISS_MS);
          // spoken=true means the house satellite already said it —
          // render the card, never double-speak.
          if (!a.spoken) enqueueSpeech(a);
        }
      } catch {
        // backend hiccup — next tick
      }
    };

    pollOnce();
    const id = setInterval(pollOnce, POLL_MS);
    const timers = dismissTimersRef.current;
    return () => {
      cancelled = true;
      clearInterval(id);
      timers.forEach((t) => clearTimeout(t));
      timers.clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!toasts.length) return;
    const dock = document.querySelector("[data-puff-dock]");
    const r = dock?.getBoundingClientRect();
    setTopPx(
      r && r.width > 0 && r.bottom > 0 && r.bottom < window.innerHeight * 0.6
        ? Math.round(r.bottom + 28)
        : null,
    );
  }, [toasts.length]);

  if (!toasts.length) return null;

  return (
    <div
      className="fixed top-16 right-3 left-3 z-50 flex flex-col gap-2 sm:top-20 sm:right-6 sm:left-auto sm:w-[380px]"
      style={topPx !== null ? { top: topPx } : undefined}
      role="status"
      aria-live="polite"
    >
      {toasts.map((t) => (
        <div key={t.ts} className="announce-in overflow-hidden rounded-lg">
          <div
            className="border backdrop-blur-md"
            style={{
              background: "var(--card)",
              borderColor: "var(--line)",
              boxShadow: "var(--shadow)",
              borderRadius: "inherit",
            }}
          >
            <div
              className="h-[2px]"
              style={{
                background:
                  "linear-gradient(90deg, var(--pink), var(--sky), var(--lav))",
              }}
            />
            <div className="px-3 py-2.5">
              <div className="flex items-center gap-1.5">
                <MsIcon
                  name="campaign"
                  size={15}
                  fill
                  style={{ color: "var(--pink)" }}
                />
                <span
                  className="text-[10px] font-bold tracking-[0.14em] uppercase"
                  style={{ color: "var(--pink)" }}
                >
                  Puff · {t.source || "system"}
                </span>
                <span
                  className="ml-auto font-mono text-[10px] tabular-nums"
                  style={{ color: "var(--text3)" }}
                >
                  {formatTime(t.ts)}
                </span>
                <button
                  onClick={() => removeToast(t.ts)}
                  aria-label="Dismiss"
                  className="-mr-1 grid h-6 w-6 shrink-0 place-items-center rounded"
                  style={{ color: "var(--text3)" }}
                >
                  <MsIcon name="close" size={14} />
                </button>
              </div>
              <p
                className="mt-1 text-[13px] leading-snug"
                style={{ color: "var(--text)" }}
              >
                {t.text}
              </p>
              {(t.saying || t.spoken) && (
                <div
                  className="mt-1.5 flex items-center gap-1.5 text-[10px]"
                  style={{ color: "var(--text3)" }}
                >
                  {t.saying ? (
                    <>
                      <span
                        className="puff-live announce-dot h-1.5 w-1.5 rounded-full"
                        style={{ background: "var(--sky)" }}
                      />
                      <span style={{ color: "var(--sky)" }}>speaking</span>
                    </>
                  ) : (
                    <>
                      <MsIcon name="graphic_eq" size={12} />
                      <span>heard on the house speaker</span>
                    </>
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
      ))}

      <style jsx>{`
        @keyframes announce-in {
          from {
            transform: translateX(24px);
            opacity: 0;
          }
          to {
            transform: translateX(0);
            opacity: 1;
          }
        }
        .announce-in {
          animation: announce-in 320ms cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes announce-dot {
          0%,
          100% {
            opacity: 1;
          }
          50% {
            opacity: 0.25;
          }
        }
        .announce-dot {
          animation: announce-dot 1.1s ease-in-out infinite;
        }
      `}</style>
    </div>
  );
}

function formatTime(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}
