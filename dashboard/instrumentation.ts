import { rewriteBlankTargets, stripFramingHeaders } from "@/app/_lib/framingHeaders";
import { booksProxyTarget, mediaProxyTarget } from "@/app/_lib/server/lanTargets";

/**
 * Runs once when the Next.js server boots. Two independent loopback proxies,
 * each with its own start guard so one failing or being disabled never keeps
 * the other from listening.
 *
 * Neither proxy has a default target: each stays off until its *_LAN_HOST is
 * set to a plain hostname or IPv4 address (see app/_lib/server/lanTargets.ts).
 *
 * media-proxy: forwards 127.0.0.1:8096 → Jellyfin at JELLYFIN_LAN_HOST:8096
 * (TCP, so HTTP + WebSocket both work). Jellyfin publishes on the LAN address
 * only, since this proxy holds 127.0.0.1:8096. The kiosk's Media page iframes
 * http://localhost:8096 instead of a LAN address because 2026 Chrome/Edge
 * "Local Network Access" enforcement silently aborts iframe navigations to
 * private-network/.local origins (fetch works, framing doesn't). Loopback
 * targets are exempt, and a same-machine iframe also inherits the kiosk's
 * autoplay allowance reliably.
 *
 * The target hostname is re-resolved in the background (30s TTL) so an
 * address change won't strand the proxy, but connections never wait on a
 * lookup — they use the cached address immediately (a per-connection mDNS
 * lookup added ~250ms to every request and a flaky lookup could make the
 * Jellyfin web app boot logged-out). If a connect fails or hangs, we retry
 * once against JELLYFIN_LAN_IP (the host itself when that is unset). With an
 * IP-literal host the lookup is a no-op and the cached address is the target.
 */
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  await startMediaProxy();
  await startCalibreProxy();
}

async function startMediaProxy() {
  const g = globalThis as { __puffpaiMediaProxy?: boolean };
  if (g.__puffpaiMediaProxy) return;
  g.__puffpaiMediaProxy = true;

  const decision = mediaProxyTarget(process.env);
  if (!decision.ok) {
    console.log(`[media-proxy] ${decision.reason}`);
    return;
  }
  const {
    listenPort: LISTEN_PORT,
    targetHost: TARGET_HOST,
    targetPort: TARGET_PORT,
    fallbackAddr: FALLBACK_IP,
  } = decision.target;

  const net = await import("node:net");
  const dns = await import("node:dns");

  const RESOLVE_TTL_MS = 30_000;
  let cachedAddr = FALLBACK_IP;
  let lastResolve = 0;
  const refreshAddr = () => {
    if (Date.now() - lastResolve < RESOLVE_TTL_MS) return;
    lastResolve = Date.now();
    dns.lookup(TARGET_HOST, { family: 4 }, (err, address) => {
      if (!err && address) cachedAddr = address;
    });
  };
  refreshAddr();

  const handleClient = (client: import("node:net").Socket) => {
    refreshAddr();
    client.setNoDelay(true);
    client.on("error", () => client.destroy());
    const connect = (addr: string, retryLeft: boolean) => {
      const upstream = net.connect(TARGET_PORT, addr);
      upstream.setNoDelay(true);
      let connected = false;
      upstream.setTimeout(4000);
      upstream.on("timeout", () => upstream.destroy(new Error("connect timeout")));
      upstream.on("connect", () => {
        connected = true;
        upstream.setTimeout(0);
        client.pipe(upstream);
        upstream.pipe(client);
      });
      upstream.on("error", () => {
        upstream.destroy();
        if (!connected && retryLeft && addr !== FALLBACK_IP) {
          connect(FALLBACK_IP, false);
        } else {
          client.destroy();
        }
      });
      client.on("close", () => upstream.destroy());
    };
    connect(cachedAddr, true);
  };

  // Listen on BOTH loopback stacks: browsers/clients resolving "localhost"
  // try ::1 first, and an IPv4-only listener costs them a ~200ms
  // happy-eyeballs fallback on every new connection.
  for (const host of ["127.0.0.1", "::1"]) {
    const server = net.createServer(handleClient);
    server.on("error", (err: NodeJS.ErrnoException) => {
      // EADDRINUSE = something else (e.g. a netsh portproxy) already forwards
      // the port — that's fine, the iframe URL still works.
      console.warn(`[media-proxy] ${host} not started: ${err.code}`);
    });
    server.listen(LISTEN_PORT, host, () => {
      console.log(
        `[media-proxy] ${host}:${LISTEN_PORT} → ${TARGET_HOST}:${TARGET_PORT}`,
      );
    });
  }
}

/**
 * books-proxy: forwards 127.0.0.1:8083 → Calibre-web at CALIBRE_LAN_HOST:8083
 * (optional; not started when that is unset), so the kiosk's Books page can
 * iframe http://localhost:8083.
 *
 * Two reasons it is a proxy and not a direct iframe of the LAN address:
 *
 * 1. Calibre-web answers with `X-Frame-Options: SAMEORIGIN`, so no origin but
 *    its own may frame it. This proxy removes that header (and a CSP
 *    `frame-ancestors`, if a future version grows one) on the way back —
 *    see app/_lib/framingHeaders.ts, which is where the filtering lives and is
 *    unit-tested.
 * 2. Same Local Network Access rule as the media proxy above: 2026 Chrome/Edge
 *    silently abort iframe navigations to private-IP origins, loopback is
 *    exempt.
 *
 * LOOPBACK ONLY — THIS IS THE SECURITY BOUNDARY.
 * Dropping X-Frame-Options is safe here and nowhere else: the listener binds
 * 127.0.0.1 and ::1, so the only page that can load an unprotected Calibre-web
 * response is one already running on the appliance itself, i.e. the kiosk. The
 * moment this binds 0.0.0.0 or a LAN interface, every browser on the network
 * (and every page it visits, via an iframe) gets a clickjacking target aimed at
 * the household's library and its session cookie. CALIBRE_LAN_HOST moves the
 * TARGET, never the listen address; there is deliberately no env var for the
 * listen address.
 *
 * It also rewrites target="_blank" out of the HTML it forwards: inside the
 * kiosk's iframe those links ("Read in Browser", "Listen in Browser", the
 * format downloads) would open an unclosable top-level window in Chromium
 * --kiosk, and the dashboard's own link containment cannot reach clicks inside
 * a cross-document iframe. See rewriteBlankTargets.
 *
 * Plain HTTP only: Calibre-web has no WebSocket surface, so there is no
 * 'upgrade' handling here (an upgrade request just gets a normal response from
 * upstream). Unlike the TCP media proxy this has to be an HTTP proxy, because
 * rewriting response headers means parsing the protocol.
 */
async function startCalibreProxy() {
  const g = globalThis as { __puffpaiCalibreProxy?: boolean };
  if (g.__puffpaiCalibreProxy) return;
  g.__puffpaiCalibreProxy = true;

  // No default target: unset or malformed CALIBRE_LAN_HOST, or a loopback
  // target on the listen port, means the books proxy stays off.
  const decision = booksProxyTarget(process.env);
  if (!decision.ok) {
    console.log(`[books-proxy] ${decision.reason}`);
    return;
  }
  const {
    listenPort: LISTEN_PORT,
    targetHost: TARGET_HOST,
    targetPort: TARGET_PORT,
  } = decision.target;

  const http = await import("node:http");
  const CONNECT_TIMEOUT_MS = 5000;
  // Generous on purpose: this bounds a hung upstream, not a slow page.
  const FIRST_BYTE_TIMEOUT_MS = 180_000;

  /** Hop-by-hop headers: they describe THIS connection, not the message, and
   *  copying them across a proxy corrupts framing (a forwarded
   *  `transfer-encoding: chunked` would label an already-dechunked body). */
  const HOP_BY_HOP = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
  ];
  /** Above this, give up on rewriting and stream. A Calibre-web page is tens
   *  of KB; 4 MB means it is not a page, and buffering it would be memory the
   *  appliance does not have to spare. */
  const MAX_REWRITE_BYTES = 4 * 1024 * 1024;

  /**
   * Only plain, unencoded, successful HTML gets buffered and rewritten.
   * Everything else — epubs, covers, JSON, redirects, 404s, gzip — streams
   * through byte-for-byte as before.
   *
   * The charset check is not pedantry: the rewrite decodes as UTF-8 and
   * re-encodes as UTF-8, so a page that declared some other charset would come
   * out corrupted. Calibre-web is UTF-8 throughout; anything that says
   * otherwise is left alone rather than guessed at.
   */
  const shouldRewrite = (
    status: number,
    headers: import("node:http").IncomingHttpHeaders,
  ) => {
    if (status < 200 || status >= 300) return false;
    if (headers["content-encoding"]) return false;
    const ct = String(headers["content-type"] ?? "").toLowerCase();
    if (!ct.startsWith("text/html")) return false;
    const charset = /charset\s*=\s*"?([\w-]+)/.exec(ct);
    return !charset || charset[1] === "utf-8" || charset[1] === "utf8";
  };

  const dropHopByHop = (headers: Record<string, unknown>) => {
    for (const key of Object.keys(headers)) {
      if (HOP_BY_HOP.indexOf(key.toLowerCase()) >= 0) delete headers[key];
    }
    return headers;
  };

  const handleRequest = (
    req: import("node:http").IncomingMessage,
    res: import("node:http").ServerResponse,
  ) => {
    const headers = dropHopByHop({ ...req.headers }) as import("node:http").OutgoingHttpHeaders;
    // Calibre-web builds redirects and cookie domains from Host; point it at
    // itself rather than leaking "localhost:8083" into its own links.
    headers.host = `${TARGET_HOST}:${TARGET_PORT}`;

    const upstream = http.request(
      {
        host: TARGET_HOST,
        port: TARGET_PORT,
        method: req.method,
        path: req.url,
        headers,
      },
      (up) => {
        // Response started: stop the connect watchdog so a long download or a
        // slow page render is not killed mid-flight.
        upstream.setTimeout(0);
        const safe = dropHopByHop(
          stripFramingHeaders(up.headers) as Record<string, unknown>,
        ) as import("node:http").OutgoingHttpHeaders;
        const status = up.statusCode ?? 502;
        up.on("error", () => res.destroy());

        if (!shouldRewrite(status, up.headers)) {
          res.writeHead(status, safe);
          up.pipe(res);
          return;
        }

        // HTML page: buffer it so target="_blank" can be rewritten (see
        // rewriteBlankTargets — a _blank inside the kiosk's iframe opens an
        // unclosable top-level window). Pages are tens of KB; anything that
        // grows past the cap is not a page we need to rewrite, so it gives up
        // and streams rather than holding the box's memory.
        const chunks: Buffer[] = [];
        let size = 0;
        let streaming = false;
        up.on("data", (chunk: Buffer) => {
          if (streaming) return;
          chunks.push(chunk);
          size += chunk.length;
          if (size <= MAX_REWRITE_BYTES) return;
          streaming = true;
          res.writeHead(status, safe);
          res.write(Buffer.concat(chunks));
          chunks.length = 0;
          up.pipe(res); // takes over from the next chunk on, backpressure included
        });
        up.on("end", () => {
          if (streaming) return;
          const body = Buffer.from(
            rewriteBlankTargets(Buffer.concat(chunks).toString("utf8")),
            "utf8",
          );
          // The rewrite changes the byte count, so the old length must go.
          for (const key of Object.keys(safe)) {
            if (key.toLowerCase() === "content-length") delete safe[key];
          }
          safe["content-length"] = String(body.length);
          res.writeHead(status, safe);
          res.end(body);
        });
      },
    );

    // Two separate clocks. CONNECT_TIMEOUT_MS only covers reaching Calibre-web;
    // once the socket is connected the wait is for the page to be GENERATED,
    // which is slow on a big library (the Categories page renders ~40k tags and
    // takes well over five seconds). A single idle timer treated that wait as
    // "unreachable" and 502'd a healthy server.
    upstream.setTimeout(CONNECT_TIMEOUT_MS, () =>
      upstream.destroy(new Error("connect timeout")),
    );
    upstream.on("socket", (socket) => {
      const connected = () => upstream.setTimeout(FIRST_BYTE_TIMEOUT_MS);
      if (socket.connecting) socket.once("connect", connected);
      else connected();
    });
    upstream.on("error", () => {
      if (!res.headersSent) {
        res.writeHead(502, { "content-type": "text/plain; charset=utf-8" });
      }
      res.end("Calibre-web is not reachable.\n");
    });

    req.on("error", () => upstream.destroy());
    res.on("close", () => upstream.destroy());
    req.pipe(upstream);
  };

  // Both loopback stacks, for the same happy-eyeballs reason as the media
  // proxy. Never a wildcard or LAN address — see the header comment.
  for (const host of ["127.0.0.1", "::1"]) {
    const server = http.createServer(handleRequest);
    server.on("error", (err: NodeJS.ErrnoException) => {
      console.warn(`[books-proxy] ${host} not started: ${err.code}`);
    });
    server.listen(LISTEN_PORT, host, () => {
      console.log(
        `[books-proxy] ${host}:${LISTEN_PORT} → ${TARGET_HOST}:${TARGET_PORT}`,
      );
    });
  }
}
