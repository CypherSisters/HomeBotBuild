/**
 * Device tokens — server-side only. An upload-only bearer credential an
 * already-paired household member can mint for an Apple Shortcut.
 *
 * WHY THIS EXISTS
 * ---------------
 * iOS has no Web Share Target, so "Share → Puff" from an iPhone must be an
 * Apple Shortcut that HTTP-POSTs the shared photo/file to the knowledge upload
 * endpoint. That request is not a browser page load: it carries no cookie jar,
 * so it can never present the HttpOnly `puff_device` pairing cookie the normal
 * gate relies on. This module issues a long-lived bearer token the Shortcut can
 * send as an `Authorization: Bearer pstk_…` header instead.
 *
 * DESIGN (least privilege, revocable, fail-closed)
 * ------------------------------------------------
 *  - Issued ONLY to an already-paired request (valid `puff_device` cookie). A
 *    stranger on the LAN cannot mint one — the issuing route checks the cookie.
 *  - SCOPED to uploads only (`scope: "upload"`). verifyDeviceToken authorises a
 *    token for EXACTLY `POST /api/pai/knowledge/upload` and nothing else. A
 *    leaked upload token can add a document; it can NOT actuate Home Assistant,
 *    read a camera, read the knowledge base, or read the personal vault. Scope
 *    is enforced here on the decoded+casefolded path, the same bytes the
 *    catch-all proxy acts on, so an encoded-separator spelling cannot smuggle a
 *    token onto a different backend route.
 *  - Stored HASHED (sha256 of the raw token), never in the clear — like a
 *    password. The raw token is returned once at creation and never again.
 *  - Revocable per-device (delete the record) and expiring (180-day TTL).
 *
 * THREAT MODEL (be honest about it)
 * ---------------------------------
 *  - Plain HTTP on the LAN: the token crosses in clear, exactly like the
 *    pairing cookie already does. An on-path attacker who can sniff/ARP-spoof
 *    the LAN can capture it. Accepted residual risk; TLS is the real fix and is
 *    not in place. Scope limits the blast radius of such a capture to
 *    "can upload a document", never house control or data exfiltration.
 *  - Issuance is rate-limited and requires an already-paired cookie, so the
 *    token surface cannot be used to bypass pairing — it is a convenience for
 *    an already-trusted device, nothing more.
 *
 * Node runtime only — uses node:crypto and node:fs. Deliberately self-contained
 * (no imports from pairing.ts) so concurrent edits to pairing.ts cannot break
 * it and vice-versa; the only shared surface is the callers that combine
 * requirePairing() with deviceTokenAllows().
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

/* ── constants ─────────────────────────────────────────────────────── */

/** Token prefix. `pstk` = Puff Shortcut ToKen. Public, not a secret. */
const TOKEN_PREFIX = "pstk_";
/** Random bytes behind the prefix (base64url → 43 chars). 256 bits of entropy. */
const TOKEN_SECRET_BYTES = 32;
/** base64url body length for TOKEN_SECRET_BYTES (ceil(32*4/3) with no padding). */
const TOKEN_BODY_LEN = 43;

/** 180 days. Configurable here only — deliberately not env-driven. */
export const TOKEN_TTL_SEC = 180 * 24 * 60 * 60;

/** Only scope that exists today. A token authorises this and nothing else. */
export type DeviceTokenScope = "upload";

/**
 * The ONE method+path an "upload" token authorises, as the decoded, lowercased
 * gate path. Exact match, anchored — no prefix/suffix, no other backend route.
 * This is the request the iPhone Shortcut makes.
 */
const UPLOAD_METHOD = "POST";
const UPLOAD_PATH = "/api/pai/knowledge/upload";

/* ── record + state file ───────────────────────────────────────────── */

export interface DeviceTokenRecord {
  /** Opaque public id (safe to show + revoke by). */
  id: string;
  /** Human label, e.g. "Sam's iPhone". Sanitised, may be empty. */
  label: string;
  /** sha256(raw token) as hex. The raw token is never stored. */
  tokenHash: string;
  scope: DeviceTokenScope;
  createdAt: string;
  expiresAt: string;
  /** Updated (best-effort) each time the token successfully authorises a call. */
  lastUsedAt: string | null;
}

interface DeviceTokenFile {
  version: 1;
  tokens: DeviceTokenRecord[];
}

/**
 * Where the token list lives. Kept a SIBLING of the pairing state so both
 * secrets share one 0700 directory and one backup/rotation story:
 *   - PUFF_DEVICE_TOKENS  explicit override, or
 *   - <dir of PUFF_PAIRING_STATE>/device_tokens.json, or
 *   - <cwd>/data/device_tokens.json   (matches pairing.json's default)
 */
function statePath(): string {
  if (process.env.PUFF_DEVICE_TOKENS) return process.env.PUFF_DEVICE_TOKENS;
  const pairing = process.env.PUFF_PAIRING_STATE;
  if (pairing) return path.join(path.dirname(pairing), "device_tokens.json");
  return path.join(process.cwd(), "data", "device_tokens.json");
}

// Cached on globalThis so Next's dev HMR / multiple route bundles share one
// copy of the in-memory rate-limit bucket instead of racing.
const G = globalThis as unknown as {
  __puffDeviceTokenIssues?: { count: number; windowStart: number };
};

function emptyFile(): DeviceTokenFile {
  return { version: 1, tokens: [] };
}

function isRecord(v: unknown): v is DeviceTokenRecord {
  if (!v || typeof v !== "object") return false;
  const r = v as Partial<DeviceTokenRecord>;
  return (
    typeof r.id === "string" &&
    typeof r.label === "string" &&
    typeof r.tokenHash === "string" &&
    /^[0-9a-f]{64}$/.test(r.tokenHash) &&
    r.scope === "upload" &&
    typeof r.createdAt === "string" &&
    typeof r.expiresAt === "string"
  );
}

/**
 * Read the token file. Missing/corrupt → an empty list (fail closed: no tokens
 * authorise anything, rather than throwing and taking the whole gate down).
 * Reads fresh every time — the file is tiny and the write path is atomic, so we
 * never serve a stale in-memory copy that could keep a revoked token alive.
 */
function readFile(): DeviceTokenFile {
  const file = statePath();
  try {
    const raw = fs.readFileSync(file, "utf8");
    const parsed: unknown = JSON.parse(raw);
    if (
      parsed &&
      typeof parsed === "object" &&
      Array.isArray((parsed as DeviceTokenFile).tokens)
    ) {
      const tokens = (parsed as DeviceTokenFile).tokens.filter(isRecord);
      return { version: 1, tokens };
    }
  } catch {
    /* missing or unreadable → empty */
  }
  return emptyFile();
}

/** Atomic 0600 write (temp + rename), same discipline as pairing.json. */
function writeFile(state: DeviceTokenFile): void {
  const file = statePath();
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2), { mode: 0o600 });
  try {
    fs.chmodSync(tmp, 0o600);
  } catch {
    /* Windows: chmod is a no-op / may throw. Not fatal. */
  }
  fs.renameSync(tmp, file);
}

/* ── constant-time compare ─────────────────────────────────────────── */

function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  const ab = Buffer.from(a, "utf8");
  const bb = Buffer.from(b, "utf8");
  if (ab.length !== bb.length) return false;
  return crypto.timingSafeEqual(ab, bb);
}

function sha256Hex(s: string): string {
  return crypto.createHash("sha256").update(s).digest("hex");
}

/* ── token format ──────────────────────────────────────────────────── */

/** True if `raw` is structurally a well-formed pstk_ token (charset + length). */
function isWellFormedToken(raw: string): boolean {
  if (typeof raw !== "string" || !raw.startsWith(TOKEN_PREFIX)) return false;
  const body = raw.slice(TOKEN_PREFIX.length);
  return body.length === TOKEN_BODY_LEN && /^[A-Za-z0-9_-]+$/.test(body);
}

/* ── scope: which method+path an upload token authorises ───────────── */

/**
 * Collapse a request path to the effective path the proxy will act on, so the
 * scope check inspects the same bytes the catch-all proxy forwards upstream.
 * Mirrors pairing.ts's effectiveGatePath (kept local to avoid a cross-module
 * dependency): fully decode (defeating double-encoding), reject control chars,
 * lowercase. Malformed encoding → null. Fail closed everywhere.
 */
function effectivePath(pathname: string): string | null {
  try {
    let prev = pathname;
    for (let i = 0; i < 3; i++) {
      const dec = decodeURIComponent(prev);
      if (dec === prev) break;
      prev = dec;
    }
    if (/[\x00-\x1f\x7f]/.test(prev)) return null;
    return prev.toLowerCase();
  } catch {
    return null;
  }
}

/**
 * Does an "upload"-scoped token authorise this method+path? EXACT match on
 * POST /api/pai/knowledge/upload (decoded, casefolded). Everything else — any
 * other method, any other path, any traversal or extra segment — is false.
 *
 * This is the scope wall: because it demands whole-path equality, no token can
 * ever satisfy the gate for /api/pai/ha/*, /api/camera/*, a knowledge read, or
 * any other route. An encoded-separator spelling that decodes to the upload
 * path is still only the upload path (and the proxy independently 400s an
 * encoded separator anyway).
 */
function scopeAllows(
  scope: DeviceTokenScope,
  method: string | undefined,
  pathname: string | undefined,
): boolean {
  if (scope !== "upload") return false;
  if ((method || "").toUpperCase() !== UPLOAD_METHOD) return false;
  if (!pathname) return false;
  const eff = effectivePath(pathname);
  if (eff === null) return false;
  return eff === UPLOAD_PATH;
}

/* ── issuance rate limit ───────────────────────────────────────────── */

const ISSUE_WINDOW_MS = 60_000;
const ISSUE_MAX = 10;

/**
 * Global in-memory issuance limiter. Issuance already requires a valid pairing
 * cookie, so this is not an anti-brute-force control — it just caps runaway
 * token creation (a buggy client, or a paired-but-hostile device spamming the
 * store) at 10/minute for the whole appliance. Single-process `next start`, so
 * one global bucket is enough; it resets on restart, which is fine.
 */
export function isIssueRateLimited(now = Date.now()): boolean {
  const e = G.__puffDeviceTokenIssues;
  if (!e) return false;
  if (now - e.windowStart >= ISSUE_WINDOW_MS) return false;
  return e.count >= ISSUE_MAX;
}

function recordIssue(now = Date.now()): void {
  const e = G.__puffDeviceTokenIssues;
  if (!e || now - e.windowStart >= ISSUE_WINDOW_MS) {
    G.__puffDeviceTokenIssues = { count: 1, windowStart: now };
    return;
  }
  e.count += 1;
}

/* ── label sanitisation ────────────────────────────────────────────── */

/** Strip control chars, collapse whitespace, cap length. Never trusted input. */
function sanitizeLabel(input: unknown): string {
  if (typeof input !== "string") return "";
  return input
    .replace(/[\x00-\x1f\x7f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 64);
}

/* ── public API ────────────────────────────────────────────────────── */

/** Metadata shape returned to the UI — never includes the raw token or hash. */
export interface DeviceTokenMeta {
  id: string;
  label: string;
  scope: DeviceTokenScope;
  createdAt: string;
  expiresAt: string;
  lastUsedAt: string | null;
  expired: boolean;
}

function toMeta(r: DeviceTokenRecord, now: number): DeviceTokenMeta {
  return {
    id: r.id,
    label: r.label,
    scope: r.scope,
    createdAt: r.createdAt,
    expiresAt: r.expiresAt,
    lastUsedAt: r.lastUsedAt ?? null,
    expired: Date.parse(r.expiresAt) <= now,
  };
}

/**
 * Mint a new upload-scoped token. CALLER MUST HAVE ALREADY VERIFIED PAIRING —
 * this function does not check the cookie; the issuing route does, before
 * calling. Returns the raw token ONCE (show it, then it is unrecoverable) plus
 * the stored metadata.
 */
export function issueDeviceToken(
  label: unknown,
  now = Date.now(),
): { raw: string; meta: DeviceTokenMeta } {
  const body = crypto.randomBytes(TOKEN_SECRET_BYTES).toString("base64url");
  const raw = `${TOKEN_PREFIX}${body}`;
  const record: DeviceTokenRecord = {
    id: crypto.randomBytes(9).toString("base64url"), // 12 url-safe chars
    label: sanitizeLabel(label),
    tokenHash: sha256Hex(raw),
    scope: "upload",
    createdAt: new Date(now).toISOString(),
    expiresAt: new Date(now + TOKEN_TTL_SEC * 1000).toISOString(),
    lastUsedAt: null,
  };

  const state = readFile();
  state.tokens.push(record);
  writeFile(state);
  recordIssue(now);

  return { raw, meta: toMeta(record, now) };
}

/** Metadata for every stored token. Never returns raw tokens or hashes. */
export function listDeviceTokens(now = Date.now()): DeviceTokenMeta[] {
  return readFile().tokens.map((r) => toMeta(r, now));
}

/** Revoke by id. Returns true if a record was removed. */
export function revokeDeviceToken(id: string): boolean {
  if (typeof id !== "string" || !id) return false;
  const state = readFile();
  const before = state.tokens.length;
  state.tokens = state.tokens.filter((r) => r.id !== id);
  if (state.tokens.length === before) return false;
  writeFile(state);
  return true;
}

/**
 * Verify a raw token for a specific method+path. Returns true ONLY when:
 *   1. the token is well-formed,
 *   2. its hash matches a stored record (constant-time compare),
 *   3. the record is not expired, AND
 *   4. the record's scope authorises this exact method+path.
 *
 * On success, best-effort stamps lastUsedAt (failure to write is swallowed —
 * an unwritable state file must not turn a valid upload into a 401). Any error
 * anywhere fails closed (returns false).
 */
export function verifyDeviceToken(
  raw: string,
  ctx: { method?: string; path?: string },
  now = Date.now(),
): boolean {
  try {
    if (!isWellFormedToken(raw)) return false;
    const candidateHash = sha256Hex(raw);

    const state = readFile();
    let matched: DeviceTokenRecord | null = null;
    // Compare against every record (no early return) so a match doesn't leak
    // its position by timing. The candidate hash is attacker-unknown regardless.
    for (const r of state.tokens) {
      if (timingSafeEqualHex(candidateHash, r.tokenHash)) matched = r;
    }
    if (!matched) return false;

    if (Date.parse(matched.expiresAt) <= now) return false;
    if (!scopeAllows(matched.scope, ctx.method, ctx.path)) return false;

    // Best-effort last-used stamp. Swallow all errors.
    try {
      matched.lastUsedAt = new Date(now).toISOString();
      writeFile(state);
    } catch {
      /* unwritable state must not fail a valid request */
    }
    return true;
  } catch {
    return false;
  }
}

/* ── gate integration helper ───────────────────────────────────────── */

/** Minimal request shape shared by NextRequest and Request in both callers. */
interface TokenCarrier {
  headers: Headers;
  method?: string;
  nextUrl?: { pathname: string };
  url?: string;
}

function readBearer(req: TokenCarrier): string | undefined {
  // Preferred: Authorization: Bearer pstk_…  (what the Shortcut sends)
  const auth = req.headers.get("authorization");
  if (auth) {
    const m = /^Bearer\s+(\S+)$/i.exec(auth.trim());
    if (m) return m[1];
  }
  // Alternative header, for clients that reserve Authorization.
  const alt = req.headers.get("x-puff-device-token");
  if (alt) return alt.trim();
  return undefined;
}

function pathnameOf(req: TokenCarrier): string | undefined {
  if (req.nextUrl?.pathname) return req.nextUrl.pathname;
  if (req.url) {
    try {
      return new URL(req.url).pathname;
    } catch {
      return undefined;
    }
  }
  return undefined;
}

/**
 * Does this request carry a valid device token that authorises it? Returns true
 * ONLY for the upload POST with a valid, unexpired, in-scope token. Everywhere
 * else — every other route, every other method, a missing/garbage token — this
 * returns false and the cookie remains the sole way through the gate.
 *
 * Wired as an ALTERNATIVE to isPaired() at both enforcement layers:
 *
 *   // middleware.ts and the /api/pai/[...path] proxy:
 *   if (gateRequires(req) && !isPaired(req) && !deviceTokenAllows(req)) 401
 *
 * Because verifyDeviceToken re-checks scope against the request's own
 * method+path, this can NEVER let a token satisfy the gate for HA control,
 * cameras, or knowledge reads — only the upload it was scoped for.
 */
export function deviceTokenAllows(req: TokenCarrier): boolean {
  try {
    const raw = readBearer(req);
    if (!raw) return false;
    return verifyDeviceToken(raw, {
      method: req.method,
      path: pathnameOf(req),
    });
  } catch {
    return false;
  }
}
