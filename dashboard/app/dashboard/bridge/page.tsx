"use client";

/**
 * Drawbridge — Zone 3 device roll + local telemetry ledger.
 *
 * Reads the backend's /bridge/* blueprint by calling API_URL (app/_lib/api.ts)
 * directly with a Bearer API key — not through the /api/pai proxy. Strictly
 * observe-only: there is no control on this page that blocks, quarantines or
 * changes anything on the network. The "Enforce" affordance is deliberately
 * inert — enforcement is a switch Alex throws, not a button a panel offers.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { mdiRefresh, mdiClose, mdiLan, mdiShieldOutline } from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import Icon from "../../_components/Icon";
import { apiGet, apiPost } from "../../_lib/api";
import ManagedByHomeAssistant from "../_components/bridge/managed";

const POLL_INTERVAL = 60_000;

type BridgeDevice = {
  key: string;
  ip: string;
  mac: string;
  hostname: string;
  mdns_names: string[];
  mdns_services: string[];
  ha_entity_id: string;
  ha_name: string;
  ha_area: string;
  adguard_name: string;
  manufacturer: string;
  category: string;
  first_seen: string;
  last_seen: string;
  seen_by: string[];
  is_gateway: boolean;
  is_self: boolean;
  hub: boolean;
  manages: number;
  bucket: string;
  display_name: string;
};

type TopDomain = { domain: string; count: number; category: string };

type DeviceTelemetry = {
  key: string;
  mac: string;
  client_ip: string;
  total_queries: number;
  telemetry_attempts: number;
  blocked: number;
  monitored: number;
  allowed: number;
  last_seen: string;
  top_domains: TopDomain[];
};

type BridgeStatus = {
  zone3_enabled: boolean;
  enforce: boolean;
  mode: "observe" | "enforce";
  inventory_enabled: boolean;
  inventory: {
    inventory_age_seconds: number | null;
    device_count: number;
    new_24h: number;
    sources?: Record<string, { ok: boolean; count: number }>;
  };
  ledger: { ledger_bytes?: number; zone3_enabled?: boolean };
  ha_buckets?: { devices: number; buckets: number; cloud_buckets: number };
};

/* ── formatting ───────────────────────────────────────────────────────── */

function relative(iso: string): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}

function age(secs: number | null | undefined): string {
  if (secs === null || secs === undefined) return "never";
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  return `${Math.round(secs / 3600)}h ago`;
}

/**
 * The dim second line under a device name. Never repeats the name itself: when
 * the roll has no HA area and the display name IS the hostname, the useful
 * extra fact is the MAC, not the hostname a second time.
 */
function subtitle(d: BridgeDevice): string {
  const parts: string[] = [];
  if (d.ha_area) parts.push(d.ha_area);
  if (d.hostname && d.hostname !== d.display_name) parts.push(d.hostname);
  if (parts.length === 0 && d.mac) parts.push(d.mac);
  return parts.join(" · ") || "—";
}

const CATEGORY_TONE: Record<string, string> = {
  TELEMETRY: "var(--pink)",
  ADS: "var(--lemon)",
  TRACKING: "var(--magenta)",
  CLOUD: "var(--sky)",
  UPDATE: "var(--mint)",
  other: "var(--text2)",
};

const SOURCE_TONE: Record<string, string> = {
  arp: "var(--sky)",
  mdns: "var(--lav)",
  ha: "var(--mint)",
  adguard: "var(--pink)",
};

/* ── atoms ────────────────────────────────────────────────────────────── */

function Chip({ label, tone }: { label: string; tone: string }) {
  return (
    <span
      className="rounded px-1.5 py-px text-[10px] font-semibold tracking-wider uppercase"
      style={{ color: tone, background: "var(--chip)" }}
    >
      {label}
    </span>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: string;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 px-4 py-2.5 first:pl-0">
      <span
        className="text-[10px] font-semibold tracking-[0.14em] uppercase"
        style={{ color: "var(--text2)" }}
      >
        {label}
      </span>
      <span
        className="truncate font-mono text-base leading-none tabular-nums"
        style={{ color: tone ?? "var(--text)" }}
      >
        {value}
      </span>
    </div>
  );
}

/* ── page ─────────────────────────────────────────────────────────────── */

export default function DrawbridgePage() {
  const [devices, setDevices] = useState<BridgeDevice[]>([]);
  const [telemetry, setTelemetry] = useState<Record<string, DeviceTelemetry>>({});
  const [status, setStatus] = useState<BridgeStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [openKey, setOpenKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [roll, summary, st] = await Promise.all([
        apiGet<{ devices: BridgeDevice[] }>("/bridge/devices"),
        apiGet<{ devices: DeviceTelemetry[] }>("/bridge/telemetry/summary?hours=24"),
        apiGet<BridgeStatus>("/bridge/status"),
      ]);
      setDevices(roll.devices || []);
      const byKey: Record<string, DeviceTelemetry> = {};
      for (const row of summary.devices || []) byKey[row.key] = row;
      setTelemetry(byKey);
      setStatus(st);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Drawbridge is not answering");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [load]);

  const rescan = useCallback(async () => {
    setRefreshing(true);
    try {
      await apiPost("/bridge/refresh");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Scan failed");
    } finally {
      setRefreshing(false);
    }
  }, [load]);

  const open = useMemo(
    () => devices.find((d) => d.key === openKey) ?? null,
    [devices, openKey],
  );

  const totals = useMemo(() => {
    let attempts = 0;
    for (const row of Object.values(telemetry)) attempts += row.telemetry_attempts;
    return attempts;
  }, [telemetry]);

  const sources = status?.inventory?.sources ?? {};

  return (
    <SectionMain>
      {/* Masthead — editorial, not a hero banner. */}
      <header
        className="mb-4 flex flex-wrap items-end justify-between gap-4 border-b pb-3"
        style={{ borderColor: "var(--line)" }}
      >
        <div className="flex items-end gap-3">
          <Icon path={mdiLan} size="26" />
          <div>
            <h1
              className="text-2xl leading-none font-bold tracking-[0.18em] uppercase"
              style={{ color: "var(--text)" }}
            >
              Drawbridge
            </h1>
            <p className="mt-1.5 text-xs" style={{ color: "var(--text2)" }}>
              Everything on your network, and what it tries to tell the outside
              world.
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* Was a dead, greyed-out "Enforce" span. A control that looks like
              a control and does nothing is worse than no control: it reads as
              "already handled". The switch is real and it lives on the Firewall
              page behind an owner confirmation, so this is a link to it — the
              page still observes and still changes nothing itself. */}
          <Link
            href="/dashboard/firewall"
            className="flex items-center gap-1.5 rounded px-2.5 py-1 text-[10px] font-semibold tracking-[0.12em] uppercase"
            style={{ background: "var(--chip)", color: "var(--text2)" }}
            title={
              status?.mode === "enforce"
                ? "Enforcing — open the Firewall page to change it"
                : "Observing. The enforcement switch is on the Firewall page."
            }
          >
            <Icon path={mdiShieldOutline} size="14" />
            Firewall
          </Link>
          <button
            onClick={rescan}
            disabled={refreshing}
            className="flex items-center gap-1.5 rounded px-2.5 py-1 text-[10px] font-semibold tracking-[0.12em] uppercase transition-opacity disabled:opacity-40"
            style={{ background: "var(--chip)", color: "var(--text)" }}
          >
            <Icon path={mdiRefresh} size="14" />
            {refreshing ? "Scanning" : "Rescan"}
          </button>
        </div>
      </header>

      {/* Status strip — hairline-separated cells, monospace figures. */}
      <div
        className="mb-5 flex flex-wrap divide-x rounded border"
        style={{ borderColor: "var(--line)", background: "var(--card2)" }}
      >
        <Stat
          label="Zone 3"
          value={status?.zone3_enabled ? "On" : "Off"}
          tone={status?.zone3_enabled ? "var(--mint)" : "var(--text2)"}
        />
        <Stat
          label="Mode"
          value={status?.mode === "enforce" ? "Enforce" : "Observe"}
          tone={status?.mode === "enforce" ? "var(--pink)" : "var(--sky)"}
        />
        <Stat label="Devices" value={devices.length} />
        {/* Devices behind the hub. A home can have a handful on the wire and
            many more behind the hub; both are the household's devices. */}
        <Stat label="Managed" value={status?.ha_buckets?.devices ?? 0} />
        <Stat
          label="New 24h"
          value={status?.inventory?.new_24h ?? 0}
          tone={
            (status?.inventory?.new_24h ?? 0) > 0 ? "var(--lemon)" : undefined
          }
        />
        <Stat
          label="Telemetry 24h"
          value={totals}
          tone={totals > 0 ? "var(--pink)" : undefined}
        />
        <Stat
          label="Last scan"
          value={age(status?.inventory?.inventory_age_seconds)}
        />
        <div className="flex items-center gap-1.5 px-4 py-2.5">
          {["arp", "mdns", "ha", "adguard"].map((s) => (
            <span
              key={s}
              title={`${s}: ${sources[s]?.ok ? `${sources[s].count} seen` : "no data"}`}
              className="rounded px-1.5 py-px text-[10px] font-semibold tracking-wider uppercase"
              style={{
                background: "var(--chip)",
                color: sources[s]?.ok ? SOURCE_TONE[s] : "var(--text2)",
                opacity: sources[s]?.ok ? 1 : 0.45,
              }}
            >
              {s}
            </span>
          ))}
        </div>
      </div>

      {error && (
        <div
          className="mb-4 rounded border-l-2 px-3 py-2 text-xs"
          style={{
            borderColor: "var(--pink)",
            background: "var(--card2)",
            color: "var(--text2)",
          }}
        >
          {error}
        </div>
      )}

      {/* Device roll. */}
      <div
        className="overflow-x-auto rounded border"
        style={{ borderColor: "var(--line)", background: "var(--card)" }}
      >
        <table className="puff-bridge-table w-full min-w-[860px] text-left text-xs">
          <thead>
            <tr
              className="border-b"
              style={{ borderColor: "var(--line)", color: "var(--text2)" }}
            >
              {[
                "Device",
                "Make",
                "Address",
                "Seen by",
                "Last",
                "24h",
                "Top domain",
              ].map((h) => (
                <th
                  key={h}
                  className="px-3 py-2 text-[10px] font-semibold tracking-[0.12em] uppercase"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td
                  colSpan={7}
                  className="px-3 py-10 text-center"
                  style={{ color: "var(--text2)" }}
                >
                  Reading the neighbour table…
                </td>
              </tr>
            )}
            {!loading && devices.length === 0 && (
              <tr>
                <td
                  colSpan={7}
                  className="px-3 py-10 text-center"
                  style={{ color: "var(--text2)" }}
                >
                  Nothing on the roll yet. Rescan to look.
                </td>
              </tr>
            )}
            {devices.map((d) => {
              const t = telemetry[d.key];
              const top = t?.top_domains?.[0];
              return (
                <tr
                  key={d.key}
                  onClick={() => setOpenKey(d.key)}
                  className="cursor-pointer border-b transition-colors last:border-0 hover:bg-white/5"
                  style={{ borderColor: "var(--line)" }}
                >
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <span
                        className="font-semibold"
                        style={{ color: "var(--text)" }}
                      >
                        {d.display_name}
                      </span>
                      {d.is_gateway && <Chip label="gateway" tone="var(--lemon)" />}
                      {d.is_self && <Chip label="puffpai" tone="var(--mint)" />}
                      {/* The hub is not one row among eighteen: it is the only
                          route the ~100 addressless devices have to anywhere. */}
                      {d.hub && (
                        <Chip
                          label={`manages ${d.manages}`}
                          tone="var(--lav)"
                        />
                      )}
                    </div>
                    <div
                      className="truncate font-mono text-[10px]"
                      style={{ color: "var(--text2)" }}
                    >
                      {subtitle(d)}
                    </div>
                  </td>
                  <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                    <div>{d.manufacturer || "unknown"}</div>
                    <div className="text-[10px]" style={{ color: "var(--text2)" }}>
                      {d.category}
                    </div>
                  </td>
                  <td
                    className="px-3 py-2 font-mono tabular-nums"
                    style={{ color: "var(--text2)" }}
                  >
                    {d.ip || "—"}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {d.seen_by.map((s) => (
                        <Chip key={s} label={s} tone={SOURCE_TONE[s] ?? "var(--text2)"} />
                      ))}
                    </div>
                  </td>
                  <td
                    className="px-3 py-2 font-mono tabular-nums"
                    style={{ color: "var(--text2)" }}
                  >
                    {relative(d.last_seen)}
                  </td>
                  <td className="px-3 py-2 font-mono tabular-nums">
                    <span
                      style={{
                        color: t?.telemetry_attempts
                          ? "var(--pink)"
                          : "var(--text2)",
                      }}
                    >
                      {t?.telemetry_attempts ?? 0}
                    </span>
                    <span style={{ color: "var(--text2)" }}>
                      {" / "}
                      {t?.total_queries ?? 0}
                    </span>
                  </td>
                  <td className="max-w-[220px] px-3 py-2">
                    {top ? (
                      <div className="flex items-center gap-1.5">
                        <span
                          className="truncate font-mono text-[11px]"
                          style={{ color: "var(--text2)" }}
                        >
                          {top.domain}
                        </span>
                        <Chip
                          label={top.category}
                          tone={CATEGORY_TONE[top.category] ?? "var(--text2)"}
                        />
                      </div>
                    ) : (
                      <span style={{ color: "var(--text2)" }}>—</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="mt-3 text-[11px]" style={{ color: "var(--text2)" }}>
        Observe mode. Nothing here blocks anything — the ledger stays on this
        box.
      </p>

      <ManagedByHomeAssistant pollInterval={POLL_INTERVAL} />

      {open && (
        <DeviceDrawer
          device={open}
          telemetry={telemetry[open.key]}
          onClose={() => setOpenKey(null)}
        />
      )}
    </SectionMain>
  );
}

/* ── drawer ───────────────────────────────────────────────────────────── */

function DeviceDrawer({
  device,
  telemetry,
  onClose,
}: {
  device: BridgeDevice;
  telemetry?: DeviceTelemetry;
  onClose: () => void;
}) {
  const rows = telemetry?.top_domains ?? [];

  return (
    <div className="fixed inset-0 z-[60] flex justify-end">
      <div
        className="absolute inset-0 bg-black/55"
        onClick={onClose}
        aria-hidden
      />
      <aside
        className="gloss-highlight relative z-10 flex h-full w-full max-w-md flex-col overflow-y-auto border-l"
        // Opaque on purpose: glass-card alone lets the device table read
        // through the drawer and the two grids fight each other.
        style={{ borderColor: "var(--line)", background: "var(--bg2)" }}
      >
        <div
          className="flex items-start justify-between gap-3 border-b px-5 py-4"
          style={{ borderColor: "var(--line)" }}
        >
          <div className="min-w-0">
            <h2
              className="truncate text-lg leading-tight font-bold"
              style={{ color: "var(--text)" }}
            >
              {device.display_name}
            </h2>
            <p
              className="mt-0.5 font-mono text-[11px]"
              style={{ color: "var(--text2)" }}
            >
              {device.ip || "no address"} · {device.mac || "no mac"}
            </p>
          </div>
          <button onClick={onClose} aria-label="Close">
            <Icon path={mdiClose} size="18" />
          </button>
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 px-5 py-4 text-xs">
          {[
            ["Maker", device.manufacturer || "unknown"],
            ["Category", device.category],
            ["HA area", device.ha_area || "—"],
            ["HA entity", device.ha_entity_id || "—"],
            ["First seen", relative(device.first_seen) + " ago"],
            ["Last seen", relative(device.last_seen) + " ago"],
            ["mDNS", device.mdns_services.join(", ") || "—"],
            ["AdGuard name", device.adguard_name || "—"],
          ].map(([label, value]) => (
            <div key={label} className="min-w-0">
              <dt
                className="text-[10px] font-semibold tracking-[0.12em] uppercase"
                style={{ color: "var(--text2)" }}
              >
                {label}
              </dt>
              <dd className="truncate" style={{ color: "var(--text2)" }}>
                {value}
              </dd>
            </div>
          ))}
        </dl>

        <div
          className="flex divide-x border-y"
          style={{ borderColor: "var(--line)" }}
        >
          <Stat label="Queries 24h" value={telemetry?.total_queries ?? 0} />
          <Stat
            label="Telemetry"
            value={telemetry?.telemetry_attempts ?? 0}
            tone={telemetry?.telemetry_attempts ? "var(--pink)" : undefined}
          />
          <Stat label="Blocked" value={telemetry?.blocked ?? 0} />
          <Stat label="Monitored" value={telemetry?.monitored ?? 0} />
        </div>

        <div className="px-5 py-4">
          <h3
            className="mb-2 text-[10px] font-semibold tracking-[0.14em] uppercase"
            style={{ color: "var(--text2)" }}
          >
            Top domains · 24h
          </h3>
          {rows.length === 0 ? (
            <p className="text-xs" style={{ color: "var(--text2)" }}>
              No DNS activity recorded. The ledger fills once AdGuard is
              deployed and Zone 3 is on.
            </p>
          ) : (
            <table className="puff-bridge-table w-full text-left text-xs">
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.domain}
                    className="border-b last:border-0"
                    style={{ borderColor: "var(--line)" }}
                  >
                    <td
                      className="max-w-0 truncate py-1.5 pr-2 font-mono text-[11px]"
                      style={{ color: "var(--text2)" }}
                    >
                      {row.domain}
                    </td>
                    <td className="w-24 py-1.5">
                      <Chip
                        label={row.category}
                        tone={CATEGORY_TONE[row.category] ?? "var(--text2)"}
                      />
                    </td>
                    <td
                      className="w-10 py-1.5 text-right font-mono tabular-nums"
                      style={{ color: "var(--text)" }}
                    >
                      {row.count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </aside>
    </div>
  );
}
