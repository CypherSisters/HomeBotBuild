/**
 * Unit test for the books deep-link target builder. Plain Node — this repo has
 * no test runner, and calibreTargetPath is pure.
 *
 *   npm run test:bookstarget   (or: node app/_lib/__tests__/books-target.test.mjs)
 *
 * Requires Node >=22.6 for native TypeScript type stripping, like the other
 * tests here. Exits non-zero on any failure.
 *
 * What is being guarded: the ?book=&fmt= query on /dashboard/media/books comes
 * from the "show me" queue, not from a link the owner clicked, and its output
 * is concatenated onto an origin and handed to an iframe. So the assertions are
 * mostly about junk NOT surviving: a dropped value must land on the catalogue
 * or the detail page, never on a crafted path.
 */
import assert from "node:assert/strict";

const { calibreTargetPath } = await import("../booksTarget.ts");

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

check("no params is the catalogue", () => {
  assert.equal(calibreTargetPath({}), "/");
  assert.equal(calibreTargetPath({ book: null, fmt: null }), "/");
  assert.equal(calibreTargetPath({ book: undefined }), "/");
  // fmt without a book is meaningless, not a reader URL.
  assert.equal(calibreTargetPath({ fmt: "epub" }), "/");
});

check("book alone is the detail page", () => {
  assert.equal(calibreTargetPath({ book: "1" }), "/book/1");
  assert.equal(calibreTargetPath({ book: "4211" }), "/book/4211");
  assert.equal(calibreTargetPath({ book: "100000000" }), "/book/100000000");
});

check("book + format opens the reader", () => {
  assert.equal(calibreTargetPath({ book: "7", fmt: "epub" }), "/read/7/epub");
  assert.equal(calibreTargetPath({ book: "7", fmt: "cbr" }), "/read/7/cbr");
  assert.equal(calibreTargetPath({ book: "7", fmt: "cbz" }), "/read/7/cbz");
  assert.equal(calibreTargetPath({ book: "7", fmt: "cb7" }), "/read/7/cb7");
  assert.equal(calibreTargetPath({ book: "7", fmt: "pdf" }), "/read/7/pdf");
  assert.equal(calibreTargetPath({ book: "7", fmt: "txt" }), "/read/7/txt");
  assert.equal(calibreTargetPath({ book: "7", fmt: "djvu" }), "/read/7/djvu");
});

check("format is lower-cased and trimmed", () => {
  assert.equal(calibreTargetPath({ book: "7", fmt: "EPUB" }), "/read/7/epub");
  assert.equal(calibreTargetPath({ book: "7", fmt: " Cbz " }), "/read/7/cbz");
});

check("path traversal in book is dropped, not escaped", () => {
  assert.equal(calibreTargetPath({ book: "1/../../admin" }), "/");
  assert.equal(calibreTargetPath({ book: "../../admin" }), "/");
  assert.equal(calibreTargetPath({ book: "1%2F..%2Fadmin" }), "/");
  assert.equal(calibreTargetPath({ book: "//evil.example.com" }), "/");
});

check("query/fragment smuggling in fmt falls back to the detail page", () => {
  assert.equal(calibreTargetPath({ book: "7", fmt: "epub?x=1" }), "/book/7");
  assert.equal(calibreTargetPath({ book: "7", fmt: "epub#/../admin" }), "/book/7");
  assert.equal(calibreTargetPath({ book: "7", fmt: "epub/../../admin" }), "/book/7");
  assert.equal(calibreTargetPath({ book: "7", fmt: ".epub" }), "/book/7");
});

check("unknown or empty formats fall back to the detail page", () => {
  assert.equal(calibreTargetPath({ book: "7", fmt: "" }), "/book/7");
  assert.equal(calibreTargetPath({ book: "7", fmt: "mobi" }), "/book/7");
  assert.equal(calibreTargetPath({ book: "7", fmt: "html" }), "/book/7");
});

check("negative, zero and float ids are dropped", () => {
  assert.equal(calibreTargetPath({ book: "-1" }), "/");
  assert.equal(calibreTargetPath({ book: "0" }), "/");
  assert.equal(calibreTargetPath({ book: "1.5" }), "/");
  assert.equal(calibreTargetPath({ book: "+3" }), "/");
  assert.equal(calibreTargetPath({ book: "1e3" }), "/");
  assert.equal(calibreTargetPath({ book: " 7 " }), "/");
});

check("out-of-range and non-numeric ids are dropped", () => {
  assert.equal(calibreTargetPath({ book: "100000001" }), "/");
  assert.equal(calibreTargetPath({ book: "99999999999999999999" }), "/");
  assert.equal(calibreTargetPath({ book: "abc" }), "/");
  assert.equal(calibreTargetPath({ book: "7abc" }), "/");
  assert.equal(calibreTargetPath({ book: "" }), "/");
});

check("non-string params never throw", () => {
  assert.equal(calibreTargetPath({ book: 7 }), "/");
  assert.equal(calibreTargetPath({ book: "7", fmt: 1 }), "/book/7");
  assert.equal(calibreTargetPath(undefined), "/");
});

check("every result is a rooted path with no scheme or authority", () => {
  for (const params of [
    {},
    { book: "7" },
    { book: "7", fmt: "cbz" },
    { book: "javascript:alert(1)" },
    { book: "7", fmt: "javascript:alert(1)" },
  ]) {
    const out = calibreTargetPath(params);
    assert.equal(out.startsWith("/"), true, `${out} is not rooted`);
    assert.equal(out.startsWith("//"), false, `${out} is protocol-relative`);
    assert.equal(/^[/a-z0-9]+$/.test(out), true, `${out} has unexpected characters`);
  }
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
