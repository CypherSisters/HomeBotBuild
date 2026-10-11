"use client";

/**
 * ShowViewer - the "pull it up on screen" overlay.
 *
 * Polls GET /dashboard/display, which voice sets ("show me the TED talk about
 * surveillance" -> the backend finds the talk and queues its viewer URL). When
 * something is queued this fills the screen with the dashboard's own knowledge
 * viewer (an iframe of current.url, deep-linked to the exact article/video).
 * The Close button clears it (DELETE /dashboard/display); saying "clear the
 * screen" clears it too, and the next poll picks up the empty state. Mounted in
 * the dashboard layout so it works on every kiosk page. seq only changes when
 * the backend state changes, so the poll is cheap and does not re-mount the
 * iframe on every tick.
 *
 * SECURE (vault) MODE - Piece E of the Zone-1 vault DOCUMENT display design.
 * When the backend queues an ephemeral vault view (kind === "vault" or
 * secure === true), the overlay renders a locked-down viewer:
 *   - the iframe src is the BACKEND origin directly (${API}${url}) so the kiosk
 *     browser fetches /vault/view/<token> from :8000 itself and the backend's
 *     loopback-only check is the real boundary (W1). A phone hitting :8000 is
 *     non-loopback -> opaque 404 -> the "home screen only" message.
 *   - the iframe is sandboxed (sandbox="", no allow=) with referrerPolicy
 *     "no-referrer": no scripts, no same-origin escalation, no popups, no
 *     top-nav, no autoplay/fullscreen. It can only paint a static PDF/image.
 *   - a client hard-timer is the PRIMARY auto-clear (blur/visibility are
 *     unreliable in fullscreen kiosk Chromium); blur/visibility/Escape/Close
 *     are supplementary. Any clear DESTROYS the view: DELETE the ephemeral
 *     token AND the display slot, unmount the iframe, keep no bytes in state.
 * Non-vault (knowledge/url) behavior is unchanged.
 *
 * DIRECTIONS. kind === "directions" frames the
 * dashboard's own directions page, same-origin like a knowledge item. The src
 * is rebuilt from meta.route_id by directionsFrameUrl() rather than taken from
 * the item, so the overlay can only ever land on /dashboard/directions/*. The
 * page hides its own back button when framed; this Close dismisses it like
 * every other kind.
 */

import { useEffect, useState, useCallback, useRef } from "react";
import MsIcon from "@/app/_components/MsIcon";
import { FREDOKA, NUNITO } from "@/app/_components/Widgets/DesignTile";
import { directionsFrameUrl } from "@/app/_lib/directions";

const API = "/api/pai";
const POLL_MS = 2500;
const DEFAULT_TTL_S = 60; // client hard-timer for a vault view
const STRICT_TTL_S = 45; // strict/health doc subsets clear faster
const LOAD_TIMEOUT_MS = 6000; // if the frame never loads, show the graceful notice

interface DisplayItem {
  kind: string;
  url: string;
  title: string;
  seq?: number;
  // vault-mode hints (backend ephemeral overlay item):
  secure?: boolean;
  ttl?: number;
  strict?: boolean;
  // directions hint: { route_id, mode }
  meta?: Record<string, unknown>;
}

/**
 * A vault view is any item the backend flags secure or classifies as vault.
 * A "directions" item never is, whatever flags it carries: it always frames
 * the dashboard's own directions page (directionsFrameUrl).
 */
function isSecure(item: DisplayItem | null): boolean {
  if (!item || item.kind === "directions") return false;
  return item.kind === "vault" || item.secure === true;
}

/** Extract the opaque view token from "/vault/view/<token>" (path only). */
function vaultToken(url: string): string | null {
  const m = url.match(/\/vault\/view\/([^/?#]+)/);
  return m ? m[1] : null;
}

/** Client hard-timer length for a vault item, honoring backend ttl/strict hints. */
function ttlSeconds(item: DisplayItem): number {
  if (typeof item.ttl === "number" && item.ttl > 0) return Math.floor(item.ttl);
  return item.strict ? STRICT_TTL_S : DEFAULT_TTL_S;
}

export default function ShowViewer() {
  const [item, setItem] = useState<DisplayItem | null>(null);
  const [remaining, setRemaining] = useState<number>(0);
  const [loadState, setLoadState] = useState<"loading" | "ok" | "failed">("ok");
  const lastSeqRef = useRef<number>(-1);
  // Latest item, readable from stable callbacks without re-subscribing.
  const itemRef = useRef<DisplayItem | null>(null);
  itemRef.current = item;

  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      fetch(`${API}/dashboard/display`, { signal: AbortSignal.timeout(8000) })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (cancelled || !d) return;
          const seq = typeof d.seq === "number" ? d.seq : 0;
          if (seq === lastSeqRef.current) return;
          lastSeqRef.current = seq;
          setItem(d.current ?? null);
        })
        .catch(() => {});
    };
    poll();
    const iv = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, []);

  // Clear = DESTROY. Unmount the frame, re-sync the poll, and for a vault view
  // also invalidate the ephemeral bytes on the backend. Single path for the
  // hard-timer, blur, visibility, Escape, and the Close button so every trigger
  // tears the plaintext down the same way. No document bytes ever live in React
  // state - only the live iframe src, which this drops.
  const dismiss = useCallback(() => {
    const cur = itemRef.current;
    setItem(null);
    lastSeqRef.current = -1; // re-sync on the next poll
    if (isSecure(cur) && cur) {
      const tok = vaultToken(cur.url);
      if (tok) {
        fetch(`${API}/vault/view/${encodeURIComponent(tok)}`, {
          method: "DELETE",
        }).catch(() => {});
      }
    }
    fetch(`${API}/dashboard/display`, { method: "DELETE" }).catch(() => {});
  }, []);

  // Escape closes it (if the kiosk has a keyboard attached).
  useEffect(() => {
    if (!item) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [item, dismiss]);

  // Supplementary clears for a vault view: a walk-away (window blur) or the tab
  // going background (visibilitychange -> hidden) tears the plaintext down. The
  // hard-timer below is the load-bearing one; these are best-effort because
  // fullscreen kiosk Chromium often does not fire them.
  useEffect(() => {
    if (!isSecure(item)) return;
    const onBlur = () => dismiss();
    const onVis = () => {
      if (document.visibilityState === "hidden") dismiss();
    };
    window.addEventListener("blur", onBlur);
    document.addEventListener("visibilitychange", onVis);
    return () => {
      window.removeEventListener("blur", onBlur);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [item, dismiss]);

  // PRIMARY auto-clear: a wall-clock countdown. Uses elapsed-since-start rather
  // than a naive decrement so a throttled/paused kiosk tab still clears close to
  // on time when it resumes. Fires dismiss() at zero.
  useEffect(() => {
    if (!isSecure(item) || !item) {
      setRemaining(0);
      return;
    }
    const total = ttlSeconds(item);
    setRemaining(total);
    const started = Date.now();
    const iv = setInterval(() => {
      const left = total - Math.floor((Date.now() - started) / 1000);
      if (left <= 0) {
        clearInterval(iv);
        setRemaining(0);
        dismiss();
      } else {
        setRemaining(left);
      }
    }, 250);
    return () => clearInterval(iv);
  }, [item, dismiss]);

  // Load watch: for a vault view, if the frame neither loads nor errors within
  // the timeout (e.g. a non-kiosk client whose ${API} points at a dead/unreachable
  // backend), fall back to the graceful notice instead of a hung frame. onLoad
  // and onError below resolve it faster when they fire.
  useEffect(() => {
    if (!isSecure(item)) {
      setLoadState("ok");
      return;
    }
    setLoadState("loading");
    const t = setTimeout(() => {
      setLoadState((s) => (s === "loading" ? "failed" : s));
    }, LOAD_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [item]);

  // Directions first: decided before (and instead of) the vault path.
  const directionsSrc = directionsFrameUrl(item);
  const directions = directionsSrc !== null;
  if (!item || (!item.url && !directions)) return null;

  const secure = !directions && isSecure(item);
  // W1: a vault view is fetched from the BACKEND origin directly so the kiosk
  // browser is the loopback client the backend gates on. Non-vault items keep
  // the existing same-origin (:3003 Next route) behavior untouched.
  const iframeSrc = directions
    ? directionsSrc
    : secure
      ? `${API}${item.url}`
      : item.url;
  const showFailure = secure && loadState === "failed";

  return (
    <div
      className="fixed inset-0 z-[900] flex flex-col"
      style={{
        // Solid (not translucent + backdrop-blur): a full-screen blur pass runs
        // every frame and competes with video decode/compositing on the
        // power-capped Orin (choppy playback). The old background was already
        // 94% opaque, so a solid fill looks the same and drops the GPU cost.
        background: "rgb(8, 6, 20)",
        animation: "slide-up 0.25s ease-out",
      }}
    >
      <div
        className="flex flex-none items-center justify-between gap-4 px-6 py-4"
        style={{ borderBottom: "1px solid var(--line)" }}
      >
        <div className="flex min-w-0 items-center gap-3">
          <span
            className="grid h-9 w-9 flex-none place-items-center rounded-full"
            style={{
              background: "color-mix(in srgb, var(--pink) 18%, transparent)",
            }}
          >
            <MsIcon
              name={secure ? "lock" : directions ? "directions" : "cast"}
              size={20}
              style={{ color: "var(--pink)" }}
            />
          </span>
          <span
            className="truncate"
            style={{
              fontFamily: FREDOKA,
              fontWeight: 600,
              fontSize: 20,
              color: "var(--text)",
            }}
          >
            {item.title ||
              (secure ? "Protected document" : directions ? "Directions" : "Pulled up")}
          </span>
        </div>
        <div className="flex flex-none items-center gap-3">
          {secure && (
            <span
              className="hidden items-center gap-2 rounded-full px-4 py-2 sm:inline-flex"
              style={{
                background: "color-mix(in srgb, var(--pink) 14%, transparent)",
                border:
                  "1px solid color-mix(in srgb, var(--pink) 35%, transparent)",
                fontFamily: NUNITO,
                fontWeight: 700,
                fontSize: 13,
                color: "var(--pink)",
                whiteSpace: "nowrap",
              }}
            >
              {"🔒 Protected · auto-clears in "}
              {remaining}s
            </span>
          )}
          <button
            type="button"
            onClick={dismiss}
            className="flex flex-none cursor-pointer items-center gap-2 rounded-full border-0 px-5 py-2 transition hover:opacity-90"
            style={{
              background: "linear-gradient(140deg, var(--pink), var(--magenta))",
              color: "#fff",
              fontFamily: NUNITO,
              fontWeight: 700,
              fontSize: 15,
            }}
          >
            <MsIcon name="close" size={19} />
            Close
          </button>
        </div>
      </div>
      {showFailure ? (
        <div className="flex min-h-0 w-full flex-1 flex-col items-center justify-center gap-4 px-6 text-center">
          <span
            className="grid h-16 w-16 place-items-center rounded-full"
            style={{
              background: "color-mix(in srgb, var(--pink) 14%, transparent)",
            }}
          >
            <MsIcon name="lock" size={34} style={{ color: "var(--pink)" }} />
          </span>
          <span
            style={{
              fontFamily: FREDOKA,
              fontWeight: 600,
              fontSize: 20,
              color: "var(--text)",
              maxWidth: 420,
            }}
          >
            This document can only be viewed on your home screen.
          </span>
        </div>
      ) : secure ? (
        <iframe
          key={item.seq ?? item.url}
          src={iframeSrc}
          title={item.title || "Protected document"}
          className="min-h-0 w-full flex-1"
          style={{ border: 0, background: "var(--card, #0b0616)" }}
          sandbox=""
          referrerPolicy="no-referrer"
          onLoad={() => setLoadState("ok")}
          onError={() => setLoadState("failed")}
        />
      ) : (
        // Non-secure (knowledge/general) iframe: no `key`, matching the kiosk's
        // anti-flash tuning — a seq change should not force an iframe remount +
        // full reload (a re-decode competes with compositing on the capped box).
        <iframe
          src={iframeSrc}
          title={item.title || "Pulled up"}
          className="min-h-0 w-full flex-1"
          style={{ border: 0, background: "var(--card, #0b0616)" }}
          allow="autoplay; fullscreen"
        />
      )}
    </div>
  );
}
