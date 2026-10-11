/**
 * Client-side recurrence expansion, with the `custom:` weekday SET as the
 * subject. Plain Node — this repo has no test runner, and expandEvent is pure.
 *
 *   npm run test:recurrence     (or: node app/_lib/__tests__/recurrence.test.mjs)
 *
 * Requires Node >=22.6 for native TypeScript type stripping, like the pairing
 * tests. Exits non-zero on any failure.
 *
 * What is being guarded: the backend writes "custom:mon,tue,thu,fri" for a
 * recurrence the fixed six cannot say. If `normalizeRule` fell through to
 * `default: return null`, the month grid and the home "Today" strip would treat
 * a four-mornings-a-week practice as a single non-recurring event, shown
 * on ONE day. The week view is unaffected because it reads the server's
 * /apps/calendar/occurrences, which expands the same value server-side.
 *
 * Lives under app/_lib/ (underscore-private), so Next never routes it.
 */
import assert from "node:assert/strict";

const { expandEvent, expandEvents, customDayCodes, customRecurrenceLabel } =
  await import("../recurrence.ts");

let pass = 0;
let fail = 0;

function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    fail += 1;
    console.error(`FAIL  ${name}\n      ${err.message}`);
  }
}

const PRACTICE = {
  id: "e1",
  title: "Soccer practice",
  start: "2026-09-10T06:15",
  end: "2026-09-10T07:15",
  rec: "custom:mon,tue,thu,fri",
};

const days = (occurrences) =>
  occurrences.map((o) => {
    const d = new Date(o.start);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
      d.getDate(),
    ).padStart(2, "0")}`;
  });

check("a Mon, Tue, Thu, Fri set lands on four days of the week of Sep 14", () => {
  const out = expandEvent(
    PRACTICE,
    new Date(2026, 8, 14, 0, 0, 0),
    new Date(2026, 8, 20, 23, 59, 59),
  );
  assert.deepEqual(days(out), [
    "2026-09-14",
    "2026-09-15",
    "2026-09-17",
    "2026-09-18",
  ]);
});

check("every occurrence keeps the 6:15 start and the one-hour duration", () => {
  const out = expandEvent(
    PRACTICE,
    new Date(2026, 8, 14, 0, 0, 0),
    new Date(2026, 8, 20, 23, 59, 59),
  );
  for (const occ of out) {
    const start = new Date(occ.start);
    const end = new Date(occ.end);
    assert.equal(start.getHours(), 6);
    assert.equal(start.getMinutes(), 15);
    assert.equal(end.getTime() - start.getTime(), 60 * 60 * 1000);
    assert.equal(occ.title, PRACTICE.title);
  }
});

check("a single-day custom set repeats weekly, not once", () => {
  const out = expandEvent(
    { ...PRACTICE, rec: "custom:tue" },
    new Date(2026, 8, 14, 0, 0, 0),
    new Date(2026, 8, 28, 23, 59, 59),
  );
  assert.equal(out.length, 2);
  for (const occ of out) assert.equal(new Date(occ.start).getDay(), 2);
});

check("an unreadable custom value degrades to the base occurrence", () => {
  const out = expandEvent(
    { ...PRACTICE, rec: "custom:someday" },
    new Date(2026, 8, 1),
    new Date(2026, 9, 1),
  );
  assert.equal(out.length, 1, "wrong days are worse than one day");
});

check("the legacy vocabulary is untouched", () => {
  const weekdays = expandEvents(
    [{ id: "e2", title: "W", start: "2026-09-14T09:00", rec: "weekdays" }],
    new Date(2026, 8, 14, 0, 0, 0),
    new Date(2026, 8, 20, 23, 59, 59),
  );
  assert.equal(weekdays.length, 5);
  const weekly = expandEvents(
    [{ id: "e3", title: "W", start: "2026-09-15T09:00", rec: "weekly" }],
    new Date(2026, 8, 14, 0, 0, 0),
    new Date(2026, 8, 28, 23, 59, 59),
  );
  assert.equal(weekly.length, 2); // Sep 15 and Sep 22; Sep 29 is past the range
  const none = expandEvents(
    [{ id: "e4", title: "W", start: "2026-09-15T09:00", rec: null }],
    new Date(2026, 8, 14, 0, 0, 0),
    new Date(2026, 8, 20, 23, 59, 59),
  );
  assert.equal(none.length, 1);
});

check("day codes come back in week order, whatever order they were said in", () => {
  assert.deepEqual(customDayCodes("custom:fri,tue,thu,mon"), [
    "MO",
    "TU",
    "TH",
    "FR",
  ]);
  assert.deepEqual(customDayCodes("custom:SUN, mon"), ["MO", "SU"]);
  assert.equal(customDayCodes("weekly"), null);
  assert.equal(customDayCodes(null), null);
  assert.equal(customDayCodes("custom:nonsense"), null);
});

check("a weekday set has a label a person can read in the picker", () => {
  assert.equal(
    customRecurrenceLabel("custom:mon,tue,thu,fri"),
    "Custom days (Mon, Tue, Thu, Fri)",
  );
  assert.equal(customRecurrenceLabel("weekdays"), null);
  assert.equal(customRecurrenceLabel(""), null);
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
