"use client";

/**
 * Now Playing tile (design 4b.5) — every media source the house can be using,
 * in one tile.
 *
 * Sources, in the order they matter to someone standing in front of the
 * appliance:
 *
 *   Radio · this device   the World Radio singleton in THIS browser. Read from
 *                         the audio manager directly, not the backend: it is
 *                         the only source we can both see and control locally,
 *                         and it is the one the viewer can silence right now.
 *   Radio · PuffPAI       the appliance's own radio — the server-side player
 *                         that voice and chat drive. Shown in EVERY mode, the
 *                         kiosk included: the kiosk browser is not that
 *                         player, so suppressing the row on the wall display
 *                         hid every voice-started station.
 *                         Suppressed only when the backend marks it stale
 *                         ("playing" is no longer a fact) or when this browser
 *                         is already playing the same station itself, which
 *                         would read as a double.
 *   Spotify               display-only; the dashboard has no Spotify transport.
 *   Jellyfin              pause/resume via the backend, as before.
 *
 * Everything goes through the same-origin /api/pai proxy rather than an
 * env-supplied base. Direct backend calls only resolve on the kiosk — the CSP's
 * connect-src allows localhost, not the LAN IP — so the old direct fetch made
 * this tile silently empty on every phone in the house.
 *
 * Layout: one source gets the full treatment (art + progress). Two or three
 * get compact rows, because the tile is 2 grid columns wide at kiosk density
 * and a progress bar would cost a whole source its line.
 */

import { useEffect, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import MsIcon from "../MsIcon";
import DesignTile, { FREDOKA, HATCH, NUNITO, MicroLabel } from "./DesignTile";
import {
  getServerState,
  getState,
  subscribe,
  toggle,
  type RadioState,
} from "@/app/dashboard/_components/WorldRadio/audio";
import { stopApplianceRadio } from "@/app/dashboard/_components/WorldRadio/appliance";

const MAX_ROWS = 3;

interface NowPlayingSession {
  session_id: string;
  device: string;
  is_paused: boolean;
  position_seconds: number;
  now_playing: {
    item_id: string;
    title: string;
    artist: string;
    album: string;
    duration_seconds: number;
    art_url: string | null;
  } | null;
}

interface ApplianceRadio {
  status?: string;
  station?: { name?: string } | null;
  stale?: boolean;
  /** "appliance" = the backend's own player. Absent on older backends. */
  source?: string;
}

interface SpotifySource {
  active?: boolean;
  /** Audible right now — distinct from active (a paused session is active). */
  playing?: boolean;
  user?: string | null;
  track?: {
    title?: string;
    artists?: string | string[];
    album?: string;
    uri?: string;
  } | null;
}

interface Sources {
  radio?: ApplianceRadio | null;
  spotify?: SpotifySource | null;
}

/** A single line in the tile. `control` is omitted for display-only sources. */
interface Row {
  key: string;
  icon: string;
  label: string;
  line: string;
  dim?: boolean;
  /** Only true while sound is genuinely coming out (drives the .puff-live dot). */
  live?: boolean;
  art?: string | null;
  /** 0-100, or null when the source has no meaningful duration (streams). */
  pct?: number | null;
  control?: { icon: string; aria: string; onClick: () => void };
}

function joinArtists(a: string | string[] | undefined): string {
  if (Array.isArray(a)) return a.filter(Boolean).join(", ");
  return a ?? "";
}

export default function NowPlayingTile() {
  const router = useRouter();
  const [sessions, setSessions] = useState<NowPlayingSession[]>([]);
  const [sources, setSources] = useState<Sources>({});
  const [busy, setBusy] = useState(false);

  const radio: RadioState = useSyncExternalStore(
    subscribe,
    getState,
    getServerState,
  );

  const load = () => {
    // The /api/pai proxy strips its prefix verbatim (/api/pai/X → backend /X),
    // and the backend route really is /api/media/... — hence the double /api.
    fetch("/api/pai/api/media/now-playing", {
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        // Each half is applied independently: a backend that predates the
        // `sources` block still populates Jellyfin, and vice versa.
        if (Array.isArray(d?.sessions)) setSessions(d.sessions);
        if (d?.sources && typeof d.sources === "object") setSources(d.sources);
      })
      .catch(() => {
        // Backend restarting or not yet deployed — keep the last known rows.
      });
  };

  useEffect(() => {
    // Every mode polls, the kiosk included. The kiosk is NOT the appliance's
    // player: voice and chat
    // play through the backend's own radio player, so the wall display has to
    // ask the backend like any other client.
    load();
    const iv = setInterval(load, 5000);
    return () => clearInterval(iv);
  }, []);

  const jellyfin =
    sessions.find((s) => s.now_playing && !s.is_paused) ??
    sessions.find((s) => s.now_playing) ??
    null;
  const np = jellyfin?.now_playing ?? null;

  const toggleJellyfin = () => {
    if (!jellyfin || busy) return;
    setBusy(true);
    fetch("/api/pai/api/media/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: jellyfin.session_id,
        action: jellyfin.is_paused ? "resume" : "pause",
      }),
      signal: AbortSignal.timeout(10000),
    })
      .catch(() => {})
      .finally(() => {
        setBusy(false);
        setTimeout(load, 400);
      });
  };

  /* The appliance's radio: the backend's own server-side player. Read before
     the rows are built because the LOCAL row's stop needs it too. */
  const appliance = sources.radio;
  const applianceStatus = appliance?.status ?? "idle";
  // A backend that predates the two-slot split sends no `source`, and
  // everything it reported in this slot was the appliance — so a missing
  // source reads as one.
  const applianceOwned = (appliance?.source ?? "appliance") === "appliance";
  /** Is the box itself making (or about to make) radio noise right now? */
  const applianceActive =
    Boolean(appliance) &&
    applianceOwned &&
    !appliance?.stale &&
    Boolean(appliance?.station?.name) &&
    (applianceStatus === "playing" ||
      applianceStatus === "connecting" ||
      applianceStatus === "paused");

  /**
   * Stop the SERVER-SIDE player. Without this, a voice-started station would
   * have no control anywhere in the UI: the browser transport only ever pauses
   * this browser's own <audio>. The row is cleared optimistically so the tap reads as having
   * worked; the poll confirms it, or restores it if the backend disagrees.
   */
  const stopAppliance = () => {
    if (busy) return;
    setBusy(true);
    setSources((prev) => ({
      ...prev,
      radio: { status: "idle", station: null, source: "appliance" },
    }));
    void stopApplianceRadio().finally(() => {
      setBusy(false);
      setTimeout(load, 1200);
    });
  };

  const rows: Row[] = [];

  /* 1 — radio in this browser. Controllable without touching the network. */
  if (radio.station) {
    const silent = radio.status === "paused" || radio.status === "error";
    rows.push({
      key: "radio-local",
      icon: "radio",
      label: "Radio · this device",
      line: radio.station.name,
      live: radio.status === "playing",
      dim: radio.status === "error",
      control: {
        icon: silent ? "play_arrow" : "stop",
        aria: silent ? "Resume radio" : "Stop radio",
        // toggle() already means resume-if-paused / retry-if-errored / else stop.
        // When the box is playing too (the de-dup case, or two different
        // stations), a stop here silences that player as well: one tap should
        // mean "quiet", not "quiet in this tab only".
        onClick: () => {
          if (!radio.station) return;
          if (!silent && applianceActive) stopAppliance();
          toggle(radio.station);
        },
      },
    });
  }

  /* 2 — the appliance's radio: the backend's own server-side player. */
  // De-duplication: only when THIS browser is itself playing the same station
  // do the two rows say the same thing. Then the local row wins — it is the one
  // with a working stop button.
  const duplicatesLocal =
    radio.status === "playing" &&
    Boolean(radio.station?.name) &&
    appliance?.station?.name === radio.station?.name;
  if (
    appliance &&
    applianceOwned &&
    !duplicatesLocal &&
    !appliance.stale &&
    appliance.station?.name &&
    (applianceStatus === "playing" ||
      applianceStatus === "connecting" ||
      applianceStatus === "paused")
  ) {
    rows.push({
      key: "radio-appliance",
      icon: "speaker",
      label: "Radio · PuffPAI",
      line: appliance.station.name,
      live: applianceStatus === "playing",
      dim: applianceStatus !== "playing",
      // The only control in the UI that reaches the box's own player.
      control: {
        icon: "stop",
        aria: "Stop the PuffPAI radio",
        onClick: stopAppliance,
      },
    });
  }

  /* 3 — Spotify. Display-only: the dashboard owns no Spotify transport. */
  const spotify = sources.spotify;
  const track = spotify?.track ?? null;
  if (spotify && (spotify.active || track)) {
    const artists = joinArtists(track?.artists);
    rows.push({
      key: "spotify",
      icon: "graphic_eq",
      label: `Spotify · ${spotify.user || "Connect"}`,
      line: track?.title
        ? artists
          ? `${track.title} — ${artists}`
          : track.title
        : "Connected",
      // `playing` when the backend sends it (audible now); a paused session
      // must not read as live. Fall back to the old heuristic mid-rollout.
      live: spotify.playing !== undefined
        ? Boolean(spotify.playing)
        : Boolean(spotify.active && track),
      dim: !track,
    });
  }

  /* 5 — Jellyfin, unchanged behaviour. */
  if (jellyfin && np) {
    rows.push({
      key: "jellyfin",
      icon: "movie",
      label: `Jellyfin · ${jellyfin.device || "player"}`,
      line: np.artist ? `${np.artist} — ${np.title}` : np.title,
      live: !jellyfin.is_paused,
      art: np.art_url ?? "/design-cloud-5.jpg",
      pct:
        np.duration_seconds > 0
          ? Math.min(100, (100 * jellyfin.position_seconds) / np.duration_seconds)
          : null,
      control: {
        icon: jellyfin.is_paused ? "play_arrow" : "pause",
        aria: jellyfin.is_paused ? "Resume" : "Pause",
        onClick: toggleJellyfin,
      },
    });
  }

  const shown = rows.slice(0, MAX_ROWS);

  /* ── empty / single source: the full-size treatment ──────────────────── */
  if (shown.length <= 1) {
    const row = shown[0] ?? null;
    return (
      <DesignTile img="/design-cloud-4.jpg" padding="14px 18px">
        <div className="flex h-full items-center gap-3">
          {row?.art ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={row.art}
              alt=""
              className="puff-np-art h-[64px] w-[64px] flex-none rounded-[18px] object-cover"
            />
          ) : row ? (
            <div
              className="puff-np-art grid h-[64px] w-[64px] flex-none place-items-center rounded-[18px]"
              style={{ background: HATCH, color: "var(--text2)" }}
            >
              <MsIcon name={row.icon} size={28} />
            </div>
          ) : (
            <div
              className="puff-np-art h-[64px] w-[64px] flex-none rounded-[18px]"
              style={{ background: HATCH }}
            />
          )}

          <div className="min-w-0 flex-1">
            <MicroLabel style={{ marginBottom: 6 }}>
              {row ? row.label : "Media · local"}
            </MicroLabel>
            <div className="flex min-w-0 items-center gap-2">
              {/* Deliberately STATIC (no animate-pulse): radio plays for hours
                  and the kiosk sits on this page — an exempted .puff-live
                  pulse here would keep Chromium compositing at 60fps for the
                  whole session, the exact cost the motion governor exists to
                  prevent. Presence of the dot alone means "audible now". */}
              {row?.live && (
                <span
                  className="inline-block h-2 w-2 flex-none rounded-full"
                  style={{ background: "var(--pink)" }}
                />
              )}
              <div
                className="overflow-hidden text-ellipsis whitespace-nowrap"
                style={{
                  fontFamily: FREDOKA,
                  fontWeight: 600,
                  fontSize: 17,
                  color: row && !row.dim ? "var(--text)" : "var(--text3)",
                }}
              >
                {row ? row.line : "Nothing playing"}
              </div>
            </div>
            {row?.pct != null && (
              <div
                className="mt-2 h-1.5 overflow-hidden rounded-full"
                style={{ background: "var(--chip)" }}
              >
                <div
                  className="puff-meter-fill h-full rounded-full transition-all"
                  style={{ width: `${row.pct}%`, background: "var(--sky)" }}
                />
              </div>
            )}
          </div>

          <button
            type="button"
            onClick={
              row?.control
                ? row.control.onClick
                : () => router.push("/dashboard/media")
            }
            disabled={busy}
            aria-label={row?.control ? row.control.aria : "Open media"}
            className="grid h-[52px] w-[52px] flex-none cursor-pointer place-items-center rounded-full border transition hover:opacity-80 disabled:opacity-40"
            style={{
              borderColor: "var(--line)",
              background: "var(--card2)",
              color: "var(--text)",
            }}
          >
            <MsIcon name={row?.control?.icon ?? "play_arrow"} size={30} />
          </button>
        </div>
      </DesignTile>
    );
  }

  /* ── two or three sources: compact rows, no art, no progress ──────────── */
  return (
    <DesignTile img="/design-cloud-4.jpg" padding="12px 16px">
      <div className="flex h-full flex-col justify-center gap-2 overflow-hidden">
        {shown.map((row) => (
          <div key={row.key} className="flex min-w-0 items-center gap-2.5">
            <div
              className="grid h-7 w-7 flex-none place-items-center rounded-[10px]"
              style={{ background: "var(--chip)", color: "var(--text2)" }}
            >
              <MsIcon name={row.icon} size={16} />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex min-w-0 items-center gap-1.5">
                {/* Static dot — see the single-source branch for why no pulse. */}
                {row.live && (
                  <span
                    className="inline-block h-1.5 w-1.5 flex-none rounded-full"
                    style={{ background: "var(--pink)" }}
                  />
                )}
                <div
                  className="truncate"
                  style={{
                    fontFamily: NUNITO,
                    fontWeight: 700,
                    fontSize: 13,
                    color: row.dim ? "var(--text3)" : "var(--text)",
                  }}
                >
                  {row.line}
                </div>
              </div>
              <MicroLabel style={{ fontSize: 9.5, letterSpacing: "0.1em" }}>
                {row.label}
              </MicroLabel>
            </div>
            {row.control && (
              <button
                type="button"
                onClick={row.control.onClick}
                disabled={busy}
                aria-label={row.control.aria}
                className="grid h-8 w-8 flex-none cursor-pointer place-items-center rounded-full border transition hover:opacity-80 disabled:opacity-40"
                style={{
                  borderColor: "var(--line)",
                  background: "var(--card2)",
                  color: "var(--text)",
                }}
              >
                <MsIcon name={row.control.icon} size={18} />
              </button>
            )}
          </div>
        ))}
      </div>
    </DesignTile>
  );
}
