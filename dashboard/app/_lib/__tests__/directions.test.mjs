/**
 * Unit test for the directions page helpers (app/_lib/directions.ts). Plain
 * Node, like the other tests here; requires Node >=22.6 for TypeScript type
 * stripping. Exits non-zero on any failure.
 *
 *   npm run test:directions   (or: node app/_lib/__tests__/directions.test.mjs)
 *
 * What is guarded: the page's words (distance, time, data date), which id
 * reaches the backend, what state a failed fetch shows, where the kiosk
 * overlay may frame, and the privacy rule: no coordinate ever reaches a string
 * the page renders.
 */
import assert from "node:assert/strict";

const d = await import("../directions.ts");

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

const ID = "3f9c0a1b2c3d4e5f";
const RECORD = {
  id: ID,
  created_at: "2026-10-11T18:04:05.123456+00:00",
  mode: "drive",
  origin: { label: "Home" },
  destination: {
    label: "the Target on Example Road",
    address: "100 Example Road, Testville",
    lat: 31.15,
    lon: -99.65,
  },
  distance_mi: 4.2,
  duration_s: 720,
  steps: [
    { n: 1, text: "Drive north on Elm Street.", distance_mi: 0.5, duration_s: 60 },
    { n: 2, text: "Turn right onto Example Road.", distance_mi: 3.6, duration_s: 600 },
    { n: 3, text: "Your destination is on the right.", distance_mi: 0.0, duration_s: 0 },
  ],
  data_date: "2026-09-30",
  traffic_note: "Times assume normal traffic.",
  omaps_url:
    "https://omaps.app/v2/nav?origin=currentLocation&destination=31.150000,-99.650000&destination_name=Target&mode=drive",
};
const NOW = new Date(2026, 9, 11, 12, 0, 0);
const EM_DASH = String.fromCharCode(0x2014);

console.log("\n--- formatting ---");
check("distance", () => {
  assert.equal(d.formatDistance(4.2), "4.2 mi");
  assert.equal(d.formatDistance(4.25), "4.3 mi");
  assert.equal(d.formatDistance(1), "1.0 mi");
  assert.equal(d.formatDistance(12.6), "13 mi");
  assert.equal(d.formatDistance(0.05), "250 ft");
  assert.equal(d.formatDistance(0.001), "50 ft");
  assert.equal(d.formatDistance(0), "");
  assert.equal(d.formatDistance(NaN), "");
});
check("duration", () => {
  assert.equal(d.formatDuration(720), "12 min");
  assert.equal(d.formatDuration(30), "under 1 min");
  assert.equal(d.formatDuration(3600), "1 hr");
  assert.equal(d.formatDuration(3900), "1 hr 5 min");
  assert.equal(d.formatDuration(0), "");
});
check("mode labels", () => {
  assert.equal(d.modeLabel("drive"), "By car");
  assert.equal(d.modeLabel("walk"), "On foot");
  assert.equal(d.modeLabel("bike"), "By bike");
});
check("data date label, never a guess", () => {
  assert.equal(d.dataDateLabel("2026-09-30"), "Map data from September 30, 2026");
  assert.equal(d.dataDateLabel(null), "Map data date unknown");
  assert.equal(d.dataDateLabel(""), "Map data date unknown");
  assert.equal(d.dataDateLabel("last week"), "Map data date unknown");
  assert.equal(d.dataDateLabel("2026-13-01"), "Map data date unknown");
});
check("printed-on label", () => {
  assert.equal(d.printedOnLabel(NOW), "Printed October 11, 2026");
});

console.log("\n--- ids ---");
check("latest and real ids reach the backend", () => {
  assert.equal(d.routeApiPath("latest"), "/maps/routes/latest");
  assert.equal(d.routeApiPath(ID), `/maps/routes/${ID}`);
});
check("anything else does not", () => {
  for (const bad of ["", "LATEST", "3F9C0A1B2C3D4E5F", "abc", "../status", `${ID}0`, null, undefined]) {
    assert.equal(d.routeApiPath(bad), null, String(bad));
  }
});

console.log("\n--- page state ---");
const ok = (data) => ({ status: 200, data });
check("a route shows", () => assert.equal(d.classifyLoad(ok(RECORD), ok({ live: true })).kind, "ready"));
check("a saved route shows even when the maps are down", () =>
  assert.equal(d.classifyLoad(ok(RECORD), ok({ live: false })).kind, "ready"));
check("no such route", () =>
  assert.equal(d.classifyLoad({ status: 404, data: { error: "No such route." } }, ok({ live: true })).kind, "not-found"));
check("bad id is 'not saved'", () =>
  assert.equal(d.classifyLoad({ status: 400, data: { error: "Not a route id." } }, null).kind, "not-found"));
check("no routes yet", () =>
  assert.equal(d.classifyLoad({ status: 404, data: { error: "No routes yet." } }, ok({ live: true })).kind, "no-routes"));
check("no routes and maps down is offline", () =>
  assert.equal(d.classifyLoad({ status: 404, data: { error: "No routes yet." } }, ok({ live: false })).kind, "offline"));
check("proxy 404 is not set up", () =>
  assert.equal(d.classifyLoad({ status: 404, data: { error: "not_found" } }, { status: 404, data: null }).kind, "not-set-up"));
check("network error", () => assert.equal(d.classifyLoad(null, null).kind, "error"));
check("server error with maps down", () =>
  assert.equal(d.classifyLoad({ status: 500, data: null }, ok({ live: false })).kind, "offline"));
check("a 200 that is not a route is an error", () =>
  assert.equal(d.classifyLoad(ok({ id: "nope" }), ok({ live: true })).kind, "error"));
check("messages are plain and have no em dash", () => {
  assert.equal(d.LOAD_MESSAGES["not-found"], "That route isn't saved anymore. Ask Puff again.");
  for (const m of Object.values(d.LOAD_MESSAGES)) assert.ok(!m.includes(EM_DASH), m);
});

console.log("\n--- the view model ---");
const view = d.buildDirectionsView(d.normalizeRoute(RECORD), NOW);
check("summary", () => {
  assert.equal(view.title, "Directions to the Target on Example Road");
  assert.equal(view.address, "100 Example Road, Testville");
  assert.equal(view.distance, "4.2 mi");
  assert.equal(view.duration, "12 min");
  assert.equal(view.mode, "By car");
  assert.equal(view.trafficNote, "Times assume normal traffic.");
  assert.equal(view.dataDate, "Map data from September 30, 2026");
  assert.deepEqual(view.steps.map((s) => s.distance), ["0.5 mi", "3.6 mi", ""]);
});
check("walk and bike carry no traffic note", () => {
  const w = d.buildDirectionsView(d.normalizeRoute({ ...RECORD, mode: "walk", traffic_note: null }), NOW);
  assert.equal(w.trafficNote, null);
  assert.equal(w.mode, "On foot");
});
check("drive with a missing note still says normal traffic", () => {
  const v = d.buildDirectionsView(d.normalizeRoute({ ...RECORD, traffic_note: null }), NOW);
  assert.equal(v.trafficNote, "Times assume normal traffic.");
});
check("QR link kept only for omaps.app over https", () => {
  assert.equal(view.qrUrl, RECORD.omaps_url);
  assert.equal(d.normalizeRoute({ ...RECORD, omaps_url: "http://omaps.app/x" }).omaps_url, null);
  assert.equal(d.normalizeRoute({ ...RECORD, omaps_url: "https://evil.example/omaps.app" }).omaps_url, null);
  assert.equal(d.normalizeRoute({ ...RECORD, omaps_url: "javascript:alert(1)" }).omaps_url, null);
});

console.log("\n--- no coordinates on the page ---");
check("the coordinate detector", () => {
  assert.ok(d.hasCoordinateText("31.150000,-99.650000"));
  assert.ok(d.hasCoordinateText("at 31.150, -99.650 now"));
  assert.ok(d.hasCoordinateText("-30.1234"));
  assert.ok(!d.hasCoordinateText("Drive 0.5 mi on Elm Street."));
  assert.ok(!d.hasCoordinateText("100 Example Road, Testville"));
  assert.ok(!d.hasCoordinateText("4.2 mi"));
});
// Unicode written as escapes so the source stays ASCII.
const DEG = "°", PRIME = "′", DPRIME = "″", UMINUS = "−";
check("degrees-minutes-seconds forms are stripped", () => {
  assert.equal(d.safeText(`30${DEG}7'N 97${DEG}34'W`), "");
  assert.equal(d.safeText(`Pin at 30${DEG} 7.4' N, 97${DEG} 34.1' W today`), "Pin at today");
  assert.equal(d.safeText(`30${DEG}7${PRIME}12${DPRIME}N 97${DEG}34${PRIME}W`), "");
  assert.equal(d.safeText(`30${DEG}7'12"N`), "");
  assert.equal(d.safeText(`near 30.1234${DEG}`), "near");
});
check("unicode minus disappears with its number, no stray sign", () => {
  assert.equal(d.safeText(`at ${UMINUS}97.5678 now`), "at now");
  assert.equal(d.safeText(`30.123400, ${UMINUS}97.567800`), "");
  assert.ok(!d.safeText(`x ${UMINUS}97.5678`).includes(UMINUS));
  assert.equal(d.safeText("at -97.5678 now"), "at now");
});
check("road text is kept", () => {
  for (const keep of [
    "0.3 mi",
    "1100 Congress Ave",
    "Exit 12.5",
    "Exit 1.5,2.5 split",
    "Hwy 281.1234",
    "Take I-35.1234 north",
    "Pair with too few decimals 31.15, -99.65",
    "Not a latitude 123.4567, 45.6789",
  ]) {
    assert.equal(d.safeText(keep), keep);
    assert.ok(!d.hasCoordinateText(keep), keep);
  }
});
check("normalizeRoute drops destination lat/lon", () => {
  const r = d.normalizeRoute(RECORD);
  assert.deepEqual(Object.keys(r.destination).sort(), ["address", "label"]);
});
check("no rendered string carries a coordinate, even from a leaky record", () => {
  const leaky = {
    ...RECORD,
    destination: { label: "Dropped pin 31.150000, -99.650000", address: "near 30.12340,-97.56780", lat: 31.15, lon: -99.65 },
    steps: [{ n: 1, text: "Head north from 30.123400, -97.567800.", distance_mi: 1, duration_s: 60 }],
    traffic_note: "Times assume normal traffic.",
  };
  for (const rec of [RECORD, leaky]) {
    const v = d.buildDirectionsView(d.normalizeRoute(rec), NOW);
    const { qrUrl, ...rendered } = v;
    const text = JSON.stringify(rendered);
    assert.ok(!d.hasCoordinateText(text), text);
    assert.ok(!/lat|lon/i.test(Object.keys(rendered).join(",")));
  }
  const v = d.buildDirectionsView(d.normalizeRoute(leaky), NOW);
  assert.equal(v.destination, "Dropped pin");
  assert.equal(v.steps[0].text, "Head north from.");
});

console.log("\n--- kiosk overlay target ---");
check("directionsFrameUrl: other kinds are not its business", () => {
  assert.equal(d.directionsFrameUrl(null), null);
  assert.equal(d.directionsFrameUrl({ kind: "vault", url: "/vault/view/abc" }), null);
  assert.equal(d.directionsFrameUrl({ kind: "knowledge", url: "/dashboard/knowledge" }), null);
});
check("directionsFrameUrl: secure flag and hostile urls never escape the directions pages", () => {
  for (const url of ["//evil.example", "https://evil.example/", "/vault/view/abc", "javascript:alert(1)", "/dashboard/directions/../vault"]) {
    assert.equal(d.directionsFrameUrl({ kind: "directions", secure: true, url }), "/dashboard/directions", url);
    assert.equal(
      d.directionsFrameUrl({ kind: "directions", secure: true, url, meta: { route_id: ID } }),
      `/dashboard/directions/${ID}/print`,
      url,
    );
  }
});
check("built from meta.route_id", () => {
  assert.equal(
    d.directionsFrameSrc({ url: "/elsewhere", meta: { route_id: ID, mode: "drive" } }),
    `/dashboard/directions/${ID}/print`,
  );
});
check("falls back to the exact print url, then to the latest", () => {
  assert.equal(d.directionsFrameSrc({ url: `/dashboard/directions/${ID}/print` }), `/dashboard/directions/${ID}/print`);
  assert.equal(d.directionsFrameSrc({ url: "https://evil.example/" }), "/dashboard/directions");
  assert.equal(d.directionsFrameSrc({ url: `/dashboard/directions/${ID}/print?x=1` }), "/dashboard/directions");
  assert.equal(d.directionsFrameSrc({ meta: { route_id: "../../vault" } }), "/dashboard/directions");
  assert.equal(d.directionsFrameSrc({}), "/dashboard/directions");
});

console.log("\n--- fitting the omaps link into a QR ---");
const qr = await import("../qr.ts");
const fitsQr = (u) => {
  try {
    qr.encodeQr(u, { ecc: "L" });
    return true;
  } catch {
    return false;
  }
};
const OM = (name) =>
  "https://omaps.app/v2/nav?origin=currentLocation&destination=31.150000,-99.650000" +
  (name === null ? "" : `&destination_name=${encodeURIComponent(name)}`) +
  "&mode=walk";
const PREFIX =
  "https://omaps.app/v2/nav?origin=currentLocation&destination=31.150000,-99.650000&destination_name=";
check("a link that fits is unchanged", () => {
  assert.equal(d.fitOmapsUrl(RECORD.omaps_url, fitsQr), RECORD.omaps_url);
});
check("a long name is trimmed at a word boundary, other params byte for byte", () => {
  const fullName = "Saint Example Memorial Regional Medical Center Emergency Department North Tower "
    .repeat(3)
    .trim();
  const long = OM(fullName);
  assert.equal(fitsQr(long), false);
  const out = d.fitOmapsUrl(long, fitsQr);
  assert.ok(out && fitsQr(out), String(out));
  assert.ok(out.startsWith(PREFIX), out);
  assert.ok(out.endsWith("&mode=walk"), out);
  const name = out.slice(PREFIX.length, out.length - "&mode=walk".length);
  const words = decodeURIComponent(name).split(" ");
  assert.ok(words.length >= 1 && words.length < fullName.split(" ").length);
  assert.equal(words.join(" "), fullName.split(" ").slice(0, words.length).join(" "));
});
check("trimming with a length rule drops words one at a time", () => {
  const budget = OM("Alpha Beta").length;
  assert.equal(d.fitOmapsUrl(OM("Alpha Beta Gamma"), (c) => c.length <= budget), OM("Alpha Beta"));
});
check("name dropped entirely when no word prefix fits", () => {
  const budget = OM(null).length;
  assert.equal(
    d.fitOmapsUrl(OM("Supercalifragilisticexpialidocious Place"), (c) => c.length <= budget),
    OM(null),
  );
});
check("a one-word name too long for the QR is dropped, coordinates kept", () => {
  const out = d.fitOmapsUrl(OM("X".repeat(400)), fitsQr);
  assert.equal(out, OM(null));
  assert.ok(fitsQr(out));
});
check("null when even the bare link does not fit, and the fallback line is plain", () => {
  assert.equal(d.fitOmapsUrl(OM("A B C"), () => false), null);
  assert.equal(
    d.QR_TOO_LONG_MESSAGE,
    "This route can't fit in a QR code. Open Directions on your phone to get the link.",
  );
  assert.ok(!d.QR_TOO_LONG_MESSAGE.includes(EM_DASH));
});

console.log(`\n${pass} passed, ${fail} failed`);
assert.equal(fail, 0, `${fail} directions assertion(s) failed`);
