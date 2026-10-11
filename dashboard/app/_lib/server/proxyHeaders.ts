/**
 * Which headers the keyed proxy sends upstream, and where each one comes from.
 *
 * Extracted from app/api/pai/[...path]/route.ts so the policy is a pure
 * function with a test, instead of a header block inside a Next route handler
 * that nothing could exercise without a running server.
 *
 * THE RULE: the client contributes almost nothing. The returned object is built
 * from scratch on every call, so a header is upstream only because a line here
 * put it there. Everything that carries authority is either injected from
 * server-side state (the appliance API key, the client address) or derived from
 * the pairing cookie the route guard already verified (the device identity).
 *
 * WHAT THIS EXISTS TO PREVENT
 * ---------------------------
 * The backend keys its 300-second vault-unlocked chat session, the one that
 * answers protected questions ("what are my allergies") with DECRYPTED vault
 * content and no further PIN, on a per-caller id. Keyed on `X-Client-Id` or the
 * caller's address, it would put ALL household devices in ONE unlock bucket,
 * because through this proxy every request arrives as 127.0.0.1: one member
 * types the PIN and everybody's Talk page answers from the vault for five
 * minutes. A caller with the API key talking to the backend directly could
 * also just name a bucket.
 *
 * The backend therefore ignores `X-Client-Id` entirely and keys on
 * `X-Puff-Device-Id`, honoured only from a trusted proxy peer (loopback, or an
 * address named in PUFFPAI_TRUSTED_PROXY_IPS), so the identity has to be
 * asserted HERE, from the cookie, and a client copy must never get through.
 * So: an allowlist. A relay default would make every backend header a
 * client-controlled input forever; an allowlist makes each one a decision.
 *
 * A relayed header is also SCOPED to the paths where the backend actually reads
 * it. "Which headers cross" and "on which requests" are two separate decisions;
 * relaying a header everywhere because one subtree needs it hands a credential
 * to every other surface for free.
 *
 * puff puff pass
 */

/**
 * One relayed client header, and the request paths it is relayed ON.
 *
 * `path` is the already-decoded backend path segments (`["vault","contents"]`
 * for `/api/pai/vault/contents`), the same array the proxy allowlist and the
 * route's own checks ran against.
 */
export interface ForwardedClientHeader {
  /** Inbound header name, lowercase (header lookups are case-insensitive). */
  readonly header: string;
  /** Canonical name written upstream, so casing never depends on the client. */
  readonly upstream: string;
  /** True on the paths where the backend actually reads this header. */
  readonly scope: (path: readonly string[]) => boolean;
  /** Human reason, for the next person reading this list. */
  readonly why: string;
}

/**
 * The backend families its first-run setup-code gate covers. Mirrors
 * `api_server._SETUP_CODE_PREFIXES` ('/setup', '/wizard', '/persona',
 * '/build/prove', '/build/self'), segment-anchored the same way `_under_prefix`
 * is, so "/buildings" is not "/build". The header is read nowhere else.
 */
function isSetupCodePath(path: readonly string[]): boolean {
  const [first, second] = path;
  if (first === "setup" || first === "wizard" || first === "persona") return true;
  return first === "build" && (second === "prove" || second === "self");
}

/**
 * The families whose WRITES the backend refuses after first run unless the
 * caller holds an unlocked vault session (`api_server._setup_gate`, reached
 * from the /setup routes and from `_enforce_post_setup_gate` for /wizard and
 * /persona). `_setup_vault_session_live()` reads X-Vault-Token there, so the
 * owner's own unlock has to be able to arrive.
 */
function isPostSetupGatedPath(path: readonly string[]): boolean {
  const [first] = path;
  return first === "setup" || first === "wizard" || first === "persona";
}

/**
 * Client headers relayed upstream verbatim. Add an entry only with a reason,
 * only for a header the dashboard's own code sends, and only with the narrowest
 * `scope` that keeps the feature working.
 *
 * Content-Type and Accept are handled separately: not X-*, and transport rather
 * than application semantics.
 *
 * Deliberately NOT here:
 *   x-client-id        the P0 above. Nothing reads it now, and relaying it
 *                      would let a future reader become a hole again.
 *   x-puff-device-id   asserted below from the verified cookie. A client copy
 *                      must never reach the backend or the derivation is
 *                      decorative.
 *   x-puff-anonymous   also asserted below, server-side, and the exact inverse:
 *                      it tells the backend "this proxied caller has NO verified
 *                      identity". A client able to set it could push a paired
 *                      device onto the identity-less path; relaying it in either
 *                      direction would make the marker the client's statement
 *                      instead of ours.
 *   x-puff-display     a client-side presentation hint (kiosk vs. phone). It
 *                      may decide what a screen renders, never who the backend
 *                      thinks is asking, so it has no business upstream.
 *   x-puffpai-client-addr  server-generated below. A client copy would buy the
 *                      backend's "standing at the machine" setup exemption.
 *   x-api-key / authorization  the appliance credential. Injected server-side.
 *   x-zone3-ticket     the dashboard sends consent tickets in the BODY. Nothing
 *                      here sends the header form, and a consent artefact is
 *                      exactly what should not ride in on a relayed header.
 *   x-forwarded-* / x-real-ip  client-settable, and would misinform the
 *                      backend's per-peer rate limiter and trusted-proxy check.
 *   cookie             the puff_device pairing cookie is the credential this
 *                      whole gate rests on. It stops here.
 */
export const FORWARDED_CLIENT_HEADERS: readonly ForwardedClientHeader[] = [
  {
    header: "x-vault-token",
    upstream: "X-Vault-Token",
    // Scoped to the subtrees whose handlers read it: /vault/* (including
    // /vault/secrets/*), and the /setup, /wizard and /persona families, where
    // _setup_vault_session_live() is how the owner, standing at an unlocked
    // vault, may still change a FINISHED build. Relaying it anywhere else
    // would hand a household member's unlock session to /apps, /ha,
    // /knowledge and anything added later, for nothing. Whole subtrees rather
    // than exact paths, because every endpoint there reads the same header and
    // a hardcoded list would silently refuse the next one added.
    scope: (path) => path[0] === "vault" || isPostSetupGatedPath(path),
    why:
      "The caller's own vault UNLOCK SESSION, minted by the backend only after " +
      "a correct PIN and validated there against _vault_sessions and the " +
      "vault's own _validate_session() on every /vault/* call. Relaying " +
      "it cannot bypass the PIN: a caller without a valid token still gets 403 " +
      '"Vault is locked". Without it every /vault/* read 403s through the ' +
      "proxy even after a good unlock.",
  },
  {
    header: "x-setup-code",
    upstream: "X-Setup-Code",
    // The first-run code the owner reads off the machine's console
    // (app/_lib/services/setupService.ts, and the wizard and build services
    // through setupCodeHeaders()). Scoped to the families the backend's
    // _enforce_setup_code() reads it on. The backend rate-limits and rotates
    // it (puffpai/security/setup_code.py). Relayed verbatim, never logged here.
    scope: isSetupCodePath,
    why:
      "The ONE value a client is meant to push through: proof that whoever is " +
      "driving first-run setup from another device can read the console of " +
      "the machine. Without it every setup, wizard and prove call from a " +
      "browser is refused while first-run setup is open.",
  },
];

/** The header the backend keys chat/vault sessions on. Server-set only. */
export const DEVICE_ID_HEADER = "X-Puff-Device-Id";

/**
 * Marker for "this request came through the proxy and has NO verified device
 * identity". Server-set only, and never relayed from a client.
 *
 * WHY A MARKER IS NEEDED AT ALL. Omitting DEVICE_ID_HEADER is ambiguous at the
 * backend: it is what a proxied request with no identity looks like, and ALSO
 * what a direct loopback caller (a script on the box, the CLI) looks like. The
 * backend resolved that ambiguity by keying both on the socket peer, i.e. on
 * `peer:127.0.0.1`, one shared vault-unlock bucket for every unidentified
 * proxied caller in the household. A legacy v1 cookie, an unpaired browser at
 * the "knowledge" enforcement stage, the device-token upload path and the kiosk
 * all landed in it together, so one member's 300-second unlock was inheritable
 * by any of the others for the rest of the window.
 *
 * With the marker the backend can be explicit instead: a trusted proxy peer that
 * says "anonymous" gets a single-use session key and no vault answers, while a
 * direct caller keeps its per-peer behaviour.
 *
 * NOT a display hint. A kiosk is anonymous or identified for exactly the same
 * reason as any other browser (whether its pairing cookie verifies) and never
 * because of where it is or what it renders.
 */
export const ANONYMOUS_HEADER = "X-Puff-Anonymous";

/**
 * Header naming the REAL client of a proxied request. Mirrors
 * `api_server._SETUP_CLIENT_ADDR_HEADER`; the backend trusts it only from a
 * loopback peer (this proxy) and grades anything it cannot parse as a loopback
 * address, including the literal "unknown", as a remote client.
 */
export const CLIENT_ADDR_HEADER = "X-PuffPAI-Client-Addr";

/**
 * Response headers relayed back to the browser. Everything else the backend
 * sends is dropped, as it always was.
 */
export const RELAYED_RESPONSE_HEADERS: readonly string[] = [
  "content-type",
  "cache-control",
  "content-disposition",
];

/**
 * Extra response headers for the offline-library ASSET endpoint only. It
 * serves third-party files (an HTML or SVG page can sit inside a ZIM) on the
 * dashboard's own origin, and for anything that could run it answers with
 * `Content-Disposition: attachment` plus a `sandbox` CSP. Dropping the CSP
 * here would leave the download as the only defence; relaying it everywhere
 * would let any backend route set policy on the dashboard origin.
 */
export const KNOWLEDGE_ASSET_RESPONSE_HEADERS: readonly string[] = [
  "content-disposition",
  "content-security-policy",
  "x-content-type-options",
];

/** `/knowledge/library/<id>/asset`, decoded segments, exact shape. */
export function isKnowledgeAssetPath(path: readonly string[]): boolean {
  return (
    path.length === 4 &&
    path[0] === "knowledge" &&
    path[1] === "library" &&
    !!path[2] &&
    path[3] === "asset"
  );
}

/**
 * The ONLY asset types served inline: images, audio, video, fonts and WebVTT
 * subtitles (a <track> needs text/vtt). CSS, PDF, HTML, SVG: attachment.
 * Key = the bare type (before any ";", trimmed, lower-cased); value = the
 * clean type sent to the browser. Must match _ZIM_INLINE_ASSET_TYPES in the
 * backend's api_server.py. Everything else is an octet-stream attachment.
 */
export const INLINE_ASSET_TYPES: Readonly<Record<string, string>> = Object.freeze({
  "image/png": "image/png",
  "image/jpeg": "image/jpeg",
  "image/jpg": "image/jpeg",
  "image/pjpeg": "image/jpeg",
  "image/gif": "image/gif",
  "image/webp": "image/webp",
  "image/avif": "image/avif",
  "image/bmp": "image/bmp",
  "image/x-icon": "image/x-icon",
  "image/vnd.microsoft.icon": "image/x-icon",
  "video/mp4": "video/mp4",
  "video/webm": "video/webm",
  "video/ogg": "video/ogg",
  "audio/mpeg": "audio/mpeg",
  "audio/mp3": "audio/mpeg",
  "audio/mp4": "audio/mp4",
  "audio/aac": "audio/aac",
  "audio/ogg": "audio/ogg",
  "audio/opus": "audio/opus",
  "audio/webm": "audio/webm",
  "audio/wav": "audio/wav",
  "audio/x-wav": "audio/wav",
  "audio/wave": "audio/wav",
  "audio/flac": "audio/flac",
  "audio/x-flac": "audio/flac",
  "font/woff": "font/woff",
  "font/woff2": "font/woff2",
  "font/ttf": "font/ttf",
  "font/otf": "font/otf",
  "text/vtt": "text/vtt",
  "application/font-woff": "font/woff",
  "application/x-font-woff": "font/woff",
  "application/font-woff2": "font/woff2",
  "application/x-font-ttf": "font/ttf",
  "application/x-font-otf": "font/otf",
});

export const KNOWLEDGE_ASSET_CSP = "default-src 'none'; sandbox";
const ASSET_FALLBACK_TYPE = "application/octet-stream";

/**
 * The Content-Type to send for an asset, and whether it may be inline.
 * Fails closed: a comma, quote or control character anywhere (Headers.get
 * also joins duplicate headers with ", "), or a bare type not on the list,
 * is an octet-stream attachment. Parameters are never forwarded.
 */
export function knowledgeAssetContentType(
  raw: string | null | undefined,
): { contentType: string; inline: boolean } {
  const value = typeof raw === "string" ? raw : "";
  if (!value || /[,"\u0000-\u001f\u007f]/.test(value)) {
    return { contentType: ASSET_FALLBACK_TYPE, inline: false };
  }
  const bare = value.split(";", 1)[0].trim().toLowerCase();
  const clean = Object.prototype.hasOwnProperty.call(INLINE_ASSET_TYPES, bare)
    ? INLINE_ASSET_TYPES[bare]
    : undefined;
  return clean
    ? { contentType: clean, inline: true }
    : { contentType: ASSET_FALLBACK_TYPE, inline: false };
}

/**
 * Set the asset headers on a response, whatever the backend sent: the clean
 * type, nosniff, the sandbox CSP, and an attachment for anything not inline.
 * Defence in depth behind the backend, which applies the same list.
 */
export function hardenKnowledgeAssetHeaders(
  headers: Headers,
  upstreamContentType: string | null | undefined,
): void {
  const { contentType, inline } = knowledgeAssetContentType(upstreamContentType);
  headers.set("Content-Type", contentType);
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Content-Security-Policy", KNOWLEDGE_ASSET_CSP);
  if (inline) headers.delete("Content-Disposition");
  else headers.set("Content-Disposition", "attachment");
}

/** Which upstream response headers the proxy copies back for this path. */
export function relayedResponseHeaders(path: readonly string[]): readonly string[] {
  if (!isKnowledgeAssetPath(path)) return RELAYED_RESPONSE_HEADERS;
  return Array.from(
    new Set([...RELAYED_RESPONSE_HEADERS, ...KNOWLEDGE_ASSET_RESPONSE_HEADERS]),
  );
}

/** Minimal shape read off a request. NextRequest satisfies it. */
export interface HeaderCarrier {
  headers: {
    get(name: string): string | null;
  };
}

/**
 * Build the upstream header set for a request that has ALREADY passed the
 * route's pairing gate and path allowlist.
 *
 * @param req            the incoming request; only headers are read, and only
 *                       the ones named here. An unlisted header is never even
 *                       looked at, let alone forwarded.
 * @param apiKey         PUFFPAI_API_KEY, or "" to attach no Authorization.
 *                       Empty means the backend refuses the call, which is the
 *                       safe direction for a misconfigured box.
 * @param deviceSession  the identity, or null for "assert no identity" (which
 *                       is not silence: null makes this function write
 *                       ANONYMOUS_HEADER). The proxy route supplies
 *                       pairing.proxyDeviceSessionFor(req).sessionId, the same
 *                       derivation as deviceSessionIdFor plus the eager v1
 *                       upgrade. Passed in rather than derived here so this
 *                       function stays pure over its inputs, and so the caller
 *                       cannot hand it something read off the request: the only
 *                       way to get a non-null value is to verify a cookie.
 * @param path           decoded backend path segments, for the scoped relays.
 *                       Required, so a caller cannot forget it; an empty array
 *                       relays no client header at all.
 * @param clientAddr     the proxy's own claim about the client's address; see
 *                       the route's clientAddr(), which returns "unknown" on
 *                       purpose because Next exposes no trustworthy peer.
 *
 * The server-set headers are written LAST so no client-supplied copy can win,
 * and none of them is in the allowlist either, so none gets that far.
 *
 * Content-Length is deliberately NOT forwarded. The route buffers the body and
 * hands fetch a Uint8Array, so undici computes the length itself from the bytes
 * it is actually sending; relaying the client's number could only contradict
 * that (a client may lie, and a rejected-at-the-cap body never gets here).
 */
export function upstreamHeaders(
  req: HeaderCarrier,
  apiKey: string,
  deviceSession: string | null,
  path: readonly string[],
  clientAddr: string,
): Record<string, string> {
  const headers: Record<string, string> = {};

  const ct = req.headers.get("content-type");
  if (ct) headers["Content-Type"] = ct;
  const accept = req.headers.get("accept");
  if (accept) headers["Accept"] = accept;

  for (const rule of FORWARDED_CLIENT_HEADERS) {
    if (!rule.scope(path)) continue;
    const value = req.headers.get(rule.header);
    if (value) headers[rule.upstream] = value;
  }

  // WHOSE request this really is, as far as the backend's first-run setup-code
  // exemption is concerned. Always server-generated; never copied from input.
  headers[CLIENT_ADDR_HEADER] = clientAddr;

  // WHO the backend should treat this as for chat/vault sessions. Derived
  // server-side from the verified pairing cookie: an HMAC of the device id
  // under the pairing secret, so it is neither the cookie (which must not land
  // in a backend log) nor the raw device id (which any paired caller can read
  // from /api/pair/devices).
  //
  // No valid cookie -> the identity header is absent AND the anonymous marker is
  // present. Exactly one of the two is ever written, both server-side, both
  // after the relay loop so no client copy of either can survive.
  //
  // The marker is not a placeholder identity: the backend turns it into a key
  // unique to that one request, so unidentified callers are isolated from each
  // other as well as from paired devices. A constant placeholder here would have
  // reproduced the shared bucket it exists to remove.
  if (deviceSession) {
    headers[DEVICE_ID_HEADER] = deviceSession;
    delete headers[ANONYMOUS_HEADER];
  } else {
    delete headers[DEVICE_ID_HEADER];
    headers[ANONYMOUS_HEADER] = "1";
  }

  if (apiKey) headers["Authorization"] = `Bearer ${apiKey}`;

  return headers;
}
