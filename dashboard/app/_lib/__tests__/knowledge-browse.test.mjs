/**
 * Unit test for the library browser's pure logic. Plain Node — this repo has
 * no test runner, and knowledgeBrowse.ts is DOM-free on purpose.
 *
 *   npm run test:knowledgebrowse
 *
 * Requires Node >=22.6 for native TypeScript type stripping, like the other
 * tests here. Exits non-zero on any failure.
 *
 * What is being guarded: Back/Forward must behave like a browser (a new link
 * from a Back-ed page forks the future), the stack must not grow without
 * bound on a wall kiosk that never reloads, and a click on the <b> inside
 * <a><b>Cheese</b></a> must still resolve to the anchor — that last one is
 * the whole reason internal links work at all.
 */
import assert from "node:assert/strict";

const {
  HISTORY_CAP,
  historyInit,
  historyPush,
  historyBack,
  historyForward,
  canGoBack,
  canGoForward,
  currentEntry,
  normalizePrefix,
  linkTargetFromClick,
  isNavigableArticleHref,
  articleFragmentId,
  ARTICLE_BOX_STYLE,
  shouldPreferBrowse,
  titleFromZimUrl,
  safeDecodeURIComponent,
  rewriteArticleAssetUrls,
} = await import("../knowledgeBrowse.ts");

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

const entry = (t) => ({ url: `C/${t}`, title: t });

/* ── history stack ───────────────────────────────────────────── */

check("empty history has nothing current and no moves", () => {
  const h = historyInit();
  assert.equal(currentEntry(h), null);
  assert.equal(canGoBack(h), false);
  assert.equal(canGoForward(h), false);
});

check("push makes the new entry current", () => {
  const h = historyPush(historyPush(historyInit(), entry("Cheese")), entry("Brie"));
  assert.equal(currentEntry(h).title, "Brie");
  assert.equal(h.entries.length, 2);
  assert.equal(canGoBack(h), true);
  assert.equal(canGoForward(h), false);
});

check("back walks to the previous entry and enables forward", () => {
  let h = historyPush(historyPush(historyInit(), entry("Cheese")), entry("Brie"));
  h = historyBack(h);
  assert.equal(currentEntry(h).title, "Cheese");
  assert.equal(canGoBack(h), false);
  assert.equal(canGoForward(h), true);
});

check("forward returns to where back came from", () => {
  let h = historyPush(historyPush(historyInit(), entry("Cheese")), entry("Brie"));
  h = historyForward(historyBack(h));
  assert.equal(currentEntry(h).title, "Brie");
  assert.equal(canGoForward(h), false);
});

check("back at the start and forward at the end are no-ops", () => {
  const h = historyPush(historyInit(), entry("Cheese"));
  assert.equal(historyBack(h), h);
  assert.equal(historyForward(h), h);
  assert.equal(currentEntry(historyBack(h)).title, "Cheese");
  assert.equal(currentEntry(historyBack(historyInit())), null);
});

check("a push after back clears the forward branch", () => {
  let h = historyPush(historyPush(historyInit(), entry("Cheese")), entry("Brie"));
  h = historyBack(h);
  h = historyPush(h, entry("Cheddar"));
  assert.deepEqual(
    h.entries.map((e) => e.title),
    ["Cheese", "Cheddar"],
  );
  assert.equal(canGoForward(h), false);
  assert.equal(currentEntry(h).title, "Cheddar");
});

check("the stack caps at HISTORY_CAP, dropping the oldest", () => {
  let h = historyInit();
  for (let i = 0; i < HISTORY_CAP + 10; i += 1) h = historyPush(h, entry(`A${i}`));
  assert.equal(h.entries.length, HISTORY_CAP);
  assert.equal(h.index, HISTORY_CAP - 1);
  assert.equal(h.entries[0].title, "A10");
  assert.equal(currentEntry(h).title, `A${HISTORY_CAP + 9}`);
});

check("push does not mutate the previous state", () => {
  const first = historyPush(historyInit(), entry("Cheese"));
  const second = historyPush(first, entry("Brie"));
  assert.equal(first.entries.length, 1);
  assert.equal(second.entries.length, 2);
});

/* ── prefix normalisation ────────────────────────────────────── */

check("prefix trims, collapses whitespace and capitalises", () => {
  assert.equal(normalizePrefix("  cheese "), "Cheese");
  assert.equal(normalizePrefix("new  york"), "New york");
  assert.equal(normalizePrefix("a"), "A");
  assert.equal(normalizePrefix("Étude"), "Étude");
});

check("a blank prefix normalises to the empty string", () => {
  assert.equal(normalizePrefix(""), "");
  assert.equal(normalizePrefix("   "), "");
  assert.equal(normalizePrefix(undefined), "");
});

check("prefix is capped so a paste cannot build an absurd query", () => {
  assert.equal(normalizePrefix("x".repeat(500)).length, 64);
});

/* ── click target extraction ─────────────────────────────────── */

/** Minimal stand-in for a DOM node: only what the walk touches. */
function node(tagName, attrs = {}, parent = null) {
  return {
    tagName,
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    parentElement: parent,
  };
}

check("a click on the anchor itself yields the zim target", () => {
  const a = node("A", { href: "#", "data-zim-url": "C/Cheese" });
  assert.deepEqual(linkTargetFromClick(a), { kind: "zim", url: "C/Cheese", fragment: null });
});

check("a click on nested markup inside the anchor still resolves", () => {
  const a = node("A", { href: "#", "data-zim-url": "C/Brie", "data-zim-fragment": "History" });
  const b = node("B", {}, a);
  const span = node("SPAN", {}, b);
  assert.deepEqual(linkTargetFromClick(span), {
    kind: "zim",
    url: "C/Brie",
    fragment: "History",
  });
});

check("an in-page anchor is reported as an anchor, not a navigation", () => {
  assert.deepEqual(linkTargetFromClick(node("A", { href: "#See_also" })), {
    kind: "anchor",
    fragment: "See_also",
  });
  // A bare "#" is the inert href the backend puts on data-zim links; on its
  // own it is nothing to act on.
  assert.equal(linkTargetFromClick(node("A", { href: "#" })), null);
});

check("absolute http(s) links are left for ExternalLinkGuard", () => {
  assert.deepEqual(linkTargetFromClick(node("A", { href: "https://ted.com/x" })), {
    kind: "external",
    href: "https://ted.com/x",
  });
});

check("script-bearing hrefs are never navigable, whatever their spelling", () => {
  for (const bad of [
    "javascript:alert(1)", "JavaScript:alert(1)", " javascript:alert(1)",
    "java\tscript:alert(1)", "java\nscript:x", "\u0001javascript:x",
    "vbscript:x", "data:text/html,x", "blob:https://x/y", "file:///etc/passwd",
  ]) {
    assert.equal(isNavigableArticleHref(bad), false, JSON.stringify(bad));
  }
  for (const good of ["#", "#Sec", "https://ted.com/x", "http://a/b",
                      "mailto:a@b.c", "C/Whale", "", null]) {
    assert.equal(isNavigableArticleHref(good), true, JSON.stringify(good));
  }
});

check("fragments map into the backend's zim- id namespace, idempotently", () => {
  assert.equal(articleFragmentId("History"), "zim-History");
  assert.equal(articleFragmentId("zim-History"), "zim-History");
  assert.equal(articleFragmentId("__NEXT_DATA__"), "zim-__NEXT_DATA__");
});

const nodeFs = await import("node:fs");
const nodePath = await import("node:path");
const { fileURLToPath: nodeFileURLToPath } = await import("node:url");

check("the article box contains everything the article paints", () => {
  assert.deepEqual(
    { ...ARTICLE_BOX_STYLE },
    { position: "relative", overflow: "hidden", contain: "paint", isolation: "isolate" },
  );
  // Both places that inject article HTML must apply it to that very element.
  const fs = nodeFs;
  const path = nodePath;
  const here = path.dirname(nodeFileURLToPath(import.meta.url));
  for (const rel of [
    ["..", "..", "dashboard", "knowledge", "page.tsx"],
    ["..", "..", "dashboard", "knowledge", "viewer", "page.tsx"],
  ]) {
    const src = fs.readFileSync(path.join(here, ...rel), "utf8");
    const injections = src.match(/<div[^>]*dangerouslySetInnerHTML=\{\{\s*__html:\s*selectedArticle\.html\s*\}\}[^>]*\/>/g) || [];
    assert.ok(injections.length >= 1, rel.join("/"));
    for (const tag of injections) {
      assert.ok(/style=\{ARTICLE_BOX_STYLE\}/.test(tag), `${rel.join("/")}: ${tag}`);
    }
  }
});

check("a click on plain text outside any anchor is nothing", () => {
  const p = node("P", {}, node("DIV", {}));
  assert.equal(linkTargetFromClick(p), null);
  assert.equal(linkTargetFromClick(null), null);
});

/* ── main page vs title index ────────────────────────────────── */

/** Link counts of the size a real ZIM index page produces. */
const zimLinks = (n) =>
  `<div>${'<a href="#" data-zim-url="C/X">x</a>'.repeat(n)}</div>`;

check("a dense wiki landing page is kept as the front door", () => {
  // Wikipedia 134, Simple Wikipedia 76, iFixit 24.
  assert.equal(shouldPreferBrowse(zimLinks(134)), false);
  assert.equal(shouldPreferBrowse(zimLinks(76)), false);
  assert.equal(shouldPreferBrowse(zimLinks(24)), false);
});

check("Gutenberg's datatables main page sends the reader to A-Z", () => {
  // C/Home is a JavaScript app: 3 links and a script that never runs here.
  assert.equal(shouldPreferBrowse(zimLinks(3)), true);
});

check("the threshold is five links", () => {
  assert.equal(shouldPreferBrowse(zimLinks(4)), true);
  assert.equal(shouldPreferBrowse(zimLinks(5)), false);
});

check("an empty or linkless main page prefers browsing", () => {
  assert.equal(shouldPreferBrowse(""), true);
  assert.equal(shouldPreferBrowse("<p>Welcome to the library.</p>"), true);
  // Attribute casing varies with the serialiser; the count must not.
  assert.equal(shouldPreferBrowse('<a DATA-ZIM-URL="C/A"></a>'.repeat(6)), false);
});

check("titles fall back to the zim path when nothing is loaded yet", () => {
  assert.equal(titleFromZimUrl("C/New_York_City"), "New York City");
  assert.equal(titleFromZimUrl("A/Cheese.html"), "Cheese");
  assert.equal(titleFromZimUrl(""), "");
});

/* ── asset URLs: a malformed % breaks one link, not the article ──────── */

check("safeDecodeURIComponent decodes, and returns the raw value on a bad %", () => {
  assert.equal(safeDecodeURIComponent("a%26b.png"), "a&b.png");
  assert.equal(safeDecodeURIComponent("100%.png"), "100%.png");
  assert.equal(safeDecodeURIComponent("%E0%A4%A"), "%E0%A4%A");
});

check("asset URLs move to the same-origin asset route", () => {
  assert.equal(
    rewriteArticleAssetUrls('<img src="/api/pai/knowledge/library/wiki/asset?path=I/a%26b.png">'),
    '<img src="/api/knowledge/asset?collection=wiki&path=I%2Fa%26b.png">',
  );
});

check("one malformed asset name does not throw or spoil its neighbours", () => {
  const html =
    '<img src="/api/pai/knowledge/library/wiki/asset?path=I/100%.png">' +
    '<img src="/api/pai/knowledge/library/wiki/asset?path=I/ok%20x.png">';
  const out = rewriteArticleAssetUrls(html);
  assert.ok(out.includes("path=I%2F100%25.png"), out);
  assert.ok(out.includes("path=I%2Fok%20x.png"), out);
  assert.ok(!out.includes("/api/pai/"), out);
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
