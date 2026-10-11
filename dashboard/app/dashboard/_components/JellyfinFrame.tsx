"use client";

/**
 * JellyfinFrame — a PERSISTENT, single-instance embed of the Jellyfin web UI.
 *
 * Mounted once in the dashboard layout (not in the /dashboard/media/jellyfin
 * route), so the iframe keeps a stable identity across route changes. This
 * buys us two things that an inline-in-the-page iframe cannot:
 *
 *   (a) Playback continues across dashboard navigation. Navigating away from
 *       /dashboard/media/jellyfin no longer unmounts the iframe, so music
 *       keeps playing.
 *       Off that route we only HIDE it (visibility:hidden + no pointer
 *       events) — visibility:hidden keeps the media element alive and audible,
 *       whereas conditional rendering / unmounting would kill audio.
 *
 *   (b) The embedded player stays registered as an ACTIVE Jellyfin session on
 *       every page. Voice remote-play commands ("play some music") need a live
 *       cast target; a persistent frame guarantees one exists everywhere.
 *
 * The iframe attributes (loopback media-proxy src, autoplay allowances, no
 * sandbox) are carried over verbatim from the old inline media page — see
 * app/dashboard/media/jellyfin/page.tsx for the LNA / loopback-proxy
 * rationale.
 *
 * OVERLAY STATE MACHINE (Jellyfin route only — the iframe itself stays mounted
 * app-wide for music continuity):
 *
 *   loading ──(status ok AND iframe onLoad)──▶ ok        (frame shown, no overlay)
 *   loading ──(status not ok)────────────────▶ error     (error card)
 *   loading ──(no onLoad within 12s)─────────▶ error     (error card)
 *   error   ──(Retry: re-check + bump key)───▶ loading
 *
 * DEEP LINK: /dashboard/media/jellyfin?item=<32-hex jellyfin id> opens that
 * item instead of the library root (see the JELLYFIN_ID block below). No item
 * = today's behaviour, unchanged. ?view=livetv / ?view=home (the Media hub's
 * Live TV and Jellyfin tiles) open those Jellyfin pages on every tap.
 *
 * The loopback proxy silently destroys sockets when the Pi is unreachable, so
 * the iframe's onLoad may NEVER fire on connection-reset and we cannot read its
 * cross-origin content. We therefore drive the machine from the /api/media/status
 * pre-flight AND a 12s onLoad timeout, re-checking status every 15s while unhealthy.
 */
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import MsIcon from "../../_components/MsIcon";
import TvCastPanel from "./TvCastPanel";
import NativeTvButton from "./NativeTvButton";

/**
 * Jellyfin origin for THIS viewer. Jellyfin runs on the appliance itself
 * (docker, published on the LAN IP), so the right origin depends on where the
 * browser is:
 *
 *   - on the appliance (kiosk, dashboard origin localhost:3003) →
 *     http://localhost:8096, the instrumentation.ts loopback proxy — loopback
 *     is exempt from browsers' Local Network Access policy and inherits the
 *     kiosk's autoplay allowance;
 *   - any other browser on the LAN (dashboard origin <appliance-ip>:3003) →
 *     the same host the dashboard was loaded from, port 8096, hitting the
 *     docker-published port directly (private→private framing is not
 *     LNA-gated, and "localhost" would wrongly point at the viewer's own
 *     machine).
 *
 * window.location.hostname picks the right one for both without config.
 * NEXT_PUBLIC_JELLYFIN_URL still overrides everything when set at build time.
 * Client-only (window): call from an effect, never during render.
 */
function resolveJellyfinUrl(): string {
  return (
    process.env.NEXT_PUBLIC_JELLYFIN_URL ??
    `http://${window.location.hostname}:8096`
  );
}

/* Control-bar buttons: big, labelled, thumb-sized (52px) for a wall screen
   at arm's length. Colours come from the theme variables so the bar wears
   the active profile's palette like the rest of the dashboard. */
const BAR_BTN =
  "flex flex-none items-center gap-2 rounded-full border px-5 shadow-lg transition-opacity hover:opacity-90 active:opacity-75";
const BAR_BTN_STYLE = {
  height: 52,
  borderColor: "var(--line)",
  background: "rgba(0,0,0,0.55)",
  color: "#f1f5f9",
  font: "700 16px var(--font-nunito), sans-serif",
} as const;

const LOAD_TIMEOUT_MS = 12_000;
const RECHECK_INTERVAL_MS = 15_000;

/**
 * Deep link: /dashboard/media/jellyfin?item=<jellyfin id>.
 *
 * The backend's "show me <film>" path resolves a real library item and queues
 * that route (see media_library.show_movie / dashboard_display.set_display,
 * which only accepts a SAME-ORIGIN path — so the Jellyfin origin itself can
 * never be the payload and the id has to ride on our own route). Without this
 * the overlay opened the library root and the viewer had to find the film
 * again by hand.
 *
 * Jellyfin 10.11's web client routes on the hash, `#/details?id=<id>`, so the
 * deep link is a hash on the same frame src: the iframe navigates within its
 * own document instead of reloading, which is what keeps audio alive.
 *
 * Ids are validated, not trusted. A Jellyfin item id is a 32-char hex GUID;
 * anything else is dropped rather than concatenated into a URL we then hand to
 * an iframe.
 */
const JELLYFIN_ID = /^[0-9a-f]{32}$/i;

/** The `item` query param of the CURRENT location, or null. Read from window
 *  rather than useSearchParams so this component (mounted in the dashboard
 *  layout, i.e. on every route) does not drag the whole app into a Suspense
 *  boundary at build time. */
function readItemParam(): string | null {
  try {
    const raw = new URLSearchParams(window.location.search).get("item");
    return raw && JELLYFIN_ID.test(raw) ? raw.toLowerCase() : null;
  } catch {
    return null;
  }
}

/**
 * Section links from the Media hub tiles: /dashboard/media/jellyfin?view=livetv
 * opens Jellyfin's Live TV page, ?view=home its home page. Allowlisted: only
 * these two names map to a hash, anything else is ignored. A route with no
 * ?view= (sidebar, back button) leaves the frame wherever the viewer left it.
 */
function readViewHash(): string | null {
  try {
    const raw = new URLSearchParams(window.location.search).get("view");
    return raw === "livetv" || raw === "home" ? `#/${raw}` : null;
  } catch {
    return null;
  }
}

/** Reports the current query string so a ?item= / ?view= swap while already
 *  on the Jellyfin page re-runs the deep-link effect (a pathname alone does
 *  not change). Isolated in its own Suspense boundary so useSearchParams
 *  does not push the whole layout into one at build time. Renders nothing. */
function SearchWatcher({ onChange }: { onChange: (q: string) => void }) {
  const params = useSearchParams();
  const q = params?.toString() ?? "";
  useEffect(() => onChange(q), [q, onChange]);
  return null;
}

export default function JellyfinFrame() {
  const pathname = usePathname();
  const router = useRouter();
  // /dashboard/media is now the media HUB (launchers + World Radio); only the
  // deeper Jellyfin route gets the overlay.
  const onJellyfin = pathname?.startsWith("/dashboard/media/jellyfin");

  const containerRef = useRef<HTMLDivElement>(null);
  const [search, setSearch] = useState("");

  // Resolved on mount (needs window); null only for the first client tick and
  // during SSR, so server and first client render agree (no iframe yet).
  const [jellyfinUrl, setJellyfinUrl] = useState<string | null>(null);
  useEffect(() => {
    setJellyfinUrl(resolveJellyfinUrl());
  }, []);

  /* The src actually on the iframe. Starts as the library root and is only
     ever PUSHED forward to a deep link — never rewritten back. Rewriting it on
     leaving the route would navigate the persistent frame back to the root and
     kill the playing audio, which is the one thing this component exists to
     protect. */
  const [frameSrc, setFrameSrc] = useState<string | null>(null);
  useEffect(() => {
    if (jellyfinUrl) setFrameSrc((cur) => cur ?? jellyfinUrl);
  }, [jellyfinUrl]);

  const iframeRef = useRef<HTMLIFrameElement>(null);
  const lastItemRef = useRef<string | null>(null);
  // The location this effect last acted on; one landing fires the effect for
  // both the pathname and the search change, and must navigate only once.
  const lastLocationRef = useRef<string | null>(null);
  useEffect(() => {
    /* Re-runs on every route change AND on a query-only swap (?item= /
       ?view= while already on this page, via SearchWatcher). */
    if (!pathname?.startsWith("/dashboard/media/jellyfin")) {
      lastLocationRef.current = null;
      return;
    }
    if (!jellyfinUrl) return;
    const location = `${pathname}${window.location.search}`;
    if (location === lastLocationRef.current) return;
    lastLocationRef.current = location;
    const item = readItemParam();
    if (item) {
      if (item === lastItemRef.current) return;
      lastItemRef.current = item;
      setFrameSrc(`${jellyfinUrl}#/details?id=${item}`);
      return;
    }
    const view = readViewHash();
    if (!view) return;
    /* A tile tap lands on its section EVERY time, even if the viewer wandered
       off inside Jellyfin since the last tap. Jellyfin's root redirects to
       /web/, so basing the hash there makes this a same-document hash change
       (audio keeps playing). React skips an unchanged src, so when the target
       equals the current attribute, set it by hand to navigate again. */
    lastItemRef.current = null;
    const url = `${jellyfinUrl}/web/${view}`;
    const frame = iframeRef.current;
    if (frame && frame.getAttribute("src") === url) frame.src = url;
    else setFrameSrc(url);
  }, [pathname, search, jellyfinUrl]);

  // null = checking, true = reachable, false = unreachable
  const [statusOk, setStatusOk] = useState<boolean | null>(null);
  const [iframeLoaded, setIframeLoaded] = useState(false);
  const [loadTimedOut, setLoadTimedOut] = useState(false);
  // Bumping this remounts the iframe (a fresh navigation attempt) on Retry.
  const [frameKey, setFrameKey] = useState(0);

  // Ref mirror of statusOk so the recheck interval can read the latest value
  // without re-subscribing every render.
  const statusOkRef = useRef<boolean | null>(null);
  statusOkRef.current = statusOk;

  const checkStatus = useCallback(async () => {
    try {
      const res = await fetch("/api/media/status", { cache: "no-store" });
      const data = await res.json();
      setStatusOk(!!data?.ok);
    } catch {
      setStatusOk(false);
    }
  }, []);

  // Status pre-flight: check on mount / route-enter / retry, then poll every
  // 15s WHILE unhealthy. Only runs on the Jellyfin route.
  useEffect(() => {
    if (!onJellyfin) return;
    void checkStatus();
    const id = setInterval(() => {
      if (statusOkRef.current !== true) void checkStatus();
    }, RECHECK_INTERVAL_MS);
    return () => clearInterval(id);
  }, [onJellyfin, frameKey, checkStatus]);

  // onLoad watchdog: if the iframe hasn't fired onLoad within 12s of entering
  // the Jellyfin route (or of a retry), flag a timeout so the error card shows.
  useEffect(() => {
    if (!onJellyfin) return;
    setLoadTimedOut(false);
    const id = setTimeout(() => setLoadTimedOut(true), LOAD_TIMEOUT_MS);
    return () => clearTimeout(id);
  }, [onJellyfin, frameKey]);

  // A fresh iframe (new key) hasn't loaded yet.
  useEffect(() => {
    setIframeLoaded(false);
  }, [frameKey]);

  /* Focus guard. The Jellyfin app calls focus() on itself while it boots, and
     because that happens INSIDE the child document neither visibility:hidden
     nor inert prevents it — the parent's activeElement simply becomes this
     iframe. On every dashboard page that yanked the caret out of whatever the
     user was typing in (the chat composer being the obvious victim: you typed,
     the frame finished booting, and your next keystroke went nowhere).

     So: remember the last thing focused outside the frame, and hand focus back
     whenever the hidden frame takes it. Polls briefly after each (re)load —
     the child's own focus() fires no event we can hear in this document — then
     relies on the cheap focusin listener alone. */
  useEffect(() => {
    if (onJellyfin) return;
    const container = containerRef.current;
    if (!container) return;

    let lastOutside: HTMLElement | null = null;
    const remember = (e: Event) => {
      const t = e.target as HTMLElement | null;
      if (t && !container.contains(t)) lastOutside = t;
    };
    const reclaim = () => {
      const active = document.activeElement as HTMLElement | null;
      if (!active || !container.contains(active)) return;
      active.blur();
      if (lastOutside?.isConnected) lastOutside.focus();
    };

    document.addEventListener("focusin", remember, true);
    document.addEventListener("focusin", reclaim, true);
    // The boot-time grab lands within a few seconds of the frame (re)loading.
    const poll = setInterval(reclaim, 250);
    const stop = setTimeout(() => clearInterval(poll), 20_000);

    return () => {
      document.removeEventListener("focusin", remember, true);
      document.removeEventListener("focusin", reclaim, true);
      clearInterval(poll);
      clearTimeout(stop);
    };
  }, [onJellyfin, frameKey]);

  /* Control bar actions. The frame is cross-origin (port 8096), so we cannot
     reach into Jellyfin's player; the one thing we CAN do every time is give
     the iframe a fresh document. Remounting it (new key) unloads the old page,
     which ends any playback inside it, then lands on the target screen. Costs
     a few seconds of reload; in exchange it never depends on what state the
     Jellyfin player was left in. */
  const reloadFrameAt = useCallback(
    (hash: "#/livetv" | "#/home") => {
      if (!jellyfinUrl) return;
      lastItemRef.current = null;
      setIframeLoaded(false);
      setLoadTimedOut(false);
      setFrameSrc(`${jellyfinUrl}/web/${hash}`);
      setFrameKey((k) => k + 1);
    },
    [jellyfinUrl],
  );
  // Channels: always the Live TV guide, even from inside a playing channel.
  const handleChannels = useCallback(() => reloadFrameAt("#/livetv"), [reloadFrameAt]);
  // Stop: end whatever is playing in the frame and park it on Jellyfin home.
  const handleStop = useCallback(() => reloadFrameAt("#/home"), [reloadFrameAt]);

  // "Cast to...": a sheet over the frame (see TvCastPanel). Closed whenever
  // the viewer leaves the Jellyfin route.
  const [castOpen, setCastOpen] = useState(false);
  useEffect(() => {
    if (!onJellyfin) setCastOpen(false);
  }, [onJellyfin]);

  /* Pin the frame to the dashboard's real content box (the scroll area under
     the topbar, right of the rail; marked data-puff-content in the layout).
     It used to be hardcoded top-14 / xl:left-60, but the topbar is
     clamp(72px,9vh,96px) tall and the rail clamp(212px,15vw,264px) wide, and
     the rail is a static column from lg up. On the 2560x1440 kiosk that put
     the z-20 frame over the bottom 40px of the topbar and 24px of the rail,
     and between lg and xl over the whole rail. Measuring follows the layout
     instead of guessing it. Event-driven only (ResizeObserver + resize), no
     polling or animation. */
  const [box, setBox] = useState<{
    top: number;
    left: number;
    right: number;
    bottom: number;
  } | null>(null);
  useEffect(() => {
    const el = document.querySelector<HTMLElement>("[data-puff-content]");
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      const doc = document.documentElement;
      const next = {
        top: Math.round(r.top),
        left: Math.round(r.left),
        right: Math.round(doc.clientWidth - r.right),
        bottom: Math.round(doc.clientHeight - r.bottom),
      };
      setBox((cur) =>
        cur &&
        cur.top === next.top &&
        cur.left === next.left &&
        cur.right === next.right &&
        cur.bottom === next.bottom
          ? cur
          : next,
      );
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  const handleRetry = useCallback(() => {
    setIframeLoaded(false);
    setLoadTimedOut(false);
    setStatusOk(null);
    setFrameKey((k) => k + 1);
  }, []);

  const handleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      void document.exitFullscreen();
    } else {
      void containerRef.current?.requestFullscreen();
    }
  }, []);

  /* Fullscreen belongs to the APPLIANCE, never to the embedded app.
     A wall kiosk has no tab strip, no window chrome and usually no keyboard,
     so whoever owns the fullscreen element owns the only way out. When Jellyfin
     took it, its <video> painted over every PuffPAI control and the exit lived
     inside Jellyfin's own player UI — tap the wrong spot and the screen was a
     dead end. The iframe no longer gets `allow="fullscreen"`, and if anything
     still manages to grab it we take it back onto our own container, which
     keeps the Exit control on top. */
  const [isFullscreen, setIsFullscreen] = useState(false);
  useEffect(() => {
    const onChange = () => {
      const el = document.fullscreenElement;
      const container = containerRef.current;
      if (el && container && el !== container) {
        // Something inside (the player) claimed the screen — reclaim it so an
        // exit control is always reachable.
        void document.exitFullscreen().catch(() => {});
        setIsFullscreen(false);
        return;
      }
      setIsFullscreen(!!el && el === container);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  /* Never leave the frame fullscreen while it is hidden. If the page changes
     for ANY reason (Back, rail, voice, show-me) while our container owns
     fullscreen, the container goes invisible + inert and the bar unmounts:
     a black backdrop with no control, a dead end on a keyboardless kiosk. */
  const exitOurFullscreen = useCallback(async () => {
    const el = document.fullscreenElement;
    if (el && el === containerRef.current) {
      await document.exitFullscreen().catch(() => {});
    }
  }, []);
  useEffect(() => {
    if (!onJellyfin) void exitOurFullscreen();
  }, [onJellyfin, exitOurFullscreen]);

  // Back: leave fullscreen first, then navigate.
  const handleBack = useCallback(async () => {
    await exitOurFullscreen();
    router.push("/dashboard/media");
  }, [exitOurFullscreen, router]);

  const showError =
    onJellyfin && (statusOk === false || (loadTimedOut && !iframeLoaded));
  const showSpinner = onJellyfin && !showError && !iframeLoaded;

  // Fixed to the measured content area (see the box effect): below the
  // topbar, right of the rail, so neither is ever under the frame. Until the
  // first measurement lands, the old class geometry is the fallback. z-20 sits
  // above normal page content (z-10) and below the mobile rail drawer (z-50),
  // DeviceDiscoveryToast and modals (z-50). Off the Jellyfin route it stays
  // mounted (audio keeps playing) but invisible and non-interactive.
  return (
    <div
      ref={containerRef}
      /* inert while hidden. visibility:hidden alone does NOT stop the Jellyfin
         app inside the frame from calling focus() as it boots — that made the
         iframe document.activeElement on EVERY dashboard page, so the caret
         jumped out of the chat composer (and any other field) a second after
         the page settled. inert removes the whole subtree from the focus and
         hit-test order; the media element keeps playing regardless. */
      inert={!onJellyfin}
      className={`fixed z-20 ${box ? "" : "top-14 bottom-0 left-0 right-0 xl:left-60"} ${
        onJellyfin ? "p-2" : "p-6 invisible pointer-events-none"
      } ${isFullscreen ? "bg-black" : ""}`}
      style={box ?? undefined}
    >
      <Suspense fallback={null}>
        <SearchWatcher onChange={setSearch} />
      </Suspense>
      <div className="flex h-full w-full flex-col gap-2">
        {/* CONTROL BAR: Jellyfin route only, always visible, never on top of
            the iframe. It is its own row ABOVE the frame, so it covers none of
            Jellyfin's controls: the player's back arrow (top-left), its
            header/settings/cast buttons (top-right) and its bottom OSD all
            stay fully inside the iframe and fully tappable. Replaces the old
            floating "PuffPAI" pill (which sat on Jellyfin's back arrow) and
            the floating fullscreen square (which sat on its header buttons).
            No auto-hide: taps inside the cross-origin frame cannot be seen
            from here, so a hidden bar could not be brought back reliably.
            Static: hover/active colour only, nothing animates at rest. */}
        {onJellyfin && (
          <div
            role="toolbar"
            aria-label="TV and media controls"
            className="flex flex-none items-center gap-2"
          >
            <button
              type="button"
              onClick={handleBack}
              aria-label="Back to the Media page"
              className={BAR_BTN}
              style={{
                ...BAR_BTN_STYLE,
                color: "#fff",
                background: "linear-gradient(140deg, var(--pink), var(--magenta))",
              }}
            >
              <MsIcon name="arrow_back" size={24} />
              Back
            </button>
            <button
              type="button"
              onClick={handleChannels}
              aria-label="Channels: open the Live TV guide"
              className={BAR_BTN}
              style={BAR_BTN_STYLE}
            >
              <MsIcon name="live_tv" size={24} />
              Channels
            </button>
            <button
              type="button"
              onClick={handleStop}
              aria-label="Stop what is playing"
              className={BAR_BTN}
              style={BAR_BTN_STYLE}
            >
              <MsIcon name="stop" size={24} fill />
              Stop
            </button>
            <button
              type="button"
              onClick={() => setCastOpen(true)}
              aria-label="Cast to a TV in the house"
              className={BAR_BTN}
              style={BAR_BTN_STYLE}
            >
              <MsIcon name="cast" size={24} />
              Cast to...
            </button>
            <NativeTvButton onBeforePlay={handleChannels} />
            <div className="min-w-0 flex-1" />
            {/* Fullscreen lives in the bar too, so the way out of fullscreen
                is the same big labelled button in the same place. */}
            <button
              type="button"
              onClick={handleFullscreen}
              aria-label={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
              className={BAR_BTN}
              style={BAR_BTN_STYLE}
            >
              <MsIcon name={isFullscreen ? "close_fullscreen" : "fullscreen"} size={24} />
              {isFullscreen ? "Exit fullscreen" : "Fullscreen"}
            </button>
          </div>
        )}

        <div className="relative min-h-0 w-full flex-1">
        {frameSrc && (
          <iframe
            key={frameKey}
            ref={iframeRef}
            src={frameSrc}
            title="Jellyfin — PuffPAI media server"
            className="h-full w-full rounded-xl border border-[#ff2d95]/25 bg-black"
            /* No `fullscreen` here on purpose — see the fullscreen effect
               above. Jellyfin's player still fills this frame, which already
               covers the whole content area. */
            allow="autoplay; encrypted-media"
            onLoad={() => setIframeLoaded(true)}
          />
        )}

        {onJellyfin && castOpen && <TvCastPanel onClose={() => setCastOpen(false)} />}

        {/* Loading skeleton — Jellyfin route only, before onLoad / error. */}
        {showSpinner && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 rounded-xl border border-[#ff2d95]/25 bg-black">
            <div className="h-10 w-10 animate-spin rounded-full border-2 border-[#ff2d95]/30 border-t-[#ff2d95]" />
            <p className="text-sm text-slate-400">Connecting to media server…</p>
          </div>
        )}

        {/* Error card — Jellyfin route only. */}
        {showError && (
          <div className="absolute inset-0 flex items-center justify-center rounded-xl border border-[#ff2d95]/25 bg-black p-6">
            <div className="max-w-md text-center">
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full border border-[#ff2d95]/40 text-[#ff2d95]">
                <svg
                  viewBox="0 0 24 24"
                  className="h-6 w-6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M12 9v4M12 17h.01M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                </svg>
              </div>
              <h2 className="mb-2 text-lg font-semibold text-slate-100">
                Media server unreachable
              </h2>
              <p className="mb-6 text-sm text-slate-400">
                The media server may be down, or the media drive may have
                unmounted. Playback can&apos;t load until it&apos;s back.
              </p>
              <div className="flex items-center justify-center gap-3">
                <button
                  type="button"
                  onClick={handleRetry}
                  className="rounded-lg border border-[#ff2d95]/50 bg-[#ff2d95]/10 px-4 py-2 text-sm font-medium text-slate-100 transition-colors hover:border-[#ff2d95] hover:bg-[#ff2d95]/20"
                >
                  Retry
                </button>
                <a
                  href={jellyfinUrl ?? "#"}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="rounded-lg border border-slate-600 px-4 py-2 text-sm font-medium text-slate-300 transition-colors hover:border-slate-400 hover:text-white"
                >
                  Open Jellyfin directly
                </a>
              </div>
            </div>
          </div>
        )}
        </div>
      </div>
    </div>
  );
}
