// The owner's vault unlock session, for the build engine's changes.
//
// After setup, installing or removing an app, storing a key, connecting
// GitHub, opening the Developer terminal and the other engine changes need an
// unlocked Vault: the backend answers 409 `setup_complete` without one
// (api_server._ENGINE_POST_SETUP_WRITES and _setup_gate). `vaultFetch` sends
// the session it holds as X-Vault-Token; when the backend still says locked,
// it asks for the PIN through the prompt the dashboard shell mounts
// (VaultPinPrompt), then sends the same request once more with the new session.
//
// WHERE IT LIVES. A module variable, memory only, like the first-run setup
// code: never localStorage, sessionStorage, a cookie, a store or the URL, never
// logged, gone with the tab. The PIN itself never reaches this module; only
// the session the backend hands back after a correct PIN does.
//
// Plain JavaScript inside a .ts file (Node runs it with type stripping), no
// imports, so the test can load it without a build.

let session = "";

/** The line the PIN prompt shows when an engine change waits for the Vault. */
export const VAULT_NEEDED_LINE = "Unlock your Vault to make this change.";

export const VAULT_NOT_SET_UP_LINE = "Your Vault is not set up yet. Choose a PIN on the Vault page first.";
export const VAULT_WRONG_PIN_LINE = "That PIN did not open your Vault. Try again.";
export const VAULT_NO_ANSWER_LINE = "Your bot did not answer. Try again in a moment.";

/**
 * Plain words for a refused POST /vault/unlock. The backend's message is never
 * shown as it came: some of it is written for programs, not people. Only the
 * try-limit wait is passed on, as a number.
 */
export function unlockRefusalWords(status: number, body: unknown): string {
  const b = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  if (b.needs_initialization === true) return VAULT_NOT_SET_UP_LINE;
  if (status === 401 && b.error === "pairing_required") {
    return "Pair this screen first, then try again.";
  }
  const said = typeof b.message === "string" ? b.message : "";
  const wait = /try again in (\d{1,6}) seconds/i.exec(said);
  if (wait) return `Too many tries. Wait ${wait[1]} seconds, then try again.`;
  if (/code required/i.test(said)) {
    return "Your Vault also asks for the code from your sign-in app. Open it on the Vault page.";
  }
  if (status === 401) return VAULT_WRONG_PIN_LINE;
  if (status === 0 || status >= 500) return VAULT_NO_ANSWER_LINE;
  return "Your Vault did not open. Try again.";
}

/** Keep (or clear, with "") the session the backend answered an unlock with. */
export function setVaultSession(token: unknown): void {
  session = typeof token === "string" ? token.trim().slice(0, 512) : "";
}

/** True while this tab holds a session. Says nothing about whether it is live. */
export function hasVaultSession(): boolean {
  return session !== "";
}

/** The header block for a call the backend may gate on the Vault. */
export function vaultSessionHeaders(): Record<string, string> {
  return session ? { "X-Vault-Token": session } : {};
}

/** True when a reply means "this change needs an unlocked Vault". */
export function isVaultLockedReply(status: number, body: unknown): boolean {
  if (status !== 409 || !body || typeof body !== "object") return false;
  return (body as { error?: unknown }).error === "setup_complete";
}

/** Shows the PIN prompt; resolves with a fresh session, or "" if the owner stops. */
type Asker = (line: string) => Promise<string>;

let asker: Asker | null = null;
let asking: Promise<string> | null = null;

/** The shell's prompt registers here. Returns the unregister function. */
export function registerVaultAsker(fn: Asker): () => void {
  asker = fn;
  return () => {
    if (asker === fn) asker = null;
  };
}

/** Ask once for every request waiting at the same time. */
function askOnce(): Promise<string> {
  if (!asker) return Promise.resolve("");
  if (!asking) {
    const current = asker;
    asking = current(VAULT_NEEDED_LINE).finally(() => {
      asking = null;
    });
  }
  return asking;
}

function withSession(init: RequestInit | undefined): RequestInit {
  const headers = new Headers(init?.headers);
  headers.delete("X-Vault-Token");
  const extra = vaultSessionHeaders();
  for (const name of Object.keys(extra)) headers.set(name, extra[name]);
  return { ...(init || {}), headers };
}

/**
 * `fetch`, plus the Vault session. A reply that says the Vault is locked asks
 * for the PIN (when a prompt is mounted) and sends the request once more; if
 * the owner stops, or no prompt is mounted, the locked reply is returned as it came,
 * so the caller shows the backend's own words.
 */
export async function vaultFetch(input: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(input, withSession(init));
  if (res.status !== 409 || !asker) return res;
  let body: unknown = null;
  try {
    body = await res.clone().json();
  } catch {
    return res;
  }
  if (!isVaultLockedReply(res.status, body)) return res;
  setVaultSession("");
  const fresh = await askOnce();
  if (!fresh) return res;
  setVaultSession(fresh);
  // A deadline that ran out while the owner typed their PIN is not this request's.
  const again = init?.signal?.aborted ? { ...init, signal: undefined } : init;
  return fetch(input, withSession(again));
}
