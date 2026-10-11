/**
 * The dashboard server's question to the Bridge: "is this switch open?"
 *
 * The backend's egress registry (src/puffpai/security/egress_doors.py) is the
 * one list of every way out of the house and the one place each switch is
 * set. The dashboard's own server routes that reach the internet (radio
 * search, the radio play count, TuneIn, the page reader) are not Python, so
 * they cannot call the registry's request wrapper. Instead each one asks the
 * backend over the same keyed connection the /api/pai proxy uses, before it
 * opens any socket or looks up any name:
 *
 *   GET {backend}/system/egress/door/<id>   (Authorization: Bearer <key>)
 *   -> { success: true, id, open: boolean, hosts: string[], dynamic_hosts }
 *
 * FAIL CLOSED. Only a well-formed answer that says `open: true` for the id
 * that was asked lets a request through. A missing API key, a backend that is
 * down, slow, redirecting or answering anything else is a refusal, and the
 * route sends nothing.
 *
 * NO COPY OF THE CONFIG. The dashboard never reads the flags itself, so the
 * switch in Settings, the environment override and the owner overlay all
 * decide here exactly as they do for the backend's own calls.
 *
 * SHORT CACHE. A clear yes or no is remembered for BRIDGE_CACHE_MS, so a
 * burst of searches while someone types asks once, and closing a switch takes
 * effect within that window. A failed check is never remembered.
 *
 * `hosts` comes back with the answer so a route can use the registry's own
 * host list as its allowlist rather than a second list kept here.
 */

/** The switches the dashboard server asks about. */
export type BridgeSwitchId = "radio_browser" | "tunein" | "page_reader";

/** How long a clear answer is reused. Short on purpose: see the header. */
export const BRIDGE_CACHE_MS = 3_000;

/** The backend is on the same box; a check slower than this is a failure. */
export const BRIDGE_TIMEOUT_MS = 2_500;

/**
 * Names the owner sees on the Settings card for each switch. Kept equal to
 * DOOR_LABELS in app/dashboard/settings/EgressDoorsCard.tsx (a test checks),
 * so a refusal names the exact row to look for.
 */
export const BRIDGE_SWITCH_LABELS: Readonly<Record<BridgeSwitchId, string>> = {
  radio_browser: "Radio station directory",
  tunein: "TuneIn station search",
  page_reader: "Web page reader",
};

/** What each switch lets the household do, for the refusal sentence. */
const BRIDGE_SWITCH_ACTIVITY: Readonly<Record<BridgeSwitchId, string>> = {
  radio_browser: "Finding radio stations",
  tunein: "Searching TuneIn",
  page_reader: "Opening web pages in the reader",
};

export type BridgeDecision =
  | { open: true; id: BridgeSwitchId; hosts: readonly string[] }
  | {
      open: false;
      id: BridgeSwitchId;
      /** bridge_closed: the switch is off. bridge_unavailable: no usable answer. */
      reason: "bridge_closed" | "bridge_unavailable";
    };

export interface BridgeCheckOptions {
  /** Injected by tests; production uses the global fetch. */
  fetchImpl?: typeof fetch;
  /** Injected by tests; production uses Date.now. */
  now?: () => number;
}

const cache = new Map<BridgeSwitchId, { at: number; decision: BridgeDecision }>();

/** Forget every remembered answer (tests). */
export function clearBridgeCache(): void {
  cache.clear();
}

/**
 * Same backend and key as app/api/pai/[...path]/route.ts. Read per call, not
 * at import, so a test can set them.
 */
function backendBase(): string {
  return (
    process.env.PUFFPAI_BACKEND_URL ??
    process.env.PUFFPAI_API_URL ??
    "http://localhost:8000"
  ).replace(/\/+$/, "");
}

function apiKey(): string {
  return process.env.PUFFPAI_API_KEY ?? "";
}

/** Parse the backend's answer. Anything unexpected is null (unavailable). */
function readAnswer(id: BridgeSwitchId, body: unknown): BridgeDecision | null {
  if (!body || typeof body !== "object") return null;
  const b = body as {
    success?: unknown;
    id?: unknown;
    open?: unknown;
    hosts?: unknown;
  };
  if (b.success !== true || b.id !== id || typeof b.open !== "boolean") {
    return null;
  }
  if (!b.open) return { open: false, id, reason: "bridge_closed" };
  if (!Array.isArray(b.hosts) || !b.hosts.every((h) => typeof h === "string")) {
    return null;
  }
  return { open: true, id, hosts: Object.freeze([...(b.hosts as string[])]) };
}

/**
 * Ask the backend whether switch `id` is open. Never throws; every failure is
 * a closed answer.
 */
export async function checkBridgeSwitch(
  id: BridgeSwitchId,
  opts: BridgeCheckOptions = {},
): Promise<BridgeDecision> {
  const now = opts.now ?? Date.now;
  const hit = cache.get(id);
  if (hit && now() - hit.at < BRIDGE_CACHE_MS) return hit.decision;
  cache.delete(id);

  const unavailable: BridgeDecision = {
    open: false,
    id,
    reason: "bridge_unavailable",
  };
  const key = apiKey();
  // Without the key the backend refuses the question anyway; do not ask.
  if (!key) return unavailable;

  const doFetch = opts.fetchImpl ?? fetch;
  let decision: BridgeDecision | null = null;
  try {
    const res = await doFetch(
      `${backendBase()}/system/egress/door/${encodeURIComponent(id)}`,
      {
        method: "GET",
        headers: { Authorization: `Bearer ${key}`, Accept: "application/json" },
        cache: "no-store",
        // A redirect is not an answer from the backend; never follow one
        // with the key attached.
        redirect: "error",
        signal: AbortSignal.timeout(BRIDGE_TIMEOUT_MS),
      },
    );
    if (res.status !== 200) {
      void res.body?.cancel().catch(() => {});
      return unavailable;
    }
    decision = readAnswer(id, await res.json());
  } catch {
    return unavailable;
  }
  if (!decision) return unavailable;
  cache.set(id, { at: now(), decision });
  return decision;
}

/** The plain sentence a person sees when a switch refuses a request. */
export function bridgeRefusalText(
  id: BridgeSwitchId,
  reason: "bridge_closed" | "bridge_unavailable",
): string {
  const label = BRIDGE_SWITCH_LABELS[id];
  if (reason === "bridge_closed") {
    return (
      `${BRIDGE_SWITCH_ACTIVITY[id]} is switched off. To turn it on, open ` +
      `Settings and switch on "${label}" in the Bridge section.`
    );
  }
  return (
    `Could not check whether the "${label}" switch is on, so nothing was ` +
    `sent. Try again in a moment.`
  );
}

export interface BridgeRefusal {
  status: number;
  body: {
    ok: false;
    reason: "bridge_closed" | "bridge_unavailable";
    switch: BridgeSwitchId;
    error: string;
  };
}

/**
 * The JSON refusal for a closed decision: 403 when the switch is off, 503
 * when the answer could not be had. `error` is the sentence to show.
 */
export function bridgeRefusal(
  decision: Extract<BridgeDecision, { open: false }>,
): BridgeRefusal {
  return {
    status: decision.reason === "bridge_closed" ? 403 : 503,
    body: {
      ok: false,
      reason: decision.reason,
      switch: decision.id,
      error: bridgeRefusalText(decision.id, decision.reason),
    },
  };
}

/** A refusal as a ready Response, for routes that answer with one. */
export function bridgeRefusalResponse(
  decision: Extract<BridgeDecision, { open: false }>,
): Response {
  const r = bridgeRefusal(decision);
  return Response.json(r.body, {
    status: r.status,
    headers: { "Cache-Control": "no-store" },
  });
}
