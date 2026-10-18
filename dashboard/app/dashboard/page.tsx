"use client";

/**
 * /dashboard — the home screen.
 *
 * Hero: status chips + greeting + "Hey Puff" sub-line + 12-bar waveform
 * + mic orb → /dashboard/talk. The clock lives in the topbar; Sync lives in
 * the arrange tray.
 *
 * Board: react-grid-layout TileGrid, storage key
 * "dash-board-v2" so the current default layout wins over stale saves. Every
 * tile wears the design skin (radius-28 glass, MsIcon + Fredoka headers,
 * JBMono micro-stats); new tiles: Live transcript, standalone Weather,
 * Jetson Orin telemetry, Now Playing, split Reminders, Library stats.
 *
 * Arrange mode: toggled by the topbar's "puffpai:toggle-arrange" window
 * event (this page answers with "puffpai:arrange-state"). While arranging,
 * tiles get a magenta remove bubble and the dashed pink tray below the grid
 * offers hidden tiles back as "+ Tile" pills, plus Sync / Reset / Done.
 */

import { useEffect, useCallback, useMemo, useState, useRef } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import MsIcon from "../_components/MsIcon";
import OwnerAppChips from "./_components/OwnerAppChips";
import MeetPersonas from "./_components/MeetPersonas";
import ProveStrip from "@/app/_components/Widgets/ProveStrip";

import { useAppDispatch, useAppSelector } from "@/app/_stores/hooks";
import { fetchSystemStatus } from "@/app/_stores/systemSlice";
import { fetchChatHistory } from "@/app/_stores/chatSlice";
import { useAppData } from "@/app/_lib/hooks/useAppData";
import { ROLE_PLACEHOLDERS } from "./apps/_kit/tokens";
import { useOwnerName } from "@/app/_lib/hooks/useHousehold";
import {
  getHADevices,
  type HADevice,
} from "@/app/_lib/services/haService";
import {
  addDashboardCamera,
  removeDashboardCamera,
  getConsentRequests,
  onConsentChanged,
  type ConsentRequest,
} from "@/app/_lib/services/consentService";
import ConsentRequestsBanner, {
  ConsentRequestRow,
} from "@/app/_components/Consent/ConsentRequests";
import SuggestionsTile from "@/app/dashboard/_components/SuggestionsTile";
import TileGrid, {
  clearTileLayout,
  type TileDef,
} from "@/app/_components/TileGrid";
import {
  useWidgets,
  renderWidget,
} from "@/app/_components/Widgets/WidgetBoard";
import DesignTile, {
  FREDOKA,
  NUNITO,
  JBM,
  HATCH,
  MicroStat,
} from "@/app/_components/Widgets/DesignTile";
import TranscriptTile from "@/app/_components/Widgets/TranscriptTile";
import WeatherTile from "@/app/_components/Widgets/WeatherTile";
import JetsonTile from "@/app/_components/Widgets/JetsonTile";
import NowPlayingTile from "@/app/_components/Widgets/NowPlayingTile";
import SmartHomeTile from "@/app/_components/Widgets/SmartHomeTile";
import RemindersTile, {
  type ReminderRow,
} from "@/app/_components/Widgets/RemindersTile";
import LibraryTile from "@/app/_components/Widgets/LibraryTile";
import { useHaConfirmation } from "@/app/_lib/hooks/useHaConfirmation";
import HaConfirmDialog from "@/app/_components/Confirm/HaConfirmDialog";
import { expandEvents } from "@/app/_lib/recurrence";
import { conversationTitle } from "@/app/_lib/chatTitle";
import { playAlarmSound, showBrowserNotification } from "@/app/_lib/audioUnlock";
import { shouldChime } from "@/app/_lib/alarmTime";
import { cameraPollMs, onInputAfterQuiet } from "@/app/_lib/kioskActivity";

const REFRESH_INTERVAL_MS = 30_000;
const HIDDEN_KEY = "puffpai_dash_hidden_v1";
const TRANSCRIPT_KEY = "puffpai_show_transcript";

/** Tray labels for the built-in tiles. */
const TILE_NAMES: Record<string, string> = {
  transcript: "Live transcript",
  weather: "Weather",
  jetson: "This machine",
  "smart-home": "Smart home",
  media: "Now playing",
  cameras: "Cameras",
  schedule: "Schedule",
  reminders: "Reminders",
  library: "Library",
  "recent-chats": "Recent chats",
  apps: "Apps",
  meals: "Meals",
  workout: "Workout",
};

/* Design hero waveform: 12 bars, exact heights + colors (design 607-619). */
const WAVE_HEIGHTS = [60, 90, 45, 100, 70, 35, 85, 55, 95, 40, 75, 30];
const WAVE_COLORS = [
  "var(--pink)",
  "var(--pink)",
  "var(--magenta)",
  "var(--lav)",
  "var(--sky)",
  "var(--sky)",
  "var(--pink)",
  "var(--magenta)",
  "var(--lav)",
  "var(--sky)",
  "var(--pink)",
  "var(--magenta)",
];

export default function DashboardPage() {
  const dispatch = useAppDispatch();
  const router = useRouter();
  const {
    status: systemStatus,
    backendConnected,
    loading,
    error,
  } = useAppSelector((state) => state.system);
  const { conversations } = useAppSelector((state) => state.chat);
  const { speaking: puffSpeaking, isListening: puffListening } = useAppSelector(
    (state) => state.avatar
  );

  /* ── Kiosk fit: this screen lives on a wall display, so the whole board has
        to land inside the viewport below the topbar with nothing to scroll.
        The layout's scroll container also holds the footer, so the exact
        height is "what's left in the scroller after our siblings" rather than
        a plain 100%. Below lg we hand the page back to normal scrolling. ── */
  const sectionRef = useRef<HTMLElement | null>(null);
  const [kioskHeight, setKioskHeight] = useState<number | null>(null);
  useEffect(() => {
    const el = sectionRef.current;
    const scroller = el?.parentElement;
    if (!el || !scroller) return;
    const measure = () => {
      if (window.innerWidth < 1024) {
        setKioskHeight(null);
        return;
      }
      let siblings = 0;
      for (const child of Array.from(scroller.children)) {
        if (child !== el) siblings += (child as HTMLElement).offsetHeight;
      }
      const h = Math.max(360, scroller.clientHeight - siblings);
      setKioskHeight((prev) => (prev === h ? prev : h));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(scroller);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  /* ── Arrange mode (topbar toggles it via a window event) ── */
  const [arranging, setArranging] = useState(false);
  const [gridEpoch, setGridEpoch] = useState(0);
  const resetLayout = useCallback(() => {
    clearTileLayout("dash-board-v2");
    setGridEpoch((n) => n + 1);
  }, []);

  useEffect(() => {
    const onToggle = () => setArranging((a) => !a);
    window.addEventListener("puffpai:toggle-arrange", onToggle);
    return () => window.removeEventListener("puffpai:toggle-arrange", onToggle);
  }, []);
  useEffect(() => {
    window.dispatchEvent(
      new CustomEvent("puffpai:arrange-state", { detail: { arranging } })
    );
  }, [arranging]);

  /* ── Tile visibility (arrange-tray add/remove; transcript follows the
        Settings "Show live transcript" localStorage flag) ── */
  const [hiddenIds, setHiddenIds] = useState<string[]>([]);
  const [showTranscript, setShowTranscript] = useState(true);
  useEffect(() => {
    const read = () => {
      try {
        setShowTranscript(localStorage.getItem(TRANSCRIPT_KEY) !== "0");
        const raw = localStorage.getItem(HIDDEN_KEY);
        const parsed = raw ? JSON.parse(raw) : [];
        setHiddenIds(Array.isArray(parsed) ? parsed : []);
      } catch {
        /* private mode */
      }
    };
    read();
    window.addEventListener("storage", read);
    window.addEventListener("puffpai:show-transcript", read as EventListener);
    // Settings page fires this on every toggle write (see settings/page.tsx).
    window.addEventListener("puffpai-setting-changed", read as EventListener);
    return () => {
      window.removeEventListener("storage", read);
      window.removeEventListener(
        "puffpai:show-transcript",
        read as EventListener
      );
      window.removeEventListener(
        "puffpai-setting-changed",
        read as EventListener
      );
    };
  }, []);

  const hideTile = useCallback((id: string) => {
    if (id === "transcript") {
      try {
        localStorage.setItem(TRANSCRIPT_KEY, "0");
      } catch {}
      setShowTranscript(false);
      return;
    }
    setHiddenIds((prev) => {
      const next = Array.from(new Set([...prev, id]));
      try {
        localStorage.setItem(HIDDEN_KEY, JSON.stringify(next));
      } catch {}
      return next;
    });
  }, []);

  const unhideTile = useCallback((id: string) => {
    if (id === "transcript") {
      try {
        localStorage.setItem(TRANSCRIPT_KEY, "1");
      } catch {}
      setShowTranscript(true);
      return;
    }
    setHiddenIds((prev) => {
      const next = prev.filter((x) => x !== id);
      try {
        localStorage.setItem(HIDDEN_KEY, JSON.stringify(next));
      } catch {}
      return next;
    });
  }, []);

  /* ── Owner name, from the one identity source. With no name yet the
     greeting goes without one; the transcript's initials keep Operator. ── */
  const ownerName = useOwnerName("");
  const displayName = ownerName || "Operator";

  // Registry widgets (news feeds, …) join the same grid as the built-ins.
  const widgets = useWidgets();

  // Live data — auto-polls every 2s for chat-driven updates
  const { data: calendarData } = useAppData<any>("calendar");
  const { data: alarmData } = useAppData<any>("alarmflow");

  // Countdown ticks live in LiveRemindersTile (below), not here: a 1s tick
  // on this component re-rendered the whole dashboard. This minute tick only
  // keeps the greeting and "5m ago" chat stamps honest. It is aligned to the
  // next whole minute (then every 60s) so the text changes on the minute
  // instead of up to 60s late.
  const [, setMinuteTick] = useState(0);
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;
    const bump = () => setMinuteTick((t) => t + 1);
    const align = setTimeout(() => {
      bump();
      id = setInterval(bump, 60_000);
    }, 60_000 - (Date.now() % 60_000));
    return () => {
      clearTimeout(align);
      if (id) clearInterval(id);
    };
  }, []);

  // Today's events (from real calendar) — expanding any recurring events
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const endOfToday = new Date();
  endOfToday.setHours(23, 59, 59, 999);
  const seenEventKeys = new Set<string>();
  const todayEvents = expandEvents(
    calendarData?.events || [],
    startOfToday,
    endOfToday
  )
    .filter((e) => {
      const k = `${e.title}|${e.start}`;
      if (seenEventKeys.has(k)) return false;
      seenEventKeys.add(k);
      return true;
    })
    .slice(0, 4);

  // Pending tasks (real)
  const pendingTasks = (calendarData?.tasks || [])
    .filter((t: any) => t.status !== "completed")
    .slice(0, 4);

  // Chime when the SERVER says an item fired — display only.
  //
  // Computing due times here would make the chime exist only while this
  // page is the mounted screen: walk the kiosk into another app and a due
  // alarm would ring nowhere at all. The server-side scheduler owns firing,
  // announces on the appliance speaker, and marks the
  // item `status: "fired"`; every client already polls
  // /apps/alarmflow/updated, so this effect just reacts to that. Still once
  // per item, and still nothing for an item that fired before we mounted:
  // `fired_ts` tells us how old the ring is, so opening a screen an hour
  // later does not retroactively chime.
  const notifiedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const items = alarmData?.items || [];
    if (!items.length) return;
    const now = Date.now();
    for (const item of items) {
      if (!shouldChime(item)) continue;
      if (notifiedRef.current.has(item.id)) continue;
      notifiedRef.current.add(item.id);
      if (item.fired_ts && now - item.fired_ts > 300_000) continue;
      playAlarmSound(item.sound);
      showBrowserNotification(
        item.mode === "timer"
          ? `Timer done: ${item.desc || "Timer"}`
          : `Alarm: ${item.desc || "Alarm"}`
      );
    }
  }, [alarmData]);

  const refresh = useCallback(() => {
    dispatch(fetchSystemStatus());
    dispatch(fetchChatHistory());
  }, [dispatch]);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [refresh]);

  const inferenceModel = systemStatus?.inference?.model ?? "---";
  const inferenceBackend = systemStatus?.inference?.backend ?? "---";
  const inferenceStatus = systemStatus?.inference?.status ?? "unknown";
  const knowledgeCount = systemStatus?.knowledge?.library_count ?? 0;


  // Recent conversations
  const recentChats =
    conversations.length > 0
      ? conversations.slice(0, 3).map((c, i) => ({
          title: conversationTitle(c),
          preview:
            c.messages.length > 0
              ? c.messages[c.messages.length - 1].content.slice(0, 80) +
                (c.messages[c.messages.length - 1].content.length > 80
                  ? "..."
                  : "")
              : "No messages",
          date: formatRelativeTime(c.created_at),
          color: ["var(--pink)", "var(--sky)", "var(--mint)"][i % 3],
          href: `/dashboard/chat?id=${c.id}`,
        }))
      : [
          {
            title: "Getting started",
            preview: "Welcome to PuffPAI. Your private AI is ready.",
            date: "",
            color: "var(--pink)",
            href: "/dashboard/chat",
          },
        ];

  const voiceActive = puffSpeaking || puffListening;
  const statusLabel = !backendConnected
    ? "OFFLINE"
    : puffListening
      ? "LISTENING"
      : "ONLINE";
  const statusColor = backendConnected ? "var(--mint)" : "var(--puff-red)";

  /* ── Tile board (spec default layout, 12-col, rowHeight 72) ── */
  const allTiles: TileDef[] = [
    {
      id: "transcript",
      lg: { x: 0, y: 0, w: 4, h: 4, minW: 3, minH: 2 },
      node: (
        <TranscriptTile conversations={conversations} profileName={displayName} />
      ),
    },
    {
      id: "weather",
      lg: { x: 4, y: 0, w: 3, h: 4, minW: 2, minH: 2 },
      node: <WeatherTile />,
    },
    {
      id: "jetson",
      lg: { x: 7, y: 0, w: 3, h: 4, minW: 2, minH: 2 },
      node: <JetsonTile />,
    },
    {
      id: "smart-home",
      lg: { x: 0, y: 4, w: 3, h: 3, minW: 2, minH: 2 },
      node: <SmartHomeTile />,
    },
    {
      id: "media",
      lg: { x: 10, y: 2, w: 2, h: 2, minW: 2, minH: 2 },
      node: <NowPlayingTile />,
    },
    {
      id: "cameras",
      // Widest tile in band B: a security feed at w3 is too small to read
      // at kiosk distance.
      lg: { x: 5, y: 4, w: 4, h: 3, minW: 2, minH: 2 },
      node: (
        <DesignTile
          icon="videocam"
          iconColor="var(--lav)"
          title="Cameras"
          img="/design-cloud-4.jpg"
          stat={<MicroStat color="var(--pink)">● LIVE</MicroStat>}
          bodyClassName="min-h-0 flex-1"
        >
          <CamerasPanel />
        </DesignTile>
      ),
    },
    {
      id: "schedule",
      lg: { x: 3, y: 4, w: 2, h: 3, minW: 2, minH: 2 },
      node: (
        <DesignTile
          icon="calendar_month"
          iconColor="var(--sky)"
          title="Schedule"
          img="/design-cloud-2.jpg"
          href="/dashboard/apps/calendar"
          stat={
            <MicroStat>
              {todayEvents.length} TODAY
            </MicroStat>
          }
        >
          {todayEvents.length === 0 ? (
            <EmptyNote>No events scheduled for today.</EmptyNote>
          ) : (
            <div className="flex flex-col gap-2.5">
              {todayEvents.map((event: any, idx: number) => {
                const dots = [
                  "var(--sky)",
                  "var(--lav)",
                  "var(--pink)",
                  "var(--mint)",
                ];
                const time = event.start
                  ? new Date(event.start).toLocaleTimeString([], {
                      hour: "numeric",
                      minute: "2-digit",
                    })
                  : "All day";
                const rowKey =
                  event._occurrenceKey || `${event.id}-${event.start}-${idx}`;
                return (
                  <div key={rowKey} className="flex items-center gap-3">
                    <span
                      className="h-2 w-2 flex-none rounded-full"
                      style={{ background: dots[idx % dots.length] }}
                    />
                    <span
                      className="w-[62px] flex-none"
                      style={{
                        fontFamily: JBM,
                        fontWeight: 600,
                        fontSize: 12,
                        color: "var(--text3)",
                      }}
                    >
                      {time}
                    </span>
                    <span
                      className="truncate"
                      style={{
                        fontFamily: NUNITO,
                        fontWeight: 600,
                        fontSize: 15,
                        color: "var(--text)",
                      }}
                    >
                      {event.title || "Untitled"}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </DesignTile>
      ),
    },
    {
      id: "reminders",
      lg: { x: 9, y: 4, w: 3, h: 3, minW: 2, minH: 2 },
      node: (
        <LiveRemindersTile
          alarmItems={alarmData?.items}
          pendingTasks={pendingTasks}
        />
      ),
    },
    {
      id: "library",
      lg: { x: 10, y: 0, w: 2, h: 2, minW: 2, minH: 2 },
      node: <LibraryTile packs={knowledgeCount} />,
    },
    {
      id: "recent-chats",
      lg: { x: 0, y: 7, w: 4, h: 3, minW: 3, minH: 2 },
      node: (
        <DesignTile
          icon="chat_bubble"
          iconColor="var(--pink)"
          title="Recent chats"
          img="/design-cloud-5.jpg"
          stat={
            <Link href="/dashboard/chat">
              <MicroStat>VIEW ALL →</MicroStat>
            </Link>
          }
        >
          <div className="grid h-full min-h-0 grid-cols-3 gap-2.5">
            {recentChats.slice(0, 3).map((chat, i) => (
              <Link
                key={i}
                href={chat.href}
                className="flex min-w-0 flex-col justify-between rounded-2xl border p-3 transition hover:opacity-80"
                style={{ borderColor: "var(--line)", background: "var(--card2)" }}
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span
                      className="h-1.5 w-1.5 flex-none rounded-full"
                      style={{ background: chat.color }}
                    />
                    <span
                      className="truncate"
                      style={{
                        fontFamily: NUNITO,
                        fontWeight: 700,
                        fontSize: 14,
                        color: "var(--text)",
                      }}
                    >
                      {chat.title}
                    </span>
                  </div>
                  <p
                    className="mt-1 line-clamp-2"
                    style={{
                      fontFamily: NUNITO,
                      fontWeight: 500,
                      fontSize: 12,
                      color: "var(--text3)",
                    }}
                  >
                    {chat.preview}
                  </p>
                </div>
                {chat.date && (
                  <span
                    style={{
                      fontFamily: JBM,
                      fontWeight: 600,
                      fontSize: 10,
                      color: "var(--text3)",
                    }}
                  >
                    {chat.date}
                  </span>
                )}
              </Link>
            ))}
          </div>
        </DesignTile>
      ),
    },
    {
      id: "apps",
      lg: { x: 4, y: 7, w: 4, h: 3, minW: 3, minH: 2 },
      node: (
        <DesignTile
          icon="grid_view"
          iconColor="var(--lav)"
          title="Apps"
          img="/design-cloud-2.jpg"
        >
          <div className="grid h-full min-h-0 grid-cols-[repeat(auto-fit,minmax(64px,1fr))] content-start gap-2">
            <AppChip icon="alarm" label="AlarmFlow" color="var(--lemon)" href="/dashboard/apps/alarmflow" />
            <AppChip icon="calendar_month" label="Calendar" color="var(--sky)" href="/dashboard/apps/calendar" />
            <AppChip icon="restaurant" label="FreshTable" color="var(--mint)" href="/dashboard/apps/freshtable" />
            <AppChip icon="fitness_center" label="FitFam" color="var(--pink)" href="/dashboard/apps/fitfam" />
            <AppChip icon="quiz" label="Trivia" color="var(--lav)" href="/dashboard/apps/trivia" />
            <AppChip icon="music_note" label="Media" color="var(--magenta)" href="/dashboard/media" />
            {/* The six above are hardcoded because the six pages are. An app the
                owner scaffolded has neither, so it comes from the box - together
                with one sentence that is known to work on it. */}
            <OwnerAppChips />
          </div>
        </DesignTile>
      ),
    },
    {
      id: "meals",
      lg: { x: 8, y: 7, w: 2, h: 3, minW: 2, minH: 2 },
      node: (
        <DesignTile
          icon="restaurant"
          iconColor="var(--mint)"
          title="Meals"
          img="/design-cloud-5.jpg"
          href="/dashboard/apps/freshtable"
        >
          <MealsPanel />
        </DesignTile>
      ),
    },
    {
      id: "workout",
      lg: { x: 10, y: 7, w: 2, h: 3, minW: 2, minH: 2 },
      node: (
        <DesignTile
          icon="fitness_center"
          iconColor="var(--lemon)"
          title="Workout"
          img="/design-cloud-2.jpg"
          href="/dashboard/apps/fitfam"
        >
          <WorkoutPanel />
        </DesignTile>
      ),
    },
    /* Registry widgets — added/removed via chat ("add NPR news to my
       dashboard"). New ones drop in at the bottom of the board. */
    ...widgets.map(
      (w, i): TileDef => ({
        id: w.id,
        lg: {
          x: (i % 3) * 4,
          y: 10 + Math.floor(i / 3) * 2,
          w: 4,
          h: 2,
        },
        node: renderWidget(w),
      })
    ),
  ];

  const isHidden = (id: string) =>
    id === "transcript" ? !showTranscript : hiddenIds.includes(id);
  const visibleTiles = allTiles.filter((t) => !isHidden(t.id));
  const hiddenTiles = allTiles.filter((t) => isHidden(t.id));
  const trayName = (id: string) =>
    TILE_NAMES[id] ??
    widgets.find((w) => w.id === id)?.props?.title ??
    id;

  return (
    <section
      ref={sectionRef}
      className="flex min-h-0 flex-col"
      style={{
        animation: "slide-up 0.3s ease-out",
        padding: "clamp(10px, 1.1vw, 16px)",
        height: kioskHeight ? `${kioskHeight}px` : undefined,
        overflow: kioskHeight ? "hidden" : undefined,
      }}
    >
      {/* ═══════ HERO (design 4a) ═══════ */}
      <div
        className="puff-glass relative mb-3 flex flex-none items-center overflow-hidden rounded-[32px] border backdrop-blur-[18px]"
        style={{
          height: "clamp(170px, 19.5vh, 236px)",
          minHeight: "clamp(170px, 19.5vh, 236px)",
          borderColor: "var(--line)",
          background: "var(--card)",
          boxShadow: "var(--shadow)",
          padding: "14px clamp(18px, 2.4vw, 36px)",
          gap: "clamp(18px, 2.4vw, 36px)",
        }}
      >
        {/* Cloud photo behind the glass. objectPosition pulls the crop off
            the photo's three cloud shapes: `cover` on a 442×612 portrait
            inside a ~1600×288 hero magnifies them ~4× into garish
            yellow/pink/green blobs. The lower band of the same photo is a
            smooth pastel wash — the ambient cloudscape the design wants. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src="/design-cloud-2.jpg"
          alt=""
          aria-hidden
          className="pointer-events-none absolute inset-0 h-full w-full object-cover"
          style={{ opacity: "var(--img2)", objectPosition: "center 88%" }}
        />
        <div className="relative min-w-0 flex-1">
          <div className="mb-2.5 flex flex-wrap items-center gap-3">
            <span
              className="inline-flex items-center gap-2 rounded-full px-3.5 py-1.5"
              style={{
                background: `color-mix(in srgb, ${statusColor} 16%, transparent)`,
                color: statusColor,
                fontFamily: JBM,
                fontWeight: 700,
                fontSize: 12,
                letterSpacing: "0.1em",
              }}
            >
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{
                  background: statusColor,
                  animation: backendConnected
                    ? "puffBlink 3s infinite"
                    : undefined,
                }}
              />
              {statusLabel}
            </span>
            {backendConnected && inferenceStatus !== "unknown" && (
              <span
                className="rounded-full px-3.5 py-1.5"
                style={{
                  background: "var(--chip)",
                  color: "var(--text2)",
                  fontFamily: JBM,
                  fontWeight: 700,
                  fontSize: 12,
                  letterSpacing: "0.06em",
                }}
              >
                {inferenceModel} · {inferenceBackend}
              </span>
            )}
          </div>
          <h1
            style={{
              margin: 0,
              fontFamily: FREDOKA,
              fontWeight: 600,
              fontSize: "clamp(28px, 2.6vw, 44px)",
              lineHeight: 1.05,
              letterSpacing: "-1px",
              color: "var(--text)",
            }}
          >
            {ownerName ? `${getGreeting()}, ${ownerName}.` : `${getGreeting()}.`}
          </h1>
          <p
            className="max-w-[560px]"
            style={{
              margin: "8px 0 0",
              fontFamily: NUNITO,
              fontWeight: 500,
              fontSize: "clamp(15px, 1.15vw, 19px)",
              lineHeight: 1.4,
              color: "var(--text2)",
            }}
          >
            Puff is awake and running entirely on this machine. Say{" "}
            <span style={{ color: "var(--pink)", fontWeight: 700 }}>
              &ldquo;Hey Puff&rdquo;
            </span>{" "}
            or tap to talk.
          </p>
          {/* 12-bar waveform — animates while Puff speaks/listens */}
          <div className="mt-3 flex h-8 items-end gap-[5px]" aria-hidden>
            {WAVE_HEIGHTS.map((h, i) => (
              <span
                key={i}
                className="w-1.5 rounded-full"
                style={{
                  height: `${h}%`,
                  background: WAVE_COLORS[i],
                  ...(voiceActive
                    ? {
                        animation: `puffWave 1.1s ease-in-out ${i * 0.08}s infinite`,
                      }
                    : { opacity: 0.6 }),
                }}
              />
            ))}
          </div>
        </div>
        {/* Puff's parking pocket: the hero's spare right-side stretch, next
            to the mic orb. FloatingPuff measures
            [data-puff-dock] and parks here; below lg she free-floats. */}
        <div
          data-puff-dock
          aria-hidden
          className="hidden h-full flex-none lg:block"
          style={{ width: "clamp(160px, 15vw, 250px)" }}
        />
        {/* Mic orb → Talk */}
        <button
          type="button"
          onClick={() => router.push("/dashboard/talk")}
          aria-label="Talk to Puff"
          className="relative grid flex-none cursor-pointer place-items-center rounded-full border-0"
          style={{
            width: "clamp(72px, 6vw, 100px)",
            height: "clamp(72px, 6vw, 100px)",
            background: "linear-gradient(150deg, var(--pink), var(--magenta))",
            color: "#fff",
            boxShadow: "0 14px 44px var(--glow1)",
          }}
        >
          <MsIcon name="mic" size={0} style={{ fontSize: "clamp(34px, 2.8vw, 46px)" }} />
        </button>
      </div>

      {/* Error banner */}
      {error && !loading && (
        <div
          className="mb-3 flex flex-none items-center gap-3 rounded-3xl border px-5 py-3 backdrop-blur-[14px]"
          style={{
            borderColor: "color-mix(in srgb, var(--puff-red) 35%, transparent)",
            background: "color-mix(in srgb, var(--puff-red) 7%, transparent)",
          }}
        >
          <MsIcon name="error" size={24} style={{ color: "var(--puff-red)" }} />
          <div className="flex-1">
            <p
              style={{
                fontFamily: NUNITO,
                fontWeight: 700,
                fontSize: 15,
                color: "var(--puff-red)",
              }}
            >
              Backend offline
            </p>
            <p
              style={{
                fontFamily: NUNITO,
                fontWeight: 500,
                fontSize: 13,
                color: "var(--text2)",
              }}
            >
              Start the backend with{" "}
              <code
                style={{
                  fontFamily: JBM,
                  fontSize: 12,
                  color: "var(--puff-red)",
                }}
              >
                python -m puffpai.server
              </code>{" "}
              and sync.
            </p>
          </div>
          <button
            onClick={refresh}
            className="cursor-pointer rounded-full border px-4 py-1.5 transition hover:opacity-80"
            style={{
              borderColor:
                "color-mix(in srgb, var(--puff-red) 35%, transparent)",
              color: "var(--puff-red)",
              fontFamily: NUNITO,
              fontWeight: 700,
              fontSize: 13,
            }}
          >
            Retry
          </button>
        </div>
      )}

      {/* The bot the owner built, and what this build's own checks found.
           Both above the arrangeable board for the same reason the consent
           queue is: TileGrid sinks a never-seen tile to the bottom of a saved
           layout. Each renders nothing when it has nothing honest to say. */}
      <MeetPersonas />
      <ProveStrip />

      {/* Drawbridge consent queue — above the arrangeable board on purpose:
           TileGrid drops never-seen tiles at the BOTTOM of a saved layout,
           so a consent tile would sink out of sight. Renders nothing when
           the queue is empty. */}
      <ConsentRequestsBanner />

      {/* What Puff found in what was sent to her — same reason for being up
           here, same silence when the queue is empty. */}
      <SuggestionsTile />

      {/* ═══════ MAIN BOARD ═══════
           flex-1 + min-h-0 makes this the leftover slot; TileGrid measures it
           and sizes its rows to fill it exactly (kiosk fit). */}
      <div className="min-h-0 flex-1">
        <TileGrid
          key={gridEpoch}
          storageKey="dash-board-v2"
          editing={arranging}
          onRemoveTile={hideTile}
          tiles={visibleTiles}
          fitHeight
        />
      </div>

      {/* Arrange tray (design 791-803) */}
      {arranging && (
        <div
          className="mt-3 flex flex-none flex-wrap items-center gap-3.5 rounded-3xl border border-dashed px-5 py-3"
          style={{ borderColor: "var(--pink)", background: "var(--card2)" }}
        >
          <div
            style={{
              fontFamily: NUNITO,
              fontWeight: 500,
              fontSize: 15,
              color: "var(--text2)",
            }}
          >
            Drag a tile to move it, or say{" "}
            <span style={{ color: "var(--pink)", fontWeight: 700 }}>
              &ldquo;Puff, put the weather top left&rdquo;
            </span>
            {hiddenTiles.length > 0 ? ", then add:" : "."}
          </div>
          {hiddenTiles.map((t) => (
            <TrayPill key={t.id} icon="add" onClick={() => unhideTile(t.id)}>
              {trayName(t.id)}
            </TrayPill>
          ))}
          <div className="flex-1" />
          <TrayPill icon="sync" onClick={refresh}>
            Sync
          </TrayPill>
          <TrayPill icon="restart_alt" onClick={resetLayout}>
            Reset layout
          </TrayPill>
          <button
            type="button"
            onClick={() => setArranging(false)}
            className="flex h-11 cursor-pointer items-center gap-2 rounded-full border-0 px-5 transition hover:opacity-90"
            style={{
              background: "linear-gradient(140deg, var(--pink), var(--magenta))",
              color: "#fff",
              fontFamily: NUNITO,
              fontWeight: 700,
              fontSize: 15,
            }}
          >
            <MsIcon name="check_circle" size={19} />
            Done
          </button>
        </div>
      )}
    </section>
  );
}

/* ═══════════════ Live reminders (owns the countdown tick) ═══════════════ */

/** Countdown refresh while a timer is running. */
const REMINDER_TICK_MS = 1_000;
/** Longest single wait for an alarm's due time; re-checks after sleep/skew. */
const REMINDER_MAX_WAIT_MS = 60 * 60_000;

/** Active timers/alarms, LIVE remaining from ts + tot, soonest first. */
function activeAlarmRows(items: any[] | undefined, nowMs: number): any[] {
  return (items || [])
    .filter((i: any) => {
      if (i.status === "stopped") return false;
      const liveRem = computeLiveRemaining(i, nowMs);
      return liveRem > -300; // still show up to 5 min past expiry ("ringing")
    })
    .map((i: any) => ({
      ...i,
      _liveRem: computeLiveRemaining(i, nowMs),
    }))
    .sort((a: any, b: any) => (a._liveRem ?? 0) - (b._liveRem ?? 0))
    .slice(0, 6);
}

/**
 * The Reminders tile plus the only per-second clock on the dashboard.
 * - A running timer is on screen (counting down, or ringing until it drops
 *   off 5 min past expiry): tick every REMINDER_TICK_MS.
 * - Only alarms: one timeout to the soonest alarm's due second (the row
 *   shows a fixed time; it just has to flip to ringing on time).
 * - Nothing time-dependent: no timer at all.
 * Re-renders only this tile; the chime and the spoken announce are driven
 * by the server's `fired` status (Dashboard effect, FloatingPuff), not by
 * this tick.
 */
function LiveRemindersTile({
  alarmItems,
  pendingTasks,
}: {
  alarmItems: any[] | undefined;
  pendingTasks: any[];
}) {
  const [tick, setTick] = useState(0);
  const nowMs = Date.now();
  const activeAlarms = activeAlarmRows(alarmItems, nowMs);

  const timerRunning = activeAlarms.some(
    (i: any) => i.mode === "timer" && i.status !== "paused" && i.ts && i.tot
  );
  const nextAlarmDueMs = activeAlarms.reduce((min: number, i: any) => {
    if (i.mode !== "alarm" || !i.targetMs || i._liveRem <= 0) return min;
    return Math.min(min, i.targetMs);
  }, Infinity);

  useEffect(() => {
    if (timerRunning) {
      const id = setInterval(() => setTick((t) => t + 1), REMINDER_TICK_MS);
      return () => clearInterval(id);
    }
    if (Number.isFinite(nextAlarmDueMs)) {
      // liveRem hits 0 one second before targetMs (floor); +50ms margin.
      const wait = Math.min(
        REMINDER_MAX_WAIT_MS,
        Math.max(0, nextAlarmDueMs - 1000 - Date.now() + 50)
      );
      const id = setTimeout(() => setTick((t) => t + 1), wait);
      return () => clearTimeout(id);
    }
  }, [timerRunning, nextAlarmDueMs, tick]);

  // Reminders rows: soonest alarms first, then open tasks.
  const reminderRows: ReminderRow[] = [
    ...activeAlarms.map((item: any) => {
      const liveRem = item._liveRem ?? item.rem ?? 0;
      return {
        id: item.id,
        label:
          item.mode === "timer"
            ? `${item.desc || "Timer"} · ${formatRemaining(liveRem)}`
            : `${item.desc || "Alarm"} · ${formatAlarmTime(item)}`,
        ringing: item.status !== "stopped" && liveRem <= 0,
      };
    }),
    ...pendingTasks.map((task: any) => ({
      id: `task-${task.id}`,
      label: task.title || "Untitled task",
    })),
  ];

  return <RemindersTile items={reminderRows} />;
}

/* ═══════════════ HELPERS ═══════════════ */

function getGreeting(): string {
  const hour = new Date().getHours();
  if (hour >= 5 && hour < 12) return "Good morning";
  if (hour >= 12 && hour < 18) return "Good afternoon";
  return "Good evening";
}

function formatRelativeTime(isoString: string): string {
  try {
    const then = new Date(isoString).getTime();
    const now = Date.now();
    const diffSec = Math.floor((now - then) / 1000);
    if (diffSec < 60) return "Just now";
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    return `${Math.floor(diffSec / 86400)}d ago`;
  } catch {
    return "";
  }
}

/**
 * Live remaining seconds for a timer/alarm from its stored creation
 * timestamp + duration, so countdowns stay honest between polls.
 */
function computeLiveRemaining(item: any, nowMs: number): number {
  if (!item) return 0;
  if (item.mode === "alarm" && item.targetMs) {
    return Math.max(0, Math.floor((item.targetMs - nowMs) / 1000));
  }
  if (item.mode === "timer" && item.ts && item.tot) {
    const expiryMs = item.ts + item.tot * 1000;
    return Math.max(-9999, Math.floor((expiryMs - nowMs) / 1000));
  }
  return item.rem || 0;
}

function formatRemaining(seconds: number): string {
  if (seconds == null || isNaN(seconds)) return "unknown";
  if (seconds <= 0) return "ringing!";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatAlarmTime(item: any): string {
  if (item.targetMs) {
    return new Date(item.targetMs).toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });
  }
  return formatRemaining(item._liveRem ?? item.rem ?? 0);
}

/* ═══════════════ SUB-COMPONENTS ═══════════════ */

function EmptyNote({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontFamily: NUNITO,
        fontWeight: 500,
        fontSize: 14,
        color: "var(--text3)",
      }}
    >
      {children}
    </div>
  );
}

function TrayPill({
  icon,
  onClick,
  children,
}: {
  icon: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex h-11 cursor-pointer items-center gap-2 rounded-full border px-[18px] transition hover:opacity-80"
      style={{
        borderColor: "var(--line)",
        background: "var(--card)",
        color: "var(--text)",
        fontFamily: NUNITO,
        fontWeight: 700,
        fontSize: 15,
      }}
    >
      <MsIcon name={icon} size={19} />
      {children}
    </button>
  );
}

function AppChip({
  icon,
  label,
  color,
  href,
}: {
  icon: string;
  label: string;
  color: string;
  href: string;
}) {
  return (
    <Link
      href={href}
      className="group flex flex-col items-center justify-center gap-1 rounded-2xl border py-2 transition hover:opacity-80"
      style={{ borderColor: "var(--line)", background: "var(--card2)" }}
    >
      <div
        className="grid h-9 w-9 place-items-center rounded-[12px]"
        style={{
          background: `color-mix(in srgb, ${color} 16%, transparent)`,
        }}
      >
        <MsIcon name={icon} size={22} style={{ color }} />
      </div>
      <span
        style={{
          fontFamily: NUNITO,
          fontWeight: 600,
          fontSize: 12,
          color: "var(--text2)",
        }}
      >
        {label}
      </span>
    </Link>
  );
}

// Snapshot refresh cadence lives in app/_lib/kioskActivity.ts: CAMERA_POLL_MS
// (5s) everywhere, KIOSK_CAMERA_IDLE_POLL_MS (15s) on an untouched kiosk.
// Keep gentle: aggressive polling keeps battery cameras awake and drains
// them, and camera hubs throttle.

/* ═══════════════ Live meals panel (FreshTable meal plan) ═══════════════ */
function MealsPanel() {
  const { data } = useAppData<any>("freshtable");
  const dayName = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"][new Date().getDay()];
  const todays: any[] = (data?.mealPlan ?? [])
    .filter((e: any) => e.day === dayName)
    .sort((a: any, b: any) =>
      ["breakfast", "lunch", "dinner", "snack"].indexOf(a.meal) -
      ["breakfast", "lunch", "dinner", "snack"].indexOf(b.meal));
  const order = data?.groceryOrder;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-hidden">
        {todays.length === 0 ? (
          <EmptyNote>
            No meals planned for today. Say{" "}
            <span style={{ color: "var(--pink)", fontWeight: 700 }}>
              &ldquo;plan this week&rsquo;s meals&rdquo;
            </span>{" "}
            in chat.
          </EmptyNote>
        ) : (
          todays.slice(0, 4).map((e) => (
            <div key={e.id} className="flex items-baseline justify-between gap-3">
              <span
                style={{
                  fontFamily: JBM,
                  fontWeight: 700,
                  fontSize: 10,
                  letterSpacing: "0.1em",
                  color: "var(--text3)",
                  textTransform: "uppercase",
                }}
              >
                {e.meal}
              </span>
              <span
                className="min-w-0 flex-1 truncate text-right"
                style={{
                  fontFamily: NUNITO,
                  fontWeight: 600,
                  fontSize: 15,
                  color: "var(--text)",
                }}
              >
                {e.dish}
              </span>
            </div>
          ))
        )}
      </div>
      {order?.item_count > 0 && (
        <div
          className="mt-2 flex flex-none items-center justify-between rounded-xl px-3 py-2"
          style={{ background: "var(--chip)" }}
        >
          <span
            style={{
              fontFamily: NUNITO,
              fontWeight: 600,
              fontSize: 13,
              color: "var(--text2)",
            }}
          >
            Grocery order draft
          </span>
          <span
            style={{
              fontFamily: JBM,
              fontWeight: 700,
              fontSize: 12,
              color: "var(--pink)",
            }}
          >
            {order.item_count} items
          </span>
        </div>
      )}
    </div>
  );
}

/* ═══════════════ Per-user workout panel (offline library) ═══════════════ */
interface WorkoutProgram {
  user: string;
  users: string[];
  program: string;
  days_per_week: number;
  session: { name: string; duration_min: number; exercises: { name: string; sets: number; reps: string }[] } | null;
}

function WorkoutPanel() {
  const [selected, setSelected] = useState<string>("");
  const [data, setData] = useState<WorkoutProgram | null>(null);

  /* Household members, in order of authority:
       1. /fitness/program's own `users[]` - the only list this widget's
          `?user=` picker is guaranteed to be keyed by, so it wins.
       2. the FitFam app's member list, the same source the app routes read,
          for a box where the fitness library has not been populated yet.
       3. role placeholders - a fresh install must never show invented names.
     (2) and (3) are display-only: a name from either may not be a key
     /fitness/program recognises, in which case the fetch below simply returns
     nothing and the panel keeps its current content. */
  const { data: fitfamData } = useAppData<any>("fitfam");
  const members = useMemo(() => {
    const fromProgram = data?.users ?? [];
    if (fromProgram.length) return fromProgram;
    const fromApp = Object.values(fitfamData?.users ?? {})
      .map((u: any) => u?.name)
      .filter((n: any): n is string => typeof n === "string" && n.trim() !== "");
    if (fromApp.length) return fromApp;
    return [...ROLE_PLACEHOLDERS];
  }, [data?.users, fitfamData?.users]);

  useEffect(() => {
    const saved = typeof window !== "undefined" ? localStorage.getItem("puffpai_workout_user") : null;
    if (saved) setSelected(saved);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch(`${CAM_API_BASE}/fitness/program${selected ? `?user=${encodeURIComponent(selected)}` : ""}`, {
      signal: AbortSignal.timeout(10000),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        // Adopt the payload whenever it carries a roster, not only when there
        // is a session: on a rest day the old guard threw the real member list
        // away and the panel fell back to its hard-coded one.
        if (!cancelled && d && (d.session || d.users?.length)) setData(d);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [selected]);

  const pick = (u: string) => {
    setSelected(u);
    try { localStorage.setItem("puffpai_workout_user", u); } catch {}
  };

  return (
    <div className="flex h-full min-h-0 flex-col gap-2 overflow-hidden">
      {/* Family member pills */}
      <div className="flex flex-none flex-wrap gap-1.5">
        {members.map((u) => {
          const active = (data?.user ?? "") === u;
          return (
            <button
              key={u}
              onClick={(e) => { e.preventDefault(); e.stopPropagation(); pick(u); }}
              className="cursor-pointer rounded-full border-0 px-3 py-1 transition"
              style={{
                background: active
                  ? "linear-gradient(140deg, var(--pink), var(--magenta))"
                  : "var(--chip)",
                color: active ? "#fff" : "var(--text2)",
                fontFamily: NUNITO,
                fontWeight: 700,
                fontSize: 11,
              }}
            >
              {u}
            </button>
          );
        })}
      </div>
      <div
        className="flex-none rounded-2xl px-3.5 py-2.5"
        style={{ background: "var(--card2)" }}
      >
        <p
          style={{
            fontFamily: NUNITO,
            fontWeight: 700,
            fontSize: 15,
            color: "var(--text)",
          }}
        >
          {data?.session?.name ?? "…"}
        </p>
        <p
          style={{
            fontFamily: JBM,
            fontWeight: 600,
            fontSize: 11,
            color: "var(--text3)",
          }}
        >
          {data?.session ? `${data.session.duration_min} MIN · ${data.program.toUpperCase()}` : ""}
        </p>
      </div>
      <div className="min-h-0 flex-1 space-y-1 overflow-hidden">
        {(data?.session?.exercises ?? []).slice(0, 4).map((ex) => (
          <div key={ex.name} className="flex items-center justify-between">
            <span
              className="truncate"
              style={{
                fontFamily: NUNITO,
                fontWeight: 600,
                fontSize: 13,
                color: "var(--text2)",
              }}
            >
              {ex.name}
            </span>
            <span
              style={{
                fontFamily: JBM,
                fontWeight: 600,
                fontSize: 12,
                color: "var(--mint)",
              }}
            >
              {ex.sets} × {ex.reps}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ═══════════════ Dynamic camera tiles (registry-driven) ═══════════════
 * Tiles come from the PuffPAI backend registry (data/dashboard_cameras.json),
 * which the chat agent (Cortex→Bridge→Drawbridge) manages. Shows no tiles
 * until the registry answers. */
const CAM_API_BASE = "/api/pai";

interface CameraTile {
  entity_id: string;
  label: string;
}

function CamerasPanel() {
  const [tiles, setTiles] = useState<CameraTile[]>([]);
  /** True once the registry answered; only registry tiles are removable. */
  const [fromRegistry, setFromRegistry] = useState(false);

  const load = useCallback(() => {
    return fetch(`${CAM_API_BASE}/dashboard/cameras`, {
      signal: AbortSignal.timeout(8000),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (Array.isArray(d?.cameras)) {
          setTiles(d.cameras);
          setFromRegistry(true);
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
    const iv = setInterval(load, REFRESH_INTERVAL_MS);
    // Approving a consent request elsewhere can add a tile — pick it up now
    // rather than up to 30s later.
    const off = onConsentChanged(load);
    return () => {
      clearInterval(iv);
      off();
    };
  }, [load]);

  const removeTile = useCallback(
    async (entityId: string) => {
      await removeDashboardCamera(entityId);
      setTiles((prev) => prev.filter((t) => t.entity_id !== entityId));
      load();
    },
    [load]
  );

  const body =
    tiles.length === 0 ? (
      <EmptyNote>No cameras on the dashboard yet. Add one below.</EmptyNote>
    ) : tiles.length === 1 ? (
      <CameraFeed
        label={tiles[0].label}
        status="live"
        entityId={tiles[0].entity_id}
        fill
        onRemove={fromRegistry ? () => removeTile(tiles[0].entity_id) : undefined}
      />
    ) : (
      // Stacked full-width tiles with staggered probes so the hub never sees
      // simultaneous stream requests.
      <div className="flex h-full flex-col gap-2 overflow-y-auto">
        {tiles.map((t, i) => (
          <CameraFeed
            key={t.entity_id}
            label={t.label}
            status="live"
            entityId={t.entity_id}
            startDelayMs={i * 5000}
            onRemove={fromRegistry ? () => removeTile(t.entity_id) : undefined}
          />
        ))}
      </div>
    );

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      <div className="min-h-0 flex-1">{body}</div>
      <AddCameraControl
        existing={tiles.map((t) => t.entity_id)}
        onAdded={load}
      />
    </div>
  );
}

/* ── Add a camera tile ──────────────────────────────────────────────
 * POST /dashboard/cameras runs Bridge -> Drawbridge. A missing per-device
 * consent comes back 403 having FILED a consent request — that is the
 * designed flow, so it's shown as "Permission needed" with the new request
 * inline, not as an error. */
function AddCameraControl({
  existing,
  onAdded,
}: {
  existing: string[];
  onAdded: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [candidates, setCandidates] = useState<HADevice[] | null>(null);
  const [choice, setChoice] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<ConsentRequest | null>(null);

  // Candidate cameras come from the HA bridge the dashboard already talks to.
  useEffect(() => {
    if (!open || candidates !== null) return;
    getHADevices("camera")
      .then((d) => setCandidates(Array.isArray(d?.devices) ? d.devices : []))
      .catch(() => setCandidates([]));
  }, [open, candidates]);

  const available = (candidates ?? []).filter(
    (d) => d.entity_id.startsWith("camera.") && !existing.includes(d.entity_id)
  );
  const useTypedInput = candidates !== null && available.length === 0;
  const entityId = useTypedInput ? typed.trim() : choice;
  const typedValid = /^camera\.[a-z0-9_]+$/.test(typed.trim());
  const canSubmit = !busy && (useTypedInput ? typedValid : !!choice);

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setNotice(null);
    setError(null);
    setPending(null);
    try {
      const res = await addDashboardCamera(entityId);
      if (res.kind === "added") {
        setNotice(res.message);
        setChoice("");
        setTyped("");
        setCandidates(null);
        onAdded();
      } else if (res.kind === "needs_permission") {
        // The backend's 403 text is written for the chat agent, not the owner.
        setNotice("Permission needed. Drawbridge filed a request for this camera.");
        const reqs = await getConsentRequests().catch((): ConsentRequest[] => []);
        setPending(reqs.find((r) => r.entity_id === entityId) ?? null);
      } else {
        setError(res.message);
      }
    } catch (e: any) {
      setError(e?.message || "Could not reach the backend.");
    } finally {
      setBusy(false);
    }
  };

  const smallFont = {
    fontFamily: NUNITO,
    fontWeight: 600,
    fontSize: 12,
  } as const;

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex cursor-pointer items-center justify-center gap-1.5 rounded-full border py-1.5 transition hover:opacity-80"
        style={{
          ...smallFont,
          borderColor: "var(--line)",
          color: "var(--text3)",
          background: "transparent",
        }}
      >
        <MsIcon name="add" size={15} />
        Add camera
      </button>
    );
  }

  return (
    <div
      className="rounded-2xl border p-2.5"
      style={{ borderColor: "var(--line)", background: "var(--card2)" }}
    >
      <div className="flex items-center gap-2">
        {useTypedInput ? (
          <input
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder="camera.entity_id"
            spellCheck={false}
            className="min-w-0 flex-1 rounded-full border px-3 py-1.5 outline-none"
            style={{
              fontFamily: JBM,
              fontSize: 11,
              borderColor: "var(--line)",
              background: "var(--card)",
              color: "var(--text)",
            }}
          />
        ) : (
          <select
            value={choice}
            onChange={(e) => setChoice(e.target.value)}
            className="min-w-0 flex-1 rounded-full border px-3 py-1.5 outline-none"
            style={{
              ...smallFont,
              borderColor: "var(--line)",
              background: "var(--card)",
              color: "var(--text)",
            }}
          >
            <option value="">
              {candidates === null ? "Loading cameras…" : "Choose a camera…"}
            </option>
            {available.map((d) => (
              <option key={d.entity_id} value={d.entity_id}>
                {d.friendly_name || d.entity_id}
              </option>
            ))}
          </select>
        )}
        <button
          type="button"
          onClick={submit}
          disabled={!canSubmit}
          className="cursor-pointer rounded-full border-0 px-3.5 py-1.5 transition hover:opacity-90 disabled:opacity-40"
          style={{
            ...smallFont,
            background: "linear-gradient(140deg, var(--pink), var(--magenta))",
            color: "#fff",
          }}
        >
          {busy ? "Adding…" : "Add"}
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="cursor-pointer border-0 bg-transparent px-1 transition hover:opacity-70"
          style={{ color: "var(--text3)" }}
          aria-label="Close"
        >
          <MsIcon name="close" size={16} />
        </button>
      </div>
      {useTypedInput && typed.trim() !== "" && !typedValid && (
        <p className="mt-1.5" style={{ ...smallFont, fontSize: 11, color: "var(--text3)" }}>
          Entity ids look like <span style={{ fontFamily: JBM }}>camera.front_door</span>.
        </p>
      )}
      {notice && (
        <p className="mt-2" style={{ ...smallFont, color: "var(--lemon)" }}>
          {notice}
        </p>
      )}
      {error && (
        <p className="mt-2" style={{ ...smallFont, color: "var(--puff-red)" }}>
          {error}
        </p>
      )}
      {pending && (
        <div className="mt-2">
          <ConsentRequestRow
            request={pending}
            onResolved={() => {
              setPending(null);
              setNotice(null);
              setCandidates(null);
              onAdded();
            }}
          />
        </div>
      )}
    </div>
  );
}

function CameraFeed({
  label,
  entityId,
  status = "offline",
  fill = false,
  startDelayMs = 0,
  onRemove,
}: {
  label: string;
  entityId?: string;
  status?: "live" | "offline";
  fill?: boolean;
  /** Stagger probes so multiple tiles don't hit the Reolink hub at once */
  startDelayMs?: number;
  /** Omit to hide the remove control (e.g. the offline fallback tile) */
  onRemove?: () => void;
}) {
  const [imgSrc, setImgSrc] = useState<string | null>(null);
  const [streamFailed, setStreamFailed] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const isLive = status === "live" && !!entityId;

  /* Turning a camera off is safety-critical: the backend refuses it with a 409
   * unless the request carries a human-confirmation ticket. This hook wraps the
   * attempt — on a 409 it opens the confirm dialog rather than surfacing an
   * error, and a 403 from the Drawbridge consent gate routes to the existing
   * consent surface instead. */
  const [powerNotice, setPowerNotice] = useState<string | null>(null);
  const confirmPower = useHaConfirmation({
    onResult: (r) => {
      if (r.kind === "ok") setPowerNotice(null);
      else if (r.kind === "failed") setPowerNotice(r.message);
    },
    onConsentNeeded: () =>
      setPowerNotice("Permission needed. Approve this camera in Consent requests."),
  });

  // Poll HA camera proxy for snapshots. Battery cams sleep — long probe
  // timeout, staggered starts, and a failed stream retries every 30s.
  useEffect(() => {
    if (!isLive || !entityId) return;
    let cancelled = false;
    let poll: ReturnType<typeof setTimeout> | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let lastFetchMs = 0;

    const fetchSnapshot = () => {
      lastFetchMs = Date.now();
      setImgSrc(`/api/camera/${entityId}?t=${lastFetchMs}`);
    };

    // Self-rescheduling so the cadence can change between frames: 15s on an
    // untouched kiosk, 5s otherwise (cameraPollMs).
    // dueMs/waitMs remember the pending wait so a wake can shrink it in
    // proportion instead of collapsing every camera to "now".
    let dueMs = 0;
    let waitMs = 0;
    const schedule = (delayMs: number) => {
      if (poll) clearTimeout(poll);
      waitMs = delayMs;
      dueMs = Date.now() + delayMs;
      poll = setTimeout(() => {
        poll = null;
        if (cancelled) return;
        fetchSnapshot();
        schedule(cameraPollMs());
      }, delayMs);
    };

    // Input after a quiet spell: pull the next frame in to the 5s cadence
    // instead of waiting out the rest of a 15s wait. The rest of the wait is
    // scaled (15s -> 5s), not clamped to 0, so each camera keeps its place in
    // the startDelayMs stagger and they don't all fetch at once in lockstep.
    const offWake = onInputAfterQuiet(() => {
      if (cancelled || !poll) return;
      const fast = cameraPollMs();
      if (waitMs <= fast) return;
      const remaining = Math.max(0, dueMs - Date.now());
      schedule(Math.round((remaining * fast) / waitMs));
    });

    const probe = () => {
      fetch(`/api/camera/${entityId}`, { signal: AbortSignal.timeout(20000) })
        .then((r) => {
          if (cancelled) return;
          if (r.ok) {
            setStreamFailed(false);
            fetchSnapshot();
            schedule(cameraPollMs());
          } else {
            setStreamFailed(true);
            retry = setTimeout(probe, 30_000);
          }
        })
        .catch(() => {
          if (!cancelled) {
            setStreamFailed(true);
            retry = setTimeout(probe, 30_000);
          }
        });
    };

    const kickoff = setTimeout(probe, startDelayMs);
    return () => {
      cancelled = true;
      clearTimeout(kickoff);
      if (retry) clearTimeout(retry);
      if (poll) clearTimeout(poll);
      offWake();
    };
  }, [isLive, entityId, startDelayMs]);

  const showStream = isLive && imgSrc && !streamFailed;

  return (
    <div
      className={`group relative overflow-hidden rounded-[14px] border ${fill ? "h-full" : ""}`}
      style={{
        borderColor: "var(--line)",
        background: showStream ? "var(--puff-surface-deep)" : "transparent",
      }}
    >
      {showStream && (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={imgSrc}
          alt={label}
          className={`${fill ? "h-full w-full" : "aspect-video w-full"} object-cover`}
          onError={() => setStreamFailed(true)}
        />
      )}
      {!showStream && (
        /* Design's hatched placeholder for offline/unavailable feeds */
        <div
          className={`grid place-items-end ${fill ? "h-full" : "aspect-video"}`}
          style={{ background: HATCH, padding: 8 }}
        >
          <span
            className="justify-self-start"
            style={{
              fontFamily: JBM,
              fontWeight: 600,
              fontSize: 11,
              letterSpacing: "0.08em",
              color: "var(--text3)",
              textTransform: "uppercase",
            }}
          >
            {isLive ? (streamFailed ? "Stream unavailable" : "Connecting…") : "Offline"}
          </span>
        </div>
      )}
      {/* Caption chip + REC status */}
      {showStream && (
        <div className="absolute inset-x-0 bottom-0 flex items-end justify-between bg-gradient-to-t from-black/60 to-transparent px-2 pb-1.5 pt-4">
          <span
            style={{
              fontFamily: JBM,
              fontWeight: 600,
              fontSize: 11,
              letterSpacing: "0.08em",
              color: "rgba(255,255,255,0.85)",
              textTransform: "uppercase",
            }}
          >
            {label}
          </span>
          {isLive && (
            <span
              className="flex items-center gap-1"
              style={{
                fontFamily: JBM,
                fontWeight: 700,
                fontSize: 9,
                color: "var(--pink)",
                textTransform: "uppercase",
              }}
            >
              <span
                className="h-1.5 w-1.5 rounded-full"
                style={{ background: "var(--pink)" }}
              />
              REC
            </span>
          )}
        </div>
      )}
      {/* Stop recording. Never fires blind: the backend refuses without a
        * confirmation ticket, and HaConfirmDialog is where one is obtained. */}
      {isLive && entityId && (
        <div className="absolute left-1.5 top-1.5 z-10 flex flex-col items-start gap-1">
          <button
            type="button"
            onClick={() =>
              void confirmPower.run({
                kind: "control",
                entityId,
                action: "turn_off",
              })
            }
            title={`Turn off ${label} (asks you to confirm)`}
            aria-label={`Turn off ${label}`}
            className="puff-noblur cursor-pointer rounded-md border-0 bg-black/50 p-1 text-white/40 opacity-0 backdrop-blur-sm transition group-hover:opacity-100"
          >
            <MsIcon name="videocam_off" size={13} />
          </button>
          {powerNotice && (
            <span
              className="puff-noblur max-w-[12rem] rounded bg-black/80 px-1.5 py-0.5 backdrop-blur-sm"
              style={{
                fontFamily: NUNITO,
                fontSize: 9,
                lineHeight: 1.3,
                color: "var(--lemon)",
              }}
            >
              {powerNotice}
            </span>
          )}
        </div>
      )}
      <HaConfirmDialog controller={confirmPower} />
      {/* Remove this tile — two-step so a stray click can't drop a camera */}
      {onRemove && (
        <div className="absolute right-1.5 top-1.5 z-10">
          {confirmRemove ? (
            <div className="puff-noblur flex items-center gap-1 rounded-md bg-black/80 px-1.5 py-1 backdrop-blur-sm">
              <span style={{ fontFamily: NUNITO, fontSize: 9, color: "rgba(255,255,255,0.7)" }}>
                Remove tile?
              </span>
              <button
                type="button"
                onClick={() => {
                  setConfirmRemove(false);
                  onRemove();
                }}
                className="cursor-pointer rounded border-0 bg-transparent px-1 uppercase"
                style={{ fontFamily: NUNITO, fontWeight: 700, fontSize: 9, color: "var(--pink)" }}
              >
                Yes
              </button>
              <button
                type="button"
                onClick={() => setConfirmRemove(false)}
                className="cursor-pointer rounded border-0 bg-transparent px-1 uppercase"
                style={{ fontFamily: NUNITO, fontSize: 9, color: "rgba(255,255,255,0.5)" }}
              >
                No
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmRemove(true)}
              title={`Remove ${label} from the dashboard`}
              aria-label={`Remove ${label} from the dashboard`}
              className="puff-noblur cursor-pointer rounded-md border-0 bg-black/50 p-1 text-white/40 opacity-0 backdrop-blur-sm transition group-hover:opacity-100"
            >
              <MsIcon name="close" size={13} />
            </button>
          )}
        </div>
      )}
    </div>
  );
}
