// First-run setup service — the browser face of the backend's /setup/* routes.
//
// EVERY call goes through the same-origin proxy at /api/pai, which attaches the
// appliance's backend key server-side. The browser therefore sends no
// credential of its own, and nothing here ever puts a URL to the backend in the
// bundle (the base is a relative path on purpose — a LAN phone and the kiosk
// resolve it identically).
//
// Nothing in this module writes to localStorage, sessionStorage, a store, a URL
// or the console. The vault PIN, the recovery code and the Home Assistant token
// are arguments that are POSTed and then forgotten; they live only in the
// component state of the step that asked for them.
//
// Every call resolves — it never throws — so a step can render "the backend is
// not answering" instead of tripping an unhandled rejection.

/** Same-origin proxy. Relative by design: no backend host in the bundle. */
const BASE = "/api/pai";

/**
 * The first-run setup code, for as long as this page is open.
 *
 * WHY IT IS NEEDED. Before setup completes, the backend's own post-completion
 * gate has nothing to refuse, and this proxy attaches the appliance's API key
 * server-side — so without a code any device that can load the dashboard could
 * run the whole wizard, including `POST /setup/vault`, whose reply carries the
 * vault RECOVERY CODE. The backend requires `X-Setup-Code` on every
 * state-changing /setup/* call whose client is not the machine itself, and the
 * code is printed only on that machine's console (`puffpai setup-code`).
 *
 * WHERE IT LIVES. A module variable, written only by `setSetupCode` from the
 * wizard's component state, which is the authoritative copy. Memory only: it is
 * never put in localStorage, sessionStorage, a cookie, a store or the URL, is
 * never logged, and dies with the tab. The wizard clears it on unmount and
 * after completion.
 */
let setupCode = "";

/** Set (or clear, with "") the code attached to state-changing setup calls. */
export function setSetupCode(code: string): void {
  setupCode = typeof code === "string" ? code.trim() : "";
}

/**
 * The header block for a call the backend's first-run gate also covers.
 *
 * The gate is not only about `/setup/*`: `/wizard/*` and `/build/prove*`
 * are the same build surface under other names, and they are refused the same
 * way while first-run setup is open. Those two services own their own `fetch`,
 * so they ask for the header here rather than each keeping a second copy of
 * the code — one copy, in memory, cleared by the wizard on unmount.
 *
 * Empty object when nothing has been typed: an absent header is what a local
 * caller (the CLI, a curl on the box) sends, and the backend grades it.
 */
export function setupCodeHeaders(): Record<string, string> {
  return setupCode ? { "X-Setup-Code": setupCode } : {};
}

export interface Reply<T = any> {
  /** Transport-level success (HTTP 2xx) AND not an `{ok:false}` body. */
  ok: boolean;
  status: number;
  data: T;
  /** Plain-language failure line, empty when ok. */
  error: string;
}

/** The backend answers either `{ok:false,error,message}` or a bare payload. */
interface Envelope {
  ok?: boolean;
  error?: string;
  message?: string;
  detail?: unknown;
}

function errorLine(status: number, data: Envelope): string {
  if (status === 0) return "Cannot reach the backend.";
  if (status === 401) return "This screen has not been paired with the household yet.";
  const text =
    (typeof data?.message === "string" && data.message) ||
    (typeof data?.error === "string" && data.error) ||
    (typeof data?.detail === "string" && data.detail) ||
    "";
  if (text) return text;
  if (status === 502 || status === 504) return "The backend is not answering.";
  return `The backend returned HTTP ${status}.`;
}

async function req<T = any>(
  path: string,
  method: "GET" | "POST" = "GET",
  body?: unknown,
): Promise<Reply<T>> {
  let res: Response;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  // Sent on every call once the owner has typed it — including GETs, which cost
  // nothing and keep the header from being a per-route thing to remember.
  if (setupCode) headers["X-Setup-Code"] = setupCode;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    // The dev/prod server answered nothing at all (backend down before the
    // proxy could reply, or the network dropped). Status 0 = "backend down".
    return { ok: false, status: 0, data: {} as T, error: errorLine(0, {}) };
  }
  let data: any = {};
  try {
    data = await res.json();
  } catch {
    /* empty or non-JSON body */
  }
  const ok = res.ok && data?.ok !== false;
  return {
    ok,
    status: res.status,
    data: data as T,
    error: ok ? "" : errorLine(res.status, data || {}),
  };
}

/** True when the backend itself could not be reached (not a validation error). */
export function isBackendDown(reply: Reply): boolean {
  return reply.status === 0 || reply.status === 502 || reply.status === 504;
}

/**
 * Some routes answer `{ok:true, <block>:{…}}` and some answer the block's keys
 * inline. Read through either without guessing wrong: if `key` is present and
 * a plain object it is the payload, otherwise the envelope itself is. An array
 * under `key` is a field of the envelope (e.g. `/setup/surfaces` answers with
 * `surfaces: [...]` beside `available` and `reasons`), never the payload.
 */
export function unwrap<T>(data: any, key: string): T {
  const inner = data?.[key];
  if (inner && typeof inner === "object" && !Array.isArray(inner)) return inner as T;
  return (data ?? {}) as T;
}

/* ── status ─────────────────────────────────────────────────────────── */

export interface SetupStatus {
  ollama?: { reachable: boolean | null; models?: string[]; selected?: string; url?: string };
  vault?: { initialized?: boolean };
  ha?: { configured?: boolean; url?: string };
  knowledge?: { zim_dir?: string; zim_dirs?: string[]; auto_index?: boolean };
  identity?: { name?: string };
  household?: HouseholdStatus;
  surfaces?: unknown;
  pending_apply?: PendingItem[];
  manifest?: { exists?: boolean; summary?: string[] };
  complete?: boolean;
  /**
   * True when THIS caller must send `X-Setup-Code` on state-changing steps.
   * Per-caller, not a global flag: the backend answers false for a client on
   * the machine itself (the CLI wizard, a local curl) and true for a browser
   * coming through this proxy while setup is open. Never carries the code.
   */
  setup_code_required?: boolean;
}

/** `probeOllama=false` skips the network round-trip — the gate only needs
 *  `complete`. */
export function getSetupStatus(probeOllama = true): Promise<Reply<SetupStatus>> {
  return req<SetupStatus>(`/setup/status?ollama=${probeOllama ? "1" : "0"}`);
}

export function markComplete(): Promise<Reply> {
  return req("/setup/complete", "POST");
}

/* ── identity ───────────────────────────────────────────────────────── */

export interface HomeBotDefaults {
  persona?: string;
  bot_name?: string;
  pack?: string;
  goal?: string;
}

export function getDefaults(): Promise<Reply<HomeBotDefaults>> {
  return req<HomeBotDefaults>("/setup/defaults");
}

export function saveIdentity(botName: string, persona?: string): Promise<Reply> {
  return req("/setup/identity", "POST", persona ? { bot_name: botName, persona } : { bot_name: botName });
}

/* ── posture (build network × run egress) ───────────────────────────── */

export interface PostureOption {
  id: string;
  label: string;
  description?: string;
  default?: boolean;
  note?: string;
}

export interface EgressSwitch {
  id: string;
  label: string;
  description?: string;
  purpose?: string;
  enabled?: boolean;
}

export interface PostureOptions {
  build_network: PostureOption[];
  run_egress: PostureOption[];
  egress_switches: EgressSwitch[];
}

export interface PostureAssessment {
  build_network?: string;
  run_egress?: string;
  enabled_switches?: string[];
  summary?: string | string[];
  notes?: string[];
  /** The posture, already shaped for `manifest.posture`. Carried into the
   *  manifest commit verbatim — see `ManifestSections`. */
  manifest_block?: Record<string, unknown>;
}

export function getPosture(): Promise<Reply<PostureOptions>> {
  return req<PostureOptions>("/setup/posture");
}

export function savePosture(
  buildNetwork: string,
  runEgress: string,
  enabledSwitches: string[],
): Promise<Reply<PostureAssessment>> {
  return req<PostureAssessment>("/setup/posture", "POST", {
    build_network: buildNetwork,
    run_egress: runEgress,
    enabled_switches: enabledSwitches,
  });
}

/* ── hardware / brain / crew ────────────────────────────────────────── */

export interface Census {
  memory?: { total_gb: number | null; available_gb?: number | null; source?: string };
  cpu?: { model: string | null; logical_cores: number | null; source?: string };
  accelerator?: { kind?: string; name?: string | null; vram_gb?: number | null; source?: string };
  disk?: { total_gb: number | null; free_gb: number | null };
  soc?: { model: string | null; is_jetson?: boolean };
  hardware_class?: string;
  model_budget_gb?: number | null;
  notes?: string[];
}

export interface BrainOption {
  id: string;
  label: string;
  params?: string;
  disk_gb?: number | null;
  fit?: string;
  installed?: boolean;
  benchmark?: unknown;
  benchmark_note?: string | null;
}

export interface BrainMenu {
  hardware_class?: string;
  total_ram_gb?: number | null;
  options?: BrainOption[];
  too_big?: { id: string; label: string; min_ram_gb: number }[];
  recommended?: { id: string; label: string; reason?: string; installed?: boolean } | null;
  message?: string | null;
}

export interface CrewBudget {
  hardware_class?: string;
  master_core?: number | null;
  max_specialists?: number;
  basis?: string;
  note?: string;
}

export function getHardware(statedRam?: number): Promise<Reply<{ census: Census; summary?: string[] }>> {
  if (statedRam) return req("/setup/hardware", "POST", { stated_ram: statedRam });
  return req("/setup/hardware", "GET");
}

export function getBrainMenu(statedRam?: number): Promise<Reply<{ menu: BrainMenu; summary?: string[] }>> {
  return req("/setup/brain-menu", "POST", statedRam ? { stated_ram: statedRam } : {});
}

export function getCrew(statedRam?: number): Promise<Reply<{ crew: CrewBudget; summary?: string[] }>> {
  return req("/setup/crew", "POST", statedRam ? { stated_ram: statedRam } : {});
}

export function saveOllama(model: string, url?: string): Promise<Reply> {
  return req("/setup/ollama", "POST", url ? { model, url } : { model });
}

/* ── household ──────────────────────────────────────────────────────── */

export interface HouseholdStatus {
  count?: number;
  owner?: string;
  names?: string[];
  error?: string;
  /** Names-only household block. The manifest route does not accept a
   *  `household` section yet, so this is shown, not sent. */
  manifest_block?: Record<string, unknown>;
}

export function getHousehold(): Promise<Reply<HouseholdStatus>> {
  return req<HouseholdStatus>("/setup/household");
}

export function saveHousehold(
  ownerName: string,
  members: { name: string }[],
): Promise<Reply<HouseholdStatus>> {
  return req<HouseholdStatus>("/setup/household", "POST", {
    owner_name: ownerName,
    members,
  });
}

/* ── goal / surfaces ────────────────────────────────────────────────── */

export interface SurfaceDerivation {
  goal?: string;
  surfaces?: string[];
  reasons?: Record<string, string>;
  sensitive?: string[];
  defaulted?: boolean;
  owner_edited?: boolean;
  message?: string | null;
  /** The full menu, when the backend offers one; otherwise the union of
   *  `surfaces` and `reasons` is the menu. */
  available?: string[];
  /** The allowlist shaped for `manifest.surfaces`. This route writes NOTHING:
   *  the derivation only becomes real when it is carried into the manifest
   *  commit, so this block has to be kept and sent. */
  manifest_block?: Record<string, unknown>;
}

export function getSurfaces(): Promise<Reply<SurfaceDerivation>> {
  return req<SurfaceDerivation>("/setup/surfaces");
}

/** What `POST /setup/interview` says a goal matches in the catalog.
 *
 *  `/setup/surfaces` derives the allowlist and its cited reasons, but it drops
 *  the one fact the wizard needs to make an honest offer: whether the catalog
 *  matched ANYTHING. `scaffold_offer` is that fact, and `message` is the
 *  sentence the box wrote for it — "Nothing we ship matches that yet. You can
 *  still build it…". Shown verbatim, because inventing a cheerier version of a
 *  no-match is exactly what this must not do. */
export interface InterviewProposal {
  goal?: string;
  flat?: { id: string; label?: string; matched_terms?: string[] }[];
  message?: string | null;
  /** True when nothing in the catalog matched, so a blank persona is the move. */
  scaffold_offer?: boolean;
}

export function getInterview(
  goal: string,
): Promise<Reply<{ proposal?: InterviewProposal }>> {
  return req<{ proposal?: InterviewProposal }>("/setup/interview", "POST", { goal });
}

/** Owner edits are sent as add/remove against the derivation, mirroring
 *  `setup_ops.apply_surface_choices`. */
export function saveSurfaces(
  goal: string,
  add: string[],
  remove: string[],
): Promise<Reply<SurfaceDerivation>> {
  return req<SurfaceDerivation>("/setup/surfaces", "POST", { goal, add, remove });
}

/* ── storage ────────────────────────────────────────────────────────── */

export interface StoragePaths {
  state_dir?: string;
  knowledge_dir?: string;
  vault_dir?: string;
  media_dir?: string;
}

export interface StorageResult {
  ok?: boolean;
  paths?: StoragePaths;
  problems?: Record<string, string>;
  config_path?: string;
  /** Where this build keeps things, shaped for `manifest.storage`. */
  manifest_block?: Record<string, unknown>;
}

export function getStorage(): Promise<Reply<StorageResult>> {
  return req<StorageResult>("/setup/storage");
}

export function saveStorage(paths: StoragePaths): Promise<Reply<StorageResult>> {
  return req<StorageResult>("/setup/storage", "POST", paths);
}

/* ── vault ──────────────────────────────────────────────────────────── */

export interface VaultCreated {
  recovery_code?: string;
  recovery_code_notice?: string | string[];
  notice?: string | string[];
  already_initialized?: boolean;
  message?: string;
  /** Status-only vault record for `manifest.vault`. Carries no PIN and no
   *  code — the backend builds it from `zone_vault.vault_manifest_block`. */
  vault?: Record<string, unknown>;
}

/** Seals the vault under `pin` and returns the recovery code ONCE. The PIN is
 *  never stored anywhere by this module and the code is only returned. */
export function createVault(pin: string): Promise<Reply<VaultCreated>> {
  return req<VaultCreated>("/setup/vault", "POST", { pin });
}

/** The owner typed the code back; the backend adjudicates the match. */
export function confirmVaultRecovery(recoveryCode: string): Promise<Reply> {
  return req("/setup/vault/confirm", "POST", { recovery_code: recoveryCode });
}

/* ── Home Assistant ─────────────────────────────────────────────────── */

export const DEFAULT_HA_URL = "http://homeassistant.local:8123";

export interface HaTestResult {
  ok?: boolean;
  version?: string;
  location_name?: string;
  error?: string;
  message?: string;
}

/** `present:false` is a complete answer — a household without HA is a finished
 *  build, so no URL and no token are sent. */
export function saveHa(
  present: boolean,
  url?: string,
  token?: string,
): Promise<Reply<{ ha?: Record<string, unknown>; configured?: boolean }>> {
  if (!present) return req("/setup/ha", "POST", { present: false });
  return req("/setup/ha", "POST", { present: true, url, token });
}

export function testHa(url: string, token: string): Promise<Reply<HaTestResult>> {
  return req<HaTestResult>("/setup/ha/test", "POST", { url, token });
}

/* ── bridge (Zone 3) ────────────────────────────────────────────────── */

export interface BridgePosture {
  id: string;
  label?: string;
  name?: string;
  description?: string;
  summary?: string;
  strictness?: number;
  recommendable?: boolean;
}

export interface BridgeOptions {
  recommended?: string;
  reason?: string;
  default?: string;
  postures?: BridgePosture[];
  note?: string;
  /** Can THIS machine run HomeBot's own network edge (Zone 3)? The backend
   *  answers `zone3_support` (Linux + Docker + a free port 53) and every
   *  refusal carries its plain sentence in `reasons`. Absent ⇒ the card is
   *  shown disabled: it fails closed. */
  zone3_support?: {
    supported?: boolean;
    reasons?: string[];
    platform?: string;
    docker?: boolean;
    port_53?: string;
  };
  /** The edge-security menu. The Zone 3 row carries the label and description
   *  the card renders, so the browser and the terminal wizard say the same
   *  thing. */
  edge_options?: {
    question?: string;
    kinds?: {
      id: string;
      label?: string;
      description?: string;
      available?: boolean;
      reasons?: string[];
      mode?: string;
      doc?: string;
    }[];
    default?: string;
    note?: string;
  };
}

export function getBridge(): Promise<Reply<BridgeOptions>> {
  return req<BridgeOptions>("/setup/bridge");
}

/** `networkEdge` is the Zone 3 opt-in. The backend records it for the owner to
 *  apply (it starts nothing itself) and answers with the reconciled posture. */
export function saveBridge(
  posture: string,
  networkEdge: boolean,
): Promise<Reply<{ bridge?: Record<string, unknown>; posture?: string; explanation?: string; network_edge_recorded?: boolean }>> {
  return req("/setup/bridge", "POST", { posture, network_edge: networkEdge });
}

/* ── knowledge ──────────────────────────────────────────────────────── */

export function saveKnowledge(
  zimDir: string,
  autoIndex: boolean,
  createDir = false,
): Promise<Reply> {
  return req("/setup/knowledge", "POST", {
    zim_dir: zimDir,
    auto_index: autoIndex,
    create_dir: createDir,
  });
}

/* ── manifest / pending apply ───────────────────────────────────────── */

export interface ManifestPreview {
  manifest?: Record<string, unknown>;
  summary?: string[];
  diff?: string[];
  warnings?: string[];
  written?: boolean;
  path?: string | null;
}

/**
 * The sections the manifest is assembled FROM.
 *
 * `build_manifest.assemble_manifest` defaults every section it is not given to
 * an empty shape — it reads nothing back off disk. So a commit that sends no
 * sections writes a manifest that records none of the owner's answers: no bot
 * name, no model, a defaulted posture and an EMPTY surface allowlist (which
 * means "nothing extra is loaded"). Every step therefore keeps the block the
 * backend handed it and the review step sends the lot, exactly as
 * `setup_cli._step_manifest` does for the terminal wizard.
 */
export interface ManifestSections {
  identity?: Record<string, unknown>;
  brain?: Record<string, unknown>;
  crew?: Record<string, unknown>;
  storage?: Record<string, unknown>;
  knowledge?: Record<string, unknown>;
  egress?: Record<string, unknown>;
  integrations?: Record<string, unknown>;
  posture?: Record<string, unknown>;
  surfaces?: Record<string, unknown>;
  vault?: Record<string, unknown>;
  bridge?: Record<string, unknown>;
}

/** Whether a manifest already exists on the box (GET is a status, not a
 *  preview: it answers `{exists, summary}`). */
export function getManifestStatus(): Promise<Reply<{ exists?: boolean; summary?: string[] }>> {
  return req<{ exists?: boolean; summary?: string[] }>("/setup/manifest");
}

/** Assemble and diff WITHOUT writing. */
export function previewManifest(sections: ManifestSections): Promise<Reply<ManifestPreview>> {
  return req<ManifestPreview>("/setup/manifest", "POST", { ...sections });
}

/** Assemble and WRITE. The contract guard runs on write. */
export function commitManifest(sections: ManifestSections): Promise<Reply<ManifestPreview>> {
  return req<ManifestPreview>("/setup/manifest", "POST", { ...sections, commit: true });
}

export interface PendingItem {
  kind?: string;
  detail?: Record<string, unknown>;
  recorded_at?: number;
}

export function getPendingApply(): Promise<Reply<{ pending?: PendingItem[] }>> {
  return req<{ pending?: PendingItem[] }>("/setup/pending-apply");
}
