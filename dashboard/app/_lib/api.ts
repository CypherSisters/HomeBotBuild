// Base API client for the PuffPAI backend.
//
// THE BROWSER NEVER HOLDS THE BACKEND KEY.
//
// Every call below goes to the same-origin proxy at /api/pai, which attaches
// `Authorization: Bearer <PUFFPAI_API_KEY>` server-side
// (app/api/pai/[...path]/route.ts) behind the pairing gate. So this module
// sends no Authorization header at all, and there are deliberately no
// browser-side key getter/setter helpers any more. (Their old names are
// asserted absent by scripts/check-no-secret-in-bundle.js, so they are not
// spelled out anywhere under app/ — including in prose like this.)
//
//   * a NEXT_PUBLIC_ env var is INLINED into the client bundle by Next, so
//     reading the key from one shipped the owner's backend credential to every
//     browser on the LAN in plain JavaScript;
//   * localStorage was no better. It kept the key in the browser where any
//     script on the page could read it, and it bought nothing once the proxy
//     started attaching the key itself.
//
// If a future call genuinely needs a credential the proxy cannot supply, add it
// to the proxy, not to this file. Nothing here should ever learn the key again.
//
// The base is a hardcoded relative path, never read from an env var.
// An env-supplied base could point the browser straight at the backend (or at
// another origin), which bypasses the proxy and therefore the pairing gate, the
// path allowlist and the cross-site check — and would send X-Vault-Token to
// whatever host was configured. Same-origin-relative is the only safe value, so
// it is the only value.
import { vaultFetch } from './vaultSession';

const API_URL = '/api/pai';

// Removing the code that WROTE the key does not remove the copies it already
// left behind: any browser that ever used the old Profile > API Key card still
// has the appliance's backend credential sitting in localStorage. Deleting the
// writer and leaving the copy would be the worst of both worlds, so purge it on
// module load. Safe to run forever — it is a no-op once the key is gone, and
// nothing reads that entry any more.
if (typeof window !== 'undefined') {
  try {
    localStorage.removeItem('puffpai_api_key');
  } catch {
    /* storage disabled or partitioned: nothing to clean up */
  }
}

function getHeaders(): HeadersInit {
  return { 'Content-Type': 'application/json' };
}

export async function apiGet<T = any>(
  endpoint: string,
  // `signal` lets a caller put a deadline on a request that can legitimately
  // hang: the ZIM title index on an 8-million-article pack is the known case,
  // and a spinner that never resolves is what made the Knowledge page look
  // broken. Optional, so every existing call is unchanged.
  opts?: { cache?: RequestCache; signal?: AbortSignal },
): Promise<T> {
  const res = await fetch(`${API_URL}${endpoint}`, {
    method: 'GET',
    headers: getHeaders(),
    cache: opts?.cache,
    signal: opts?.signal,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

/**
 * GET that keeps the HTTP status instead of throwing.
 *
 * A 404 is part of the contract for routes that may not be deployed yet
 * (`/ha/home` on a box running an older backend): the caller has to tell
 * "not built yet" apart from "the request failed", and apiGet flattens both
 * into one Error.
 */
export async function apiGetRaw<T = any>(
  endpoint: string,
  // `signal` as in apiGet: a poll that can hang needs a deadline, otherwise a
  // stalled socket freezes the caller's retry loop.
  opts?: { cache?: RequestCache; signal?: AbortSignal },
): Promise<{ status: number; ok: boolean; data: T | null }> {
  const res = await fetch(`${API_URL}${endpoint}`, {
    method: 'GET',
    headers: getHeaders(),
    cache: opts?.cache ?? 'no-store',
    signal: opts?.signal,
  });
  const data = (await res.json().catch(() => null)) as T | null;
  return { status: res.status, ok: res.ok, data };
}

export async function apiPost<T = any>(endpoint: string, body?: any): Promise<T> {
  const res = await vaultFetch(`${API_URL}${endpoint}`, {
    method: 'POST',
    headers: getHeaders(),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

/**
 * POST that keeps the HTTP status and the raw body instead of throwing.
 *
 * Some backend routes (e.g. POST /dashboard/cameras) answer non-2xx with a
 * human-readable `message` and no `error` key — apiPost would discard both.
 * Use this when the status code is part of the contract.
 */
export async function apiPostRaw<T = any>(
  endpoint: string,
  body?: any
): Promise<{ status: number; ok: boolean; data: T | null }> {
  const res = await vaultFetch(`${API_URL}${endpoint}`, {
    method: 'POST',
    headers: getHeaders(),
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = (await res.json().catch(() => null)) as T | null;
  return { status: res.status, ok: res.ok, data };
}

export async function apiPut<T = any>(endpoint: string, body?: any): Promise<T> {
  const res = await vaultFetch(`${API_URL}${endpoint}`, {
    method: 'PUT',
    headers: getHeaders(),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

// A DELETE may carry a body: some backend deletions require a typed-back
// confirmation in it (DELETE /persona/<id> wants {"confirm": "<id>"}), which is
// the owner saying they mean THAT one. Optional, so every existing caller is
// unchanged and still sends none.
export async function apiDelete<T = any>(endpoint: string, body?: any): Promise<T> {
  const res = await vaultFetch(`${API_URL}${endpoint}`, {
    method: 'DELETE',
    headers: getHeaders(),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

// SSE streaming for chat
export function apiStream(endpoint: string, body: any, onChunk: (text: string) => void, onDone: () => void) {
  fetch(`${API_URL}${endpoint}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, stream: true }),
  }).then(async (res) => {
    if (!res.ok) throw new Error(res.statusText);
    const reader = res.body?.getReader();
    const decoder = new TextDecoder();
    if (!reader) { onDone(); return; }

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const text = decoder.decode(value);
      const lines = text.split('\n');
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6);
          if (data === '[DONE]') { onDone(); return; }
          try {
            const parsed = JSON.parse(data);
            const content = parsed.choices?.[0]?.delta?.content || '';
            if (content) onChunk(content);
          } catch { /* skip non-JSON lines */ }
        }
      }
    }
    onDone();
  }).catch(() => onDone());
}

// Health check
export async function checkHealth(): Promise<boolean> {
  try {
    const res = await fetch(`${API_URL}/health`);
    return res.ok;
  } catch {
    return false;
  }
}

export { API_URL };
