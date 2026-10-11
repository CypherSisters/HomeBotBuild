/**
 * Pairing-gate assertions, with the suggestion-resolution exemption as the
 * subject. Plain Node — this repo has no test runner, and the gate is pure
 * enough not to need one.
 *
 *   npm run test:pairing        (or: node app/_lib/server/__tests__/pairing-gate.test.mjs)
 *
 * Requires Node >=22.6 for native TypeScript type stripping. Exits non-zero on
 * any failure. Nothing is written and no request is made: pathRequiresPairing()
 * is pure path/method/origin logic and never touches the pairing state file.
 *
 * Lives under app/_lib/ (underscore-private), so Next never routes it.
 */
import assert from "node:assert/strict";

const { pathRequiresPairing } = await import("../pairing.ts");

const ID = "c841a287-b4ab-44d4-8513-4aa929109746"; // shape of a real backend id
let pass = 0, fail = 0;

/** Default: same-origin browser request (what the kiosk and /send send). */
function check(mode, path, method, expected, label, opts = { sameOrigin: true }) {
  process.env.PUFF_PAIRING_ENFORCE = mode;
  const got = pathRequiresPairing(path, method, opts);
  const ok = got === expected;
  if (ok) pass++; else fail++;
  console.log(
    `${ok ? "PASS" : "FAIL"}  [${mode.padEnd(9)}] ${method.padEnd(6)} ${path}` +
      `  -> gated=${got} (want ${expected})   ${label}`
  );
}

console.log("\n--- knowledge stage: the exemption ---");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept`, "POST", false, "accept allowed");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/dismiss`, "POST", false, "dismiss allowed");
check("knowledge", `/api/knowledge/suggestions/${ID}/accept`, "POST", false, "non-pai prefix allowed");
check("knowledge", `/api/pai/knowledge/suggestions/${ID.toUpperCase()}/ACCEPT`, "POST", false, "case-insensitive via eff");
check("knowledge", `/api/pai/knowledge%2fsuggestions%2f${ID}%2faccept`, "POST", false, "encoded separators still match");
check("knowledge", "/api/pai/knowledge/suggestions/pending", "GET", false, "pending read still open");

console.log("\n--- knowledge stage: everything else still gated ---");
check("knowledge", "/api/pai/knowledge/upload", "POST", true, "UPLOAD STILL GATED");
check("knowledge", "/api/pai/knowledge%2fupload", "POST", true, "encoded upload still gated");
check("knowledge", "/api/pai/knowledge/library/foo", "DELETE", true, "delete still gated");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept`, "DELETE", true, "non-POST method gated");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept`, "PUT", true, "PUT gated");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept`, "", true, "missing method fails closed");

console.log("\n--- knowledge stage: traversal / spoof attempts must be REJECTED ---");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept/../upload`, "POST", true, "suffix traversal");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept%2f..%2fupload`, "POST", true, "encoded suffix traversal");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}%2f..%2fupload/accept`, "POST", true, "id-segment traversal");
check("knowledge", "/api/pai/knowledge/suggestions/../upload/accept", "POST", true, "dotdot as id");
check("knowledge", "/api/pai/knowledge/suggestions/%2e%2e/upload/accept", "POST", true, "encoded dotdot as id");
check("knowledge", "/api/pai/knowledge/suggestions/%252e%252e/upload/accept", "POST", true, "double-encoded dotdot");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept/`, "POST", true, "trailing slash");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/acceptx`, "POST", true, "suffixed verb");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/xaccept`, "POST", true, "prefixed verb");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept/upload`, "POST", true, "extra segment");
check("knowledge", `/x/api/pai/knowledge/suggestions/${ID}/accept`, "POST", false, "not a knowledge path at all (ungated by prefix, not by exemption)");
check("knowledge", `/api/pai/knowledge/suggestions//accept`, "POST", true, "empty id");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept%00`, "POST", true, "NUL byte fails closed");
check("knowledge", "/api/pai/knowledge/suggestions/a%2fb/accept", "POST", true, "slash inside id");
check("knowledge", "/api/pai/knowledge/suggestions/a%5cb/accept", "POST", true, "backslash inside id");
check("knowledge", "/api/pai/knowledge/upload/../suggestions/x/accept", "POST", true, "dotdot anywhere");
check("knowledge", `/api/pai/knowledge/suggestions/${"a".repeat(200)}/accept`, "POST", true, "over-long id");
check("knowledge", "/api/pai/knowledge/suggestions/%zz/accept", "POST", true, "malformed escape fails closed");

console.log("\n--- same-origin is REQUIRED for the exemption ---");
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept`, "POST", true, "CROSS-SITE accept gated", { sameOrigin: false });
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/dismiss`, "POST", true, "CROSS-SITE dismiss gated", { sameOrigin: false });
check("knowledge", `/api/pai/knowledge/suggestions/${ID}/accept`, "POST", true, "empty opts fails closed", {});
// Third arg genuinely absent — must not be routed through check()'s own default.
{
  process.env.PUFF_PAIRING_ENFORCE = "knowledge";
  const got = pathRequiresPairing(`/api/pai/knowledge/suggestions/${ID}/accept`, "POST");
  const ok = got === true;
  if (ok) pass++; else fail++;
  console.log(`${ok ? "PASS" : "FAIL"}  [knowledge] POST   <2-arg call, opts arg absent>  -> gated=${got} (want true)   omitted opts fails closed`);
}
check("knowledge", "/api/pai/knowledge/suggestions/pending", "GET", false, "reads unaffected by origin", { sameOrigin: false });
check("knowledge", "/api/pai/knowledge/upload", "POST", true, "upload unaffected by origin", { sameOrigin: true });

console.log("\n--- 'all' stage: NOTHING changes, everything gated ---");
check("all", `/api/pai/knowledge/suggestions/${ID}/accept`, "POST", true, "accept GATED at all");
check("all", `/api/pai/knowledge/suggestions/${ID}/dismiss`, "POST", true, "dismiss GATED at all");
check("all", `/api/knowledge/suggestions/${ID}/accept`, "POST", true, "non-pai prefix GATED at all");
check("all", "/api/pai/knowledge/suggestions/pending", "GET", true, "reads gated at all");
check("all", "/api/pai/knowledge/upload", "POST", true, "upload gated at all");
check("all", "/api/pair/status", "GET", false, "pairing endpoint still exempt");

console.log("\n--- /api/spotify: keyed OAuth callback, gated like every other keyed route ---");
// The callback POSTs to the backend with the appliance's PUFFPAI_API_KEY, so at
// the "all" stage it must be gated exactly like /api/ha — including the bare
// prefix and any future sibling under it. Spotify reaches it by top-level GET
// navigation, which SameSite=Lax still carries the device cookie on.
check("all", "/api/spotify/callback", "GET", true, "SPOTIFY CALLBACK GATED at all");
check("all", "/api/spotify/callback", "POST", true, "gated for writes too");
check("all", "/api/spotify", "GET", true, "bare prefix gated");
check("all", "/api/spotify/anything/else", "GET", true, "whole subtree gated");
check("all", "/api/spotify%2fcallback", "GET", true, "encoded separator still gated");
check("all", "/api/spotifyx/callback", "GET", false, "prefix match is segment-anchored, not substring");
check("all", "/api/spotify/callback", "GET", true, "cross-site is gated too", { sameOrigin: false });
check("all", "/api/ha/status", "GET", true, "sibling keyed route unchanged");
// "knowledge" stage gates only the knowledge surface — /api/spotify is ungated
// there, mirroring /api/ha (asserted alongside so the two cannot drift).
check("knowledge", "/api/spotify/callback", "GET", false, "ungated at knowledge, like /api/ha");
check("knowledge", "/api/spotify/callback", "POST", false, "ungated at knowledge even for writes");
check("knowledge", "/api/ha/status", "GET", false, "…and /api/ha agrees");
check("knowledge", "/api/ha/service", "POST", false, "…for writes too");
check("off", "/api/spotify/callback", "GET", false, "off means off");

console.log("\n--- 'off' stage ---");
check("off", `/api/pai/knowledge/suggestions/${ID}/accept`, "POST", false, "off means off");
check("off", "/api/pai/knowledge/upload", "POST", false, "off means off");


console.log("\n--- traversal OUT of the /api/pair exemption ---");
// The pairing endpoints are exempt so a fresh device can redeem a code. That
// exemption was a raw prefix test on a path that had been decoded but never
// canonicalised, so a ".." segment let a caller wear "/api/pair/" as a costume
// while the string resolved into the keyed proxy. effectiveGatePath() now
// refuses any path that is not already canonical, and null means gated.
//
// NOTE ON SEVERITY, so nobody reads these as a patched live hole: none of these
// were reachable over HTTP. Next canonicalises before middleware and before a
// route handler, verified on next start (15 shapes, none reached the backend
// unpaired, raw socket included; and /api/pai/../pair/status answers 200 from
// the pairing endpoint, proving normalisation happens upstream). These assert
// that the GATE is correct about its own input, so the guarantee stops
// depending on the framework normalising first.
const OUT = [
  ["/api/pair/../pai/vault/contents", "raw .. walks into the keyed proxy"],
  ["/api/pair/%2e%2e/pai/vault/contents", "%2e%2e"],
  ["/api/pair/..%2fpai/vault/contents", "..%2f"],
  ["/api/pair/%252e%252e/pai/vault/contents", "double-encoded .."],
  ["/api/pair/.%2e/pai/vault/contents", "half-encoded .."],
  ["/api/pair/%2e./pai/vault/contents", "other half-encoded .."],
  ["/api/pair/./../pai/ha/service/light/turn_on", "single dot then .."],
  ["/API/PAIR/../pai/vault/contents", "mixed case"],
  ["/api/pair//../pai/vault/contents", "doubled slash then .."],
  ["/api/pair/.." + String.fromCharCode(92) + "pai/vault/contents", "backslash separator"],
  ["/api/pair/..%5cpai/vault/contents", "encoded backslash"],
  ["/api/pair/ ../pai/vault/contents", "leading space on the .. segment"],
  ["/api/pair./../pai/vault/contents", "trailing dot on the pair segment"],
  ["/api/pair/../ha/status", ".. into /api/ha"],
  ["/api/pair/../camera/front", ".. into /api/camera"],
  ["/api/pair/../knowledge/upload", ".. into /api/knowledge"],
  ["/api/pair/../../api/pai/vault/contents", "double .."],
  ["/api/pair/../pai/vault/contents/", "trailing slash variant"],
];
for (const [tp, label] of OUT) check("all", tp, "GET", true, label);
for (const [tp, label] of OUT) check("all", tp, "POST", true, label + " (POST)");
// The same shapes must not slip through the knowledge stage's read exemption
// either: a GET is exempt there by method, so the path check has to hold.
for (const [tp, label] of OUT.slice(0, 6))
  check("knowledge", tp.replace("/pai/vault/contents", "/pai/knowledge/upload"), "POST", true, label + " (knowledge stage)");

console.log("\n--- the exemption itself still works (no over-blocking) ---");
check("all", "/api/pair", "POST", false, "redeem a code");
check("all", "/api/pair/status", "GET", false, "status");
check("all", "/api/pair/code", "GET", false, "show the code");
check("all", "/api/pair/rotate", "POST", false, "rotate");
check("all", "/api/pair/devices", "GET", false, "list devices");
check("all", "/api/pair/devices/revoke", "POST", false, "revoke a device");
check("all", "/api/pair/devices/rename", "POST", false, "rename a device");
check("all", "/api/pair/device-token", "POST", false, "mint a shortcut token");
check("all", "/api/pair/device-token/revoke", "POST", false, "revoke a shortcut token");
check("all", "/dashboard", "GET", false, "pages render so the pairing UI can show");
check("all", "/dashboard/", "GET", false, "trailing slash on a page still fine");
check("all", "/dashboard/settings", "GET", false, "settings page (where pairing lives)");
check("all", "/send", "GET", false, "/send page");
check("all", "/login", "GET", false, "/login panel");
check("all", "/api/health", "GET", false, "health probe");
check("all", "/api/pai/knowledge/library/My%20Library", "GET", true, "a space in a name is not traversal (gated as a normal keyed path)");
check("all", "/api/pai/vault/item/some.key", "GET", true, "an interior dot is fine (gated as a normal keyed path)");
check("off", "/api/pair/../pai/vault/contents", "GET", false, "off still means off, even for a hostile path");

console.log("\n--- Send to Puff: messages and voice memos ---");
check("all", "/api/pai/chat/send", "POST", true, "phone message gated at all");
check("all", "/api/pai/chat/history", "GET", true, "household thread read gated at all");
check("all", "/api/pai/chat/voice-memo", "POST", true, "voice memo gated at all");
check("all", "/api/pai/chat/voice-memo", "POST", true, "voice memo gated cross-site too", { sameOrigin: false });
check("knowledge", "/api/pai/chat/voice-memo", "POST", true, "voice memo gated at knowledge, like the photo upload");
check("knowledge", "/api/pai/chat/VOICE-MEMO", "POST", true, "case spelling still gated");
check("knowledge", "/api/pai/chat%2fvoice-memo", "POST", true, "encoded separator still gated");
check("knowledge", "/api/pai/chat/voice-memo", "GET", true, "any method gated");
check("off", "/api/pai/chat/voice-memo", "POST", false, "off still means off");

console.log(`\n${pass} passed, ${fail} failed`);
assert.equal(fail, 0, `${fail} gate assertions failed`);
