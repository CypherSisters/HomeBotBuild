"use client";

/**
 * KioskMediaBridge — the appliance's hands for radio.
 *
 * The World Radio player is a browser audio element (see WorldRadio/audio.ts),
 * so "PuffPAI, play the BBC" can only make sound if some browser actually calls
 * play(). The kiosk browser IS the appliance's speakers, so this component runs
 * there and nowhere else: it polls the backend for radio commands issued by the
 * LLM, drives the singleton audio manager, and reports back what the speakers
 * are really doing.
 *
 * Two directions, both over the same-origin /api/pai proxy (works on LAN, no
 * key in the bundle):
 *
 *   backend → appliance   mediahub_poll_command  (also the liveness heartbeat)
 *                         mediahub_ack_command   (what actually happened)
 *   appliance → backend   mediahub_report_radio  (status + station, on change)
 *
 * Appliance-ness (see isAppliance below — kiosk class AND a loopback origin) is
 * re-checked every tick rather than captured at mount: Settings toggles
 * html.puff-kiosk live, and this component is mounted in the dashboard layout
 * that never remounts.
 *
 * Nothing here throws and nothing here renders. Every failure is a silent
 * retry on the next tick — the backend restarts often enough that noisy error
 * handling would be worse than useless.
 */

import { useEffect } from "react";
import { getState, play, stop, subscribe, type Station } from "./WorldRadio/audio";

const POLL_MS = 4000;
/** Let the element get past "connecting" before we claim a status. */
const ACK_SETTLE_MS = 1500;
/** Re-report while a station is set so the backend can age out stale state. */
const HEARTBEAT_MS = 20000;
/** Coalesce the connecting→playing burst into one report. */
const REPORT_DEBOUNCE_MS = 300;

interface RadioCommand {
  id: string;
  action: string;
  station: Station | null;
  issued_at?: string;
}

/**
 * Is THIS browser the appliance itself?
 *
 * The puff-kiosk class alone is not an identity: it comes from
 * localStorage.puffpai_display_mode, which any phone can set via
 * ?puffMode=kiosk or the Settings toggle. A phone in kiosk display mode
 * would then execute radio commands meant for the appliance AND ack them,
 * clearing the one-slot command so the real appliance never sees it.
 *
 * The appliance's own kiosk browser loads the dashboard over loopback
 * (puffpai-kiosk.sh opens http://localhost:3003/dashboard?puffMode=kiosk);
 * every other viewer on the LAN reaches it by IP or hostname. Loopback is
 * therefore an origin-level fact no remote browser can spoof — the same
 * distinction JellyfinFrame already relies on to pick its iframe origin.
 */
function isAppliance(): boolean {
  if (typeof document === "undefined") return false;
  if (!document.documentElement.classList.contains("puff-kiosk")) return false;
  const host = window.location.hostname;
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

async function callAction(
  action: string,
  params: Record<string, unknown>,
): Promise<unknown> {
  const res = await fetch("/api/pai/apps/mediahub/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, params }),
    cache: "no-store",
    signal: AbortSignal.timeout(8000),
  });
  if (!res.ok) throw new Error(`mediahub ${action} ${res.status}`);
  const data = (await res.json()) as { success?: boolean; result?: unknown };
  if (data?.success === false) throw new Error(`mediahub ${action} refused`);
  return data?.result;
}

/** Only status/station matter to the backend — volume changes are not news. */
function signature(): string {
  const s = getState();
  return `${s.status}|${s.station?.stationuuid ?? ""}`;
}

/**
 * The poll result is the command itself per the contract, but tolerate an
 * envelope ({radio_command: ...}) so a backend shape tweak degrades to "no
 * command" instead of throwing.
 */
function readCommand(result: unknown): RadioCommand | null {
  if (!result || typeof result !== "object") return null;
  const obj = result as Record<string, unknown>;
  const cmd =
    obj.radio_command && typeof obj.radio_command === "object"
      ? (obj.radio_command as Record<string, unknown>)
      : obj;
  if (typeof cmd.id !== "string" || typeof cmd.action !== "string") return null;
  return cmd as unknown as RadioCommand;
}

export default function KioskMediaBridge() {
  useEffect(() => {
    let alive = true;
    let lastCommandId: string | null = null;
    let lastSignature: string | null = null;
    let lastReportAt = 0;
    let debounce: ReturnType<typeof setTimeout> | null = null;
    const pending = new Set<ReturnType<typeof setTimeout>>();

    const later = (fn: () => void, ms: number) => {
      const t = setTimeout(() => {
        pending.delete(t);
        if (alive) fn();
      }, ms);
      pending.add(t);
    };

    const report = () => {
      if (!alive || !isAppliance()) return;
      const s = getState();
      lastSignature = signature();
      lastReportAt = Date.now();
      void callAction("mediahub_report_radio", {
        status: s.status,
        station: s.station,
      }).catch(() => {
        // Next change or the 20s heartbeat re-sends it.
      });
    };

    const tick = () => {
      if (!alive || !isAppliance()) return;

      // The poll doubles as our heartbeat, but a station that has been sitting
      // in the same state still needs a fresh timestamp on the backend side.
      if (getState().station && Date.now() - lastReportAt >= HEARTBEAT_MS) {
        report();
      }

      void callAction("mediahub_poll_command", {})
        .then((result) => {
          if (!alive) return;
          const cmd = readCommand(result);
          if (!cmd || cmd.id === lastCommandId) return;
          // Claim the id before acting: a slow ack must not let the next tick
          // execute the same command twice.
          lastCommandId = cmd.id;

          if (cmd.action === "play" && cmd.station?.url_resolved) {
            play(cmd.station);
          } else if (cmd.action === "stop") {
            stop();
          }
          // An unknown action (or a play with no usable stream) still gets
          // acked below — with the manager's real, unchanged status, which is
          // the honest answer to "did you do it".

          later(() => {
            const s = getState();
            void callAction("mediahub_ack_command", {
              id: cmd.id,
              status: s.status,
              station: s.station,
            }).catch(() => {});
          }, ACK_SETTLE_MS);
        })
        .catch(() => {
          // Backend restarting / offline — try again in POLL_MS.
        });
    };

    const unsubscribe = subscribe(() => {
      if (!alive || !isAppliance()) return;
      if (signature() === lastSignature) return;
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => {
        debounce = null;
        report();
      }, REPORT_DEBOUNCE_MS);
    });

    // Unconditional: a freshly reloaded kiosk starts idle and must SAY so —
    // reporting only when a station is set would leave a pre-reload "playing"
    // record standing while our live poll heartbeat vouches for it.
    report();
    tick();
    const iv = setInterval(tick, POLL_MS);

    return () => {
      alive = false;
      clearInterval(iv);
      if (debounce) clearTimeout(debounce);
      pending.forEach((t) => clearTimeout(t));
      pending.clear();
      unsubscribe();
    };
  }, []);

  return null;
}
