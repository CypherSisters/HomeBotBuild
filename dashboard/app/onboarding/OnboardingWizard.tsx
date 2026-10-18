"use client";

/**
 * First-run setup, the browser face of HomeBot's `setup` flow.
 *
 * Four questions and a build: look at the hardware, pick the brain's size
 * (with one button to download it when it is not here yet), set up the Vault,
 * and, if the owner wants, connect their Home Assistant. Then the build, then the
 * dashboard. A first-run code step comes first only when this browser is not
 * on the machine itself.
 *
 * Everything else takes HomeBot's own defaults without a question: the bot's
 * name, the build posture (nothing reaches the internet until the owner switches it
 * on), every HomeBot part switched on, the storage folders, and the Bridge
 * posture the backend ships as its default. Each is still sent through its own
 * /setup/* route, so the manifest records the backend's answer, not one this
 * page invented. The household, the knowledge folder and the rest are set
 * later from their own pages.
 *
 * SECRETS. The vault PIN, its recovery code and the Home Assistant token live
 * in component state and nowhere else: not localStorage, not sessionStorage,
 * not the URL, never logged. They are POSTed through the same-origin /api/pai
 * proxy (which adds the appliance's own key server-side) and cleared the moment
 * the step that owns them is finished, and again on unmount.
 *
 * RESILIENCE. Every step's load and every step's save can fail two ways: the
 * backend is unreachable, or it answered `{ok:false}`. Both render one line and
 * a Retry, in an aria-live region. Back always works; the Home Assistant step
 * carries a Skip. No step can dead-end.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import MsIcon from "../_components/MsIcon";
import { HA_TOKEN_HOW, plainHaWords } from "../_components/HomeAssistantConnect";
import { recoveryCodeMatches, RECOVERY_CONFIRM_FAILED } from "../_lib/recoveryCode";
import { pinRefusal } from "../_lib/vaultPin";
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
  getModelPull,
  getSetupStatus,
  getStorage,
  isBackendDown,
  markComplete,
  saveBridge,
  saveHa,
  saveIdentity,
  saveOllama,
  savePosture,
  saveStorage,
  saveSurfaces,
  setSetupCode,
  startModelPull,
  testHa,
  unwrap,
  type BridgeOptions,
  type BrainMenu,
  type Census,
  type CrewBudget,
  type ManifestSections,
  type ModelPull,
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
  microLabel,
} from "./ui";

type StepId = "welcome" | "hardware" | "brain" | "vault" | "ha" | "build";

const STEP_LABELS: Record<StepId, string> = {
  welcome: "Welcome",
  hardware: "Hardware",
  brain: "Brain size",
  vault: "Vault",
  ha: "Home Assistant",
  build: "Build",
};

/** The vault PIN rules, shown before the field, not after the rejection. */
const PIN_RULES = [
  "At least 8 characters that are not all numbers, or at least 10 digits.",
  "No spaces.",
  "Not a run or a repeat (123456, 111111).",
  "Not a birthday or an address anyone could guess.",
];

const HEADING_ID = "onboarding-step-heading";

/** How often a running model download is read back. */
const PULL_POLL_MS = 2000;

function labelOf(entry: { label?: string; name?: string; id: string }): string {
  return entry.label || entry.name || entry.id;
}

export default function OnboardingWizard() {
  const router = useRouter();
  const params = useSearchParams();
  const rerun = params.get("rerun") === "1";

  const [booting, setBooting] = useState(true);
  const [step, setStep] = useState(0);
  const [built, setBuilt] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  /** What to run again when the owner presses Retry. */
  const retry = useRef<null | (() => void)>(null);
  const heading = useRef<HTMLHeadingElement | null>(null);

  /**
   * The first-run setup code, and whether the backend wants one.
   *
   * Component state is the authoritative copy, the same rule the vault PIN and
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
  /** The silent defaults that must land before any LAN address is saved. */
  const [basicsSaved, setBasicsSaved] = useState(false);

  const [census, setCensus] = useState<Census | null>(null);
  const [hwSummary, setHwSummary] = useState<string[]>([]);
  const [statedRam, setStatedRam] = useState("");

  const [menu, setMenu] = useState<BrainMenu | null>(null);
  const [crew, setCrew] = useState<CrewBudget | null>(null);
  const [model, setModel] = useState("");
  const [pull, setPull] = useState<{ model: string; state: string; line: string } | null>(null);

  // Vault. PIN and code are component state only, see the module docstring.
  const [pin, setPin] = useState("");
  const [pin2, setPin2] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [recoveryNotice, setRecoveryNotice] = useState<string[]>([]);
  const [recoveryTyped, setRecoveryTyped] = useState("");
  const [recoveryConfirmed, setRecoveryConfirmed] = useState(false);
  const [vaultSealed, setVaultSealed] = useState(false);

  const [haUrl, setHaUrl] = useState(DEFAULT_HA_URL);
  const [haToken, setHaToken] = useState("");
  const [haResult, setHaResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [haTesting, setHaTesting] = useState(false);
  const [haConnected, setHaConnected] = useState(false);

  const [doneLines, setDoneLines] = useState<string[]>([]);

  // The manifest is assembled from what the caller sends and NOTHING else (see
  // ManifestSections). Each step keeps the block the backend handed it here;
  // the build step commits the lot.
  const sections = useRef<ManifestSections>({});
  const [ollamaUrl, setOllamaUrl] = useState("");

  const path: StepId[] = setupCodeRequired
    ? ["welcome", "hardware", "brain", "vault", "ha", "build"]
    : ["hardware", "brain", "vault", "ha", "build"];
  const current: StepId = path[Math.min(step, path.length - 1)];

  const statedRamNum = useCallback((): number | undefined => {
    const n = parseFloat(statedRam);
    return Number.isFinite(n) && n > 0 ? n : undefined;
  }, [statedRam]);

  /** Keep a manifest block a step's response carried. Ignores anything that is
   *  not an object, so a route that stops returning one cannot poison the
   *  commit: the section simply falls back to the backend's own default. */
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
      if (d.goal) setDefaultGoal(d.goal);
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
  }, [step, built, booting]);

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

  useEffect(() => {
    if (booting) return;
    if (current === "hardware" && !census) void loadHardware();
    if (current === "brain" && !menu) void loadBrain();
    // Loaders are stable callbacks; the guards above keep this to one call.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, booting]);

  // ── the brain download ─────────────────────────────────────────────
  const readPull = useCallback(
    (id: string, reply: Reply<ModelPull>) => {
      if (reply.status === 404) {
        setPull({
          model: id,
          state: "failed",
          line: `This box cannot download it from here. On the box, run: ollama pull ${id}`,
        });
        return;
      }
      if (!reply.ok) {
        setPull({ model: id, state: "failed", line: reply.error });
        return;
      }
      const state = reply.data?.state || "running";
      const line = reply.data?.line || reply.data?.message || "";
      setPull({ model: id, state, line });
      if (state === "done") void loadBrain();
    },
    [loadBrain],
  );

  const downloadBrain = useCallback(
    async (id: string) => {
      setPull({ model: id, state: "running", line: "Starting the download." });
      readPull(id, await startModelPull(id));
    },
    [readPull],
  );

  useEffect(() => {
    if (!pull || pull.state !== "running") return;
    const timer = setTimeout(() => {
      void getModelPull(pull.model).then((reply) => readPull(pull.model, reply));
    }, PULL_POLL_MS);
    return () => clearTimeout(timer);
  }, [pull, readPull]);

  // ── the vault ceremony ─────────────────────────────────────────────
  const sealVault = useCallback(async () => {
    // The same rule the box enforces (../_lib/vaultPin.ts), said before the
    // vault is asked.
    const refusal = pinRefusal(pin);
    if (refusal) {
      setError(refusal);
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
    const reply = await testHa(haUrl.trim(), haToken);
    setHaTesting(false);
    if (!reply.ok || reply.data?.ok === false) {
      // `error` is the backend's code; the sentence is in `message`.
      setHaResult({ ok: false, text: plainHaWords(reply.data?.message || reply.error) });
      return;
    }
    const where = reply.data?.location_name ? ` (${reply.data.location_name})` : "";
    setHaResult({ ok: true, text: `Home Assistant answered${where}.` });
  }, [haToken, haUrl]);

  /** "No Home Assistant" is a complete answer; it is recorded, not left blank. */
  const recordNoHa = useCallback(async () => {
    setHaToken("");
    setHaConnected(false);
    const reply = await saveHa(false);
    record("integrations", {
      home_assistant: reply.data?.ha || { present: false },
      media_library: { present: false },
    });
  }, [record]);

  // ── HomeBot's defaults, sent through the same routes setup always uses ──
  /** Name and posture. Saved before any address on the home network is, because
   *  the address check reads the posture the owner's build runs under. */
  const saveBasics = useCallback(async (): Promise<boolean> => {
    if (basicsSaved) return true;
    const name = botName.trim() || "HomeBot";
    const identity = await saveIdentity(name, persona || undefined);
    if (!identity.ok) {
      fail(identity, () => void advance());
      return false;
    }
    // `identity` is the one section with no block route: assemble_manifest
    // reads `identity.identity` and `identity.name`.
    record("identity", { identity: identity.data?.persona || persona, name });
    // Both axes left to the backend: it records them as defaulted, not chosen.
    const posture = await savePosture("", "", []);
    if (!posture.ok) {
      fail(posture, () => void advance());
      return false;
    }
    record("posture", posture.data?.manifest_block);
    setBasicsSaved(true);
    return true;
    // `advance` is defined below and stable enough for the retry closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [basicsSaved, botName, fail, persona, record]);

  /** Every HomeBot part on, the default folders, the default Bridge posture,
   *  then the manifest and the finished flag. */
  const build = useCallback(async (): Promise<boolean> => {
    const again = () => void advance();
    if (!(await saveBasics())) return false;

    // Every surface the build knows, switched on.
    const derived = await saveSurfaces(defaultGoal, [], []);
    if (!derived.ok) {
      fail(derived, again);
      return false;
    }
    const d = unwrap<SurfaceDerivation>(derived.data, "surfaces");
    const missing = (d.available || []).filter((s) => !(d.surfaces || []).includes(s));
    const all = missing.length ? await saveSurfaces(defaultGoal, missing, []) : derived;
    if (!all.ok) {
      fail(all, again);
      return false;
    }
    record("surfaces", all.data?.manifest_block);

    const storage = await getStorage();
    if (!storage.ok) {
      fail(storage, again);
      return false;
    }
    const placed = await saveStorage(unwrap<StoragePaths>(storage.data, "paths"));
    const problems = Object.values(placed.data?.problems || {});
    if (!placed.ok || problems.length > 0) {
      setError(problems.length ? problems.join(" ") : placed.error);
      retry.current = again;
      return false;
    }
    record("storage", placed.data?.manifest_block);

    const ladder = await getBridge();
    if (!ladder.ok) {
      fail(ladder, again);
      return false;
    }
    const b = unwrap<BridgeOptions>(ladder.data, "bridge");
    const posture = b.default || b.postures?.[0]?.id || "";
    // Nobody picks the Bridge during HomeBot setup: the default is recorded
    // as the default.
    const bridge = await saveBridge(posture, false, true);
    if (!bridge.ok) {
      fail(bridge, again);
      return false;
    }
    // The backend reconciles the choice against the build posture (stricter
    // wins), so the block it answers with is what the build record gets.
    record("bridge", bridge.data?.bridge);

    const written = await commitManifest(sections.current);
    if (!written.ok) {
      fail(written, again);
      return false;
    }
    const done = await markComplete();
    if (!done.ok) {
      fail(done, again);
      return false;
    }
    // Setup is over, so the first-run code is over: the backend has just
    // destroyed its copy and nothing further needs the header.
    setSetupCodeInput("");
    setSetupCodeRequired(false);
    setSetupCode("");
    setDoneLines(written.data?.summary || []);
    return true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultGoal, fail, record, saveBasics]);

  // ── commit for the current step ────────────────────────────────────
  const commit = useCallback(async (): Promise<boolean> => {
    switch (current) {
      case "welcome":
      case "hardware":
        // The first answer that needs the code proves it, and the defaults
        // that later steps depend on are in place before they are needed.
        return saveBasics();
      case "brain": {
        if (!model) {
          setError("Pick a size. You can change it later from Settings.");
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
      case "vault": {
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
      case "ha": {
        if (!haToken) {
          // Nothing typed: the same as Skip.
          await recordNoHa();
          return true;
        }
        if (!haUrl.trim()) {
          setError("Home Assistant needs its address.");
          return false;
        }
        const reply = await saveHa(true, haUrl.trim(), haToken);
        if (!reply.ok) {
          setError(plainHaWords(reply.error));
          retry.current = null;
          return false;
        }
        // The `ha` block records presence, the address and WHETHER a token was
        // placed, never the token, which stays on the box.
        record("integrations", {
          home_assistant: reply.data?.ha || { present: true },
          media_library: { present: false },
        });
        // The token is written into the backend's config; the browser forgets it.
        setHaToken("");
        setHaConnected(true);
        return true;
      }
      case "build":
        return build();
      default:
        return true;
    }
    // `advance` is defined below and stable enough for the retry closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    build,
    confirmRecovery,
    crew,
    current,
    fail,
    haToken,
    haUrl,
    menu,
    model,
    ollamaUrl,
    record,
    recordNoHa,
    recoveryConfirmed,
    saveBasics,
    sealVault,
    vaultSealed,
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
    if (!ok) return;
    retry.current = null;
    if (current === "build") setBuilt(true);
    else setStep((s) => Math.min(s + 1, path.length - 1));
  }, [busy, commit, current, path.length]);

  const back = useCallback(() => {
    clearError();
    setStep((s) => Math.max(0, s - 1));
  }, [clearError]);

  const skipHa = useCallback(async () => {
    clearError();
    setBusy(true);
    await recordNoHa();
    setBusy(false);
    setStep((s) => Math.min(s + 1, path.length - 1));
  }, [clearError, path.length, recordNoHa]);

  // The brain step's current pick, so a model that is not on this box can say
  // so and offer the download.
  const pickedBrain = (menu?.options || []).find((o) => o.id === model);
  const pullHere = pull && pull.model === model ? pull : null;

  // ── render ─────────────────────────────────────────────────────────
  if (booting) {
    return (
      <div style={{ ...microLabel, padding: 32 }} aria-live="polite">
        Checking your setup...
      </div>
    );
  }

  const labels = path.map((id) => STEP_LABELS[id]);
  const nextLabel = built
    ? "Open the dashboard"
    : current === "build"
      ? "Build it"
      : current === "ha" && !haToken
        ? "Continue without it"
        : "Continue";

  return (
    <div style={{ maxWidth: 860, margin: "0 auto", padding: "8px 0 40px" }}>
      <Stepper labels={labels} current={built ? path.length : step} />

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (built) {
            router.replace("/dashboard");
            return;
          }
          void advance();
        }}
        style={{ marginTop: 18 }}
      >
        <Card>
          <Eyebrow>
            {built
              ? "Done"
              : `Step ${String(step + 1).padStart(2, "0")} of ${path.length} · ${STEP_LABELS[current]}`}
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
            {built ? `${botName} is ready` : headingFor(current)}
          </h1>

          <div style={{ marginTop: 10 }}>
            {!built && current === "welcome" && (
              <>
                <Lede>
                  This box runs your household&apos;s AI. What it learns stays on this computer, and
                  nothing leaves it without your consent. First, the code that proves you are at the box.
                </Lede>
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
                  It proves the person answering these questions is the person who owns the box, and
                  the answers include the Vault.
                </Notice>
              </>
            )}

            {!built && current === "hardware" && (
              <>
                <Lede>What this computer has. Correct the memory if the reading is wrong.</Lede>
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
                  <PuffButton
                    label="Read the hardware again"
                    onClick={() => {
                      setMenu(null);
                      void loadHardware();
                    }}
                  />
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

            {!built && current === "brain" && (
              <>
                <Lede>
                  A bigger brain thinks better and needs more memory. These fit this computer, biggest
                  first. You can change it later in Settings.
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
                      o.installed ? "already on this computer" : "not on this computer yet",
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                    note={o.id === menu?.recommended?.id ? "Suggested for this computer." : undefined}
                  />
                ))}
                {pickedBrain && !pickedBrain.installed && (
                  <Notice>
                    <div>
                      {labelOf(pickedBrain)} is not on this computer yet
                      {pickedBrain.disk_gb ? ` (${pickedBrain.disk_gb} GB)` : ""}. Downloading it uses
                      the internet once: Ollama fetches it from its own library. Pressing Download
                      turns the Model downloads switch on for this one download, and turns it back off
                      when the download ends. If you had already turned that switch on, it stays on.
                    </div>
                    <div style={{ marginTop: 10 }}>
                      <PuffButton
                        label={pullHere?.state === "running" ? "Downloading..." : "Download it"}
                        onClick={() => void downloadBrain(pickedBrain.id)}
                        disabled={pullHere?.state === "running" || pullHere?.state === "done"}
                      />
                    </div>
                    {pullHere?.line ? (
                      <div aria-live="polite" style={{ marginTop: 8 }}>
                        {pullHere.line}
                      </div>
                    ) : null}
                    <div style={{ marginTop: 8, color: "var(--text3)" }}>
                      You can also go on now and download it later; your bot answers once it is here.
                    </div>
                  </Notice>
                )}
                {(menu?.options || []).length === 0 && (
                  <Notice tone="warn">
                    {menu?.message || "No list of sizes came back. You can set the brain later in Settings."}
                  </Notice>
                )}
                {(menu?.too_big || []).length > 0 && (
                  <Notice tone="warn">
                    Too big for this computer: {(menu?.too_big || []).map((t) => t.label).join(", ")}.
                  </Notice>
                )}
              </>
            )}

            {!built && current === "vault" && (
              <>
                {!vaultSealed && (
                  <>
                    <Lede>
                      The Vault holds what only the household should see. One PIN opens it, and that
                      PIN is never stored here. Installing apps and storing keys later asks for it too.
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
                    The Vault is set up and the recovery code is confirmed. Keep the paper somewhere
                    the household can find it.
                  </Notice>
                )}
              </>
            )}

            {!built && current === "ha" && (
              <>
                <Lede>
                  If you run Home Assistant, connect it now so {botName} can see and run your home. You
                  can skip this and connect it later from the Smart Home page or Settings.
                </Lede>
                <Field id="ob-ha-url" label="Home Assistant address">
                  <TextInput id="ob-ha-url" value={haUrl} onChange={setHaUrl} />
                </Field>
                <Field id="ob-ha-token" label="Long-lived access token" hint={HA_TOKEN_HOW}>
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
                    label={haTesting ? "Testing..." : "Test"}
                    onClick={() => void runHaTest()}
                    disabled={haTesting || !haUrl.trim() || !haToken}
                  />
                </div>
                {haResult ? (
                  <Notice tone={haResult.ok ? "ok" : "warn"}>{haResult.text}</Notice>
                ) : null}
              </>
            )}

            {!built && current === "build" && (
              <>
                <Lede>Here is what gets built. Press Build it when you are ready.</Lede>
                <div style={{ marginTop: 14 }}>
                  <FactRow label="Name" value={botName} />
                  <FactRow label="Brain" value={pickedBrain ? labelOf(pickedBrain) : model || "not picked"} />
                  <FactRow label="Vault" value={recoveryConfirmed ? "set up" : "not set up"} />
                  <FactRow label="Home Assistant" value={haConnected ? haUrl : "not connected"} />
                  <FactRow label="HomeBot parts" value="all on" />
                  <FactRow label="Household apps" value="AlarmFlow, Calendar, FreshTable, FitFam, Trivia" />
                  <FactRow label="The Bridge" value="closed" />
                </div>
                <Notice>
                  The Bridge starts closed: nothing reaches the internet until you switch it on. You
                  can open single services later on the Firewall page.
                </Notice>
              </>
            )}

            {built && (
              <>
                <Lede>Everything above is recorded on this box. The dashboard is next.</Lede>
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
            {!built && <PuffButton label="Back" onClick={back} disabled={step === 0 || busy} />}
            {!built && current === "ha" && haToken ? (
              <PuffButton label="Skip" onClick={() => void skipHa()} disabled={busy} />
            ) : null}
            <span style={{ flex: 1 }} />
            <PuffButton
              type="submit"
              kind="primary"
              label={busy ? "Working..." : vaultButtonLabel(current, built, vaultSealed, recoveryConfirmed, nextLabel)}
              disabled={busy}
            />
          </div>
        </Card>
      </form>
    </div>
  );
}

function headingFor(step: StepId): string {
  switch (step) {
    case "welcome":
      return "Welcome";
    case "hardware":
      return "What we are working with";
    case "brain":
      return "Pick the brain's size";
    case "vault":
      return "Set up the Vault";
    case "ha":
      return "Connect your Home Assistant";
    default:
      return "Build it";
  }
}

/** The vault step's button says what it is about to do. */
function vaultButtonLabel(
  step: StepId,
  built: boolean,
  sealed: boolean,
  confirmed: boolean,
  fallback: string,
): string {
  if (built || step !== "vault") return fallback;
  if (!sealed) return "Set up the Vault";
  if (!confirmed) return "I wrote it down";
  return fallback;
}
