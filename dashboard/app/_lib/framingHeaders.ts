/**
 * Kiosk-safe upstream helpers: the pure parts of the loopback reverse proxies
 * that make a third-party web app survivable inside the wall kiosk's iframe.
 *
 * Two jobs, both pure and both unit-tested (npm run test:framing):
 *   stripFramingHeaders   let the kiosk frame the app at all
 *   rewriteBlankTargets   stop the framed app from escaping the frame
 *
 * (Module name kept as framingHeaders for the sake of a readable history; both
 * halves are about the same frame.)
 *
 * WHY IT EXISTS
 * -------------
 * Calibre-web answers with `X-Frame-Options: SAMEORIGIN`, which means the wall
 * kiosk cannot put its UI in an iframe on the dashboard's own page. The
 * loopback proxy in instrumentation.ts removes that one header on the way back
 * so the Books page can frame it.
 *
 * That removal is ONLY safe because the proxy binds 127.0.0.1 / ::1 and
 * nothing else: the only page that can reach the stripped response is one
 * already running on the appliance itself, i.e. the kiosk. Stripping
 * X-Frame-Options on a LAN-bound listener would hand every device on the
 * network a clickjacking target pointed at the household's library. If this
 * function ever gets reused, check the listener first.
 *
 * SCOPE — exactly two headers, and the CSP one conditionally:
 *   x-frame-options                       always dropped (its only job is to
 *                                         forbid framing)
 *   content-security-policy               dropped ONLY when it carries a
 *   content-security-policy-report-only   `frame-ancestors` directive, which
 *                                         is the CSP spelling of the same
 *                                         refusal. A CSP without it (which is
 *                                         what Calibre-web sends today) is
 *                                         passed through untouched, so the
 *                                         framed app keeps its script-src and
 *                                         friends.
 * Everything else — set-cookie, content-type, cache-control, etags, vary,
 * unknown vendor headers — is copied through verbatim, original key casing
 * included.
 *
 * Isomorphic on purpose: no node: imports, no DOM access, no I/O. That is what
 * lets it be unit-tested directly (npm run test:framing).
 *
 * puff puff pass
 */

/** A node-style incoming/outgoing header bag. */
export type HeaderBag = Record<string, string | string[] | number | undefined>;

/** The two CSP header names, lowercased. */
const CSP_KEYS = ["content-security-policy", "content-security-policy-report-only"];

/**
 * True if a header value carries a `frame-ancestors` directive.
 *
 * Node hands repeated headers over as an array; a single one of them saying
 * `frame-ancestors 'none'` is enough to block the frame, because multiple CSP
 * headers are enforced as an intersection. So: any element counts.
 */
export function hasFrameAncestors(value: string | string[] | number | undefined): boolean {
  if (value === undefined || value === null) return false;
  const parts = Array.isArray(value) ? value : [value];
  return parts.some((part) => String(part).toLowerCase().includes("frame-ancestors"));
}

/**
 * Copy of `headers` with the framing refusals removed. Input is never mutated.
 *
 * Header names are matched case-insensitively (HTTP header names are
 * case-insensitive, and only node's own parser guarantees lowercase keys), but
 * the surviving keys keep whatever casing they arrived with.
 */
export function stripFramingHeaders(headers: HeaderBag | null | undefined): HeaderBag {
  const out: HeaderBag = {};
  if (!headers) return out;
  for (const key of Object.keys(headers)) {
    const lower = key.toLowerCase();
    const value = headers[key];
    if (lower === "x-frame-options") continue;
    if (CSP_KEYS.indexOf(lower) >= 0 && hasFrameAncestors(value)) continue;
    out[key] = value;
  }
  return out;
}

/**
 * Every `target="_blank"` in an HTML document, rewritten to `target="_self"`.
 *
 * WHY
 * ---
 * Calibre-web's book detail page opens "Read in Browser", "Listen in Browser"
 * and each format link with target="_blank" (cps/templates/detail.html). In a
 * normal browser that is a new tab. On the wall kiosk — Chromium --kiosk, no
 * tab strip, no back button, no close — it is a new TOP-LEVEL window with no
 * way out, which is the precise dead end app/_lib/externalLinks.ts exists to
 * prevent. The dashboard's ExternalLinkGuard cannot help here: the click
 * happens inside a cross-document iframe it can neither see nor cancel. So the
 * escape has to be removed upstream of the browser, in the proxy.
 *
 * Navigating in the SAME frame is recoverable: Calibre-web's reader has its own
 * in-page back button (read.html's #back-button → web.index), and the kiosk's
 * rail is still on screen around the iframe.
 *
 * SCOPE / LIMITS
 * --------------
 * A regex over markup, deliberately: parsing Calibre-web's HTML to rewrite two
 * attributes would cost more than it protects. Consequences, accepted:
 *   - a literal `target="_blank"` inside a <script> string or in visible page
 *     text is rewritten too (harmless here — the former is the same escape by
 *     another route, the latter does not occur in this app)
 *   - only the exact value `_blank` is touched. A NAMED target (`target="x"`)
 *     is left alone: it addresses an existing frame, not a new window, and
 *     rewriting it would break intra-app framing.
 *   - `rel`, `href` and everything else on the anchor are untouched.
 * Matching is case-insensitive on both the attribute and the value, tolerates
 * whitespace around `=`, and accepts the double-quoted, single-quoted and
 * unquoted spellings. The unquoted form must end at whitespace, `/` or `>` so
 * `target=_blankfoo` (a named target that merely starts with the word) is not
 * mangled.
 */
const BLANK_TARGET =
  /target(\s*=\s*)(?:"_blank"|'_blank'|_blank(?=[\s/>]|$))/gi;

export function rewriteBlankTargets(html: string): string {
  if (!html) return html;
  return html.replace(BLANK_TARGET, 'target="_self"');
}
