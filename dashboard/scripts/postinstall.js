/**
 * Postinstall script for PuffPAI dashboard.
 * 1. Copies talkinghead playback-worklet.js to public/ (served at /playback-worklet.js)
 * 2. Patches talkinghead's dynamic lipsync import() to use a static import map
 *    so bundlers (Turbopack, webpack) don't choke on the non-literal import.
 * 3. Rewrites talkinghead's AudioWorklet URL to load the worklet from the
 *    public/ copy instead of `new URL('./playback-worklet.js', import.meta.url)`.
 *    Turbopack statically analyses that pattern and tries to resolve the worklet
 *    as a bundled module, which fails the whole build:
 *      talkinghead.mjs:36  Module not found: Can't resolve './playback-worklet.js'
 *    Pointing at the static /playback-worklet.js path (step 1's copy) keeps the
 *    file out of the module graph, so the build passes with NO turbopackIgnore hack.
 *
 * Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
 */
const fs = require("fs");
const path = require("path");

const TALKINGHEAD_MJS = path.join(
  __dirname,
  "..",
  "node_modules",
  "talkinghead",
  "modules",
  "talkinghead.mjs"
);

// Step 1: Copy playback worklet
const workletSrc = path.join(
  __dirname,
  "..",
  "node_modules",
  "talkinghead",
  "modules",
  "playback-worklet.js"
);
const workletDst = path.join(__dirname, "..", "public", "playback-worklet.js");
if (fs.existsSync(workletSrc)) {
  fs.copyFileSync(workletSrc, workletDst);
  console.log("[postinstall] Copied playback-worklet.js to public/");
}

// Step 2 & 3: Patch talkinghead. Both patches are idempotent and independent
// (each guards on its own marker) so a re-run never skips one because the other
// already applied.
if (fs.existsSync(TALKINGHEAD_MJS)) {
  let src = fs.readFileSync(TALKINGHEAD_MJS, "utf8");
  let changed = false;

  // Idempotency markers. Each guard must match the text the corresponding
  // patch actually writes, so the marker is stored WITHOUT comment syntax
  // (`//` vs `/* */`) — the patched source may wrap it either way.
  const LIPSYNC_MARKER = "PATCHED: static lipsync import map";
  const WORKLET_MARKER = "PATCHED: worklet served from public";

  // ── Step 2: dynamic lipsync import() -> static import map ────────────────
  const oldLipsync = `const moduleName = path + 'lipsync-' + lang.toLowerCase() + '.mjs';
      const className = 'Lipsync' + lang.charAt(0).toUpperCase() + lang.slice(1);
      import(moduleName).then( module => {
        this.lipsync[lang] = new module[className];
      });`;

  const newLipsync = `// ${LIPSYNC_MARKER} (avoids non-literal dynamic import)
      const className = 'Lipsync' + lang.charAt(0).toUpperCase() + lang.slice(1);
      const loaders = {
        'en': () => import('./lipsync-en.mjs'),
        'fi': () => import('./lipsync-fi.mjs'),
        'de': () => import('./lipsync-de.mjs'),
        'fr': () => import('./lipsync-fr.mjs'),
        'lt': () => import('./lipsync-lt.mjs'),
      };
      const loader = loaders[lang.toLowerCase()];
      if (loader) {
        loader().then( module => {
          this.lipsync[lang] = new module[className];
        });
      }`;

  if (src.includes(LIPSYNC_MARKER)) {
    console.log("[postinstall] lipsync import already patched, skipping");
  } else if (src.includes(oldLipsync)) {
    src = src.replace(oldLipsync, newLipsync);
    changed = true;
    console.log("[postinstall] Patched talkinghead dynamic import -> static import map");
  } else {
    console.log("[postinstall] WARNING: lipsync import code not found in talkinghead.mjs");
    console.log("[postinstall] The library may have been updated. Manual patch may be needed.");
  }

  // ── Step 3: AudioWorklet URL -> static public path ──────────────────────
  // `new URL('./playback-worklet.js', import.meta.url)` makes Turbopack try to
  // resolve the worklet as a bundled module (build fails at talkinghead.mjs:36).
  // The worklet is already copied to public/ (step 1), so load it from there.
  const oldWorklet =
    "const workletUrl = new URL('./playback-worklet.js', import.meta.url);";
  const newWorklet =
    `const workletUrl = /* ${WORKLET_MARKER}/ */ ` +
    "new URL('/playback-worklet.js', (typeof self !== 'undefined' && self.location) ? self.location.origin : 'http://localhost');";

  if (src.includes(WORKLET_MARKER)) {
    console.log("[postinstall] worklet URL already patched, skipping");
  } else if (src.includes(oldWorklet)) {
    src = src.replace(oldWorklet, newWorklet);
    changed = true;
    console.log("[postinstall] Patched talkinghead worklet URL -> /playback-worklet.js");
  } else {
    console.log("[postinstall] WARNING: worklet URL line not found in talkinghead.mjs");
    console.log("[postinstall] The library may have been updated. Manual patch may be needed.");
  }

  if (changed) {
    fs.writeFileSync(TALKINGHEAD_MJS, src, "utf8");
  }
} else {
  console.log("[postinstall] talkinghead.mjs not found, skipping patch");
}
