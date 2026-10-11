/**
 * Home Assistant service layer.
 *
 * All calls now go through the PuffPAI API server (ha_bridge) at
 * the same-origin /api/pai proxy instead of directly to Home Assistant.
 * This ensures a single source of truth for HA communication.
 *
 * puff puff pass
 */

const API_BASE = "/api/pai";
const API_KEY = "";

export interface HAEntity {
  entity_id: string;
  state: string;
  attributes: Record<string, any>;
  last_changed: string;
  last_updated: string;
}

export interface HADevice {
  entity_id: string;
  domain: string;
  friendly_name: string;
  state: string;
  is_on: boolean;
  area_name: string | null;
  attributes: Record<string, any>;
}

export interface HARoom {
  area_id: string;
  name: string;
  device_count: number;
  devices: HADevice[];
}

export interface HAAutomation {
  entity_id: string;
  friendly_name: string;
  state: string;
  last_triggered: string | null;
  mode: string | null;
}

export interface HADomainGroup {
  domain: string;
  entities: HAEntity[];
  count: number;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function headers(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (API_KEY) h["Authorization"] = `Bearer ${API_KEY}`;
  return h;
}

async function apiGet(path: string) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: headers(),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function apiPost(path: string, body: any) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

export async function getHAStatus(): Promise<{ connected: boolean; ha_url: string; ha_version?: string }> {
  return apiGet("/ha/status");
}

// ---------------------------------------------------------------------------
// Entities (raw HA states — backward compat)
// ---------------------------------------------------------------------------

export async function getHAEntities(): Promise<{ entities: HAEntity[]; count: number; domains: Record<string, number> }> {
  return apiGet("/ha/entities");
}

export async function getHAEntity(entityId: string): Promise<{ entity: HADevice }> {
  return apiGet(`/ha/entity/${entityId}`);
}

// ---------------------------------------------------------------------------
// Devices (normalised, controllable)
// ---------------------------------------------------------------------------

export async function getHADevices(domain?: string, query?: string): Promise<{ devices: HADevice[]; count: number }> {
  const params = new URLSearchParams();
  if (domain) params.set("domain", domain);
  if (query) params.set("q", query);
  const qs = params.toString();
  return apiGet(`/ha/devices${qs ? `?${qs}` : ""}`);
}

// ---------------------------------------------------------------------------
// Device management (overview + approval workflow)
// ---------------------------------------------------------------------------

export interface HAOverviewDevice {
  entity_id: string;
  name: string;
  domain: string;
  state: string;
  managed: boolean;
  is_new: boolean;
  security_mode: string | null;
  first_seen: string | null;
}

export interface HADeviceOverview {
  success: boolean;
  devices: HAOverviewDevice[];
  counts: { total: number; managed: number; unmanaged: number; new: number };
}

export async function getHADeviceOverview(): Promise<HADeviceOverview> {
  return apiGet("/ha/devices/overview");
}

export async function approvePendingDevice(
  entityId: string,
  securityMode: "balanced" | "lockdown" | "open" = "balanced",
): Promise<any> {
  return apiPost(`/devices/pending/${entityId}/approve`, { security_mode: securityMode });
}

export async function rejectPendingDevice(entityId: string): Promise<any> {
  return apiPost(`/devices/pending/${entityId}/reject`, {});
}

// ---------------------------------------------------------------------------
// Device control
// ---------------------------------------------------------------------------

export async function controlDevice(entityId: string, action: string, extra?: Record<string, any>): Promise<any> {
  return apiPost("/ha/control", { entity_id: entityId, action, ...extra });
}

export async function setLight(entityId: string, opts: { brightness_pct?: number; color_temp?: string | number; rgb_color?: number[] }): Promise<any> {
  return apiPost("/ha/light", { entity_id: entityId, ...opts });
}

export async function setClimate(entityId: string, opts: { temperature?: number; hvac_mode?: string }): Promise<any> {
  return apiPost("/ha/climate", { entity_id: entityId, ...opts });
}

export async function callHAService(domain: string, service: string, data: any): Promise<any> {
  return apiPost(`/ha/service/${domain}/${service}`, data);
}

// ---------------------------------------------------------------------------
// Rooms
// ---------------------------------------------------------------------------

export async function getHARooms(): Promise<{ rooms: HARoom[]; count: number }> {
  return apiGet("/ha/rooms");
}

// ---------------------------------------------------------------------------
// Automations
// ---------------------------------------------------------------------------

export async function getHAAutomations(): Promise<HAAutomation[]> {
  const data = await apiGet("/ha/automations");
  return data.automations ?? [];
}

export async function createAutomation(config: Record<string, any>): Promise<any> {
  return apiPost("/ha/automations", config);
}

export async function triggerAutomation(entityId: string): Promise<any> {
  return apiPost(`/ha/automations/${entityId}/trigger`, {});
}

export async function toggleAutomation(entityId: string, enable: boolean): Promise<any> {
  return apiPost(`/ha/automations/${entityId}/toggle`, { enable });
}

// ---------------------------------------------------------------------------
// Scenes & Scripts
// ---------------------------------------------------------------------------

export async function getHAScenes(): Promise<any[]> {
  const data = await apiGet("/ha/scenes");
  return data.scenes ?? [];
}

export async function activateScene(entityId: string): Promise<any> {
  return apiPost(`/ha/scenes/${entityId}/activate`, {});
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

export async function getHAHistory(entityId: string, start?: string, end?: string): Promise<any> {
  const params = new URLSearchParams();
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  const qs = params.toString();
  return apiGet(`/ha/history/${entityId}${qs ? `?${qs}` : ""}`);
}

// ---------------------------------------------------------------------------
// Camera
// ---------------------------------------------------------------------------

export function getHACameraImage(entityId: string): string {
  return `${API_BASE}/ha/camera/${entityId}?t=${Date.now()}`;
}
