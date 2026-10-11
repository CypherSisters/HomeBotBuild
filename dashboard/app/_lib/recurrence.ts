/**
 * Calendar event recurrence expansion.
 *
 * The backend stores events with a single `start` time and an optional
 * `rec` field describing how to repeat them. This module expands those
 * stored events into concrete instances for a date range so the UI can
 * display every occurrence.
 *
 * Supported `rec` values:
 *   - "daily"          — every day
 *   - "weekdays"       — Mon–Fri
 *   - "weekly"         — same day of week each week
 *   - "biweekly"       — every 14 days
 *   - "monthly"        — same day of month each month
 *   - "yearly"         — same date each year
 *   - "custom:mon,tue,thu,fri" — an explicit weekday SET
 *   - object form: { freq, interval?, byday?, count?, until? }
 *     where byday is a list like ["MO","TU","WE","TH","FR"]
 *
 * The `custom:` form exists because the fixed six cannot say "every Tuesday,
 * Wednesday, Thursday and Friday", which is what the household actually says.
 * The box writes it (puffpai/calendar_dates.py, CUSTOM_PREFIX) and expands it
 * server-side for /apps/calendar/occurrences; this module is the CLIENT-side
 * expansion the month grid and the home "Today" strip use, and until now its
 * `default: return null` treated such a row as non-recurring — so a practice
 * on four mornings a week showed up on exactly one of them.
 *
 * Returns clones of the original event with `start`/`end` overridden
 * for each occurrence and a synthesized `_occurrenceKey` for React keys.
 */

export interface CalendarEvent {
  id: string;
  title: string;
  start: string; // ISO datetime
  end?: string;
  cat?: string;
  to?: string;
  prio?: string;
  rec?: string | RecurrenceRule | null;
  loc?: string | null;
  src?: string;
  taskId?: string;
  [key: string]: any;
}

export interface RecurrenceRule {
  freq: "daily" | "weekly" | "monthly" | "yearly";
  interval?: number;
  byday?: string[]; // ["MO", "TU", ...]
  count?: number;
  until?: string; // ISO date
}

const DAY_CODES = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];

/** The prefix the box writes a weekday SET with. */
export const CUSTOM_REC_PREFIX = "custom:";

/** Backend abbreviation -> the iCal-style code `expandEvent` matches on. */
const CUSTOM_DAY_CODES: Record<string, string> = {
  mon: "MO",
  tue: "TU",
  tues: "TU",
  wed: "WE",
  weds: "WE",
  thu: "TH",
  thur: "TH",
  thurs: "TH",
  fri: "FR",
  sat: "SA",
  sun: "SU",
};

const DAY_LABELS: Record<string, string> = {
  MO: "Mon",
  TU: "Tue",
  WE: "Wed",
  TH: "Thu",
  FR: "Fri",
  SA: "Sat",
  SU: "Sun",
};

const DAY_ORDER = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];

/**
 * The day codes in a `custom:` recurrence, in week order, or null.
 *
 * null means "not a weekday-set recurrence" — including a `custom:` value
 * whose day names are all unreadable, which degrades to the single base
 * occurrence rather than to wrong days (the same choice the box makes).
 */
export function customDayCodes(rec: unknown): string[] | null {
  if (typeof rec !== "string") return null;
  const text = rec.toLowerCase().trim();
  if (!text.startsWith(CUSTOM_REC_PREFIX)) return null;
  const codes = new Set<string>();
  for (const token of text.slice(CUSTOM_REC_PREFIX.length).split(",")) {
    const code = CUSTOM_DAY_CODES[token.trim()];
    if (code) codes.add(code);
  }
  const ordered = DAY_ORDER.filter((d) => codes.has(d));
  return ordered.length > 0 ? ordered : null;
}

/**
 * "Custom days (Tue, Wed, Thu, Fri)" — how a weekday set is shown to a person.
 * Returns null for anything that is not one.
 */
export function customRecurrenceLabel(rec: unknown): string | null {
  const codes = customDayCodes(rec);
  if (!codes) return null;
  return `Custom days (${codes.map((d) => DAY_LABELS[d]).join(", ")})`;
}

function parseISO(s: string): Date | null {
  if (!s) return null;
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}

function addMonths(d: Date, n: number): Date {
  const r = new Date(d);
  r.setMonth(r.getMonth() + n);
  return r;
}

function addYears(d: Date, n: number): Date {
  const r = new Date(d);
  r.setFullYear(r.getFullYear() + n);
  return r;
}

function normalizeRule(rec: string | RecurrenceRule | null | undefined): RecurrenceRule | null {
  if (!rec) return null;
  if (typeof rec === "object") return rec;
  const lower = rec.toLowerCase().trim();
  const customDays = customDayCodes(lower);
  if (customDays) {
    return { freq: "weekly", interval: 1, byday: customDays };
  }
  switch (lower) {
    case "daily":
    case "every day":
      return { freq: "daily", interval: 1 };
    case "weekdays":
    case "every weekday":
    case "mon-fri":
    case "weekday":
      return { freq: "weekly", interval: 1, byday: ["MO", "TU", "WE", "TH", "FR"] };
    case "weekly":
      return { freq: "weekly", interval: 1 };
    case "biweekly":
    case "every two weeks":
      return { freq: "weekly", interval: 2 };
    case "monthly":
      return { freq: "monthly", interval: 1 };
    case "yearly":
    case "annually":
      return { freq: "yearly", interval: 1 };
    case "none":
    case "":
      return null;
    default:
      return null;
  }
}

/**
 * Expand a single event (which may or may not be recurring) into concrete
 * occurrences within [rangeStart, rangeEnd]. Returns at most `maxOccurrences`
 * to prevent runaway expansion.
 */
export function expandEvent(
  event: CalendarEvent,
  rangeStart: Date,
  rangeEnd: Date,
  maxOccurrences = 366
): CalendarEvent[] {
  const start = parseISO(event.start);
  if (!start) return [];

  const rule = normalizeRule(event.rec);

  // Non-recurring event — return as-is if it falls in range
  if (!rule) {
    if (start >= rangeStart && start <= rangeEnd) {
      return [{ ...event, _occurrenceKey: `${event.id}-${start.toISOString()}` }];
    }
    return [];
  }

  const interval = rule.interval || 1;
  const occurrences: CalendarEvent[] = [];
  const until = rule.until ? parseISO(rule.until) : null;

  // Compute event duration so we can preserve end time on each occurrence
  const end = parseISO(event.end || event.start) || start;
  const durationMs = end.getTime() - start.getTime();

  let cursor = new Date(start);
  let count = 0;

  // Don't iterate forever — bound by maxOccurrences AND rangeEnd AND `until`/`count`
  while (cursor <= rangeEnd && occurrences.length < maxOccurrences) {
    if (rule.count && count >= rule.count) break;
    if (until && cursor > until) break;

    // Check if this occurrence is in our display range
    if (cursor >= rangeStart) {
      // For weekly with byday, only emit on listed days
      if (rule.freq === "weekly" && rule.byday && rule.byday.length > 0) {
        const dayCode = DAY_CODES[cursor.getDay()];
        if (rule.byday.includes(dayCode)) {
          const occEnd = new Date(cursor.getTime() + durationMs);
          occurrences.push({
            ...event,
            start: cursor.toISOString(),
            end: occEnd.toISOString(),
            _occurrenceKey: `${event.id}-${cursor.toISOString()}`,
            _isRecurringInstance: true,
          });
          count++;
        }
      } else {
        const occEnd = new Date(cursor.getTime() + durationMs);
        occurrences.push({
          ...event,
          start: cursor.toISOString(),
          end: occEnd.toISOString(),
          _occurrenceKey: `${event.id}-${cursor.toISOString()}`,
          _isRecurringInstance: count > 0,
        });
        count++;
      }
    }

    // Step cursor forward
    if (rule.freq === "weekly" && rule.byday && rule.byday.length > 0) {
      // For weekday rules, advance by 1 day at a time
      cursor = addDays(cursor, 1);
    } else if (rule.freq === "daily") {
      cursor = addDays(cursor, interval);
    } else if (rule.freq === "weekly") {
      cursor = addDays(cursor, 7 * interval);
    } else if (rule.freq === "monthly") {
      cursor = addMonths(cursor, interval);
    } else if (rule.freq === "yearly") {
      cursor = addYears(cursor, interval);
    } else {
      break;
    }
  }

  return occurrences;
}

/**
 * Expand a list of events into all occurrences within a date range.
 */
export function expandEvents(
  events: CalendarEvent[],
  rangeStart: Date,
  rangeEnd: Date
): CalendarEvent[] {
  const out: CalendarEvent[] = [];
  for (const event of events || []) {
    out.push(...expandEvent(event, rangeStart, rangeEnd));
  }
  out.sort((a, b) => a.start.localeCompare(b.start));
  return out;
}

/**
 * Get the next single occurrence of an event from a given starting point.
 * Useful for "what's next" displays.
 */
export function nextOccurrence(
  event: CalendarEvent,
  from: Date = new Date()
): CalendarEvent | null {
  const horizon = addYears(from, 1);
  const occs = expandEvent(event, from, horizon, 1);
  return occs[0] || null;
}
