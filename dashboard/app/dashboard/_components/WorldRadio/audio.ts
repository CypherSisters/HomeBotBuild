/**
 * World Radio audio manager — ONE HTMLAudioElement for the whole app, owned by
 * this module rather than by any React tree.
 *
 * This is the same problem JellyfinFrame solves for the iframe: a player that
 * lives inside a route's component tree dies the moment the router unmounts
 * that route, so walking from Media to Home would silence the radio. A
 * module-level singleton outlives every render and every navigation — the
 * player UI on /dashboard/media is only a SUBSCRIBER to this state, and the
 * stream keeps running while the user browses elsewhere.
 *
 * The element is created lazily on first use so importing this module is safe
 * during SSR (no `document` at module scope).
 *
 * Status is reported honestly, never optimistically: "playing" is set from the
 * element's own `playing` event, not from the call to play(). Streams that
 * 404, refuse CORS-free playback, or die mid-song land in "error" — the UI
 * shows the station as unavailable instead of pretending sound is happening.
 */

export interface Station {
  stationuuid: string;
  name: string;
  url_resolved: string;
  country: string;
  countrycode: string;
  codec: string;
  bitrate: number;
  tags: string;
  votes: number;
  homepage: string;
}

/**
 * idle       nothing selected
 * connecting selected, buffering (or stalled mid-stream — no audio right now)
 * playing    audio is actually coming out
 * paused     silenced by a pause we didn't initiate (OS media key, phone
 *            media notification, headset button); station kept, resumable
 * error      the stream failed; `station` is kept so the UI can name it
 */
export type RadioStatus = "idle" | "connecting" | "playing" | "paused" | "error";

export interface RadioState {
  station: Station | null;
  status: RadioStatus;
  volume: number;
}

const VOLUME_KEY = "puffpai_radio_volume";
const DEFAULT_VOLUME = 0.7;

let audio: HTMLAudioElement | null = null;
/** Guards the click report so it fires once per station start, not per event. */
let reportedUuid: string | null = null;

let state: RadioState = {
  station: null,
  status: "idle",
  volume: DEFAULT_VOLUME,
};

const listeners = new Set<() => void>();

function emit() {
  // forEach, not for..of: tsconfig targets ES5 and Set iteration would need
  // downlevelIteration.
  listeners.forEach((fn) => fn());
}

function setState(patch: Partial<RadioState>) {
  const next = { ...state, ...patch };
  if (
    next.station === state.station &&
    next.status === state.status &&
    next.volume === state.volume
  ) {
    return;
  }
  state = next;
  emit();
}

function loadVolume(): number {
  try {
    const raw = localStorage.getItem(VOLUME_KEY);
    if (raw === null) return DEFAULT_VOLUME;
    const v = Number.parseFloat(raw);
    return Number.isFinite(v) ? Math.min(Math.max(v, 0), 1) : DEFAULT_VOLUME;
  } catch {
    return DEFAULT_VOLUME;
  }
}

/** Tell radio-browser what we tuned to (catalog etiquette). Never blocks. */
function reportClick(uuid: string) {
  if (reportedUuid === uuid) return;
  reportedUuid = uuid;
  void fetch(`/api/radio/click/${encodeURIComponent(uuid)}`, {
    method: "POST",
    keepalive: true,
  }).catch(() => {
    /* counter is courtesy, not correctness */
  });
}

function ensureAudio(): HTMLAudioElement {
  if (audio) return audio;

  const el = new Audio();
  el.preload = "none";
  // Live streams have no meaningful buffer to seek in; keep the element from
  // trying to be clever about it.
  el.autoplay = false;
  el.volume = loadVolume();
  state = { ...state, volume: el.volume };
  // Emit, don't mutate silently: without this, subscribers that rendered
  // before the element existed only ever see the persisted volume by luck
  // (hydrate()'s setState short-circuits on the already-matching value).
  emit();

  el.addEventListener("playing", () => {
    setState({ status: "playing" });
    if (state.station) reportClick(state.station.stationuuid);
  });
  // Buffer underrun / no data arriving: honest "not currently audible".
  el.addEventListener("waiting", () => {
    if (state.station) setState({ status: "connecting" });
  });
  el.addEventListener("stalled", () => {
    if (state.station) setState({ status: "connecting" });
  });
  el.addEventListener("error", () => {
    if (state.station) setState({ status: "error" });
  });
  // A live stream that "ends" has dropped us, not finished.
  el.addEventListener("ended", () => {
    if (state.station) setState({ status: "error" });
  });
  // A pause we didn't initiate (OS media key, phone media notification,
  // headset button) silences the element — say so instead of keeping a fake
  // "playing". Our own teardowns are safe from this: stop() writes
  // station:null/status:idle synchronously, and play() writes "connecting",
  // both before this queued event runs, so the status guard no-ops for them.
  el.addEventListener("pause", () => {
    if (state.status === "playing") setState({ status: "paused" });
  });

  audio = el;
  return el;
}

/** Read the current snapshot (useSyncExternalStore-compatible). */
export function getState(): RadioState {
  return state;
}

/** Server snapshot: stable object, never touches the DOM. */
const SERVER_STATE: RadioState = {
  station: null,
  status: "idle",
  volume: DEFAULT_VOLUME,
};
export function getServerState(): RadioState {
  return SERVER_STATE;
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Pull the persisted volume into the live element (call once on mount). */
export function hydrate() {
  const el = ensureAudio();
  const v = loadVolume();
  if (el.volume !== v) el.volume = v;
  setState({ volume: v });
}

export function play(station: Station) {
  const el = ensureAudio();
  reportedUuid = null;
  setState({ station, status: "connecting" });
  el.src = station.url_resolved;
  el.load();
  void el.play().catch(() => {
    // Autoplay refusal or an unplayable stream — either way there is no sound,
    // so say error rather than leaving a spinner up forever.
    setState({ status: "error" });
  });
}

export function stop() {
  const el = ensureAudio();
  el.pause();
  el.removeAttribute("src");
  el.load();
  reportedUuid = null;
  setState({ station: null, status: "idle" });
}

/** Resume after an external pause. The stream picks back up at the live edge. */
export function resume() {
  const el = ensureAudio();
  if (state.station) setState({ status: "connecting" });
  void el.play().catch(() => {
    setState({ status: "error" });
  });
}

/** Stop if the given station is playing, resume it if paused, else start it. */
export function toggle(station: Station) {
  const same = state.station?.stationuuid === station.stationuuid;
  if (same && state.status === "paused") {
    resume();
  } else if (same && state.status !== "error") {
    stop();
  } else {
    play(station);
  }
}

export function setVolume(v: number) {
  // NaN survives min/max clamping and would throw on element assignment.
  if (!Number.isFinite(v)) return;
  const clamped = Math.min(Math.max(v, 0), 1);
  ensureAudio().volume = clamped;
  setState({ volume: clamped });
  try {
    localStorage.setItem(VOLUME_KEY, String(clamped));
  } catch {
    /* private mode — volume just won't persist */
  }
}
