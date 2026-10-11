/**
 * Read a proxied request body into memory, with a hard ceiling.
 *
 * WHY THIS EXISTS
 * ---------------
 * Forwarding `req.body` (a web ReadableStream) with `duplex: "half"` is the
 * textbook way to relay a body without buffering it, and it works on Linux. On
 * the Windows/Node runtime, a STREAMED request body through undici fails
 * part-way for multipart/form-data: the route's `fetch` rejects and the
 * handler's catch turns it into `{"error":"PuffPAI backend unreachable"}`, so
 * every file upload through the proxy would 502 while the same request with
 * `curl` straight at the backend, and every non-file POST through the proxy,
 * works. The symptom is indistinguishable from a backend that is actually down.
 *
 * So the proxy buffers: hand `fetch` a Uint8Array, which undici frames with a
 * known Content-Length and no streaming handshake. The cost is memory, so the
 * cap is not optional — it is the same 50 MB the backend enforces on
 * /knowledge/upload (src/puffpai/api_server.py), so a body this accepts is a
 * body the backend would accept on size, and anything larger is refused HERE
 * with a clear 413 instead of being read into the dashboard's heap first.
 *
 * Chunked uploads with no declared Content-Length would slip past the route's
 * cheap header check and rely on the backend's own limit, so this counts bytes
 * as they arrive and stops at the cap.
 *
 * puff puff pass
 */

/** Outcome of a capped read. `tooLarge` is the caller's cue to answer 413. */
export type BodyReadResult =
  | { status: "ok"; body: Uint8Array<ArrayBuffer> | undefined }
  | { status: "tooLarge" };

/** Minimal shape read off a request, so this is testable without Next. */
export interface BodyCarrier {
  body: ReadableStream<Uint8Array> | null;
}

/**
 * Drain `req.body` into one Uint8Array, refusing at `maxBytes`.
 *
 * Returns `body: undefined` when the request has no body at all (a bodyless
 * POST), which the caller passes to fetch as `undefined` rather than an empty
 * buffer — an empty Uint8Array would declare `Content-Length: 0` on a request
 * that had no body, which some backends treat differently.
 *
 * The stream is cancelled on refusal so the socket is not left half-read.
 */
export async function readBodyCapped(
  req: BodyCarrier,
  maxBytes: number,
): Promise<BodyReadResult> {
  if (!req.body) return { status: "ok", body: undefined };

  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel().catch(() => {
          /* the client hung up first; nothing to release */
        });
        return { status: "tooLarge" };
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }

  // One allocation of the exact size rather than repeated concatenation: a
  // 50 MB photo copied on every chunk would be quadratic.
  const out = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.byteLength;
  }
  return { status: "ok", body: out };
}
