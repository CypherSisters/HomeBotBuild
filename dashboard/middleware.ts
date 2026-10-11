/**
 * Middleware: security headers for dashboard pages + household pairing gate
 * for every route that proxies to the backend on the server's behalf.
 *
 * RUNTIME: this middleware runs on the **Node.js** runtime (`runtime: "nodejs"`
 * in the exported config, supported since Next 15.5 and used here on 16.2.x).
 * That is required: the pairing check needs node:crypto (HMAC + timingSafeEqual)
 * and node:fs (the pairing state file), neither of which exists on the edge
 * runtime. If a future Next upgrade drops Node middleware, the build fails
 * loudly — it cannot silently degrade to "no gate", and the per-route
 * `requirePairing()` guards in the proxy handlers remain as a second layer.
 */
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import {
  isPaired,
  isSameOrigin,
  pathRequiresPairing,
  renewalCookieFor,
  unpairedResponse,
} from "@/app/_lib/server/pairing";
import { deviceTokenAllows } from "@/app/_lib/server/deviceToken";
import { frameSrcHosts } from "@/app/_lib/externalLinks";
import { jellyfinCspOrigins } from "@/app/_lib/server/lanTargets";

export const runtime = "nodejs";

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // ── pairing gate ────────────────────────────────────────────────────
  // pathRequiresPairing() encodes the staged rollout (PUFF_PAIRING_ENFORCE:
  // off | knowledge | all, defaulting to "knowledge") and always exempts
  // /api/pair/* so an unpaired device can still redeem a code. Pages are never
  // gated here — they must render so the pairing UI can be shown.
  //
  // The same-origin flag is consulted by exactly one rule: the suggestion
  // accept/dismiss exemption at the "knowledge" stage, which must not be
  // drivable cross-site. Omitting it would fail closed (gate everything), so a
  // future refactor that drops it degrades safely.
  //
  // deviceTokenAllows() is a narrow ALTERNATIVE to the cookie: it returns true
  // ONLY for POST /api/pai/knowledge/upload carrying a valid, unexpired,
  // upload-scoped device token (the iPhone Shortcut path). It cannot satisfy
  // the gate for any other route or method — verifyDeviceToken re-checks the
  // token's scope against this request's own method+path. So a leaked upload
  // token can never actuate HA, read a camera, or read the knowledge base.
  if (
    pathRequiresPairing(pathname, request.method, {
      sameOrigin: isSameOrigin(request),
    }) &&
    !isPaired(request) &&
    !deviceTokenAllows(request)
  ) {
    return unpairedResponse();
  }

  const response = NextResponse.next();

  // ── device-cookie renewal / upgrade ────────────────────────────────
  // Runs on EVERY matched request that got past the gate, including plain
  // /dashboard/* page loads. That breadth is the point: the wall kiosk mostly
  // loads pages, so gating renewal on API traffic would leave the one device
  // that can never re-pair itself on the oldest cookie in the house.
  //
  // Two things happen here, both only for a request that ALREADY carries a
  // valid cookie:
  //   - a v2 cookie older than 30 days is re-issued with the same device id
  //     (sliding renewal — only possible for a device still in the registry,
  //     since v2 verification requires membership; a revoked device cannot
  //     renew its way back in);
  //   - a legacy v1 cookie is upgraded in place: the device gets a registry row
  //     and a v2 cookie, which is what finally makes it individually revocable.
  //
  // It deliberately CANNOT touch the unpaired path — that returned above — and
  // renewalCookieFor() returns null for anything that does not verify, so no
  // Set-Cookie is ever attached to a request that failed the gate or that was
  // authorised by a scoped device token instead of a cookie.
  //
  // Wrapped so a failure here is a no-op, never a 500. Renewal is housekeeping:
  // the request has already been authorised, and an unwritable registry or an
  // unreadable secret must not turn a good request into an error page. The
  // device simply keeps the cookie it arrived with and we try again next time.
  try {
    const renewal = renewalCookieFor(request);
    if (renewal) response.cookies.set(renewal);
  } catch {
    /* no renewal this request */
  }

  // Security headers apply to dashboard pages only (unchanged behaviour —
  // adding them to JSON API responses would be noise, and the CSP is about
  // what the page is allowed to load).
  if (pathname === "/dashboard" || pathname.startsWith("/dashboard/")) {
    // SAMEORIGIN, not DENY: the "show me X" overlay (ShowViewer) frames the
    // dashboard's own routes (the knowledge viewer, the media watch page, an app
    // page) in a full-screen iframe. DENY blocked every one of those — the frame
    // rendered the browser's "refused to connect" error. SAMEORIGIN still blocks
    // any cross-origin site from framing the dashboard (clickjacking defense is
    // intact); only our own same-origin pages may embed each other.
    response.headers.set("X-Frame-Options", "SAMEORIGIN");
    response.headers.set("X-Content-Type-Options", "nosniff");
    response.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
    response.headers.set("X-XSS-Protection", "1; mode=block");

    // CSP for local-first app.
    // Jellyfin is allowed as an image and connect source (Now Playing album
    // art, the Media page). localhost:8096 is the loopback media proxy
    // (instrumentation.ts) that the kiosk iframes; loopback is exempt from
    // browsers' Local Network Access policy. The LAN origin is what every
    // other browser on the network reaches directly, and it is added only when
    // JELLYFIN_LAN_HOST is set to a plain hostname or IPv4 address. Read per
    // request (this middleware runs on the Node runtime, like the
    // PUFF_PAIRING_ENFORCE read in pairing.ts), and validated so the env value
    // can never add a CSP directive.
    const JELLYFIN = jellyfinCspOrigins(process.env);
    // frame-src now also covers the contained-link overlay's FRAME mode: the
    // LAN apps the dashboard links to with target="_blank" (Node-RED :1880,
    // Home Assistant :8123) are rendered in a sandboxed iframe
    // INSIDE the overlay so the close button always stays reachable on the
    // kiosk. It is a four-host ALLOWLIST (the appliance itself + HA), http
    // only — never a blanket `http:`, which would let injected markup embed
    // an arbitrary page inside the dashboard's own chrome. Kept in
    // app/_lib/externalLinks.ts so the client-side mode picker and this header
    // cannot drift apart. Jellyfin's origins are a subset and stay listed
    // explicitly for img-src/connect-src.
    const LAN_FRAMES = frameSrcHosts();
    response.headers.set(
      "Content-Security-Policy",
      "default-src 'self'; " +
        "script-src 'self' 'unsafe-eval' 'unsafe-inline'; " +
        "style-src 'self' 'unsafe-inline'; " +
        `img-src 'self' data: blob: ${JELLYFIN}; ` +
        // World Radio streams live on arbitrary LAN-external hosts, many http-only; the page itself is http on the LAN so there is no mixed-content downgrade.
        "media-src 'self' blob: http: https:; " +
        `frame-src 'self' ${LAN_FRAMES}; ` +
        // Standards-track twin of X-Frame-Options: SAMEORIGIN above. 'self'
        // lets the show-me overlay frame the dashboard's own routes while still
        // refusing every cross-origin embedder — browsers that honor CSP over
        // XFO get the same same-origin-only rule.
        "frame-ancestors 'self'; " +
        // Third-party library HTML (ZIM packs) is injected into the knowledge
        // viewer, and default-src covers neither of these two directives.
        "base-uri 'self'; form-action 'self'; " +
        `connect-src 'self' blob: http://localhost:* ws://localhost:* ${JELLYFIN}; ` +
        "font-src 'self'; worker-src 'self' blob:;"
    );
  }

  return response;
}

export const config = {
  runtime: "nodejs",
  // /dashboard/:path*  → security headers (and /dashboard/device/models, a
  //                      server-side Ollama proxy, when enforcement is "all")
  // the /api entries    → every server-side proxy that reaches the backend or
  //                      another LAN service carrying the appliance's own
  //                      credentials. /api/pair/* is intentionally absent.
  // Not matched (no credential, no backend reach): /api/health,
  // /api/media/status, /api/system/stats, /api/radio/* (the World Radio
  // catalog proxy — reaches only the public radio-browser.info API).
  matcher: [
    "/dashboard/:path*",
    "/api/pai/:path*",
    "/api/knowledge/:path*",
    "/api/ha/:path*",
    "/api/camera/:path*",
    "/api/spotify/:path*",
  ],
};
