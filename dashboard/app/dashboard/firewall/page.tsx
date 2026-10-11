"use client";

/**
 * Firewall — Zone 3's control side.
 *
 * Drawbridge (`/dashboard/bridge`) is the observe half: it says what every
 * device on this network is telling the outside world. This page is the other
 * half of the same instrument — what the house is ALLOWED to say — and the two
 * are cross-linked in their mastheads because a person who has just seen a
 * television phoning home is one click from the only screen that can stop it.
 *
 * What replaced what
 * ------------------
 * The page offers no rule-add or lockdown controls: their backends answer 501
 * on the Orin by design. A control that cannot work is worse than no control,
 * because the owner concludes the house is protected. Everything here reads a
 * live route through `apiGet`, and the
 * host firewall — whose writes really are unavailable on this box — is shown as
 * READ-ONLY with the backend's own reason, not as a broken button.
 *
 * The consent rule
 * ----------------
 * Every control on this page that changes what reaches the internet goes
 * through `useZone3Confirmation`: the route refuses with 409 and a proposal,
 * the owner reads the server's words, and a single-use ticket actuates. The
 * page never assumes a change succeeded — it reloads and re-renders from the
 * backend.
 *
 * puff puff pass
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  mdiRefresh,
  mdiShieldOutline,
  mdiLan,
  mdiLock,
  mdiDoorClosed,
  mdiCheck,
  mdiClose,
} from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import Icon from "../../_components/Icon";
import { apiGet } from "../../_lib/api";
import {
  Chip,
  Notice,
  SectionRule,
  TIER_HEADING,
  TIER_LABEL,
  TIER_NOTE,
  TIER_TONE,
  Stat,
  StatusStrip,
  TableFrame,
  Thead,
  type Tier,
} from "../_components/bridge/atoms";
import Zone3ConfirmDialog from "../_components/bridge/Zone3ConfirmDialog";
import { useZone3Confirmation } from "../../_lib/hooks/useZone3Confirmation";
import { PinEntry } from "../../_components/Consent/ConsentRequests";
import { resolveConsentRequest } from "../../_lib/services/consentService";

const POLL_INTERVAL = 60_000;

/* ── shapes, as the backend actually sends them ───────────────────────── */

type AdGuard = {
  reachable: boolean;
  version?: string;
  protection_enabled?: boolean;
  active_puffpai_rules?: number;
  queries_today?: number;
  blocked_today?: number;
  upstreams?: string[];
  url?: string;
  error?: string;
};

type Zone3Status = {
  success?: boolean;
  message?: string;
  managed_devices: number;
  mode_distribution: Record<string, number>;
  safety_critical_count: number;
  total_recorded_domains: number;
  total_blocked_domains: number;
  enforcing: boolean;
  mode: string;
  adguard: AdGuard | null;
};

type DevicePolicy = {
  ip: string;
  mac: string;
  manufacturer: string | null;
  category: string;
  hostname: string;
  mode: string;
  is_safety_critical: boolean;
  blocked_domains: string[];
  updated_at: string;
};

type Unmanaged = {
  ip: string;
  mac: string;
  hostname?: string;
  manufacturer?: string | null;
  category?: string;
  suggested_mode?: string;
  is_safety_critical?: boolean;
};

type FirewallStatus = {
  blocked_count: number;
  backend: string;
  lockdown_active: boolean;
  available: boolean;
  reason?: string;
};

type Door = {
  id?: string;
  name?: string;
  open: boolean;
  reason?: string;
  source?: string;
  writable?: boolean;
  app?: string | null;
  third_party?: string | null;
  hosts?: string[];
};

/**
 * One Home Assistant integration, joined server-side with any Zone 3 policy
 * recorded for it. `mode` is null when nobody has decided anything: absent is
 * not "open", and rendering it as a mode would claim a decision nobody made.
 */
type BucketRow = {
  id: string;
  tier: string;
  integration: string;
  label?: string;
  device_count: number;
  entity_count?: number;
  areas?: Record<string, number>;
  lookups_24h: number;
  watching_24h: number;
  mode: string | null;
  recorded_by?: string | null;
  domains_recorded: number;
  policy_supported: boolean;
};

type ConsentRequest = {
  entity_id: string;
  action?: string;
  reason?: string;
  requested_by?: string;
  created_at?: string;
};

/* ── vocabulary ───────────────────────────────────────────────────────── */

/**
 * The band header above each tier group. A heading, its tone and the one
 * sentence that says why the rows below are grouped that way — because
 * "Local" alone does not tell an owner that nothing leaves the house.
 */
function TierHeading({ tier, count }: { tier: Tier; count: number }) {
  return (
    <div className="mt-5 mb-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <h3
        className="text-[11px] font-bold tracking-[0.16em] uppercase"
        style={{ color: TIER_TONE[tier] }}
      >
        {TIER_HEADING[tier]}
      </h3>
      <span
        className="font-mono text-[11px] tabular-nums"
        style={{ color: "var(--text2)" }}
      >
        {count}
      </span>
      <span className="text-[11px]" style={{ color: "var(--text2)" }}>
        {TIER_NOTE[tier]}
      </span>
    </div>
  );
}

/** "Kitchen, Den, Porch", or a count once the list stops being a sentence. */
function rooms(areas?: Record<string, number>): string {
  const names = Object.keys(areas ?? {}).sort();
  if (names.length === 0) return "—";
  if (names.length <= 3) return names.join(", ");
  return `${names.length} rooms · ${names.slice(0, 2).join(", ")}…`;
}

const MODES = ["lockdown", "balanced", "open", "always_allow"] as const;

const MODE_TONE: Record<string, string> = {
  lockdown: "var(--pink)",
  balanced: "var(--sky)",
  open: "var(--lemon)",
  always_allow: "var(--mint)",
};

/**
 * What each mode actually does, in one clause. Shown in the selector's tooltip
 * because "BALANCED" is not self-explanatory and a person changing a mode is
 * deciding what their thermostat may reach.
 */
const MODE_GLOSS: Record<string, string> = {
  lockdown: "Only what the device needs to work. Everything else refused.",
  balanced: "Telemetry, ads and tracking refused. Normal traffic untouched.",
  open: "Nothing refused. Logged only.",
  always_allow: "Never filtered. For anything life-safety.",
};

function door_label(id: string | undefined): string {
  return (id ?? "door").replace(/_/g, " ");
}

/* ── page ─────────────────────────────────────────────────────────────── */

export default function FirewallPage() {
  const [zone3, setZone3] = useState<Zone3Status | null>(null);
  const [zone3Off, setZone3Off] = useState("");
  const [managed, setManaged] = useState<DevicePolicy[]>([]);
  const [unmanaged, setUnmanaged] = useState<Unmanaged[]>([]);
  const [host, setHost] = useState<FirewallStatus | null>(null);
  const [doors, setDoors] = useState<Door[]>([]);
  const [buckets, setBuckets] = useState<BucketRow[]>([]);
  const [consent, setConsent] = useState<ConsentRequest[]>([]);
  // Approved but the vault is locked: entity_id -> the server's PIN prompt.
  // Kept apart from `consent` so the next poll can't drop the PIN field.
  const [pinPrompts, setPinPrompts] = useState<Record<string, string>>({});
  const [pinDone, setPinDone] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    // Settled, not all-or-nothing: Zone 3 answers 503 when the sinkhole is not
    // running, and that must not blank the door list or the consent queue.
    const [z3, mg, um, fw, sys, cs, bk] = await Promise.allSettled([
      apiGet<Zone3Status>("/zone3/status"),
      apiGet<{ devices: DevicePolicy[] }>("/zone3/managed"),
      apiGet<{ devices: Unmanaged[] }>("/zone3/unmanaged"),
      apiGet<FirewallStatus>("/firewall/status"),
      apiGet<{ egress_doors: Door[] }>("/system/status"),
      apiGet<{ requests: ConsentRequest[] }>("/sentinel/consent/requests"),
      apiGet<{ buckets: BucketRow[] }>("/zone3/buckets"),
    ]);

    if (z3.status === "fulfilled") {
      setZone3(z3.value);
      setZone3Off("");
    } else {
      setZone3(null);
      setZone3Off(
        z3.reason instanceof Error
          ? z3.reason.message
          : "Zone 3 is not answering.",
      );
    }
    setManaged(mg.status === "fulfilled" ? (mg.value.devices ?? []) : []);
    setUnmanaged(um.status === "fulfilled" ? (um.value.devices ?? []) : []);
    setHost(fw.status === "fulfilled" ? fw.value : null);
    setDoors(sys.status === "fulfilled" ? (sys.value.egress_doors ?? []) : []);
    setConsent(cs.status === "fulfilled" ? (cs.value.requests ?? []) : []);
    setBuckets(bk.status === "fulfilled" ? (bk.value.buckets ?? []) : []);

    setError(
      sys.status === "rejected" && fw.status === "rejected"
        ? "The appliance is not answering."
        : "",
    );
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [load]);

  const confirmer = useZone3Confirmation(load);

  const enforcing = Boolean(zone3?.enforcing);
  const adguard = zone3?.adguard ?? null;

  const openDoors = useMemo(() => doors.filter((d) => d.open).length, [doors]);

  const activeRules = adguard?.active_puffpai_rules ?? 0;

  /* ── the three gated actions ────────────────────────────────────────── */

  const toggleEnforcement = useCallback(() => {
    confirmer.run({
      label: "Enforcement",
      endpoint: "/zone3/enforce",
      body: { enabled: !enforcing },
      kind: "zone3_enforce",
      target: "zone3",
      action: enforcing ? "off" : "on",
    });
  }, [confirmer, enforcing]);

  const setMode = useCallback(
    (device: DevicePolicy, mode: string) => {
      if (mode === device.mode) return;
      confirmer.run({
        label: `${device.hostname || device.ip} → ${mode}`,
        endpoint: "/zone3/policy",
        body: {
          ip: device.ip,
          mac: device.mac,
          mode,
          hostname: device.hostname,
          manufacturer: device.manufacturer,
          category: device.category,
          is_safety_critical: device.is_safety_critical,
        },
        kind: "zone3_mode",
        target: device.ip,
        action: mode,
      });
    },
    [confirmer],
  );

  /**
   * Bucket mode, through the SAME 409 -> /zone3/confirm -> redeem round trip
   * as every other Zone 3 change. Nothing here decides anything: the server
   * writes the proposal, including the "this breaks the integration" sentence
   * for LOCKDOWN, and the dialog renders the server's words.
   */
  const setBucketMode = useCallback(
    (bucket: BucketRow, mode: string) => {
      if (mode === (bucket.mode ?? "")) return;
      confirmer.run({
        label: `${bucket.integration} → ${mode}`,
        endpoint: "/zone3/bucket-policy",
        body: { bucket: bucket.id, mode },
        kind: "zone3_bucket",
        target: bucket.id,
        action: mode,
      });
    },
    [confirmer],
  );

  /**
   * Buckets in tier bands. Every tier is drawn, because the page rendering
   * only `cloud:*` is precisely how a house with nineteen integrations got a
   * blank section: `policy_supported` says whether there is a CONTROL, not
   * whether there is anything to say.
   */
  const byTier = useMemo(() => {
    const empty: Record<Tier, BucketRow[]> = {
      cloud: [],
      wifi_direct: [],
      local: [],
      unknown: [],
      system: [],
    };
    for (const b of buckets) {
      (empty[b.tier as Tier] ?? empty.unknown).push(b);
    }
    return empty;
  }, [buckets]);

  /** The System band is a single line until it is asked to be more. */
  const [systemOpen, setSystemOpen] = useState(false);

  const secure = useCallback(
    (device: Unmanaged) => {
      confirmer.run({
        label: `Secure ${device.hostname || device.ip}`,
        endpoint: "/zone3/secure",
        body: {
          ip: device.ip,
          mac: device.mac,
          hostname: device.hostname ?? "",
        },
        kind: "zone3_secure",
        target: device.ip,
        action: "secure",
      });
    },
    [confirmer],
  );

  const resolveConsent = useCallback(
    async (entity_id: string, approve: boolean) => {
      setBusy(true);
      try {
        const res = await resolveConsentRequest(entity_id, approve);
        if (approve && res.needs_pin) {
          // Approved; finish with the vault PIN right here (same as the
          // consent Approve card).
          setPinPrompts((p) => ({
            ...p,
            [entity_id]: res.message || "Enter your vault PIN to finish.",
          }));
        }
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : "That could not be recorded.");
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  return (
    <SectionMain>
      <Zone3ConfirmDialog controller={confirmer} />

      {/* Masthead. */}
      <header
        className="mb-4 flex flex-wrap items-end justify-between gap-4 border-b pb-3"
        style={{ borderColor: "var(--line)" }}
      >
        <div className="flex items-end gap-3">
          <Icon path={mdiShieldOutline} size="26" />
          <div>
            <h1
              className="text-2xl leading-none font-bold tracking-[0.18em] uppercase"
              style={{ color: "var(--text)" }}
            >
              Firewall
            </h1>
            <p className="mt-1.5 text-xs" style={{ color: "var(--text2)" }}>
              What this house is allowed to say to the outside world.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Link
            href="/dashboard/bridge"
            className="flex items-center gap-1.5 rounded px-2.5 py-1 text-[10px] font-semibold tracking-[0.12em] uppercase"
            style={{ background: "var(--chip)", color: "var(--text2)" }}
          >
            <Icon path={mdiLan} size="14" />
            Drawbridge
          </Link>
          <button
            onClick={load}
            className="flex items-center gap-1.5 rounded px-2.5 py-1 text-[10px] font-semibold tracking-[0.12em] uppercase"
            style={{ background: "var(--chip)", color: "var(--text)" }}
          >
            <Icon path={mdiRefresh} size="14" />
            Reload
          </button>
        </div>
      </header>

      {/* 1 — Posture. Facts only; nothing here changes anything. */}
      <StatusStrip>
        <Stat
          label="Zone 3"
          value={zone3 ? "On" : "Off"}
          tone={zone3 ? "var(--mint)" : "var(--text2)"}
        />
        <Stat
          label="Mode"
          value={enforcing ? "Enforce" : "Observe"}
          tone={enforcing ? "var(--pink)" : "var(--sky)"}
          title={
            enforcing
              ? "Rules are live — matching lookups are refused."
              : "Every lookup is logged. Nothing is blocked."
          }
        />
        <Stat
          label="AdGuard"
          value={
            !adguard
              ? "—"
              : adguard.reachable
                ? (adguard.version ?? "up")
                : "Unreachable"
          }
          tone={
            !adguard
              ? undefined
              : adguard.reachable
                ? "var(--mint)"
                : "var(--pink)"
          }
          title={adguard?.error || adguard?.url || undefined}
        />
        <Stat
          label="Upstream"
          value={adguard?.upstreams?.[0] ?? "—"}
          title={adguard?.upstreams?.join(", ")}
        />
        <Stat
          label="Rules"
          value={activeRules}
          tone={activeRules > 0 && enforcing ? "var(--pink)" : undefined}
        />
        <Stat label="Managed" value={zone3?.managed_devices ?? 0} />
        <Stat
          label="Host firewall"
          value={
            host
              ? `${host.lockdown_active ? "Lockdown" : "Default-deny"}, ${openDoors} door${openDoors === 1 ? "" : "s"}`
              : "—"
          }
          title={host?.reason || host?.backend}
        />
      </StatusStrip>

      {error && <Notice>{error}</Notice>}

      {zone3Off && (
        <Notice tone="var(--lemon)">
          Zone 3 is not running, so nothing on this network is being filtered or
          logged. {zone3Off}
        </Notice>
      )}

      {adguard && !adguard.reachable && (
        <Notice>
          AdGuard is not answering
          {adguard.error ? ` (${adguard.error})` : ""} — so nothing is being
          filtered right now, whatever the rules below say.
        </Notice>
      )}

      {host && !host.available && (
        <Notice tone="var(--sky)">
          The host firewall is readable but not writable on this appliance
          {/* The backend's reason is a full sentence and usually ends in a
              period; adding our own produced "in force.. Its state". */}
          {host.reason ? `: ${host.reason.replace(/\.\s*$/, "")}` : ""}. Its
          state is shown above; there are deliberately no block controls here,
          because they would not work.
        </Notice>
      )}

      {/* 2 — The switch. */}
      <SectionRule
        title="Enforcement"
        note={
          enforcing
            ? "Rules are live. Matching lookups are refused."
            : "Observing. Everything is logged, nothing is blocked."
        }
      />
      <div
        className="flex flex-wrap items-center justify-between gap-4 rounded border px-4 py-3"
        style={{ borderColor: "var(--line)", background: "var(--card2)" }}
      >
        <p className="max-w-xl text-xs leading-relaxed" style={{ color: "var(--text2)" }}>
          {enforcing
            ? `${activeRules} rule(s) are live across ${zone3?.managed_devices ?? 0} device(s). Turning this off returns every rule to commented-out and stops AdGuard protection — PuffPAI keeps watching and blocks nothing.`
            : `Turning this on makes ${activeRules || zone3?.total_recorded_domains || 0} recorded rule(s) live across ${zone3?.managed_devices ?? 0} device(s). ${zone3?.safety_critical_count ?? 0} safety-critical device(s) stay always-allowed.`}
        </p>
        <button
          onClick={toggleEnforcement}
          disabled={!zone3}
          className="rounded px-3.5 py-1.5 text-[10px] font-semibold tracking-[0.12em] uppercase disabled:cursor-not-allowed disabled:opacity-40"
          style={{
            background: enforcing ? "var(--chip)" : "var(--pink)",
            color: enforcing ? "var(--text)" : "#111",
          }}
        >
          {enforcing ? "Stop enforcing" : "Start enforcing"}
        </button>
      </div>

      {/* 3 — Device policy. */}
      <SectionRule
        title="Device policy"
        note={`${managed.length} managed · ${unmanaged.length} not yet secured`}
      />
      <TableFrame>
        <table className="puff-bridge-table w-full min-w-[820px] text-left text-xs">
          <Thead
            columns={["Device", "Address", "Kind", "Mode", "Domains", ""]}
          />
          <tbody>
            {loading && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center" style={{ color: "var(--text2)" }}>
                  Reading the policy store…
                </td>
              </tr>
            )}

            {!loading && managed.length === 0 && unmanaged.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-6 text-center" style={{ color: "var(--text2)" }}>
                  No devices under policy yet.
                </td>
              </tr>
            )}

            {managed.map((d) => {
              const locked = d.is_safety_critical;
              return (
                <tr
                  key={`m-${d.ip}`}
                  className="border-b last:border-0"
                  style={{ borderColor: "var(--line)" }}
                >
                  <td className="px-3 py-2" style={{ color: "var(--text)" }}>
                    <span className="flex items-center gap-1.5">
                      {locked && (
                        <span title="Safety-critical — always allowed, and the mode cannot be changed here">
                          <Icon path={mdiLock} size="13" />
                        </span>
                      )}
                      {d.hostname || d.manufacturer || d.ip}
                    </span>
                  </td>
                  <td className="px-3 py-2 font-mono" style={{ color: "var(--text2)" }}>
                    {d.ip}
                    <span className="ml-2" style={{ color: "var(--text2)" }}>
                      {d.mac}
                    </span>
                  </td>
                  <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                    {d.category}
                  </td>
                  <td className="px-3 py-2">
                    {locked ? (
                      <Chip label="always allow" tone="var(--mint)" title={MODE_GLOSS.always_allow} />
                    ) : (
                      <select
                        value={d.mode}
                        onChange={(e) => setMode(d, e.target.value)}
                        title={MODE_GLOSS[d.mode]}
                        className="rounded border px-1.5 py-0.5 text-[10px] font-semibold tracking-wider uppercase"
                        style={{
                          borderColor: "var(--line)",
                          background: "var(--chip)",
                          color: MODE_TONE[d.mode] ?? "var(--text)",
                        }}
                      >
                        {MODES.map((m) => (
                          <option key={m} value={m}>
                            {m.replace("_", " ")}
                          </option>
                        ))}
                      </select>
                    )}
                  </td>
                  <td
                    className="px-3 py-2 font-mono tabular-nums"
                    style={{ color: enforcing ? "var(--pink)" : "var(--text2)" }}
                    title={
                      enforcing
                        ? "Refused at DNS."
                        : "Recorded, not blocked — Zone 3 is observing."
                    }
                  >
                    {d.blocked_domains?.length ?? 0}
                  </td>
                  <td className="px-3 py-2" />
                </tr>
              );
            })}

            {unmanaged.map((d) => (
              <tr
                key={`u-${d.ip}`}
                className="border-b last:border-0"
                style={{ borderColor: "var(--line)" }}
              >
                <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                  {d.hostname || d.manufacturer || d.ip}
                </td>
                <td className="px-3 py-2 font-mono" style={{ color: "var(--text2)" }}>
                  {d.ip}
                  <span className="ml-2" style={{ color: "var(--text2)" }}>
                    {d.mac}
                  </span>
                </td>
                <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                  {d.category ?? "unknown"}
                </td>
                <td className="px-3 py-2">
                  <Chip label="unmanaged" tone="var(--text2)" />
                </td>
                <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                  —
                </td>
                <td className="px-3 py-2 text-right">
                  <button
                    onClick={() => secure(d)}
                    className="rounded px-2 py-0.5 text-[10px] font-semibold tracking-[0.12em] uppercase"
                    style={{ background: "var(--chip)", color: "var(--sky)" }}
                    title={`Suggested: ${(d.suggested_mode ?? "balanced").toUpperCase()}`}
                  >
                    Secure
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableFrame>


      {/* 3b — Home Assistant integrations. The hub's traffic, not a device's.

          Grouped by tier, and every tier is drawn. Rendering only `cloud:*`
          would show an empty section to a house whose hub reports mostly
          Home Assistant's own internals. A tier with nothing to
          block still has something to say. */}
      <SectionRule
        title="Home Assistant integrations"
        note={
          `${byTier.cloud.length} cloud-backed · ${byTier.local.length} local · ` +
          `${byTier.unknown.length} unclassified · ${byTier.system.length} internal`
        }
      />
      <p
        className="max-w-3xl px-1 pb-3 text-xs leading-relaxed"
        style={{ color: "var(--text2)" }}
      >
        These rules name Home Assistant as the client, because the hub is what
        talks to a vendor&rsquo;s cloud — not the devices themselves.
      </p>

      {loading && (
        <p className="px-1 pb-3 text-xs" style={{ color: "var(--text2)" }}>
          Reading the integration roll…
        </p>
      )}

      {!loading && buckets.length === 0 && (
        <p className="px-1 pb-3 text-xs" style={{ color: "var(--text2)" }}>
          No Home Assistant integrations found.
        </p>
      )}

      {/* ── Cloud-backed: the only tier with a control, because it is the
             only tier where there is anything to refuse. ── */}
      {!loading && buckets.length > 0 && (
        <>
          <TierHeading tier="cloud" count={byTier.cloud.length} />
          {byTier.cloud.length === 0 ? (
            <p
              className="rounded border px-3 py-2.5 text-xs leading-relaxed"
              style={{
                borderColor: "var(--line)",
                background: "var(--card2)",
                color: "var(--text2)",
              }}
            >
              No cloud-backed integrations. Nothing here leaves the house
              through Home Assistant.
            </p>
          ) : (
            <TableFrame>
              <table className="puff-bridge-table w-full min-w-[820px] text-left text-xs">
                <Thead
                  columns={[
                    "Integration",
                    "Devices",
                    "Lookups 24h",
                    "Mode",
                    "Domains",
                    "",
                  ]}
                />
                <tbody>
                  {byTier.cloud.map((b) => (
                    <tr
                      key={`b-${b.id}`}
                      className="border-b last:border-0"
                      style={{ borderColor: "var(--line)" }}
                    >
                      <td className="px-3 py-2" style={{ color: "var(--text)" }}>
                        <span className="flex flex-wrap items-center gap-2">
                          <Chip
                            label={TIER_LABEL.cloud}
                            tone={TIER_TONE.cloud}
                            title={TIER_NOTE.cloud}
                          />
                          <span className="font-semibold">
                            {b.label || b.integration}
                          </span>
                        </span>
                        {b.recorded_by === "tool" && (
                          <span
                            className="mt-1 block text-[10px]"
                            style={{ color: "var(--lemon)" }}
                            title="A chat tool recorded this mode. You have not approved it; it goes live with everything else when enforcement is switched on."
                          >
                            recorded by a chat tool
                          </span>
                        )}
                      </td>
                      <td
                        className="px-3 py-2 font-mono tabular-nums"
                        style={{ color: "var(--text2)" }}
                      >
                        {b.device_count}
                      </td>
                      <td
                        className="px-3 py-2 font-mono tabular-nums"
                        style={{ color: "var(--text2)" }}
                        title={`${b.watching_24h} of these were telemetry, ads or tracking`}
                      >
                        {b.lookups_24h}
                      </td>
                      <td className="px-3 py-2">
                        <select
                          value={b.mode ?? ""}
                          onChange={(e) => setBucketMode(b, e.target.value)}
                          title={
                            b.mode ? MODE_GLOSS[b.mode] : "No mode recorded yet."
                          }
                          className="rounded border px-1.5 py-0.5 text-[10px] font-semibold tracking-wider uppercase"
                          style={{
                            borderColor: "var(--line)",
                            background: "var(--chip)",
                            color: b.mode
                              ? (MODE_TONE[b.mode] ?? "var(--text)")
                              : "var(--text2)",
                          }}
                        >
                          {/* Absent is its own option, and it is not selectable
                              back to: "not set" is a fact about the ledger, not
                              a mode. */}
                          <option value="" disabled>
                            not set
                          </option>
                          {MODES.map((m) => (
                            <option key={m} value={m}>
                              {m.replace("_", " ")}
                            </option>
                          ))}
                        </select>
                        {b.mode === "lockdown" && (
                          <span
                            className="ml-2 text-[10px] font-semibold"
                            style={{ color: "var(--pink)" }}
                            title="LOCKDOWN refuses this vendor's cloud API for Home Assistant, not just its telemetry. The devices in this integration stop responding until you set it back."
                          >
                            breaks integration
                          </span>
                        )}
                      </td>
                      <td
                        className="px-3 py-2 font-mono tabular-nums"
                        style={{
                          color: enforcing ? "var(--pink)" : "var(--text2)",
                        }}
                        title={
                          enforcing
                            ? "Refused at DNS."
                            : "Recorded, not blocked — Zone 3 is observing."
                        }
                      >
                        {b.domains_recorded || "—"}
                      </td>
                      <td className="px-3 py-2" />
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableFrame>
          )}

          {/* ── Local and wifi_direct: listed in full, with what they are
                 instead of a control that would do nothing. ── */}
          {(["local", "wifi_direct"] as const).map((tier) =>
            byTier[tier].length === 0 ? null : (
              <div key={tier}>
                <TierHeading tier={tier} count={byTier[tier].length} />
                <TableFrame>
                  <table className="puff-bridge-table w-full min-w-[720px] text-left text-xs">
                    <Thead
                      columns={["Integration", "Devices", "Entities", "Rooms", ""]}
                    />
                    <tbody>
                      {byTier[tier].map((b) => (
                        <tr
                          key={`b-${b.id}`}
                          className="border-b last:border-0"
                          style={{ borderColor: "var(--line)" }}
                        >
                          <td className="px-3 py-2" style={{ color: "var(--text)" }}>
                            <span className="flex flex-wrap items-center gap-2">
                              <Chip
                                label={TIER_LABEL[tier]}
                                tone={TIER_TONE[tier]}
                                title={TIER_NOTE[tier]}
                              />
                              <span className="font-semibold">
                                {b.label || b.integration}
                              </span>
                            </span>
                          </td>
                          <td
                            className="px-3 py-2 font-mono tabular-nums"
                            style={{ color: "var(--text2)" }}
                          >
                            {b.device_count}
                          </td>
                          <td
                            className="px-3 py-2 font-mono tabular-nums"
                            style={{ color: "var(--text2)" }}
                          >
                            {b.entity_count ?? 0}
                          </td>
                          <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                            {rooms(b.areas)}
                          </td>
                          <td
                            className="px-3 py-2 text-right text-[11px]"
                            style={{ color: "var(--mint)" }}
                            title="Local only — nothing leaves the house for this integration, so there is nothing to block."
                          >
                            local only — nothing leaves
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </TableFrame>
              </div>
            ),
          )}

          {/* ── Unknown: named, never hidden. A bucket the owner can answer
                 is worth more than a device quietly dropped. ── */}
          {byTier.unknown.length > 0 && (
            <>
              <TierHeading tier="unknown" count={byTier.unknown.length} />
              <TableFrame>
                <table className="puff-bridge-table w-full min-w-[720px] text-left text-xs">
                  <Thead
                    columns={["Integration", "Devices", "Entities", "Rooms", ""]}
                  />
                  <tbody>
                    {byTier.unknown.map((b) => (
                      <tr
                        key={`b-${b.id}`}
                        className="border-b last:border-0"
                        style={{ borderColor: "var(--line)" }}
                      >
                        <td className="px-3 py-2" style={{ color: "var(--text)" }}>
                          <span className="flex flex-wrap items-center gap-2">
                            <Chip
                              label={TIER_LABEL.unknown}
                              tone={TIER_TONE.unknown}
                              title={TIER_NOTE.unknown}
                            />
                            <span className="font-semibold">
                              {b.label || b.integration}
                            </span>
                          </span>
                        </td>
                        <td
                          className="px-3 py-2 font-mono tabular-nums"
                          style={{ color: "var(--text2)" }}
                        >
                          {b.device_count}
                        </td>
                        <td
                          className="px-3 py-2 font-mono tabular-nums"
                          style={{ color: "var(--text2)" }}
                        >
                          {b.entity_count ?? 0}
                        </td>
                        <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                          {rooms(b.areas)}
                        </td>
                        <td
                          className="px-3 py-2 text-right text-[11px]"
                          style={{ color: "var(--text2)" }}
                        >
                          not classified yet
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableFrame>
            </>
          )}

          {/* ── System: one line, expandable. Thirteen rows about the sun and
                 the backup job would bury the two that matter. ── */}
          {byTier.system.length > 0 && (
            <div className="mt-5">
              <button
                type="button"
                onClick={() => setSystemOpen((v) => !v)}
                className="flex w-full cursor-pointer items-center gap-2 rounded border px-3 py-2 text-left text-xs transition hover:opacity-80"
                style={{
                  borderColor: "var(--line)",
                  background: "var(--card2)",
                  color: "var(--text2)",
                }}
                aria-expanded={systemOpen}
              >
                <Chip
                  label={TIER_LABEL.system}
                  tone={TIER_TONE.system}
                  title={TIER_NOTE.system}
                />
                <span>
                  {byTier.system.length} Home Assistant internal
                  {byTier.system.length === 1 ? "" : "s"}
                </span>
                <span className="flex-1" />
                <span style={{ color: "var(--text2)" }}>
                  {systemOpen ? "hide" : "show"}
                </span>
              </button>
              {systemOpen && (
                <p
                  className="mt-1.5 rounded border px-3 py-2 text-xs leading-relaxed"
                  style={{
                    borderColor: "var(--line)",
                    background: "var(--card2)",
                    color: "var(--text2)",
                  }}
                >
                  {TIER_NOTE.system}{" "}
                  {byTier.system
                    .map((b) => b.label || b.integration)
                    .join(", ")}
                </p>
              )}
            </div>
          )}
        </>
      )}

      {/* 4 — Doors. The box's own egress, not the household's. */}
      <SectionRule
        title="What can leave this house"
        note={`${openDoors} of ${doors.length} door(s) open — PuffPAI's own outbound reach`}
      />
      <TableFrame>
        <table className="puff-bridge-table w-full min-w-[640px] text-left text-xs">
          <Thead columns={["Door", "State", "Set by", "Goes to"]} />
          <tbody>
            {doors.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center" style={{ color: "var(--text2)" }}>
                  {loading ? "Reading the door registry…" : "No doors reported."}
                </td>
              </tr>
            )}
            {doors.map((door) => (
              <tr
                key={door.id ?? door.name}
                className="border-b last:border-0"
                style={{ borderColor: "var(--line)" }}
              >
                <td className="px-3 py-2" style={{ color: "var(--text)" }}>
                  <span className="flex items-center gap-1.5">
                    <Icon path={mdiDoorClosed} size="13" />
                    {door_label(door.id ?? door.name)}
                    {door.app && (
                      <Chip label={door.app} tone="var(--lav)" />
                    )}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <Chip
                    label={door.open ? "open" : "shut"}
                    tone={door.open ? "var(--lemon)" : "var(--text2)"}
                    title={door.reason}
                  />
                </td>
                <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                  {door.source ?? "—"}
                </td>
                <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                  {door.third_party ?? door.hosts?.slice(0, 2).join(", ") ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableFrame>
      {/* Read-only here on purpose: the doors have their own owner-write route
          and their own disclosure copy on the Settings card, and duplicating a
          switch is how two surfaces come to disagree about one boolean. */}
      <p className="mt-1.5 text-[11px]" style={{ color: "var(--text2)" }}>
        Shown here for context. Doors are opened and shut on the Settings card,
        where each one's disclosure is printed beside it.
      </p>

      {/* 5 — Consent queue. */}
      <SectionRule
        title="Waiting on you"
        note={
          consent.length === 0
            ? "Nothing is waiting"
            : `${consent.length} request(s) Drawbridge will not decide alone`
        }
      />
      {pinDone && <p className="mb-2 text-xs" style={{ color: "var(--text2)" }}>{pinDone}</p>}
      {Object.entries(pinPrompts).map(([id, prompt]) => (
        <div
          key={id}
          className="mb-2 rounded border px-3 py-2.5"
          style={{ borderColor: "var(--line)" }}
        >
          <p className="font-mono text-xs" style={{ color: "var(--text)" }}>{id}</p>
          <PinEntry
            entityId={id}
            prompt={prompt}
            tone="theme"
            onDone={(msg) => {
              setPinPrompts((p) => {
                const next = { ...p };
                delete next[id];
                return next;
              });
              setPinDone(msg);
              void load();
            }}
          />
        </div>
      ))}
      <TableFrame>
        <table className="puff-bridge-table w-full min-w-[640px] text-left text-xs">
          <Thead columns={["What", "Why", "Asked", ""]} />
          <tbody>
            {consent.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center" style={{ color: "var(--text2)" }}>
                  Nothing is waiting for your decision.
                </td>
              </tr>
            )}
            {consent.filter((req) => !pinPrompts[req.entity_id]).map((req) => (
              <tr
                key={req.entity_id}
                className="border-b last:border-0"
                style={{ borderColor: "var(--line)" }}
              >
                <td className="px-3 py-2 font-mono" style={{ color: "var(--text)" }}>
                  {req.entity_id}
                </td>
                <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                  {req.reason || req.action || "—"}
                </td>
                <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                  {req.requested_by || "—"}
                </td>
                <td className="px-3 py-2 text-right">
                  <span className="flex justify-end gap-1.5">
                    <button
                      onClick={() => resolveConsent(req.entity_id, true)}
                      disabled={busy}
                      className="flex items-center gap-1 rounded px-2 py-0.5 text-[10px] font-semibold tracking-[0.12em] uppercase disabled:opacity-40"
                      style={{ background: "var(--chip)", color: "var(--mint)" }}
                    >
                      <Icon path={mdiCheck} size="13" />
                      Allow
                    </button>
                    <button
                      onClick={() => resolveConsent(req.entity_id, false)}
                      disabled={busy}
                      className="flex items-center gap-1 rounded px-2 py-0.5 text-[10px] font-semibold tracking-[0.12em] uppercase disabled:opacity-40"
                      style={{ background: "var(--chip)", color: "var(--pink)" }}
                    >
                      <Icon path={mdiClose} size="13" />
                      Deny
                    </button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableFrame>
    </SectionMain>
  );
}
