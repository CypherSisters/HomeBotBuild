"use client";

/**
 * First-run wizard — the browser face of HomeBot's `setup` flow.
 *
 * Thirteen steps, in the order the owner can actually answer them: name the
 * bot, state the posture, look at the hardware, fit a brain, name the
 * household, say what it is for, put the files somewhere, seal the Vault, plug
 * in the smart home (or not), pick a Bridge posture, point at knowledge packs,
 * read the manifest back, done.
 *
 * SECRETS. The vault PIN, its recovery code and the Home Assistant token live
 * in component state and nowhere else: not localStorage, not sessionStorage,
 * not the URL, never logged. They are POSTed through the same-origin /api/pai
 * proxy (which adds the appliance's own key server-side) and cleared the moment
 * the step that owns them is finished — and again on unmount.
 *
 * RESILIENCE. Every step's load and every step's save can fail two ways: the
 * backend is unreachable, or it answered `{ok:false}`. Both render one line and
 * a Retry, in an aria-live region. Back always works; the optional steps carry
 * a Skip. No step can dead-end.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import MsIcon from "../_components/MsIcon";
import { recoveryCodeMatches, RECOVERY_CONFIRM_FAILED } from "../_lib/recoveryCode";
import {
  commitManifest,
  confirmVaultRecovery,
  createVault,
  DEFAULT_HA_URL,
  getBrainMenu,
  getBridge,
  getCrew,
  getDefaults,
  getHardware,
  getHousehold,
  getPendingApply,
  getPosture,
  getSetupStatus,
  getStorage,
  getInterview,
  getSurfaces,
  isBackendDown,
  markComplete,
  previewManifest,
  saveBridge,
  saveHa,
  saveHousehold,
  saveIdentity,
  saveKnowledge,
  saveOllama,
  savePosture,
  saveStorage,
  saveSurfaces,
  setSetupCode,
  testHa,
  unwrap,
  type BridgeOptions,
  type BrainMenu,
  type Census,
  type CrewBudget,
  type HouseholdStatus,
  type InterviewProposal,
  type ManifestPreview,
  type ManifestSections,
  type PendingItem,
  type PostureOptions,
  type Reply,
  type StoragePaths,
  type SurfaceDerivation,
} from "../_lib/services/setupService";
import {
  Card,
  Choice,
  Eyebrow,
  FactRow,
  Field,
  Lede,
  Notice,
  PuffButton,
  Stepper,
  TextInput,
  Toggle,
  inputStyle,
  microLabel,
} from "./ui";

const STEPS = [
  "Welcome",
  "Posture",
  "Hardware",
  "Brain",
  "Household",
  "Goal",
  "Storage",
  "Vault",
  "Smart home",
  "Bridge",
  "Knowledge",
  "Review",
  "Done",
] as const;

const S_WELCOME = 0;
const S_POSTURE = 1;
const S_HARDWARE = 2;
const S_BRAIN = 3;
const S_HOUSEHOLD = 4;
const S_GOAL = 5;
const S_STORAGE = 6;
const S_VAULT = 7;
const S_HA = 8;
const S_BRIDGE = 9;
const S_KNOWLEDGE = 10;
const S_REVIEW = 11;
const S_DONE = 12;

/** Steps the owner may walk past without answering. */
const SKIPPABLE = new Set([S_HA, S_KNOWLEDGE]);

/** The vault PIN rules, shown before the field, not after the rejection. */
const PIN_RULES = [
  "At least six characters.",
  "No spaces.",
  "Not a run or a repeat (123456, 111111).",
  "Not a birthday or an address anyone could guess.",
];

const HEADING_ID = "onboarding-step-heading";

function labelOf(entry: { label?: string; name?: string; id: string }): string {
  return entry.label || entry.name || entry.id;
}

export default function OnboardingWizard() {
  const router = useRouter();
  const params = useSearchParams();
  const rerun = params.get("rerun") === "1";

  const [booting, setBooting] = useState(true);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  /** What to run again when the owner presses Retry. */
  const retry = useRef<null | (() => void)>(null);
  const heading = useRef<HTMLHeadingElement | null>(null);

  /**
   * The first-run setup code, and whether the backend wants one.
   *
   * Component state is the authoritative copy — the same rule the vault PIN and
   * the HA token follow. It is mirrored into setupService (memory only) so every
   * step's request carries the header, and cleared on unmount and on completion.
   * Never localStorage, sessionStorage, a cookie, a store or the URL.
   */
  const [setupCodeRequired, setSetupCodeRequired] = useState(false);
  const [setupCodeInput, setSetupCodeInput] = useState("");

  // ── step data ──────────────────────────────────────────────────────
  const [botName, setBotName] = useState("HomeBot");
  const [persona, setPersona] = useState("");
  const [defaultGoal, setDefaultGoal] = useState("");

  const [postureOpts, setPostureOpts] = useState<PostureOptions | null>(null);
  const [buildNetwork, setBuildNetwork] = useState("");
  const [runEgress, setRunEgress] = useState("");
  const [doors, setDoors] = useState<string[]>([]);

  const [census, setCensus] = useState<Census | null>(null);
  const [hwSummary, setHwSummary] = useState<string[]>([]);
  const [statedRam, setStatedRam] = useState("");

  const [menu, setMenu] = useState<BrainMenu | null>(null);
  const [crew, setCrew] = useState<CrewBudget | null>(null);
  const [model, setModel] = useState("");

  const [ownerName, setOwnerName] = useState("");
  const [members, setMembers] = useState<string[]>([]);

  const [derivation, setDerivation] = useState<SurfaceDerivation | null>(null);
  // What the CATALOG said about the goal, which /setup/surfaces does not carry:
  // whether anything matched at all. A goal nothing matches is a fact, and the
  // useful next move is a blank persona rather than a longer shopping list.
  const [proposal, setProposal] = useState<InterviewProposal | null>(null);
  const [goal, setGoal] = useState("");
  const [surfaces, setSurfaces] = useState<string[]>([]);

  const [paths, setPaths] = useState<StoragePaths>({});
  const [pathProblems, setPathProblems] = useState<Record<string, string>>({});

  // Vault. PIN and code are component state only — see the module docstring.
  const [pin, setPin] = useState("");
  const [pin2, setPin2] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [recoveryNotice, setRecoveryNotice] = useState<string[]>([]);
  const [recoveryTyped, setRecoveryTyped] = useState("");
  const [recoveryConfirmed, setRecoveryConfirmed] = useState(false);
  const [vaultSealed, setVaultSealed] = useState(false);

  const [haPresent, setHaPresent] = useState<boolean | null>(null);
  const [haUrl, setHaUrl] = useState(DEFAULT_HA_URL);
  const [haToken, setHaToken] = useState("");
  const [haResult, setHaResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [haTesting, setHaTesting] = useState(false);

  const [bridge, setBridge] = useState<BridgeOptions | null>(null);
  const [bridgePosture, setBridgePosture] = useState("");
  const [networkEdge, setNetworkEdge] = useState(false);

  const [zimDir, setZimDir] = useState("");
  const [autoIndex, setAutoIndex] = useState(true);

  const [manifest, setManifest] = useState<ManifestPreview | null>(null);
  const [pending, setPending] = useState<PendingItem[]>([]);
  const [doneLines, setDoneLines] = useState<string[]>([]);

  // The manifest is assembled from what the caller sends and NOTHING else (see
  // ManifestSections). Each step keeps the block the backend handed it here;
  // the review step previews and commits the lot.
  const sections = useRef<ManifestSections>({});
  const [ollamaUrl, setOllamaUrl] = useState("");

  const statedRamNum = useCallback((): number | undefined => {
    const n = parseFloat(statedRam);
    return Number.isFinite(n) && n > 0 ? n : undefined;
  }, [statedRam]);

  /** Keep a manifest block a step's response carried. Ignores anything that is
   *  not an object, so a route that stops returning one cannot poison the
   *  commit — the section simply falls back to the backend's own default. */
  const record = useCallback((key: keyof ManifestSections, block: unknown) => {
    if (block && typeof block === "object" && !Array.isArray(block)) {
      sections.current[key] = block as Record<string, unknown>;
    }
  }, []);

  /** Show one line and keep the action that produced it, for Retry. */
  const fail = useCallback((reply: Reply, again: () => void) => {
    setError(reply.error || "That did not work.");
    retry.current = again;
  }, []);

  const clearError = useCallback(() => {
    setError("");
    retry.current = null;
  }, []);

  // ── boot: status + defaults ────────────────────────────────────────
  const boot = useCallback(async () => {
    setBusy(true);
    const status = await getSetupStatus(false);
    if (status.ok && status.data?.complete === true && !rerun) {
      router.replace("/dashboard");
      return;
    }
    if (!status.ok && isBackendDown(status)) {
      fail(status, () => {
        void boot();
      });
      setBusy(false);
      setBooting(false);
      return;
    }
    const defaults = await getDefaults();
    if (defaults.ok) {
      const d = unwrap<{ bot_name?: string; persona?: string; goal?: string }>(
        defaults.data,
        "defaults",
      );
      if (d.bot_name) setBotName(d.bot_name);
      if (d.persona) setPersona(d.persona);
      if (d.goal) {
        setDefaultGoal(d.goal);
        setGoal((current) => current || d.goal || "");
      }
    }
    // Does this browser need the console code? The backend answers per-caller;
    // a client on the machine itself is told false.
    setSetupCodeRequired(status.data?.setup_code_required === true);
    const name = status.data?.identity?.name;
    if (name) setBotName(name);
    // The brain block records WHERE the model is served from as well as which
    // one was chosen; the browser never needs the URL for anything else.
    if (status.data?.ollama?.url) setOllamaUrl(status.data.ollama.url);
    clearError();
    setBusy(false);
    setBooting(false);
  }, [clearError, fail, rerun, router]);

  useEffect(() => {
    void boot();
  }, [boot]);

  // Focus the heading whenever the step changes: a screen reader announces the
  // new step, and the keyboard lands at the top of it rather than wherever the
  // last button was.
  useEffect(() => {
    if (booting) return;
    heading.current?.focus();
  }, [step, booting]);

  // Mirror the typed code into the transport so every step carries the header.
  // One-way: component state -> service. The service never reads it back.
  useEffect(() => {
    setSetupCode(setupCodeInput);
  }, [setupCodeInput]);

  // Secrets must not outlive this screen.
  useEffect(() => {
    return () => {
      setPin("");
      setPin2("");
      setRecoveryCode("");
      setRecoveryTyped("");
      setHaToken("");
      // The setup code is a credential too: drop the transport's copy as well
      // as this component's, so a later page in the same tab cannot use it.
      setSetupCode("");
    };
  }, []);

  // ── per-step loaders ───────────────────────────────────────────────
  const loadPosture = useCallback(async () => {
    setBusy(true);
    const reply = await getPosture();
    setBusy(false);
    if (!reply.ok) {
      fail(reply, () => void loadPosture());
      return;
    }
    const data = unwrap<PostureOptions>(reply.data, "options");
    setPostureOpts(data);
    setBuildNetwork(
      (current) => current || data.build_network?.find((o) => o.default)?.id || data.build_network?.[0]?.id || "",
    );
    setRunEgress(
      (current) => current || data.run_egress?.find((o) => o.default)?.id || data.run_egress?.[0]?.id || "",
    );
    clearError();
  }, [clearError, fail]);

  const loadHardware = useCallback(async () => {
    setBusy(true);
    const reply = await getHardware(statedRamNum());
    setBusy(false);
    if (!reply.ok) {
      fail(reply, () => void loadHardware());
      return;
    }
    setCensus(unwrap<Census>(reply.data, "census"));
    setHwSummary(reply.data?.summary || []);
    clearError();
  }, [clearError, fail, statedRamNum]);

  const loadBrain = useCallback(async () => {
    setBusy(true);
    const ram = statedRamNum();
    const menuReply = await getBrainMenu(ram);
    if (!menuReply.ok) {
      setBusy(false);
      fail(menuReply, () => void loadBrain());
      return;
    }
    const m = unwrap<BrainMenu>(menuReply.data, "menu");
    setMenu(m);
    setModel((current) => current || m.recommended?.id || m.options?.[0]?.id || "");
    const crewReply = await getCrew(ram);
    setBusy(false);
    if (crewReply.ok) {
      const c = unwrap<CrewBudget>(crewReply.data, "crew");
      setCrew(c);
    }
    clearError();
  }, [clearError, fail, statedRamNum]);

  const loadHousehold = useCallback(async () => {
    setBusy(true);
    const reply = await getHousehold();
    setBusy(false);
    if (!reply.ok) {
      fail(reply, () => void loadHousehold());
      return;
    }
    const h = unwrap<HouseholdStatus>(reply.data, "household");
    if (h.owner) setOwnerName((current) => current || h.owner || "");
    const others = (h.names || []).filter((n) => n && n !== h.owner);
    if (others.length) setMembers((current) => (current.length ? current : others));
    clearError();
  }, [clearError, fail]);

  const loadSurfaces = useCallback(async () => {
    setBusy(true);
    const reply = await getSurfaces();
    setBusy(false);
    if (!reply.ok) {
      fail(reply, () => void loadSurfaces());
      return;
    }
    const d = unwrap<SurfaceDerivation>(reply.data, "surfaces");
    setDerivation(d);
    setGoal((current) => current || d.goal || defaultGoal);
    setSurfaces((current) => (current.length ? current : d.surfaces || []));
    clearError();
    // Second, independent question, and a failure here is not worth an error
    // screen: this step still works without it. All it adds is the offer below.
    const interview = await getInterview(d.goal || defaultGoal);
    if (interview.ok) {
      setProposal(
        unwrap<{ proposal?: InterviewProposal }>(interview.data, "interview")
          .proposal ?? null,
      );
    }
  }, [clearError, defaultGoal, fail]);

  const loadStorage = useCallback(async () => {
    setBusy(true);
    const reply = await getStorage();
    setBusy(false);
    if (!reply.ok) {
      fail(reply, () => void loadStorage());
      return;
    }
    const p = unwrap<StoragePaths>(reply.data, "paths");
    setPaths((current) => (Object.keys(current).length ? current : p));
    clearError();
  }, [clearError, fail]);

  const loadBridge = useCallback(async () => {
    setBusy(true);
    const reply = await getBridge();
    setBusy(false);
    if (!reply.ok) {
      fail(reply, () => void loadBridge());
      return;
    }
    const b = unwrap<BridgeOptions>(reply.data, "bridge");
    setBridge(b);
    setBridgePosture((current) => current || b.recommended || b.default || b.postures?.[0]?.id || "");
    clearError();
  }, [clearError, fail]);

  const loadReview = useCallback(async () => {
    setBusy(true);
    // A PREVIEW, not a read: GET /setup/manifest answers {exists, summary} for
    // whatever is already on disk, which on a first run is nothing at all. The
    // owner has to be shown the build they just described, so it is assembled
    // from the blocks the steps collected — the same POST the commit uses,
    // without `commit`.
    const reply = await previewManifest(sections.current);
    if (!reply.ok) {
      setBusy(false);
      fail(reply, () => void loadReview());
      return;
    }
    setManifest(reply.data || {});
    const pendingReply = await getPendingApply();
    setBusy(false);
    if (pendingReply.ok) setPending(pendingReply.data?.pending || []);
    clearError();
  }, [clearError, fail]);

  useEffect(() => {
    if (booting) return;
    if (step === S_POSTURE && !postureOpts) void loadPosture();
    if (step === S_HARDWARE && !census) void loadHardware();
    if (step === S_BRAIN && !menu) void loadBrain();
    if (step === S_HOUSEHOLD && !ownerName && members.length === 0) void loadHousehold();
    if (step === S_GOAL && !derivation) void loadSurfaces();
    if (step === S_STORAGE && Object.keys(paths).length === 0) void loadStorage();
    if (step === S_BRIDGE && !bridge) void loadBridge();
    if (step === S_REVIEW && !manifest) void loadReview();
    // Loaders are stable callbacks; the guards above keep this to one call.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, booting]);

  // ── the vault ceremony ─────────────────────────────────────────────
  const sealVault = useCallback(async () => {
    if (pin.length < 6) {
      setError("That PIN is too short: six characters at least.");
      return;
    }
    if (pin !== pin2) {
      setError("The two PINs are not the same.");
      return;
    }
    setBusy(true);
    const reply = await createVault(pin);
    setBusy(false);
    if (!reply.ok) {
      fail(reply, () => void sealVault());
      return;
    }
    // Status only: configured/sealed/method, never the PIN and never the code.
    record("vault", reply.data?.vault);
    const code = reply.data?.recovery_code || "";
    const notice = reply.data?.recovery_code_notice ?? reply.data?.notice;
    setRecoveryNotice(Array.isArray(notice) ? notice : notice ? [notice] : []);
    setVaultSealed(true);
    // The PIN has done its job. It is not needed again on this screen.
    setPin("");
    setPin2("");
    if (!code) {
      // Sealed, but no code came back (an already-initialised vault says so).
      setRecoveryConfirmed(true);
      clearError();
      return;
    }
    setRecoveryCode(code);
    clearError();
  }, [clearError, fail, pin, pin2, record]);

  const confirmRecovery = useCallback(async () => {
    if (!recoveryCodeMatches(recoveryCode, recoveryTyped)) {
      setError(RECOVERY_CONFIRM_FAILED);
      retry.current = null;
      return;
    }
    setBusy(true);
    const reply = await confirmVaultRecovery(recoveryTyped);
    setBusy(false);
    if (!reply.ok) {
      fail(reply, () => void confirmRecovery());
      return;
    }
    setRecoveryConfirmed(true);
    // The build record says the owner wrote the code down. Merged, so the
    // sealed/method facts from the seal step survive.
    record("vault", {
      ...(sections.current.vault || {}),
      ...(typeof reply.data?.vault === "object" && reply.data?.vault ? reply.data.vault : {}),
    });
    // Both halves are spent: the code is off the screen and out of memory.
    setRecoveryCode("");
    setRecoveryTyped("");
    clearError();
  }, [clearError, fail, record, recoveryCode, recoveryTyped]);

  const runHaTest = useCallback(async () => {
    setHaTesting(true);
    const reply = await testHa(haUrl, haToken);
    setHaTesting(false);
    if (!reply.ok || reply.data?.ok === false) {
      setHaResult({
        ok: false,
        text:
          reply.data?.error ||
          reply.error ||
          "Home Assistant did not accept that address and token.",
      });
      return;
    }
    const where = reply.data?.location_name ? ` (${reply.data.location_name})` : "";
    const version = reply.data?.version ? ` version ${reply.data.version}` : "";
    setHaResult({ ok: true, text: `Connected to Home Assistant${where}${version}.` });
  }, [haToken, haUrl]);

  // ── commit for the current step ────────────────────────────────────
  const commit = useCallback(async (): Promise<boolean> => {
    switch (step) {
      case S_WELCOME: {
        const name = botName.trim() || "HomeBot";
        setBotName(name);
        const reply = await saveIdentity(name, persona || undefined);
        if (!reply.ok) {
          fail(reply, () => void advance());
          return false;
        }
        // `identity` is the one section with no block route: assemble_manifest
        // reads `identity.identity` and `identity.name` (setup_cli does the
        // same), and the persona comes back echoed from the backend.
        record("identity", { identity: reply.data?.persona || persona, name });
        return true;
      }
      case S_POSTURE: {
        const reply = await savePosture(buildNetwork, runEgress, runEgress === "enumerated" ? doors : []);
        if (!reply.ok) {
          fail(reply, () => void advance());
          return false;
        }
        // `posture` is the validated home for both axes and the door table.
        // The manifest's separate `egress` section keeps its own default on
        // purpose: setup_cli does not fill it either, and inventing a value
        // for it here would put an unvalidated vocabulary in the build record.
        record("posture", reply.data?.manifest_block);
        return true;
      }
      case S_HARDWARE:
        return true;
      case S_BRAIN: {
        if (!model) {
          setError("Pick a model. You can change it later from Settings.");
          return false;
        }
        const reply = await saveOllama(model);
        if (!reply.ok) {
          fail(reply, () => void advance());
          return false;
        }
        record("brain", {
          model,
          url: ollamaUrl,
          installed: Boolean((menu?.options || []).find((o) => o.id === model)?.installed),
          recommended: menu?.recommended ?? null,
        });
        if (crew) record("crew", crew);
        return true;
      }
      case S_HOUSEHOLD: {
        if (!ownerName.trim()) {
          setError("Your name, so the household has an owner.");
          return false;
        }
        const reply = await saveHousehold(
          ownerName.trim(),
          members.map((n) => n.trim()).filter(Boolean).map((name) => ({ name })),
        );
        if (!reply.ok) {
          fail(reply, () => void advance());
          return false;
        }
        return true;
      }
      case S_GOAL: {
        const base = derivation?.surfaces || [];
        const add = surfaces.filter((s) => !base.includes(s));
        const remove = base.filter((s) => !surfaces.includes(s));
        const reply = await saveSurfaces(goal.trim(), add, remove);
        if (!reply.ok) {
          fail(reply, () => void advance());
          return false;
        }
        const d = unwrap<SurfaceDerivation>(reply.data, "surfaces");
        if (d.surfaces) {
          setDerivation(d);
          setSurfaces(d.surfaces);
        }
        // This route writes nothing. The allowlist becomes real only when this
        // block reaches the manifest commit, so losing it here would ship a
        // build whose surfaces are all switched off.
        record("surfaces", reply.data?.manifest_block);
        return true;
      }
      case S_STORAGE: {
        const reply = await saveStorage(paths);
        const problems = reply.data?.problems || {};
        if (!reply.ok || Object.keys(problems).length > 0) {
          setPathProblems(problems);
          if (Object.keys(problems).length === 0) fail(reply, () => void advance());
          else setError("Some of those folders will not work: see the notes under each one.");
          return false;
        }
        setPathProblems({});
        record("storage", reply.data?.manifest_block);
        return true;
      }
      case S_VAULT: {
        if (!vaultSealed) {
          await sealVault();
          return false;
        }
        if (!recoveryConfirmed) {
          await confirmRecovery();
          return false;
        }
        return true;
      }
      case S_HA: {
        if (haPresent === null) {
          setError("Answer yes or no: either one finishes this step.");
          return false;
        }
        if (haPresent && (!haUrl.trim() || !haToken)) {
          setError("Home Assistant needs its address and a long-lived token.");
          return false;
        }
        const reply = haPresent
          ? await saveHa(true, haUrl.trim(), haToken)
          : await saveHa(false);
        if (!reply.ok) {
          fail(reply, () => void advance());
          return false;
        }
        // The `ha` block records presence, the address and WHETHER a token was
        // placed — never the token, which stays on the box.
        record("integrations", {
          home_assistant: reply.data?.ha || { present: haPresent },
          media_library: { present: Boolean((paths.media_dir || "").trim()) },
        });
        // The token is written into the backend's config; the browser forgets it.
        setHaToken("");
        return true;
      }
      case S_BRIDGE: {
        if (!bridgePosture) {
          setError("Pick one of the postures.");
          return false;
        }
        const reply = await saveBridge(bridgePosture, networkEdge);
        if (!reply.ok) {
          fail(reply, () => void advance());
          return false;
        }
        // The backend reconciles the choice against Step 0's egress tier
        // (stricter wins), so the block it answers with — not the id that was
        // clicked — is what the build record gets.
        record("bridge", reply.data?.bridge);
        return true;
      }
      case S_KNOWLEDGE: {
        if (!zimDir.trim()) return true; // nothing pointed at is a valid answer
        const reply = await saveKnowledge(zimDir.trim(), autoIndex, true);
        if (!reply.ok) {
          fail(reply, () => void advance());
          return false;
        }
        record("knowledge", { zim_dir: zimDir.trim(), auto_index: autoIndex });
        return true;
      }
      case S_REVIEW: {
        const written = await commitManifest(sections.current);
        if (!written.ok) {
          fail(written, () => void advance());
          return false;
        }
        const done = await markComplete();
        if (!done.ok) {
          fail(done, () => void advance());
          return false;
        }
        // Setup is over, so the first-run code is over: the backend has just
        // destroyed its copy and nothing further needs the header.
        setSetupCodeInput("");
        setSetupCodeRequired(false);
        setSetupCode("");
        setDoneLines(written.data?.summary || manifest?.summary || []);
        return true;
      }
      default:
        return true;
    }
    // `advance` is defined below and stable enough for the retry closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    autoIndex,
    botName,
    bridgePosture,
    buildNetwork,
    confirmRecovery,
    crew,
    derivation,
    doors,
    fail,
    goal,
    haPresent,
    haToken,
    haUrl,
    manifest,
    members,
    menu,
    model,
    networkEdge,
    ollamaUrl,
    ownerName,
    paths,
    persona,
    record,
    recoveryConfirmed,
    runEgress,
    sealVault,
    step,
    surfaces,
    vaultSealed,
    zimDir,
  ]);

  const advance = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    let ok = false;
    try {
      ok = await commit();
    } catch {
      // A bug in a step must not become an unhandled rejection.
      setError("Something went wrong on this step. Try again.");
    }
    setBusy(false);
    if (ok) {
      retry.current = null;
      setStep((s) => Math.min(s + 1, S_DONE));
    }
  }, [busy, commit]);

  const back = useCallback(() => {
    clearError();
    setStep((s) => Math.max(0, s - 1));
  }, [clearError]);

  const skip = useCallback(async () => {
    clearError();
    if (step === S_HA) {
      // "No Home Assistant" is a complete answer; record it rather than leave
      // the step unanswered.
      setHaPresent(false);
      setHaToken("");
      const reply = await saveHa(false);
      record("integrations", {
        home_assistant: reply.data?.ha || { present: false },
        media_library: { present: Boolean((paths.media_dir || "").trim()) },
      });
    }
    setStep((s) => Math.min(s + 1, S_DONE));
  }, [clearError, paths.media_dir, record, step]);

  const toggleIn = (list: string[], id: string): string[] =>
    list.includes(id) ? list.filter((x) => x !== id) : [...list, id];

  const surfaceMenu = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    const push = (id: string) => {
      if (id && !seen.has(id)) {
        seen.add(id);
        out.push(id);
      }
    };
    (derivation?.available || []).forEach(push);
    (derivation?.surfaces || []).forEach(push);
    Object.keys(derivation?.reasons || {}).forEach(push);
    surfaces.forEach(push);
    return out;
  }, [derivation, surfaces]);

  // Zone 3 is offered only where it can actually run. The verdict and its
  // plain sentences come from the backend (`zone3_support`); the row's own copy
  // comes from the edge menu, so the browser and the terminal wizard say the
  // same thing. Missing either ⇒ the card stays off: it fails closed.
  const zone3Row = (bridge?.edge_options?.kinds || []).find((k) => k.id === "zone3");
  const zone3Supported = Boolean(bridge?.zone3_support?.supported && zone3Row?.available !== false);
  const zone3Reasons = bridge?.zone3_support?.reasons || zone3Row?.reasons || [];

  // The brain step's current pick, so a model that is not on this box can say
  // so with the command that gets it.
  const pickedBrain = (menu?.options || []).find((o) => o.id === model);

  // ── render ─────────────────────────────────────────────────────────
  if (booting) {
    return (
      <div style={{ ...microLabel, padding: 32 }} aria-live="polite">
        Checking your setup…
      </div>
    );
  }

  const canSkip = SKIPPABLE.has(step);
  const nextLabel =
    step === S_REVIEW ? "Build it" : step === S_DONE ? "Open the dashboard" : "Continue";

  return (
    <div style={{ maxWidth: 860, margin: "0 auto", padding: "8px 0 40px" }}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <Stepper labels={STEPS} current={step} />
        {/* The same build on one screen, every row proposed, nothing applied
            until it is. The step-by-step is still here; the review is the way
            to see the whole of it at once. */}
        <Link
          href="/onboarding/review"
          style={{
            fontFamily: "var(--font-jbmono), monospace",
            fontWeight: 700,
            fontSize: 10,
            letterSpacing: ".14em",
            textTransform: "uppercase",
            color: "var(--text2)",
          }}
        >
          See the whole build on one screen
        </Link>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (step === S_DONE) {
            router.replace("/dashboard");
            return;
          }
          void advance();
        }}
        style={{ marginTop: 18 }}
      >
        <Card>
          <Eyebrow>
            Step {String(step + 1).padStart(2, "0")} of {STEPS.length} · {STEPS[step]}
          </Eyebrow>
          <h1
            id={HEADING_ID}
            ref={heading}
            tabIndex={-1}
            style={{
              fontFamily: "var(--font-fredoka), var(--font-nunito), sans-serif",
              fontSize: 28,
              fontWeight: 700,
              color: "var(--text)",
              margin: "8px 0 0",
              outline: "none",
            }}
          >
            {headingFor(step, botName)}
          </h1>

          <div style={{ marginTop: 10 }}>
            {step === S_WELCOME && (
              <>
                <Lede>
                  This box runs your household&apos;s AI. Everything it learns stays here. Give it a
                  name. The family will use it out loud.
                </Lede>
                <Field id="ob-bot-name" label="Bot name">
                  <TextInput id="ob-bot-name" value={botName} onChange={setBotName} placeholder="HomeBot" />
                </Field>
                {setupCodeRequired ? (
                  <>
                    <Field
                      id="ob-setup-code"
                      label="First-run code"
                      hint="Printed on the console of the machine running HomeBot when it started. Run `puffpai setup-code` there to see it again."
                    >
                      <TextInput
                        id="ob-setup-code"
                        value={setupCodeInput}
                        onChange={setSetupCodeInput}
                        placeholder="ABCD-EFGH"
                      />
                    </Field>
                    <Notice>
                      HomeBot printed this code in its console when it started (run{" "}
                      <code>puffpai setup-code</code> to see it again). It proves the person
                      answering these questions is the person who owns the box, and the answers
                      include the Vault.
                    </Notice>
                  </>
                ) : null}
                {persona ? (
                  <Notice>Persona: {persona}. Change it later in Settings.</Notice>
                ) : null}
              </>
            )}

            {step === S_POSTURE && (
              <>
                <Lede>Two questions: how this build gets assembled, and what it may reach once it runs.</Lede>
                <div style={{ marginTop: 16 }}>
                  <Eyebrow>Building it</Eyebrow>
                  {(postureOpts?.build_network || []).map((o) => (
                    <Choice
                      key={o.id}
                      name="ob-build-network"
                      value={o.id}
                      checked={buildNetwork === o.id}
                      onSelect={setBuildNetwork}
                      title={labelOf(o)}
                      description={o.description}
                    />
                  ))}
                </div>
                <div style={{ marginTop: 18 }}>
                  <Eyebrow>Running it</Eyebrow>
                  {(postureOpts?.run_egress || []).map((o) => (
                    <Choice
                      key={o.id}
                      name="ob-run-egress"
                      value={o.id}
                      checked={runEgress === o.id}
                      onSelect={setRunEgress}
                      title={labelOf(o)}
                      description={o.description}
                      note={o.note}
                    />
                  ))}
                </div>
                {runEgress === "enumerated" && (
                  <div style={{ marginTop: 18 }}>
                    <Eyebrow>Bridge switches you are opening</Eyebrow>
                    <Lede>Every switch is shut until you open it. Each one says what it sends.</Lede>
                    {(postureOpts?.egress_switches || []).map((sw) => (
                      <Toggle
                        key={sw.id}
                        id={`ob-door-${sw.id}`}
                        checked={doors.includes(sw.id)}
                        onChange={() => setDoors((d) => toggleIn(d, sw.id))}
                        title={labelOf(sw)}
                        description={sw.description || sw.purpose}
                      />
                    ))}
                  </div>
                )}
              </>
            )}

            {step === S_HARDWARE && (
              <>
                <Lede>What this machine actually is. Correct the memory if the reading is wrong.</Lede>
                <div style={{ marginTop: 14 }}>
                  <FactRow label="Class" value={census?.hardware_class || "unknown"} />
                  <FactRow
                    label="Memory"
                    value={census?.memory?.total_gb ? `${census.memory.total_gb} GB` : "not detected"}
                  />
                  <FactRow label="CPU" value={census?.cpu?.model || "not detected"} />
                  <FactRow
                    label="Accelerator"
                    value={
                      census?.accelerator?.name ||
                      (census?.accelerator?.kind === "none" ? "none" : census?.accelerator?.kind || "none")
                    }
                  />
                  <FactRow
                    label="Disk free"
                    value={census?.disk?.free_gb ? `${census.disk.free_gb} GB` : "not detected"}
                  />
                </div>
                <Field
                  id="ob-stated-ram"
                  label="Stated memory (GB)"
                  hint="Leave this empty unless the detected figure is wrong."
                >
                  <TextInput
                    id="ob-stated-ram"
                    value={statedRam}
                    onChange={setStatedRam}
                    placeholder={census?.memory?.total_gb ? String(census.memory.total_gb) : "16"}
                  />
                </Field>
                <div style={{ marginTop: 10 }}>
                  <PuffButton label="Re-read the hardware" onClick={() => void loadHardware()} />
                </div>
                {hwSummary.length > 0 && (
                  <Notice>
                    {hwSummary.map((line, i) => (
                      <div key={i}>{line}</div>
                    ))}
                  </Notice>
                )}
                {(census?.notes || []).length > 0 && (
                  <Notice tone="warn">
                    {(census?.notes || []).map((line, i) => (
                      <div key={i}>{line}</div>
                    ))}
                  </Notice>
                )}
              </>
            )}

            {step === S_BRAIN && (
              <>
                <Lede>
                  Models that fit this machine&apos;s memory, biggest first. The list shows size and
                  fit, not speed.
                </Lede>
                {(menu?.options || []).map((o) => (
                  <Choice
                    key={o.id}
                    name="ob-model"
                    value={o.id}
                    checked={model === o.id}
                    onSelect={setModel}
                    title={`${labelOf(o)}${o.params ? ` · ${o.params}` : ""}`}
                    description={[
                      o.disk_gb ? `${o.disk_gb} GB on disk` : "",
                      o.fit ? `fit: ${o.fit}` : "",
                      o.installed ? "already installed" : "not installed yet",
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                    note={o.benchmark_note || "Untested on your hardware."}
                  />
                ))}
                {/* Saving the pick (/setup/ollama) downloads nothing, and nothing
                    later fetches it on its own: a model that is not here has to
                    be pulled. Say how, for the Ollama this build uses. */}
                {pickedBrain && !pickedBrain.installed && (
                  <Notice>
                    {labelOf(pickedBrain)} is not installed yet, and picking it here downloads
                    nothing. Pull it with <code>ollama pull {pickedBrain.id}</code>
                    {ollamaUrl ? <> on the Ollama at {ollamaUrl}</> : null}. HomeBot itself
                    fetches the models on this list only through the Model pull Bridge switch.
                  </Notice>
                )}
                {(menu?.options || []).length === 0 && (
                  <Notice tone="warn">
                    {menu?.message || "No model menu came back. You can set the model later in Settings."}
                  </Notice>
                )}
                {(menu?.too_big || []).length > 0 && (
                  <Notice tone="warn">
                    Too big for this machine: {(menu?.too_big || []).map((t) => t.label).join(", ")}.
                  </Notice>
                )}
                {/* The crew budget is a FACT about this machine, not a
                    question: nothing in the build accepts a crew size, so a
                    number box here would be a field the wizard pretends to
                    store. It is reported, and it goes into the manifest. */}
                <div style={{ marginTop: 18 }}>
                  <Eyebrow>Specialist crew</Eyebrow>
                  <FactRow
                    label="Specialists this machine can carry"
                    value={String(crew?.max_specialists ?? 0)}
                  />
                  {crew?.note ? <Notice>{crew.note}</Notice> : null}
                </div>
              </>
            )}

            {step === S_HOUSEHOLD && (
              <>
                <Lede>Who lives here. Names only: no ages, no birthdays, nothing it does not need.</Lede>
                <Field id="ob-owner" label="Your name (the owner)">
                  <TextInput id="ob-owner" value={ownerName} onChange={setOwnerName} placeholder="Alex" />
                </Field>
                {members.map((name, i) => (
                  <Field key={i} id={`ob-member-${i}`} label={`Household member ${i + 1}`}>
                    <div className="flex gap-2">
                      <TextInput
                        id={`ob-member-${i}`}
                        value={name}
                        onChange={(next) =>
                          setMembers((list) => list.map((v, j) => (j === i ? next : v)))
                        }
                      />
                      <PuffButton
                        label="Remove"
                        onClick={() => setMembers((list) => list.filter((_, j) => j !== i))}
                      />
                    </div>
                  </Field>
                ))}
                <div style={{ marginTop: 12 }}>
                  <PuffButton label="Add someone" onClick={() => setMembers((list) => [...list, ""])} />
                </div>
              </>
            )}

            {step === S_GOAL && (
              <>
                <Lede>
                  What this household wants out of it. The surfaces below follow from the goal, every
                  one is a switch you own.
                </Lede>
                <Field id="ob-goal" label="Household goal">
                  <textarea
                    id="ob-goal"
                    value={goal}
                    onChange={(e) => setGoal(e.target.value)}
                    rows={3}
                    style={inputStyle}
                  />
                </Field>
                <div style={{ marginTop: 16 }}>
                  <Eyebrow>Surfaces</Eyebrow>
                  {surfaceMenu.map((id) => (
                    <Toggle
                      key={id}
                      id={`ob-surface-${id}`}
                      checked={surfaces.includes(id)}
                      onChange={() => setSurfaces((list) => toggleIn(list, id))}
                      title={id}
                      description={derivation?.reasons?.[id]}
                    />
                  ))}
                </div>
                {derivation?.message ? <Notice>{derivation.message}</Notice> : null}
                {proposal?.scaffold_offer && (
                  /* The catalog matched nothing. The box's own sentence, shown
                     verbatim, plus the thing it promises: a blank persona with
                     the owner's words in it. Opened in a new tab on purpose —
                     leaving the wizard mid-run would lose the answers above. */
                  <Notice>
                    {proposal.message}
                    <div style={{ marginTop: 10 }}>
                      <a
                        href="/dashboard/apps/new"
                        target="_blank"
                        rel="noreferrer"
                        style={{ color: "var(--sky)", fontWeight: 700 }}
                      >
                        Build a persona for it &rarr;
                      </a>{" "}
                      <span style={{ color: "var(--text3)" }}>
                        (opens in a new tab, so you keep your place here)
                      </span>
                    </div>
                  </Notice>
                )}
                {surfaces.length === 0 && (
                  <Notice tone="warn">
                    Nothing is switched on, so your bot will decline everything until you turn
                    something back on. You can still continue.
                  </Notice>
                )}
                {(derivation?.sensitive || []).length > 0 && (
                  <Notice tone="warn">
                    Sensitive: {(derivation?.sensitive || []).join(", ")}. These reach the house
                    itself, so they always ask first.
                  </Notice>
                )}
              </>
            )}

            {step === S_STORAGE && (
              <>
                <Lede>Four folders. Full paths: they have to mean the same thing from anywhere.</Lede>
                {STORAGE_FIELDS.map(([key, label, hint]) => (
                  <Field
                    key={key}
                    id={`ob-path-${key}`}
                    label={label}
                    hint={hint}
                    error={pathProblems[key]}
                  >
                    <TextInput
                      id={`ob-path-${key}`}
                      value={(paths as Record<string, string | undefined>)[key] || ""}
                      onChange={(next) => setPaths((p) => ({ ...p, [key]: next }))}
                      invalid={Boolean(pathProblems[key])}
                    />
                  </Field>
                ))}
              </>
            )}

            {step === S_VAULT && (
              <>
                {!vaultSealed && (
                  <>
                    <Lede>
                      The Vault holds what only the household should see. One PIN opens it, and that
                      PIN is never stored here.
                    </Lede>
                    <ul style={{ margin: "12px 0 0", paddingLeft: 18, color: "var(--text2)", fontSize: 14 }}>
                      {PIN_RULES.map((rule) => (
                        <li key={rule}>{rule}</li>
                      ))}
                    </ul>
                    <Field id="ob-pin" label="Vault PIN">
                      <TextInput id="ob-pin" type="password" value={pin} onChange={setPin} autoComplete="new-password" />
                    </Field>
                    <Field id="ob-pin2" label="PIN again">
                      <TextInput id="ob-pin2" type="password" value={pin2} onChange={setPin2} autoComplete="new-password" />
                    </Field>
                  </>
                )}

                {vaultSealed && !recoveryConfirmed && (
                  <>
                    <Lede>
                      Write this code down on paper now. It is shown once, it is the only way back
                      into the Vault if the PIN is lost, and nothing here keeps a copy.
                    </Lede>
                    <div
                      style={{
                        marginTop: 14,
                        padding: "16px 18px",
                        borderRadius: 14,
                        border: "1px solid var(--pink)",
                        background: "var(--chip)",
                        fontFamily: "var(--font-jbmono), monospace",
                        fontSize: 20,
                        letterSpacing: ".08em",
                        color: "var(--text)",
                        wordBreak: "break-all",
                      }}
                    >
                      {recoveryCode}
                    </div>
                    {recoveryNotice.map((line, i) => (
                      <Notice key={i}>{line}</Notice>
                    ))}
                    <Field id="ob-recovery" label="Type the code back">
                      <TextInput
                        id="ob-recovery"
                        value={recoveryTyped}
                        onChange={setRecoveryTyped}
                        autoComplete="off"
                      />
                    </Field>
                  </>
                )}

                {vaultSealed && recoveryConfirmed && (
                  <Notice tone="ok">
                    The Vault is sealed and the recovery code is confirmed. Keep the paper somewhere
                    the household can find it.
                  </Notice>
                )}
              </>
            )}

            {step === S_HA && (
              <>
                <Lede>
                  Do you run Home Assistant? A household without it is a finished build, not a
                  degraded one.
                </Lede>
                <Choice
                  name="ob-ha"
                  value="no"
                  checked={haPresent === false}
                  onSelect={() => setHaPresent(false)}
                  title="No"
                  description="Skip the smart-home step entirely."
                />
                <Choice
                  name="ob-ha"
                  value="yes"
                  checked={haPresent === true}
                  onSelect={() => setHaPresent(true)}
                  title="Yes"
                  description="Point this build at it with a long-lived access token."
                />
                {haPresent === true && (
                  <>
                    <Field id="ob-ha-url" label="Home Assistant address">
                      <TextInput id="ob-ha-url" value={haUrl} onChange={setHaUrl} />
                    </Field>
                    <Field
                      id="ob-ha-token"
                      label="Long-lived access token"
                      hint="Created in Home Assistant under your profile. It is sent to this box and stored there, never in the browser."
                    >
                      <TextInput
                        id="ob-ha-token"
                        type="password"
                        value={haToken}
                        onChange={setHaToken}
                        autoComplete="off"
                      />
                    </Field>
                    <div style={{ marginTop: 12 }}>
                      <PuffButton
                        label={haTesting ? "Testing…" : "Test the connection"}
                        onClick={() => void runHaTest()}
                        disabled={haTesting || !haUrl.trim() || !haToken}
                      />
                    </div>
                    {haResult ? (
                      <Notice tone={haResult.ok ? "ok" : "warn"}>{haResult.text}</Notice>
                    ) : null}
                  </>
                )}
              </>
            )}

            {step === S_BRIDGE && (
              <>
                <Lede>
                  The Bridge is what your bot may reach beyond itself. Stricter is always available;
                  the suggestion is only a suggestion.
                </Lede>
                {bridge?.reason ? <Notice>{bridge.reason}</Notice> : null}
                {(bridge?.postures || []).map((p) => (
                  <Choice
                    key={p.id}
                    name="ob-bridge"
                    value={p.id}
                    checked={bridgePosture === p.id}
                    onSelect={setBridgePosture}
                    title={labelOf(p)}
                    description={p.description || p.summary}
                    note={p.id === bridge?.recommended ? "Suggested for what you described." : undefined}
                  />
                ))}
                <div style={{ marginTop: 18 }}>
                  <Eyebrow>Zone 3 · the network edge</Eyebrow>
                  <Toggle
                    id="ob-network-edge"
                    checked={networkEdge && zone3Supported}
                    onChange={setNetworkEdge}
                    disabled={!zone3Supported}
                    title={zone3Row?.label || "Filter DNS for the whole house"}
                    description={
                      <>
                        {zone3Row?.description ||
                          "Runs a DNS filter on this box and watches what the household's other devices ask for."}
                        <div style={{ marginTop: 4 }}>
                          {zone3Supported
                            ? "Nothing is started here. The commands are written down for you to run."
                            : zone3Reasons.join(" ") ||
                              "This machine cannot run Zone 3; your own edge gear, or nothing at all, is a complete answer."}
                        </div>
                      </>
                    }
                  />
                </div>
                {bridge?.note ? <Notice>{bridge.note}</Notice> : null}
              </>
            )}

            {step === S_KNOWLEDGE && (
              <>
                <Lede>
                  Point at a folder of knowledge packs (ZIM files) and documents, or skip. You can
                  add them any time from the Knowledge page.
                </Lede>
                <Field id="ob-zim" label="Knowledge folder" hint="A full path. It will be created if it does not exist.">
                  <TextInput id="ob-zim" value={zimDir} onChange={setZimDir} placeholder="e.g. /srv/homebot/knowledge or D:\HomeBot\knowledge" />
                </Field>
                <Toggle
                  id="ob-autoindex"
                  checked={autoIndex}
                  onChange={setAutoIndex}
                  title="Index what is in there"
                  description="Builds the search index in the background so answers can cite the source."
                />
              </>
            )}

            {step === S_REVIEW && (
              <>
                <Lede>This is the build you just described. It is written as a plain manifest you can re-read, diff, or re-run.</Lede>
                <div style={{ marginTop: 14 }}>
                  {(manifest?.summary || []).map((line, i) => (
                    <div
                      key={i}
                      style={{
                        padding: "8px 0",
                        borderTop: "1px solid var(--line)",
                        color: "var(--text)",
                        fontSize: 15,
                      }}
                    >
                      {line}
                    </div>
                  ))}
                  {(manifest?.summary || []).length === 0 && (
                    <Notice tone="warn">No manifest preview came back yet.</Notice>
                  )}
                </div>
                {(manifest?.warnings || []).length > 0 && (
                  <Notice tone="warn">
                    {(manifest?.warnings || []).map((line, i) => (
                      <div key={i}>{line}</div>
                    ))}
                  </Notice>
                )}
                {pending.length > 0 && (
                  <div style={{ marginTop: 18 }}>
                    <Eyebrow>Decided here, applied elsewhere</Eyebrow>
                    {pending.map((item, i) => (
                      <div
                        key={i}
                        style={{
                          padding: "8px 0",
                          borderTop: "1px solid var(--line)",
                          color: "var(--text2)",
                          fontSize: 14,
                        }}
                      >
                        {item.kind}
                        {typeof item.detail?.note === "string" ? `: ${item.detail.note}` : ""}
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}

            {step === S_DONE && (
              <>
                <Lede>{botName} is built. Everything above is recorded in the manifest on this box.</Lede>
                <div style={{ marginTop: 14 }}>
                  {doneLines.map((line, i) => (
                    <div
                      key={i}
                      style={{
                        padding: "8px 0",
                        borderTop: "1px solid var(--line)",
                        color: "var(--text)",
                        fontSize: 15,
                      }}
                    >
                      {line}
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>

          {/* Errors are announced, not just coloured. */}
          <div aria-live="assertive" style={{ minHeight: 0 }}>
            {error ? (
              <div
                style={{
                  marginTop: 16,
                  padding: "10px 14px",
                  borderRadius: 14,
                  border: "1px solid var(--puff-red)",
                  background: "var(--card2)",
                  color: "var(--text)",
                  fontSize: 14,
                }}
              >
                <span className="flex items-center gap-2">
                  <MsIcon name="error" size={18} />
                  {error}
                </span>
                {retry.current ? (
                  <div style={{ marginTop: 10 }}>
                    <PuffButton
                      label="Try again"
                      onClick={() => {
                        const again = retry.current;
                        setError("");
                        retry.current = null;
                        again?.();
                      }}
                    />
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>

          <div
            className="flex flex-wrap items-center gap-3"
            style={{ marginTop: 22, paddingTop: 16, borderTop: "1px solid var(--line)" }}
          >
            <PuffButton label="Back" onClick={back} disabled={step === 0 || busy} />
            {canSkip && <PuffButton label="Skip" onClick={() => void skip()} disabled={busy} />}
            <span style={{ flex: 1 }} />
            <PuffButton
              type="submit"
              kind="primary"
              label={busy ? "Working…" : vaultButtonLabel(step, vaultSealed, recoveryConfirmed, nextLabel)}
              disabled={busy}
            />
          </div>
        </Card>
      </form>
    </div>
  );
}

/** Storage step fields: key, label, one line of why. */
const STORAGE_FIELDS: [string, string, string][] = [
  ["state_dir", "State", "Where this build keeps its own files: logs, indexes, state."],
  ["knowledge_dir", "Knowledge", "Where knowledge packs (ZIM files) live."],
  ["vault_dir", "Vault", "Where the Vault's encrypted files live."],
  ["media_dir", "Media", "Your media library, if you have one. May be left empty."],
];

function headingFor(step: number, botName: string): string {
  switch (step) {
    case S_WELCOME:
      return "Name your bot";
    case S_POSTURE:
      return "How this build lives";
    case S_HARDWARE:
      return "What we are working with";
    case S_BRAIN:
      return "Pick a brain";
    case S_HOUSEHOLD:
      return "Who lives here";
    case S_GOAL:
      return "What it is for";
    case S_STORAGE:
      return "Where things go";
    case S_VAULT:
      return "Seal the Vault";
    case S_HA:
      return "Your smart home";
    case S_BRIDGE:
      return "The Bridge";
    case S_KNOWLEDGE:
      return "Knowledge packs";
    case S_REVIEW:
      return "Read it back";
    default:
      return `${botName} is ready`;
  }
}

/** The vault step's button says what it is about to do. */
function vaultButtonLabel(
  step: number,
  sealed: boolean,
  confirmed: boolean,
  fallback: string,
): string {
  if (step !== S_VAULT) return fallback;
  if (!sealed) return "Seal the Vault";
  if (!confirmed) return "I wrote it down";
  return fallback;
}
