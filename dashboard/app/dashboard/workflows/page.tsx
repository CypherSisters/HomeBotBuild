"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import {
  mdiRobotOutline,
  mdiWeatherLightningRainy,
  mdiWeatherPartlyCloudy,
  mdiWeatherSunny,
  mdiShieldLockOutline,
  mdiShieldCheckOutline,
  mdiNetworkOutline,
  mdiMicrophoneOutline,
  mdiCalendarSync,
  mdiNewspaperVariantOutline,
  mdiCartOutline,
  mdiHeartPulse,
  mdiSilverwareForkKnife,
  mdiPlayCircleOutline,
  mdiExitRun,
  mdiRefresh,
  mdiPlay,
  mdiCheckCircle,
  mdiAlertCircle,
  mdiClockOutline,
  mdiConnection,
  mdiTimerSandComplete,
  mdiGestureTapButton,
} from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import SectionTitleLineWithButton from "../../_components/Section/TitleLineWithButton";
import CardBox from "../../_components/CardBox";
import Icon from "../../_components/Icon";
import Button from "../../_components/Button";
import {
  getEngineStatus,
  getWorkflowFlows,
  triggerWorkflow,
  type EngineState,
  type EngineStatusResult,
  type WorkflowFlow,
} from "../../_lib/services/automationService";
import { detectKiosk } from "../_lib/kiosk";
import HaAutomations from "./HaAutomations";

/* ── Per-flow presentation ────────────────────────────────────────
 * Keyed by the Node-RED tab label (the flow's real name). This is the
 * human layer over the live flow list: what the flow does, how it fires,
 * and how to categorise it. Unknown flows fall back to DEFAULT_META so a
 * newly-added flow still renders - it just gets a generic blurb until it
 * earns an entry here. */
type Cadence = "webhook" | "scheduled" | "event" | "routine";
interface FlowMeta {
  title?: string;
  description: string;
  icon: string;
  color: string;
  category: string;
  cadence: Cadence;
  payload?: Record<string, unknown>;
}

const FLOW_META: Record<string, FlowMeta> = {
  weather_alerts: {
    title: "Weather Alerts",
    description:
      "Checks the National Weather Service for active watches, warnings and advisories for your area.",
    icon: mdiWeatherLightningRainy,
    color: "from-sky-400 to-blue-600",
    category: "weather",
    cadence: "webhook",
    payload: {},
  },
  daily_weather: {
    title: "Daily Weather",
    description: "Pulls the day's forecast each morning for the dashboard and your briefing.",
    icon: mdiWeatherPartlyCloudy,
    color: "from-sky-400 to-cyan-600",
    category: "weather",
    cadence: "scheduled",
  },
  daily_briefing: {
    title: "Daily Briefing",
    description:
      "Composes your morning briefing from calendar, weather and news into a single spoken summary.",
    icon: mdiNewspaperVariantOutline,
    color: "from-violet-400 to-purple-600",
    category: "briefing",
    cadence: "scheduled",
  },
  daily_news: {
    title: "Daily News",
    description: "Fetches the day's headlines for the news widget and briefing.",
    icon: mdiNewspaperVariantOutline,
    color: "from-indigo-400 to-blue-600",
    category: "news",
    cadence: "scheduled",
  },
  calendar_sync: {
    title: "Calendar Sync",
    description: "Keeps your family calendar and events current for the Calendar app and reminders.",
    icon: mdiCalendarSync,
    color: "from-teal-400 to-emerald-600",
    category: "calendar",
    cadence: "scheduled",
  },
  grocery_list: {
    title: "Grocery List",
    description: "Refreshes the shopping list from meal plans and pantry gaps.",
    icon: mdiCartOutline,
    color: "from-lime-400 to-green-600",
    category: "food",
    cadence: "routine",
  },
  meal_planner: {
    title: "Meal Planner",
    description: "Plans the week's meals and feeds the grocery list.",
    icon: mdiSilverwareForkKnife,
    color: "from-amber-400 to-orange-600",
    category: "food",
    cadence: "scheduled",
  },
  health_check: {
    title: "Health Check",
    description: "Probes every service on the box and reports what's up or degraded.",
    icon: mdiHeartPulse,
    color: "from-rose-400 to-red-600",
    category: "system",
    cadence: "scheduled",
  },
  media_now_playing: {
    title: "Now Playing",
    description: "Tracks what's currently playing across your media sources for the dashboard.",
    icon: mdiPlayCircleOutline,
    color: "from-fuchsia-400 to-pink-600",
    category: "media",
    cadence: "event",
  },
  network_scan: {
    title: "Network Scanner",
    description:
      "Scans your local network for devices and classifies them. Results feed the Firewall device list.",
    icon: mdiNetworkOutline,
    color: "from-emerald-400 to-teal-600",
    category: "security",
    cadence: "webhook",
    payload: {},
  },
  block_device: {
    title: "Block Device",
    description:
      "Adds a firewall rule through Node-RED. Used by the Firewall page when blocking a device.",
    icon: mdiShieldLockOutline,
    color: "from-orange-400 to-red-600",
    category: "security",
    cadence: "webhook",
    payload: { ip: "", reason: "Manual block from dashboard" },
  },
  security_check: {
    title: "Security Check",
    description: "Runs a security audit - locks, doors and cameras - and flags anything left open.",
    icon: mdiShieldCheckOutline,
    color: "from-orange-400 to-amber-600",
    category: "security",
    cadence: "scheduled",
  },
  good_morning: {
    title: "Good Morning",
    description: "Morning routine - lights up and reads the weather when the house wakes.",
    icon: mdiWeatherSunny,
    color: "from-yellow-400 to-amber-600",
    category: "home",
    cadence: "routine",
  },
  leaving_home: {
    title: "Leaving Home",
    description: "Lockdown routine - locks up and turns the lights off when everyone leaves.",
    icon: mdiExitRun,
    color: "from-slate-400 to-gray-600",
    category: "home",
    cadence: "routine",
  },
  puffpai_voice: {
    title: "Voice Pipeline",
    description:
      "Routes voice commands through Node-RED for advanced multi-step smart-home workflows.",
    icon: mdiMicrophoneOutline,
    color: "from-violet-400 to-purple-600",
    category: "voice",
    cadence: "webhook",
    payload: { text: "", intent: "general" },
  },
};

const DEFAULT_META: FlowMeta = {
  description: "Node-RED automation flow.",
  icon: mdiRobotOutline,
  color: "from-slate-400 to-slate-600",
  category: "automation",
  cadence: "routine",
};

/** How each flow fires - shown as a small pill so a scheduled flow isn't
 *  mistaken for one you're expected to click. */
const CADENCE_META: Record<Cadence, { label: string; icon: string }> = {
  webhook: { label: "manual", icon: mdiGestureTapButton },
  scheduled: { label: "scheduled", icon: mdiTimerSandComplete },
  event: { label: "on event", icon: mdiConnection },
  routine: { label: "routine", icon: mdiRobotOutline },
};

function humanize(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/* ── What each state says, once, in one place ─────────────────────
 * The chip and the empty-flows line must never disagree, and only
 * `engine_offline` is allowed to blame Node-RED. */
const STATE_COPY: Record<
  EngineState,
  { chip: string; message: string; tone: string }
> = {
  connected: {
    chip: "Connected",
    message: "",
    tone: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
  },
  engine_offline: {
    chip: "Node-RED offline",
    message: "Node-RED is offline - can't list flows.",
    tone: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
  },
  pairing_required: {
    chip: "Pair this browser",
    message: "This browser isn't paired with PuffPAI.",
    tone: "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400",
  },
  unreachable: {
    chip: "Reconnecting…",
    message: "Can't reach PuffPAI right now. Retrying…",
    tone: "bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
  },
};

/** Poll cadence: steady while things work, quick-then-patient while they don't. */
const POLL_MS = 15000;
const BACKOFF_MS = [2000, 5000, 15000];

/* ── Page component ───────────────────────────────────────────── */
export default function WorkflowsPage() {
  const [engine, setEngine] = useState<EngineStatusResult>({
    state: "unreachable",
  });
  const [flows, setFlows] = useState<WorkflowFlow[]>([]);
  const [loading, setLoading] = useState(true);
  /* The editor listens on the box's loopback only, so the link is a dead end
   * (or worse, a pointer at the VIEWER's own machine) from a phone on the LAN.
   * Measured on the client and starting `false`, so it never flashes. */
  const [onAppliance, setOnAppliance] = useState(false);
  const [triggerStates, setTriggerStates] = useState<
    Record<string, "idle" | "running" | "success" | "error">
  >({});
  const [triggerResults, setTriggerResults] = useState<Record<string, string>>(
    {}
  );

  const failuresRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const runRef = useRef<() => void>(() => {});

  useEffect(() => {
    const host = window.location.hostname;
    setOnAppliance(host === "localhost" || host === "127.0.0.1" || detectKiosk());
  }, []);

  /** One probe. Returns the state so the caller can pick the next delay. */
  const refresh = useCallback(async (): Promise<EngineState> => {
    const result = await getEngineStatus();
    setEngine(result);
    if (result.state === "connected") {
      const flowList = await getWorkflowFlows();
      setFlows(flowList.workflows || []);
    } else {
      setFlows([]);
    }
    // `loading` is the FIRST fetch only - every later poll repaints in place
    // instead of blanking the page back to "Checking...".
    setLoading(false);
    return result.state;
  }, []);

  /* Self-healing poll. If refresh() ran only once on mount, a dashboard
   * restart would leave the page reading "Offline" forever. */
  useEffect(() => {
    let cancelled = false;

    function schedule(ms: number) {
      if (cancelled) return;
      timerRef.current = setTimeout(run, ms);
    }

    async function run() {
      if (cancelled) return;
      if (document.visibilityState === "hidden") {
        schedule(POLL_MS);
        return;
      }
      const state = await refresh();
      if (cancelled) return;
      if (state === "unreachable") {
        const i = Math.min(failuresRef.current, BACKOFF_MS.length - 1);
        failuresRef.current += 1;
        schedule(BACKOFF_MS[i]);
      } else {
        failuresRef.current = 0;
        schedule(POLL_MS);
      }
    }

    runRef.current = () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      failuresRef.current = 0;
      run();
    };

    const onVisible = () => {
      if (document.visibilityState === "visible") runRef.current();
    };

    run();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh]);

  const retry = useCallback(() => runRef.current(), []);

  const webhooks = engine.status?.webhooks || {};
  const connected = engine.state === "connected";
  const copy = STATE_COPY[engine.state];

  const handleTrigger = async (flowName: string) => {
    setTriggerStates((prev) => ({ ...prev, [flowName]: "running" }));
    setTriggerResults((prev) => ({ ...prev, [flowName]: "" }));
    try {
      const meta = FLOW_META[flowName] || DEFAULT_META;
      const result = await triggerWorkflow(flowName, meta.payload || {});
      if (result.success) {
        setTriggerStates((prev) => ({ ...prev, [flowName]: "success" }));
        setTriggerResults((prev) => ({
          ...prev,
          [flowName]:
            typeof result.data === "string"
              ? result.data
              : JSON.stringify(result.data, null, 2).slice(0, 200),
        }));
      } else {
        setTriggerStates((prev) => ({ ...prev, [flowName]: "error" }));
        setTriggerResults((prev) => ({
          ...prev,
          [flowName]: result.error || "Unknown error",
        }));
      }
    } catch (err: unknown) {
      setTriggerStates((prev) => ({ ...prev, [flowName]: "error" }));
      setTriggerResults((prev) => ({
        ...prev,
        [flowName]: err instanceof Error ? err.message : "Failed to trigger",
      }));
    }
    // Reset state after 5 seconds
    setTimeout(() => {
      setTriggerStates((prev) => ({ ...prev, [flowName]: "idle" }));
    }, 5000);
  };

  // Sort: manually-triggerable flows first, then the rest alphabetically -
  // the things you can click land at the top.
  const ordered = [...flows].sort((a, b) => {
    const at = webhooks[a.name] ? 0 : 1;
    const bt = webhooks[b.name] ? 0 : 1;
    return at - bt || a.name.localeCompare(b.name);
  });
  const activeCount = flows.filter((f) => f.active).length;
  const triggerable = flows.filter((f) => webhooks[f.name]).length;

  return (
    <SectionMain>
      {/* Header */}
      <SectionTitleLineWithButton
        icon={mdiRobotOutline}
        title="Automation Workflows"
        main
      >
        <Button
          icon={mdiRefresh}
          label="Refresh"
          color="info"
          small
          roundedFull
          onClick={retry}
        />
      </SectionTitleLineWithButton>

      {/* Node-RED connection status + live counts */}
      <CardBox className="mb-6">
        <div className="flex flex-wrap items-center gap-4">
          <div
            className={`flex h-12 w-12 items-center justify-center rounded-full shadow-md ${
              connected
                ? "bg-gradient-to-br from-green-400 to-emerald-600"
                : "bg-gradient-to-br from-gray-400 to-gray-600"
            } text-white`}
          >
            <Icon path={mdiConnection} size="24" />
          </div>
          <div className="flex-1 min-w-[12rem]">
            <p className="text-sm text-gray-500 dark:text-slate-400">
              Node-RED Automation Engine
            </p>
            <p className="text-lg font-semibold text-[#0C2F44] dark:text-white">
              {onAppliance ? engine.status?.base_url || "http://localhost:1880" : "On the box (opens on the kiosk)"}
            </p>
            {!connected && !loading && (
              <p className="mt-0.5 text-xs text-gray-500 dark:text-slate-400">
                {copy.message}
                {engine.detail ? ` (${engine.detail})` : ""}
              </p>
            )}
          </div>

          {/* Live counts, straight from the runtime */}
          <div className="flex items-center gap-5">
            <div className="text-center">
              <div className="text-lg font-bold">{flows.length}</div>
              <div className="text-[10px] uppercase tracking-wide text-gray-400">
                Flows
              </div>
            </div>
            <div className="text-center">
              <div className="text-lg font-bold text-green-500">{activeCount}</div>
              <div className="text-[10px] uppercase tracking-wide text-gray-400">
                Active
              </div>
            </div>
            <div className="text-center">
              <div className="text-lg font-bold text-[#00B4D8]">{triggerable}</div>
              <div className="text-[10px] uppercase tracking-wide text-gray-400">
                Manual
              </div>
            </div>
          </div>

          <span
            className={`rounded-full px-3 py-1 text-xs font-medium ${
              loading
                ? "bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300"
                : copy.tone
            }`}
            title={loading ? undefined : copy.message || undefined}
          >
            {loading ? "Checking…" : copy.chip}
          </span>

          {/* Manual retry: the poll heals on its own, this is for impatience. */}
          {!connected && !loading && (
            <button
              onClick={retry}
              className="rounded-full bg-[#00B4D8]/10 px-3 py-1 text-xs font-medium text-[#00B4D8] transition hover:bg-[#00B4D8]/20"
            >
              Retry
            </button>
          )}
          {engine.state === "pairing_required" && (
            <a
              href="/send"
              className="rounded-full bg-[#00B4D8]/10 px-3 py-1 text-xs font-medium text-[#00B4D8] transition hover:bg-[#00B4D8]/20"
            >
              Pair this browser &rarr;
            </a>
          )}

          {/* Node-RED binds to the box's loopback: the editor only opens there. */}
          {connected &&
            (onAppliance ? (
              <a
                href={engine.status?.base_url || "http://localhost:1880"}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-full bg-[#00B4D8]/10 px-3 py-1 text-xs font-medium text-[#00B4D8] transition hover:bg-[#00B4D8]/20"
              >
                Open Node-RED &rarr;
              </a>
            ) : (
              <span className="text-xs text-gray-400 dark:text-slate-500">
                Editor opens on the kiosk (Node-RED listens on the box only)
              </span>
            ))}
        </div>
      </CardBox>

      {/* Flow list */}
      <SectionTitleLineWithButton
        icon={mdiClockOutline}
        title={`Deployed Flows${flows.length ? ` · ${flows.length}` : ""}`}
      />

      {flows.length === 0 && !loading && (
        <CardBox>
          <p className="py-8 text-center text-sm text-gray-400">
            {connected ? "No flows reported by Node-RED." : copy.message}
            {engine.state === "pairing_required" && (
              <>
                {" "}
                <a href="/send" className="text-[#00B4D8] hover:underline">
                  Pair it
                </a>
              </>
            )}
          </p>
        </CardBox>
      )}

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2 xl:grid-cols-3">
        {ordered.map((flow) => {
          const meta = FLOW_META[flow.name] || DEFAULT_META;
          const cadence = CADENCE_META[meta.cadence];
          const canTrigger = !!webhooks[flow.name] && connected;
          const state = triggerStates[flow.name] || "idle";
          const resultText = triggerResults[flow.name] || "";

          return (
            <CardBox key={flow.id || flow.name} className="relative overflow-hidden">
              {/* Gradient accent bar */}
              <div
                className={`absolute inset-x-0 top-0 h-1 bg-gradient-to-r ${meta.color}`}
              />

              <div className="flex items-start gap-4 pt-2">
                <div
                  className={`flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-xl bg-gradient-to-br ${meta.color} text-white shadow-md`}
                >
                  <Icon path={meta.icon} size="24" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-base font-semibold text-[#0C2F44] dark:text-white">
                      {meta.title || humanize(flow.name)}
                    </h3>
                    {/* Live active/inactive dot */}
                    <span
                      className={`inline-flex items-center gap-1 text-[11px] font-medium ${
                        flow.active ? "text-green-500" : "text-gray-400"
                      }`}
                    >
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${
                          flow.active ? "bg-green-500" : "bg-gray-400"
                        }`}
                      />
                      {flow.active ? "active" : "inactive"}
                    </span>
                  </div>

                  <div className="mt-1 flex flex-wrap items-center gap-1.5">
                    <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-gray-500 dark:bg-slate-700 dark:text-slate-400">
                      {meta.category}
                    </span>
                    <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500 dark:bg-slate-700 dark:text-slate-400">
                      <Icon path={cadence.icon} size="11" />
                      {cadence.label}
                    </span>
                  </div>

                  <p className="mt-2 text-sm text-gray-500 dark:text-slate-400">
                    {meta.description}
                  </p>

                  {/* Trigger result */}
                  {resultText && (
                    <div
                      className={`mt-3 rounded-lg p-2 font-mono text-xs ${
                        state === "success"
                          ? "bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-400"
                          : "bg-red-50 text-red-700 dark:bg-red-900/20 dark:text-red-400"
                      }`}
                    >
                      {resultText}
                    </div>
                  )}
                </div>

                {/* Run - only for webhook flows */}
                {webhooks[flow.name] ? (
                  <button
                    onClick={() => handleTrigger(flow.name)}
                    disabled={!canTrigger || state === "running"}
                    className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full transition ${
                      state === "running"
                        ? "animate-spin bg-blue-100 text-blue-600"
                        : state === "success"
                          ? "bg-green-100 text-green-600"
                          : state === "error"
                            ? "bg-red-100 text-red-600"
                            : canTrigger
                              ? "bg-[#00B4D8]/10 text-[#00B4D8] hover:bg-[#00B4D8]/20"
                              : "bg-gray-100 text-gray-400 cursor-not-allowed"
                    }`}
                    title={canTrigger ? "Run workflow" : copy.message}
                  >
                    <Icon
                      path={
                        state === "success"
                          ? mdiCheckCircle
                          : state === "error"
                            ? mdiAlertCircle
                            : mdiPlay
                      }
                      size="20"
                    />
                  </button>
                ) : (
                  <span
                    className="flex h-10 flex-shrink-0 items-center rounded-full bg-gray-50 px-2 text-[10px] text-gray-400 dark:bg-slate-800"
                    title="Runs on a schedule or trigger - not manually fired"
                  >
                    auto
                  </span>
                )}
              </div>

              {/* Webhook URL (small footer) */}
              {webhooks[flow.name] && (
                <div className="mt-3 border-t border-gray-100 pt-2 dark:border-slate-700">
                  <code className="text-[10px] text-gray-400 dark:text-slate-500">
                    {webhooks[flow.name]}
                  </code>
                </div>
              )}
            </CardBox>
          );
        })}
      </div>

      {/* Home Assistant automations (read-only) */}
      <HaAutomations />
    </SectionMain>
  );
}
