/**
 * The keyed proxy can actually relay a file upload.
 *
 *   npm run test:body
 *
 * Plain Node, no test runner, no Next. Exits non-zero on any failure.
 *
 * WHAT THIS GUARDS AGAINST
 * ------------------------
 * A proxy that forwards `req.body` (a ReadableStream) with `duplex: "half"`. On
 * Windows/Node that fails part-way through a multipart/form-data body, the
 * route's fetch rejects, and the handler's catch reports
 * `{"error":"PuffPAI backend unreachable"}`, so every document and photo
 * upload through the dashboard fails while the same request sent straight at
 * the backend with curl succeeds. Non-file POSTs are fine, which makes it look
 * like a backend problem rather than a proxy one.
 *
 * WHAT IS PROVEN HERE, AND WHAT IS NOT
 * ------------------------------------
 * A real loopback HTTP server stands in for the backend. The test builds a real
 * multipart body with FormData, runs it through readBodyCapped — the exact
 * function the route uses — and POSTs the buffered result the way the route
 * does. The stub then PARSES what arrived back into a FormData and compares the
 * file's name and bytes. So this covers the whole shape of the bug: a multipart
 * body surviving the hop with its boundary and content intact.
 *
 * It does NOT exercise the Next route handler itself (pairing gate, header
 * allowlist, path checks) — those have their own tests
 * (pairing-gate, proxy-headers). This one is about the body.
 *
 * puff puff pass
 */
import assert from "node:assert/strict";
import http from "node:http";
import { readBodyCapped } from "../requestBody.ts";

let pass = 0;
let fail = 0;
function ok(label, cond, detail = "") {
  if (cond) pass++;
  else fail++;
  console.log(`${cond ? "PASS" : "FAIL"}  ${label}${detail ? `  ${detail}` : ""}`);
}

/** The 50 MB the route uses, mirrored from the backend's own upload limit. */
const MAX_BODY_BYTES = 50 * 1024 * 1024;

/**
 * Stub upstream. Answers 200 with what it managed to parse out of the request,
 * so a body that arrived truncated or with a broken boundary shows up as a
 * parse failure rather than a pass.
 */
const server = http.createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks);
  const ct = req.headers["content-type"] || "";
  let parsed = null;
  try {
    const form = await new Response(raw, { headers: { "content-type": ct } }).formData();
    const file = form.get("file");
    parsed = {
      field: form.get("field"),
      filename: file && typeof file !== "string" ? file.name : null,
      content: file && typeof file !== "string" ? await file.text() : null,
    };
  } catch (e) {
    parsed = { parseError: String(e) };
  }
  res.writeHead(200, { "content-type": "application/json" });
  res.end(
    JSON.stringify({
      method: req.method,
      contentType: ct,
      contentLength: req.headers["content-length"] ?? null,
      bytes: raw.length,
      parsed,
    }),
  );
});

await new Promise((r) => server.listen(0, "127.0.0.1", r));
const BASE = `http://127.0.0.1:${server.address().port}`;

/**
 * Do what the route does: buffer the incoming body under the cap, then send it
 * upstream with the client's Content-Type (the boundary lives in there).
 */
async function proxyLike(incoming) {
  const read = await readBodyCapped(incoming, MAX_BODY_BYTES);
  if (read.status === "tooLarge") return { status: 413 };
  const upstream = await fetch(`${BASE}/knowledge/upload`, {
    method: incoming.method,
    headers: incoming.headers.get("content-type")
      ? { "Content-Type": incoming.headers.get("content-type") }
      : {},
    body: read.body,
  });
  return { status: upstream.status, json: await upstream.json() };
}

try {
  /* ── 1. multipart round-trip ──────────────────────────────────────────── */
  const CONTENT = "line one\nline two — with a dash and a ünicode char\n".repeat(40);
  const form = new FormData();
  form.set("field", "documents");
  form.set("file", new File([CONTENT], "notes.txt", { type: "text/plain" }));
  const multipart = new Request(`${BASE}/knowledge/upload`, { method: "POST", body: form });

  const r1 = await proxyLike(multipart);
  ok("multipart upload reaches the upstream", r1.status === 200, `status ${r1.status}`);
  ok(
    "upstream could parse the multipart body",
    !r1.json?.parsed?.parseError,
    r1.json?.parsed?.parseError || "",
  );
  ok("the file name survived", r1.json?.parsed?.filename === "notes.txt", String(r1.json?.parsed?.filename));
  ok("the file bytes survived exactly", r1.json?.parsed?.content === CONTENT);
  ok("the other form field survived", r1.json?.parsed?.field === "documents");
  ok(
    "the buffered body is framed with a Content-Length",
    r1.json?.contentLength !== null && Number(r1.json.contentLength) === r1.json.bytes,
    `declared ${r1.json?.contentLength}, received ${r1.json?.bytes}`,
  );

  /* ── 2. plain JSON POST still works ───────────────────────────────────── */
  const jsonReq = new Request(`${BASE}/chat/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: "hi" }),
  });
  const r2 = await proxyLike(jsonReq);
  ok("json POST reaches the upstream", r2.status === 200, `status ${r2.status}`);
  ok("json body arrived whole", r2.json?.bytes === Buffer.byteLength(JSON.stringify({ message: "hi" })));

  /* ── 3. bodyless request sends no body ────────────────────────────────── */
  const empty = await readBodyCapped({ body: null }, MAX_BODY_BYTES);
  ok(
    "a request with no body yields undefined, not an empty buffer",
    empty.status === "ok" && empty.body === undefined,
  );

  /* ── 4. the cap refuses rather than truncating ────────────────────────── */
  const big = new Request(`${BASE}/knowledge/upload`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: new Uint8Array(4096),
  });
  const capped = await readBodyCapped(big, 1024);
  ok("over the cap is refused", capped.status === "tooLarge");

  const under = new Request(`${BASE}/knowledge/upload`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: new Uint8Array(1024),
  });
  const exact = await readBodyCapped(under, 1024);
  ok(
    "a body exactly at the cap is allowed",
    exact.status === "ok" && exact.body?.byteLength === 1024,
  );

  /* ── 5. a chunked body with no declared length is still counted ────────
     This is the case the route's Content-Length check cannot see, and the
     reason the cap has to be enforced while reading. */
  const chunked = {
    body: new ReadableStream({
      start(controller) {
        for (let i = 0; i < 8; i++) controller.enqueue(new Uint8Array(512));
        controller.close();
      },
    }),
  };
  const chunkedResult = await readBodyCapped(chunked, 1024);
  ok("a chunked body over the cap is refused too", chunkedResult.status === "tooLarge");
} finally {
  server.close();
}

console.log(`\n${pass} passed, ${fail} failed`);
assert.equal(fail, 0, `${fail} proxy-body assertion(s) failed`);
