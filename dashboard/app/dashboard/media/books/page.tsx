"use client";

/**
 * /dashboard/media/books — the household's Calibre-web library.
 *
 * Three renders, decided by the hostname the page was loaded from and by
 * whether something is framing us:
 *
 * KIOSK (loopback origin): a slim header strip plus a full-height iframe of
 *   http://localhost:8083, the books-proxy in instrumentation.ts. Calibre-web
 *   itself sends `X-Frame-Options: SAMEORIGIN` and cannot be framed at all;
 *   the proxy strips that header, and it only listens on loopback, so this is
 *   the one origin where the frame is possible (and the one where it is safe).
 *   The strip exists because the kiosk is Chromium --kiosk, touch only, no
 *   browser chrome: once a framed app owns the screen the only way back to
 *   Media was the rail's main entry. The X (and Escape, for a keyboard) puts
 *   her back in /dashboard/media directly.
 *
 * SHOW-ME OVERLAY (framed): no strip at all. ShowViewer draws its own close
 *   affordance over the frame, and the dashboard layout's embed guard has
 *   already dropped the rail and topbar, so the iframe simply fills the frame.
 *
 * PHONE / LAPTOP (any other hostname): framing would be refused by
 *   X-Frame-Options with nothing but a blank rectangle to show for it, so
 *   don't pretend. One card, one button, opening the real Calibre-web in a
 *   real browser tab via the documented containment opt-out
 *   (data-external="allow" — the link-containment reader extracts ARTICLE text
 *   and would fail on a JS app). The button carries the SAME target path as
 *   the kiosk frame, so "show me Dune" on a phone still offers Dune.
 *
 * DEEP LINK: the backend answers "show me <book>" by queuing
 *   ?book=<calibre id>&fmt=<epub|cbr|cbz|pdf>. Validation and URL building are
 *   calibreTargetPath (app/_lib/booksTarget.ts) — junk degrades to the
 *   catalogue, never to a crafted path. Unauthenticated, Calibre-web bounces a
 *   deep link through /login?next=… and lands back on the book, so these are
 *   safe as an iframe src.
 *
 * The persistent JellyfinFrame is deliberately NOT reused: that overlay exists
 * so playback and the cast target survive navigation. A book reader has no
 * such state worth pinning to every page, and a second always-mounted iframe
 * would cost the kiosk memory for nothing.
 *
 * puff puff pass
 */
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import SectionMain from "@/app/_components/Section/Main";
import MsIcon from "@/app/_components/MsIcon";
import { FREDOKA, NUNITO } from "@/app/_components/Widgets/DesignTile";
import { calibreTargetPath } from "@/app/_lib/booksTarget";

const LOOPBACK = ["localhost", "127.0.0.1", "::1"];

/** Where the X goes. The media hub, not the dashboard root: she came from
 *  Media and that is where "close" should mean. */
const BACK_TO = "/dashboard/media";

/** Same origin rule as the Jellyfin page: loopback frames the local proxy,
 *  everything else gets the LAN address it already resolved us at.
 *  Client-only (window). */
function resolveCalibreUrl(hostname: string): string {
  if (LOOPBACK.indexOf(hostname) >= 0) {
    return process.env.NEXT_PUBLIC_CALIBRE_URL ?? "http://localhost:8083";
  }
  return `http://${hostname}:8083`;
}

/** Both halves of the join carry a slash; one is enough. */
function joinUrl(base: string, path: string): string {
  return base.replace(/\/+$/, "") + path;
}

type View = {
  /** Loopback origin: the proxy can be framed. */
  framed: boolean;
  /** Framed by ShowViewer: render content only, no strip. */
  embedded: boolean;
  /** Fully resolved Calibre-web URL, deep link included. */
  src: string;
};

export default function MediaBooksPage() {
  const router = useRouter();
  /* null until mount: SSR and the first client render must agree, and the
     hostname, the query string and window.top are all client-only. */
  const [view, setView] = useState<View | null>(null);
  /* null while the pre-flight runs; false = Calibre-web did not answer
     through the books proxy (not configured, or not running). */
  const [connected, setConnected] = useState<boolean | null>(null);

  useEffect(() => {
    let live = true;
    fetch("/api/media/status?service=books", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { ok: false }))
      .then((d: { ok?: boolean }) => {
        if (live) setConnected(Boolean(d?.ok));
      })
      .catch(() => {
        if (live) setConnected(false);
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    let embedded = false;
    try {
      embedded = window.self !== window.top;
    } catch {
      // Cross-origin parent throws on access → we are definitely framed.
      embedded = true;
    }

    const host = window.location.hostname;
    let path = "/";
    try {
      const q = new URLSearchParams(window.location.search);
      path = calibreTargetPath({ book: q.get("book"), fmt: q.get("fmt") });
    } catch {
      /* no query: the catalogue */
    }

    setView({
      framed: LOOPBACK.indexOf(host) >= 0,
      embedded,
      src: joinUrl(resolveCalibreUrl(host), path),
    });
  }, []);

  /* Escape is the keyboard twin of the X. Only when the strip is on screen —
     inside the overlay, Escape belongs to ShowViewer. */
  const stripped = view !== null && view.framed && !view.embedded;
  useEffect(() => {
    if (!stripped) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") router.push(BACK_TO);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stripped, router]);

  if (view !== null && view.framed && connected !== false) {
    const frame = (
      <iframe
        /* Keyed by the src so a new "show me" replaces the book outright
           instead of pushing a history entry the kiosk cannot go back from. */
        key={view.src}
        src={view.src}
        title="Calibre-web — the house library"
        className={
          view.embedded
            ? "h-dvh w-full border-0"
            : "h-[calc(100vh-7rem-3.5rem)] min-h-[440px] w-full rounded-[28px] border-0"
        }
        style={{ background: "var(--card)" }}
      />
    );

    // Inside the show-me overlay: the frame is the whole page.
    if (view.embedded) return frame;

    return (
      <SectionMain>
        <div className="mb-2 flex h-14 items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <MsIcon name="menu_book" size={22} fill style={{ color: "var(--lemon)" }} />
            <span
              className="truncate"
              style={{
                fontFamily: FREDOKA,
                fontWeight: 600,
                fontSize: 17,
                color: "var(--text)",
              }}
            >
              Books
            </span>
          </div>
          <button
            type="button"
            onClick={() => router.push(BACK_TO)}
            aria-label="Close books"
            className="grid h-11 w-11 shrink-0 cursor-pointer place-items-center rounded-full transition-opacity hover:opacity-80"
            style={{
              background: "var(--chip)",
              border: "1px solid var(--line)",
              color: "var(--text)",
            }}
          >
            <MsIcon name="close" size={22} />
          </button>
        </div>
        {frame}
      </SectionMain>
    );
  }

  return (
    <SectionMain>
      {/* Spacer while the hostname is unknown, so the page does not jump. */}
      {view === null ? (
        <div aria-hidden className="h-[calc(100vh-7rem)] min-h-[480px] w-full" />
      ) : (
        <div
          className="max-w-[520px] rounded-[28px] border p-5"
          style={{ borderColor: "var(--line)", background: "var(--card)" }}
        >
          <div className="flex items-center gap-3">
            <span
              className="grid h-11 w-11 place-items-center rounded-2xl"
              style={{ background: "var(--chip)", color: "var(--lemon)" }}
            >
              <MsIcon name="menu_book" size={24} fill />
            </span>
            <div
              style={{
                fontFamily: FREDOKA,
                fontWeight: 600,
                fontSize: 19,
                color: "var(--text)",
              }}
            >
              Books
            </div>
          </div>
          <div
            className="mt-3"
            style={{
              fontFamily: NUNITO,
              fontWeight: 500,
              fontSize: 13.5,
              lineHeight: 1.45,
              color: "var(--text2)",
            }}
          >
            {connected === false
              ? "Books are not connected. Calibre-web did not answer on this system. To add it, set CALIBRE_LAN_HOST in the dashboard's environment and restart the dashboard."
              : "The shelf opens in its own tab here."}
          </div>
          {connected !== false && (
          <div className="mt-4 flex flex-wrap gap-2">
            <a
              href={view.src}
              target="_blank"
              rel="noopener noreferrer"
              data-external="allow"
              className="inline-flex min-h-[44px] items-center gap-2 rounded-2xl px-4"
              style={{
                background: "var(--chip)",
                border: "1px solid var(--line)",
                fontFamily: NUNITO,
                fontWeight: 600,
                fontSize: 14,
                color: "var(--text)",
              }}
            >
              Open Calibre-web
              <MsIcon name="open_in_new" size={16} style={{ color: "var(--text3)" }} />
            </a>
          </div>
          )}
        </div>
      )}
    </SectionMain>
  );
}
