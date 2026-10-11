"use client";

import { useEffect, useState, useCallback } from "react";
import {
  mdiAccountGroup,
  mdiRefresh,
  mdiSend,
  mdiBrain,
  mdiLan,
  mdiShieldCheck,
  mdiClockOutline,
  mdiCheckCircle,
  mdiAlertCircle,
  mdiCircleOutline,
  mdiMagnify,
  mdiHomeAutomation,
  mdiShieldSearch,
  mdiLightbulbGroup,
  mdiRobotOutline,
  mdiThermometer,
  mdiLockCheck,
  mdiEyeOutline,
  mdiWeatherSunny,
  mdiExitRun,
  mdiClose,
  mdiHeadset,
  mdiArchive,
  mdiCalendarClock,
  mdiHeartPulse,
  mdiCloseCircle,
  mdiToolbox,
  mdiProgressClock,
  mdiSleep,
} from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import SectionTitleLineWithButton from "../../_components/Section/TitleLineWithButton";
import CardBox from "../../_components/CardBox";
import Icon from "../../_components/Icon";
import Button from "../../_components/Button";
import AgentNamesPanel from "./_components/AgentNamesPanel";
import {
  getAgentStatuses,
  getAgentActivity,
  sendAgentTask,
  type AgentStatus,
  type AgentActivity,
  type TeamAgent,
  type AgentBus,
  type AgentCapability,
} from "../../_lib/services/agentService";
import { containerMaxW } from "../../_lib/config";
// Absorbed page (design nav merge): "Workflows" renders inside Agent Team.
// Imported read-only - app/dashboard/workflows/** stays untouched and its
// route keeps working on its own.
import WorkflowsPage from "../workflows/page";

/* ── Agent definitions ────────────────────────────────────────── */
const AGENT_META: Record<
  string,
  { label: string; role: string; icon: string; color: string; border: string }
> = {
  cortex: {
    label: "Cortex",
    role: "Core AI / NLP Engineer",
    icon: mdiBrain,
    color: "from-purple-500 to-violet-700",
    border: "border-l-purple-500",
  },
  bridge: {
    label: "Bridge",
    role: "Integration / API Engineer",
    icon: mdiLan,
    color: "from-cyan-400 to-blue-600",
    border: "border-l-cyan-400",
  },
  // Display name is Drawbridge (PUFFPAI_MESSAGE_CORE); `sentinel` is the
  // internal agent id the API speaks and must not change.
  sentinel: {
    label: "Drawbridge",
    role: "Privacy / Security Engineer",
    icon: mdiShieldCheck,
    color: "from-orange-400 to-red-600",
    border: "border-l-orange-400",
  },
  /* ── Product team agents ──────────────────────────────────── */
  concierge: {
    label: "Concierge",
    role: "Front Desk / Routing",
    icon: mdiHeadset,
    color: "from-fuchsia-500 to-pink-600",
    border: "border-l-fuchsia-500",
  },
  steward: {
    label: "Steward",
    role: "Home / Tools",
    icon: mdiHomeAutomation,
    color: "from-cyan-400 to-blue-600",
    border: "border-l-cyan-400",
  },
  archivist: {
    label: "Archivist",
    role: "Archive / Knowledge",
    icon: mdiArchive,
    color: "from-emerald-400 to-green-700",
    border: "border-l-emerald-400",
  },
  majordomo: {
    label: "Majordomo",
    role: "Orchestration / Scheduling",
    icon: mdiCalendarClock,
    color: "from-indigo-400 to-blue-700",
    border: "border-l-indigo-400",
  },
  caretaker: {
    label: "Caretaker",
    role: "System health",
    icon: mdiHeartPulse,
    color: "from-rose-400 to-red-600",
    border: "border-l-rose-400",
  },
  // WF-7, the generic tool tier. Without an entry here it would fall back to
  // the cortex meta and render as a purple brain labelled "Core AI / NLP
  // Engineer" - the opposite of what it is: deterministic tools, no model, and
  // the only agent that reaches the public internet.
  toolsmith: {
    label: "Toolsmith",
    role: "Web / Workspace Tools",
    icon: mdiToolbox,
    color: "from-amber-400 to-orange-600",
    border: "border-l-amber-400",
  },
};

// The `agent` here is only the display tag + colour: POST /agents/task routes
// on the task TEXT (see _match_task_action), so these map to the product-team
// agent each request naturally belongs to, not the retired embryo names.
const QUICK_ACTIONS = [
  // Caretaker - health
  { label: "System Health", agent: "caretaker", task: "Run a full system health check on all services", icon: mdiMagnify },
  // Steward - home / devices / automations
  { label: "Device Analytics", agent: "steward", task: "Show device analytics - unavailable devices and low batteries", icon: mdiHomeAutomation },
  { label: "Suggest Automations", agent: "steward", task: "Suggest automations based on my devices", icon: mdiRobotOutline },
  { label: "Home Summary", agent: "steward", task: "Give me a full environment summary - temps, lights, who is home", icon: mdiThermometer },
  { label: "Discover Devices", agent: "steward", task: "Discover all devices and show a full inventory", icon: mdiHomeAutomation },
  { label: "Morning Routine", agent: "steward", task: "Good morning - lights on, weather report", icon: mdiWeatherSunny },
  { label: "Leaving Home", agent: "steward", task: "Leaving home - lock up and turn off lights", icon: mdiExitRun },
  { label: "List Workflows", agent: "steward", task: "List all Node-RED workflows and their status", icon: mdiRobotOutline },
  { label: "Toggle Lights", agent: "steward", task: "Toggle all lights", icon: mdiLightbulbGroup },
  // Drawbridge (agent id `sentinel`) - security / privacy
  { label: "Security Audit", agent: "sentinel", task: "Run a full security audit - check all locks, doors, cameras", icon: mdiLockCheck },
  { label: "Privacy Scan", agent: "sentinel", task: "Run a privacy audit on all configs and data files", icon: mdiShieldSearch },
  { label: "Network Inventory", agent: "sentinel", task: "Show all network connected devices", icon: mdiEyeOutline },
  { label: "Exposure Check", agent: "sentinel", task: "Check if any services are externally exposed", icon: mdiShieldCheck },
];

/** Solid accent colour per agent id (the card gradients are two-stop and can't
 *  be used as a text colour). Covers the legacy embryo ids too, so a feed entry
 *  still tints correctly. */
const AGENT_TEXT: Record<string, string> = {
  concierge: "text-fuchsia-500",
  steward: "text-cyan-500",
  sentinel: "text-orange-500",
  archivist: "text-emerald-500",
  majordomo: "text-indigo-500",
  caretaker: "text-rose-500",
  toolsmith: "text-amber-500",
  cortex: "text-fuchsia-500",
  bridge: "text-cyan-500",
};

/* ── Helper components ────────────────────────────────────────── */

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { bg: string; text: string; icon: string }> = {
    active: { bg: "bg-green-100 dark:bg-green-900/30", text: "text-green-700 dark:text-green-400", icon: mdiCheckCircle },
    working: { bg: "bg-blue-100 dark:bg-blue-900/30", text: "text-blue-700 dark:text-blue-400", icon: mdiClockOutline },
    completed: { bg: "bg-green-100 dark:bg-green-900/30", text: "text-green-700 dark:text-green-400", icon: mdiCheckCircle },
    idle: { bg: "bg-gray-100 dark:bg-gray-700/30", text: "text-gray-500 dark:text-gray-400", icon: mdiCircleOutline },
    queued: { bg: "bg-amber-100 dark:bg-amber-900/30", text: "text-amber-700 dark:text-amber-400", icon: mdiClockOutline },
    error: { bg: "bg-red-100 dark:bg-red-900/30", text: "text-red-700 dark:text-red-400", icon: mdiAlertCircle },
  };
  const s = map[status] || map.idle;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${s.bg} ${s.text}`}>
      <Icon path={s.icon} size="14" />
      {status}
    </span>
  );
}

function timeAgo(ts: string | null): string {
  if (!ts) return "never";
  const diff = Date.now() - new Date(ts).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

/* ── Stat pill ────────────────────────────────────────────────── */
function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-700 last:border-0">
      <span className="text-xs text-gray-500 dark:text-gray-400">{label}</span>
      <span className={`text-sm font-medium ${color || ""}`}>{value}</span>
    </div>
  );
}

function capitalize(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** The agent bus can hand back strings carrying an em dash (a health note, a
 *  model class). This dashboard renders no em dashes, so normalise them to a
 *  hyphen at the render boundary. Built via fromCharCode so the source file
 *  itself stays em-dash-free. */
const EM_DASH = String.fromCharCode(0x2014);
function stripEmDash(s: string): string {
  return s.split(EM_DASH).join("-");
}

/**
 * Health values arrive loosely typed from the agent bus: scalars, string
 * arrays (unit lists) and small objects (e.g. `restart_policy`). `String(val)`
 * turned the objects into the literal "[object Object]"; flatten them to a
 * readable "key: value · key: value" instead.
 */
function formatHealthValue(val: unknown): string {
  if (val === null || val === undefined) return "-";
  if (Array.isArray(val)) return val.map((v) => formatHealthValue(v)).join(", ");
  if (typeof val === "object") {
    return Object.entries(val as Record<string, unknown>)
      .map(([k, v]) => `${k.replace(/_/g, " ")}: ${formatHealthValue(v)}`)
      .join(" · ");
  }
  return stripEmDash(String(val));
}

/* ── Bus status strip ─────────────────────────────────────────── */
function BusStrip({ bus }: { bus: AgentBus }) {
  return (
    <CardBox className="mb-6">
      <div className="p-4 flex flex-wrap items-center gap-x-6 gap-y-3">
        <div className="flex items-center gap-2">
          <Icon path={mdiLan} size="18" className="text-[var(--puff-primary)]" />
          <span className="font-bold text-sm">Message Bus</span>
        </div>
        <Stat2 label="Agents" value={String(bus.agent_count)} />
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500 dark:text-gray-400">Bus</span>
          <span
            className={`inline-flex items-center gap-1 text-xs font-medium ${
              bus.enabled ? "text-green-500" : "text-gray-400"
            }`}
          >
            <Icon path={bus.enabled ? mdiCheckCircle : mdiCircleOutline} size="14" />
            {bus.enabled ? "enabled" : "disabled"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500 dark:text-gray-400">Reviewer</span>
          <span className="text-sm font-medium">{capitalize(bus.reviewer)}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500 dark:text-gray-400">Reviewer wired</span>
          <span
            className={`inline-flex items-center gap-1 text-xs font-medium ${
              bus.reviewer_wired ? "text-green-500" : "text-red-500"
            }`}
          >
            <Icon path={bus.reviewer_wired ? mdiCheckCircle : mdiCloseCircle} size="14" />
            {bus.reviewer_wired ? "wired" : "unwired"}
          </span>
        </div>
      </div>
    </CardBox>
  );
}

/* ── Inline stat (label + value on one line) ──────────────────── */
function Stat2({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-gray-500 dark:text-gray-400">{label}</span>
      <span className="text-sm font-medium">{value}</span>
    </div>
  );
}

/** Legacy activity-log ids → the product-team agent each became.
 *  The activity feed still writes the embryo names, so without this the six
 *  agents would answer to one name in the roster and another in the feed. */
const LEGACY_ALIAS: Record<string, string> = {
  cortex: "concierge",
  bridge: "steward",
  sentinel: "sentinel",
};

/** What to call an agent on screen: the build's display name if the backend
 *  sent one, then the built-in label, then the raw id. Never renders blank. */
function agentLabel(id: string, team: TeamAgent[]): string {
  const canonical = LEGACY_ALIAS[id] || id;
  const row = team.find((t) => t.name === canonical);
  return (
    row?.display_name ||
    AGENT_META[canonical]?.label ||
    AGENT_META[id]?.label ||
    id
  );
}

/** Visual meta (icon/colour/border) for an id, resolving legacy embryo ids
 *  (cortex/bridge) to the product-team agent they became - so the activity
 *  feed borders match the roster cards instead of showing a stale colour. */
function agentMetaFor(id: string) {
  const canonical = LEGACY_ALIAS[id] || id;
  return (
    AGENT_META[canonical] ||
    AGENT_META[id] || {
      label: id,
      role: "",
      icon: mdiAccountGroup,
      color: "from-gray-400 to-gray-600",
      border: "border-l-gray-400",
    }
  );
}

/** Human wording + colour for the feed's raw event ids. Anything unmapped is
 *  rendered by replacing underscores with spaces, so a new event type still
 *  reads cleanly. */
const EVENT_META: Record<string, { label: string; color: string }> = {
  task_started: { label: "started", color: "#3b82f6" },
  task_queued: { label: "queued", color: "#f59e0b" },
  task_completed: { label: "completed", color: "#22c55e" },
  task_unhandled: { label: "no handler", color: "#9ca3af" },
  task_rerouted: { label: "rerouted", color: "#8b5cf6" },
  consent_requested: { label: "consent asked", color: "#f97316" },
  consent_resolved: { label: "consent resolved", color: "#22c55e" },
};

/* ── Capability status styling ────────────────────────────────── */
const CAP_STATUS: Record<string, { color: string; label: string }> = {
  live: { color: "#22c55e", label: "live" },
  "needs-auth": { color: "#f59e0b", label: "needs setup" },
  offline: { color: "#9ca3af", label: "offline" },
  planned: { color: "#64748b", label: "planned" },
};

/** One capability line in an agent's "Handles" list: a status dot, the human
 *  label, a write badge when it can change state, and a note on hover. */
function CapabilityRow({ cap }: { cap: AgentCapability }) {
  const s = CAP_STATUS[cap.status] || CAP_STATUS.live;
  const writes = cap.access !== "read";
  return (
    <div className="flex items-center gap-2 py-1" title={cap.note || cap.label}>
      <span className="h-1.5 w-1.5 flex-none rounded-full" style={{ background: s.color }} />
      <span className="flex-1 truncate text-xs" style={{ color: "var(--text2)" }}>
        {cap.label}
      </span>
      {writes && (
        <span
          className="inline-flex flex-none items-center justify-center rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase leading-none tracking-wide"
          style={{ background: "var(--chip)", color: "var(--lav)" }}
        >
          {cap.access}
        </span>
      )}
      {cap.status !== "live" && (
        <span className="flex-none text-[10px] font-medium" style={{ color: s.color }}>
          {s.label}
        </span>
      )}
    </div>
  );
}

/* ── Team agent card ──────────────────────────────────────────── */
function TeamAgentCard({ agent }: { agent: TeamAgent }) {
  const meta = AGENT_META[agent.name] || AGENT_META.cortex;
  const caps = agent.capabilities || [];
  // Health keys already shown elsewhere or rendered as the ok indicator.
  const extraHealth = Object.entries(agent.health || {}).filter(
    ([key]) => !["agent", "ok", "scope"].includes(key)
  );
  return (
    <CardBox className="relative overflow-hidden">
      <div className={`absolute top-0 left-0 right-0 h-1 bg-gradient-to-r ${meta.color}`} />
      <div className="p-4">
        {/* ── Header: name + role + live status ── */}
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-3">
            <div
              className={`w-10 h-10 rounded-lg bg-gradient-to-br ${meta.color} flex items-center justify-center`}
            >
              <Icon path={meta.icon} size="20" className="text-white" />
            </div>
            <div>
              <h3 className="font-bold text-lg leading-tight">
                {agent.display_name || meta.label || capitalize(agent.name)}
              </h3>
              <p className="text-xs text-gray-500 dark:text-gray-400">{meta.role}</p>
            </div>
          </div>
          <StatusBadge status={agent.status} />
        </div>

        {/* ── Identity chips: scope · workflow (lineage dropped - the
              "← Cortex/Bridge" tags re-introduced the retired names). Chips use
              inline-flex so the label sits centred in the pill. Theme tokens,
              not Tailwind colour pairs: the shell keeps the `dark` class on
              <html> in BOTH palettes, so `dark:text-*` stayed invisible in day
              mode. */}
        <div className="flex flex-wrap items-center gap-1.5 mb-3">
          <span
            className="inline-flex max-w-full flex-none items-center justify-center whitespace-nowrap rounded-full px-2.5 py-1 text-xs leading-none"
            style={{ background: "var(--chip)", color: "var(--text2)" }}
          >
            {agent.scope}
          </span>
          {agent.workflow && (
            <span
              className="inline-flex max-w-full flex-none items-center justify-center whitespace-nowrap rounded-full px-2.5 py-1 font-mono text-xs leading-none"
              style={{ background: "var(--chip)", color: "var(--text2)" }}
            >
              {agent.workflow}
            </span>
          )}
        </div>

        {/* ── What it's doing right now ── */}
        <div
          className="mb-3 flex items-start gap-2 rounded-lg px-3 py-2"
          style={{ background: "var(--card2)" }}
        >
          <Icon
            path={agent.task ? mdiProgressClock : mdiSleep}
            size="16"
            className={agent.task ? "text-[var(--puff-primary)] mt-0.5" : "text-gray-400 mt-0.5"}
          />
          <div className="min-w-0">
            <p className="text-[10px] uppercase tracking-wide text-gray-400">
              {agent.task ? "Currently" : "Status"}
            </p>
            <p className="text-sm truncate">
              {agent.task || `Idle - last active ${timeAgo(agent.timestamp)}`}
            </p>
          </div>
        </div>

        {/* ── What it handles (from the capability registry) ── */}
        {caps.length > 0 && (
          <div>
            <p className="text-[10px] uppercase tracking-wide text-gray-400 mb-0.5">
              Handles · {caps.length}
            </p>
            <div className="divide-y" style={{ borderColor: "var(--line)" }}>
              {caps.map((c) => (
                <CapabilityRow key={c.id} cap={c} />
              ))}
            </div>
          </div>
        )}

        {agent.model_class && (
          <p className="text-[11px] text-gray-400 mt-3">
            {stripEmDash(agent.model_class).replace(/\s*\+\s*/g, ", ")}
          </p>
        )}

        {/* ── Health footer ── */}
        <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={`inline-flex items-center gap-1 text-xs font-medium ${
                agent.health?.ok ? "text-green-500" : "text-red-500"
              }`}
            >
              <Icon path={agent.health?.ok ? mdiCheckCircle : mdiCloseCircle} size="14" />
              {agent.health?.ok ? "ok" : "fault"}
            </span>
            {extraHealth.map(([key, val]) => {
              const isBool = typeof val === "boolean";
              const good = isBool ? val : true;
              return (
                <span
                  key={key}
                  className="inline-flex max-w-full items-center gap-1 rounded-full px-2 py-0.5 text-xs"
                  style={
                    isBool && good
                      ? { background: "var(--chip)", color: "var(--mint)" }
                      : { background: "var(--chip)", color: "var(--text2)" }
                  }
                >
                  <span className="whitespace-nowrap">{key.replace(/_/g, " ")}</span>
                  {!isBool && (
                    <span className="max-w-[16rem] truncate font-medium" title={formatHealthValue(val)}>
                      {formatHealthValue(val)}
                    </span>
                  )}
                </span>
              );
            })}
          </div>
        </div>
      </div>
    </CardBox>
  );
}

/* ── Result Panel - renders structured data per action type ──── */
function ResultPanel({
  taskResult,
  onClose,
  onExecuteAction,
}: {
  taskResult: { routed_to: string; status: string; result?: any } | null;
  onClose: () => void;
  onExecuteAction: (task: string, agent: string, params: Record<string, any>) => void;
}) {
  const [selectedSuggestions, setSelectedSuggestions] = useState<number[]>([]);

  if (!taskResult || !taskResult.result) return null;
  const r = taskResult.result;
  const data = r.result || {};
  const action: string = r.action || "";
  const agent = taskResult.routed_to;
  const meta = AGENT_META[agent] || AGENT_META.cortex;

  return (
    <CardBox className="mb-6 relative overflow-hidden">
      <div className={`absolute top-0 left-0 right-0 h-1 bg-gradient-to-r ${meta.color}`} />
      <div className="p-5">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <div className={`w-7 h-7 rounded-md bg-gradient-to-br ${meta.color} flex items-center justify-center`}>
              <Icon path={meta.icon} size="16" className="text-white" />
            </div>
            <h3 className="font-bold text-base">{meta.label}</h3>
            <span className="text-xs text-gray-400">{action}</span>
          </div>
          <button onClick={onClose} className="p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded">
            <Icon path={mdiClose} size="18" className="text-gray-400" />
          </button>
        </div>

        {/* ── System Health ──────────────────────────────────── */}
        {action === "system-health" && (
          <div>
            <p className="text-sm font-medium mb-3">{data.summary}</p>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
              {Object.entries(data.services || {}).map(([name, info]: [string, any]) => (
                <div key={name} className="rounded-lg border border-gray-200 dark:border-gray-700 p-3">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-medium capitalize">{name.replace(/_/g, " ")}</span>
                    <span className={`text-xs font-bold ${info.status === "online" || info.status === "available" ? "text-green-500" : "text-red-500"}`}>
                      {info.status}
                    </span>
                  </div>
                  {info.models && <p className="text-xs text-gray-400 truncate">{info.models.join(", ")}</p>}
                  {info.uptime && <p className="text-xs text-gray-400">{info.uptime}</p>}
                  {info.message && <p className="text-xs text-gray-400 truncate">{info.message}</p>}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Security Audit ─────────────────────────────────── */}
        {action === "security-audit" && (
          <div>
            <div className="flex items-center gap-4 mb-4">
              <div className="text-3xl font-bold">{data.security_score}</div>
              <div className="text-sm text-gray-500">Security Score</div>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
              <div className="text-center p-2 rounded-lg bg-gray-50 dark:bg-gray-800">
                <div className="text-lg font-bold">{data.locks}</div>
                <div className="text-xs text-gray-500">Locks</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-gray-50 dark:bg-gray-800">
                <div className="text-lg font-bold">{data.doors}</div>
                <div className="text-xs text-gray-500">Doors</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-gray-50 dark:bg-gray-800">
                <div className="text-lg font-bold">{data.cameras}</div>
                <div className="text-xs text-gray-500">Cameras</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-gray-50 dark:bg-gray-800">
                <div className="text-lg font-bold">{data.motion_sensors}</div>
                <div className="text-xs text-gray-500">Motion</div>
              </div>
            </div>
            {data.issues && (
              <div>
                <h4 className="text-xs font-medium text-gray-500 mb-2">Issues</h4>
                {data.issues.map((issue: string, i: number) => (
                  <p key={i} className={`text-sm py-1 ${issue.includes("All clear") ? "text-green-600" : "text-orange-600"}`}>{issue}</p>
                ))}
              </div>
            )}
            {data.issues && !data.issues.every((i: string) => i.includes("All clear")) && (
              <button
                onClick={() => onExecuteAction(
                  "Fix security issues - lock all locks, close doors",
                  "sentinel",
                  { actions: ["lock_all", "close_doors", "arm_alarm"] }
                )}
                className="mt-4 px-4 py-2 rounded-lg text-sm font-medium bg-orange-500 text-white hover:bg-orange-600 transition-colors"
              >
                🔒 Fix All Security Issues
              </button>
            )}
          </div>
        )}

        {/* ── Device Analytics ───────────────────────────────── */}
        {action === "device-analytics" && (
          <div>
            <Stat label="Total Entities" value={String(data.total_entities)} />
            <Stat label="Unavailable" value={String(data.unavailable_count)} color={data.unavailable_count > 0 ? "text-orange-500" : "text-green-500"} />
            {data.low_battery?.length > 0 && (
              <div className="mt-3">
                <h4 className="text-xs font-medium text-gray-500 mb-1">Low Battery</h4>
                {data.low_battery.map((d: any, i: number) => (
                  <p key={i} className="text-sm text-orange-500">{d.name}: {d.battery}</p>
                ))}
              </div>
            )}
            <div className="mt-3">
              <h4 className="text-xs font-medium text-gray-500 mb-1">By Domain</h4>
              <div className="grid grid-cols-2 gap-1">
                {Object.entries(data.by_domain || {}).map(([domain, count]: [string, any]) => (
                  <div key={domain} className="flex justify-between text-xs py-0.5">
                    <span className="text-gray-600 dark:text-gray-300">{domain}</span>
                    <span className="font-medium">{count}</span>
                  </div>
                ))}
              </div>
            </div>
            {data.unavailable_count > 0 && data.unavailable_devices?.length > 0 && (
              <div className="mt-3">
                <h4 className="text-xs font-medium text-gray-500 mb-1">Unavailable Devices</h4>
                {data.unavailable_devices.map((name: string, i: number) => (
                  <p key={i} className="text-xs text-gray-400">{name}</p>
                ))}
              </div>
            )}
          </div>
        )}

        {/* ── Suggest Automations ────────────────────────────── */}
        {action === "suggest-automations" && (
          <div>
            <Stat label="Existing Automations" value={String(data.existing_automations)} />
            <Stat label="New Suggestions" value={String(data.suggestions_count)} />
            <div className="mt-3 space-y-3">
              {(data.suggestions || []).map((s: any, i: number) => (
                <div key={i} className={`rounded-lg border p-3 cursor-pointer transition-colors ${
                  selectedSuggestions.includes(i)
                    ? "border-cyan-400 bg-cyan-50 dark:bg-cyan-900/20"
                    : "border-gray-200 dark:border-gray-700 hover:border-gray-300"
                }`}
                  onClick={() => setSelectedSuggestions(prev =>
                    prev.includes(i) ? prev.filter(x => x !== i) : [...prev, i]
                  )}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <input
                      type="checkbox"
                      checked={selectedSuggestions.includes(i)}
                      onChange={() => {}}
                      className="rounded text-cyan-500 focus:ring-cyan-400"
                    />
                    <span className="text-sm font-medium flex-1">{s.name}</span>
                    <span className={`text-xs px-2 py-0.5 rounded-full ${
                      s.priority === "high" ? "bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400"
                      : s.priority === "medium" ? "bg-yellow-100 text-yellow-600 dark:bg-yellow-900/30 dark:text-yellow-400"
                      : "bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400"
                    }`}>
                      {s.priority}
                    </span>
                  </div>
                  <p className="text-xs text-gray-500 mb-1 ml-6"><strong>Trigger:</strong> {s.trigger}</p>
                  <p className="text-xs text-gray-500 mb-1 ml-6"><strong>Action:</strong> {s.action}</p>
                  <p className="text-xs text-gray-400 font-mono ml-6">{s.entities}</p>
                </div>
              ))}
            </div>
            {(data.suggestions || []).length > 0 && (
              <div className="mt-4 flex items-center gap-3">
                <button
                  onClick={() => {
                    const selected = selectedSuggestions.map((i: number) => data.suggestions[i]).filter(Boolean);
                    if (selected.length > 0) {
                      onExecuteAction(
                        `Create ${selected.length} automation(s): ${selected.map((s: any) => s.name).join(', ')}`,
                        'bridge',
                        { suggestions: selected }
                      );
                    }
                  }}
                  disabled={selectedSuggestions.length === 0}
                  className="px-4 py-2 rounded-lg text-sm font-medium bg-cyan-500 text-white hover:bg-cyan-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  Create {selectedSuggestions.length > 0 ? `${selectedSuggestions.length} Selected` : "Selected"} Automation{selectedSuggestions.length !== 1 ? "s" : ""}
                </button>
                <button
                  onClick={() => {
                    const all = data.suggestions || [];
                    setSelectedSuggestions(all.map((_: any, i: number) => i));
                  }}
                  className="px-3 py-2 rounded-lg text-xs text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                >
                  Select All
                </button>
                {selectedSuggestions.length > 0 && (
                  <button
                    onClick={() => setSelectedSuggestions([])}
                    className="px-3 py-2 rounded-lg text-xs text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                  >
                    Clear
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {/* ── Environment Summary ────────────────────────────── */}
        {action === "environment-summary" && (
          <div>
            <Stat label="Lights" value={data.lights || ""} />
            {(data.lights_on || []).length > 0 && (
              <div className="mt-2 mb-3">
                <h4 className="text-xs font-medium text-gray-500 mb-1">Lights On</h4>
                <div className="flex flex-wrap gap-1">
                  {data.lights_on.map((name: string, i: number) => (
                    <span key={i} className="text-xs bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400 px-2 py-0.5 rounded-full">{name}</span>
                  ))}
                </div>
              </div>
            )}
            {Object.entries(data.temperatures || {}).length > 0 && (
              <div className="mt-2">
                <h4 className="text-xs font-medium text-gray-500 mb-1">Temperatures</h4>
                {Object.entries(data.temperatures).map(([name, val]: [string, any]) => (
                  <Stat key={name} label={name} value={`${val}°`} />
                ))}
              </div>
            )}
            {(data.people || []).length > 0 && (
              <div className="mt-2">
                <h4 className="text-xs font-medium text-gray-500 mb-1">People</h4>
                {data.people.map((p: any, i: number) => (
                  <Stat key={i} label={p.name} value={p.state} color={p.state === "home" ? "text-green-500" : "text-gray-400"} />
                ))}
              </div>
            )}
            {(data.climate || []).length > 0 && (
              <div className="mt-2">
                <h4 className="text-xs font-medium text-gray-500 mb-1">Climate</h4>
                {data.climate.map((c: any, i: number) => (
                  <Stat key={i} label={c.name} value={`${c.mode} - ${c.current_temp || "?"}° → ${c.target_temp || "?"}°`} />
                ))}
              </div>
            )}
          </div>
        )}

        {/* ── Discover Devices ───────────────────────────────── */}
        {action === "discover-devices" && (
          <div>
            <Stat label="Total Entities" value={String(data.total_entities)} />
            <Stat label="Needs Attention" value={String(data.needs_attention)} color={data.needs_attention > 0 ? "text-orange-500" : "text-green-500"} />
            <div className="mt-3">
              <h4 className="text-xs font-medium text-gray-500 mb-1">By Domain</h4>
              <div className="grid grid-cols-2 gap-1">
                {Object.entries(data.by_domain || {}).map(([domain, count]: [string, any]) => (
                  <div key={domain} className="flex justify-between text-xs py-0.5">
                    <span className="text-gray-600 dark:text-gray-300">{domain}</span>
                    <span className="font-medium">{count}</span>
                  </div>
                ))}
              </div>
            </div>
            {Object.entries(data.manufacturers || {}).length > 0 && (
              <div className="mt-3">
                <h4 className="text-xs font-medium text-gray-500 mb-1">Manufacturers</h4>
                {Object.entries(data.manufacturers).map(([mfr, count]: [string, any]) => (
                  <Stat key={mfr} label={mfr} value={String(count)} />
                ))}
              </div>
            )}
          </div>
        )}

        {/* ── List Workflows ─────────────────────────────────── */}
        {action === "list-workflows" && (
          <div>
            <div className="flex gap-4 mb-3">
              <Stat label="Total" value={String(data.total)} />
              <Stat label="Active" value={String(data.active)} color="text-green-500" />
              <Stat label="Inactive" value={String(data.inactive)} color="text-gray-400" />
            </div>
            <div className="space-y-1 max-h-64 overflow-y-auto">
              {(data.workflows || []).map((w: any) => (
                <div key={w.id} className="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-700 last:border-0">
                  <span className="text-sm">{w.name}</span>
                  <span className={`text-xs font-medium ${w.active ? "text-green-500" : "text-gray-400"}`}>
                    {w.active ? "active" : "inactive"}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Privacy Audit ──────────────────────────────────── */}
        {action === "privacy-audit" && (
          <div>
            <div className="grid grid-cols-3 gap-3 mb-4">
              <div className="text-center p-2 rounded-lg bg-red-50 dark:bg-red-900/20">
                <div className="text-lg font-bold text-red-600">{data.alerts}</div>
                <div className="text-xs text-gray-500">Alerts</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-yellow-50 dark:bg-yellow-900/20">
                <div className="text-lg font-bold text-yellow-600">{data.warnings}</div>
                <div className="text-xs text-gray-500">Warnings</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-blue-50 dark:bg-blue-900/20">
                <div className="text-lg font-bold text-blue-600">{data.info}</div>
                <div className="text-xs text-gray-500">Info</div>
              </div>
            </div>
            <div className="space-y-1">
              {(data.issues || []).map((issue: string, i: number) => {
                const color = issue.startsWith("ALERT") ? "text-red-600" : issue.startsWith("WARN") ? "text-yellow-600" : issue.startsWith("INFO") ? "text-blue-600" : "text-green-600";
                return <p key={i} className={`text-sm ${color}`}>{issue}</p>;
              })}
            </div>
            <div className="mt-3">
              <h4 className="text-xs font-medium text-gray-500 mb-1">Areas Checked</h4>
              <div className="flex flex-wrap gap-1">
                {(data.areas_checked || []).map((area: string, i: number) => (
                  <span key={i} className="text-xs bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 px-2 py-0.5 rounded-full">{area}</span>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* ── Network Inventory ──────────────────────────────── */}
        {action === "network-inventory" && (
          <div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
              <div className="text-center p-2 rounded-lg bg-gray-50 dark:bg-gray-800">
                <div className="text-lg font-bold">{data.total_devices}</div>
                <div className="text-xs text-gray-500">Total</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-green-50 dark:bg-green-900/20">
                <div className="text-lg font-bold text-green-600">{data.home}</div>
                <div className="text-xs text-gray-500">Home</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-gray-50 dark:bg-gray-800">
                <div className="text-lg font-bold text-gray-400">{data.away}</div>
                <div className="text-xs text-gray-500">Away</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-orange-50 dark:bg-orange-900/20">
                <div className="text-lg font-bold text-orange-500">{data.unidentified}</div>
                <div className="text-xs text-gray-500">Unidentified</div>
              </div>
            </div>
            <div className="space-y-1 max-h-48 overflow-y-auto">
              {(data.devices || []).map((d: any, i: number) => (
                <div key={i} className="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-700 last:border-0">
                  <div>
                    <span className="text-sm">{d.name}</span>
                    {d.ip && <span className="text-xs text-gray-400 ml-2">{d.ip}</span>}
                  </div>
                  <span className={`text-xs font-medium ${d.home ? "text-green-500" : "text-gray-400"}`}>
                    {d.state}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Exposure Check ─────────────────────────────────── */}
        {action === "check-exposure" && (
          <div>
            <div className="flex items-center gap-3 mb-4">
              <span className="text-sm font-medium">Risk Level:</span>
              <span className={`text-sm font-bold px-3 py-1 rounded-full ${
                data.risk_level === "high" ? "bg-red-100 text-red-600 dark:bg-red-900/30" :
                data.risk_level === "medium" ? "bg-yellow-100 text-yellow-600 dark:bg-yellow-900/30" :
                "bg-green-100 text-green-600 dark:bg-green-900/30"
              }`}>
                {data.risk_level?.toUpperCase()}
              </span>
              <span className="text-xs text-gray-400">{data.warnings} warning(s)</span>
            </div>
            <div className="space-y-2">
              {Object.entries(data.checks || {}).map(([service, status]: [string, any]) => (
                <div key={service} className="flex items-center justify-between py-2 border-b border-gray-100 dark:border-gray-700 last:border-0">
                  <span className="text-sm capitalize">{service.replace(/_/g, " ")}</span>
                  <span className={`text-xs ${String(status).includes("WARNING") ? "text-orange-500" : String(status).includes("good") ? "text-green-500" : "text-gray-500"}`}>
                    {String(status)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Good Morning / Leaving Home / Toggle Lights / Generic ── */}
        {["good-morning", "leaving-home", "control-lights"].includes(action) && (
          <div>
            {Object.entries(data).map(([key, val]: [string, any]) => (
              <Stat key={key} label={key.replace(/_/g, " ")} value={String(val)} />
            ))}
          </div>
        )}

        {/* ── Create Automations Result ──────────────────────── */}
        {action === "create-automations" && (
          <div>
            <div className="grid grid-cols-3 gap-3 mb-4">
              <div className="text-center p-2 rounded-lg bg-green-50 dark:bg-green-900/20">
                <div className="text-lg font-bold text-green-600">{data.created}</div>
                <div className="text-xs text-gray-500">Created</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-red-50 dark:bg-red-900/20">
                <div className="text-lg font-bold text-red-600">{data.failed}</div>
                <div className="text-xs text-gray-500">Failed</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-gray-50 dark:bg-gray-800">
                <div className="text-lg font-bold">{data.total}</div>
                <div className="text-xs text-gray-500">Total</div>
              </div>
            </div>
            {(data.details || []).map((d: any, i: number) => (
              <div key={i} className="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-700 last:border-0">
                <span className="text-sm">{d.name}</span>
                <span className="text-xs text-green-500 font-medium">{d.status} ({d.triggers} trigger, {d.actions} action)</span>
              </div>
            ))}
            {(data.errors || []).map((e: any, i: number) => (
              <div key={`err-${i}`} className="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-700 last:border-0">
                <span className="text-sm text-red-500">{e.name}</span>
                <span className="text-xs text-red-400 truncate max-w-[200px]">{e.error}</span>
              </div>
            ))}
          </div>
        )}

        {/* ── Fix Security Result ──────────────────────────────── */}
        {action === "fix-security" && (
          <div>
            <div className="grid grid-cols-2 gap-3 mb-4">
              <div className="text-center p-2 rounded-lg bg-green-50 dark:bg-green-900/20">
                <div className="text-lg font-bold text-green-600">{data.fixes_applied}</div>
                <div className="text-xs text-gray-500">Fixed</div>
              </div>
              <div className="text-center p-2 rounded-lg bg-red-50 dark:bg-red-900/20">
                <div className="text-lg font-bold text-red-600">{data.fixes_failed}</div>
                <div className="text-xs text-gray-500">Failed</div>
              </div>
            </div>
            {data.new_security_score && (
              <div className="mb-3 text-center">
                <span className="text-sm text-gray-500">New Security Score: </span>
                <span className="text-lg font-bold text-green-600">{data.new_security_score}</span>
              </div>
            )}
            {(data.details || []).map((d: any, i: number) => (
              <div key={i} className="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-700 last:border-0">
                <span className="text-sm">{d.name}</span>
                <span className="text-xs text-green-500 font-medium">{d.action}</span>
              </div>
            ))}
            {(data.errors || []).map((e: any, i: number) => (
              <div key={`err-${i}`} className="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-700 last:border-0">
                <span className="text-sm text-red-500">{e.entity}</span>
                <span className="text-xs text-red-400">{e.error}</span>
              </div>
            ))}
          </div>
        )}

        {/* ── Fallback for unknown actions ────────────────────── */}
        {!["system-health", "security-audit", "device-analytics", "suggest-automations",
          "environment-summary", "discover-devices", "list-workflows", "privacy-audit",
          "network-inventory", "check-exposure", "good-morning", "leaving-home", "control-lights",
          "create-automations", "fix-security",
        ].includes(action) && data && (
          <div>
            {Object.entries(data).map(([key, val]: [string, any]) => (
              <Stat key={key} label={key} value={typeof val === "object" ? JSON.stringify(val) : String(val)} />
            ))}
          </div>
        )}
      </div>
    </CardBox>
  );
}

/* ── Absorption tabs - design language pill row (Team | Workflows) ── */
function AbsorbTabs<T extends string>({
  tabs,
  active,
  onSelect,
}: {
  tabs: { key: T; label: string }[];
  active: T;
  onSelect: (key: T) => void;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-center gap-3">
      {tabs.map((t) => {
        const isActive = t.key === active;
        return (
          <button
            key={t.key}
            type="button"
            onClick={() => onSelect(t.key)}
            className="rounded-full px-6 py-3 transition hover:scale-[1.02]"
            style={
              isActive
                ? {
                    background:
                      "linear-gradient(140deg, var(--pink), var(--magenta))",
                    color: "#fff",
                    font: "700 15px var(--font-nunito), sans-serif",
                    boxShadow: "0 10px 26px var(--glow1)",
                    border: "1px solid transparent",
                  }
                : {
                    background: "var(--card2)",
                    border: "1px solid var(--line)",
                    color: "var(--text2)",
                    font: "700 15px var(--font-nunito), sans-serif",
                    backdropFilter: "blur(14px)",
                    WebkitBackdropFilter: "blur(14px)",
                  }
            }
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/* ── Page component ───────────────────────────────────────────── */
export default function AgentsPage() {
  // Absorption tab: Team (this page) | Workflows (embedded page)
  const [pageTab, setPageTab] = useState<"team" | "workflows">("team");

  // Deep link: /dashboard/agents?tab=workflows. Read after mount (not via
  // useSearchParams) to keep the page prerender/Suspense-free.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("tab") === "workflows") {
      setPageTab("workflows");
    }
  }, []);
  const [agents, setAgents] = useState<AgentStatus[]>([]);
  const [team, setTeam] = useState<TeamAgent[]>([]);
  const [bus, setBus] = useState<AgentBus | null>(null);
  const [activity, setActivity] = useState<AgentActivity[]>([]);
  const [loading, setLoading] = useState(true);
  const [taskText, setTaskText] = useState("");
  const [targetAgent, setTargetAgent] = useState("auto");
  const [priority, setPriority] = useState("medium");
  const [sending, setSending] = useState(false);
  const [taskResult, setTaskResult] = useState<any>(null);

  const refresh = useCallback(async () => {
    try {
      const [statusRes, activityRes] = await Promise.all([
        getAgentStatuses(),
        getAgentActivity(30),
      ]);
      setAgents(statusRes.agents);
      setTeam(statusRes.team ?? []);
      setBus(statusRes.bus ?? null);
      setActivity(activityRes.activity);
    } catch {
      // backend may be offline
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  const executeTask = async (text: string, agent: string, prio: string, params?: Record<string, any>) => {
    setSending(true);
    setTaskResult(null);
    try {
      const res = await sendAgentTask(text, agent, prio, params);
      setTaskResult(res.task);
      setTaskText("");
      refresh();
    } catch (err: any) {
      setTaskResult({ routed_to: "system", status: "error", result: { action: "error", result: { error: err.message } } });
    } finally {
      setSending(false);
    }
  };

  const handleSendTask = () => {
    if (!taskText.trim() || sending) return;
    executeTask(taskText, targetAgent, priority);
  };

  const handleQuickAction = (action: (typeof QUICK_ACTIONS)[0]) => {
    setTargetAgent(action.agent);
    setTaskText(action.task);
    executeTask(action.task, action.agent, "medium");
  };

  const absorbTabs = (
    <AbsorbTabs
      tabs={[
        { key: "team" as const, label: "Team" },
        { key: "workflows" as const, label: "Workflows" },
      ]}
      active={pageTab}
      onSelect={(key) => setPageTab(key)}
    />
  );

  // Absorbed page rendered in place - /dashboard/workflows stays alive too.
  if (pageTab === "workflows") {
    return (
      <>
        <div className={`px-6 pt-6 ${containerMaxW}`}>{absorbTabs}</div>
        <WorkflowsPage />
      </>
    );
  }

  return (
    <SectionMain>
      {/* Absorption tabs: Team | Workflows */}
      {absorbTabs}

      <SectionTitleLineWithButton
        icon={mdiAccountGroup}
        title="Agent Team"
        main
      >
        <Button
          icon={mdiRefresh}
          color="whiteDark"
          onClick={refresh}
          small
          roundedFull
        />
      </SectionTitleLineWithButton>

      {team.length > 0 ? (
        <>
          {/* ── Bus status strip ─────────────────────────────────── */}
          {bus && <BusStrip bus={bus} />}

          {/* ── Names: what this household calls them ───────────── */}
          <AgentNamesPanel team={team} />

          {/* ── Product Team Agents ──────────────────────────────── */}
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6 mb-6">
            {team.map((a) => (
              <TeamAgentCard key={a.name} agent={a} />
            ))}
          </div>
        </>
      ) : (
        /* ── Legacy fallback (API unreachable / no team reported) ── */
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-6">
          {(agents.length > 0
            ? agents
            : [
                { agent: "cortex", status: "idle", task: null, timestamp: null },
                { agent: "bridge", status: "idle", task: null, timestamp: null },
                { agent: "sentinel", status: "idle", task: null, timestamp: null },
              ]
          ).map((a) => {
            const meta = AGENT_META[a.agent] || AGENT_META.cortex;
            return (
              <CardBox key={a.agent} className="relative overflow-hidden">
                <div className={`absolute top-0 left-0 right-0 h-1 bg-gradient-to-r ${meta.color}`} />
                <div className="p-4">
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-3">
                      <div className={`w-10 h-10 rounded-lg bg-gradient-to-br ${meta.color} flex items-center justify-center`}>
                        <Icon path={meta.icon} size="20" className="text-white" />
                      </div>
                      <div>
                        <h3 className="font-bold text-lg">{meta.label}</h3>
                        <p className="text-xs text-gray-500 dark:text-gray-400">{meta.role}</p>
                      </div>
                    </div>
                    <StatusBadge status={a.status} />
                  </div>
                  {a.task && (
                    <p className="text-sm text-gray-600 dark:text-gray-300 mt-2 truncate">{a.task}</p>
                  )}
                  <p className="text-xs text-gray-400 mt-2">Last active: {timeAgo(a.timestamp)}</p>
                </div>
              </CardBox>
            );
          })}
        </div>
      )}

      {/* ── Task Input ─────────────────────────────────────────── */}
      <CardBox className="mb-6">
        <div className="p-4">
          <h3 className="font-bold mb-3">Send Task</h3>
          <div className="flex flex-col sm:flex-row gap-3">
            <input
              /* Same legacy trap as the knowledge search: main.css forces
                 `.dark input[type="text"]` to a slate box, which reads as a
                 navy slab on the day palette. kb-viewer-search is the themed
                 glass field (css/_theme.css). */
              type="text"
              className="kb-viewer-search flex-1 text-sm"
              placeholder="Describe a task for the agent team..."
              value={taskText}
              onChange={(e) => setTaskText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSendTask()}
            />
            <select
              className="px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm"
              value={targetAgent}
              onChange={(e) => setTargetAgent(e.target.value)}
            >
              <option value="auto">Auto-Route</option>
              {(team.length > 0
                ? team.map((t) => ({ value: t.name, label: t.display_name || t.name }))
                : [
                    { value: "concierge", label: "Concierge" },
                    { value: "steward", label: "Steward" },
                    { value: "sentinel", label: "Drawbridge" },
                  ]
              ).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <select
              className="px-3 py-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm"
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
            <Button
              icon={mdiSend}
              color="info"
              label={sending ? "Running..." : "Send"}
              onClick={handleSendTask}
              disabled={!taskText.trim() || sending}
              small
            />
          </div>
        </div>
      </CardBox>

      {/* ── Result Panel ───────────────────────────────────────── */}
      <ResultPanel
        taskResult={taskResult}
        onClose={() => setTaskResult(null)}
        onExecuteAction={(task, agent, params) => executeTask(task, agent, "medium", params)}
      />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* ── Activity Feed ──────────────────────────────────── */}
        <div className="lg:col-span-2">
          <CardBox>
            <div className="p-4">
              <h3 className="font-bold mb-3">Activity Feed</h3>
              <div className="space-y-2 max-h-96 overflow-y-auto">
                {activity.length === 0 && !loading && (
                  <p className="text-sm text-gray-400 py-8 text-center">
                    No activity yet. Send a task to get started.
                  </p>
                )}
                {activity.map((item, i) => {
                  const m = agentMetaFor(item.agent);
                  const ev = EVENT_META[item.event] || {
                    label: (item.event || "").replace(/_/g, " "),
                    color: "var(--text2)",
                  };
                  return (
                    <div key={`${item.timestamp}-${i}`} className={`border-l-4 ${m.border} pl-3 py-2`}>
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex min-w-0 items-center gap-2">
                          <span className="truncate text-sm font-medium">
                            {agentLabel(item.agent, team)}
                          </span>
                          <span
                            className="flex-none rounded-full px-1.5 py-0.5 text-[10px] font-semibold"
                            style={{ background: "var(--chip)", color: ev.color }}
                          >
                            {ev.label}
                          </span>
                        </div>
                        <span className="flex-none text-xs text-gray-400">{timeAgo(item.timestamp)}</span>
                      </div>
                      {item.task && (
                        <p className="mt-0.5 text-sm text-gray-600 dark:text-gray-300">{item.task}</p>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          </CardBox>
        </div>

        {/* ── Quick Actions ──────────────────────────────────── */}
        <div>
          <CardBox>
            <div className="p-4">
              <h3 className="font-bold mb-3">Quick Actions</h3>
              <div className="space-y-1 max-h-96 overflow-y-auto">
                {QUICK_ACTIONS.map((action) => {
                  const agentColor = AGENT_TEXT[action.agent] || "text-gray-500";
                  return (
                    <button
                      key={action.label}
                      className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-left hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors"
                      onClick={() => handleQuickAction(action)}
                      disabled={sending}
                    >
                      <Icon path={action.icon} size="18" className={agentColor} />
                      <span>{action.label}</span>
                      <span className={`ml-auto text-xs ${agentColor}`}>
                        {agentLabel(action.agent, team)}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          </CardBox>
        </div>
      </div>
    </SectionMain>
  );
}
