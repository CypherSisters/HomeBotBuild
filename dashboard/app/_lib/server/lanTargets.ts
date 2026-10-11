/**
 * Where the dashboard's loopback media and books proxies forward to, and which
 * LAN origin the page CSP may name for Jellyfin. All three read the SAME env
 * values, so they are validated in one place.
 *
 * FAIL CLOSED. There is no default target. Jellyfin and Calibre-web are
 * optional and their address is specific to each install; a baked-in address
 * would forward the kiosk's requests (and the session cookies riding on them)
 * to whatever device happens to hold that address on somebody else's network.
 * Unset, empty or malformed means "this proxy is off" and "no LAN origin in the
 * CSP".
 *
 * A value is accepted only as a plain hostname or IPv4 address: letters,
 * digits, dots and hyphens. No scheme, port, path, brackets, spaces, quotes or
 * semicolons, so an env value can never add a CSP directive or source
 * expression. IPv6 literals are refused (they would need brackets in a URL).
 *
 * Pure module: no imports, so plain-node tests can load it under Node's
 * TypeScript type stripping.
 *
 * puff puff pass
 */

export type EnvLike = Record<string, string | undefined>;

const HOST_RE =
  /^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$/;

/** The value, trimmed, if it is a plain hostname or IPv4 address; else null. */
export function plainLanHost(raw: string | undefined | null): string | null {
  const v = (raw ?? "").trim();
  return v && HOST_RE.test(v) ? v : null;
}

/**
 * True for a name that points back at this machine. Used to refuse a target
 * that would make a proxy forward to its own listener. A hostname that only
 * RESOLVES to loopback is not caught here (resolution happens later).
 */
export function isLoopbackHost(host: string): boolean {
  const h = host.toLowerCase();
  return (
    h === "localhost" ||
    h.endsWith(".localhost") ||
    h === "0.0.0.0" ||
    /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h)
  );
}

function port(raw: string | undefined, fallback: number): number | null {
  if (raw === undefined || raw.trim() === "") return fallback;
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 && n < 65536 ? n : null;
}

export interface ProxyTarget {
  readonly listenPort: number;
  readonly targetHost: string;
  readonly targetPort: number;
  /** Address retried when the host does not answer. Defaults to the host. */
  readonly fallbackAddr: string;
}

/** A start decision: a target, or the one-line reason the proxy stays off. */
export type ProxyDecision =
  | { readonly ok: true; readonly target: ProxyTarget }
  | { readonly ok: false; readonly reason: string };

function decide(
  hostVar: string,
  rawHost: string | undefined,
  rawFallback: string | undefined,
  listenPort: number | null,
  targetPort: number,
): ProxyDecision {
  if (!(rawHost ?? "").trim()) {
    return { ok: false, reason: `${hostVar} not set; not started` };
  }
  const targetHost = plainLanHost(rawHost);
  if (!targetHost) {
    return { ok: false, reason: `${hostVar} is not a plain hostname or IPv4 address; not started` };
  }
  if (listenPort === null) {
    return { ok: false, reason: "listen port is not a valid port; not started" };
  }
  let fallbackAddr = targetHost;
  if ((rawFallback ?? "").trim()) {
    const f = plainLanHost(rawFallback);
    if (!f) {
      return { ok: false, reason: "fallback address is not a plain hostname or IPv4 address; not started" };
    }
    fallbackAddr = f;
  }
  if (
    listenPort === targetPort &&
    (isLoopbackHost(targetHost) || isLoopbackHost(fallbackAddr))
  ) {
    return { ok: false, reason: "target is its own listener; not started" };
  }
  return { ok: true, target: { listenPort, targetHost, targetPort, fallbackAddr } };
}

/** Jellyfin: JELLYFIN_LAN_HOST (required), JELLYFIN_LAN_IP, JELLYFIN_PROXY_PORT. */
export function mediaProxyTarget(env: EnvLike): ProxyDecision {
  return decide(
    "JELLYFIN_LAN_HOST",
    env.JELLYFIN_LAN_HOST,
    env.JELLYFIN_LAN_IP,
    port(env.JELLYFIN_PROXY_PORT, 8096),
    8096,
  );
}

/** Calibre-web: CALIBRE_LAN_HOST (required), CALIBRE_PROXY_PORT. */
export function booksProxyTarget(env: EnvLike): ProxyDecision {
  return decide(
    "CALIBRE_LAN_HOST",
    env.CALIBRE_LAN_HOST,
    undefined,
    port(env.CALIBRE_PROXY_PORT, 8083),
    8083,
  );
}

/**
 * Jellyfin source list for the page CSP: always the loopback media proxy,
 * plus the LAN origin only when JELLYFIN_LAN_HOST is a valid plain host.
 */
export function jellyfinCspOrigins(env: EnvLike): string {
  const lan = plainLanHost(env.JELLYFIN_LAN_HOST);
  return lan && !isLoopbackHost(lan)
    ? `http://localhost:8096 http://${lan}:8096`
    : "http://localhost:8096";
}
