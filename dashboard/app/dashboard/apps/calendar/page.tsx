"use client";

import { useState, useCallback, useEffect, useMemo } from "react";
import {
  mdiCalendarMonth,
  mdiCheckCircle,
  mdiPill,
  mdiCurrencyUsd,
  mdiPlus,
  mdiAccountGroup,
  mdiChevronLeft,
  mdiChevronRight,
  mdiClockOutline,
  mdiMapMarker,
  mdiAlert,
  mdiCheck,
  mdiClose,
  mdiRefresh,
  mdiCreditCardOutline,
  mdiAutorenew,
  mdiOpenInNew,
} from "@mdi/js";
import SectionTitleLineWithButton from "../../../_components/Section/TitleLineWithButton";
import CardBox from "../../../_components/CardBox";
import Icon from "../../../_components/Icon";
import FormField from "../../../_components/FormField";
import { useAppData } from "@/app/_lib/hooks/useAppData";
import {
  AppButton,
  AppShell,
  EmptyState,
  ErrorState,
  LoadingState,
  RowSkeleton,
  SampleBadge,
} from "../_kit";
import { expandEvents, customRecurrenceLabel } from "@/app/_lib/recurrence";
import ImportEvents from "./ImportEvents";
import WeekView from "./WeekView";
import {
  DOW_LABELS,
  addDays as addCalDays,
  isoDay,
  rangeLabel,
  startOfWeek,
  weekDays,
  type Occurrence,
} from "@/app/_lib/calendarWeek";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type FamilyMember = {
  id: string;
  name: string;
  color: string;
  emoji: string;
  role: "parent" | "child";
};

type CalEvent = {
  id: string;
  title: string;
  cat: string;
  start: string;
  end: string;
  to: string;
  prio: string;
  src: string;
  rec: string | null;
  loc: string | null;
};

type CalTask = {
  id: string;
  title: string;
  cat: string;
  due: string;
  to: string;
  prio: string;
  status: "pending" | "completed";
  src: string;
};

type CalMed = {
  id: string;
  name: string;
  dose: string;
  to: string;
  freq: string;
  next: string;
  status: "due" | "taken";
  refill: string;
};

type CalBill = {
  id: string;
  name: string;
  amt: number;
  due: string;
  cat: string;
  status: "pending" | "paid";
  auto: boolean;
};

type CalendarData = {
  family: FamilyMember[];
  events: CalEvent[];
  tasks: CalTask[];
  meds: CalMed[];
  bills: CalBill[];
  /** Advances whenever the document changes, including by voice. */
  last_updated?: string;
  /** True only for a demo or fixture payload. Nothing here ever seeds. */
  sample?: boolean;
  /**
   * Dinners, when the document carries them. FreshTable owns this data and the
   * cross-app read is not built, so on a real box this is absent and the meals
   * strip is simply not rendered.
   */
  mealPlan?: { day: string; dish: string }[];
};

/**
 * Which calendar surface the Events tab shows.
 *
 * `week` is the landing state: today is a column of the default
 * view, so "what is happening today" costs zero taps. `month` is the existing
 * month grid plus the Upcoming Events list, unchanged and one tap away.
 */
type CalView = "day" | "week" | "month";

type TabKey = "events" | "tasks" | "meds" | "bills";

/* ------------------------------------------------------------------ */
/*  Constants                                                          */
/* ------------------------------------------------------------------ */

const TABS: { key: TabKey; label: string; icon: string }[] = [
  { key: "events", label: "Events", icon: mdiCalendarMonth },
  { key: "tasks", label: "Tasks", icon: mdiCheckCircle },
  { key: "meds", label: "Meds", icon: mdiPill },
  { key: "bills", label: "Bills", icon: mdiCurrencyUsd },
];

const EVENT_CATEGORIES = [
  "work",
  "medical",
  "sports",
  "school",
  "family",
  "bills",
  "appointment",
  "other",
];
const PRIORITIES = ["low", "medium", "high", "urgent"];
const BILL_CATEGORIES = ["utilities", "insurance", "subscription", "housing", "other"];
const MED_FREQUENCIES = ["daily", "twice-daily", "weekly", "as-needed"];

const PRIORITY_COLORS: Record<string, string> = {
  low: "bg-[color-mix(in_srgb,var(--mint)_16%,transparent)] text-[var(--mint)]",
  medium: "bg-[color-mix(in_srgb,var(--sky)_16%,transparent)] text-[var(--sky)]",
  high: "bg-[color-mix(in_srgb,var(--lemon)_16%,transparent)] text-[var(--lemon)]",
  urgent: "bg-[color-mix(in_srgb,var(--puff-red)_16%,transparent)] text-[var(--puff-red)]",
};

const DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function formatDate(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function formatTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

function formatCurrency(amt: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(amt);
}

function isoDate(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function daysInMonth(year: number, month: number): number {
  return new Date(year, month + 1, 0).getDate();
}

function firstDayOfMonth(year: number, month: number): number {
  return new Date(year, month, 1).getDay();
}

function isWithinDays(iso: string, days: number): boolean {
  if (!iso) return false;
  const target = new Date(iso);
  const now = new Date();
  const diff = (target.getTime() - now.getTime()) / (1000 * 60 * 60 * 24);
  return diff >= 0 && diff <= days;
}

function getMember(family: FamilyMember[], id: string | undefined): FamilyMember | undefined {
  if (!id) return undefined;
  return family.find((m) => m.id === id);
}

/* ------------------------------------------------------------------ */
/*  Small sub-components                                               */
/* ------------------------------------------------------------------ */

function FamilyBadge({ member }: { member: FamilyMember | undefined }) {
  if (!member) return null;
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium puff-on-accent"
      style={{ backgroundColor: member.color }}
    >
      <span>{member.emoji}</span>
      {member.name}
    </span>
  );
}

function PriorityPill({ prio }: { prio: string }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${PRIORITY_COLORS[prio] || PRIORITY_COLORS.medium}`}>
      {prio}
    </span>
  );
}

function CollapsibleForm({
  title,
  icon,
  isOpen,
  onToggle,
  className = "",
  children,
}: {
  title: string;
  icon: string;
  isOpen: boolean;
  onToggle: () => void;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <CardBox className={`mb-6 ${className}`}>
      <button
        type="button"
        onClick={onToggle}
        className="puff-48-h flex w-full items-center justify-between text-left"
      >
        <div className="flex items-center gap-2">
          <Icon path={icon} size="20" className="text-[var(--sky)]" />
          <span className="font-semibold text-[var(--text)]">{title}</span>
        </div>
        <Icon
          path={isOpen ? mdiClose : mdiPlus}
          size="20"
          className="text-[var(--text3)] transition-transform"
        />
      </button>
      {isOpen && <div className="mt-4 border-t border-[var(--line)] pt-4">{children}</div>}
    </CardBox>
  );
}

/* ------------------------------------------------------------------ */
/*  Main Page Component                                                */
/* ------------------------------------------------------------------ */

export default function CalendarPage() {
  const { data, loading, error, runAction, reload } = useAppData("calendar");
  const calData = data as CalendarData | null;

  const [activeTab, setActiveTab] = useState<TabKey>("events");
  const [selectedMember, setSelectedMember] = useState<string>("all");
  const [showAddForm, setShowAddForm] = useState(false);

  // Week view: the landing surface, plus the day it is anchored on.
  const [calView, setCalView] = useState<CalView>("week");
  const [anchorIso, setAnchorIso] = useState(() => isoDay(new Date()));
  const [narrow, setNarrow] = useState(false);

  // Calendar navigation
  const today = new Date();
  const [calYear, setCalYear] = useState(today.getFullYear());
  const [calMonth, setCalMonth] = useState(today.getMonth());

  // Event form state
  const [evtTitle, setEvtTitle] = useState("");
  const [evtStart, setEvtStart] = useState("");
  const [evtEnd, setEvtEnd] = useState("");
  const [evtCat, setEvtCat] = useState("other");
  const [evtTo, setEvtTo] = useState("");
  const [evtPrio, setEvtPrio] = useState("medium");
  const [evtLoc, setEvtLoc] = useState("");
  const [evtRec, setEvtRec] = useState("");

  // Task form state
  const [taskTitle, setTaskTitle] = useState("");
  const [taskDue, setTaskDue] = useState("");
  const [taskCat, setTaskCat] = useState("other");
  const [taskTo, setTaskTo] = useState("");
  const [taskPrio, setTaskPrio] = useState("medium");

  // Med form state
  const [medName, setMedName] = useState("");
  const [medDose, setMedDose] = useState("");
  const [medTo, setMedTo] = useState("");
  const [medFreq, setMedFreq] = useState("daily");
  const [medNext, setMedNext] = useState("");

  // Bill form state
  const [billName, setBillName] = useState("");
  const [billAmt, setBillAmt] = useState("");
  const [billDue, setBillDue] = useState("");
  const [billCat, setBillCat] = useState("utilities");
  const [billAuto, setBillAuto] = useState(false);

  /* ---- Derived data ---- */

  const family = calData?.family ?? [];
  const bills = calData?.bills ?? [];

  const filteredEvents = useMemo(
    () => {
      const events = calData?.events ?? [];
      // Expand recurring events into concrete instances for the visible
      // calendar window: 60 days back to 365 days forward (covers most use cases).
      const start = new Date();
      start.setDate(start.getDate() - 60);
      start.setHours(0, 0, 0, 0);
      const end = new Date();
      end.setDate(end.getDate() + 365);
      end.setHours(23, 59, 59, 999);
      const expanded = expandEvents(events as any, start, end) as CalEvent[];
      return selectedMember === "all"
        ? expanded
        : expanded.filter((e: any) => e.to === selectedMember);
    },
    [calData?.events, selectedMember],
  );
  const filteredTasks = useMemo(
    () => {
      const tasks = calData?.tasks ?? [];
      return selectedMember === "all" ? tasks : tasks.filter((t) => t.to === selectedMember);
    },
    [calData?.tasks, selectedMember],
  );
  const filteredMeds = useMemo(
    () => {
      const meds = calData?.meds ?? [];
      return selectedMember === "all" ? meds : meds.filter((m) => m.to === selectedMember);
    },
    [calData?.meds, selectedMember],
  );

  const pendingTasks = filteredTasks.filter((t) => t.status === "pending");
  const completedTasks = filteredTasks.filter((t) => t.status === "completed");
  const pendingBills = bills.filter((b) => b.status === "pending");
  const totalPending = pendingBills.reduce((sum, b) => sum + b.amt, 0);

  /* ---- Events by date for calendar (includes tasks & bills) ---- */

  type CalendarDot = { label: string; color: string; type: "event" | "task" | "med" | "bill" };

  const dotsByDate = useMemo(() => {
    const map: Record<string, CalendarDot[]> = {};
    const addDot = (dateStr: string | undefined, dot: CalendarDot) => {
      if (!dateStr) return;
      const key = dateStr.slice(0, 10);
      if (!key) return;
      if (!map[key]) map[key] = [];
      map[key].push(dot);
    };
    // Events
    for (const ev of filteredEvents) {
      addDot(ev.start, { label: ev.title, color: getMember(family, ev.to)?.color || "var(--sky)", type: "event" });
    }
    // Tasks with due dates
    for (const t of filteredTasks) {
      if (t.due) addDot(t.due, { label: `☑ ${t.title}`, color: t.status === "completed" ? "var(--mint)" : "var(--lemon)", type: "task" });
    }
    // Bills with due dates
    for (const b of bills) {
      if (b.due) addDot(b.due, { label: `$ ${b.name}`, color: b.status === "paid" ? "var(--mint)" : "var(--puff-red)", type: "bill" });
    }
    // Meds (daily markers for today)
    const today = new Date().toISOString().slice(0, 10);
    for (const m of filteredMeds) {
      addDot(today, { label: `💊 ${m.name}`, color: m.status === "taken" ? "var(--mint)" : "var(--lav)", type: "med" });
    }
    return map;
  }, [filteredEvents, filteredTasks, bills, filteredMeds, family]);

  // Keep eventsByDate for backward compat with event list rendering
  const eventsByDate = useMemo(() => {
    const map: Record<string, CalEvent[]> = {};
    for (const ev of filteredEvents) {
      const dateKey = ev.start?.slice(0, 10);
      if (dateKey) {
        if (!map[dateKey]) map[dateKey] = [];
        map[dateKey].push(ev);
      }
    }
    return map;
  }, [filteredEvents]);

  /* ---- Action handlers ---- */

  const handleAddEvent = useCallback(async () => {
    if (!evtTitle || !evtStart) return;
    await runAction("cal_add_event", {
      title: evtTitle,
      start_iso: evtStart,
      end_iso: evtEnd || evtStart,
      cat: evtCat,
      assigned_to: evtTo,
      prio: evtPrio,
      location: evtLoc || null,
      recurrence: evtRec || null,
    });
    setEvtTitle("");
    setEvtStart("");
    setEvtEnd("");
    setEvtCat("other");
    setEvtTo("");
    setEvtPrio("medium");
    setEvtLoc("");
    setEvtRec("");
    setShowAddForm(false);
  }, [evtTitle, evtStart, evtEnd, evtCat, evtTo, evtPrio, evtLoc, evtRec, runAction]);

  const handleAddTask = useCallback(async () => {
    if (!taskTitle || !taskDue) return;
    await runAction("cal_add_task", {
      title: taskTitle,
      due_iso: taskDue,
      cat: taskCat,
      assigned_to: taskTo,
      prio: taskPrio,
    });
    setTaskTitle("");
    setTaskDue("");
    setTaskCat("other");
    setTaskTo("");
    setTaskPrio("medium");
    setShowAddForm(false);
  }, [taskTitle, taskDue, taskCat, taskTo, taskPrio, runAction]);

  const handleAddMed = useCallback(async () => {
    if (!medName || !medDose) return;
    await runAction("cal_add_med", {
      name: medName,
      dose: medDose,
      assigned_to: medTo,
      freq: medFreq,
      next_time: medNext,
    });
    setMedName("");
    setMedDose("");
    setMedTo("");
    setMedFreq("daily");
    setMedNext("");
    setShowAddForm(false);
  }, [medName, medDose, medTo, medFreq, medNext, runAction]);

  const handleAddBill = useCallback(async () => {
    if (!billName || !billAmt || !billDue) return;
    await runAction("cal_add_bill", {
      name: billName,
      amount: parseFloat(billAmt),
      due_iso: billDue,
      cat: billCat,
      auto: billAuto,
    });
    setBillName("");
    setBillAmt("");
    setBillDue("");
    setBillCat("utilities");
    setBillAuto(false);
    setShowAddForm(false);
  }, [billName, billAmt, billDue, billCat, billAuto, runAction]);

  const handleCompleteTask = useCallback(
    (taskId: string) => runAction("cal_complete_task", { task_identifier: taskId }),
    [runAction],
  );

  const handleDeleteEvent = useCallback(
    (eventId: string) => runAction("cal_delete_event", { event_identifier: eventId }),
    [runAction],
  );

  const handleDeleteTask = useCallback(
    (taskId: string) => runAction("cal_delete_task", { task_identifier: taskId }),
    [runAction],
  );

  const handleTakeMed = useCallback(
    (medId: string) => runAction("cal_take_med", { med_identifier: medId }),
    [runAction],
  );

  const handlePayBill = useCallback(
    (billId: string) => runAction("cal_pay_bill", { bill_identifier: billId }),
    [runAction],
  );

  /* ---- Calendar month helpers ---- */

  const prevMonth = () => {
    if (calMonth === 0) {
      setCalMonth(11);
      setCalYear(calYear - 1);
    } else {
      setCalMonth(calMonth - 1);
    }
  };

  const nextMonth = () => {
    if (calMonth === 11) {
      setCalMonth(0);
      setCalYear(calYear + 1);
    } else {
      setCalMonth(calMonth + 1);
    }
  };

  const calDays = daysInMonth(calYear, calMonth);
  const calStart = firstDayOfMonth(calYear, calMonth);
  const todayStr = isoDate(today);

  /* ---- Week view ---- */

  // The phone shows one day. This is the only place the view is
  // narrowed; the switch below still works and still says which view is on.
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(max-width: 700px)");
    const sync = () => setNarrow(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  const effectiveView: CalView = calView === "month" ? "month" : narrow ? "day" : calView;
  const anchorDate = useMemo(() => new Date(`${anchorIso}T00:00:00`), [anchorIso]);
  const pickerDays = useMemo(() => weekDays(startOfWeek(anchorDate)), [anchorDate]);
  const boardDays = useMemo(
    () => (effectiveView === "day" ? [anchorDate] : pickerDays),
    [effectiveView, anchorDate, pickerDays],
  );

  const shiftBoard = useCallback(
    (steps: number) =>
      setAnchorIso((iso) =>
        isoDay(addCalDays(new Date(`${iso}T00:00:00`), steps * (effectiveView === "day" ? 1 : 7))),
      ),
    [effectiveView],
  );

  const openDay = useCallback((d: Date) => {
    setAnchorIso(isoDay(d));
    setCalView("day");
  }, []);

  /**
   * Undo a landed item using the delete action that already exists for its
   * kind. A medication has no delete action in the allowlist, so the strip
   * offers no Undo for one rather than a button that would 403.
   */
  const undoOccurrence = useCallback(
    (occ: Occurrence) => {
      if (occ.kind === "event") return void handleDeleteEvent(occ.event_id);
      if (occ.kind === "task") return void handleDeleteTask(occ.event_id);
    },
    [handleDeleteEvent, handleDeleteTask],
  );

  /**
   * The filter card, the tab strip and the add-form header keep every control
   * they have; on the wall they just stop spending the board's height on
   * margins and padding.
   */
  const leadCls = activeTab === "events" && effectiveView !== "month" ? "puff-wk-lead" : "";

  /* ---- Loading / Error states ---- */

  if (loading) {
    return (
      <AppShell title="Calendar" hint="Events, tasks, meds and bills">
        <LoadingState
          skeleton={<div className="puff-card puff-pad">
            <div className="grid grid-cols-7 gap-px">
              {Array.from({ length: 42 }).map((_, i) => (
                <div key={i} className="aspect-square rounded-md bg-[var(--chip)]" />
              ))}
            </div>
          </div>}
          line="Loading the calendar"
        />
      </AppShell>
    );
  }

  if (error) {
    return (
      <AppShell title="Calendar" hint="Events, tasks, meds and bills">
        <ErrorState
          skeleton={<div className="puff-card puff-pad">
            <div className="grid grid-cols-7 gap-px">
              {Array.from({ length: 42 }).map((_, i) => (
                <div key={i} className="aspect-square rounded-md bg-[var(--chip)]" />
              ))}
            </div>
          </div>}
          line="The calendar is not loading right now."
          detail={error}
          onRetry={reload}
        />
      </AppShell>
    );
  }

  /* ================================================================ */
  /*  Render                                                           */
  /* ================================================================ */

  return (
    <AppShell
      title="Calendar"
      hint={
        activeTab === "events" && effectiveView !== "month"
          ? rangeLabel(boardDays)
          : "Events, tasks, meds and bills"
      }
      actions={
        <div className="puff-taprow flex flex-wrap items-center gap-2">
          {activeTab === "events" && (
            <>
              {effectiveView !== "month" && (
                <>
                  <button
                    type="button"
                    className="puff-tap"
                    aria-label={effectiveView === "day" ? "Previous day" : "Previous week"}
                    onClick={() => shiftBoard(-1)}
                  >
                    <Icon path={mdiChevronLeft} size="24" />
                  </button>
                  <button
                    type="button"
                    className="puff-tap"
                    aria-label={effectiveView === "day" ? "Next day" : "Next week"}
                    onClick={() => shiftBoard(1)}
                  >
                    <Icon path={mdiChevronRight} size="24" />
                  </button>
                  <button
                    type="button"
                    className="puff-tap"
                    onClick={() => setAnchorIso(isoDay(new Date()))}
                  >
                    TODAY
                  </button>
                </>
              )}
              <div className="puff-wk-seg" role="group" aria-label="Calendar view">
                {/* A phone has two views, not three: seven columns do not fit
                    390px, so WEEK is not offered rather than offered and then
                    quietly overridden. */}
                {((narrow ? ["day", "month"] : ["day", "week", "month"]) as CalView[]).map((v) => (
                  <button
                    key={v}
                    type="button"
                    aria-pressed={effectiveView === v}
                    onClick={() => setCalView(v)}
                  >
                    {v.toUpperCase()}
                  </button>
                ))}
              </div>
            </>
          )}
          {calData?.sample ? <SampleBadge /> : null}
          <ImportEvents runAction={runAction} onDone={reload} />
          <AppButton
            icon={mdiOpenInNew}
            label="Open Calendar"
            tone="primary"
            small
            roundedFull
            href="/dashboard/apps/calendar/manage"
          />
          <AppButton
            label="Refresh"
            icon={mdiRefresh}
            tone="quiet"
            small
            roundedFull
            onClick={() => runAction("cal_list_events", { days_ahead: 30 })}
          />
        </div>
      }
    >

      {/* ---- Family Member Filter ---- */}
      <CardBox className={`mb-6 ${leadCls}`}>
        <div className="flex flex-wrap items-center gap-3">
          <Icon path={mdiAccountGroup} size="20" className="text-[var(--sky)]" />
          <span className="text-sm font-semibold text-[var(--text)]">Filter by:</span>
          <button
            type="button"
            onClick={() => setSelectedMember("all")}
            className={`puff-pill ${selectedMember === "all" ? "puff-pill-on" : ""}`}
          >
            All Family
          </button>
          {family.map((m) => (
            <button
              key={m.id}
              type="button"
              onClick={() => setSelectedMember(m.id)}
              className={`puff-pill ${selectedMember === m.id ? "puff-pill-on" : ""}`}
              style={
                selectedMember === m.id
                  ? { backgroundColor: m.color, borderColor: m.color }
                  : {}
              }
            >
              <span>{m.emoji}</span>
              {m.name}
            </button>
          ))}
        </div>
      </CardBox>

      {/* ---- Tab Navigation ---- */}
      <div className={`mb-6 flex gap-1 rounded-xl bg-white/60 p-1 backdrop-blur-sm ${leadCls}`}>
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => {
              setActiveTab(tab.key);
              setShowAddForm(false);
            }}
            className={`puff-pill flex-1 justify-center rounded-lg ${
              activeTab === tab.key ? "puff-pill-on" : "border-transparent bg-transparent"
            }`}
          >
            <Icon path={tab.icon} size="18" />
            <span className="hidden sm:inline">{tab.label}</span>
          </button>
        ))}
      </div>

      {/* ================================================================ */}
      {/*  EVENTS TAB                                                       */}
      {/* ================================================================ */}
      {activeTab === "events" && (
        <>
          {/* Add Event Form */}
          <CollapsibleForm
            title="Add New Event"
            icon={mdiPlus}
            isOpen={showAddForm}
            onToggle={() => setShowAddForm(!showAddForm)}
            className={showAddForm ? "" : leadCls}
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <FormField label="Title" labelFor="evt-title">
                {({ className }) => (
                  <input
                    id="evt-title"
                    className={className}
                    value={evtTitle}
                    onChange={(e) => setEvtTitle(e.target.value)}
                    placeholder="Event title"
                  />
                )}
              </FormField>
              <FormField label="Category" labelFor="evt-cat">
                {({ className }) => (
                  <select id="evt-cat" className={className} value={evtCat} onChange={(e) => setEvtCat(e.target.value)}>
                    {EVENT_CATEGORIES.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Start" labelFor="evt-start">
                {({ className }) => (
                  <input
                    id="evt-start"
                    type="datetime-local"
                    className={className}
                    value={evtStart}
                    onChange={(e) => setEvtStart(e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="End" labelFor="evt-end">
                {({ className }) => (
                  <input
                    id="evt-end"
                    type="datetime-local"
                    className={className}
                    value={evtEnd}
                    onChange={(e) => setEvtEnd(e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="Assign To" labelFor="evt-to">
                {({ className }) => (
                  <select id="evt-to" className={className} value={evtTo} onChange={(e) => setEvtTo(e.target.value)}>
                    <option value="">-- Select --</option>
                    {family.map((m) => (
                      <option key={m.id} value={m.id}>{m.emoji} {m.name}</option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Priority" labelFor="evt-prio">
                {({ className }) => (
                  <select id="evt-prio" className={className} value={evtPrio} onChange={(e) => setEvtPrio(e.target.value)}>
                    {PRIORITIES.map((p) => (
                      <option key={p} value={p}>{p}</option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Location" labelFor="evt-loc">
                {({ className }) => (
                  <input
                    id="evt-loc"
                    className={className}
                    value={evtLoc}
                    onChange={(e) => setEvtLoc(e.target.value)}
                    placeholder="Optional location"
                  />
                )}
              </FormField>
              <FormField label="Recurrence" labelFor="evt-rec">
                {({ className }) => (
                  <select id="evt-rec" className={className} value={evtRec} onChange={(e) => setEvtRec(e.target.value)}>
                    {/* A weekday SET ("custom:mon,tue,thu,fri") is not one of
                        the fixed six. A <select> whose value is not among its
                        options silently falls back to the first one, so the
                        set would be written away as "None" the moment this
                        form carried an existing row. Its own option keeps it. */}
                    {customRecurrenceLabel(evtRec) ? (
                      <option value={evtRec}>{customRecurrenceLabel(evtRec)}</option>
                    ) : null}
                    <option value="">None</option>
                    <option value="daily">Daily</option>
                    <option value="weekdays">Every weekday (Mon–Fri)</option>
                    <option value="weekly">Weekly</option>
                    <option value="biweekly">Every 2 weeks</option>
                    <option value="monthly">Monthly</option>
                    <option value="yearly">Yearly</option>
                  </select>
                )}
              </FormField>
            </div>
            <div className="mt-4 flex justify-end">
              <AppButton
                label="Add Event"
                icon={mdiPlus}
                tone="primary"
                roundedFull
                onClick={handleAddEvent}
                disabled={!evtTitle || !evtStart}
              />
            </div>
          </CollapsibleForm>

          {/* ---- Week / Day board: the landing surface (audit build item #4) ---- */}
          {effectiveView !== "month" && (
            <>
              {effectiveView === "day" && (
                <div className="puff-wk-daypick" role="group" aria-label="Pick a day">
                  {pickerDays.map((d) => {
                    const iso = isoDay(d);
                    return (
                      <button
                        key={iso}
                        type="button"
                        aria-pressed={iso === anchorIso}
                        onClick={() => setAnchorIso(iso)}
                      >
                        {DOW_LABELS[(d.getDay() + 6) % 7]}
                        <br />
                        {d.getDate()}
                      </button>
                    );
                  })}
                </div>
              )}
              <WeekView
                days={boardDays}
                family={family}
                doc={calData}
                lastUpdated={calData?.last_updated ?? null}
                selectedMember={selectedMember}
                onOpenDay={openDay}
                onCompleteTask={handleCompleteTask}
                onUndo={undoOccurrence}
              />
            </>
          )}

          {/* ---- Month grid + Upcoming Events: unchanged, one tap away ---- */}
          {effectiveView === "month" && (
          <>
          {/* Mini Month Calendar */}
          <CardBox className="mb-6">
            <div className="flex items-center justify-between mb-4">
              <button type="button" onClick={prevMonth} className="puff-48 rounded-full p-1 hover:bg-[var(--chip)]">
                <Icon path={mdiChevronLeft} size="24" className="text-[var(--sky)]" />
              </button>
              <h3 className="text-lg font-semibold text-[var(--text)]">
                {MONTH_NAMES[calMonth]} {calYear}
              </h3>
              <button type="button" onClick={nextMonth} className="puff-48 rounded-full p-1 hover:bg-[var(--chip)]">
                <Icon path={mdiChevronRight} size="24" className="text-[var(--sky)]" />
              </button>
            </div>
            <div className="grid grid-cols-7 gap-px">
              {/* Day headers */}
              {DAY_NAMES.map((d) => (
                <div key={d} className="py-2 text-center text-xs font-semibold text-[var(--text2)]">
                  {d}
                </div>
              ))}
              {/* Empty cells before first day */}
              {Array.from({ length: calStart }).map((_, i) => (
                <div key={`empty-${i}`} className="h-10" />
              ))}
              {/* Day cells */}
              {Array.from({ length: calDays }).map((_, i) => {
                const dayNum = i + 1;
                const dateStr = `${calYear}-${String(calMonth + 1).padStart(2, "0")}-${String(dayNum).padStart(2, "0")}`;
                const dayDots = dotsByDate[dateStr] || [];
                const isToday = dateStr === todayStr;
                return (
                  <div
                    key={dayNum}
                    className={`relative flex h-10 flex-col items-center justify-center rounded-lg text-sm transition ${
                      isToday
                        ? "bg-[var(--sky)] font-bold puff-on-accent shadow-md"
                        : dayDots.length > 0
                          ? "bg-[color-mix(in_srgb,var(--sky)_10%,transparent)] font-medium text-[var(--sky)]"
                          : "text-[var(--text2)] hover:bg-[var(--card)]"
                    }`}
                    title={dayDots.map((d) => d.label).join(", ")}
                  >
                    {dayNum}
                    {dayDots.length > 0 && (
                      <div className="absolute bottom-0.5 flex gap-0.5">
                        {dayDots.slice(0, 4).map((dot, idx) => (
                            <span
                              key={idx}
                              className="h-1.5 w-1.5 rounded-full"
                              style={{ backgroundColor: dot.color }}
                            />
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </CardBox>

          {/* Upcoming Events List */}
          <SectionTitleLineWithButton icon={mdiCalendarMonth} title="Upcoming Events">
            <span className="puff-cap">{filteredEvents.length} shown</span>
          </SectionTitleLineWithButton>
          <div className="mb-6 space-y-3">
            {filteredEvents.length === 0 ? (
              <EmptyState
                skeleton={<RowSkeleton rows={4} />}
                line="Nothing on the calendar."
                say={'say "add soccer Thursday at 4"'}
              />
            ) : (
              filteredEvents.map((ev) => {
                const member = getMember(family, ev.to);
                // Recurring events expand to multiple instances with the
                // same id, so use _occurrenceKey when available.
                const rowKey = (ev as any)._occurrenceKey || `${ev.id}-${ev.start}`;
                return (
                  <CardBox key={rowKey} isHoverable className="!flex-row items-center gap-4">
                    {/* Color bar */}
                    <div
                      className="h-14 w-1.5 flex-shrink-0 rounded-full"
                      style={{ backgroundColor: member?.color || "var(--sky)" }}
                    />
                    <div className="flex-1 min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <h4 className="font-semibold text-[var(--text)]">{ev.title}</h4>
                        <PriorityPill prio={ev.prio} />
                        <span className="rounded-full bg-[var(--chip)] px-2 py-0.5 text-xs text-[var(--text2)]">
                          {ev.cat}
                        </span>
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-[var(--text2)]">
                        <span className="inline-flex items-center gap-1">
                          <Icon path={mdiClockOutline} size="12" />
                          {formatDate(ev.start)} {formatTime(ev.start)}
                          {ev.end && ev.end !== ev.start && <> &mdash; {formatTime(ev.end)}</>}
                        </span>
                        {ev.loc && (
                          <span className="inline-flex items-center gap-1">
                            <Icon path={mdiMapMarker} size="12" />
                            {ev.loc}
                          </span>
                        )}
                      </div>
                    </div>
                    <FamilyBadge member={member} />
                    <button
                      type="button"
                      onClick={() => handleDeleteEvent(ev.id)}
                      className="puff-48 flex-shrink-0 rounded-full p-1.5 text-[var(--text3)] transition hover:bg-[color-mix(in_srgb,var(--puff-red)_16%,transparent)] hover:text-[var(--puff-red)]"
                      title="Delete event"
                    >
                      <Icon path={mdiClose} size="16" />
                    </button>
                  </CardBox>
                );
              })
            )}
          </div>
          </>
          )}
        </>
      )}

      {/* ================================================================ */}
      {/*  TASKS TAB                                                        */}
      {/* ================================================================ */}
      {activeTab === "tasks" && (
        <>
          {/* Add Task Form */}
          <CollapsibleForm
            title="Add New Task"
            icon={mdiPlus}
            isOpen={showAddForm}
            onToggle={() => setShowAddForm(!showAddForm)}
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <FormField label="Title" labelFor="task-title">
                {({ className }) => (
                  <input
                    id="task-title"
                    className={className}
                    value={taskTitle}
                    onChange={(e) => setTaskTitle(e.target.value)}
                    placeholder="Task title"
                  />
                )}
              </FormField>
              <FormField label="Due Date" labelFor="task-due">
                {({ className }) => (
                  <input
                    id="task-due"
                    type="date"
                    className={className}
                    value={taskDue}
                    onChange={(e) => setTaskDue(e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="Category" labelFor="task-cat">
                {({ className }) => (
                  <select id="task-cat" className={className} value={taskCat} onChange={(e) => setTaskCat(e.target.value)}>
                    {EVENT_CATEGORIES.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Assign To" labelFor="task-to">
                {({ className }) => (
                  <select id="task-to" className={className} value={taskTo} onChange={(e) => setTaskTo(e.target.value)}>
                    <option value="">-- Select --</option>
                    {family.map((m) => (
                      <option key={m.id} value={m.id}>{m.emoji} {m.name}</option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Priority" labelFor="task-prio">
                {({ className }) => (
                  <select id="task-prio" className={className} value={taskPrio} onChange={(e) => setTaskPrio(e.target.value)}>
                    {PRIORITIES.map((p) => (
                      <option key={p} value={p}>{p}</option>
                    ))}
                  </select>
                )}
              </FormField>
            </div>
            <div className="mt-4 flex justify-end">
              <AppButton
                label="Add Task"
                icon={mdiPlus}
                tone="primary"
                roundedFull
                onClick={handleAddTask}
                disabled={!taskTitle || !taskDue}
              />
            </div>
          </CollapsibleForm>

          {/* Kanban-style columns */}
          <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
            {/* Pending Column */}
            <div>
              <div className="mb-3 flex items-center gap-2">
                <span className="h-3 w-3 rounded-full bg-[var(--sky)]" />
                <h3 className="text-lg font-semibold text-[var(--text)]">
                  Pending ({pendingTasks.length})
                </h3>
              </div>
              <div className="space-y-3">
                {pendingTasks.length === 0 ? (
                  <CardBox>
                    <p className="py-6 text-center text-sm text-[var(--text3)]">All caught up!</p>
                  </CardBox>
                ) : (
                  pendingTasks.map((t) => {
                    const member = getMember(family, t.to);
                    return (
                      <CardBox key={t.id} isHoverable>
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0 flex-1">
                            <h4 className="font-semibold text-[var(--text)]">{t.title}</h4>
                            <div className="mt-1 flex flex-wrap items-center gap-2">
                              <FamilyBadge member={member} />
                              <PriorityPill prio={t.prio} />
                              <span className="text-xs text-[var(--text2)]">
                                Due: {formatDate(t.due)}
                              </span>
                            </div>
                          </div>
                          <div className="flex gap-1">
                            <button
                              type="button"
                              onClick={() => handleCompleteTask(t.id)}
                              className="flex-shrink-0 rounded-full bg-[color-mix(in_srgb,var(--mint)_16%,transparent)] p-2 text-[var(--mint)] transition hover:bg-[color-mix(in_srgb,var(--mint)_16%,transparent)]"
                              title="Complete task"
                            >
                              <Icon path={mdiCheck} size="18" />
                            </button>
                            <button
                              type="button"
                              onClick={() => handleDeleteTask(t.id)}
                              className="flex-shrink-0 rounded-full p-2 text-[var(--text3)] transition hover:bg-[color-mix(in_srgb,var(--puff-red)_16%,transparent)] hover:text-[var(--puff-red)]"
                              title="Delete task"
                            >
                              <Icon path={mdiClose} size="18" />
                            </button>
                          </div>
                        </div>
                      </CardBox>
                    );
                  })
                )}
              </div>
            </div>

            {/* Completed Column */}
            <div>
              <div className="mb-3 flex items-center gap-2">
                <span className="h-3 w-3 rounded-full bg-[var(--mint)]" />
                <h3 className="text-lg font-semibold text-[var(--text)]">
                  Completed ({completedTasks.length})
                </h3>
              </div>
              <div className="space-y-3">
                {completedTasks.length === 0 ? (
                  <CardBox>
                    <p className="py-6 text-center text-sm text-[var(--text3)]">No completed tasks yet.</p>
                  </CardBox>
                ) : (
                  completedTasks.map((t) => {
                    const member = getMember(family, t.to);
                    return (
                      <CardBox key={t.id} className="opacity-70">
                        <div className="flex items-start gap-2">
                          <Icon path={mdiCheckCircle} size="20" className="mt-0.5 flex-shrink-0 text-[var(--mint)]" />
                          <div className="min-w-0 flex-1">
                            <h4 className="font-medium text-[var(--text2)] line-through">{t.title}</h4>
                            <div className="mt-1 flex flex-wrap items-center gap-2">
                              <FamilyBadge member={member} />
                              <span className="text-xs text-[var(--text3)]">
                                Due: {formatDate(t.due)}
                              </span>
                            </div>
                          </div>
                        </div>
                      </CardBox>
                    );
                  })
                )}
              </div>
            </div>
          </div>
        </>
      )}

      {/* ================================================================ */}
      {/*  MEDS TAB                                                         */}
      {/* ================================================================ */}
      {activeTab === "meds" && (
        <>
          {/* Add Med Form */}
          <CollapsibleForm
            title="Add New Medication"
            icon={mdiPlus}
            isOpen={showAddForm}
            onToggle={() => setShowAddForm(!showAddForm)}
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <FormField label="Medication Name" labelFor="med-name">
                {({ className }) => (
                  <input
                    id="med-name"
                    className={className}
                    value={medName}
                    onChange={(e) => setMedName(e.target.value)}
                    placeholder="e.g. Vitamin D"
                  />
                )}
              </FormField>
              <FormField label="Dose" labelFor="med-dose">
                {({ className }) => (
                  <input
                    id="med-dose"
                    className={className}
                    value={medDose}
                    onChange={(e) => setMedDose(e.target.value)}
                    placeholder="e.g. 250mg or As directed"
                  />
                )}
              </FormField>
              <FormField label="Assign To" labelFor="med-to">
                {({ className }) => (
                  <select id="med-to" className={className} value={medTo} onChange={(e) => setMedTo(e.target.value)}>
                    <option value="">-- Select --</option>
                    {family.map((m) => (
                      <option key={m.id} value={m.id}>{m.emoji} {m.name}</option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Frequency" labelFor="med-freq">
                {({ className }) => (
                  <select id="med-freq" className={className} value={medFreq} onChange={(e) => setMedFreq(e.target.value)}>
                    {MED_FREQUENCIES.map((f) => (
                      <option key={f} value={f}>{f}</option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Next Time" labelFor="med-next">
                {({ className }) => (
                  <input
                    id="med-next"
                    type="time"
                    className={className}
                    value={medNext}
                    onChange={(e) => setMedNext(e.target.value)}
                  />
                )}
              </FormField>
            </div>
            <div className="mt-4 flex justify-end">
              <AppButton
                label="Add Medication"
                icon={mdiPlus}
                tone="primary"
                roundedFull
                onClick={handleAddMed}
                disabled={!medName || !medDose}
              />
            </div>
          </CollapsibleForm>

          {/* Medication Cards */}
          <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-2">
            {filteredMeds.length === 0 ? (
              <CardBox className="md:col-span-2">
                <p className="py-8 text-center text-[var(--text3)]">
                  No medications tracked. Add one above!
                </p>
              </CardBox>
            ) : (
              filteredMeds.map((med) => {
                const member = getMember(family, med.to);
                const refillSoon = isWithinDays(med.refill, 7);
                const isDue = med.status === "due";
                return (
                  <CardBox key={med.id} isHoverable>
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <Icon path={mdiPill} size="20" className={isDue ? "text-[var(--sky)]" : "text-[var(--mint)]"} />
                          <h4 className="font-semibold text-[var(--text)]">{med.name}</h4>
                        </div>
                        <div className="mt-2 space-y-1 text-sm text-[var(--text2)]">
                          <p><span className="font-medium">Dose:</span> {med.dose}</p>
                          <p><span className="font-medium">Frequency:</span> {med.freq}</p>
                          <p>
                            <span className="font-medium">Next:</span> {med.next}
                          </p>
                        </div>
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          <FamilyBadge member={member} />
                          <span
                            className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                              isDue
                                ? "bg-[color-mix(in_srgb,var(--lemon)_16%,transparent)] text-[var(--lemon)]"
                                : "bg-[color-mix(in_srgb,var(--mint)_16%,transparent)] text-[var(--mint)]"
                            }`}
                          >
                            {isDue ? "Due" : "Taken"}
                          </span>
                          {refillSoon && (
                            <span className="inline-flex items-center gap-1 rounded-full bg-[color-mix(in_srgb,var(--puff-red)_16%,transparent)] px-2 py-0.5 text-xs font-medium text-[var(--puff-red)]">
                              <Icon path={mdiAlert} size="12" />
                              Refill by {formatDate(med.refill)}
                            </span>
                          )}
                        </div>
                      </div>
                      {isDue && (
                        <button
                          type="button"
                          onClick={() => handleTakeMed(med.id)}
                          className="flex-shrink-0 rounded-xl bg-gradient-to-r from-[var(--sky)] to-[var(--sky)] px-4 py-2 text-sm font-medium puff-on-accent shadow-md transition hover:shadow-lg"
                        >
                          Take
                        </button>
                      )}
                    </div>
                  </CardBox>
                );
              })
            )}
          </div>
        </>
      )}

      {/* ================================================================ */}
      {/*  BILLS TAB                                                        */}
      {/* ================================================================ */}
      {activeTab === "bills" && (
        <>
          {/* Total Pending Banner */}
          <CardBox className="mb-6">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-12 w-12 items-center justify-center rounded-full bg-gradient-to-br from-[var(--sky)] to-[var(--sky)] puff-on-accent shadow-md">
                  <Icon path={mdiCurrencyUsd} size="24" />
                </div>
                <div>
                  <p className="text-sm text-[var(--text2)]">Total Pending</p>
                  <p className="text-2xl font-bold text-[var(--text)]">
                    {formatCurrency(totalPending)}
                  </p>
                </div>
              </div>
              <span className="rounded-full bg-[color-mix(in_srgb,var(--lemon)_16%,transparent)] px-3 py-1 text-sm font-medium text-[var(--lemon)]">
                {pendingBills.length} unpaid
              </span>
            </div>
          </CardBox>

          {/* Add Bill Form */}
          <CollapsibleForm
            title="Add New Bill"
            icon={mdiPlus}
            isOpen={showAddForm}
            onToggle={() => setShowAddForm(!showAddForm)}
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <FormField label="Bill Name" labelFor="bill-name">
                {({ className }) => (
                  <input
                    id="bill-name"
                    className={className}
                    value={billName}
                    onChange={(e) => setBillName(e.target.value)}
                    placeholder="e.g. Electric"
                  />
                )}
              </FormField>
              <FormField label="Amount" labelFor="bill-amt">
                {({ className }) => (
                  <input
                    id="bill-amt"
                    type="number"
                    step="0.01"
                    className={className}
                    value={billAmt}
                    onChange={(e) => setBillAmt(e.target.value)}
                    placeholder="0.00"
                  />
                )}
              </FormField>
              <FormField label="Due Date" labelFor="bill-due">
                {({ className }) => (
                  <input
                    id="bill-due"
                    type="date"
                    className={className}
                    value={billDue}
                    onChange={(e) => setBillDue(e.target.value)}
                  />
                )}
              </FormField>
              <FormField label="Category" labelFor="bill-cat">
                {({ className }) => (
                  <select id="bill-cat" className={className} value={billCat} onChange={(e) => setBillCat(e.target.value)}>
                    {BILL_CATEGORIES.map((c) => (
                      <option key={c} value={c}>{c}</option>
                    ))}
                  </select>
                )}
              </FormField>
              <FormField label="Auto-Pay">
                {() => (
                  <label className="flex cursor-pointer items-center gap-3 py-3">
                    <div className="relative">
                      <input
                        type="checkbox"
                        checked={billAuto}
                        onChange={(e) => setBillAuto(e.target.checked)}
                        className="peer sr-only"
                      />
                      <div className="h-6 w-11 rounded-full bg-[var(--chip)] transition peer-checked:bg-[var(--sky)]" />
                      <div className="absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow-md transition peer-checked:translate-x-5" />
                    </div>
                    <span className="text-sm text-[var(--text2)]">
                      {billAuto ? "Auto-pay enabled" : "Manual payment"}
                    </span>
                  </label>
                )}
              </FormField>
            </div>
            <div className="mt-4 flex justify-end">
              <AppButton
                label="Add Bill"
                icon={mdiPlus}
                tone="primary"
                roundedFull
                onClick={handleAddBill}
                disabled={!billName || !billAmt || !billDue}
              />
            </div>
          </CollapsibleForm>

          {/* Bills List */}
          <CardBox hasTable className="mb-6">
            <div className="puff-scroll-x">
            <table>
              <thead>
                <tr>
                  <th>Bill</th>
                  <th>Amount</th>
                  <th>Due Date</th>
                  <th>Category</th>
                  <th>Auto-Pay</th>
                  <th>Status</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {bills.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="py-8 text-center text-[var(--text3)]">
                      No bills tracked. Add one above!
                    </td>
                  </tr>
                ) : (
                  bills.map((bill) => {
                    const isPending = bill.status === "pending";
                    const overdue = isPending && new Date(bill.due) < today;
                    return (
                      <tr key={bill.id} className={overdue ? "bg-[color-mix(in_srgb,var(--puff-red)_16%,transparent)]" : ""}>
                        <td data-label="Bill">
                          <div className="flex items-center gap-2">
                            <Icon path={mdiCreditCardOutline} size="16" className="text-[var(--sky)]" />
                            <span className="font-medium">{bill.name}</span>
                          </div>
                        </td>
                        <td data-label="Amount">
                          <span className="font-semibold text-[var(--text)]">
                            {formatCurrency(bill.amt)}
                          </span>
                        </td>
                        <td data-label="Due Date">
                          <span className={overdue ? "font-medium text-[var(--puff-red)]" : ""}>
                            {formatDate(bill.due)}
                            {overdue && (
                              <span className="ml-1 text-xs text-[var(--puff-red)]">(overdue)</span>
                            )}
                          </span>
                        </td>
                        <td data-label="Category">
                          <span className="rounded bg-[var(--chip)] px-1.5 py-0.5 text-xs">
                            {bill.cat}
                          </span>
                        </td>
                        <td data-label="Auto-Pay">
                          {bill.auto ? (
                            <span className="inline-flex items-center gap-1 rounded-full bg-[color-mix(in_srgb,var(--sky)_10%,transparent)] px-2 py-0.5 text-xs font-medium text-[var(--sky)]">
                              <Icon path={mdiAutorenew} size="12" />
                              Auto
                            </span>
                          ) : (
                            <span className="text-xs text-[var(--text3)]">Manual</span>
                          )}
                        </td>
                        <td data-label="Status">
                          <span
                            className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                              isPending
                                ? "bg-[color-mix(in_srgb,var(--lemon)_16%,transparent)] text-[var(--lemon)]"
                                : "bg-[color-mix(in_srgb,var(--mint)_16%,transparent)] text-[var(--mint)]"
                            }`}
                          >
                            {bill.status}
                          </span>
                        </td>
                        <td data-label="Action">
                          {isPending && (
                            <AppButton
                              label="Pay"
                              icon={mdiCurrencyUsd}
                              tone="ok"
                              small
                              roundedFull
                              onClick={() => handlePayBill(bill.id)}
                            />
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
            </div>
          </CardBox>
              </>
      )}
    </AppShell>
  );
}
