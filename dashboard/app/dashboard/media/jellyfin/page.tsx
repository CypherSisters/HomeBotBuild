"use client";

/**
 * /dashboard/media/jellyfin — the full Jellyfin web UI.
 *
 * Two jobs:
 * 1. Full media browsing/playback without leaving the dashboard.
 * 2. While this page is open, the embedded player registers as an ACTIVE
 *    Jellyfin session — giving voice commands ("play some music",
 *    "play the playlist X") a device to cast to. Without an open player,
 *    Jellyfin remote-play has no target.
 *
 * Jellyfin 10.11 sends no X-Frame-Options/CSP, so embedding works — BUT
 * 2026 Chrome/Edge "Local Network Access" enforcement aborts iframe
 * navigations to .local/private-IP origins. So the kiosk frames the loopback
 * media-proxy (instrumentation.ts, localhost:8096) instead: loopback targets
 * are exempt, and autoplay allowances apply reliably. LAN-only — works in
 * Lockdown.
 *
 * NOTE: on a normal dashboard view the actual iframe does not live here. It is
 * a persistent frame mounted in the dashboard layout —
 * app/dashboard/_components/JellyfinFrame.tsx — so playback continues across
 * navigation and the player stays registered as an active session on every
 * page (voice remote-play needs a live cast target). This page only reserves
 * the space; JellyfinFrame overlays it (fixed position).
 *
 * EMBEDDED (the "show me X" overlay) IS THE EXCEPTION.
 * ---------------------------------------------------
 * ShowViewer frames this route full-screen, and the dashboard layout's embed
 * guard renders page content ONLY — no rail, no topbar, and deliberately no
 * JellyfinFrame (a nested persistent frame would recurse). So inside the
 * overlay the spacer above was the whole page: "show me Casablanca"
 * resolved a real library item on the backend, queued
 * /dashboard/media/jellyfin?item=<id>, and the viewer got an empty screen.
 *
 * Framed, therefore, this page renders its OWN iframe, deep-linked to the
 * queued item. It is short-lived and single-purpose (the overlay closes and it
 * goes away), so none of the persistent-frame reasoning applies to it.
 *
 * puff puff pass
 */
import { useEffect, useState } from "react";
import SectionMain from "@/app/_components/Section/Main";

/** Same origin rule as JellyfinFrame.resolveJellyfinUrl — see the comment
 *  there for the loopback / LAN split. Client-only (window). */
function resolveJellyfinUrl(): string {
  return (
    process.env.NEXT_PUBLIC_JELLYFIN_URL ??
    `http://${window.location.hostname}:8096`
  );
}

/** A Jellyfin item id is a 32-char hex GUID. Anything else is dropped rather
 *  than concatenated into a URL handed to an iframe. */
const JELLYFIN_ID = /^[0-9a-f]{32}$/i;

export default function MediaJellyfinPage() {
  /* null until mount: SSR and the first client render must agree, and both
     window.top and the query string are client-only. */
  const [embedSrc, setEmbedSrc] = useState<string | null>(null);

  useEffect(() => {
    let framed = false;
    try {
      framed = window.self !== window.top;
    } catch {
      // Cross-origin parent throws on access → we are definitely framed.
      framed = true;
    }
    if (!framed) return;

    const base = resolveJellyfinUrl();
    let item: string | null = null;
    try {
      const raw = new URLSearchParams(window.location.search).get("item");
      if (raw && JELLYFIN_ID.test(raw)) item = raw.toLowerCase();
    } catch {
      /* no query */
    }
    // Jellyfin 10.11's web client routes on the hash.
    setEmbedSrc(item ? `${base}#/details?id=${item}` : base);
  }, []);

  if (embedSrc) {
    return (
      <iframe
        src={embedSrc}
        title="Jellyfin — PuffPAI media server"
        className="h-dvh w-full border-0 bg-black"
        allow="autoplay; encrypted-media"
      />
    );
  }

  return (
    <SectionMain>
      {/* Spacer that preserves page scroll geometry. The persistent
          JellyfinFrame overlay sits on top (fixed position) and owns ALL
          loading / error / spinner states, so this stays an empty, silent
          fallback — no text that could contradict the frame's own status. */}
      <div aria-hidden className="h-[calc(100vh-7rem)] min-h-[480px] w-full" />
    </SectionMain>
  );
}
