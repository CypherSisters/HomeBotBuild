/**
 * Home blueprint service: the house as rooms, devices and a saved floorplan.
 *
 * Two backend surfaces, both PuffPAI's own (nothing here talks to Home
 * Assistant directly, and nothing here writes to HA):
 *
 *   GET  /ha/home          resolved devices + areas + floors + live entity state
 *   GET  /ha/home/layout   the owner's floorplan (rooms, geometry, placements)
 *   PUT  /ha/home/layout   save it back
 *
 * Control is deliberately NOT a new route. Everything actuating goes through
 * the paths that already exist and are already gated: `/ha/control` and
 * `/ha/service/<domain>/<service>` via haConfirmService (which owns the 409 +
 * ticket dance for the safety-critical domains), and `/ha/light` `/ha/climate`
 * via haService for the two shaped endpoints.
 *
 * The page never calls any of this directly. It calls a HomeDriver
 * (`liveDriver` below), so the UI stays independent of the endpoints.
 *
 * puff puff pass
 */

import { apiGetRaw, apiPut } from "../api";
import { setLight, setClimate } from "./haService";
import {
  attemptHaRequest,
  mintHaTicket,
  type HaTicketProposal,
} from "./haConfirmService";

/* ── Contract types (mirror the backend /ha/home payload) ─────────── */

export type HomeKind =
  | "light"
  | "switch"
  | "climate"
  | "media"
  | "camera"
  | "lock"
  | "cover"
  | "sensor"
  | "hub"
  | "other";

export type HomeTier = "local" | "cloud" | "wifi_direct" | "unknown" | "system";

export interface HomeEntity {
  entity_id: string;
  domain: string;
  name: string;
  state: string;
  attributes: Record<string, any>;
  controllable: boolean;
  safety_critical: boolean;
}

export interface HomeDevice {
  /** HA device id, or `entity:<entity_id>` for device-less entities. */
  device_id: string;
  name: string;
  manufacturer: string;
  model: string;
  area_id: string | null;
  area_source: "ha" | "layout" | "none";
  kind: HomeKind;
  primary_entity_id: string | null;
  integration: string;
  tier: HomeTier;
  bridge_key: string | null;
  entities: HomeEntity[];
}

export interface HomeArea {
  area_id: string;
  name: string;
  floor_id: string | null;
  source: string;
}

export interface HomeFloor {
  floor_id: string;
  name: string;
  level: number;
}

export interface HomeRegistry {
  ok: boolean;
  method: string;
  /** null until the first registry pull has happened. */
  age_seconds: number | null;
}

export interface HomePayload {
  success: boolean;
  generated_at: string;
  registry: HomeRegistry;
  floors: HomeFloor[];
  areas: HomeArea[];
  devices: HomeDevice[];
  /** What the resolver left out of `devices`. Absent on older boxes. */
  hidden?: { non_physical: number; system: number };
}

/* ── Traffic (GET /ha/home/traffic) ──────────────────────────────────── */

/**
 * One domain a device looked up.
 *
 * There is a category and a count here and nothing else. The allowed /
 * monitored / blocked verdicts exist ONLY per device: AdGuard reports what it
 * did per client, not per name, and inventing a per-domain verdict from a
 * category would be a lie the owner would act on.
 */
export interface TrafficDomain {
  domain: string;
  count: number;
  /** TELEMETRY, ADS, TRACKING, other, or whatever the backend adds next. */
  category: string;
}

export interface TrafficRow {
  bridge_key: string;
  ip: string;
  allowed: number;
  blocked: number;
  monitored: number;
  total: number;
  last_seen: string;
  /** At most 8, biggest first. */
  top_domains: TrafficDomain[];
}

/** A LAN address with traffic that is not one of the house's HA devices. */
export interface LanOnlyRow extends TrafficRow {
  key: string;
  display_name: string;
  category: string;
  manufacturer: string;
  hostname: string;
  is_gateway: boolean;
  is_self: boolean;
}

export interface TrafficVisibility {
  adguard_reachable: boolean;
  queries_today: number;
  protection_enabled: boolean;
  enforcing: boolean;
  mode: string;
  /** False means the router is not pointing at Puff's DNS. Draw nothing. */
  seeing_traffic: boolean;
}

export interface HomeTraffic {
  success: boolean;
  hours: number;
  generated_at: string;
  visibility: TrafficVisibility;
  /** Keyed by home device_id. Only devices that have a bridge_key appear. */
  devices: Record<string, TrafficRow>;
  lan_only: LanOnlyRow[];
}

/**
 * Traffic never throws.
 *
 * The route is newer than the rest of the map and may simply not be on the
 * box, which is a state the layer draws rather than an error the page shows.
 */
export type TrafficResult =
  | { status: "ok"; traffic: HomeTraffic }
  | { status: "unavailable"; message: string }
  | { status: "error"; message: string };

export const TRAFFIC_UNAVAILABLE = "Traffic needs the traffic service. Not on this box yet.";

/** Room geometry is integer grid units, 1 unit is about a foot, 0 to 200. */
export interface HomeLayoutRoom {
  area_id: string;
  name: string;
  floor_id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  /** PuffPAI-only room (area_id prefixed `puff_`), not an HA area. */
  custom: boolean;
}

/** Position inside its room, 0 to 1 on each axis. */
export interface HomePlacement {
  area_id: string;
  x: number;
  y: number;
}

export interface HomeLayout {
  version: number;
  floors: HomeFloor[];
  rooms: HomeLayoutRoom[];
  placements: Record<string, HomePlacement>;
  updated_at?: string;
  /** false on a fresh box: the dashboard seeds a schematic from the areas. */
  seeded?: boolean;
}

export const EMPTY_LAYOUT: HomeLayout = {
  version: 1,
  floors: [],
  rooms: [],
  placements: {},
  seeded: false,
};

/* ── Control intents ─────────────────────────────────────────────────── */

/**
 * What the map can ask for, in the map's own vocabulary.
 *
 * A driver turns one of these into whichever existing endpoint is right. The
 * UI never names an endpoint.
 */
export type ControlIntent =
  | {
      kind: "toggle";
      entityId: string;
      action: "turn_on" | "turn_off" | "toggle";
    }
  | { kind: "brightness"; entityId: string; pct: number }
  | {
      kind: "climate";
      entityId: string;
      temperature?: number;
      hvac_mode?: string;
    }
  | {
      kind: "service";
      entityId: string;
      domain: string;
      service: string;
      data?: Record<string, any>;
    };

export type ControlOutcome =
  | { status: "ok" }
  /** The backend refused without a ticket. Open the confirm sheet. */
  | { status: "needs_confirmation"; message: string }
  | { status: "error"; message: string };

export type TicketOutcome =
  | { ok: true; ticket: string; proposal: HaTicketProposal }
  | { ok: false; message: string };

/** Thrown by `getHome()` when the route is not on this box yet. */
export class HomeUnavailableError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "HomeUnavailableError";
    this.status = status;
  }
}

export interface HomeDriver {
  getHome(): Promise<HomePayload>;
  /** Never rejects: a missing route comes back as `unavailable`. */
  getTraffic(hours: number): Promise<TrafficResult>;
  getLayout(): Promise<HomeLayout>;
  saveLayout(layout: HomeLayout): Promise<void>;
  /** Run an intent, optionally redeeming a ticket minted for it. */
  control(intent: ControlIntent, ticket?: string): Promise<ControlOutcome>;
  /** Mint a ticket for an intent. Does NOT actuate anything. */
  requestTicket(intent: ControlIntent): Promise<TicketOutcome>;
}

/* ── Live driver ─────────────────────────────────────────────────────── */

export async function getHome(): Promise<HomePayload> {
  const { status, ok, data } = await apiGetRaw<HomePayload>("/ha/home");
  if (status === 404) {
    throw new HomeUnavailableError(404, "The home map service is not on this box.");
  }
  if (!ok || !data) {
    const message =
      (data && typeof (data as any).error === "string" && (data as any).error) ||
      `Could not read the house (${status}).`;
    throw new Error(message);
  }
  return data;
}

export async function getTraffic(hours: number): Promise<TrafficResult> {
  try {
    const { status, ok, data } = await apiGetRaw<HomeTraffic>(
      `/ha/home/traffic?hours=${encodeURIComponent(String(hours))}`,
    );
    if (status === 404) return { status: "unavailable", message: TRAFFIC_UNAVAILABLE };
    if (!ok || !data || !data.visibility)
      return { status: "error", message: `Could not read traffic (${status}).` };
    return {
      status: "ok",
      traffic: {
        ...data,
        devices: data.devices ?? {},
        lan_only: data.lan_only ?? [],
      },
    };
  } catch (err: any) {
    return { status: "error", message: err?.message || "Could not read traffic." };
  }
}

export async function getLayout(): Promise<HomeLayout> {
  const { status, ok, data } = await apiGetRaw<HomeLayout>("/ha/home/layout");
  if (status === 404) {
    throw new HomeUnavailableError(404, "The home map service is not on this box.");
  }
  if (!ok || !data) throw new Error(`Could not read the floorplan (${status}).`);
  return {
    version: data.version ?? 1,
    floors: data.floors ?? [],
    // The store accepts a room with no floor; the sheet cannot draw one, so it
    // lands on the first floor rather than vanishing.
    rooms: (data.rooms ?? []).map((r) => ({
      ...r,
      floor_id: r.floor_id || data.floors?.[0]?.floor_id || "ground",
    })),
    placements: data.placements ?? {},
    updated_at: data.updated_at,
    seeded: data.seeded,
  };
}

export async function saveLayout(layout: HomeLayout): Promise<void> {
  await apiPut("/ha/home/layout", {
    version: layout.version ?? 1,
    floors: layout.floors,
    rooms: layout.rooms,
    placements: layout.placements,
  });
}

/**
 * Intent to endpoint.
 *
 * Toggles and media transport ride `attemptHaRequest`, which is the only path
 * that can carry a ticket, so a lock or a cover reaches the ticket flow by
 * construction. Brightness and climate keep their shaped endpoints; neither
 * domain is safety-critical, so neither can need a ticket.
 */
export async function control(
  intent: ControlIntent,
  ticket?: string,
): Promise<ControlOutcome> {
  try {
    if (intent.kind === "brightness") {
      await setLight(intent.entityId, { brightness_pct: intent.pct });
      return { status: "ok" };
    }
    if (intent.kind === "climate") {
      await setClimate(intent.entityId, {
        temperature: intent.temperature,
        hvac_mode: intent.hvac_mode,
      });
      return { status: "ok" };
    }
  } catch (err: any) {
    return { status: "error", message: err?.message || "The call was refused." };
  }

  const attempt = await attemptHaRequest(
    intent.kind === "toggle"
      ? { kind: "control", entityId: intent.entityId, action: intent.action }
      : {
          kind: "service",
          domain: intent.domain,
          service: intent.service,
          entityId: intent.entityId,
          data: intent.data,
        },
    ticket,
  );
  if (attempt.kind === "ok") return { status: "ok" };
  if (attempt.kind === "needs_confirmation")
    return { status: "needs_confirmation", message: attempt.message };
  return { status: "error", message: attempt.message };
}

export async function requestTicket(intent: ControlIntent): Promise<TicketOutcome> {
  if (intent.kind === "brightness" || intent.kind === "climate") {
    return { ok: false, message: "This action does not take a confirmation." };
  }
  const minted = await mintHaTicket(
    intent.kind === "toggle"
      ? { kind: "control", entity_id: intent.entityId, action: intent.action }
      : {
          kind: "service",
          entity_id: intent.entityId,
          action: `${intent.domain}.${intent.service}`,
        },
  );
  if (!minted.ok) return { ok: false, message: minted.message };
  return { ok: true, ticket: minted.result.ticket, proposal: minted.result.proposal };
}

export const liveDriver: HomeDriver = {
  getHome,
  getTraffic,
  getLayout,
  saveLayout,
  control,
  requestTicket,
};
