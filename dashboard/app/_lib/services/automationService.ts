/**
 * Automation workflow service - talks to the backend's workflow-engine
 * endpoints. The engine is Node-RED (default http://localhost:1880).
 */
import { apiGet, apiGetRaw, apiPost } from "../api";
import { classifyEngineStatus, type EngineStatusResult } from "./engineStatus";

export interface AutomationStatus {
  success: boolean;
  connected: boolean;
  base_url: string;
  webhooks: Record<string, string>;
  error?: string;
}

export interface AutomationTriggerResult {
  success: boolean;
  data?: any;
  error?: string;
}

/** Predefined workflow definitions for the dashboard */
export interface WorkflowDef {
  id: string;
  name: string;
  description: string;
  webhookName: string;
  icon: string; // MDI icon path
  color: string; // Tailwind color class
  category: "automation" | "security" | "data" | "voice";
  /** Default payload when triggered manually */
  defaultPayload?: Record<string, any>;
}

/** One live Node-RED flow (a "tab" in the runtime), from GET /workflows. */
export interface WorkflowFlow {
  id: string;
  name: string;
  active: boolean;
}

export interface WorkflowFlowList {
  success: boolean;
  total?: number;
  active?: number;
  inactive?: number;
  workflows?: WorkflowFlow[];
  error?: string;
}

export async function getAutomationStatus(): Promise<AutomationStatus> {
  return apiGet<AutomationStatus>("/automation/status");
}

/* Engine status: see ./engineStatus for the classifier, which keeps
 * "engine offline" apart from pairing and network failures. */
export { classifyEngineStatus } from "./engineStatus";
export type { EngineState, EngineStatusResult } from "./engineStatus";

/** Ask the backend where the automation engine stands, without throwing. */
export async function getEngineStatus(): Promise<EngineStatusResult> {
  try {
    const raw = await apiGetRaw<unknown>("/automation/status", {
      signal: AbortSignal.timeout(8000),
    });
    return classifyEngineStatus(raw);
  } catch {
    // fetch rejected: offline, DNS, connection refused (service restarting),
    // or our own 8 s deadline.
    return classifyEngineStatus(null);
  }
}

/**
 * The live list of every Node-RED flow in the runtime. Backed by
 * GET /workflows (Node-RED admin /flows). Degrades to an empty list if the
 * request fails, so the page still renders the known trigger cards.
 */
export async function getWorkflowFlows(): Promise<WorkflowFlowList> {
  try {
    return await apiGet<WorkflowFlowList>("/workflows");
  } catch {
    return { success: false, workflows: [] };
  }
}

export async function triggerWorkflow(
  webhookName: string,
  payload: Record<string, any> = {}
): Promise<AutomationTriggerResult> {
  return apiPost<AutomationTriggerResult>(`/automation/trigger/${webhookName}`, payload);
}
