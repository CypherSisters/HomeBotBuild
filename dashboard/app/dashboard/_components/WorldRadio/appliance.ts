"use client";

/**
 * The appliance's own radio — the SERVER-SIDE player, not this browser's.
 *
 * Voice ("PuffPAI, play some jazz radio") starts a player that lives on the
 * box and outputs straight to Pulse. No browser is involved, so every transport
 * control the dashboard had — all of which drive the local <audio> element in
 * ./audio.ts — would leave that stream running, unpausable from the
 * dashboard or the media page.
 *
 * This module is the one place the UI can see and silence that player:
 *
 *   useApplianceRadio()   polls the same /api/media/now-playing the Now Playing
 *                         tile reads, and exposes only the appliance slot.
 *   stopApplianceRadio()  posts the backend's stop envelope,
 *                         which kills the server-side player AND queues the
 *                         kiosk browser's stop.
 *
 * Everything goes through the same-origin /api/pai proxy (the proxy holds the
 * API key; no key in the bundle), the same route KioskMediaBridge posts to.
 *
 * puff puff pass
 */

import { useCallback, useEffect, useRef, useState } from "react";

/** Matches the tile's own cadence; the appliance row is never fast-moving. */
const POLL_MS = 5000;

export interface ApplianceRadio {
  status: string;
  station: string | null;
  /** Sound is (or is about to be) coming out of the appliance right now. */
  active: boolean;
}

const IDLE: ApplianceRadio = { status: "idle", station: null, active: false };

/**
 * Ask the backend to stop everything radio: the server-side player first, the
 * kiosk browser player via the queued command. `pause` is a synonym backend
 * side — a live stream has no pause — so "stop" is the honest verb to send.
 *
 * Resolves false on any failure; the caller's optimistic UI is corrected by the
 * next poll either way.
 */
export async function stopApplianceRadio(): Promise<boolean> {
  try {
    const res = await fetch("/api/pai/apps/mediahub/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "mediahub_radio_request",
        params: { action: "stop" },
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (!res.ok) return false;
    const data = (await res.json().catch(() => null)) as {
      success?: boolean;
    } | null;
    return data?.success !== false;
  } catch {
    return false;
  }
}

function readAppliance(payload: unknown): ApplianceRadio {
  const sources = (payload as { sources?: { radio?: unknown } } | null)?.sources;
  const radio = sources?.radio as
    | {
        status?: string;
        station?: { name?: string } | null;
        stale?: boolean;
        source?: string;
      }
    | null
    | undefined;
  if (!radio) return IDLE;
  // A backend that predates the two-slot split sends no `source`, and
  // everything it reported in this slot was the appliance.
  if ((radio.source ?? "appliance") !== "appliance") return IDLE;
  // Stale means "playing" is no longer a fact — do not offer a stop for it.
  if (radio.stale) return IDLE;
  const status = radio.status ?? "idle";
  const name = radio.station?.name ?? null;
  const active =
    Boolean(name) &&
    (status === "playing" || status === "connecting" || status === "paused");
  return { status, station: name, active };
}

/**
 * Live view of the appliance's radio slot, plus the stop that silences it.
 *
 * `stop()` clears the row optimistically — a tap that leaves "playing" on the
 * screen for five seconds reads as a dead button — and the next poll confirms
 * or restores the truth.
 */
export function useApplianceRadio(): ApplianceRadio & {
  stop: () => Promise<void>;
  busy: boolean;
} {
  const [state, setState] = useState<ApplianceRadio>(IDLE);
  const [busy, setBusy] = useState(false);
  /** Suppress polls that were already in flight when we optimistically idled. */
  const mutedUntil = useRef(0);

  useEffect(() => {
    let alive = true;
    const load = () => {
      // The /api/pai proxy strips its prefix verbatim (/api/pai/X → backend /X)
      // and the backend route really is /api/media/... — hence the double /api.
      fetch("/api/pai/api/media/now-playing", {
        cache: "no-store",
        signal: AbortSignal.timeout(10000),
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (!alive || d == null) return;
          if (Date.now() < mutedUntil.current) return;
          setState(readAppliance(d));
        })
        .catch(() => {
          // Backend restarting — keep the last known row.
        });
    };
    load();
    const iv = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, []);

  const stop = useCallback(async () => {
    setBusy(true);
    mutedUntil.current = Date.now() + 2500;
    setState(IDLE);
    try {
      await stopApplianceRadio();
    } finally {
      setBusy(false);
    }
  }, []);

  return { ...state, stop, busy };
}
