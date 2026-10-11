"use client";

/**
 * WorldRadio — a native internet-radio player for the Media hub.
 *
 * Catalog comes from one of two open directories — radio-browser.info (the
 * volunteer directory Home Assistant's Radio Browser integration also uses,
 * via /api/radio/search) or TuneIn's OPML directory (via /api/tunein/search) —
 * chosen with the source toggle. Either way the browser only ever talks to its
 * own origin for metadata. The audio itself is a plain HTMLAudioElement pointed
 * at the station's stream, which is why middleware.ts opens media-src.
 *
 * The element does NOT live in this component — see ./audio.ts. This UI is a
 * subscriber to a module-level singleton, so navigating off /dashboard/media
 * leaves the music playing, exactly like the persistent JellyfinFrame.
 *
 * radio-browser rows arrive already playable (url_resolved is a stream). TuneIn
 * rows carry only a guide_id; the stream is resolved lazily on play through
 * /api/tunein/resolve, so we resolve exactly the one station the user clicks.
 *
 * No station artwork: img-src stays closed to third parties, so every station
 * gets the same generic glyph rather than a broken image.
 *
 * puff puff pass
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import MsIcon from "@/app/_components/MsIcon";
import { bridgeRefusalMessage } from "@/app/_lib/bridgeRefusal";
import {
  FREDOKA,
  JBM,
  NUNITO,
  MicroLabel,
} from "@/app/_components/Widgets/DesignTile";
import {
  getServerState,
  getState,
  hydrate,
  setVolume,
  stop,
  subscribe,
  toggle,
  type Station,
} from "./audio";
import { useApplianceRadio } from "./appliance";
import {
  FAVS_MAX,
  addRemote,
  isStorable,
  loadCached,
  removeRemote,
  resolveTuneIn,
  sameStation,
  saveCached,
  syncFavorites,
  toWireId,
  type RemoteResult,
} from "./favorites";

const SEARCH_DEBOUNCE_MS = 350;
const RESULT_LIMIT = 30;
/** Slowest honest cadence for "voice just saved a station" to reach the wall. */
const FAV_REFRESH_MS = 60_000;
/** A tab flicked away and back does not deserve a second round-trip. */
const FAV_VISIBLE_MIN_GAP_MS = 5_000;
/** How long the favorites note stays up before it stops being news. */
const FAV_NOTE_MS = 6_000;

/**
 * Why a star did not stick. Never silent: a favorite that quietly fails to
 * save is one the owner believes is saved until the day they look for it.
 */
function favFailureNote(res: RemoteResult): string {
  if (res.ok) return "";
  if (res.error === "favorites_full") {
    return `Favorites are full (${res.max ?? FAVS_MAX}).`;
  }
  if (res.error === "invalid_station") return "Couldn't save that station.";
  // Unreachable, or a refusal we have no better words for. Same honest line:
  // the station is not on the household list.
  return "Couldn't reach Puff — not saved.";
}

/** The two catalog backends the toggle switches between. */
type RadioSource = "radio-browser" | "tunein";

/**
 * A TuneIn station is identified by a guide_id like "s328971" (never a UUID),
 * which is how we tell its rows apart from radio-browser's — a TuneIn row is
 * playable-once-resolved even before it has a url_resolved.
 */
const isTuneIn = (s: Station) => /^s\d+$/.test(s.stationuuid);

/** Genre chips. `tag: null` = the catalog's most-voted stations overall. */
const GENRES: { label: string; tag: string | null }[] = [
  { label: "Top", tag: null },
  { label: "News", tag: "news" },
  { label: "Jazz", tag: "jazz" },
  { label: "Classical", tag: "classical" },
  { label: "Rock", tag: "rock" },
  { label: "Pop", tag: "pop" },
  { label: "Country", tag: "country" },
  { label: "Electronic", tag: "electronic" },
  { label: "Chill", tag: "chillout" },
];

/* ── favorites ──────────────────────────────────────────────────────────────
 *
 * The list itself belongs to the household and lives on the backend, shared
 * with voice ("save this station"); see ./favorites.ts for the sync rules.
 * localStorage is the offline cache behind it — favorites still render, and
 * still play, when the backend is unreachable.
 */

/* ── row ────────────────────────────────────────────────────────────────── */

function detail(s: Station): string {
  const bits = [
    s.country || s.countrycode,
    s.codec ? (s.bitrate ? `${s.codec} ${s.bitrate}k` : s.codec) : "",
  ].filter(Boolean);
  return bits.join(" · ");
}

function StationRow({
  station,
  active,
  status,
  isFav,
  onToggleFav,
  onPlay,
  pending = false,
  unavailable = false,
}: {
  station: Station;
  active: boolean;
  status: "idle" | "connecting" | "playing" | "paused" | "error";
  isFav: boolean;
  onToggleFav: (s: Station) => void;
  onPlay: (s: Station) => void;
  /** TuneIn: this row is resolving its stream URL (pre-playback). */
  pending?: boolean;
  /** TuneIn: resolution failed — no stream to hand the player. */
  unavailable?: boolean;
}) {
  const failed = (active && status === "error") || unavailable;
  const live = active && status === "playing";
  const busy = (active && status === "connecting") || pending;

  return (
    <div
      className="flex items-center gap-3 rounded-2xl px-3.5 py-2.5"
      style={{
        background: active ? "var(--chip)" : "var(--card2)",
        border: "1px solid",
        borderColor: active ? "var(--line)" : "transparent",
      }}
    >
      <button
        type="button"
        onClick={() => onPlay(station)}
        aria-label={live ? `Stop ${station.name}` : `Play ${station.name}`}
        className="grid h-9 w-9 flex-none cursor-pointer place-items-center rounded-full border transition-opacity hover:opacity-80"
        style={{
          borderColor: "var(--line)",
          background: live ? "var(--pink)" : "var(--card)",
          color: live ? "#fff" : failed ? "var(--puff-red)" : "var(--text2)",
        }}
      >
        <MsIcon
          name={failed ? "error" : live ? "stop" : busy ? "hourglass_top" : "play_arrow"}
          size={20}
          fill={live}
        />
      </button>

      <div className="min-w-0 flex-1">
        <div
          className="truncate"
          style={{
            fontFamily: NUNITO,
            fontWeight: 600,
            fontSize: 15,
            color: failed ? "var(--puff-red)" : active ? "var(--pink)" : "var(--text)",
          }}
        >
          {station.name}
        </div>
        <div
          className="truncate"
          style={{
            fontFamily: JBM,
            fontWeight: 600,
            fontSize: 10.5,
            letterSpacing: "0.06em",
            color: "var(--text3)",
            textTransform: "uppercase",
          }}
        >
          {failed
            ? "Unavailable — stream did not respond"
            : busy
              ? "Tuning in…"
              : detail(station)}
        </div>
      </div>

      {station.votes > 0 && (
        <span
          className="hidden flex-none sm:inline"
          title={`${station.votes} votes`}
          style={{
            fontFamily: JBM,
            fontWeight: 700,
            fontSize: 10.5,
            letterSpacing: "0.06em",
            color: "var(--text3)",
          }}
        >
          ♥ {station.votes}
        </span>
      )}

      <button
        type="button"
        onClick={() => onToggleFav(station)}
        aria-label={isFav ? `Remove ${station.name} from favorites` : `Save ${station.name} to favorites`}
        aria-pressed={isFav}
        className="grid h-8 w-8 flex-none cursor-pointer place-items-center rounded-full transition-opacity hover:opacity-70"
        style={{ background: "transparent", color: isFav ? "var(--lemon)" : "var(--text3)" }}
      >
        <MsIcon name="star" size={19} fill={isFav} />
      </button>
    </div>
  );
}

/* ── player ─────────────────────────────────────────────────────────────── */

export default function WorldRadio() {
  const radio = useSyncExternalStore(subscribe, getState, getServerState);
  /** The box's OWN player — what voice starts. Not this browser's element. */
  const appliance = useApplianceRadio();

  const [source, setSource] = useState<RadioSource>("radio-browser");
  const [favs, setFavs] = useState<Station[]>([]);
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [tag, setTag] = useState<string | null>(null);
  const [results, setResults] = useState<Station[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The server refused because a Bridge switch is off: its own sentence. */
  const [bridgeNote, setBridgeNote] = useState<string | null>(null);
  /** TuneIn only: the station whose stream is resolving right now. */
  const [pendingId, setPendingId] = useState<string | null>(null);
  /** TuneIn only: a station whose resolve failed (shown as unavailable). */
  const [failedId, setFailedId] = useState<string | null>(null);
  /** One inline line under Favorites — today only "the list is full". */
  const [favNote, setFavNote] = useState<string | null>(null);

  /** Favorites the household list cannot hold yet (TuneIn, stream unresolved). */
  const localOnlyRef = useRef<Station[]>([]);
  const favsRef = useRef<Station[]>([]);
  const syncingRef = useRef(false);
  const lastSyncRef = useRef(0);
  const noteTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    favsRef.current = favs;
  }, [favs]);

  /** One line, no animation, gone again in six seconds. */
  const note = useCallback((text: string | null) => {
    if (noteTimerRef.current) clearTimeout(noteTimerRef.current);
    noteTimerRef.current = null;
    setFavNote(text);
    if (!text) return;
    noteTimerRef.current = setTimeout(() => setFavNote(null), FAV_NOTE_MS);
  }, []);

  useEffect(
    () => () => {
      if (noteTimerRef.current) clearTimeout(noteTimerRef.current);
    },
    []
  );

  const applyFavs = useCallback((next: Station[]) => {
    setFavs(next);
    saveCached(next);
  }, []);

  /** The household list is truth; local-only rows ride along at the end. */
  const applyRemote = useCallback(
    (remote: Station[]) => {
      applyFavs([
        ...remote,
        ...localOnlyRef.current.filter((p) => !remote.some((r) => sameStation(r, p))),
      ]);
    },
    [applyFavs]
  );

  /**
   * Pull the household list. A backend that is down, restarting or refusing
   * leaves the cached list exactly as it is — no error UI, no lost favorites,
   * and the first-sync flag stays unset so the handover is retried later.
   */
  const syncFavs = useCallback(
    async (retryPending: boolean) => {
      if (syncingRef.current) return;
      syncingRef.current = true;
      lastSyncRef.current = Date.now();
      try {
        const out = await syncFavorites(loadCached(), { retryPending });
        if (!out.favorites) return;
        localOnlyRef.current = out.localOnly;
        applyRemote(out.favorites);
        if (out.note) note(out.note);
      } finally {
        syncingRef.current = false;
      }
    },
    [applyRemote, note]
  );

  // localStorage + the audio element's persisted volume are client-only. Show
  // the cache first, then let the household list correct it.
  useEffect(() => {
    hydrate();
    setFavs(loadCached());
    void syncFavs(true);
  }, [syncFavs]);

  // A station saved by voice should appear on the wall without a reload — but
  // slowly. One minute, plus the moment the tab comes back into view. The
  // returning-tab pass is a read only: no resolve storm on every glance.
  useEffect(() => {
    const iv = setInterval(() => void syncFavs(true), FAV_REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      if (Date.now() - lastSyncRef.current < FAV_VISIBLE_MIN_GAP_MS) return;
      void syncFavs(false);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(iv);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [syncFavs]);

  // Debounce so typing doesn't hammer the volunteer catalog.
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(
      () => setDebounced(query.trim()),
      SEARCH_DEBOUNCE_MS
    );
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query]);

  useEffect(() => {
    const controller = new AbortController();

    // TuneIn is a pure name search with no genre chips and no top-stations
    // default, so an empty box just clears the list rather than fetching.
    if (source === "tunein" && !debounced) {
      setResults([]);
      setLoading(false);
      setError(null);
      setBridgeNote(null);
      return () => controller.abort();
    }

    let url: string;
    if (source === "tunein") {
      const params = new URLSearchParams({
        q: debounced,
        limit: String(RESULT_LIMIT),
      });
      url = `/api/tunein/search?${params.toString()}`;
    } else {
      const params = new URLSearchParams({ limit: String(RESULT_LIMIT) });
      if (debounced) params.set("name", debounced);
      // A genre chip narrows a name search; "Top" (null) leaves it wide open.
      if (tag) params.set("tag", tag);
      url = `/api/radio/search?${params.toString()}`;
    }

    setLoading(true);
    setError(null);

    fetch(url, { signal: controller.signal, cache: "no-store" })
      .then(async (res) => {
        const data = await res.json().catch(() => null);
        const blocked = res.ok ? null : bridgeRefusalMessage(data);
        setBridgeNote(blocked);
        if (blocked) {
          setResults([]);
          return;
        }
        if (!res.ok) {
          throw new Error(
            (data && typeof data.error === "string" && data.error) ||
              `catalog error ${res.status}`
          );
        }
        setResults(Array.isArray(data?.stations) ? data.stations : []);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setResults([]);
        setError(
          err instanceof Error ? err.message : "radio catalog unreachable"
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
  }, [debounced, tag, source]);

  /**
   * Star state by IDENTITY — the wire id, or the stream URL for a row the
   * catalog gave no id. Never the name: two stations called "Jazz FM" are two
   * stations, and the backend keys them apart the same way.
   */
  const favKeys = useMemo(() => {
    const ids = new Set<string>();
    const urls = new Set<string>();
    for (const f of favs) {
      if (f.stationuuid) ids.add(toWireId(f.stationuuid));
      if (f.url_resolved) urls.add(f.url_resolved);
    }
    return { ids, urls };
  }, [favs]);

  const isFavStation = useCallback(
    (s: Station) =>
      (!!s.stationuuid && favKeys.ids.has(toWireId(s.stationuuid))) ||
      (!!s.url_resolved && favKeys.urls.has(s.url_resolved)),
    [favKeys]
  );

  const patchResult = useCallback((resolved: Station) => {
    setResults((prev) =>
      prev.map((r) => (r.stationuuid === resolved.stationuuid ? resolved : r))
    );
  }, []);

  /** Write a station's resolved stream back into results + favorites so a
   *  replay (and offline favorites) skip the resolve round-trip. A favorite
   *  is re-sent to the backend under the same identity, which updates the
   *  household row in place — so voice replays it without resolving either. */
  const rememberResolved = useCallback(
    (resolved: Station) => {
      patchResult(resolved);
      if (!favsRef.current.some((f) => sameStation(f, resolved))) return;
      applyFavs(
        favsRef.current.map((f) => (sameStation(f, resolved) ? resolved : f))
      );
      localOnlyRef.current = localOnlyRef.current.filter(
        (x) => !sameStation(x, resolved)
      );
      void addRemote(resolved).then((res) => {
        if (res.ok) applyRemote(res.favorites);
        else localOnlyRef.current = [...localOnlyRef.current, resolved];
      });
    },
    [applyFavs, applyRemote, patchResult]
  );

  /**
   * Star / unstar — optimistic here, written through to the household list,
   * and reverted if the backend says no (or says nothing at all).
   */
  const handleToggleFav = useCallback(
    async (s: Station) => {
      note(null);
      const before = favsRef.current;
      const beforeLocalOnly = localOnlyRef.current;
      const wasFav = before.some((f) => sameStation(f, s));
      // A new favorite goes to the TOP, which is where the backend puts it —
      // so the optimistic row does not jump when the real list arrives.
      applyFavs(
        wasFav ? before.filter((f) => !sameStation(f, s)) : [s, ...before]
      );

      const revert = (res: RemoteResult) => {
        localOnlyRef.current = beforeLocalOnly;
        applyFavs(before);
        note(favFailureNote(res));
      };

      if (wasFav) {
        localOnlyRef.current = beforeLocalOnly.filter((x) => !sameStation(x, s));
        const res = await removeRemote(s);
        if (res.ok) applyRemote(res.favorites);
        else revert(res);
        return;
      }

      // The backend refuses a row it could not play. A TuneIn station with no
      // stream yet gets one resolve attempt; if that fails it stays this
      // browser's favorite alone and is offered again on the next load.
      let station = s;
      if (!isStorable(station)) {
        const resolved = isTuneIn(station) ? await resolveTuneIn(station) : null;
        if (!resolved) {
          localOnlyRef.current = [...beforeLocalOnly, station];
          return;
        }
        patchResult(resolved);
        station = resolved;
      }

      const res = await addRemote(station);
      if (res.ok) applyRemote(res.favorites);
      else revert(res);
    },
    [applyFavs, applyRemote, note, patchResult]
  );

  const switchSource = useCallback(
    (next: RadioSource) => {
      if (next === source) return;
      setResults([]);
      setError(null);
      setBridgeNote(null);
      setFailedId(null);
      setPendingId(null);
      setSource(next);
    },
    [source]
  );

  /**
   * A stop pressed here means "quiet", not "quiet in this tab". If the box's
   * own player is running (a voice-started station), silence it too — read
   * through a ref so this stays a stable callback despite the polling hook.
   */
  const applianceRef = useRef(appliance);
  useEffect(() => {
    applianceRef.current = appliance;
  }, [appliance]);
  const silenceAppliance = useCallback(() => {
    if (applianceRef.current.active) void applianceRef.current.stop();
  }, []);

  const handlePlay = useCallback(
    async (s: Station) => {
      // toggle() on the station already playing here is a STOP — the moment to
      // take the appliance's stream down with it. Read live state rather than
      // the render's copy so this callback needs no status deps.
      const live = getState();
      if (
        live.station?.stationuuid === s.stationuuid &&
        live.status !== "paused" &&
        live.status !== "error"
      ) {
        silenceAppliance();
      }
      // radio-browser rows (and already-resolved TuneIn favorites) carry a
      // stream URL — play immediately.
      if (s.url_resolved) {
        toggle(s);
        return;
      }
      // No URL and not a TuneIn guide_id → nothing we can resolve.
      if (!isTuneIn(s)) return;

      setFailedId((id) => (id === s.stationuuid ? null : id));
      setPendingId(s.stationuuid);
      try {
        const res = await fetch(
          `/api/tunein/resolve?id=${encodeURIComponent(s.stationuuid)}`,
          { cache: "no-store" }
        );
        const data = await res.json().catch(() => null);
        const blocked = res.ok ? null : bridgeRefusalMessage(data);
        if (blocked) setBridgeNote(blocked);
        const url =
          data && typeof data.url_resolved === "string" ? data.url_resolved : "";
        if (!res.ok || !url) throw new Error("resolve failed");

        const resolved: Station = { ...s, url_resolved: url };
        rememberResolved(resolved);
        toggle(resolved);
      } catch {
        setFailedId(s.stationuuid);
      } finally {
        setPendingId((id) => (id === s.stationuuid ? null : id));
      }
    },
    [rememberResolved, silenceAppliance]
  );

  // radio-browser rows are pre-filtered for HLS/empty URLs, but this list also
  // includes localStorage favorites from older builds — re-check here. TuneIn
  // rows are playable once resolved, so they pass on guide_id alone.
  const playable = useCallback(
    (s: Station) =>
      isTuneIn(s) || (!!s.url_resolved && !s.url_resolved.includes(".m3u8")),
    []
  );

  const shownFavs = favs.filter(playable);
  const shownResults = results.filter(playable);

  const current = radio.station;
  const isPlaying = radio.status === "playing";
  const statusLabel =
    radio.status === "playing"
      ? "On air"
      : radio.status === "connecting"
        ? "Tuning in…"
        : radio.status === "paused"
          ? "Paused"
          : radio.status === "error"
            ? "Stream unavailable"
            : "Nothing playing";

  return (
    <section
      id="world-radio"
      className="relative overflow-hidden rounded-[28px] border backdrop-blur-[18px]"
      style={{
        borderColor: "var(--line)",
        background: "var(--card)",
        padding: "18px 20px 20px",
      }}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/design-cloud-3.jpg"
        alt=""
        aria-hidden
        className="pointer-events-none absolute inset-0 h-full w-full object-cover"
        style={{ opacity: "var(--imgTile)" }}
      />

      <div className="relative">
        {/* header */}
        <div className="mb-3 flex items-center gap-2">
          <MsIcon name="radio" size={19} style={{ color: "var(--sky)" }} />
          <div
            style={{
              fontFamily: FREDOKA,
              fontWeight: 600,
              fontSize: 18,
              color: "var(--text)",
            }}
          >
            World Radio
          </div>
          <div className="flex-1" />
          <MicroLabel>{source === "tunein" ? "TuneIn" : "radio-browser"}</MicroLabel>
        </div>

        {/* player bar */}
        <div
          className="mb-4 flex flex-wrap items-center gap-3 rounded-2xl px-3.5 py-3"
          style={{ background: "var(--card2)", border: "1px solid var(--line)" }}
        >
          <button
            type="button"
            onClick={() => (current ? handlePlay(current) : undefined)}
            disabled={!current}
            aria-label={isPlaying ? "Stop radio" : "Play radio"}
            className="grid h-10 w-10 flex-none cursor-pointer place-items-center rounded-full border transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-40"
            style={{
              borderColor: "var(--line)",
              background: isPlaying ? "var(--pink)" : "var(--card)",
              color: isPlaying ? "#fff" : "var(--text2)",
            }}
          >
            <MsIcon name={isPlaying ? "stop" : "play_arrow"} size={22} fill={isPlaying} />
          </button>

          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              {/* Deliberately STATIC: radio plays for hours, and a .puff-live
                  pulse would keep the compositor at 60fps that whole time on a
                  kiosk parked here — the exact cost the motion governor exists
                  to prevent. The dot's presence alone means "on air". */}
              {isPlaying && (
                <span
                  className="inline-block h-2 w-2 flex-none rounded-full"
                  style={{ background: "var(--pink)" }}
                />
              )}
              <div
                className="truncate"
                style={{
                  fontFamily: NUNITO,
                  fontWeight: 700,
                  fontSize: 15,
                  color: current ? "var(--text)" : "var(--text3)",
                }}
              >
                {current ? current.name : "No station selected"}
              </div>
            </div>
            <MicroLabel
              style={{
                color:
                  radio.status === "error" ? "var(--puff-red)" : "var(--text3)",
              }}
            >
              {statusLabel}
              {current && radio.status !== "error" ? ` · ${detail(current)}` : ""}
            </MicroLabel>
          </div>

          <div className="flex flex-none items-center gap-2">
            <MsIcon
              name={radio.volume === 0 ? "volume_off" : "volume_up"}
              size={19}
              style={{ color: "var(--text3)" }}
            />
            <input
              type="range"
              min={0}
              max={1}
              step={0.01}
              value={radio.volume}
              onChange={(e) => setVolume(Number.parseFloat(e.target.value))}
              aria-label="Radio volume"
              className="w-28 cursor-pointer accent-[var(--pink)]"
            />
            {current && (
              <button
                type="button"
                onClick={() => {
                  silenceAppliance();
                  stop();
                }}
                aria-label="Clear station"
                className="grid h-8 w-8 cursor-pointer place-items-center rounded-full transition-opacity hover:opacity-70"
                style={{ color: "var(--text3)" }}
              >
                <MsIcon name="close" size={18} />
              </button>
            )}
          </div>
        </div>

        {/* The appliance's own stream — voice-started, playing out of the box's
            speakers with no browser involved. Shown on EVERY surface, kiosk
            included: the wall display is precisely where it is audible, and
            before this it had no stop anywhere in the UI. */}
        {appliance.active && (
          <div
            className="puff-appliance-row mb-4 flex items-center gap-3 rounded-2xl px-3.5 py-2.5"
            style={{ background: "var(--card2)", border: "1px solid var(--line)" }}
          >
            <button
              type="button"
              onClick={() => void appliance.stop()}
              disabled={appliance.busy}
              aria-label="Stop the PuffPAI radio"
              className="grid h-9 w-9 flex-none cursor-pointer place-items-center rounded-full border transition-opacity hover:opacity-80 disabled:opacity-40"
              style={{
                borderColor: "var(--line)",
                background:
                  appliance.status === "playing" ? "var(--pink)" : "var(--card)",
                color: appliance.status === "playing" ? "#fff" : "var(--text2)",
              }}
            >
              <MsIcon name="stop" size={20} fill={appliance.status === "playing"} />
            </button>
            <div className="min-w-0 flex-1">
              <div
                className="truncate"
                style={{
                  fontFamily: NUNITO,
                  fontWeight: 600,
                  fontSize: 15,
                  color: "var(--text)",
                }}
              >
                {appliance.station}
              </div>
              <MicroLabel>
                {`Radio · PuffPAI speakers · ${
                  appliance.status === "playing"
                    ? "On air"
                    : appliance.status === "connecting"
                      ? "Tuning in…"
                      : "Paused"
                }`}
              </MicroLabel>
            </div>
          </div>
        )}

        {/* source toggle */}
        <div className="mb-3 flex items-center gap-2">
          <MicroLabel>Source</MicroLabel>
          <div
            className="flex gap-1 rounded-full p-1"
            style={{ background: "var(--card2)", border: "1px solid var(--line)" }}
          >
            {(
              [
                ["radio-browser", "radio-browser"],
                ["tunein", "TuneIn"],
              ] as const
            ).map(([val, label]) => {
              const on = source === val;
              return (
                <button
                  key={val}
                  type="button"
                  onClick={() => switchSource(val)}
                  aria-pressed={on}
                  className="cursor-pointer rounded-full px-3 py-1 transition-opacity hover:opacity-80"
                  style={{
                    fontFamily: JBM,
                    fontWeight: 700,
                    fontSize: 10.5,
                    letterSpacing: "0.1em",
                    textTransform: "uppercase",
                    background: on ? "var(--pink)" : "transparent",
                    color: on ? "#fff" : "var(--text2)",
                  }}
                >
                  {label}
                </button>
              );
            })}
          </div>
        </div>

        {/* search + genres */}
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div
            className="flex min-w-[220px] flex-1 items-center gap-2 rounded-full px-3.5 py-2"
            style={{ background: "var(--card2)", border: "1px solid var(--line)" }}
          >
            <MsIcon name="search" size={18} style={{ color: "var(--text3)" }} />
            <input
              id="world-radio-search"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={
                source === "tunein"
                  ? "Search TuneIn — station, city, or callsign"
                  : "Search stations"
              }
              aria-label="Search radio stations by name"
              className="w-full bg-transparent outline-none"
              style={{
                fontFamily: NUNITO,
                fontWeight: 600,
                fontSize: 14,
                color: "var(--text)",
              }}
            />
          </div>
        </div>

        {/* genre chips — radio-browser only; TuneIn has no tag search */}
        {source === "radio-browser" && (
          <div className="mb-4 flex flex-wrap gap-2">
            {GENRES.map((g) => {
              const on = g.tag === tag;
              return (
                <button
                  key={g.label}
                  type="button"
                  onClick={() => setTag(g.tag)}
                  aria-pressed={on}
                  className="cursor-pointer rounded-full px-3 py-1.5 transition-opacity hover:opacity-80"
                  style={{
                    fontFamily: JBM,
                    fontWeight: 700,
                    fontSize: 10.5,
                    letterSpacing: "0.12em",
                    textTransform: "uppercase",
                    border: "1px solid var(--line)",
                    background: on ? "var(--pink)" : "var(--card2)",
                    color: on ? "#fff" : "var(--text2)",
                  }}
                >
                  {g.label}
                </button>
              );
            })}
          </div>
        )}

        {/* favorites */}
        {/* The label row also carries the only favorites message there is, so
            it has to exist when a star failed on an empty list too. Nothing
            renders when there is neither — no reserved gap, no layout shift. */}
        {(shownFavs.length > 0 || favNote) && (
          <div className="mb-4">
            <MicroLabel className="mb-2">
              Favorites
              {favNote ? (
                <span style={{ color: "var(--puff-red)" }}> · {favNote}</span>
              ) : (
                ""
              )}
            </MicroLabel>
            <div className="flex flex-col gap-2">
              {shownFavs.map((s) => (
                <StationRow
                  key={`fav-${s.stationuuid}`}
                  station={s}
                  active={current?.stationuuid === s.stationuuid}
                  status={radio.status}
                  isFav
                  onToggleFav={handleToggleFav}
                  onPlay={handlePlay}
                  pending={pendingId === s.stationuuid}
                  unavailable={failedId === s.stationuuid}
                />
              ))}
            </div>
          </div>
        )}

        {/* results */}
        <MicroLabel className="mb-2">
          {debounced ? `Results · ${debounced}` : "Stations"}
        </MicroLabel>

        {bridgeNote ? (
          <div
            className="rounded-2xl px-3.5 py-3"
            style={{
              background: "var(--card2)",
              border: "1px solid var(--line)",
              fontFamily: NUNITO,
              fontWeight: 600,
              fontSize: 14,
              color: "var(--text2)",
            }}
          >
            {bridgeNote} Favorites still play.
          </div>
        ) : error ? (
          <div
            className="rounded-2xl px-3.5 py-3"
            style={{
              background: "var(--card2)",
              border: "1px solid var(--line)",
              fontFamily: NUNITO,
              fontWeight: 600,
              fontSize: 14,
              color: "var(--puff-red)",
            }}
          >
            Station catalog unreachable. Favorites still play.
          </div>
        ) : loading && shownResults.length === 0 ? (
          <div className="flex flex-col gap-2">
            {[0, 1, 2, 3].map((i) => (
              <div
                key={i}
                className="h-[58px] rounded-2xl"
                style={{ background: "var(--card2)" }}
              />
            ))}
          </div>
        ) : shownResults.length === 0 ? (
          <div
            style={{
              fontFamily: NUNITO,
              fontWeight: 500,
              fontSize: 14,
              color: "var(--text3)",
            }}
          >
            {source === "tunein" && !debounced
              ? "Search TuneIn for a station, city, or callsign."
              : "No stations match."}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {shownResults.map((s) => (
              <StationRow
                key={s.stationuuid}
                station={s}
                active={current?.stationuuid === s.stationuuid}
                status={radio.status}
                isFav={isFavStation(s)}
                onToggleFav={handleToggleFav}
                onPlay={handlePlay}
                pending={pendingId === s.stationuuid}
                unavailable={failedId === s.stationuuid}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
