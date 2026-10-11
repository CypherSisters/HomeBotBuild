"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getCalendarOccurrences } from "@/app/_lib/services/appService";
import {
  DAY_PARTS,
  DOW_LABELS,
  type CalendarDocument,
  type Occurrence,
  isoDay,
  occDay,
  occPart,
  occTime,
  occurrencesFromDocument,
} from "@/app/_lib/calendarWeek";
import { personColor } from "../_kit";

/* ------------------------------------------------------------------ */
/*  Props                                                              */
/* ------------------------------------------------------------------ */

export type WeekMember = { id: string; name: string };

/**
 * Force the health rule on every row that arrives, whatever the API said.
 *
 * A `kind: "med"` row is a health row (the backend classes meds[] as
 * health.condition, P4). It carries NO person colour, NO
 * person label, and it is never attributed to a person by the filter — a
 * person-tagged medication row publishes a health-to-identity link on a screen
 * the whole room can see.
 *
 * The dashboard does not trust the payload to have got this right. If the
 * backend ever returns `kind: "med"` with `label_only: false` and a `person`,
 * that person is dropped here, at the single boundary every downstream view
 * reads from: the grid, both rails, the person filter and the landing strip.
 * Failing closed costs a stripe; failing open publishes a health fact.
 */
function enforceHealthRule(rows: Occurrence[]): Occurrence[] {
  return rows.map((o) =>
    o.kind === "med" || o.label_only
      ? { ...o, label_only: true, person: null }
      : o,
  );
}

export type WeekTask = {
  id: string;
  title: string;
  due: string;
  to: string;
  status: string;
};

export type WeekMeal = { day: string; dish: string };

type Props = {
  /** The days this board renders: seven for a week, one for a day. */
  days: Date[];
  /** Household members, in document order — the per-person colour order. */
  family: WeekMember[];
  /** The raw calendar document, for the fallback expansion and the rails. */
  doc: (Omit<CalendarDocument, "tasks"> & { tasks?: WeekTask[]; mealPlan?: WeekMeal[] }) | null;
  /**
   * The document's `last_updated`. When this advances, the occurrence set is
   * re-read and anything new is marked `.landed` — this is how a voice-added
   * item announces itself.
   */
  lastUpdated: string | null;
  /** `all`, or a member id. Filters chips, tasks and the person column. */
  selectedMember: string;
  /** Tap a chip or a day header to open that day. */
  onOpenDay: (day: Date) => void;
  /** Complete a task from the rail. */
  onCompleteTask: (taskId: string) => void;
  /** Undo a landed item — the existing per-kind delete action. */
  onUndo: (occ: Occurrence) => void;
};

/* ------------------------------------------------------------------ */
/*  Chip                                                               */
/* ------------------------------------------------------------------ */

function OccChip({
  occ,
  colour,
  who,
  landed,
  onSettled,
  onOpen,
}: {
  occ: Occurrence;
  colour: string | null;
  who: string | null;
  landed: boolean;
  onSettled: (id: string) => void;
  onOpen: () => void;
}) {
  const time = occTime(occ);
  return (
    <button
      type="button"
      className={`puff-wk-chip${landed ? " puff-wk-landed" : ""}`}
      data-kind={occ.kind}
      data-person={colour ? "1" : "0"}
      style={colour ? ({ ["--pc" as string]: colour } as React.CSSProperties) : undefined}
      // The class is stripped the moment the one-shot finishes, so a paused
      // animation (puff-no-motion / prefers-reduced-motion) can never hold the
      // ring on screen for a household that asked for no motion.
      onAnimationEnd={() => onSettled(occ.occurrence_id)}
      onClick={onOpen}
    >
      <span className="puff-wk-top">
        {time ? <span className="puff-wk-t">{time}</span> : null}
        {who ? <span className="puff-wk-who">{who}</span> : null}
      </span>
      <span className="puff-wk-n">{occ.title}</span>
    </button>
  );
}

/* ------------------------------------------------------------------ */
/*  Board                                                              */
/* ------------------------------------------------------------------ */

export default function WeekView({
  days,
  family,
  doc,
  lastUpdated,
  selectedMember,
  onOpenDay,
  onCompleteTask,
  onUndo,
}: Props) {
  const [occurrences, setOccurrences] = useState<Occurrence[]>([]);
  const [fallbackNote, setFallbackNote] = useState<string | null>(null);
  const [landedIds, setLandedIds] = useState<string[]>([]);
  const [landing, setLanding] = useState<Occurrence | null>(null);

  const from = days.length ? isoDay(days[0]) : "";
  const to = days.length ? isoDay(days[days.length - 1]) : "";
  const todayIso = isoDay(new Date());

  /** Previous occurrence ids, per window. Null means "nothing to diff yet". */
  const seenRef = useRef<{ window: string; ids: Set<string> } | null>(null);

  /* ---- Read the window ---- */

  useEffect(() => {
    if (!from || !to) return;
    let cancelled = false;

    (async () => {
      let next: Occurrence[] = [];
      let note: string | null = null;
      try {
        const res = await getCalendarOccurrences(from, to);
        next = enforceHealthRule(
          Array.isArray(res?.occurrences) ? (res.occurrences as Occurrence[]) : [],
        );
      } catch {
        // The route is new. A box that predates it 404s here, so expand the
        // document instead — non-recurring rows only — and say so if that
        // actually cost the household anything.
        const local = occurrencesFromDocument(doc, from, to);
        next = enforceHealthRule(local.occurrences);
        if (local.recurringSkipped > 0) {
          note =
            local.recurringSkipped === 1
              ? "1 recurring item needs the box update to show here"
              : `${local.recurringSkipped} recurring items need the box update to show here`;
        }
      }
      if (cancelled) return;

      // Diff against the last set for THIS window, so paging to another week
      // never reads as an arrival.
      const ids = new Set(next.map((o) => o.occurrence_id));
      const seen = seenRef.current;
      if (seen && seen.window === `${from}:${to}`) {
        const added = next.filter((o) => !seen.ids.has(o.occurrence_id));
        if (added.length > 0) {
          setLandedIds(added.map((o) => o.occurrence_id));
          setLanding(added[added.length - 1]);
        }
      }
      seenRef.current = { window: `${from}:${to}`, ids };

      setOccurrences(next);
      setFallbackNote(note);
    })();

    return () => {
      cancelled = true;
    };
    // `doc` is only read inside the fallback; `lastUpdated` is the signal that
    // the document changed at all, so it is the dependency that matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [from, to, lastUpdated]);

  /* ---- The strip clears itself; nothing here loops. ---- */
  useEffect(() => {
    if (!landing) return;
    const t = setTimeout(() => setLanding(null), 20000);
    return () => clearTimeout(t);
  }, [landing]);

  const settle = useCallback((id: string) => {
    setLandedIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : prev));
  }, []);

  /* ---- Person lookup ---- */

  const personOf = useMemo(() => {
    const map = new Map<string, { colour: string; who: string }>();
    family.forEach((m, i) => {
      map.set(m.id, { colour: personColor(i), who: (m.name || "").toUpperCase() });
    });
    return map;
  }, [family]);

  /**
   * The chip's paint. A `label_only` row gets NEITHER a person colour NOR a
   * person label: a person-tagged health row publishes a health-to-identity
   * link on a screen the whole room can see (hard rule).
   */
  const paintFor = (occ: Occurrence): { colour: string | null; who: string | null } => {
    // Second, independent refusal. enforceHealthRule already stripped the
    // person; this makes the rule true even for a row that reached here some
    // other way.
    if (occ.label_only || occ.kind === "med") return { colour: null, who: null };
    const p = occ.person ? personOf.get(occ.person) : undefined;
    return { colour: p?.colour ?? null, who: p?.who ?? null };
  };

  /* ---- Bucket the window ---- */

  const visible = useMemo(
    () =>
      selectedMember === "all"
        ? occurrences
        : occurrences.filter((o) => o.person === selectedMember),
    [occurrences, selectedMember],
  );

  const buckets = useMemo(() => {
    const map = new Map<string, Occurrence[]>();
    for (const occ of visible) {
      // A reminder and a task both belong to a rail, not to a day cell: the
      // week grid is what is HAPPENING, the rails are what is OUTSTANDING
      // Rendering a due task in both places would show it twice.
      if (occ.label_only || occ.kind === "task") continue;
      const key = `${occDay(occ)}|${occPart(occ)}`;
      const list = map.get(key);
      if (list) list.push(occ);
      else map.set(key, [occ]);
    }
    map.forEach((list) => list.sort((a, b) => (a.start < b.start ? -1 : 1)));
    return map;
  }, [visible]);

  const reminders = useMemo(
    () => visible.filter((o) => o.label_only).sort((a, b) => (a.start < b.start ? -1 : 1)),
    [visible],
  );

  /* ---- Rails ---- */

  const tasks = useMemo(() => {
    const all = (doc?.tasks ?? []).filter((t) => t.status !== "completed");
    const mine = selectedMember === "all" ? all : all.filter((t) => t.to === selectedMember);
    return [...mine].sort((a, b) => (a.due < b.due ? -1 : a.due > b.due ? 1 : 0));
  }, [doc?.tasks, selectedMember]);

  /**
   * Dinners, only when the document actually carries them.
   *
   * FreshTable owns this data and the cross-app read is not built, so on a real
   * box this is absent and the strip is simply not rendered — a placeholder
   * strip on a wall is a promise the product cannot keep.
   */
  const meals = doc?.mealPlan ?? [];
  const mealByDay = useMemo(() => {
    const map = new Map<string, string>();
    for (const m of meals) if (m?.day && m?.dish) map.set(m.day.slice(0, 10), m.dish);
    return map;
  }, [meals]);
  const showMeals = mealByDay.size > 0;

  const cols = days.length;
  const gridStyle = { ["--puff-wk-cols" as string]: String(cols) } as React.CSSProperties;

  const dueLabel = (due: string): string => {
    if (!due) return "";
    const day = due.slice(0, 10);
    if (day < todayIso) return "OVERDUE";
    if (day === todayIso) return "TODAY";
    const d = new Date(`${day}T00:00:00`);
    return Number.isNaN(d.getTime()) ? day : DOW_LABELS[(d.getDay() + 6) % 7];
  };

  /* ---- Render ---- */

  return (
    <div
      className={[
        "puff-wk",
        // Day view is the one layout whose extra row (the 7-day picker) the
        // page renders outside this surface, so the height constant has to
        // know about it. The strip, the note and the meals row are inside and
        // come out of the board on their own.
        days.length === 1 ? "puff-wk-dayview" : "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {landing ? (
        <div className="puff-wk-strip" role="status">
          <span className="puff-wk-verb">ADDED</span>
          <span
            className="puff-wk-fields"
            data-kind={landing.kind}
            data-who={paintFor(landing).who ? "1" : "0"}
          >
            {[
              // `THU 10` is one field, not two: the day and its number read as
              // a date, and splitting them made the strip look like a list.
              `${DOW_LABELS[(new Date(landing.start).getDay() + 6) % 7]} ${new Date(
                landing.start,
              ).getDate()}`,
              occTime(landing) || "ALL DAY",
              landing.title.toUpperCase(),
              paintFor(landing).who,
            ]
              .filter(Boolean)
              .join(" · ")}
          </span>
          {landing.kind === "med" ? null : (
            <button
              type="button"
              className="puff-tap puff-wk-undo"
              onClick={() => {
                onUndo(landing);
                setLanding(null);
              }}
            >
              UNDO
            </button>
          )}
          <button type="button" className="puff-tap" onClick={() => setLanding(null)}>
            Dismiss
          </button>
        </div>
      ) : null}

      {fallbackNote ? <div className="puff-wk-note">{fallbackNote}</div> : null}

      {/* Empty is the full week grid with no chips, one line saying what is
          missing and one utterance that fills it. Never a seed. */}
      {visible.length === 0 ? (
        <div className="puff-wk-emptyline">
          <span>Nothing on the calendar this week.</span>
          <span className="puff-wk-say">say &ldquo;add soccer Thursday at 4&rdquo;</span>
        </div>
      ) : null}

      <div className="puff-wk-board">
        <div className="puff-wk-left">
          <div className="puff-wk-card">
            <div className="puff-wk-grid" style={gridStyle}>
              <div className="puff-wk-hcell puff-wk-corner" />
              {days.map((d) => {
                const iso = isoDay(d);
                const isToday = iso === todayIso;
                return (
                  <button
                    type="button"
                    key={`h-${iso}`}
                    className={`puff-wk-hcell puff-wk-dayhead${isToday ? " puff-wk-today" : ""}`}
                    onClick={() => onOpenDay(d)}
                  >
                    <span className="puff-wk-dow">{DOW_LABELS[(d.getDay() + 6) % 7]}</span>
                    <span className="puff-wk-dnum">{d.getDate()}</span>
                    {isToday ? <span className="puff-wk-mark">TODAY</span> : null}
                  </button>
                );
              })}

              <div className="puff-wk-rowlab puff-wk-allrow">ALL DAY</div>
              {days.map((d) => {
                const iso = isoDay(d);
                const list = buckets.get(`${iso}|allday`) ?? [];
                return (
                  <div
                    key={`ad-${iso}`}
                    className={`puff-wk-cell puff-wk-allrow${
                      iso === todayIso ? " puff-wk-today" : ""
                    }`}
                  >
                    {list.map((occ) => {
                      const paint = paintFor(occ);
                      return (
                        <OccChip
                          key={occ.occurrence_id}
                          occ={occ}
                          colour={paint.colour}
                          who={paint.who}
                          landed={landedIds.includes(occ.occurrence_id)}
                          onSettled={settle}
                          onOpen={() => onOpenDay(d)}
                        />
                      );
                    })}
                  </div>
                );
              })}

              {DAY_PARTS.map((part) => (
                <React.Fragment key={part.key}>
                  <div className="puff-wk-rowlab">{part.label}</div>
                  {days.map((d) => {
                    const iso = isoDay(d);
                    const list = buckets.get(`${iso}|${part.key}`) ?? [];
                    return (
                      <div
                        key={`${part.key}-${iso}`}
                        className={`puff-wk-cell${iso === todayIso ? " puff-wk-today" : ""}`}
                      >
                        {list.map((occ) => {
                          const paint = paintFor(occ);
                          return (
                            <OccChip
                              key={occ.occurrence_id}
                              occ={occ}
                              colour={paint.colour}
                              who={paint.who}
                              landed={landedIds.includes(occ.occurrence_id)}
                              onSettled={settle}
                              onOpen={() => onOpenDay(d)}
                            />
                          );
                        })}
                      </div>
                    );
                  })}
                </React.Fragment>
              ))}
            </div>
          </div>

          {showMeals ? (
            <div className="puff-wk-card puff-wk-meals" style={gridStyle}>
              <div className="puff-wk-rowlab">DINNER</div>
              {days.map((d) => {
                const iso = isoDay(d);
                const dish = mealByDay.get(iso);
                return (
                  <div
                    key={`meal-${iso}`}
                    className={`puff-wk-cell${iso === todayIso ? " puff-wk-today" : ""}`}
                  >
                    <span className={dish ? "puff-wk-meal" : "puff-wk-meal puff-wk-meal-off"}>
                      {dish ?? "not planned"}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>

        <div className="puff-wk-rails">
          <section className="puff-wk-card puff-wk-rail">
            <h2 className="puff-cap puff-wk-railhead">
              TASKS<span className="puff-wk-ct">{tasks.length}</span>
            </h2>
            <div className="puff-wk-raillist">
              {tasks.length === 0 ? (
                <p className="puff-wk-quiet">Nothing outstanding.</p>
              ) : (
                tasks.map((t) => {
                  const p = t.to ? personOf.get(t.to) : undefined;
                  const label = dueLabel(t.due);
                  return (
                    <div
                      key={t.id}
                      className="puff-wk-railrow"
                      style={
                        p ? ({ ["--pc" as string]: p.colour } as React.CSSProperties) : undefined
                      }
                    >
                      <span className="puff-wk-railmain">
                        <span className="puff-wk-n">{t.title}</span>
                        <span className="puff-wk-railmeta">
                          {p ? <span className="puff-wk-who">{p.who}</span> : null}
                          <span className={label === "OVERDUE" ? "puff-wk-overdue" : "puff-wk-t"}>
                            {label}
                          </span>
                        </span>
                      </span>
                      <button
                        type="button"
                        className="puff-tap"
                        onClick={() => onCompleteTask(t.id)}
                        title="Mark done"
                        aria-label={`Mark done: ${t.title}`}
                      >
                        Done
                      </button>
                    </div>
                  );
                })
              )}
            </div>
          </section>

          <section className="puff-wk-card puff-wk-rail">
            <h2 className="puff-cap puff-wk-railhead">
              REMINDERS<span className="puff-wk-ct">{reminders.length}</span>
            </h2>
            <div className="puff-wk-raillist">
              {reminders.length === 0 ? (
                <p className="puff-wk-quiet">No reminders this week.</p>
              ) : (
                // Label and time only. No person colour, no person label
                // This is a hard rule, not a preference.
                reminders.map((r) => (
                  <div key={r.occurrence_id} className="puff-wk-railrow puff-wk-plain">
                    <span className="puff-wk-railmain">
                      <span className="puff-wk-n">{r.title}</span>
                    </span>
                    <span className="puff-wk-t">{occTime(r) || "ALL DAY"}</span>
                  </div>
                ))
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
