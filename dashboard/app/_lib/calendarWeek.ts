/**
 * Week-view data model for the Calendar app route (audit build item #4).
 *
 * The week view reads OCCURRENCES, not stored events: a weekly soccer practice
 * is one document row and five chips on the wall. The backend serves them at
 *
 *   GET /apps/calendar/occurrences?from=YYYY-MM-DD&to=YYYY-MM-DD   (<= 31 days)
 *   -> { from, to, occurrences: [...] }
 *
 * reached through the dashboard's same-origin proxy at
 * /api/pai/apps/calendar/occurrences.
 *
 * That route is new. On a box whose backend predates it the fetch 404s, and
 * `occurrencesFromDocument` below expands the raw calendar document instead —
 * NON-RECURRING rows only, because expanding a recurrence rule on the client is
 * exactly the split-brain the endpoint exists to remove. When the document does
 * carry recurring rows the caller is told so (`recurringSkipped`) and says so on
 * screen in one line. Never an empty grid, never a crash.
 */

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

export type OccurrenceKind = "event" | "task" | "med";

export type Occurrence = {
  /** Unique per occurrence, so a weekly event's five chips have five keys. */
  occurrence_id: string;
  /** The stored row this came from — what an edit or a delete addresses. */
  event_id: string;
  title: string;
  /** ISO datetime, or a bare YYYY-MM-DD for an all-day row. */
  start: string;
  end: string | null;
  all_day: boolean;
  /** Household member id, or null. */
  person: string | null;
  kind: OccurrenceKind;
  /**
   * Render as label + time with NO person colour and NO initial.
   *
   * Set by the backend for every health row. A person-tagged medication chip
   * publishes a health-to-identity link on a screen the whole room can see
   * (the backend classes meds[] as health.condition, P4).
   */
  label_only: boolean;
};

export type OccurrenceResponse = {
  from: string;
  to: string;
  occurrences: Occurrence[];
};

/** The four bands the wall reads from a doorway, instead of a 24-hour scroll. */
export type DayPart = "allday" | "morning" | "afternoon" | "evening";

export const DAY_PARTS: { key: Exclude<DayPart, "allday">; label: string }[] = [
  { key: "morning", label: "MORNING" },
  { key: "afternoon", label: "AFTERNOON" },
  { key: "evening", label: "EVENING" },
];

export const DOW_LABELS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];

/* ------------------------------------------------------------------ */
/*  Dates                                                              */
/* ------------------------------------------------------------------ */

export function isoDay(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  r.setHours(0, 0, 0, 0);
  return r;
}

/** Monday of the week containing `d`. The week is Mon-Sun. */
export function startOfWeek(d: Date): Date {
  const r = new Date(d);
  r.setHours(0, 0, 0, 0);
  // getDay(): 0=Sun..6=Sat. A Monday-based offset puts Sunday six days back.
  const offset = (r.getDay() + 6) % 7;
  r.setDate(r.getDate() - offset);
  return r;
}

export function weekDays(start: Date): Date[] {
  return Array.from({ length: 7 }, (_, i) => addDays(start, i));
}

/** `MON 7 SEP - SUN 13 SEP 2026`, mono, for the header. */
export function rangeLabel(days: Date[]): string {
  if (days.length === 0) return "";
  const first = days[0];
  const last = days[days.length - 1];
  const fmt = (d: Date) =>
    `${DOW_LABELS[(d.getDay() + 6) % 7]} ${d.getDate()} ${d
      .toLocaleDateString("en-US", { month: "short" })
      .toUpperCase()}`;
  if (days.length === 1) return `${fmt(first)} ${first.getFullYear()}`;
  return `${fmt(first)} – ${fmt(last)} ${last.getFullYear()}`;
}

/** Mono clock face for a chip. Empty for an all-day row. */
export function occTime(occ: Occurrence): string {
  if (occ.all_day) return "";
  const d = new Date(occ.start);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

/** The day column an occurrence belongs to, as YYYY-MM-DD. */
export function occDay(occ: Occurrence): string {
  if (occ.start.length === 10) return occ.start;
  const d = new Date(occ.start);
  return Number.isNaN(d.getTime()) ? occ.start.slice(0, 10) : isoDay(d);
}

export function occPart(occ: Occurrence): DayPart {
  if (occ.all_day) return "allday";
  const d = new Date(occ.start);
  if (Number.isNaN(d.getTime())) return "morning";
  const h = d.getHours();
  if (h < 12) return "morning";
  if (h < 17) return "afternoon";
  return "evening";
}

/* ------------------------------------------------------------------ */
/*  Client-side fallback                                               */
/* ------------------------------------------------------------------ */

type RawEvent = {
  id?: string;
  title?: string;
  start?: string;
  end?: string | null;
  to?: string;
  rec?: unknown;
  all_day?: boolean;
};
type RawTask = { id?: string; title?: string; due?: string; to?: string; status?: string };
type RawMed = { id?: string; name?: string; next?: string; to?: string };

export type CalendarDocument = {
  events?: RawEvent[];
  tasks?: RawTask[];
  meds?: RawMed[];
};

function inRange(day: string, from: string, to: string): boolean {
  return day >= from && day <= to;
}

/** True when a stored row carries any recurrence rule at all. */
function isRecurring(rec: unknown): boolean {
  if (rec === null || rec === undefined || rec === "") return false;
  if (typeof rec === "string") {
    const t = rec.trim();
    return t !== "" && t !== "none";
  }
  return true;
}

/**
 * Expand the raw document into occurrences, for NON-RECURRING rows only.
 *
 * Used only when the occurrences endpoint is absent. `recurringSkipped` counts
 * the rows this deliberately did not expand, so the page can say so in one line
 * rather than quietly showing a thinner week than the household actually has.
 */
export function occurrencesFromDocument(
  doc: CalendarDocument | null,
  from: string,
  to: string,
): { occurrences: Occurrence[]; recurringSkipped: number } {
  const out: Occurrence[] = [];
  let recurringSkipped = 0;

  for (const ev of doc?.events ?? []) {
    if (!ev?.start || !ev?.id) continue;
    if (isRecurring(ev.rec)) {
      recurringSkipped += 1;
      continue;
    }
    const day = ev.start.length === 10 ? ev.start : isoDay(new Date(ev.start));
    if (!inRange(day, from, to)) continue;
    const allDay =
      ev.all_day === true ||
      ev.start.length === 10 ||
      (ev.start.slice(11, 16) === "00:00" && (!ev.end || ev.end.slice(11, 16) === "00:00"));
    out.push({
      occurrence_id: `ev:${ev.id}:${day}`,
      event_id: String(ev.id),
      title: String(ev.title ?? ""),
      start: ev.start,
      end: ev.end ?? null,
      all_day: allDay,
      person: ev.to ? String(ev.to) : null,
      kind: "event",
      label_only: false,
    });
  }

  for (const t of doc?.tasks ?? []) {
    if (!t?.due || !t?.id) continue;
    if (t.status === "completed") continue;
    const day = t.due.slice(0, 10);
    if (!inRange(day, from, to)) continue;
    out.push({
      occurrence_id: `task:${t.id}:${day}`,
      event_id: String(t.id),
      title: String(t.title ?? ""),
      start: t.due,
      end: null,
      all_day: t.due.length === 10,
      person: t.to ? String(t.to) : null,
      kind: "task",
      label_only: false,
    });
  }

  for (const m of doc?.meds ?? []) {
    if (!m?.next || !m?.id) continue;
    const day = m.next.slice(0, 10);
    if (!inRange(day, from, to)) continue;
    out.push({
      occurrence_id: `med:${m.id}:${day}`,
      event_id: String(m.id),
      title: String(m.name ?? ""),
      start: m.next,
      end: null,
      all_day: m.next.length === 10,
      // A health row carries no person on the wall, in either direction.
      person: null,
      kind: "med",
      label_only: true,
    });
  }

  out.sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0));
  return { occurrences: out, recurringSkipped };
}
