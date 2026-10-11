/**
 * Unit test for World Radio's household favorites sync. Plain Node — this repo
 * has no test runner, and favorites.ts is deliberately free of React.
 *
 *   npm run test:radiofavs
 *     (or: node app/dashboard/_components/WorldRadio/__tests__/radio-favorites.test.mjs)
 *
 * Requires Node >=22.6 for native TypeScript type stripping, like the other
 * tests in this repo. Exits non-zero on any failure.
 *
 * What is being guarded: favorites are now the HOUSEHOLD's list, so the ways
 * this can go wrong all destroy something the owner saved — a first sync that
 * marks itself done without the backend having taken the rows, a backend
 * hiccup that wipes the cached list, an optimistic star that sticks after a
 * refusal, or a TuneIn row shipped without a stream voice could play.
 */
import assert from "node:assert/strict";

const mod = await import("../favorites.ts");
const {
  FAVS_KEY,
  SYNCED_KEY,
  MAX_SYNC_ATTEMPTS,
  attemptsOf,
  withAttempts,
  toWire,
  fromWire,
  toWireId,
  toLocalId,
  isStorable,
  loadCached,
  saveCached,
  isFirstSyncDone,
  mergeForFirstSync,
  addRemote,
  removeRemote,
  syncFavorites,
} = mod;

let pass = 0;
let fail = 0;

async function check(name, fn) {
  try {
    await fn();
    pass += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    fail += 1;
    console.error(`FAIL  ${name}\n      ${err.message}`);
  }
}

/* ── doubles ─────────────────────────────────────────────────────────────── */

function installStorage(seed = {}) {
  const store = new Map(Object.entries(seed));
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
  return store;
}

/** fetch double: `handler(action, params, url)` returns the `result` object. */
function installFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    if (String(url).startsWith("/api/tunein/resolve")) {
      const out = handler("tunein_resolve", { url: String(url) });
      calls.push({ action: "tunein_resolve", params: { url: String(url) } });
      if (out === null) throw new Error("network down");
      return { ok: out.ok !== false, json: async () => out.body ?? {} };
    }
    assert.equal(url, "/api/pai/apps/mediahub/action");
    const body = JSON.parse(init.body);
    calls.push(body);
    const out = handler(body.action, body.params);
    if (out === null) throw new Error("network down");
    if (out === "http-error") return { ok: false, json: async () => ({}) };
    return { ok: true, json: async () => ({ success: true, result: out }) };
  };
  return calls;
}

const station = (over = {}) => ({
  stationuuid: "uuid-1",
  name: "Radio One",
  url_resolved: "http://stream.example/one",
  country: "France",
  countrycode: "FR",
  codec: "MP3",
  bitrate: 128,
  tags: "jazz",
  votes: 42,
  homepage: "http://example",
  ...over,
});

const wireRow = (over = {}) => ({
  stationuuid: "uuid-1",
  name: "Radio One",
  url_resolved: "http://stream.example/one",
  country: "France",
  countrycode: "FR",
  codec: "MP3",
  bitrate: 128,
  tags: "jazz",
  homepage: "http://example",
  favicon: "",
  source: "radio-browser",
  ...over,
});

/* ── wire shape ──────────────────────────────────────────────────────────── */

await check("toWire keeps exactly the documented keys", () => {
  const wire = toWire({ ...station(), favicon: "http://f", extra: "nope" });
  assert.deepEqual(Object.keys(wire).sort(), [
    "bitrate",
    "codec",
    "country",
    "countrycode",
    "favicon",
    "homepage",
    "name",
    "source",
    "stationuuid",
    "tags",
    "url_resolved",
  ]);
  // votes is display-only and an unexpected key is a 400 backend side.
  assert.equal("votes" in wire, false);
  assert.equal("extra" in wire, false);
  assert.equal(wire.source, "radio-browser");
});

await check("TuneIn id adapter round-trips", () => {
  assert.equal(toWireId("s328971"), "tunein:s328971");
  assert.equal(toLocalId("tunein:s328971"), "s328971");
  assert.equal(toLocalId(toWireId("s1")), "s1");
  // radio-browser uuids pass through untouched, both ways.
  assert.equal(toWireId("uuid-1"), "uuid-1");
  assert.equal(toLocalId("uuid-1"), "uuid-1");
  assert.equal(toWire(station({ stationuuid: "s328971" })).stationuuid, "tunein:s328971");
  assert.equal(toWire(station({ stationuuid: "s328971" })).source, "tunein");
  assert.equal(fromWire(wireRow({ stationuuid: "tunein:s328971" })).stationuuid, "s328971");
});

await check("a TuneIn row with no stream is not storable", () => {
  assert.equal(isStorable(station({ stationuuid: "s1", url_resolved: "" })), false);
  assert.equal(isStorable(station()), true);
  assert.equal(isStorable(station({ url_resolved: "ftp://nope" })), false);
});

/* ── first sync ──────────────────────────────────────────────────────────── */

await check("mergeForFirstSync keeps order and skips known/unstorable rows", () => {
  const local = [
    station({ stationuuid: "a" }),
    station({ stationuuid: "b" }),
    station({ stationuuid: "s9", url_resolved: "" }),
    station({ stationuuid: "b" }),
  ];
  const remote = [station({ stationuuid: "b" })];
  assert.deepEqual(
    mergeForFirstSync(local, remote).map((s) => s.stationuuid),
    ["a"]
  );
});

await check("first sync imports local rows and sets the flag on ok", async () => {
  installStorage();
  const remoteAfter = [wireRow({ stationuuid: "a" }), wireRow({ stationuuid: "b" })];
  const calls = installFetch((action) => {
    if (action === "mediahub_favorites_list") return { ok: true, favorites: [wireRow({ stationuuid: "b" })], max: 40 };
    if (action === "mediahub_favorites_import") return { ok: true, favorites: remoteAfter, added: 1, skipped: 0 };
    throw new Error(`unexpected ${action}`);
  });
  const out = await syncFavorites([station({ stationuuid: "a" }), station({ stationuuid: "b" })]);
  assert.deepEqual(calls.map((c) => c.action), [
    "mediahub_favorites_list",
    "mediahub_favorites_import",
  ]);
  assert.deepEqual(calls[1].params.stations.map((s) => s.stationuuid), ["a"]);
  assert.deepEqual(out.favorites.map((s) => s.stationuuid), ["a", "b"]);
  assert.equal(isFirstSyncDone(), true);
});

await check("a failed import leaves the flag unset and the cache alone", async () => {
  installStorage();
  installFetch((action) => {
    if (action === "mediahub_favorites_list") return { ok: true, favorites: [], max: 40 };
    return null; // import dies on the wire
  });
  const out = await syncFavorites([station({ stationuuid: "a" })]);
  assert.equal(out.favorites, null);
  assert.equal(isFirstSyncDone(), false);
});

await check("nothing to hand over still completes the first sync", async () => {
  installStorage();
  const calls = installFetch(() => ({ ok: true, favorites: [wireRow()], max: 40 }));
  await syncFavorites([]);
  assert.deepEqual(calls.map((c) => c.action), ["mediahub_favorites_list"]);
  assert.equal(isFirstSyncDone(), true);
});

/* ── steady state ────────────────────────────────────────────────────────── */

await check("after the first sync the backend list wins outright", async () => {
  installStorage({ [SYNCED_KEY]: "1" });
  installFetch((action) => {
    assert.equal(action, "mediahub_favorites_list");
    // Saved by voice; the cached row was removed on another surface.
    return { ok: true, favorites: [wireRow({ stationuuid: "voice", name: "Voice Pick" })], max: 40 };
  });
  const out = await syncFavorites([station({ stationuuid: "old" })]);
  assert.deepEqual(out.favorites.map((s) => s.stationuuid), ["voice"]);
  assert.deepEqual(out.localOnly, []);
});

await check("an unreachable backend keeps the cache and says nothing", async () => {
  installStorage({ [SYNCED_KEY]: "1" });
  installFetch(() => null);
  const out = await syncFavorites([station()]);
  assert.equal(out.favorites, null);

  // A 401/502 from the proxy is the same verdict, not an empty list.
  installStorage({ [SYNCED_KEY]: "1" });
  installFetch(() => "http-error");
  assert.equal((await syncFavorites([station()])).favorites, null);

  // And a backend that refuses outright must not set the flag either.
  installStorage();
  installFetch(() => ({ ok: false, error: "nope" }));
  const refused = await syncFavorites([station()]);
  assert.equal(refused.favorites, null);
  assert.equal(isFirstSyncDone(), false);
});

await check("an unresolved TuneIn favorite stays local until it resolves", async () => {
  // Resolve fails: the row is neither lost nor sent.
  installStorage({ [SYNCED_KEY]: "1" });
  const calls = installFetch((action) => {
    if (action === "mediahub_favorites_list") return { ok: true, favorites: [], max: 40 };
    if (action === "tunein_resolve") return { ok: false, body: {} };
    throw new Error(`unexpected ${action}`);
  });
  const pending = station({ stationuuid: "s77", url_resolved: "" });
  const out = await syncFavorites([pending], { retryPending: true });
  assert.deepEqual(out.favorites, []);
  assert.deepEqual(out.localOnly.map((s) => s.stationuuid), ["s77"]);
  assert.equal(calls.some((c) => c.action === "mediahub_favorites_add"), false);

  // Resolve succeeds on a later load: it joins the household list.
  installStorage({ [SYNCED_KEY]: "1" });
  const calls2 = installFetch((action) => {
    if (action === "mediahub_favorites_list") return { ok: true, favorites: [], max: 40 };
    if (action === "tunein_resolve")
      return { ok: true, body: { url_resolved: "http://stream.example/s77" } };
    if (action === "mediahub_favorites_add")
      return {
        ok: true,
        favorites: [wireRow({ stationuuid: "tunein:s77", url_resolved: "http://stream.example/s77" })],
      };
    throw new Error(`unexpected ${action}`);
  });
  const out2 = await syncFavorites([pending], { retryPending: true });
  assert.deepEqual(out2.favorites.map((s) => s.stationuuid), ["s77"]);
  assert.deepEqual(out2.localOnly, []);
  const add = calls2.find((c) => c.action === "mediahub_favorites_add");
  assert.equal(add.params.station.stationuuid, "tunein:s77");
  assert.equal(add.params.station.url_resolved, "http://stream.example/s77");
});

/* ── retry discipline ────────────────────────────────────────────────────── */

await check("a pending row is tried once per pass, three times at most", async () => {
  const deadEnd = () => {
    installStorage({ [SYNCED_KEY]: "1" });
    return installFetch((action) => {
      if (action === "mediahub_favorites_list") return { ok: true, favorites: [], max: 40 };
      if (action === "tunein_resolve") return { ok: false, body: {} };
      throw new Error(`unexpected ${action}`);
    });
  };

  // Fresh row: exactly one resolve, and the attempt is remembered.
  let calls = deadEnd();
  let out = await syncFavorites(
    [station({ stationuuid: "s77", url_resolved: "" })],
    { retryPending: true }
  );
  assert.equal(calls.filter((c) => c.action === "tunein_resolve").length, 1);
  assert.equal(attemptsOf(out.localOnly[0]), 1);

  // Two more passes get it to the cap, and the row is still shown.
  calls = deadEnd();
  out = await syncFavorites([withAttempts(out.localOnly[0], 2)], { retryPending: true });
  assert.equal(attemptsOf(out.localOnly[0]), MAX_SYNC_ATTEMPTS);

  // At the cap nothing is asked again, ever.
  calls = deadEnd();
  out = await syncFavorites([withAttempts(station({ stationuuid: "s77", url_resolved: "" }), 3)], {
    retryPending: true,
  });
  assert.equal(calls.filter((c) => c.action === "tunein_resolve").length, 0);
  assert.deepEqual(out.localOnly.map((s) => s.stationuuid), ["s77"]);
  assert.equal(attemptsOf(out.localOnly[0]), MAX_SYNC_ATTEMPTS);
});

await check("a read-only pass never resolves or writes", async () => {
  installStorage({ [SYNCED_KEY]: "1" });
  const calls = installFetch((action) => {
    if (action === "mediahub_favorites_list") return { ok: true, favorites: [], max: 40 };
    throw new Error(`unexpected ${action}`);
  });
  const out = await syncFavorites([station({ stationuuid: "s77", url_resolved: "" })]);
  assert.deepEqual(calls.map((c) => c.action), ["mediahub_favorites_list"]);
  assert.equal(attemptsOf(out.localOnly[0]), 0);
});

await check("invalid_station stops the retries immediately", async () => {
  installStorage({ [SYNCED_KEY]: "1" });
  installFetch((action) => {
    if (action === "mediahub_favorites_list") return { ok: true, favorites: [], max: 40 };
    if (action === "tunein_resolve")
      return { ok: true, body: { url_resolved: "http://stream.example/s77" } };
    if (action === "mediahub_favorites_add") return { ok: false, error: "invalid_station" };
    throw new Error(`unexpected ${action}`);
  });
  const out = await syncFavorites(
    [station({ stationuuid: "s77", url_resolved: "" })],
    { retryPending: true }
  );
  assert.equal(attemptsOf(out.localOnly[0]), MAX_SYNC_ATTEMPTS);
});

/* ── a full household list ───────────────────────────────────────────────── */

await check("rows the full list would not take stay on this device", async () => {
  installStorage();
  const remote = [wireRow({ stationuuid: "kept" })];
  installFetch((action) => {
    if (action === "mediahub_favorites_list") return { ok: true, favorites: remote, max: 40 };
    if (action === "mediahub_favorites_import")
      return { ok: true, favorites: remote, added: 0, skipped: 0, ignored: 2, full: true };
    throw new Error(`unexpected ${action}`);
  });
  const out = await syncFavorites([station({ stationuuid: "a" }), station({ stationuuid: "b" })]);
  assert.deepEqual(out.favorites.map((s) => s.stationuuid), ["kept"]);
  assert.deepEqual(out.localOnly.map((s) => s.stationuuid), ["a", "b"]);
  assert.equal(out.note, "Favorites are full (40); some stayed on this device.");
  // The handover happened; it is not repeated on every load.
  assert.equal(isFirstSyncDone(), true);

  // And a later pass keeps those rows rather than reading them as removals.
  installStorage({ [SYNCED_KEY]: "1" });
  installFetch(() => ({ ok: true, favorites: remote, max: 40 }));
  const later = await syncFavorites([...out.localOnly, station({ stationuuid: "gone" })]);
  assert.deepEqual(later.favorites.map((s) => s.stationuuid), ["kept"]);
  assert.deepEqual(later.localOnly.map((s) => s.stationuuid), ["a", "b"]);
  assert.equal(later.note, undefined);
});

await check("a short import is spotted even without the full flag", async () => {
  installStorage();
  const remote = [wireRow({ stationuuid: "kept" }), wireRow({ stationuuid: "a" })];
  installFetch((action) => {
    if (action === "mediahub_favorites_list")
      return { ok: true, favorites: [wireRow({ stationuuid: "kept" })], max: 2 };
    if (action === "mediahub_favorites_import")
      return { ok: true, favorites: remote, added: 1 };
    throw new Error(`unexpected ${action}`);
  });
  const out = await syncFavorites([station({ stationuuid: "a" }), station({ stationuuid: "b" })]);
  assert.deepEqual(out.localOnly.map((s) => s.stationuuid), ["b"]);
  assert.equal(out.note, "Favorites are full (2); some stayed on this device.");
});

/* ── opaque ids ──────────────────────────────────────────────────────────── */

await check("a row keyed url:<hex> survives and is echoed back verbatim", async () => {
  const opaque = "url:0123456789abcdef";
  const back = fromWire(wireRow({ stationuuid: opaque }));
  assert.equal(back.stationuuid, opaque);
  assert.equal(toWireId(opaque), opaque);
  assert.equal(toWire(back).stationuuid, opaque);

  const calls = installFetch(() => ({ ok: true, favorites: [] }));
  await removeRemote(back);
  assert.deepEqual(calls[0].params, { id: opaque });

  // It must reach the caller through a whole sync pass too, not be filtered.
  installStorage({ [SYNCED_KEY]: "1" });
  installFetch(() => ({ ok: true, favorites: [wireRow({ stationuuid: opaque })], max: 40 }));
  const out = await syncFavorites([]);
  assert.deepEqual(out.favorites.map((s) => s.stationuuid), [opaque]);
});

await check("toWire strips the cache's own book-keeping", () => {
  const wire = toWire({ ...station(), _syncAttempts: 2, _localOnly: true });
  assert.equal("_syncAttempts" in wire, false);
  assert.equal("_localOnly" in wire, false);
  assert.equal(attemptsOf(withAttempts(station(), 2)), 2);
  assert.equal(attemptsOf(station()), 0);
});

/* ── write-through ───────────────────────────────────────────────────────── */

await check("add reports a refusal so the optimistic star can be reverted", async () => {
  installStorage({ [SYNCED_KEY]: "1" });
  installFetch(() => ({ ok: false, error: "favorites_full", max: 40 }));
  const res = await addRemote(station());
  assert.equal(res.ok, false);
  assert.equal(res.error, "favorites_full");
  assert.equal(res.max, 40);

  // A dead network is a refusal too — never a silent success.
  installFetch(() => null);
  const dead = await addRemote(station());
  assert.equal(dead.ok, false);
  assert.equal(dead.error, "unreachable");
});

await check("remove goes by id, and by url when there is no id", async () => {
  const calls = installFetch(() => ({ ok: true, favorites: [] }));
  await removeRemote(station({ stationuuid: "s12345" }));
  assert.deepEqual(calls[0].params, { id: "tunein:s12345" });
  await removeRemote(station({ stationuuid: "" }));
  assert.deepEqual(calls[1].params, { url: "http://stream.example/one" });
});

/* ── cache ───────────────────────────────────────────────────────────────── */

await check("the cache round-trips and shrugs off junk", () => {
  const store = installStorage();
  saveCached([station()]);
  assert.deepEqual(loadCached().map((s) => s.stationuuid), ["uuid-1"]);
  store.set(FAVS_KEY, "{not json");
  assert.deepEqual(loadCached(), []);
  store.set(FAVS_KEY, JSON.stringify([{ name: "no id" }, station()]));
  assert.equal(loadCached().length, 1);
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
