/**
 * Household device pairing — server-side only.
 *
 * WHY THIS EXISTS
 * ---------------
 * The dashboard's API routes are server-side proxies that attach the backend
 * credential (PUFFPAI_API_KEY) to whatever they forward. Before this module,
 * *any* device on the LAN could POST /api/pai/ha/service/... and actuate the
 * house with the appliance's own key — no client credential required at all.
 *
 * This adds a household pairing code: a device proves it belongs to the
 * household once (by typing an 8-character code shown on the appliance), and
 * then carries a signed, HttpOnly cookie. Unpaired LAN clients get 401.
 *
 * THREAT MODEL (be honest about it):
 *  - Protects against: any other device/person on the LAN (guest wifi, IoT
 *    gadget, neighbour on the wifi, a compromised laptop) driving the house or
 *    reading household knowledge through the dashboard's keyed proxies.
 *  - Does NOT protect against: an on-path attacker on the LAN. The appliance
 *    serves plain HTTP, so the cookie and the code are visible to anyone who
 *    can sniff or ARP-spoof the local network. TLS is the fix and is not
 *    deployed yet.
 *  - Does NOT protect the backend itself (:8000). Anything that can reach
 *    :8000 with the API key directly is out of scope here.
 *
 * DEVICE IDENTITY (v2 cookies)
 * ----------------------------
 * The original cookie (`v1.<expiry>.<hmac>`) carries no identity: every paired
 * device holds a byte-identical value. That made two things impossible:
 *   - revoking ONE device (the only lever was deleting the state file, which
 *     mints a new secret and drops the wall kiosk and every phone at once), and
 *   - telling the household who is actually paired.
 * So `v2.<deviceId>.<issued>.<expiry>.<hmac>` adds a per-device id, signed with
 * the SAME secret, and a registry file (devices.json) listing the ids that are
 * still allowed. Revocation = remove the row; the cookie stops verifying on the
 * very next request, with no restart and no secret change.
 *
 * WHY v1 KEEPS WORKING, INDEFINITELY
 * ----------------------------------
 * The live wall kiosk and the household's phones/laptops are carrying v1
 * cookies right now, and PUFF_PAIRING_ENFORCE=all is on. A cutoff — even a
 * generous one — would eventually 401 the always-on wall display with nobody
 * standing in front of it to retype a code. v1 therefore stays a first-class,
 * unexpiring cookie format in this release. Middleware silently upgrades each
 * v1 holder to v2 on its next request, so the population drains to zero on its
 * own without a single re-pair.
 *
 * The cost, stated plainly: a device still holding a v1 cookie cannot be
 * revoked from the Settings screen, because a v1 cookie proves only "I once
 * knew the code", not "I am device X". In practice that window is one request
 * wide (the upgrade fires on any matched route, including a plain /dashboard
 * page load). It is not zero, and it is the price of not dropping the kiosk.
 *
 * FAIL-CLOSED ASYMMETRY (deliberate)
 * ----------------------------------
 * A missing or corrupt devices.json makes every v2 cookie fail — no registry,
 * no membership, no entry. It must NOT affect v1, which is verified purely from
 * the HMAC secret and never consults the registry. That asymmetry is the whole
 * point: the registry can only ever REMOVE authority, never grant it, and a
 * registry problem can never take out the devices it does not describe.
 * devices.json therefore joins pairing.json on the never-delete list — deleting
 * it silently un-pairs every v2 device.
 *
 * Node runtime only — uses node:crypto and node:fs.
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

/* ── constants ─────────────────────────────────────────────────────── */

/** Cookie name the UI can look for (it cannot read the value — HttpOnly). */
export const PAIRING_COOKIE = "puff_device";

const COOKIE_VERSION = "v1";
const COOKIE_VERSION_V2 = "v2";
/** 365 days. */
export const COOKIE_MAX_AGE_SEC = 365 * 24 * 60 * 60;

/**
 * Device id: 12 base64url characters (9 random bytes → 72 bits).
 *
 * The charset is exactly [A-Za-z0-9_-] — note the absence of ".". The cookie is
 * dot-delimited, so a "." inside an id would let one field masquerade as
 * several and change how the value re-parses. Every place an id is accepted
 * from outside (cookie, request body) tests it against this regex BEFORE the id
 * is used for anything, and issueDeviceCookieValueV2 refuses to sign one that
 * fails. Anchored ^…$ with a fixed length: no prefix, suffix, or newline games
 * (JS "$" would otherwise match before a trailing \n — the {12} length bound
 * plus the anchors close that).
 */
export const DEVICE_ID_RE = /^[A-Za-z0-9_-]{12}$/;
const DEVICE_ID_BYTES = 9;

/**
 * Re-issue a v2 cookie once it is older than this. 30 days.
 *
 * WHY SLIDING RENEWAL: the cookie's 365-day life is a compromise between "the
 * wall display must not log itself out" and "a captured cookie should not be
 * good forever". Renewal lets a device that is actually in daily use keep a
 * fresh cookie without anyone retyping a code, while a cookie sniffed off the
 * LAN and squirreled away still dies on its original 365-day clock — the
 * attacker cannot renew it without also being registered, because renewal only
 * happens for cookies that pass full v2 verification, registry membership
 * included.
 */
export const RENEW_AFTER_SEC = 30 * 24 * 60 * 60;

/**
 * At most one lastSeenAt write per device per hour.
 *
 * WHY THROTTLED: this stamp is taken on the hot path — every gated request AND
 * every /dashboard page load runs a cookie verification. An unthrottled stamp
 * would mean a read-modify-write of devices.json per request; the kiosk alone
 * fires dozens in parallel on a page load, so the registry would be rewritten
 * continuously, burning the Orin's eMMC and widening the window for a torn
 * read. "Last seen" is a human-facing nicety with hour-level usefulness, so it
 * gets hour-level precision.
 */
const LAST_SEEN_THROTTLE_MS = 60 * 60 * 1000;

/**
 * Human-typeable alphabet: digits + uppercase, minus confusables 0/O/1/I.
 * 8 digits (2-9) + 24 letters = 32 symbols → 32^8 = 2^40 codes.
 */
const CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ";
const CODE_LENGTH = 8;
const CODE_GROUP = 4;

const SECRET_BYTES = 32;

/* ── state file ────────────────────────────────────────────────────── */

export interface PairingState {
  version: 1;
  /** 32-byte hex; HMAC-SHA256 key for device cookies. Never leaves the server. */
  secret: string;
  /** Current pairing code, unformatted (no dashes), uppercase. */
  code: string;
  createdAt: string;
  codeRotatedAt: string;
}

function statePath(): string {
  return (
    process.env.PUFF_PAIRING_STATE ||
    path.join(process.cwd(), "data", "pairing.json")
  );
}

// Cached on globalThis so Next's dev HMR / multiple route bundles share one
// copy instead of racing on the file.
const G = globalThis as unknown as {
  __puffPairingState?: PairingState;
  __puffPairingStatePath?: string;
  __puffPairFails?: Map<string, { count: number; windowStart: number }>;
  /** deviceId → epoch ms of the last lastSeenAt write. Throttles that write. */
  __puffDeviceSeen?: Map<string, number>;
  /** v1 cookie value → deviceId minted for it. Dedups concurrent upgrades. */
  __puffV1Upgrades?: Map<string, string>;
};

function randomCode(): string {
  // Rejection-free because 256 % 32 === 0 — every byte maps uniformly.
  const bytes = crypto.randomBytes(CODE_LENGTH);
  let out = "";
  for (let i = 0; i < CODE_LENGTH; i++) {
    out += CODE_ALPHABET[bytes[i] % CODE_ALPHABET.length];
  }
  return out;
}

function freshState(): PairingState {
  const now = new Date().toISOString();
  return {
    version: 1,
    secret: crypto.randomBytes(SECRET_BYTES).toString("hex"),
    code: randomCode(),
    createdAt: now,
    codeRotatedAt: now,
  };
}

function isValidState(v: unknown): v is PairingState {
  if (!v || typeof v !== "object") return false;
  const s = v as Partial<PairingState>;
  return (
    typeof s.secret === "string" &&
    /^[0-9a-f]{64}$/.test(s.secret) &&
    typeof s.code === "string" &&
    s.code.length === CODE_LENGTH
  );
}

function writeState(file: string, state: PairingState): void {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  // Write to a temp file then rename so a crash can't leave a truncated state.
  // mode 0600: the secret is the only thing standing between a LAN device and
  // the household's API key. (No-op on Windows dev boxes; real on the Orin.)
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(state, null, 2), { mode: 0o600 });
  try {
    fs.chmodSync(tmp, 0o600);
  } catch {
    /* Windows: chmod is a no-op / may throw. Not fatal. */
  }
  fs.renameSync(tmp, file);
}

/**
 * Read the state file.
 *
 * Returns null ONLY when the file genuinely does not exist (ENOENT) — the
 * first-boot case, where the caller may mint a fresh secret. Every other
 * problem (EACCES, EIO, a half-written file, JSON that parses but fails
 * isValidState) THROWS.
 *
 * WHY THROWING BEATS SELF-HEALING HERE
 * ------------------------------------
 * The obvious-looking alternative — "unreadable? generate a new state" — is a
 * silent mass-revocation: a new secret invalidates the wall kiosk's cookie and
 * every phone in the house at once, with no log line saying so. A permission
 * blip or a data volume that came up late would spend the household's entire
 * pairing state. Throwing instead surfaces as 401s (isPaired catches and
 * returns false) and a 500 on the pairing routes: loud, recoverable, and
 * non-destructive. The secret is only ever created when there is provably
 * nothing to destroy.
 */
function loadState(file: string): PairingState | null {
  let raw: string;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return null;
    throw err;
  }
  const parsed: unknown = JSON.parse(raw);
  if (!isValidState(parsed)) {
    throw new Error(`pairing state at ${file} is present but malformed`);
  }
  return parsed;
}

/**
 * Load pairing state, generating and persisting it on first use.
 *
 * Read FRESH from disk on every call (the file is a few hundred bytes and the
 * write path is atomic), for the same reason deviceToken.ts re-reads its
 * registry: an in-memory copy that outlives a rotation is a correctness bug.
 * Concretely, `scripts/show-pairing-code.js` can now rotate an expired code
 * from the operator's terminal — if this function served a cached copy, the
 * server would then reject the very code it just printed.
 *
 * The globalThis copy is kept only as a DISASTER FALLBACK: if the file becomes
 * unreadable while the process is running (unmounted volume, permission blip),
 * serve the last known-good state rather than throwing and 401ing the whole
 * house. It is never used to shadow a readable file.
 *
 * Fails closed: if the state cannot be read or persisted the error propagates,
 * and callers turn that into a 401/500 rather than silently running
 * unauthenticated — or, worse, minting a fresh secret (see loadState).
 */
export function getPairingState(): PairingState {
  const file = statePath();
  const cached =
    G.__puffPairingStatePath === file ? G.__puffPairingState : undefined;

  let state: PairingState | null;
  try {
    state = loadState(file);
  } catch (err) {
    if (cached) return cached;
    throw err;
  }

  if (!state) {
    // ENOENT. If we have served this path before, the file was deleted out from
    // under us — that is the operator's documented nuclear revoke, and it takes
    // effect on the next restart. Do not fight it by rewriting the file, and do
    // not mint a new secret mid-flight either; keep serving what we had.
    if (cached) return cached;
    state = freshState();
    writeState(file, state);
  }

  G.__puffPairingState = state;
  G.__puffPairingStatePath = file;
  return state;
}

/* ── paired-device registry ────────────────────────────────────────── */

/**
 * One row per device that may present a v2 cookie. This file is the revocation
 * list: nothing here grants authority (the HMAC does that), it only withholds
 * it. Remove a row and that device's cookie stops verifying on the next
 * request — no restart, no secret change, no other device affected.
 *
 * `id` is public: it is shown to paired callers by GET /api/pair/devices so the
 * Settings screen can rename and revoke by it. It is NOT a credential; knowing
 * an id gets you nothing without a cookie signed over it.
 */
export interface PairedDevice {
  id: string;
  /** Human label. Sanitised, may be empty. Never trusted. */
  name: string;
  createdAt: string;
  /** Best-effort, hour-granular. null until the device is seen again. */
  lastSeenAt: string | null;
}

interface PairedDeviceFile {
  version: 1;
  devices: PairedDevice[];
}

/**
 * Where the registry lives. A SIBLING of the pairing state, for the same reason
 * device_tokens.json is: one 0700 directory, one backup story, one thing to
 * remember not to delete. Same derivation as deviceToken.ts statePath():
 *   - PUFF_PAIRED_DEVICES  explicit override, or
 *   - <dir of PUFF_PAIRING_STATE>/devices.json, or
 *   - <cwd>/data/devices.json   (matches pairing.json's default)
 */
function devicesPath(): string {
  if (process.env.PUFF_PAIRED_DEVICES) return process.env.PUFF_PAIRED_DEVICES;
  const pairing = process.env.PUFF_PAIRING_STATE;
  if (pairing) return path.join(path.dirname(pairing), "devices.json");
  return path.join(process.cwd(), "data", "devices.json");
}

function isPairedDeviceRecord(v: unknown): v is PairedDevice {
  if (!v || typeof v !== "object") return false;
  const r = v as Partial<PairedDevice>;
  return (
    typeof r.id === "string" &&
    DEVICE_ID_RE.test(r.id) &&
    typeof r.name === "string" &&
    typeof r.createdAt === "string"
  );
}

/**
 * Read the registry for an AUTHORISATION decision. Missing, unreadable or
 * corrupt → an EMPTY list, which makes every v2 cookie fail membership. That is
 * the fail-closed direction: a registry we cannot read authorises nobody. It
 * cannot touch v1 cookies, which never come through here.
 *
 * ONLY for read-only authorisation (findPairedDevice → cookie verification).
 * Anything that writes must use readPairedDevicesForWrite() instead — see the
 * long note there for why collapsing a read error to [] is safe on this path
 * and destroys the household's device list on that one.
 *
 * Records that fail validation are dropped individually rather than poisoning
 * the whole file — same discipline as deviceToken.ts. Read fresh every time
 * (the file is tiny, the write path is atomic) so a revocation takes effect on
 * the very next request instead of whenever a cache happened to expire.
 */
function readPairedDevices(): PairedDeviceFile {
  try {
    return readPairedDevicesForWrite();
  } catch {
    /* missing or unreadable → empty, which authorises nobody */
  }
  return { version: 1, devices: [] };
}

/**
 * Read the registry for a READ-MODIFY-WRITE. Same parse, opposite error policy:
 * this one THROWS rather than pretending the registry is empty.
 *
 * WHY THE TWO POLICIES MUST DIFFER (this is a real bug that got shipped once)
 * --------------------------------------------------------------------------
 * readPairedDevices() collapsing every failure to [] is exactly right when the
 * question is "may this cookie in?" — an unreadable registry authorises nobody.
 * It is catastrophic when the question is "what should I write back?", because
 * the answer becomes "just the one row I am adding" and the write ATOMICALLY
 * REPLACES every other device with it. One transient read failure (EMFILE under
 * load, a slow mount, an EIO blip) during a pair — or during the v1→v2 upgrade
 * that fires on the kiosk's next page load — would silently un-pair the entire
 * household, including the wall display that cannot re-pair itself.
 *
 * So: ENOENT means the registry genuinely does not exist yet (first device ever)
 * and starting from an empty list is correct and lossless. EVERY other outcome —
 * EACCES, EMFILE, EIO, EISDIR, JSON that will not parse, a shape without a
 * devices array — means there may be rows we cannot see, and the only safe move
 * is to abort the mutation and write nothing. Callers turn the throw into "no
 * upgrade this request" (renewalCookieFor, already wrapped) or a 500
 * (POST /api/pair, rename, revoke), never into data loss.
 *
 * This is the same rule loadState() applies to pairing.json's secret, for the
 * same reason: never destroy credentials to recover from a read error.
 *
 * Individually invalid RECORDS are still filtered out rather than throwing —
 * a row that fails isPairedDeviceRecord can never match a DEVICE_ID_RE cookie,
 * so it is already inert, and dropping it is the documented discipline. Only a
 * file we cannot read or parse AT ALL aborts the write.
 */
function readPairedDevicesForWrite(): PairedDeviceFile {
  const file = devicesPath();
  let raw: string;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException)?.code === "ENOENT") {
      return { version: 1, devices: [] };
    }
    throw err;
  }
  const parsed: unknown = JSON.parse(raw);
  if (
    !parsed ||
    typeof parsed !== "object" ||
    !Array.isArray((parsed as PairedDeviceFile).devices)
  ) {
    throw new Error(`paired-device registry at ${file} is present but malformed`);
  }
  return {
    version: 1,
    devices: (parsed as PairedDeviceFile).devices.filter(isPairedDeviceRecord),
  };
}

/** Atomic 0600 write (temp + rename), same discipline as pairing.json. */
function writePairedDevices(state: PairedDeviceFile): void {
  const file = devicesPath();
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

/** Strip control chars, collapse whitespace, cap length. Never trusted input. */
function sanitizeDeviceName(input: unknown): string {
  if (typeof input !== "string") return "";
  return input
    .replace(/[\x00-\x1f\x7f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 64);
}

/**
 * Every registered device. Safe to hand to a paired caller as-is.
 *
 * Uses the STRICT read deliberately, even though this only feeds a display.
 * A missing registry (ENOENT) still yields an honest empty list, but a registry
 * we cannot read or parse raises, and the route turns that into a 500 rather
 * than rendering "No devices listed yet" — which would tell the household their
 * devices had been wiped when in fact the file was merely unreadable.
 */
export function listPairedDevices(): PairedDevice[] {
  return readPairedDevicesForWrite().devices;
}

/** The record for `id`, or null. Shape-checks the id before looking. */
export function findPairedDevice(id: string): PairedDevice | null {
  if (typeof id !== "string" || !DEVICE_ID_RE.test(id)) return null;
  for (const d of readPairedDevices().devices) {
    if (d.id === id) return d;
  }
  return null;
}

/**
 * Register a new device and return its record. Throws if the registry cannot be
 * written — the caller must NOT hand out a v2 cookie it failed to register,
 * because that cookie would fail membership on its very next use and the user
 * would be left "paired" but 401ing.
 */
export function addPairedDevice(name: unknown, now = Date.now()): PairedDevice {
  const record: PairedDevice = {
    id: crypto.randomBytes(DEVICE_ID_BYTES).toString("base64url"),
    name: sanitizeDeviceName(name),
    createdAt: new Date(now).toISOString(),
    lastSeenAt: new Date(now).toISOString(),
  };
  // STRICT read: if the existing rows cannot be read, abort rather than write a
  // one-row file over them. See readPairedDevicesForWrite.
  const state = readPairedDevicesForWrite();
  state.devices.push(record);
  writePairedDevices(state);
  // Seed the throttle so the request that just paired doesn't immediately
  // rewrite the file it created.
  (G.__puffDeviceSeen ??= new Map()).set(record.id, now);
  return record;
}

/**
 * Rename by id. Returns false if the id is unknown or malformed; THROWS if the
 * registry cannot be read.
 *
 * The strict read is what makes "unknown id" mean unknown. With the lenient
 * read this looked safe — an unreadable registry parsed as [], nothing matched,
 * and it returned false before writing — but that safety was incidental, not
 * designed: it held only because the early return happened to come first. It
 * also reported a confident "not_found" for a device that exists. Now an
 * unreadable registry raises and the route answers 500, and no future edit that
 * moves work above the early return can turn this into a truncating write.
 */
export function renamePairedDevice(id: string, name: unknown): boolean {
  if (typeof id !== "string" || !DEVICE_ID_RE.test(id)) return false;
  const state = readPairedDevicesForWrite();
  const target = state.devices.find((d) => d.id === id);
  if (!target) return false;
  target.name = sanitizeDeviceName(name);
  writePairedDevices(state);
  return true;
}

/**
 * Revoke by id — removes the row, which kills that device's v2 cookie on its
 * next request. Returns false if the id is unknown or malformed.
 *
 * Errors are NOT swallowed: a revoke that silently failed to persist would be
 * the worst possible lie to tell the household, so the caller turns a throw
 * into a 500 rather than reporting success. That now includes an unreadable
 * registry — with the lenient read this returned a confident "not_found" for a
 * device that was really still there and still authorised, which is the same
 * lie wearing a different status code. (It never wrote a truncated list, since
 * an empty read means nothing matches and the length check bails first — but
 * that was luck, not design. The strict read makes it structural.)
 */
export function revokePairedDevice(id: string): boolean {
  if (typeof id !== "string" || !DEVICE_ID_RE.test(id)) return false;
  const state = readPairedDevicesForWrite();
  const before = state.devices.length;
  state.devices = state.devices.filter((d) => d.id !== id);
  if (state.devices.length === before) return false;
  writePairedDevices(state);
  G.__puffDeviceSeen?.delete(id);
  return true;
}

/**
 * Best-effort "last seen" stamp, throttled to one write per device per hour
 * (see LAST_SEEN_THROTTLE_MS). Every failure is swallowed: an unwritable
 * registry must never turn a valid cookie into a 401. The throttle map lives in
 * memory and resets on restart, which costs at most one extra write per device
 * per boot.
 */
function touchPairedDevice(id: string, now: number): void {
  try {
    const seen = (G.__puffDeviceSeen ??= new Map());
    const last = seen.get(id);
    if (last !== undefined && now - last < LAST_SEEN_THROTTLE_MS) return;
    // Claim the slot BEFORE writing. If the write throws we still hold off for
    // an hour rather than retrying the failing write on every single request.
    seen.set(id, now);

    // STRICT read: a stamp must never be the thing that rewrites the registry
    // from a failed read. A throw here is swallowed by the catch below, which
    // is exactly right — losing an hour of "last seen" precision is free,
    // losing the device list is not.
    const state = readPairedDevicesForWrite();
    const target = state.devices.find((d) => d.id === id);
    if (!target) return;
    target.lastSeenAt = new Date(now).toISOString();
    writePairedDevices(state);
  } catch {
    /* a stamp is a nicety; never let it affect authorisation */
  }
}

/* ── codes ─────────────────────────────────────────────────────────── */

/** "7XK4P2M9" → "7XK4-P2M9" for display. */
export function formatCode(code: string): string {
  const groups: string[] = [];
  for (let i = 0; i < code.length; i += CODE_GROUP) {
    groups.push(code.slice(i, i + CODE_GROUP));
  }
  return groups.join("-");
}

/**
 * Strip dashes/spaces/case. Characters outside the alphabet (including the
 * excluded confusables 0/O/1/I) are dropped rather than guessed at — dropping
 * them changes the length, so the code simply fails to verify. Guessing which
 * one the user meant would widen the accepted-code space.
 */
export function normalizeCode(input: string): string {
  const upper = String(input || "").toUpperCase();
  let out = "";
  for (let i = 0; i < upper.length && out.length < 64; i++) {
    if (CODE_ALPHABET.indexOf(upper[i]) >= 0) out += upper[i];
  }
  return out;
}

/* ── code time-to-live ─────────────────────────────────────────────── */

/**
 * How long a pairing code stays redeemable after it is rotated. One hour.
 *
 * WHY A TTL AT ALL
 * ----------------
 * Before this, a code was valid until somebody manually rotated it — in
 * practice, forever. It is displayed on an always-on wall screen and encoded in
 * a QR anyone in the room can photograph, so "forever" meant a house guest from
 * three months ago could still pair a device from the driveway. A code is a
 * bearer credential shown in public; it should behave like one.
 *
 * WHY ONE HOUR, AND WHY IT IS A CONSTANT
 * --------------------------------------
 * Pairing is a deliberate, in-the-room act: somebody walks up, scans, done.
 * An hour covers "I'll do it after dinner" without covering "next season". It
 * is deliberately NOT env-tunable — an operator-visible knob here is a knob an
 * operator can set to a year, which is exactly the state we are leaving.
 *
 * WHY NO NEW STATE FIELD
 * ----------------------
 * Expiry is derived from the existing `codeRotatedAt`, so pairing.json keeps
 * exactly the fields it has today and a rollback to the previous build still
 * passes isValidState(). A file with a missing or unparseable codeRotatedAt is
 * treated as already expired, which self-heals on the next display fetch.
 */
export const PAIR_CODE_TTL_SEC = 60 * 60;

/** Epoch ms at which the current code stops being redeemable. */
function codeExpiresAtMs(state: PairingState): number {
  const rotated = Date.parse(state.codeRotatedAt || "");
  // Unparseable/absent → already expired. Fail closed: an unreadable rotation
  // timestamp must never be read as "valid forever".
  if (!Number.isFinite(rotated)) return 0;
  return rotated + PAIR_CODE_TTL_SEC * 1000;
}

function isCodeExpired(state: PairingState, now: number): boolean {
  return now >= codeExpiresAtMs(state);
}

export interface PairingCodeInfo {
  /** Formatted for display, e.g. "7XK4-P2M9". */
  code: string;
  rotatedAt: string;
  /** ISO 8601. After this instant the code no longer redeems. */
  expiresAt: string;
}

/**
 * The code to put on a screen, plus when it dies.
 *
 * LAZILY ROTATES an expired code before returning it. That is what makes a TTL
 * survivable on an appliance with an always-on wall display: whatever is on
 * screen (or in the QR) is redeemable, because the act of fetching it for
 * display is what refreshes it. Nothing expires "at" the user.
 *
 * Note the asymmetry with verifyCode(), which does NOT rotate: only a paired
 * caller can reach a display path, so only a paired caller can cause a
 * rotation. A stranger guessing codes cannot spin the wheel.
 */
export function getPairingCodeInfo(now = Date.now()): PairingCodeInfo {
  let state = getPairingState();
  if (isCodeExpired(state, now)) {
    rotatePairingCode(now);
    state = getPairingState();
  }
  return {
    code: formatCode(state.code),
    rotatedAt: state.codeRotatedAt,
    expiresAt: new Date(codeExpiresAtMs(state)).toISOString(),
  };
}

/** Current code, formatted for display. Paired callers only. */
export function getDisplayCode(): string {
  return getPairingCodeInfo().code;
}

/**
 * Rotate the pairing code only. The HMAC secret is deliberately NOT rotated,
 * so devices that are already paired keep working — rotation revokes the
 * ability to pair *new* devices with the old code, nothing more.
 * (To revoke every device, delete the state file; a fresh secret invalidates
 * all cookies.)
 */
export function rotatePairingCode(now = Date.now()): string {
  const state = getPairingState();
  // Spread first so every existing field survives verbatim. pairing.json is
  // append-only by policy: a rollback to the previous build must still parse it.
  const next: PairingState = {
    ...state,
    code: randomCode(),
    codeRotatedAt: new Date(now).toISOString(),
  };
  writeState(statePath(), next);
  G.__puffPairingState = next;
  return formatCode(next.code);
}

/* ── constant-time helpers ─────────────────────────────────────────── */

function timingSafeEqualStr(a: string, b: string): boolean {
  const ab = Buffer.from(a, "utf8");
  const bb = Buffer.from(b, "utf8");
  if (ab.length !== bb.length) {
    // Compare against self so the length-mismatch path costs roughly the same
    // as a real comparison; the length itself is still observable, which is
    // fine — the code length is public.
    crypto.timingSafeEqual(ab, ab);
    return false;
  }
  return crypto.timingSafeEqual(ab, bb);
}

/**
 * Constant-time check of a user-supplied pairing code.
 *
 * An EXPIRED code is rejected exactly like a wrong one — and, critically, on
 * the same code path: the comparison runs unconditionally and the expiry check
 * is applied afterwards as a boolean veto, never as an early return. If expiry
 * short-circuited first, an attacker could time the difference and learn
 * "the code you're guessing is stale, come back after the next rotation",
 * which is a small but free hint about the appliance's state. The caller
 * (POST /api/pair) returns the same `invalid_code` for both, so nothing
 * downstream distinguishes them either.
 *
 * Deliberately does NOT rotate on expiry — see getPairingCodeInfo(). Letting an
 * unauthenticated guess trigger rotation would hand a stranger a lever on the
 * household's credential.
 */
export function verifyCode(input: string, now = Date.now()): boolean {
  const state = getPairingState();
  const candidate = normalizeCode(input);
  const expired = isCodeExpired(state, now);
  if (candidate.length !== CODE_LENGTH) {
    // Still do a comparison so a wrong-length guess isn't distinguishable by
    // a fast return path.
    timingSafeEqualStr(state.code, state.code.slice(0, -1) + "X");
    return false;
  }
  const matches = timingSafeEqualStr(candidate, state.code);
  return matches && !expired;
}

/* ── device cookie ─────────────────────────────────────────────────── */

function sign(payload: string, secret: string): string {
  return crypto.createHmac("sha256", Buffer.from(secret, "hex"))
    .update(payload)
    .digest("hex");
}

/**
 * LEGACY cookie value: `v1.<expiry-epoch-seconds>.<hmac-sha256-hex>`
 * where the HMAC covers the literal string `v1.<expiry-epoch-seconds>`.
 *
 * No longer issued to real devices — POST /api/pair mints v2 now — but kept
 * exported and exercised by the tests because v1 cookies remain valid
 * indefinitely and this function is the canonical definition of the format
 * they must keep verifying against.
 */
export function issueDeviceCookieValue(now = Date.now()): string {
  const state = getPairingState();
  const exp = Math.floor(now / 1000) + COOKIE_MAX_AGE_SEC;
  const payload = `${COOKIE_VERSION}.${exp}`;
  return `${payload}.${sign(payload, state.secret)}`;
}

/**
 * Cookie value: `v2.<deviceId>.<issued>.<expiry>.<hmac-sha256-hex>`, where the
 * HMAC covers the literal string `v2.<deviceId>.<issued>.<expiry>`.
 *
 * `issued` is signed, not just carried, because sliding renewal keys off it: an
 * unsigned issue time would let a device claim any age it liked, which is
 * harmless today (renewal only re-issues an already-valid cookie for a
 * still-registered device) but is exactly the kind of unsigned input that grows
 * teeth later.
 *
 * Uses the SAME secret as v1 — deliberately. A second secret would be a second
 * thing to lose, and it buys nothing: v1 and v2 are distinguished by their
 * signed version prefix, so a v1 mac can never be replayed as a v2 mac (the
 * payloads differ in both length and content).
 */
export function issueDeviceCookieValueV2(
  deviceId: string,
  now = Date.now(),
): string {
  // Refuse to sign anything that isn't a well-formed id. A deviceId with a "."
  // in it would split the cookie into extra fields and could be parsed back out
  // as a different (deviceId, issued, expiry) triple, so the shape check is
  // load-bearing, not cosmetic.
  if (!DEVICE_ID_RE.test(deviceId)) {
    throw new Error("refusing to sign a malformed device id");
  }
  const state = getPairingState();
  const issued = Math.floor(now / 1000);
  const exp = issued + COOKIE_MAX_AGE_SEC;
  const payload = `${COOKIE_VERSION_V2}.${deviceId}.${issued}.${exp}`;
  return `${payload}.${sign(payload, state.secret)}`;
}

/** What a verified cookie tells us about the device carrying it. */
export interface DeviceCookieIdentity {
  version: "v1" | "v2";
  /** null for v1 — a v1 cookie proves membership, never identity. */
  deviceId: string | null;
  /** Epoch seconds. null for v1 (the format has no issue time). */
  issued: number | null;
  /** Epoch seconds. */
  expiry: number;
}

/**
 * Verify a device cookie and return what it identifies, or null if it does not
 * verify. This is the single place both formats are checked.
 *
 * v1: structure → expiry → constant-time HMAC. The registry is NOT consulted
 *     (see the fail-closed asymmetry note at the top of this file): a broken or
 *     absent devices.json must never be able to 401 the wall kiosk.
 * v2: structure → deviceId shape → expiry → constant-time HMAC → registry
 *     membership. Membership last, so an unregistered id still costs a full
 *     HMAC verification and revocation is not detectable by timing.
 *
 * Side effect on a successful v2 verify: a throttled, best-effort lastSeenAt
 * stamp (see touchPairedDevice). Mirrors deviceToken.verifyDeviceToken, which
 * stamps lastUsedAt the same way.
 */
export function verifyDeviceCookieIdentity(
  value: string | undefined | null,
  now = Date.now(),
): DeviceCookieIdentity | null {
  if (!value) return null;
  const parts = value.split(".");

  if (parts.length === 3) {
    const [version, expStr, mac] = parts;
    if (version !== COOKIE_VERSION) return null;
    if (!/^\d{1,12}$/.test(expStr)) return null;
    if (!/^[0-9a-f]{64}$/.test(mac)) return null;

    const exp = parseInt(expStr, 10);
    if (!Number.isFinite(exp) || exp * 1000 <= now) return null;

    const state = getPairingState();
    const expected = sign(`${version}.${expStr}`, state.secret);
    if (!timingSafeEqualStr(mac, expected)) return null;
    return { version: "v1", deviceId: null, issued: null, expiry: exp };
  }

  if (parts.length === 5) {
    const [version, deviceId, issuedStr, expStr, mac] = parts;
    if (version !== COOKIE_VERSION_V2) return null;
    if (!DEVICE_ID_RE.test(deviceId)) return null;
    if (!/^\d{1,12}$/.test(issuedStr)) return null;
    if (!/^\d{1,12}$/.test(expStr)) return null;
    if (!/^[0-9a-f]{64}$/.test(mac)) return null;

    const issued = parseInt(issuedStr, 10);
    const exp = parseInt(expStr, 10);
    if (!Number.isFinite(issued) || !Number.isFinite(exp)) return null;
    if (exp * 1000 <= now) return null;

    const state = getPairingState();
    const expected = sign(
      `${version}.${deviceId}.${issuedStr}.${expStr}`,
      state.secret,
    );
    if (!timingSafeEqualStr(mac, expected)) return null;

    // Registry membership. A plain === is correct here: the id is not a secret
    // (the Settings screen shows it to any paired caller) and the HMAC above
    // has already proved the cookie is ours. Nothing to leak by comparing fast.
    if (!findPairedDevice(deviceId)) return null;

    touchPairedDevice(deviceId, now);
    return { version: "v2", deviceId, issued, expiry: exp };
  }

  return null;
}

/**
 * Verify a device cookie value: accepts BOTH v1 and v2. Unchanged signature —
 * every existing caller keeps working.
 */
export function verifyDeviceCookieValue(
  value: string | undefined | null,
  now = Date.now(),
): boolean {
  return verifyDeviceCookieIdentity(value, now) !== null;
}

/** Minimal shape shared by NextRequest and Request-with-cookies. */
export interface CookieCarrier {
  cookies?: { get(name: string): { value: string } | undefined };
  headers: Headers;
}

function readPairingCookie(req: CookieCarrier): string | undefined {
  const viaApi = req.cookies?.get(PAIRING_COOKIE)?.value;
  if (viaApi) return viaApi;
  // Fallback for plain Request objects (no NextRequest cookie jar).
  const raw = req.headers.get("cookie");
  if (!raw) return undefined;
  const parts = raw.split(";");
  for (let i = 0; i < parts.length; i++) {
    const eq = parts[i].indexOf("=");
    if (eq < 0) continue;
    if (parts[i].slice(0, eq).trim() === PAIRING_COOKIE) {
      return decodeURIComponent(parts[i].slice(eq + 1).trim());
    }
  }
  return undefined;
}

/** True if this request carries a valid, unexpired device cookie (v1 or v2). */
export function isPaired(req: CookieCarrier): boolean {
  try {
    return verifyDeviceCookieValue(readPairingCookie(req));
  } catch {
    // Unreadable state file, bad secret, anything: fail closed.
    return false;
  }
}

/**
 * The registered device id this request is carrying, or null.
 *
 * null means either "not paired" or "paired with a legacy v1 cookie" — those
 * two are indistinguishable to a caller, and deliberately so: a v1 cookie
 * genuinely does not identify a device. Callers that need to tell them apart
 * (the Settings screen wants to say "this device") pair this with isPaired().
 */
export function pairedDeviceIdFor(
  req: CookieCarrier,
  now = Date.now(),
): string | null {
  try {
    return verifyDeviceCookieIdentity(readPairingCookie(req), now)?.deviceId ?? null;
  } catch {
    return null;
  }
}

/* ── derived chat/vault session identity ───────────────────────────── */

/**
 * Label mixed into the HMAC so this value can never be confused with a cookie
 * MAC over the same secret. Bump the version if the derivation changes; every
 * device then lands in a fresh backend session bucket, which is harmless.
 */
const DEVICE_SESSION_LABEL = "puff-chat-session:v1:";

/**
 * The value the proxy asserts to the backend as `X-Puff-Device-Id`.
 *
 * The backend keys its 300-second vault-unlocked chat session on this — the
 * window in which protected questions are answered out of the DECRYPTED vault
 * with no further PIN (api_server._get_chat_client_id).
 *
 * WHY NOT THE deviceId ITSELF: any paired caller can read every registered
 * deviceId out of GET /api/pair/devices. If the raw id ever leaked past the
 * proxy's header policy — a future route that relays client headers, a reverse
 * proxy bolted in front of the dashboard — a household member could name
 * someone else's session. An HMAC under the pairing secret is not derivable
 * from the id, so knowing the id buys nothing.
 *
 * WHY NOT THE COOKIE: the cookie is the credential. It must not be copied into
 * an upstream header where it can land in a backend log.
 *
 * Stable for the life of the device row: same deviceId -> same value across
 * requests, restarts and cookie renewals (issued/expiry are deliberately NOT
 * mixed in, or a renewal would silently reset the unlock window). Revoking the
 * device invalidates the cookie, so the value simply becomes unreachable.
 *
 * 32 hex chars = 128 bits: collision-free for any household, and inside the
 * backend's `^[A-Za-z0-9_-]{8,64}$` shape check.
 */
export function deviceSessionId(deviceId: string): string {
  if (typeof deviceId !== "string" || !DEVICE_ID_RE.test(deviceId)) {
    throw new Error("deviceSessionId: malformed deviceId");
  }
  const state = getPairingState();
  return sign(`${DEVICE_SESSION_LABEL}${deviceId}`, state.secret).slice(0, 32);
}

/**
 * The derived session id for the VERIFIED pairing cookie on this request, or
 * null when there is no valid v2 cookie (unpaired, bad MAC, revoked device, or
 * a legacy v1 cookie, which identifies no device).
 *
 * null means "assert nothing": the proxy must send no identity header at all,
 * and the backend then keys on its own socket peer. A placeholder would be a
 * SHARED session, which is the bug this whole path exists to close.
 */
export function deviceSessionIdFor(
  req: CookieCarrier,
  now = Date.now(),
): string | null {
  try {
    const id = pairedDeviceIdFor(req, now);
    return id ? deviceSessionId(id) : null;
  } catch {
    return null;
  }
}

/* ── sliding renewal + v1→v2 upgrade ───────────────────────────────── */

/**
 * Pure decision: is a v2 cookie issued at `issued` (epoch seconds) old enough
 * to re-issue? Extracted so the rule is testable without a request, a cookie,
 * or a state file.
 *
 * A cookie issued in the FUTURE (clock skew, or a device that was handed one
 * before an NTP correction) yields a negative age and is not renewed — renewing
 * on a nonsense timestamp would just mint another nonsense timestamp.
 */
export function shouldRenewDeviceCookie(
  issued: number,
  now = Date.now(),
): boolean {
  if (!Number.isFinite(issued)) return false;
  const ageSec = Math.floor(now / 1000) - issued;
  return ageSec >= RENEW_AFTER_SEC;
}

/**
 * Cap on the v1→v2 dedup map. A v1 cookie must pass full HMAC verification
 * before it can reach this map, so only cookies the appliance itself issued can
 * ever populate it — a handful, and no new ones are minted. The cap is pure
 * belt-and-braces against an unbounded map living forever in a long-running
 * process.
 */
const V1_UPGRADE_MAP_CAP = 64;

/**
 * Perform the v1 → v2 upgrade for an ALREADY-VERIFIED v1 cookie value, and
 * return the deviceId it now maps to plus the v2 cookie value to send. Null
 * means "do not re-issue": the row this cookie was already upgraded to has
 * since been revoked, and we do not resurrect it.
 *
 * Extracted so the two callers share one dedup map. That sharing is the whole
 * point — renewalCookieFor() runs from middleware on page loads, and
 * proxyDeviceSessionFor() runs on the keyed proxy path; if each kept its own map
 * the same v1 kiosk cookie would mint two "Migrated device" rows, and the two
 * halves of one page load would derive two different chat identities.
 *
 * The caller MUST have verified the cookie first. This function trusts `raw`
 * only as a map key, but it mints a registry row for it, so handing it an
 * unverified value would let an unpaired caller grow the registry.
 */
function upgradeV1Cookie(
  raw: string,
  now: number,
): { deviceId: string; value: string } | null {
  const upgrades = (G.__puffV1Upgrades ??= new Map());
  const known = upgrades.get(raw);
  if (known !== undefined) {
    // Already minted for this exact cookie. Only re-issue if the row still
    // exists; a revoked row stays revoked.
    if (!findPairedDevice(known)) return null;
    return { deviceId: known, value: issueDeviceCookieValueV2(known, now) };
  }

  if (upgrades.size >= V1_UPGRADE_MAP_CAP) upgrades.clear();

  const record = addPairedDevice("Migrated device", now);
  upgrades.set(raw, record.id);
  return { deviceId: record.id, value: issueDeviceCookieValueV2(record.id, now) };
}

/**
 * Decide whether this request's cookie should be replaced, and with what.
 * Returns cookie options to Set-Cookie, or null for "leave it alone".
 *
 * TWO JOBS
 * --------
 * 1. SLIDING RENEWAL. A valid v2 cookie older than RENEW_AFTER_SEC is re-issued
 *    with the same deviceId and a fresh 365-day window. Only reachable for a
 *    device that is still in the registry, because that membership check is
 *    part of v2 verification — a revoked device cannot renew its way back in.
 *
 * 2. v1 → v2 UPGRADE. A valid v1 cookie gets a registry row ("Migrated device")
 *    and a v2 cookie. This is how the existing population — the wall kiosk and
 *    the household's phones — becomes individually revocable without anybody
 *    retyping a code.
 *
 * WHY THE DEDUP MAP
 * -----------------
 * A kiosk page load fires many requests in parallel, all still carrying the
 * same v1 cookie (none of them has seen the Set-Cookie yet). Without dedup each
 * one would mint its own registry row and the household would open Settings to
 * a list of a dozen identical "Migrated device" entries. Keyed on the full v1
 * cookie VALUE, which is the only identity a v1 cookie has.
 *
 * Two genuinely different devices that happen to hold a byte-identical v1
 * cookie therefore merge into one registry row. That is acceptable and in fact
 * honest: they were cryptographically indistinguishable to begin with, and
 * pretending otherwise would be inventing an identity the credential never had.
 *
 * A mapping whose deviceId is no longer in the registry is NOT re-minted — the
 * household revoked that row and we do not resurrect it. (The device keeps
 * working off its v1 cookie regardless; see the v1 caveat at the top of the
 * file. The tombstone is in-memory only, so a restart can let a still-present
 * v1 holder reappear as a new row. Cosmetic, not a privilege change: the access
 * was never actually revoked while the v1 cookie lived.)
 *
 * NEVER THROWS. Every path is wrapped, and any failure degrades to "no renewal
 * on this request" — the request itself has already passed the gate and must
 * not be turned into a 500 by an optional housekeeping step.
 */
export function renewalCookieFor(
  req: CookieCarrier,
  now = Date.now(),
): DeviceCookieOptions | null {
  try {
    const raw = readPairingCookie(req);
    if (!raw) return null;

    const identity = verifyDeviceCookieIdentity(raw, now);
    // Not a valid cookie → this request is either unpaired or authorised some
    // other way (a scoped device token). Never Set-Cookie on that path.
    if (!identity) return null;

    if (identity.version === "v2") {
      if (identity.deviceId === null) return null;
      if (identity.issued === null) return null;
      if (!shouldRenewDeviceCookie(identity.issued, now)) return null;
      return deviceCookieOptions(
        issueDeviceCookieValueV2(identity.deviceId, now),
      );
    }

    // v1 → upgrade.
    const upgraded = upgradeV1Cookie(raw, now);
    if (!upgraded) return null;
    return deviceCookieOptions(upgraded.value);
  } catch {
    // Unwritable registry, unreadable secret, anything at all: skip renewal.
    // The cookie the request already carries stays valid, so nothing breaks.
    return null;
  }
}

/**
 * What the keyed proxy should tell the backend about this caller, and whether
 * the caller's cookie must be replaced to make that statement true.
 */
export interface ProxyDeviceSession {
  /** Value for X-Puff-Device-Id, or null for "assert no identity". */
  readonly sessionId: string | null;
  /** Cookie to Set-Cookie on this response, or null for "leave it alone". */
  readonly setCookie: DeviceCookieOptions | null;
}

/**
 * The proxy's version of deviceSessionIdFor(): same answer for a v2 device, but
 * it UPGRADES a legacy v1 cookie in place first, so the identity exists on THIS
 * request instead of on the one after it.
 *
 * WHY EAGER, NOT LAZY
 * -------------------
 * The v1 → v2 upgrade already happened lazily in middleware: the request was
 * served with its v1 cookie and the v2 replacement rode back on the response, so
 * the FIRST proxied call after an upgrade still derived no identity. For a page
 * load that is invisible. For the chat/vault surface it is not: that first call
 * is precisely the one that would have landed in the shared identity-less bucket
 * — and on the kiosk, which reloads rarely and re-pairs never, "the next
 * request" could be a long time coming. Doing the upgrade here means a v1 device
 * has a private vault-chat identity from its very first proxied call.
 *
 * A valid v1 cookie keeps working everywhere it worked before: this is an
 * upgrade, never a refusal. Non-vault surfaces never even consult the result.
 *
 * Sliding renewal is honoured for v2 as well, so a proxy-only client (a phone
 * that lives on the Talk page and never loads a gated page) still gets its
 * cookie refreshed.
 *
 * IDENTITY COMES FROM THE COOKIE AND NOTHING ELSE. Not from the path, not from
 * the screen: a kiosk/display hint (?puffMode=kiosk) changes what a surface
 * renders and never who the backend thinks is asking. An unpaired wall display
 * is anonymous exactly like an unpaired phone.
 *
 * NEVER THROWS. Any failure degrades to { null, null } — "assert no identity",
 * which the backend treats as anonymous: no vault answers rather than the wrong
 * member's vault answers.
 */
export function proxyDeviceSessionFor(
  req: CookieCarrier,
  now = Date.now(),
): ProxyDeviceSession {
  const NONE: ProxyDeviceSession = { sessionId: null, setCookie: null };
  try {
    const raw = readPairingCookie(req);
    if (!raw) return NONE;

    const identity = verifyDeviceCookieIdentity(raw, now);
    // Unpaired, revoked, expired, bad MAC, or authorised some other way (a
    // scoped device token). No identity, and never a Set-Cookie.
    if (!identity) return NONE;

    if (identity.version === "v2") {
      if (identity.deviceId === null) return NONE;
      const sessionId = deviceSessionId(identity.deviceId);
      const due =
        identity.issued !== null &&
        shouldRenewDeviceCookie(identity.issued, now);
      return {
        sessionId,
        setCookie: due
          ? deviceCookieOptions(
              issueDeviceCookieValueV2(identity.deviceId, now),
            )
          : null,
      };
    }

    // Verified v1: upgrade now and speak as the device it just became.
    const upgraded = upgradeV1Cookie(raw, now);
    if (!upgraded) return NONE; // row was revoked; stay anonymous
    return {
      sessionId: deviceSessionId(upgraded.deviceId),
      setCookie: deviceCookieOptions(upgraded.value),
    };
  } catch {
    return NONE;
  }
}

/* ── staged enforcement ────────────────────────────────────────────── */

export type EnforceMode = "off" | "knowledge" | "all";

/**
 * Staged rollout via PUFF_PAIRING_ENFORCE.
 *
 * Default is "knowledge" (NOT "all") on purpose: the kiosk browser running on
 * the appliance itself is just another unpaired client. Flipping straight to
 * "all" would 401 the main dashboard until somebody walks up to the kiosk and
 * types the code. The operator pairs the kiosk once, then sets "all".
 *
 * There is deliberately NO localhost/Host-header bypass: the Host header is
 * attacker-controlled, and under `next start` the peer IP is not reliably
 * available to route handlers. The kiosk pairs like every other device.
 *
 * An unrecognised value falls back to "all" — the restrictive choice — so a
 * typo in the unit file cannot silently disable the gate.
 */
export function enforceMode(): EnforceMode {
  const raw = (process.env.PUFF_PAIRING_ENFORCE || "").trim().toLowerCase();
  if (!raw) return "knowledge";
  if (raw === "off" || raw === "knowledge" || raw === "all") return raw;
  return "all";
}

/**
 * "off" is a DEVELOPER switch and it now says so out loud, once per process.
 *
 * With enforcement off there is no pairing gate at all, so the wizard family
 * below is reachable by any LAN client that can open the dashboard — that
 * client drives the build with the appliance's own API key attached by the
 * proxy. That is a legitimate thing to want on a laptop; it must never be a
 * thing an operator discovers by accident on a box, so it is announced on the
 * server console (never in a response body, which no unpaired caller should be
 * told anything by).
 */
let warnedEnforcementOff = false;

function warnIfEnforcementOff(mode: EnforceMode): void {
  if (mode !== "off" || warnedEnforcementOff) return;
  warnedEnforcementOff = true;
  // eslint-disable-next-line no-console
  console.warn(
    "[pairing] PUFF_PAIRING_ENFORCE=off: pairing is DISABLED. Any device on " +
      "this network can drive first-run setup and the build wizard " +
      "(/api/pai/wizard/*, /api/pai/build/prove*) through this dashboard, " +
      "using the appliance's own API key. Unset it (default: knowledge) " +
      "before this box leaves a workbench.",
  );
}

/** Paths gated in "knowledge" stage. */
const KNOWLEDGE_PREFIXES = ["/api/knowledge", "/api/pai/knowledge"];

/** Uploads outside the knowledge prefix that the "knowledge" stage gates too. */
const PHONE_UPLOAD_PATHS = ["/api/pai/chat/voice-memo"];

/**
 * The BUILD surface: the routes that decide what this appliance is.
 *
 * WHY THESE ARE GATED AT EVERY STAGE, READS INCLUDED
 * -------------------------------------------------
 * `/api/pai/wizard/*` proposes and APPLIES the build — POST /wizard/apply and
 * POST /wizard/run write config, scaffold folders, seal the vault and pick what
 * the bot may touch; POST /wizard/resume decides whether a stopped run is
 * continued. `/api/pai/build/prove*` runs proofs against the running build and
 * rewrites the recorded proof record. At the shipped default
 * (`PUFF_PAIRING_ENFORCE=knowledge`) none of that was covered by
 * KNOWLEDGE_PREFIXES, so an unpaired LAN client could drive the whole wizard
 * through the catch-all proxy with the appliance's key attached.
 *
 * The READS are gated too, not just the writes: GET /wizard/cards and GET
 * /wizard/state describe the machine (its models, its storage, what it has
 * been told, which steps are still open) — that is reconnaissance for whoever
 * is racing the owner to finish setup, and no unpaired device has a reason to
 * read it. Fail-closed is the whole family, not the mutating half.
 *
 * `/api/pai/build/self` is listed although this build ships no such route: a
 * prefix that gates nothing costs nothing, and the day it is added it is
 * covered without anybody remembering to cover it.
 */
const WIZARD_PREFIXES = [
  "/api/pai/wizard",
  "/api/pai/build/prove",
  "/api/pai/build/self",
];

/**
 * Persona WRITES (scaffold a persona, preview one, delete one) are the same
 * build surface by another door: POST /persona/scaffold writes a folder, a
 * page and an allowlist entry. Reads stay open at the "knowledge" stage,
 * exactly like the knowledge surface: the nav and the apps page render from
 * `GET /persona/list` on the kiosk, which is an unpaired client by design at
 * that stage, and the list carries no secret.
 */
const PERSONA_PREFIXES = ["/api/pai/persona"];

/**
 * Paths gated in "all" stage — every route that proxies to the backend or to
 * another LAN service on the server's behalf.
 *   /api/pai/*                  catch-all keyed backend proxy
 *   /api/knowledge/*            keyless backend proxies (source, asset)
 *   /api/ha/*                   keyed Home Assistant proxies
 *   /api/camera/*               keyed camera snapshot proxy
 *   /api/spotify/*              keyed OAuth callback → backend /api/spotify/exchange
 *   /dashboard/device/models    keyless server-side Ollama inventory proxy
 * Deliberately NOT gated: /api/health, /api/media/status, /api/system/stats
 * (no credential, no backend reach, and /api/health must answer for probes).
 *
 * /api/spotify/callback is reached by a top-level GET navigation (Spotify's 302
 * back to the appliance), which SameSite=Lax still carries the device cookie
 * on — see deviceCookieOptions() — so gating it does not break the paired
 * operator's Connect flow. Caveat: the cookie is host-scoped, so the operator
 * must have paired on the SAME host:port the registered redirect URI uses
 * (http://127.0.0.1:3003).
 */
const ALL_PREFIXES = [
  "/api/pai",
  "/api/knowledge",
  "/api/ha",
  "/api/camera",
  "/api/spotify",
  "/dashboard/device/models",
];

function hasPrefix(pathname: string, prefixes: string[]): boolean {
  for (let i = 0; i < prefixes.length; i++) {
    const p = prefixes[i];
    if (pathname === p || pathname.indexOf(p + "/") === 0) return true;
  }
  return false;
}

/**
 * Collapse a request path to the *effective* path the proxy will actually act
 * on, so the gate can't be fooled by percent-encoding.
 *
 * WHY THIS EXISTS (the bug it closes)
 * -----------------------------------
 * `nextUrl.pathname` preserves percent-encoding for an encoded separator:
 * `/api/pai/knowledge%2fupload` stays literally `…knowledge%2fupload`, so a raw
 * prefix match against `/api/pai/knowledge/` MISSES it. But the catch-all proxy
 * splits+DECODES `[...path]`, reconstructing `knowledge/upload` and forwarding
 * it upstream with the appliance key. That split — gate sees the encoded form,
 * proxy acts on the decoded form — was the bypass. We must gate on the same
 * bytes the proxy acts on.
 *
 * So: fully decode (looping to defeat double-encoding like `%252f`), then lower-
 * case for a case-insensitive gate decision (`KNOWLEDGE`, `%4bNOWLEDGE` etc. all
 * count as the knowledge surface). Decoding only ever REVEALS more path
 * structure, so it can only gate MORE, never less — it cannot move a request
 * out of a gated prefix.
 *
 * Fails closed: malformed percent-encoding (throws) or a control character in
 * the path returns null, and the caller treats null as "must be gated".
 */
/**
 * Is this decoded path already canonical? Returns it unchanged if so, else null.
 *
 * WHY THIS EXISTS, AND WHY IT REJECTS RATHER THAN RESOLVES
 * -------------------------------------------------------
 * Decoding alone left the gate deciding about a string that is not the path the
 * request ends up acting on. `/api/pair/../pai/vault/contents` decodes to
 * itself, still starts with "/api/pair/", and so hit the pairing-endpoint
 * exemption below: pathRequiresPairing() returned "not gated" for a path that
 * resolves into the keyed proxy. Same for `%2e%2e`, `..%2f`, `%252e%252e`, a
 * mixed-case spelling, a doubled slash, and a backslash separator.
 *
 * Stated plainly, because it matters for how urgently this reads: that is NOT
 * reachable over HTTP today. Next canonicalises the path before middleware or a
 * route handler sees it, so the gate is handed "/api/pai/vault/contents" and
 * correctly gates it. Verified end to end on `next start`: 15 traversal shapes,
 * none reached the backend unpaired, including from a raw socket that does no
 * client-side normalisation; and `/api/pai/../pair/status` answers 200 with the
 * pairing endpoint's body, which is the direct proof that normalisation happens
 * upstream of this code.
 *
 * So this is defence by accident, and that is the reason to fix it rather than
 * to file it. The property belongs to one framework version's routing, not to
 * this gate, and it can be lost without anyone touching this file: a Next
 * upgrade that changes normalisation, a reverse proxy or rewrite that forwards
 * a raw path, or requirePairing() being handed a request whose pathname was
 * reconstructed rather than routed — which is exactly the "if a matcher is ever
 * mis-edited" case the route handler's second layer exists for. A gate has to
 * be correct about the input it is actually given.
 *
 * It REJECTS rather than resolves on purpose. Resolving "a/../b" to "b" would
 * only make this gate agree with one particular normaliser, and would leave the
 * same question open for the next one. No legitimate caller needs a
 * non-canonical path, so anything that would change under normalisation is
 * refused and its caller is treated as gated.
 */
function canonicalOrNull(decoded: string): string | null {
  // Must be absolute; a relative path cannot be prefix-tested safely.
  if (decoded.charCodeAt(0) !== 47 /* "/" */) return null;
  // Backslash is a separator on Windows and to several path parsers, and is
  // never legitimate in these routes.
  if (decoded.indexOf("\\") !== -1) return null;
  // An empty interior segment ("//"): collapses under normalisation, and
  // "/api/pair//../pai/..." is precisely the shape that walks out of a prefix.
  if (decoded.indexOf("//") !== -1) return null;

  const segs = decoded.split("/");
  for (let i = 1; i < segs.length; i++) {
    const seg = segs[i];
    // The only legal empty segment is a trailing slash ("/dashboard/"); the
    // "//" check above already ruled out an interior one.
    if (seg.length === 0) continue;
    if (seg === "." || seg === "..") return null;
    // Leading/trailing whitespace is stripped by some parsers and kept by
    // others — another way to spell a segment that compares differently here
    // than it resolves elsewhere.
    if (seg !== seg.trim()) return null;
    // Windows path handling drops a trailing dot ("pair." -> "pair").
    if (seg.charCodeAt(seg.length - 1) === 46 /* "." */) return null;
  }
  return decoded;
}

function effectiveGatePath(pathname: string): string | null {
  try {
    let prev = pathname;
    // Bounded loop: undo double/triple encoding (%252f → %2f → /). Three passes
    // is far more than any legitimate path needs; a value still shrinking after
    // that is pathological — err toward gating by continuing to decode.
    for (let i = 0; i < 3; i++) {
      const dec = decodeURIComponent(prev);
      if (dec === prev) break;
      prev = dec;
    }
    // NUL / control chars never appear in a legitimate backend path; a decoded
    // one is a smuggling attempt. Fail closed.
    if (/[\x00-\x1f\x7f]/.test(prev)) return null;
    // Decide on the canonical path or on nothing at all. MUST run after the
    // decode loop: "%2e%2e" and "..%2f" only become traversal once decoded.
    const canonical = canonicalOrNull(prev);
    if (canonical === null) return null;
    return canonical.toLowerCase();
  } catch {
    // Malformed escape (e.g. "%zz", lone "%") → fail closed.
    return null;
  }
}

/**
 * Read methods that never mutate state. At the "knowledge" stage the gate
 * protects the knowledge surface from unpaired *writes* (document injection,
 * deletion) but lets *reads* through: browsing and searching the knowledge base
 * must work on the kiosk and any LAN device without pairing, and every other
 * backend read at this stage (calendar, HA state, cameras) is already open, so
 * gating knowledge reads bought no real protection while breaking the flagship
 * page. An unknown/missing method is treated as a write — fail closed.
 */
const READ_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/**
 * The ONE write-shaped call exempted at the "knowledge" stage: resolving a
 * proactive suggestion.
 *
 * WHY IT IS SAFE (verified against the backend handlers)
 * -----------------------------------------------------
 * `/knowledge/suggestions/<id>/accept` and `.../dismiss` (api_server.py →
 * puffpai/suggestions.py `_set_status`) do exactly one thing: move a record in
 * the *notification queue* from "pending" to "accepted"/"dismissed". They read
 * no request body, touch neither ChromaDB nor the personal vault, and accept's
 * response is the very same record the caller can already GET from
 * `/knowledge/suggestions/pending` — which is open to unpaired clients at this
 * stage. So the exemption discloses nothing new and destroys no data.
 *
 * Accepting also does NOT write to the calendar. It only hands the extracted
 * candidates to the preview-confirm modal, where a human still ticks each event.
 *
 * WHY IT IS NEEDED
 * ----------------
 * The appliance's own kiosk browser is an unpaired client by design at the
 * "knowledge" stage (see enforceMode). Gating these two routes made the
 * proactive suggestion card's buttons permanent no-ops on the kiosk and on
 * every unpaired household laptop: the card rendered from an open read, then
 * 401'd on both answers.
 *
 * WHAT STAYS GATED
 * ----------------
 * Everything else on the knowledge surface: upload, delete, re-index, source
 * writes — unchanged. At the "all" stage this exemption does not apply at all;
 * there the operator has paired the kiosk and wants nothing crossing unpaired.
 *
 * MATCHER DISCIPLINE
 * ------------------
 * Exact whole-path shape, POST only, on the DECODED+casefolded `eff` path (the
 * same bytes the catch-all proxy acts on), anchored ^…$ so no prefix/suffix
 * smuggling works. The id segment is [a-z0-9][a-z0-9_-]{0,63}:
 *   - no "/" or "\"  → a segment can never span path components
 *   - no "."         → ".." and "%2e%2e" traversal cannot form an id at all
 *   - no "%"         → an undecodable path already fails closed upstream
 * `..%2fupload/accept`, `/accept/../upload`, `/accept/`, `/acceptx`,
 * `/upload/accept` and friends therefore all miss the exemption and stay gated.
 * (Belt and braces: the catch-all proxy independently 400s any segment that
 * still contains a separator after Next's own decode.)
 */
const SUGGESTION_RESOLVE_PATH =
  /^\/api\/(?:pai\/)?knowledge\/suggestions\/[a-z0-9][a-z0-9_-]{0,63}\/(?:accept|dismiss)$/;

/**
 * `eff` MUST already be the decoded, lowercased gate path from
 * effectiveGatePath() — the regex is deliberately lowercase-only so passing a
 * raw pathname here can only ever fail to match (fail closed), never over-match.
 */
function isSuggestionResolution(eff: string, method?: string): boolean {
  if ((method || "").toUpperCase() !== "POST") return false;
  return SUGGESTION_RESOLVE_PATH.test(eff);
}

/**
 * Should this request require pairing under the current stage?
 *
 * `method` matters only at the "knowledge" stage, where reads are allowed and
 * writes are gated. At "all" every method is gated (the operator has paired the
 * kiosk by then and wants nothing crossing unpaired). Omitting `method` fails
 * closed (treated as a write).
 */
export function pathRequiresPairing(
  pathname: string,
  method?: string,
  /**
   * Caller's evidence that the request is not cross-site (isSameOrigin(req)).
   * ONLY the suggestion-resolution exemption consults it. Omitted → treated as
   * cross-site, so the exemption does not apply and the path stays gated. Every
   * other decision here is header-independent and unaffected.
   */
  opts?: { sameOrigin?: boolean },
): boolean {
  const mode = enforceMode();
  // "off" disables the gate entirely — preserve that exactly, even for
  // malformed input, so turning enforcement off means off. It is announced on
  // the console once per process so "off" is never a silent state.
  if (mode === "off") {
    warnIfEnforcementOff(mode);
    return false;
  }

  const eff = effectiveGatePath(pathname);
  // Undecodable / malformed path while a gate is active → fail closed.
  if (eff === null) return true;

  // The pairing endpoints themselves must always be reachable, or a fresh
  // device could never pair. Checked on the decoded+casefolded path so an
  // encoded spelling can't dodge the exemption either. (A request that routes
  // to a keyed proxy always decodes to an /api/pai|knowledge|ha|camera prefix,
  // never /api/pair/, so this exemption can't be abused to reach the key.)
  if (eff === "/api/pair" || eff.indexOf("/api/pair/") === 0) {
    return false;
  }

  if (mode === "knowledge") {
    // The build surface is gated at this stage as well, reads included. It is
    // checked BEFORE the knowledge prefixes so no exemption below can reach it.
    if (hasPrefix(eff, WIZARD_PREFIXES)) return true;
    // Persona writes only: the list is what the nav renders from.
    if (
      hasPrefix(eff, PERSONA_PREFIXES) &&
      !READ_METHODS.has((method || "").toUpperCase())
    ) {
      return true;
    }
    // A Send to Puff voice memo is an upload like a photo, so it is gated
    // exactly as the photo upload is, at this stage too (every method).
    if (hasPrefix(eff, PHONE_UPLOAD_PATHS)) return true;
    if (!hasPrefix(eff, KNOWLEDGE_PREFIXES)) return false;
    // Gate writes only; reads of the knowledge base stay open on the LAN.
    if (READ_METHODS.has((method || "").toUpperCase())) return false;
    // Read-equivalent by content: resolving a suggestion mutates only the
    // notification queue. Deliberately scoped to this stage — at "all" the
    // check below gates it like everything else.
    //
    // Same-origin is REQUIRED for the exemption (and only for it). Exempting a
    // state-changing route from the credential gate would otherwise make it
    // drivable by a hostile page the household happens to open — including via
    // DNS rebinding, where the browser still stamps the attacker's Origin. A
    // request with no Origin at all (curl, native app) is not browser-driven
    // and is allowed, matching isSameOrigin()'s existing contract.
    if (opts?.sameOrigin && isSuggestionResolution(eff, method)) return false;
    return true;
  }
  return hasPrefix(eff, ALL_PREFIXES);
}

const UNPAIRED_BODY = JSON.stringify({ error: "pairing_required" });

export function unpairedResponse(): Response {
  return new Response(UNPAIRED_BODY, {
    status: 401,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    },
  });
}

/**
 * Route-handler guard. Returns a 401 Response to return immediately, or null
 * to continue.
 *
 *   const denied = requirePairing(req);
 *   if (denied) return denied;
 */
export function requirePairing(
  req: CookieCarrier & {
    nextUrl?: { pathname: string };
    url?: string;
    method?: string;
  },
): Response | null {
  let pathname = req.nextUrl?.pathname;
  if (!pathname && req.url) {
    try {
      pathname = new URL(req.url).pathname;
    } catch {
      pathname = undefined;
    }
  }
  // Unknown path → treat as protected (fail closed).
  if (
    pathname &&
    !pathRequiresPairing(pathname, req.method, {
      sameOrigin: isSameOrigin(req),
    })
  )
    return null;
  if (isPaired(req)) return null;
  return unpairedResponse();
}

/* ── failed-attempt rate limiting ──────────────────────────────────── */

const RL_WINDOW_MS = 60_000;
const RL_MAX_FAILS = 5;

/**
 * In-memory failed-pairing counter.
 *
 * Single-process `next start`, so a plain Map is enough; it resets on restart,
 * which is an accepted weakness (an attacker who can restart the service has
 * already won). Not shared across workers if the deployment ever scales out.
 *
 * PER-IP BUCKETING IS OFF BY DEFAULT, ON PURPOSE. Under bare `next start`
 * there is no trusted reverse proxy, so x-forwarded-for / x-real-ip are just
 * attacker-supplied strings: bucketing on them would let a guesser mint a
 * fresh 5-attempt budget per request simply by rotating the header, i.e. no
 * rate limit at all. So unless PUFF_TRUST_PROXY is explicitly set, every
 * request shares one global bucket — guessing is capped at 5/minute for the
 * whole appliance.
 *
 * Trade-off, stated plainly: any LAN device can then keep pairing locked out a
 * minute at a time (a nuisance-level DoS on *pairing only*; paired devices are
 * unaffected). Given a 32^8 (~1.1e12) code space, 5/min makes brute force
 * hopeless, so the global cap costs nothing in security terms.
 *
 * Set PUFF_TRUST_PROXY=1 ONLY if the dashboard is fronted by a reverse proxy
 * that overwrites x-forwarded-for.
 *
 * ── CHECKED AGAINST next@16.2.12 ──
 * The question was: can we bucket on the REAL peer address instead, and drop
 * the header caveat entirely? No. Read the source before re-litigating this:
 *
 *  1. NextRequest exposes no peer address. Its whole public surface is
 *     `cookies`, `nextUrl`, `page` (deprecated), `ua` (deprecated), `url`
 *     — node_modules/next/dist/server/web/spec-extension/request.d.ts:10-26.
 *     The old `.ip` was a Vercel-platform field and is gone. There is no
 *     symbol, no AsyncLocalStorage, and no request-context escape hatch that
 *     hands user code the underlying IncomingMessage or its socket; the raw
 *     Node request stays inside the server internals in both middleware and
 *     route handlers.
 *
 *  2. Next DOES fill in x-forwarded-for from the socket when self-hosted, at
 *     node_modules/next/dist/server/base-server.js:577 —
 *       req.headers['x-forwarded-for'] ??= originalRequest?.socket?.remoteAddress
 *     Note the `??=`. It only fills the header in when the CLIENT DID NOT SEND
 *     ONE. A client that sends its own x-forwarded-for wins outright, and the
 *     result carries no marker distinguishing "synthesised from the socket"
 *     from "typed by the attacker". So this header is not a peer address; it is
 *     a peer address *unless someone preferred otherwise*, which is the same
 *     thing as attacker-controlled. Bucketing on it would hand a guesser an
 *     unlimited supply of fresh 5-attempt budgets.
 *
 * Therefore: keep the global bucket. Do not bucket on any header under bare
 * `next start`. The only correct fix is a real reverse proxy that strips and
 * rewrites x-forwarded-for, which is what PUFF_TRUST_PROXY=1 is gated on.
 *
 * The TTL on pairing codes (PAIR_CODE_TTL_SEC, added the same day) makes the
 * global bucket more than sufficient rather than merely sufficient: an attacker
 * no longer has unlimited time against a static target. At 5 guesses/minute
 * against a 32^8 space they get ~300 attempts inside a code's one-hour life,
 * i.e. roughly a 1-in-4-billion chance per code, after which the code they were
 * grinding no longer exists.
 */
function rlKey(req: CookieCarrier): string {
  if (process.env.PUFF_TRUST_PROXY !== "1") return "global";
  const xff = req.headers.get("x-forwarded-for");
  if (xff) return `xff:${xff.split(",")[0].trim()}`;
  const real = req.headers.get("x-real-ip");
  if (real) return `ip:${real.trim()}`;
  return "unknown";
}

/** True if this client is currently rate-limited (too many recent failures). */
export function isRateLimited(req: CookieCarrier, now = Date.now()): boolean {
  const map = (G.__puffPairFails ??= new Map());
  const entry = map.get(rlKey(req));
  if (!entry) return false;
  if (now - entry.windowStart >= RL_WINDOW_MS) return false;
  return entry.count >= RL_MAX_FAILS;
}

export function recordPairFailure(req: CookieCarrier, now = Date.now()): void {
  const map = (G.__puffPairFails ??= new Map());
  const key = rlKey(req);
  const entry = map.get(key);
  if (!entry || now - entry.windowStart >= RL_WINDOW_MS) {
    map.set(key, { count: 1, windowStart: now });
    return;
  }
  entry.count += 1;
}

export function clearPairFailures(req: CookieCarrier): void {
  G.__puffPairFails?.delete(rlKey(req));
}

/* ── cookie attributes ─────────────────────────────────────────────── */

export interface DeviceCookieOptions {
  name: string;
  value: string;
  httpOnly: true;
  sameSite: "lax";
  path: "/";
  maxAge: number;
  secure: false;
}

/**
 * Cookie attributes for the paired-device cookie.
 *
 * `secure: false` is DELIBERATE. The appliance serves plain HTTP on the LAN
 * (http://192.168.50.40:3003) — there is no TLS story yet. Setting Secure would
 * make the browser drop the cookie outright and pairing would silently never
 * work. The consequence is that the cookie is sniffable by anyone on the local
 * network; that is a known, accepted residual risk documented at the top of
 * this file. Flip this to true the moment the dashboard is served over HTTPS.
 *
 * SameSite=Lax means a malicious site cannot forge cross-site POST/PUT/DELETE
 * against the proxies with this cookie attached (top-level GET navigations do
 * still carry it).
 */
export function deviceCookieOptions(value: string): DeviceCookieOptions {
  return {
    name: PAIRING_COOKIE,
    value,
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: COOKIE_MAX_AGE_SEC,
    secure: false,
  };
}

/**
 * Attributes that DELETE the device cookie. Used when a device revokes itself,
 * so the browser stops sending a cookie that is about to stop verifying anyway.
 *
 * Identical name/path/attributes to deviceCookieOptions — a Set-Cookie only
 * overwrites a cookie it matches on, so a mismatched path here would leave the
 * old cookie in place and the screen would keep sending a dead credential.
 * Clearing is a courtesy, not the control: the authority is gone the moment the
 * registry row is, whether or not the browser cooperates.
 */
export function clearedDeviceCookieOptions(): DeviceCookieOptions {
  return {
    name: PAIRING_COOKIE,
    value: "",
    httpOnly: true,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
    secure: false,
  };
}

/* ── CSRF: same-origin check for state-changing pairing calls ──────── */

/**
 * Reject cross-site state-changing requests. The Origin header is set by the
 * browser and cannot be forged by page JavaScript, so comparing it against the
 * request's own Host is a cheap, reliable CSRF check that works no matter
 * which hostname/IP the appliance is reached by.
 *
 * Requests with no Origin at all (curl, native app) are allowed — they are not
 * browser-driven and therefore not CSRF.
 */
export function isSameOrigin(req: CookieCarrier): boolean {
  const origin = req.headers.get("origin");
  if (!origin) return true;
  const host = req.headers.get("host");
  if (!host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
