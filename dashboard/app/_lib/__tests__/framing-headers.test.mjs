/**
 * Unit test for the loopback proxy's framing-header filter. Plain Node — this
 * repo has no test runner, and stripFramingHeaders is pure.
 *
 *   npm run test:framing   (or: node app/_lib/__tests__/framing-headers.test.mjs)
 *
 * Requires Node >=22.6 for native TypeScript type stripping, like the other
 * tests here. Exits non-zero on any failure.
 *
 * What is being guarded: the filter is what makes the Books tile work at all
 * (Calibre-web sends X-Frame-Options: SAMEORIGIN), and it is also the blast
 * radius — anything it drops beyond the two framing headers is a protection
 * the framed app silently loses. So the assertions are as much about what
 * SURVIVES as about what goes.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const { stripFramingHeaders, hasFrameAncestors, rewriteBlankTargets } = await import(
  "../framingHeaders.ts"
);

let pass = 0;
let fail = 0;

function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    fail += 1;
    console.error(`FAIL  ${name}\n      ${err.message}`);
  }
}

/** Calibre-web's real /login response headers. */
const CALIBRE_LOGIN = {
  "content-type": "text/html; charset=utf-8",
  "content-length": "9021",
  "x-frame-options": "SAMEORIGIN",
  "content-security-policy": "default-src 'self'; object-src 'none'",
  "set-cookie": ["session=abc123; Path=/; HttpOnly", "remember_token=; Path=/"],
  vary: "Cookie",
};

check("drops X-Frame-Options", () => {
  const out = stripFramingHeaders(CALIBRE_LOGIN);
  assert.equal("x-frame-options" in out, false);
});

check("keeps a CSP that has no frame-ancestors directive", () => {
  const out = stripFramingHeaders(CALIBRE_LOGIN);
  assert.equal(out["content-security-policy"], "default-src 'self'; object-src 'none'");
});

check("passes every unrelated header through untouched", () => {
  const out = stripFramingHeaders(CALIBRE_LOGIN);
  assert.equal(out["content-type"], "text/html; charset=utf-8");
  assert.equal(out["content-length"], "9021");
  assert.equal(out.vary, "Cookie");
  assert.deepEqual(out["set-cookie"], CALIBRE_LOGIN["set-cookie"]);
});

check("matches header names case-insensitively", () => {
  const out = stripFramingHeaders({
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "CONTENT-SECURITY-POLICY-REPORT-ONLY": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
  });
  assert.deepEqual(Object.keys(out), ["X-Content-Type-Options"]);
});

check("keeps the original key casing of survivors", () => {
  const out = stripFramingHeaders({ "Content-Type": "application/epub+zip" });
  assert.deepEqual(out, { "Content-Type": "application/epub+zip" });
});

check("drops a CSP whose frame-ancestors sits among other directives", () => {
  const out = stripFramingHeaders({
    "content-security-policy": "default-src 'self'; frame-ancestors 'self'; img-src *",
  });
  assert.equal("content-security-policy" in out, false);
});

check("matches the frame-ancestors directive case-insensitively", () => {
  assert.equal(hasFrameAncestors("Frame-Ancestors 'NONE'"), true);
  const out = stripFramingHeaders({ "content-security-policy": "FRAME-ANCESTORS 'none'" });
  assert.deepEqual(out, {});
});

check("drops a repeated CSP when any one copy refuses framing", () => {
  const out = stripFramingHeaders({
    "content-security-policy": ["default-src 'self'", "frame-ancestors 'none'"],
  });
  assert.equal("content-security-policy" in out, false);
});

check("drops report-only CSP only when it carries frame-ancestors", () => {
  const kept = stripFramingHeaders({
    "content-security-policy-report-only": "default-src 'self'; report-uri /csp",
  });
  assert.equal(
    kept["content-security-policy-report-only"],
    "default-src 'self'; report-uri /csp",
  );
  const dropped = stripFramingHeaders({
    "content-security-policy-report-only": "frame-ancestors https://evil.example",
  });
  assert.deepEqual(dropped, {});
});

check("does not mutate the caller's header bag", () => {
  const input = { "x-frame-options": "SAMEORIGIN", "content-type": "text/html" };
  stripFramingHeaders(input);
  assert.equal(input["x-frame-options"], "SAMEORIGIN");
});

check("undefined / null / empty inputs are an empty bag, not a throw", () => {
  assert.deepEqual(stripFramingHeaders(undefined), {});
  assert.deepEqual(stripFramingHeaders(null), {});
  assert.deepEqual(stripFramingHeaders({}), {});
  assert.equal(hasFrameAncestors(undefined), false);
});

check("a header named like frame-ancestors is not special-cased away", () => {
  // Only the two CSP headers get the conditional treatment; an unrelated
  // header that merely mentions the word must survive.
  const out = stripFramingHeaders({ "x-debug-note": "frame-ancestors was here" });
  assert.equal(out["x-debug-note"], "frame-ancestors was here");
});

/* ---------------------------------------------------------------------------
 * rewriteBlankTargets — what keeps the kiosk out of an unclosable window.
 * ------------------------------------------------------------------------ */

check("rewrites the double-quoted form", () => {
  assert.equal(
    rewriteBlankTargets('<a href="/read/42/epub" target="_blank">Read in Browser</a>'),
    '<a href="/read/42/epub" target="_self">Read in Browser</a>',
  );
});

check("rewrites the single-quoted form", () => {
  assert.equal(
    rewriteBlankTargets("<a href='/book/42' target='_blank'>Book</a>"),
    '<a href=\'/book/42\' target="_self">Book</a>',
  );
});

check("rewrites the unquoted form, and whitespace around the equals", () => {
  assert.equal(
    rewriteBlankTargets("<a href=/x target=_blank>x</a>"),
    '<a href=/x target="_self">x</a>',
  );
  assert.equal(
    rewriteBlankTargets('<a target = "_blank" href="/y">y</a>'),
    '<a target="_self" href="/y">y</a>',
  );
});

check("matches attribute and value case-insensitively", () => {
  assert.equal(
    rewriteBlankTargets('<a TARGET="_BLANK" href="/z">z</a>'),
    '<a target="_self" href="/z">z</a>',
  );
});

check("leaves a named target alone", () => {
  const html = '<a href="/x" target="myframe">x</a><a href="/y" target=_blankly>y</a>';
  assert.equal(rewriteBlankTargets(html), html);
});

check("leaves rel and every other attribute alone", () => {
  assert.equal(
    rewriteBlankTargets(
      '<a rel="noopener noreferrer" href="/read/42" target="_blank" data-x="_blank!">r</a>',
    ),
    '<a rel="noopener noreferrer" href="/read/42" target="_self" data-x="_blank!">r</a>',
  );
});

check("returns a body with no targets unchanged", () => {
  const html = "<html><body><h1>Books</h1><a href='/book/1'>One</a></body></html>";
  assert.equal(rewriteBlankTargets(html), html);
  assert.equal(rewriteBlankTargets(""), "");
});

check("rewrites every occurrence, not just the first", () => {
  // detail.html has five of them; a non-global regex would leave four live.
  const out = rewriteBlankTargets(
    '<a target="_blank">1</a><a target="_blank">2</a><a target=\'_blank\'>3</a>',
  );
  assert.equal(out.includes("_blank"), false);
  assert.equal(out.match(/target="_self"/g).length, 3);
  // Stateful-regex guard: the same input twice must give the same output.
  assert.equal(rewriteBlankTargets('<a target="_blank">1</a>'), '<a target="_self">1</a>');
  assert.equal(rewriteBlankTargets('<a target="_blank">1</a>'), '<a target="_self">1</a>');
});


// ── The CSP the middleware actually sends ────────────────────────────────
//
// The knowledge viewer injects third-party library HTML (ZIM packs) with
// dangerouslySetInnerHTML. default-src 'self' covers most of the blast
// radius but governs NEITHER base-uri NOR form-action, so an injected
// <base> or an absolute form action would escape the policy entirely.
// These read the middleware source rather than booting Next: the value is
// a literal, and a missing directive is what we care about.

const middlewareSource = await readFile(
  new URL("../../../middleware.ts", import.meta.url),
  "utf8",
);

check("the middleware CSP pins base-uri and form-action to 'self'", () => {
  assert.match(middlewareSource, /base-uri 'self'/);
  assert.match(middlewareSource, /form-action 'self'/);
});

check("the directives the dashboard already relied on are still there", () => {
  for (const directive of [
    "default-src 'self'",
    "frame-ancestors 'self'",
    "font-src 'self'",
    "worker-src 'self' blob:",
  ]) {
    assert.ok(
      middlewareSource.includes(directive),
      `CSP lost ${directive}`,
    );
  }
});

check("the CSP never opens base-uri or form-action to a wildcard", () => {
  assert.equal(/base-uri[^;"]*\*/.test(middlewareSource), false);
  assert.equal(/form-action[^;"]*\*/.test(middlewareSource), false);
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
