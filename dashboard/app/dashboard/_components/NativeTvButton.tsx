"use client";

/**
 * NativeTvButton: "Watch smooth" on the TV control bar.
 *
 * The kiosk browser has no GPU path, so TV in the Jellyfin frame is decoded
 * and scaled on the CPU (choppy, and it slows the box). This opens a channel
 * picker that plays the pick in the appliance's native player instead: full
 * screen, decoded by a Jetson's video hardware (backend: puffpai/tv_player.py,
 * /api/tv/native/*). Tapping the picture ends it and this page is back.
 *
 * Only on the appliance itself (kiosk class AND a loopback origin, the same
 * rule KioskMediaBridge uses): the native player draws on the wall screen, so
 * a phone pressing it would start TV on a different screen. Hidden as well
 * when the backend says the machine has no native player (not a Jetson).
 *
 * The picker also lists "More free channels (ads)": channels whose ad breaks
 * Jellyfin cannot play through. They play here only, never in the frame.
 *
 * Static: no animation at rest (kiosk motion rules). Polls status only while
 * a channel is starting, then stops.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import MsIcon from "../../_components/MsIcon";

const API = "/api/pai/api/tv/native";

type Channel = { id: string; number?: string | null; name: string };
type FreeChannel = { id: string; name: string; group: string };
type Phase = "idle" | "loading" | "starting" | "failed";

function isAppliance(): boolean {
  if (typeof document === "undefined") return false;
  if (!document.documentElement.classList.contains("puff-kiosk")) return false;
  const host = window.location.hostname;
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

const BTN =
  "flex flex-none items-center gap-2 rounded-full border px-5 shadow-lg transition-opacity hover:opacity-90 active:opacity-75";
const BTN_STYLE = {
  height: 52,
  borderColor: "var(--line)",
  background: "rgba(0,0,0,0.55)",
  color: "#f1f5f9",
  font: "700 16px var(--font-nunito), sans-serif",
} as const;
const ROW =
  "flex w-full items-center gap-3 rounded-lg px-3 text-left transition-opacity hover:opacity-80 active:opacity-60";
const ROW_STYLE = { minHeight: 48, background: "rgba(255,255,255,0.06)", color: "#f1f5f9" } as const;
/* Solid sheet, same as the Cast panel: the TV frame sits right under it, so
   the theme's translucent card colours would let the picture show through. */
const SHEET_STYLE = { background: "rgba(8,10,18,0.96)", borderColor: "var(--line)", color: "#f1f5f9" } as const;
const DIM = "#cbd5e1";
const DIMMER = "#94a3b8";

export default function NativeTvButton({ onBeforePlay }: { onBeforePlay?: () => void }) {
  const [appliance, setAppliance] = useState(false);
  useEffect(() => setAppliance(isAppliance()), []);
  // Only a machine with the native player (a Jetson) shows the button.
  const [supported, setSupported] = useState(false);
  useEffect(() => {
    if (!appliance) return;
    let gone = false;
    fetch(`${API}/status`, { cache: "no-store" })
      .then((r) => r.json())
      .then((s) => {
        if (!gone) setSupported(s?.supported === true);
      })
      .catch(() => {});
    return () => {
      gone = true;
    };
  }, [appliance]);

  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [message, setMessage] = useState("");
  const [channels, setChannels] = useState<Channel[]>([]);
  const [free, setFree] = useState<FreeChannel[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
  }, []);
  useEffect(() => stopPolling, [stopPolling]);

  const loadChannels = useCallback(async () => {
    setPhase("loading");
    setMessage("");
    try {
      const r = await fetch(`${API}/channels`, { cache: "no-store", signal: AbortSignal.timeout(15000) });
      const d = await r.json();
      setChannels(Array.isArray(d?.channels) ? d.channels : []);
      setFree(Array.isArray(d?.free_ads) ? d.free_ads : []);
      setPhase("idle");
      if (d?.error) setMessage("The media server did not answer, so only the free channels are listed.");
    } catch {
      setPhase("failed");
      setMessage("Could not load the channel list. Try again in a moment.");
    }
  }, []);

  const toggle = useCallback(() => {
    setOpen((o) => {
      if (!o && channels.length === 0 && free.length === 0) void loadChannels();
      return !o;
    });
  }, [channels.length, free.length, loadChannels]);

  const play = useCallback(
    async (body: { channel_id: string } | { free_id: string }, name: string) => {
      stopPolling();
      onBeforePlay?.(); // end playback in the frame, so there is one sound
      setPhase("starting");
      setMessage(`Starting ${name}. The picture comes up within about half a minute. Tap the picture to come back here.`);
      try {
        const r = await fetch(`${API}/play`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(30000),
        });
        const d = await r.json().catch(() => ({}));
        if (d?.reason === "bridge_closed" && d?.error) {
          // The switch's own sentence says what is off and where to turn it on.
          setPhase("failed");
          setMessage(d.error);
          return;
        }
        if (!r.ok || !d?.success) {
          setPhase("failed");
          setMessage(`${name} did not start${d?.error ? `: ${d.error}` : ""}. Try another channel.`);
          return;
        }
      } catch {
        setPhase("failed");
        setMessage(`${name} did not start. Try another channel.`);
        return;
      }
      // Wait for the picture (it covers this page when it comes up), or for
      // the player to give up (it waits up to 45 s for the first frame).
      // Checks every 2 s for at most a minute.
      const until = Date.now() + 60_000;
      pollRef.current = setInterval(async () => {
        try {
          const s = await (await fetch(`${API}/status`, { cache: "no-store" })).json();
          if (s?.phase === "playing") {
            stopPolling();
            setPhase("idle");
            setMessage("");
            setOpen(false);
          } else if (s?.phase === "off") {
            stopPolling();
            setPhase("failed");
            setMessage(`${name} stopped before the picture came up. Try another channel.`);
          }
        } catch {
          /* keep waiting */
        }
        if (Date.now() > until) stopPolling();
      }, 2000);
    },
    [onBeforePlay, stopPolling],
  );

  const cancel = useCallback(async () => {
    stopPolling();
    await fetch(`${API}/stop`, { method: "POST" }).catch(() => {});
    setPhase("idle");
    setMessage("");
  }, [stopPolling]);

  if (!appliance || !supported) return null;

  const groups = free.reduce<Record<string, FreeChannel[]>>((acc, c) => {
    (acc[c.group] ||= []).push(c);
    return acc;
  }, {});

  return (
    <div className="relative flex-none">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        aria-label="Watch smooth: play a channel with the hardware video player"
        className={BTN}
        style={BTN_STYLE}
      >
        <MsIcon name="smart_display" size={24} />
        Watch smooth
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Pick a channel to watch smooth"
          className="absolute left-0 top-[60px] z-30 flex max-h-[72vh] w-[min(760px,88vw)] flex-col gap-3 overflow-y-auto rounded-xl border p-4 shadow-2xl"
          style={SHEET_STYLE}
        >
          <div className="flex items-start gap-3">
            <p className="min-w-0 flex-1 text-sm" style={{ color: DIM }}>
              Full screen on this display, smooth playback. Tap the picture to come back.
            </p>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close the channel list"
              className="flex h-10 w-10 flex-none items-center justify-center rounded-full border"
              style={{ borderColor: "var(--line)" }}
            >
              <MsIcon name="close" size={22} />
            </button>
          </div>

          {message && (
            <div className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm" style={{ background: "rgba(255,255,255,0.08)" }}>
              <span className="min-w-0 flex-1">{message}</span>
              {phase === "starting" && (
                <button type="button" onClick={cancel} className="rounded-full border px-4 py-1 font-bold" style={{ borderColor: "var(--line)" }}>
                  Cancel
                </button>
              )}
              {phase === "failed" && channels.length === 0 && free.length === 0 && (
                <button type="button" onClick={loadChannels} className="rounded-full border px-4 py-1 font-bold" style={{ borderColor: "var(--line)" }}>
                  Try again
                </button>
              )}
            </div>
          )}
          {phase === "loading" && <p className="text-sm" style={{ color: DIMMER }}>Loading channels…</p>}

          {channels.length > 0 && (
            <section className="flex flex-col gap-1.5">
              <h3 className="text-xs font-bold uppercase tracking-wide" style={{ color: DIMMER }}>Channels</h3>
              {channels.map((c) => (
                <button key={c.id} type="button" disabled={phase === "starting"} onClick={() => play({ channel_id: c.id }, c.name)} className={ROW} style={ROW_STYLE}>
                  <span className="w-12 flex-none text-right font-mono text-sm" style={{ color: DIMMER }}>{c.number ?? ""}</span>
                  <span className="min-w-0 flex-1 truncate">{c.name}</span>
                </button>
              ))}
            </section>
          )}

          {free.length > 0 && (
            <section className="flex flex-col gap-1.5">
              <h3 className="text-xs font-bold uppercase tracking-wide" style={{ color: DIMMER }}>More free channels (ads)</h3>
              <p className="text-xs" style={{ color: DIMMER }}>
                These run their own ad breaks. They play here only, with the smooth player.
              </p>
              {Object.entries(groups).map(([group, list]) => (
                <div key={group} className="flex flex-col gap-1.5">
                  <h4 className="pt-1 text-xs font-bold" style={{ color: DIM }}>{group}</h4>
                  {list.map((c) => (
                    <button key={c.id} type="button" disabled={phase === "starting"} onClick={() => play({ free_id: c.id }, c.name)} className={ROW} style={ROW_STYLE}>
                      <span className="min-w-0 flex-1 truncate">{c.name}</span>
                    </button>
                  ))}
                </div>
              ))}
            </section>
          )}
        </div>
      )}
    </div>
  );
}
