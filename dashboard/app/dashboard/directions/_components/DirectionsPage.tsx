"use client";

/**
 * DirectionsPage - one page for the kiosk screen and for paper.
 *
 * Served at /dashboard/directions (newest route) and
 * /dashboard/directions/<id>/print (one route; "latest" also works). The
 * show-me overlay (ShowViewer) frames the print path when the backend queues a
 * "directions" item, so on the kiosk this renders inside that iframe and the
 * overlay's own Close button dismisses it, the same as every other kind. Opened
 * on its own (a phone or laptop following the link in chat) it shows a Back to
 * dashboard button instead.
 *
 * Kiosk drawing budget (no graphics chip): no animations, no blur, no shadows,
 * no backdrop filters, no map tiles. The QR is one inline SVG path.
 *
 * Printing is the browser's own print (the box has no printer; phones and
 * laptops print to the home printer as usual). On the wall display itself the
 * Print button is replaced by a line saying where to print from, because a
 * print dialog on a kiosk with no printer is a dead end.
 *
 * Data: GET /api/pai/maps/routes/<id|latest> and /api/pai/maps/status (the
 * keyed proxy; "maps" is on PROXY_ALLOWED_PREFIXES).
 *
 * Every string on the page comes from buildDirectionsView() in
 * app/_lib/directions.ts, which never carries coordinates.
 *
 * puff puff pass
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import MsIcon from "@/app/_components/MsIcon";
import { FREDOKA, NUNITO, JBM } from "@/app/_components/Widgets/DesignTile";
import { apiGetRaw } from "@/app/_lib/api";
import { encodeQr, qrToSvg } from "@/app/_lib/qr";
import { detectKiosk } from "@/app/dashboard/_lib/kiosk";
import {
  buildDirectionsView,
  classifyLoad,
  fitOmapsUrl,
  QR_TOO_LONG_MESSAGE,
  LOAD_MESSAGES,
  routeApiPath,
  type LoadResult,
} from "@/app/_lib/directions";

const FETCH_TIMEOUT_MS = 8000;

/** True when the link encodes at the roomiest level (L, version 10 max). */
function fitsQr(url: string): boolean {
  try {
    encodeQr(url, { ecc: "L" });
    return true;
  } catch {
    return false;
  }
}

/**
 * The QR as an SVG string, or null if the link is missing or too long even
 * after fitOmapsUrl() shortens the place name (or drops it).
 */
function qrSvg(link: string | null): string | null {
  const url = link ? fitOmapsUrl(link, fitsQr) : null;
  if (!url) return null;
  for (const ecc of ["M", "L"] as const) {
    try {
      // Fixed title: nothing from the record goes into the SVG markup.
      return qrToSvg(encodeQr(url, { ecc }), {
        quietZone: 4,
        dark: "#000000",
        light: "#ffffff",
        title: "Open in Organic Maps",
      });
    } catch {
      /* too long at this level, try the next */
    }
  }
  return null;
}

async function fetchRaw(path: string) {
  try {
    const r = await apiGetRaw(path, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
    return { status: r.status, data: r.data as unknown };
  } catch {
    return null;
  }
}

/* Print and screen rules. Plain CSS so @media print can reach the layout's
   chrome: every element that is not this page or one of its ancestors is
   removed, and the ancestors lose their fixed heights and scroll clipping so
   the steps flow onto as many letter pages as they need. */
const CSS = `
.pd-printonly { display: none; }
.pd-root { color: var(--text); }
.pd-steps li { break-inside: avoid; }
@media print {
  @page { size: letter; margin: 0.5in; }
  html, body { background: #fff !important; }
  body *:not(:has(.pd-root)):not(.pd-root):not(.pd-root *) { display: none !important; }
  *:has(.pd-root) {
    height: auto !important; min-height: 0 !important; max-height: none !important;
    overflow: visible !important; position: static !important; display: block !important;
    background: #fff !important;
  }
  .pd-root, .pd-root * {
    color: #000 !important; background: transparent !important;
    border-color: #999 !important;
  }
  .pd-root { padding: 0 !important; max-width: none !important; font-size: 11pt; }
  .pd-noprint { display: none !important; }
  .pd-printonly { display: block !important; }
  .pd-dest { font-size: 20pt !important; }
  .pd-stat { font-size: 13pt !important; }
  .pd-steps li { padding: 4pt 0 !important; }
  .pd-step-text { font-size: 12pt !important; }
  .pd-step-n { width: 22pt !important; height: 22pt !important; font-size: 10pt !important;
    border: 1px solid #000 !important; }
  .pd-qr { width: 1.3in !important; height: 1.3in !important; }
}
`;

export default function DirectionsPage({ id }: { id: string }) {
  const [result, setResult] = useState<LoadResult | null>(null);
  const [embedded, setEmbedded] = useState(false);
  const [kiosk, setKiosk] = useState(false);
  const [now] = useState(() => new Date());

  useEffect(() => {
    try {
      setEmbedded(window.self !== window.top);
    } catch {
      setEmbedded(true);
    }
    setKiosk(detectKiosk());
  }, []);

  const load = useCallback(async () => {
    setResult(null);
    const path = routeApiPath(id);
    if (!path) {
      setResult({ kind: "not-found" });
      return;
    }
    const [route, status] = await Promise.all([fetchRaw(path), fetchRaw("/maps/status")]);
    setResult(classifyLoad(route, status));
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  const view = useMemo(
    () => (result?.kind === "ready" ? buildDirectionsView(result.route, now) : null),
    [result, now],
  );
  const qr = useMemo(() => qrSvg(view?.qrUrl ?? null), [view]);

  useEffect(() => {
    if (view) document.title = view.title;
  }, [view]);

  const btn = {
    fontFamily: NUNITO,
    fontWeight: 700,
    fontSize: 17,
    minHeight: 48,
  } as const;

  const actions = (
    <div className="pd-noprint flex flex-wrap items-center gap-3">
      {view && !kiosk && (
        <button
          type="button"
          onClick={() => window.print()}
          className="flex cursor-pointer items-center gap-2 rounded-full border-0 px-6 py-2"
          style={{
            ...btn,
            background: "linear-gradient(140deg, var(--pink), var(--magenta))",
            color: "#fff",
          }}
        >
          <MsIcon name="print" size={20} />
          Print
        </button>
      )}
      {!embedded && (
        <Link
          href="/dashboard"
          className="flex items-center gap-2 rounded-full px-6 py-2"
          style={{ ...btn, border: "1px solid var(--line)", color: "var(--text)" }}
        >
          <MsIcon name="arrow_back" size={20} />
          Back to dashboard
        </Link>
      )}
    </div>
  );

  if (!result || !view) {
    const message = !result
      ? "Loading directions..."
      : LOAD_MESSAGES[result.kind as Exclude<LoadResult["kind"], "ready">];
    return (
      <div className="pd-root mx-auto flex max-w-[1100px] flex-col gap-6 px-6 py-8">
        <style>{CSS}</style>
        <div
          style={{ fontFamily: JBM, fontWeight: 700, fontSize: 12, letterSpacing: "0.14em", color: "var(--text3)" }}
        >
          DIRECTIONS
        </div>
        <p
          role={result ? "alert" : "status"}
          style={{ fontFamily: FREDOKA, fontWeight: 600, fontSize: 28, lineHeight: 1.3, maxWidth: 760 }}
        >
          {message}
        </p>
        <div className="flex flex-wrap items-center gap-3">
          {result?.kind === "error" && (
            <button
              type="button"
              onClick={load}
              className="flex cursor-pointer items-center gap-2 rounded-full px-6 py-2"
              style={{ ...btn, border: "1px solid var(--line)", background: "var(--card)", color: "var(--text)" }}
            >
              <MsIcon name="refresh" size={20} />
              Try again
            </button>
          )}
          {actions}
        </div>
      </div>
    );
  }

  const micro = {
    fontFamily: JBM,
    fontWeight: 700,
    fontSize: 12,
    letterSpacing: "0.14em",
    textTransform: "uppercase",
    color: "var(--text3)",
  } as const;

  return (
    <div className="pd-root mx-auto flex max-w-[1100px] flex-col gap-5 px-6 py-6">
      <style>{CSS}</style>

      <header className="flex items-start justify-between gap-6">
        <div className="flex min-w-0 flex-col gap-2">
          <div style={micro} className="pd-noprint">
            Directions · {view.mode}
          </div>
          <h1
            className="pd-printonly"
            style={{ fontFamily: FREDOKA, fontWeight: 600, fontSize: "20pt", margin: 0 }}
          >
            {view.title}
          </h1>
          <div
            className="pd-dest pd-noprint"
            style={{ fontFamily: FREDOKA, fontWeight: 600, fontSize: "clamp(28px, 3.6vw, 46px)", lineHeight: 1.1 }}
          >
            {view.destination}
          </div>
          <div style={{ fontFamily: NUNITO, fontSize: 17, color: "var(--text2)" }}>
            <span className="pd-printonly">
              From Home to {view.destination}
              {view.address ? `, ${view.address}` : ""}
            </span>
            <span className="pd-noprint">{view.address ? `From Home to ${view.address}` : "From Home"}</span>
          </div>
          <div className="mt-2 flex flex-wrap items-baseline gap-x-8 gap-y-2">
            {view.distance && (
              <span className="pd-stat" style={{ fontFamily: FREDOKA, fontWeight: 600, fontSize: 30 }}>
                {view.distance}
              </span>
            )}
            {view.duration && (
              <span className="pd-stat" style={{ fontFamily: FREDOKA, fontWeight: 600, fontSize: 30 }}>
                {view.duration}
              </span>
            )}
            <span className="pd-stat" style={{ fontFamily: NUNITO, fontWeight: 700, fontSize: 20, color: "var(--text2)" }}>
              {view.mode}
            </span>
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-1" style={{ fontFamily: NUNITO, fontSize: 15, color: "var(--text2)" }}>
            {view.trafficNote && <span>{view.trafficNote}</span>}
            <span>{view.dataDate}</span>
            <span className="pd-printonly">{view.printedOn}</span>
          </div>
        </div>

        {qr && (
          <figure className="m-0 flex flex-none flex-col items-center gap-2">
            <div
              className="pd-qr"
              style={{ width: 168, height: 168, background: "#fff", borderRadius: 12, overflow: "hidden" }}
              dangerouslySetInnerHTML={{ __html: qr }}
            />
            <figcaption style={{ fontFamily: NUNITO, fontWeight: 700, fontSize: 14, color: "var(--text2)", maxWidth: 168, textAlign: "center" }}>
              Scan to open in Organic Maps
            </figcaption>
          </figure>
        )}
        {!qr && view.qrUrl && (
          <p
            className="m-0 flex-none"
            style={{ fontFamily: NUNITO, fontWeight: 700, fontSize: 15, color: "var(--text2)", maxWidth: 220 }}
          >
            {QR_TOO_LONG_MESSAGE}
          </p>
        )}
      </header>

      <div className="flex flex-wrap items-center justify-between gap-3">
        {actions}
        {kiosk && (
          <span className="pd-noprint" style={{ fontFamily: NUNITO, fontSize: 15, color: "var(--text2)" }}>
            To print, open Directions on your phone or laptop.
          </span>
        )}
      </div>

      <ol className="pd-steps m-0 flex list-none flex-col p-0" style={{ borderTop: "1px solid var(--line)" }}>
        {view.steps.map((s, i) => (
          <li
            key={`${s.n}-${i}`}
            className="flex items-start gap-4 py-4"
            style={{ borderBottom: "1px solid var(--line)" }}
          >
            <span
              className="pd-step-n grid flex-none place-items-center rounded-full"
              style={{
                width: 44,
                height: 44,
                background: "var(--chip)",
                fontFamily: JBM,
                fontWeight: 700,
                fontSize: 18,
              }}
            >
              {s.n}
            </span>
            <span
              className="pd-step-text min-w-0 flex-1"
              style={{ fontFamily: NUNITO, fontWeight: 600, fontSize: "clamp(20px, 2.2vw, 28px)", lineHeight: 1.3, paddingTop: 4 }}
            >
              {s.text}
            </span>
            {s.distance && (
              <span
                className="flex-none"
                style={{ fontFamily: JBM, fontWeight: 700, fontSize: 16, color: "var(--text2)", paddingTop: 10, whiteSpace: "nowrap" }}
              >
                {s.distance}
              </span>
            )}
          </li>
        ))}
      </ol>
      {view.steps.length === 0 && (
        <p style={{ fontFamily: NUNITO, fontSize: 18, color: "var(--text2)" }}>
          No turn by turn steps were saved for this route.
        </p>
      )}
    </div>
  );
}
