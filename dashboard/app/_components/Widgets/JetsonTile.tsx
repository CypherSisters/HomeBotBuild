"use client";

/**
 * Machine tile (design 4b.3): live GPU / VRAM / TEMP / POWER / DISK meters.
 *
 * Data: GET /api/system/stats (same-origin Next route) with contract
 * { available:boolean, gpu:{pct}, mem:{usedGb,totalGb}, tempC, powerW,
 *   uptimeSec, history:[{t,gpu}],
 *   disk:{ root, data } each {totalBytes,freeBytes,usedPct} | null,
 *   present:{gpu,temp,power} }, polled every 5s.
 * GPU and POWER are Jetson-only and are hidden when `present` says the
 * machine has no such reading; memory is labelled VRAM only on a Jetson,
 * where it is shared with the GPU. DISK shows "/"; the install's data dir,
 * when it is its own filesystem, is the quieter right-hand note. The DISK
 * row is hidden when "/" can't be read. When the route says
 * unavailable (Windows dev, sysfs unreadable) the tile shows the design's
 * hatched placeholder, never fake numbers.
 */

import { useEffect, useRef, useState } from "react";
import DesignTile, { HatchBlock, JBM, MicroStat } from "./DesignTile";

interface SystemStats {
  available: boolean;
  gpu?: { pct: number };
  mem?: { usedGb: number; totalGb: number };
  tempC?: number;
  powerW?: number;
  uptimeSec?: number;
  history?: { t: number; gpu: number }[];
  disk?: { root: DiskStat | null; data: DiskStat | null };
  present?: { gpu: boolean; temp: boolean; power: boolean };
}

interface DiskStat {
  totalBytes: number;
  freeBytes: number;
  usedPct: number;
}

// Binary units like df -h and the VRAM meter; one decimal under 10.
function fmtBytes(b: number): string {
  const gb = b / 1024 ** 3;
  if (gb >= 1024) return `${(gb / 1024).toFixed(1)} TB`;
  return gb < 10 ? `${gb.toFixed(1)} GB` : `${Math.round(gb)} GB`;
}

// Warn at 85%, alert at 95% (the tile had no thresholds of its own).
function diskColor(pct: number): string {
  if (pct >= 95) return "var(--stop)";
  if (pct >= 85) return "var(--lemon)";
  return "var(--sky)";
}

function fmtUptime(sec: number): string {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

/** Telemetry poll cadence. */
const STATS_POLL_MS = 5000;

/**
 * Everything the tile actually prints, at the precision it prints it. Two
 * polls with the same key look identical on screen, so the second one skips
 * setState (no re-render, no repaint). The kiosk has no GPU and pays ~0.5s
 * of CPU per repaint, and most 5s polls change nothing visible.
 */
function displayKey(d: SystemStats): string {
  if (!d.available) return "offline";
  const mem = d.mem;
  const memPct = mem && mem.totalGb > 0 ? (100 * mem.usedGb) / mem.totalGb : 0;
  const disk = (x: DiskStat | null | undefined) =>
    x ? `${x.usedPct}/${fmtBytes(x.freeBytes)}/${fmtBytes(x.totalBytes)}` : "-";
  return [
    d.uptimeSec != null ? fmtUptime(d.uptimeSec) : "-",
    Math.round(d.gpu?.pct ?? 0),
    (mem?.usedGb ?? 0).toFixed(1),
    Math.round(mem?.totalGb ?? 0),
    Math.round(memPct),
    Math.round(d.tempC ?? 0),
    Math.round(d.powerW ?? 0),
    disk(d.disk?.root),
    disk(d.disk?.data),
  ].join("|");
}

function Meter({
  label,
  value,
  color,
  pct,
  gradient,
  sub,
  note,
  title,
}: {
  label: string;
  value: string;
  color: string;
  pct: number;
  gradient: string;
  sub?: string;
  note?: string;
  title?: string;
}) {
  const width = Math.max(0, Math.min(100, pct));
  return (
    <div title={title}>
      <div
        className="mb-1.5 flex justify-between"
        style={{
          fontFamily: JBM,
          fontWeight: 700,
          fontSize: 13,
          color: "var(--text2)",
        }}
      >
        <span>{label}</span>
        <span style={{ color }}>{value}</span>
      </div>
      <div
        className="h-2 overflow-hidden rounded-full"
        style={{ background: "var(--chip)" }}
      >
        <div
          className="puff-meter-fill h-full rounded-full transition-all duration-700"
          style={{ width: `${width}%`, background: gradient }}
        />
      </div>
      {sub || note ? (
        <div
          className="mt-1 flex justify-between"
          style={{ fontFamily: JBM, fontSize: 11, color: "var(--text3)" }}
        >
          <span>{sub}</span>
          <span>{note}</span>
        </div>
      ) : null}
    </div>
  );
}

export default function JetsonTile() {
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [failed, setFailed] = useState(false);
  const lastKeyRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetch("/api/system/stats", {
        cache: "no-store",
        signal: AbortSignal.timeout(8000),
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (cancelled) return;
          if (d && typeof d.available === "boolean") {
            const key = displayKey(d);
            if (key !== lastKeyRef.current) {
              lastKeyRef.current = key;
              setStats(d);
            }
            setFailed(false);
          } else setFailed(true);
        })
        .catch(() => {
          if (!cancelled) setFailed(true);
        });
    };
    load();
    const iv = setInterval(load, STATS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, []);

  const live = stats?.available ? stats : null;
  const memPct =
    live?.mem && live.mem.totalGb > 0
      ? (100 * live.mem.usedGb) / live.mem.totalGb
      : 0;
  const root = live?.disk?.root ?? null;
  const data = live?.disk?.data ?? null;
  const rootColor = root ? diskColor(root.usedPct) : "var(--sky)";
  // An older route without `present` reports every meter, as before.
  const has = live?.present ?? { gpu: true, temp: true, power: true };

  return (
    <DesignTile
      icon="memory"
      iconColor="var(--sky)"
      title="This machine"
      img="/design-cloud-1.jpg"
      stat={
        <MicroStat color={live ? "var(--mint)" : "var(--text3)"}>
          {live?.uptimeSec != null ? fmtUptime(live.uptimeSec) : "unknown"}
        </MicroStat>
      }
    >
      {live ? (
        <div className="grid grid-cols-2 gap-x-[18px] gap-y-4">
          {has.gpu ? <Meter
            label="GPU"
            value={`${Math.round(live.gpu?.pct ?? 0)}%`}
            color="var(--pink)"
            pct={live.gpu?.pct ?? 0}
            gradient="linear-gradient(90deg, var(--pink), var(--magenta))"
          /> : null}
          <Meter
            label={has.gpu ? "VRAM" : "RAM"}
            value={`${(live.mem?.usedGb ?? 0).toFixed(1)} / ${Math.round(live.mem?.totalGb ?? 0)} GB`}
            color="var(--lav)"
            pct={memPct}
            gradient="linear-gradient(90deg, var(--lav), var(--sky))"
          />
          {has.temp ? <Meter
            label="TEMP"
            value={`${Math.round(live.tempC ?? 0)}°C`}
            color="var(--mint)"
            pct={((live.tempC ?? 0) / 90) * 100}
            gradient="linear-gradient(90deg, var(--mint), var(--sky))"
          /> : null}
          {has.power ? <Meter
            label="POWER"
            value={`${Math.round(live.powerW ?? 0)} W`}
            color="var(--lemon)"
            pct={((live.powerW ?? 0) / 60) * 100}
            gradient="linear-gradient(90deg, var(--lemon), var(--pink))"
          /> : null}
          {root ? (
            <div className="col-span-2">
              <Meter
                label="DISK"
                value={`${root.usedPct}%`}
                color={rootColor}
                pct={root.usedPct}
                gradient={
                  root.usedPct >= 85
                    ? `linear-gradient(90deg, ${rootColor}, ${rootColor})`
                    : "linear-gradient(90deg, var(--sky), var(--mint))"
                }
                sub={`${fmtBytes(root.freeBytes)} free of ${fmtBytes(root.totalBytes)}`}
                note={data ? `Data ${data.usedPct}%, ${fmtBytes(data.freeBytes)} free` : undefined}
                title={
                  `System disk (/): ${fmtBytes(root.freeBytes)} free of ${fmtBytes(root.totalBytes)}` +
                  (data
                    ? `
Data disk: ${fmtBytes(data.freeBytes)} free of ${fmtBytes(data.totalBytes)}`
                    : "")
                }
              />
            </div>
          ) : null}
        </div>
      ) : (
        <div className="flex h-full flex-col gap-2.5">
          <HatchBlock
            label={failed || stats ? "Telemetry offline" : "Connecting…"}
            style={{ flex: 1 }}
          />
        </div>
      )}
    </DesignTile>
  );
}
