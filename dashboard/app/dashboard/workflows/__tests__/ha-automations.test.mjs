/**
 * Workflows page: Home Assistant automations (read-only).
 * Run: node app/dashboard/workflows/__tests__/ha-automations.test.mjs
 *
 * puff puff pass
 */
import assert from "node:assert/strict";

const { selectHaAutomations, lastRunLabel } = await import("../haAutomationRows.ts");

let pass = 0;
let fail = 0;
function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log(`  ok  ${name}`);
  } catch (err) {
    fail += 1;
    console.error(`  FAIL  ${name}\n${err.stack}`);
  }
}

const NOW = Date.parse("2026-03-14T22:00:00Z");
const ENTITIES = [
  { entity_id: "automation.porch_motion_lights", state: "on",
    attributes: { friendly_name: "Porch Motion Lights",
                  last_triggered: "2026-03-14T21:00:00Z" } },
  { entity_id: "automation.puffpai_porch_1_porch_2_on_30_min_before_sunset", state: "on",
    attributes: { friendly_name: "PuffPAI Porch 1 Porch 2 on 30 min before sunset",
                  last_triggered: null } },
  { entity_id: "automation.puffpai_porch_1_porch_2_off_after_midnight_no_motion", state: "off",
    attributes: { friendly_name: "PuffPAI Porch 1 Porch 2 off after midnight no motion",
                  last_triggered: "2026-03-14T21:58:00Z" } },
  { entity_id: "light.porch_1", state: "on", attributes: {} },
];

check("EVERY automation is listed, with name/state/last run", () => {
  const { rows, madeByPuffCount } = selectHaAutomations(ENTITIES, NOW);
  assert.equal(madeByPuffCount, 2);
  assert.deepEqual(rows.map((r) => [r.name, r.stateLabel, r.lastRun, r.madeByPuffpai]), [
    ["Porch Motion Lights", "On", "1 h ago", false],
    ["PuffPAI Porch 1 Porch 2 off after midnight no motion", "Off", "2 min ago", true],
    ["PuffPAI Porch 1 Porch 2 on 30 min before sunset", "On", "never", true],
  ]);
});

check("non-automation entities never appear", () => {
  const { rows } = selectHaAutomations(ENTITIES, NOW);
  assert.ok(!rows.some((r) => r.entityId.startsWith("light.")));
});

check("unknown state reads as Unavailable; bad dates read as never", () => {
  const { rows } = selectHaAutomations(
    [{ entity_id: "automation.puffpai_x", state: "unavailable",
       attributes: { last_triggered: "nope" } }], NOW);
  assert.equal(rows[0].stateLabel, "Unavailable");
  assert.equal(rows[0].lastRun, "never");
  assert.equal(lastRunLabel(undefined, NOW), "never");
  assert.equal(lastRunLabel("2026-03-12T22:00:00Z", NOW), "2 d ago");
});

check("empty or missing input is safe", () => {
  assert.deepEqual(selectHaAutomations(null, NOW), { rows: [], madeByPuffCount: 0 });
});

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
