"use client";

/**
 * "Managed by Home Assistant" — the other half of the Drawbridge roll.
 *
 * The roll above lists what answers on the network. This lists what cannot:
 * the Zigbee bulbs, the Z-Wave locks, the cloud integrations — devices with no
 * IP address, which reach the outside world only through the hub, or not at
 * all. About a hundred of them against eighteen network rows, so it opens
 * collapsed to a single line. A hundred rows of bulbs is not a panel, it is a
 * wall, and the useful shape is the bucket: one row per integration, with the
 * tier saying who is in the control path.
 *
 * Strictly observe-only, like the page it sits on. There is no policy control
 * here — those live on the Firewall page, where they can carry the words that
 * explain what throwing them does.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { mdiChevronDown, mdiChevronRight, mdiClose } from "@mdi/js";
import Icon from "../../../_components/Icon";
import { apiGet } from "../../../_lib/api";
import {
  Chip,
  Stat,
  TableFrame,
  Thead,
  TIER_HEADING,
  TIER_LABEL,
  TIER_NOTE,
  TIER_ORDER,
  TIER_TONE,
  type Tier,
} from "./atoms";

export type BucketSummary = {
  id: string;
  tier: Tier;
  integration: string;
  label: string;
  device_count: number;
  entity_count: number;
  areas: Record<string, number>;
  kinds: Record<string, number>;
  linked_keys: string[];
  lookups: number;
  watching: number;
  top_domains: { domain: string; count: number }[];
};

type BucketDevice = {
  name: string;
  area: string;
  manufacturer: string;
  model: string;
  ha_match: string;
  linked_key: string;
  linked_name: string;
  entities: { entity_id: string; domain: string; name: string }[];
};

type BucketDetail = BucketSummary & { devices: BucketDevice[] };

function num(value: number): string {
  return value.toLocaleString();
}

/** "3 rooms · Kitchen, Den, Porch", or the rooms outright when there are few. */
function rooms(areas: Record<string, number>): string {
  const names = Object.keys(areas).sort();
  if (names.length === 0) return "—";
  if (names.length <= 3) return names.join(", ");
  return `${names.length} rooms · ${names.slice(0, 2).join(", ")}…`;
}

/** The two or three entity kinds that actually characterise a bucket. */
function kinds(map: Record<string, number>): string {
  const top = Object.entries(map)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 3);
  if (top.length === 0) return "—";
  return top.map(([kind, n]) => `${n} ${kind.replace("_", " ")}`).join(", ");
}

export default function ManagedByHomeAssistant({
  pollInterval,
}: {
  pollInterval: number;
}) {
  const [buckets, setBuckets] = useState<BucketSummary[]>([]);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const body = await apiGet<{ buckets: BucketSummary[] }>(
        "/bridge/ha/buckets?hours=24",
      );
      setBuckets(body.buckets || []);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "The hub is not answering");
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, pollInterval);
    return () => clearInterval(timer);
  }, [load, pollInterval]);

  const totals = useMemo(() => {
    let devices = 0;
    let cloud = 0;
    let lookups = 0;
    for (const b of buckets) {
      devices += b.device_count;
      lookups += b.lookups;
      if (b.tier === "cloud") cloud += 1;
    }
    return { devices, cloud, lookups };
  }, [buckets]);

  return (
    <section className="mt-7">
      {/* The one-line strip IS the collapsed state: a whole section reduced to
          the sentence a reader would have written after reading it. */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2.5 border-b pb-1.5 text-left"
        style={{ borderColor: "var(--line)" }}
        aria-expanded={expanded}
      >
        <Icon path={expanded ? mdiChevronDown : mdiChevronRight} size="16" />
        <h2
          className="text-[11px] font-bold tracking-[0.18em] uppercase"
          style={{ color: "var(--text)" }}
        >
          Managed by Home Assistant
        </h2>
        <span className="truncate text-[11px]" style={{ color: "var(--text2)" }}>
          {error
            ? error
            : buckets.length === 0
              ? "Nothing yet — the hub hasn't handed over its registries."
              : `${num(totals.devices)} devices in ${buckets.length} buckets · ` +
                `${totals.cloud} cloud-backed · 24h lookups ${num(totals.lookups)}`}
        </span>
      </button>

      {expanded && buckets.length > 0 && (
        <>
          <p className="mt-2 mb-2.5 text-[11px]" style={{ color: "var(--text2)" }}>
            These have no address of their own. Zigbee and Z-Wave devices cannot
            phone anywhere; whatever leaves the house on their behalf leaves
            through the hub, and that is the traffic counted here.
          </p>
          <TableFrame>
            <table className="puff-bridge-table w-full min-w-[860px] text-left text-xs">
              <Thead
                columns={[
                  "Integration",
                  "Devices",
                  "Rooms",
                  "What they are",
                  "24h lookups",
                  "Top domain",
                ]}
              />
              <tbody>
                {TIER_ORDER.flatMap((tier) => {
                  const rows = buckets.filter((b) => b.tier === tier);
                  if (rows.length === 0) return [];
                  return [
                    // A band header rather than four tables: the roll is one
                    // list read top to bottom, and the tier is the reason the
                    // order is what it is.
                    <tr key={`h-${tier}`} style={{ background: "var(--card2)" }}>
                      <td
                        colSpan={6}
                        className="px-3 py-1.5 text-[10px] font-bold tracking-[0.16em] uppercase"
                        style={{
                          color: TIER_TONE[tier],
                          borderTop: "1px solid var(--line)",
                        }}
                        title={TIER_NOTE[tier]}
                      >
                        {TIER_HEADING[tier]}
                        <span
                          className="ml-2 font-semibold tracking-normal normal-case"
                          style={{ color: "var(--text2)" }}
                        >
                          {TIER_NOTE[tier]}
                        </span>
                      </td>
                    </tr>,
                    ...rows.map((b) => {
                  const top = b.top_domains[0];
                  return (
                    <tr
                      key={b.id}
                      onClick={() => setOpenId(b.id)}
                      className="cursor-pointer border-b transition-colors last:border-0 hover:bg-white/5"
                      style={{ borderColor: "var(--line)" }}
                    >
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-2">
                          <Chip
                            label={TIER_LABEL[b.tier] ?? b.tier}
                            tone={TIER_TONE[b.tier] ?? "var(--text2)"}
                            title={TIER_NOTE[b.tier]}
                          />
                          <span
                            className="font-semibold"
                            style={{ color: "var(--text)" }}
                          >
                            {b.label || b.integration}
                          </span>
                        </div>
                      </td>
                      <td
                        className="px-3 py-2 font-mono tabular-nums"
                        style={{ color: "var(--text2)" }}
                      >
                        {b.device_count}
                        <span style={{ color: "var(--text2)" }}>
                          {" / "}
                          {b.entity_count}
                        </span>
                      </td>
                      <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                        {rooms(b.areas)}
                      </td>
                      <td className="px-3 py-2" style={{ color: "var(--text2)" }}>
                        {kinds(b.kinds)}
                      </td>
                      <td className="px-3 py-2 font-mono tabular-nums">
                        <span
                          style={{
                            color: b.watching ? "var(--pink)" : "var(--text2)",
                          }}
                        >
                          {b.watching}
                        </span>
                        <span style={{ color: "var(--text2)" }}>
                          {" / "}
                          {b.lookups}
                        </span>
                      </td>
                      <td className="max-w-[220px] px-3 py-2">
                        {top ? (
                          <span
                            className="truncate font-mono text-[11px]"
                            style={{ color: "var(--text2)" }}
                          >
                            {top.domain}
                          </span>
                        ) : (
                          <span style={{ color: "var(--text2)" }}>—</span>
                        )}
                      </td>
                    </tr>
                  );
                    }),
                  ];
                })}
              </tbody>
            </table>
          </TableFrame>
        </>
      )}

      {openId && <BucketDrawer id={openId} onClose={() => setOpenId(null)} />}
    </section>
  );
}

/* ── drawer ───────────────────────────────────────────────────────────── */

function BucketDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const [detail, setDetail] = useState<BucketDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    apiGet<{ bucket: BucketDetail }>(
      `/bridge/ha/bucket/${encodeURIComponent(id)}?hours=24`,
    )
      .then((body) => live && setDetail(body.bucket))
      .catch(
        (e) =>
          live &&
          setError(e instanceof Error ? e.message : "That bucket is not there"),
      );
    return () => {
      live = false;
    };
  }, [id]);

  /* By room, because "which room" is how anyone actually looks for a bulb. */
  const byRoom = useMemo(() => {
    // A plain record rather than a Map: the tsconfig target here predates
    // iterating one, and downlevelIteration is not worth turning on for this.
    const groups: Record<string, BucketDevice[]> = {};
    for (const device of detail?.devices ?? []) {
      const room = device.area || "Unassigned";
      (groups[room] ??= []).push(device);
    }
    return Object.entries(groups).sort((a, b) => a[0].localeCompare(b[0]));
  }, [detail]);

  return (
    <div className="fixed inset-0 z-[60] flex justify-end">
      <div className="absolute inset-0 bg-black/55" onClick={onClose} aria-hidden />
      <aside
        className="gloss-highlight relative z-10 flex h-full w-full max-w-md flex-col overflow-y-auto border-l"
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
              {detail?.label || detail?.integration || id.split(":")[1]}
            </h2>
            <p
              className="mt-0.5 flex items-center gap-2 font-mono text-[11px]"
              style={{ color: "var(--text2)" }}
            >
              <Chip
                label={TIER_LABEL[detail?.tier ?? "unknown"] ?? "?"}
                tone={TIER_TONE[detail?.tier ?? "unknown"] ?? "var(--text2)"}
              />
              {TIER_NOTE[detail?.tier ?? "unknown"]}
            </p>
          </div>
          <button onClick={onClose} aria-label="Close">
            <Icon path={mdiClose} size="18" />
          </button>
        </div>

        <div className="flex divide-x border-b" style={{ borderColor: "var(--line)" }}>
          <Stat label="Devices" value={detail?.device_count ?? 0} />
          <Stat label="Entities" value={detail?.entity_count ?? 0} />
          <Stat
            label="Lookups 24h"
            value={detail?.lookups ?? 0}
            tone={detail?.watching ? "var(--pink)" : undefined}
            title="Hub DNS lookups attributed to this integration."
          />
        </div>

        {error && (
          <p className="px-5 py-4 text-xs" style={{ color: "var(--text2)" }}>
            {error}
          </p>
        )}

        {byRoom.map(([room, devices]) => (
          <div key={room} className="px-5 py-4">
            <h3
              className="mb-2 text-[10px] font-semibold tracking-[0.14em] uppercase"
              style={{ color: "var(--text2)" }}
            >
              {room}
            </h3>
            {devices.map((device, index) => (
              <div
                key={`${device.name}-${index}`}
                className="mb-2.5 border-b pb-2.5 last:mb-0 last:border-0 last:pb-0"
                style={{ borderColor: "var(--line)" }}
              >
                <div className="flex items-baseline gap-2">
                  <span
                    className="text-xs font-semibold"
                    style={{ color: "var(--text)" }}
                  >
                    {device.name || "unnamed"}
                  </span>
                  {device.linked_key && (
                    <Chip
                      label="on the roll"
                      tone="var(--sky)"
                      title={`Also a network row (${device.linked_name || device.linked_key}), joined by ${device.ha_match}.`}
                    />
                  )}
                </div>
                <div className="text-[10px]" style={{ color: "var(--text2)" }}>
                  {[device.manufacturer, device.model].filter(Boolean).join(" ") ||
                    "no make recorded"}
                </div>
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                  {device.entities.map((entity) => (
                    <span
                      key={entity.entity_id}
                      className="font-mono text-[10px]"
                      style={{ color: "var(--text2)" }}
                      title={entity.name || entity.entity_id}
                    >
                      {entity.entity_id}
                    </span>
                  ))}
                  {device.entities.length === 0 && (
                    <span className="text-[10px]" style={{ color: "var(--text2)" }}>
                      no entities
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        ))}

        {detail && detail.top_domains.length > 0 && (
          <div className="px-5 py-4">
            <h3
              className="mb-2 text-[10px] font-semibold tracking-[0.14em] uppercase"
              style={{ color: "var(--text2)" }}
            >
              Where the hub reached · 24h
            </h3>
            <table className="puff-bridge-table w-full text-left text-xs">
              <tbody>
                {detail.top_domains.map((row) => (
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
          </div>
        )}
      </aside>
    </div>
  );
}
