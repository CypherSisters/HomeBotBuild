/**
 * Honest classification of a `/automation/status` answer.
 *
 * Collapsing every failure into `connected: false` and printing "Offline"
 * would blame Node-RED for problems that are not Node-RED's: an unpaired browser gets 401 {"error":"pairing_required"} from
 * the proxy, and a restarting dashboard service refuses the connection
 * outright. Those are three different things to tell the owner.
 *
 * This file is deliberately import-free (types only) so it can be unit-tested
 * with plain Node type stripping, like the other pure modules here.
 */
import type { AutomationStatus } from "./automationService";

export type EngineState =
  | "connected"
  | "engine_offline"
  | "pairing_required"
  | "unreachable";

export interface EngineStatusResult {
  state: EngineState;
  /** Backend-supplied reason, when there is one worth showing. */
  detail?: string;
  /** The parsed body, present whenever the backend answered 200 with one. */
  status?: AutomationStatus;
}

/**
 * `raw` is `null` when the request never produced an HTTP response (network
 * error, abort, timeout).
 *
 *   401 + pairing_required    -> pairing_required (this browser, not the engine)
 *   any other non-2xx         -> unreachable      (backend/proxy problem)
 *   2xx without a usable body -> unreachable      (non-JSON, wrong shape)
 *   2xx connected:false       -> engine_offline   (Node-RED really is down)
 *   2xx connected:true        -> connected
 */
export function classifyEngineStatus(
  raw: { status: number; ok: boolean; data: unknown } | null
): EngineStatusResult {
  if (!raw) return { state: "unreachable" };

  const body =
    raw.data && typeof raw.data === "object"
      ? (raw.data as Partial<AutomationStatus> & { error?: string })
      : null;
  const error = typeof body?.error === "string" ? body.error : undefined;

  if (raw.status === 401 || raw.status === 403) {
    if ((error || "").toLowerCase().includes("pair")) {
      return { state: "pairing_required", detail: error };
    }
    return { state: "unreachable", detail: error || `HTTP ${raw.status}` };
  }
  if (!raw.ok) return { state: "unreachable", detail: error || `HTTP ${raw.status}` };
  if (!body || typeof body.connected !== "boolean") {
    return { state: "unreachable", detail: error };
  }
  if (!body.connected) return { state: "engine_offline", detail: error };
  return { state: "connected", status: body as AutomationStatus };
}
