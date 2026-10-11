"use client";

/**
 * TvCastPanel: the "Cast to..." sheet.
 *
 * Flow: pick a TV -> pick something (Live TV, Movies, Shows, Anime, Music,
 * or search) -> casting, with Stop casting. Shows open to their episodes and
 * albums to their songs.
 *
 * Books are the one tab that is not cast: a book opens for reading on the
 * screen in hand (kiosk or phone), in Jellyfin's own reader, through the
 * Jellyfin page's ?item= deep link. "Read a book here" opens the Books tab
 * without picking a TV first.
 *
 * The TVs are the ones linked ONCE in Home Assistant (its Apple TV and Roku
 * integrations). Nobody types a code here: linking happens in Home Assistant's
 * own setup screen, one time per TV, and the sheet shows a plain hint naming
 * any Apple TV that is on the network but not linked yet.
 *
 * Everything goes through the pairing-gated /api/pai proxy to the backend's
 * /tvcast/* routes. The browser never sees a stream URL, a cast id, a TV's IP,
 * a file path, the Home Assistant credential or any other secret: the backend
 * returns names, kinds and states only.
 *
 * Used in two places: over the Jellyfin frame (TV control bar) and as a
 * full-screen sheet from the Media hub. Works on the wall kiosk (touch only,
 * no keyboard: tabs, A to Z, "Show more") and on a phone (search box too).
 *
 * Wall-screen rules: 56px+ touch targets, plain words, and nothing that
 * animates at rest (busy states are static text, not spinners).
 *
 * puff puff pass
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import MsIcon from "../../_components/MsIcon";

type Device = {
  id: string;
  name: string;
  kind: "appletv" | "roku";
  model: string;
  available: boolean;
  why: string;
  casting: { channel_name: string; state: string } | null;
};
type Channel = { id: string; name: string; number: string; genre?: string };
type Group = { name: string; channels: Channel[]; apple_tv_only?: boolean };
type Item = {
  id: string;
  name: string;
  kind: "movie" | "episode" | "show" | "album" | "song" | "book";
  sub: string;
  playable: boolean;
  apple_tv_only: boolean;
};
type Tab = "live" | "movies" | "shows" | "anime" | "music" | "books";

/** `device: null` = reading on this screen (Books only, no TV picked). */
type Step =
  | { s: "devices" }
  | { s: "browse"; device: Device | null }
  | { s: "children"; device: Device; parent: Item }
  | { s: "casting"; device: Device; title: string };

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "live", label: "Live TV", icon: "live_tv" },
  { id: "movies", label: "Movies", icon: "movie" },
  { id: "shows", label: "Shows", icon: "tv" },
  { id: "anime", label: "Anime", icon: "animation" },
  { id: "music", label: "Music", icon: "library_music" },
  { id: "books", label: "Books", icon: "menu_book" },
];
const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("");

async function call(path: string, body?: object) {
  const res = await fetch(`/api/pai/tvcast/${path}`, {
    method: body ? "POST" : "GET",
    cache: "no-store",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    /* non-JSON error page */
  }
  if (!res.ok || data.success === false) {
    throw new Error(
      typeof data.message === "string" ? data.message : "That did not work. Try again.",
    );
  }
  return data;
}

const BTN =
  "flex items-center gap-3 rounded-2xl border px-5 text-left transition-opacity hover:opacity-90 active:opacity-75 disabled:opacity-50";
const BTN_STYLE = {
  minHeight: 64,
  borderColor: "var(--line)",
  background: "rgba(255,255,255,0.06)",
  color: "#f1f5f9",
  font: "700 18px var(--font-nunito), sans-serif",
} as const;
const PRIMARY_STYLE = {
  ...BTN_STYLE,
  color: "#fff",
  background: "linear-gradient(140deg, var(--pink), var(--magenta))",
} as const;
const SMALL_STYLE = { ...BTN_STYLE, minHeight: 56, font: "700 16px var(--font-nunito), sans-serif" } as const;
const NOTE_STYLE = { font: "600 17px var(--font-nunito), sans-serif" } as const;
const SUB_STYLE = { font: "600 14px var(--font-nunito), sans-serif", opacity: 0.75 } as const;
const GRID = { gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))" } as const;

const itemIcon = (k: Item["kind"]) =>
  k === "show"
    ? "tv"
    : k === "album"
      ? "album"
      : k === "song"
        ? "music_note"
        : k === "book"
          ? "menu_book"
          : "movie";

export default function TvCastPanel({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [step, setStep] = useState<Step>({ s: "devices" });
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [hint, setHint] = useState("");
  const [groups, setGroups] = useState<Group[] | null>(null);
  const [tab, setTab] = useState<Tab>("live");
  const [query, setQuery] = useState("");
  const [applied, setApplied] = useState(""); // the search actually sent
  const [letter, setLetter] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [children, setChildren] = useState<Item[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const reqSeq = useRef(0);

  const loadDevices = useCallback(async (refresh = false) => {
    setBusy("Looking for TVs...");
    setError(null);
    try {
      const d = await call(`devices${refresh ? "?refresh=1" : ""}`);
      setDevices((d.devices as Device[]) ?? []);
      setHint(typeof d.hint === "string" ? d.hint : "");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }, []);

  useEffect(() => {
    void loadDevices();
  }, [loadDevices]);

  const loadChannels = useCallback(async () => {
    if (groups) return;
    setBusy("Getting channels...");
    setError(null);
    try {
      const d = await call("channels");
      setGroups((d.groups as Group[]) ?? []);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  }, [groups]);

  /** One page of Movies / Shows / Music. `more` appends the next page. */
  const loadLibrary = useCallback(
    async (t: Tab, q: string, l: string, more: boolean, from: number) => {
      if (t === "live") return;
      const seq = ++reqSeq.current;
      setBusy(more ? "Getting more..." : q ? `Searching for “${q}”...` : "Getting the list...");
      setError(null);
      try {
        const params = new URLSearchParams({ kind: t, start: String(more ? from : 0) });
        if (q) params.set("q", q);
        else if (l) params.set("letter", l);
        const d = await call(`library?${params.toString()}`);
        if (seq !== reqSeq.current) return; // a newer tab/search won
        const got = (d.items as Item[]) ?? [];
        setItems((cur) => (more ? [...cur, ...got] : got));
        setTotal(typeof d.total === "number" ? d.total : got.length);
        if (!more) listRef.current?.scrollTo({ top: 0 });
      } catch (e) {
        if (seq === reqSeq.current) setError((e as Error).message);
      } finally {
        if (seq === reqSeq.current) setBusy(null);
      }
    },
    [],
  );

  const pickTab = (t: Tab) => {
    setTab(t);
    setLetter("");
    setError(null);
    setItems([]);
    setTotal(0);
    if (t === "live") void loadChannels();
    else void loadLibrary(t, applied, "", false, 0);
  };

  const openBrowse = (device: Device | null) => {
    setStep({ s: "browse", device });
    setError(null);
    // No TV = reading here: Books only. Music only casts to an Apple TV; a
    // Roku starts on Live TV instead.
    const t = !device ? "books" : tab === "music" && device.kind !== "appletv" ? "live" : tab;
    if (t !== tab) {
      setTab(t);
      setItems([]);
      setTotal(0);
    }
    if (t === "live") void loadChannels();
    else void loadLibrary(t, applied, letter, false, 0);
  };

  const runSearch = () => {
    const q = query.trim();
    setApplied(q);
    setLetter("");
    if (tab !== "live") void loadLibrary(tab, q, "", false, 0);
  };
  const clearSearch = () => {
    setQuery("");
    setApplied("");
    if (tab !== "live") void loadLibrary(tab, "", letter, false, 0);
  };
  const pickLetter = (l: string) => {
    setLetter(l);
    setQuery("");
    setApplied("");
    void loadLibrary(tab, "", l, false, 0);
  };

  const openChildren = async (device: Device, parent: Item) => {
    setStep({ s: "children", device, parent });
    setChildren(null);
    setBusy(parent.kind === "show" ? "Getting episodes..." : "Getting songs...");
    setError(null);
    try {
      const d = await call(`library/children?id=${encodeURIComponent(parent.id)}`);
      setChildren((d.items as Item[]) ?? []);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const cast = async (device: Device, what: { channel?: string; item?: string }, title: string) => {
    setBusy(`Sending ${title} to ${device.name}...`);
    setError(null);
    try {
      await call("cast", { device: device.id, ...what });
      setStep({ s: "casting", device, title });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const stop = async (device: Device) => {
    setBusy("Stopping...");
    setError(null);
    try {
      await call("stop", { device: device.id });
      setStep({ s: "devices" });
      await loadDevices();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  /** A book opens on this screen in Jellyfin's reader (its page has Read). */
  const readHere = (it: Item) => {
    onClose();
    router.push(`/dashboard/media/jellyfin?item=${encodeURIComponent(it.id)}`);
  };

  const kindLabel = (d: Device) => (d.kind === "roku" ? "Roku" : "Apple TV");
  const device = step.s === "devices" ? null : step.device;
  const tabs = !device
    ? TABS.filter((t) => t.id === "books")
    : TABS.filter((t) => !(t.id === "music" && device.kind !== "appletv"));
  const needle = applied.toLowerCase();

  const itemButton = (d: Device | null, it: Item) => (
    <button
      key={it.id}
      type="button"
      className={BTN}
      style={BTN_STYLE}
      disabled={!!busy || (!d && it.kind !== "book")}
      onClick={() =>
        it.kind === "book"
          ? readHere(it)
          : !d
            ? undefined
            : it.playable
              ? void cast(d, { item: it.id }, it.name)
              : void openChildren(d, it)
      }
    >
      <MsIcon name={itemIcon(it.kind)} size={26} />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate">{it.name}</span>
        {it.sub && <span className="truncate" style={SUB_STYLE}>{it.sub}</span>}
      </span>
      <MsIcon
        name={it.kind === "book" ? "auto_stories" : it.playable ? "play_arrow" : "chevron_right"}
        size={26}
      />
    </button>
  );

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Cast to a TV"
      className="absolute inset-0 z-30 flex flex-col rounded-xl border p-4 sm:p-5"
      style={{ background: "rgba(8,10,18,0.96)", borderColor: "var(--line)", color: "#f1f5f9" }}
    >
      <div className="mb-3 flex flex-none flex-wrap items-center gap-3">
        {(step.s === "browse" || step.s === "children") && (
          <button
            type="button"
            className={BTN}
            style={BTN_STYLE}
            onClick={() => {
              setError(null);
              setStep(step.s === "children" ? { s: "browse", device: step.device } : { s: "devices" });
            }}
          >
            <MsIcon name="arrow_back" size={24} />
            {step.s === "children" ? "Back" : "TVs"}
          </button>
        )}
        <h2 className="min-w-0 flex-1" style={{ font: "800 24px var(--font-nunito), sans-serif" }}>
          {step.s === "devices" && "Cast to..."}
          {step.s === "browse" && (step.device ? `Pick something for ${step.device.name}` : "Read a book here")}
          {step.s === "children" && step.parent.name}
          {step.s === "casting" && "Casting"}
        </h2>
        <button type="button" aria-label="Close" className={BTN} style={BTN_STYLE} onClick={onClose}>
          <MsIcon name="close" size={24} />
          Close
        </button>
      </div>

      {step.s === "browse" && (
        <div className="mb-3 flex flex-none flex-col gap-3">
          <div role="tablist" aria-label="What to cast" className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 130px), 1fr))" }}>
            {tabs.map((t) => (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={tab === t.id}
                className={`${BTN} justify-center px-2`}
                style={tab === t.id ? PRIMARY_STYLE : SMALL_STYLE}
                onClick={() => pickTab(t.id)}
              >
                <MsIcon name={t.icon} size={24} />
                <span className="truncate">{t.label}</span>
              </button>
            ))}
          </div>
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              runSearch();
            }}
          >
            <input
              type="search"
              enterKeyHint="search"
              value={query}
              maxLength={80}
              onChange={(e) => {
                setQuery(e.target.value);
                if (tab === "live") setApplied(e.target.value.trim());
              }}
              placeholder={tab === "live" ? "Find a channel" : `Search ${TABS.find((t) => t.id === tab)?.label}`}
              aria-label="Search"
              className="min-w-0 flex-1 rounded-2xl border px-4"
              style={{
                minHeight: 56,
                borderColor: "var(--line)",
                background: "rgba(255,255,255,0.08)",
                color: "#f1f5f9",
                font: "600 18px var(--font-nunito), sans-serif",
              }}
            />
            {tab !== "live" && (
              <button type="submit" className={BTN} style={SMALL_STYLE} disabled={!!busy}>
                <MsIcon name="search" size={24} />
                <span className="hidden sm:inline">Search</span>
              </button>
            )}
            {(query || applied) && (
              <button type="button" aria-label="Clear search" className={BTN} style={SMALL_STYLE} onClick={clearSearch}>
                <MsIcon name="backspace" size={24} />
              </button>
            )}
          </form>
          {tab !== "live" && !applied && (
            <div className="flex gap-1.5 overflow-x-auto pb-1" aria-label="Jump to a letter">
              {["", ...LETTERS].map((l) => (
                <button
                  key={l || "all"}
                  type="button"
                  className="flex-none rounded-xl border"
                  style={{
                    minWidth: l ? 48 : 64,
                    minHeight: 48,
                    borderColor: "var(--line)",
                    background: letter === l ? "var(--pink)" : "rgba(255,255,255,0.06)",
                    color: "#f1f5f9",
                    font: "800 17px var(--font-nunito), sans-serif",
                  }}
                  disabled={!!busy}
                  onClick={() => pickLetter(l)}
                >
                  {l || "All"}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {(busy || error) && (
        <p
          role="status"
          className="mb-3 flex-none rounded-xl px-4 py-3"
          style={{ ...NOTE_STYLE, background: error ? "rgba(255,45,149,0.12)" : "rgba(255,255,255,0.06)" }}
        >
          {busy ?? error}
        </p>
      )}

      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto">
        {step.s === "devices" && (
          <div className="flex flex-col gap-3">
            {devices?.map((d) => (
              <div key={d.id} className="flex items-center gap-3">
                <button
                  type="button"
                  className={`${BTN} flex-1`}
                  style={BTN_STYLE}
                  disabled={!!busy || !d.available}
                  onClick={() => openBrowse(d)}
                >
                  <MsIcon name={d.kind === "roku" ? "tv" : "airplay"} size={28} />
                  <span className="flex flex-col">
                    <span>{d.name}</span>
                    <span style={SUB_STYLE}>
                      {kindLabel(d)}
                      {d.casting ? `, casting ${d.casting.channel_name}` : !d.available ? `. ${d.why}` : ""}
                    </span>
                  </span>
                </button>
                {d.casting && (
                  <button type="button" className={BTN} style={BTN_STYLE} disabled={!!busy} onClick={() => void stop(d)}>
                    <MsIcon name="stop" size={24} fill />
                    Stop
                  </button>
                )}
              </div>
            ))}
            {hint && (
              <p className="rounded-xl px-4 py-3" style={{ ...NOTE_STYLE, background: "rgba(255,255,255,0.06)" }}>
                {hint}
              </p>
            )}
            <div className="flex flex-wrap gap-3">
              <button type="button" className={BTN} style={BTN_STYLE} disabled={!!busy} onClick={() => void loadDevices(true)}>
                <MsIcon name="refresh" size={24} />
                Look again
              </button>
              <button type="button" className={BTN} style={BTN_STYLE} onClick={() => openBrowse(null)}>
                <MsIcon name="menu_book" size={24} />
                Read a book here
              </button>
            </div>
          </div>
        )}

        {step.s === "browse" && step.device && tab === "live" && groups && (
          <div className="flex flex-col gap-5">
            {groups
              .filter((g) => !(g.apple_tv_only && step.device!.kind !== "appletv"))
              .map((g) => {
                const list = needle ? g.channels.filter((c) => c.name.toLowerCase().includes(needle)) : g.channels;
                if (!list.length) return null;
                return (
                  <section key={g.name}>
                    <h3 className="mb-2" style={{ font: "800 18px var(--font-nunito), sans-serif", opacity: 0.85 }}>
                      {g.name}
                    </h3>
                    <div className="grid gap-2" style={GRID}>
                      {list.map((c) => (
                        <button
                          key={c.id}
                          type="button"
                          className={BTN}
                          style={BTN_STYLE}
                          disabled={!!busy}
                          onClick={() => void cast(step.device!, { channel: c.id }, c.name)}
                        >
                          <span style={{ minWidth: 44, opacity: 0.7 }}>{c.number}</span>
                          <span className="min-w-0 truncate">{c.name}</span>
                        </button>
                      ))}
                    </div>
                  </section>
                );
              })}
          </div>
        )}

        {step.s === "browse" && tab !== "live" && (
          <div className="flex flex-col gap-3">
            {tab === "books" && (
              <p style={SUB_STYLE}>Books open on this screen, not on a TV. On the next page, tap Read.</p>
            )}
            <div className="grid gap-2" style={GRID}>
              {items
                .filter((it) => !(it.apple_tv_only && step.device?.kind !== "appletv"))
                .map((it) => itemButton(step.device, it))}
            </div>
            {!busy && !error && items.length === 0 && (
              <p style={NOTE_STYLE}>{applied ? `Nothing found for “${applied}”.` : "Nothing here."}</p>
            )}
            {items.length < total && (
              <button
                type="button"
                className={`${BTN} self-center`}
                style={BTN_STYLE}
                disabled={!!busy}
                onClick={() => void loadLibrary(tab, applied, letter, true, items.length)}
              >
                <MsIcon name="expand_more" size={24} />
                Show more ({total - items.length} left)
              </button>
            )}
          </div>
        )}

        {step.s === "children" && children && (
          <div className="grid gap-2" style={GRID}>
            {children.map((it) => itemButton(step.device, it))}
            {children.length === 0 && <p style={NOTE_STYLE}>Nothing to play in here.</p>}
          </div>
        )}

        {step.s === "casting" && (
          <div className="flex flex-col items-center gap-5 pt-6">
            <MsIcon name="cast_connected" size={56} />
            <p className="text-center" style={{ font: "700 22px var(--font-nunito), sans-serif" }}>
              {step.title} is on {step.device.name}.
            </p>
            <div className="flex flex-wrap justify-center gap-3">
              <button type="button" className={BTN} style={PRIMARY_STYLE} disabled={!!busy} onClick={() => void stop(step.device)}>
                <MsIcon name="stop" size={24} fill />
                Stop casting
              </button>
              <button
                type="button"
                className={BTN}
                style={BTN_STYLE}
                onClick={() => setStep({ s: "browse", device: step.device })}
              >
                <MsIcon name="swap_horiz" size={24} />
                Pick something else
              </button>
              <button type="button" className={BTN} style={BTN_STYLE} onClick={onClose}>
                Done
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
