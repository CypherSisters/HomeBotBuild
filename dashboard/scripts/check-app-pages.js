#!/usr/bin/env node
/**
 * Guard: an app page reaches nothing.
 *
 * The five `public/*-app.html` pages are static HTML the browser runs. Anything
 * they fetch, the BROWSER fetches — which means no door, no ledger and no PIN
 * on the appliance ever sees it. A free-text "Endpoint URL" input, persisted to
 * localStorage and feeding model calls, under a panel that says "runs locally
 * on your machine", would let the page be pointed at any host on the internet
 * without the appliance knowing.
 *
 * The rule that makes that panel true by construction is simple enough to
 * grep for: an app page contains NO absolute URL. Not a model port, not a CDN,
 * not a font host, not even the appliance's own `http://localhost:8000` — that
 * one would work only because the API exempts localhost callers, and it would
 * put the page outside the same-origin proxy that holds the API key
 * server-side.
 *
 * Everything those pages need is same-origin: `/api/pai/...` for the API and
 * `/vendor/...` for fonts and scripts, both vendored into this repo.
 *
 * Run: node scripts/check-app-pages.js
 * Exits non-zero and prints file:line for every offender.
 */

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const PUBLIC = path.join(ROOT, "public");

/** An absolute URL of any scheme, plus the model ports by number. */
const OFFENDER = /(https?:\/\/|\b1143[45]\b)/i;

function targets() {
  const out = [];
  for (const name of fs.readdirSync(PUBLIC)) {
    if (name.endsWith("-app.html")) out.push(path.join(PUBLIC, name));
  }
  const fonts = path.join(PUBLIC, "vendor", "fonts");
  if (fs.existsSync(fonts)) {
    for (const name of fs.readdirSync(fonts)) {
      if (name.endsWith(".css")) out.push(path.join(fonts, name));
    }
  }
  return out;
}

const files = targets();
let bad = 0;

if (files.length === 0) {
  console.error("FAIL: no *-app.html found under public/. This guard is not running.");
  process.exit(1);
}

for (const file of files) {
  const rel = path.relative(ROOT, file).replace(/\\/g, "/");
  const lines = fs.readFileSync(file, "utf8").split("\n");
  lines.forEach((line, idx) => {
    if (OFFENDER.test(line)) {
      bad++;
      console.error(`${rel}:${idx + 1}: ${line.trim().slice(0, 140)}`);
    }
  });
}

if (bad) {
  console.error(
    `\nFAIL: ${bad} absolute URL(s) in app pages across ${files.length} files. ` +
      `Use /api/pai/... for the API and /vendor/... for assets.`,
  );
  process.exit(1);
}
console.log(`OK: ${files.length} app page/asset files reach nothing off this origin.`);
