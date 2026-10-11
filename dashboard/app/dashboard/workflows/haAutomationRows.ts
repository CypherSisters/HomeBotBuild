/**
 * Home Assistant automations for the Workflows page (read-only).
 *
 * The page lists every automation, even ones that do not use Node-RED.
 * These helpers pick EVERY `automation.*` entity
 * out of the existing read-only `/ha/entities` read and mark the ones PuffPAI
 * made (`automation.puffpai_*`). Nothing here writes anything.
 *
 * puff puff pass
 */

export interface HaEntityLike {
  entity_id: string;
  state: string;
  attributes?: Record<string, unknown>;
}

export interface HaAutomationRow {
  entityId: string;
  name: string;
  on: boolean;
  stateLabel: string;
  lastRun: string;
  madeByPuffpai: boolean;
}

export const PUFFPAI_PREFIX = "automation.puffpai_";

/** "never", "just now", "5 min ago", "3 h ago", "2 d ago". */
export function lastRunLabel(iso: unknown, now: number = Date.now()): string {
  if (typeof iso !== "string" || !iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "never";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function selectHaAutomations(
  entities: HaEntityLike[] | null | undefined,
  now: number = Date.now(),
): { rows: HaAutomationRow[]; madeByPuffCount: number } {
  const autos = (entities || []).filter(
    (e) => typeof e?.entity_id === "string" && e.entity_id.startsWith("automation."),
  );
  const mine = (e: HaEntityLike) => e.entity_id.startsWith(PUFFPAI_PREFIX);
  const madeByPuffCount = autos.filter(mine).length;
  const rows = autos
    .map((e) => {
      const attrs = e.attributes || {};
      const on = e.state === "on";
      return {
        entityId: e.entity_id,
        name: String(attrs.friendly_name || e.entity_id),
        on,
        stateLabel: on ? "On" : e.state === "off" ? "Off" : "Unavailable",
        lastRun: lastRunLabel(attrs.last_triggered, now),
        madeByPuffpai: mine(e),
      };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
  return { rows, madeByPuffCount };
}
