"use client";

/**
 * /dashboard/device — "Your Jetson".
 *
 * Live telemetry from /api/system/stats (4 s poll), GPU-% last-hour chart
 * from the route's ring buffer, and a "Models resident" card fed by the
 * server-side Ollama inventory (/dashboard/device/models) plus the backend
 * voice capabilities endpoint (Piper/Vosk).
 *
 * On dev (Windows) the telemetry reads fail → hatched placeholders with
 * honest microcopy; the models card still works over LAN.
 */

import { useEffect, useMemo, useState, type CSSProperties } from "react";
import MsIcon from "../../_components/MsIcon";
import { getSystemStatus } from "../../_lib/services/systemService";
import {
  getVoiceCapabilities,
  type VoiceCapabilities,
} from "../../_lib/services/voiceService";

/* ── types ─────────────────────────────────────────────────────────── */

interface SysStats {
  available: boolean;
  gpu: { pct: number };
  mem: { usedGb: number; totalGb: number };
  tempC: number;
  powerW: number;
  uptimeSec: number;
  history: { t: number; gpu: number }[];
}

interface OllamaModelRow {
  name: string;
  sizeGb: number;
  quant: string | null;
  params: string | null;
  loaded: boolean;
}

interface OllamaResp {
  available: boolean;
  models: OllamaModelRow[];
}

/* ── shared style bits ─────────────────────────────────────────────── */

const FREDOKA = "var(--font-fredoka), sans-serif";
const NUNITO = "var(--font-nunito), sans-serif";
const JBMONO = "var(--font-jbmono), monospace";
const RED = "#FF5C7A";

const microLabel: CSSProperties = {
  fontFamily: JBMONO,
  fontWeight: 700,
  fontSize: 11,
  letterSpacing: ".14em",
  color: "var(--text3)",
  textTransform: "uppercase",
};

const HATCH =
  "repeating-linear-gradient(135deg, var(--chip) 0 8px, transparent 8px 16px)";

const cardBase: CSSProperties = {
  position: "relative",
  isolation: "isolate",
  overflow: "hidden",
  borderRadius: 26,
  border: "1px solid var(--line)",
  background: "var(--card)",
  backdropFilter: "blur(18px)",
  WebkitBackdropFilter: "blur(18px)",
  padding: 24,
};

const panelBase: CSSProperties = {
  borderRadius: 28,
  border: "1px solid var(--line)",
  background: "var(--card)",
  backdropFilter: "blur(18px)",
  WebkitBackdropFilter: "blur(18px)",
  padding: "24px 28px",
  boxShadow: "var(--shadow)",
};

function CloudBg({ src }: { src: string }) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={src}
      alt=""
      aria-hidden="true"
      style={{
        position: "absolute",
        inset: 0,
        zIndex: -1,
        width: "100%",
        height: "100%",
        objectFit: "cover",
        opacity: "var(--imgTile)" as unknown as number,
        pointerEvents: "none",
      }}
    />
  );
}

/* ── stat card ─────────────────────────────────────────────────────── */

function StatCard({
  label,
  value,
  unit,
  colorVar,
  pct,
  available,
  img,
}: {
  label: string;
  value: string;
  unit: string;
  colorVar: string;
  pct: number;
  available: boolean;
  img: string;
}) {
  return (
    <div style={cardBase}>
      <CloudBg src={img} />
      <div style={microLabel}>{label}</div>
      {available ? (
        <div
          style={{
            fontFamily: FREDOKA,
            fontWeight: 600,
            fontSize: "clamp(32px,3vw,54px)",
            lineHeight: 1.1,
            color: `var(${colorVar})`,
            margin: "6px 0 10px",
          }}
        >
          {value}
          <span style={{ fontSize: 26 }}>{unit}</span>
        </div>
      ) : (
        <div
          style={{
            height: "clamp(38px,3vw,56px)",
            borderRadius: 14,
            background: HATCH,
            margin: "10px 0 12px",
          }}
        />
      )}
      <div
        style={{ height: 8, borderRadius: 99, background: "var(--chip)" }}
      >
        <div
          style={{
            width: `${available ? Math.min(100, Math.max(0, pct)) : 0}%`,
            height: "100%",
            borderRadius: 99,
            background: `var(${colorVar})`,
            transition: "width .6s ease",
          }}
        />
      </div>
      {!available && (
        <div style={{ ...microLabel, fontSize: 10, marginTop: 12 }}>
          Live GPU telemetry shows on NVIDIA Jetson boards
        </div>
      )}
    </div>
  );
}

/* ── models resident row ───────────────────────────────────────────── */

function ModelRow({
  icon,
  iconColor,
  name,
  sub,
  status,
  statusColor,
  dim,
}: {
  icon: string;
  iconColor: string;
  name: string;
  sub: string;
  status: string;
  statusColor: string;
  dim?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        padding: 16,
        borderRadius: 20,
        background: "var(--card2)",
        border: "1px solid var(--line)",
        opacity: dim ? 0.55 : 1,
      }}
    >
      <MsIcon name={icon} size={24} style={{ color: iconColor }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontFamily: NUNITO,
            fontWeight: 700,
            fontSize: 17,
            color: "var(--text)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {name}
        </div>
        <div
          style={{
            fontFamily: JBMONO,
            fontWeight: 600,
            fontSize: 12,
            color: "var(--text3)",
          }}
        >
          {sub}
        </div>
      </div>
      <span
        style={{
          fontFamily: JBMONO,
          fontWeight: 700,
          fontSize: 12,
          color: statusColor,
          flex: "none",
        }}
      >
        {status}
      </span>
    </div>
  );
}

/* ── chart ─────────────────────────────────────────────────────────── */

const BAR_COLORS = [
  "--pink",
  "--pink",
  "--pink",
  "--magenta",
  "--magenta",
  "--magenta",
  "--lav",
  "--lav",
  "--sky",
  "--sky",
  "--sky",
  "--sky",
];

function useChartBuckets(history: { t: number; gpu: number }[] | undefined) {
  return useMemo(() => {
    const now = Date.now();
    const HOUR = 3_600_000;
    const BUCKET = HOUR / 12; // 5 min
    const buckets: (number | null)[] = Array.from({ length: 12 }, () => null);
    if (history?.length) {
      const sums = new Array(12).fill(0);
      const counts = new Array(12).fill(0);
      for (const s of history) {
        const age = now - s.t;
        if (age < 0 || age >= HOUR) continue;
        const i = 11 - Math.floor(age / BUCKET);
        if (i < 0 || i > 11) continue;
        sums[i] += s.gpu;
        counts[i] += 1;
      }
      for (let i = 0; i < 12; i++) {
        if (counts[i] > 0) buckets[i] = sums[i] / counts[i];
      }
    }
    return buckets;
  }, [history]);
}

/* ── page ──────────────────────────────────────────────────────────── */

export default function DevicePage() {
  const [stats, setStats] = useState<SysStats | null>(null);
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  const [ollama, setOllama] = useState<OllamaResp | null>(null);
  const [voice, setVoice] = useState<VoiceCapabilities | null>(null);

  // Telemetry: poll every 4 s.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const res = await fetch("/api/system/stats", { cache: "no-store" });
        if (!res.ok) throw new Error(String(res.status));
        const j = (await res.json()) as SysStats;
        if (alive) setStats(j);
      } catch {
        if (alive)
          setStats({
            available: false,
            gpu: { pct: 0 },
            mem: { usedGb: 0, totalGb: 0 },
            tempC: 0,
            powerW: 0,
            uptimeSec: 0,
            history: [],
          });
      }
    };
    tick();
    const iv = setInterval(tick, 4000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, []);

  // Backend health + Ollama inventory: poll every 30 s. Voice caps once.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        await getSystemStatus();
        if (alive) setBackendOk(true);
      } catch {
        if (alive) setBackendOk(false);
      }
      try {
        const res = await fetch("/dashboard/device/models", {
          cache: "no-store",
        });
        const j = (await res.json()) as OllamaResp;
        if (alive) setOllama(j);
      } catch {
        if (alive) setOllama({ available: false, models: [] });
      }
    };
    tick();
    getVoiceCapabilities().then((c) => {
      if (alive) setVoice(c);
    });
    const iv = setInterval(tick, 30_000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, []);

  const telemetry = stats?.available === true;
  const buckets = useChartBuckets(stats?.history);
  const hasHistory = buckets.some((b) => b !== null);

  // Status pill: nominal / degraded / offline from stats + backend health.
  const pill = useMemo(() => {
    if (backendOk === null && stats === null)
      return { text: "CHECKING…", color: "var(--text3)", bg: "var(--chip)" };
    if (backendOk && telemetry)
      return {
        text: "ALL SYSTEMS NOMINAL",
        color: "var(--mint)",
        bg: "color-mix(in srgb, var(--mint) 16%, transparent)",
      };
    if (backendOk && !telemetry)
      return {
        text: "TELEMETRY OFFLINE",
        color: "var(--lemon)",
        bg: "color-mix(in srgb, var(--lemon) 16%, transparent)",
      };
    if (!backendOk && telemetry)
      return {
        text: "BACKEND OFFLINE",
        color: RED,
        bg: "color-mix(in srgb, #FF5C7A 16%, transparent)",
      };
    return {
      text: "SYSTEMS OFFLINE",
      color: RED,
      bg: "color-mix(in srgb, #FF5C7A 16%, transparent)",
    };
  }, [backendOk, telemetry, stats]);

  const totalGbLabel = telemetry ? `${Math.round(stats!.mem.totalGb)}GB ` : "";
  const memPct =
    telemetry && stats!.mem.totalGb > 0
      ? (stats!.mem.usedGb / stats!.mem.totalGb) * 100
      : 0;

  // Axis labels: -60m / -40m / -20m / NOW.
  const axis = useMemo(() => {
    const fmt = (msAgo: number) => {
      const d = new Date(Date.now() - msAgo);
      return `${String(d.getHours()).padStart(2, "0")}:${String(
        d.getMinutes()
      ).padStart(2, "0")}`;
    };
    return [fmt(3_600_000), fmt(2_400_000), fmt(1_200_000), "NOW"];
  }, [stats]); // eslint-disable-line react-hooks/exhaustive-deps

  // Models resident rows: loaded LLMs first, then a few idle, then voice.
  const llmRows = useMemo(() => {
    if (!ollama?.available) return null;
    const loaded = ollama.models.filter((m) => m.loaded);
    const idleBudget = Math.max(1, 4 - loaded.length);
    const idle = ollama.models.filter((m) => !m.loaded).slice(0, idleBudget);
    return [...loaded, ...idle];
  }, [ollama]);

  const pretty = (s: string) =>
    s && s !== "none" ? s.charAt(0).toUpperCase() + s.slice(1) : null;

  return (
    <section
      style={{
        padding:
          "clamp(16px,2.4vh,26px) clamp(18px,2vw,36px) clamp(18px,2.8vh,30px)",
        display: "flex",
        flexDirection: "column",
        gap: "clamp(14px,2vh,22px)",
      }}
    >
      {/* ── header ─────────────────────────────────────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "flex-end",
          gap: 20,
          flexWrap: "wrap",
        }}
      >
        <div>
          <h1
            style={{
              margin: 0,
              fontFamily: FREDOKA,
              fontWeight: 600,
              fontSize: "clamp(28px,2.6vw,44px)",
              letterSpacing: "-.8px",
              color: "var(--text)",
            }}
          >
            Your Jetson
          </h1>
          <p
            style={{
              margin: "6px 0 0",
              fontFamily: NUNITO,
              fontWeight: 500,
              fontSize: 19,
              color: "var(--text2)",
            }}
          >
            Orin AGX {totalGbLabel}· JetPack 6.1 · everything below runs
            locally
          </p>
        </div>
        <div style={{ flex: 1 }} />
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 9,
            padding: "12px 22px",
            borderRadius: 99,
            background: pill.bg,
            color: pill.color,
            fontFamily: JBMONO,
            fontWeight: 700,
            fontSize: 14,
          }}
        >
          <span
            style={{
              width: 9,
              height: 9,
              borderRadius: "50%",
              background: pill.color,
              display: "inline-block",
            }}
          />
          {pill.text}
        </span>
      </div>

      {/* ── 4 live stat cards ──────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-5 xl:grid-cols-4">
        <StatCard
          label="GPU load"
          value={telemetry ? String(Math.round(stats!.gpu.pct)) : ""}
          unit="%"
          colorVar="--pink"
          pct={telemetry ? stats!.gpu.pct : 0}
          available={telemetry}
          img="/design-cloud-1.jpg"
        />
        <StatCard
          label="Memory"
          value={
            telemetry
              ? stats!.mem.usedGb >= 10
                ? String(Math.round(stats!.mem.usedGb))
                : stats!.mem.usedGb.toFixed(1)
              : ""
          }
          unit="GB"
          colorVar="--lav"
          pct={memPct}
          available={telemetry}
          img="/design-cloud-5.jpg"
        />
        <StatCard
          label="Temperature"
          value={telemetry ? String(Math.round(stats!.tempC)) : ""}
          unit="°C"
          colorVar="--mint"
          pct={telemetry ? stats!.tempC : 0}
          available={telemetry}
          img="/design-cloud-4.jpg"
        />
        <StatCard
          label="Power draw"
          value={telemetry ? String(Math.round(stats!.powerW)) : ""}
          unit="W"
          colorVar="--lemon"
          pct={telemetry ? (stats!.powerW / 60) * 100 : 0}
          available={telemetry}
          img="/design-cloud-4.jpg"
        />
      </div>

      {/* ── bottom grid: chart + models resident ───────────────────── */}
      <div className="grid gap-4 lg:[grid-template-columns:minmax(0,1fr)_clamp(300px,30vw,520px)]">
        {/* GPU % last hour */}
        <div
          style={{
            ...panelBase,
            display: "flex",
            flexDirection: "column",
            minHeight: 320,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              marginBottom: 18,
            }}
          >
            <div
              style={{
                fontFamily: FREDOKA,
                fontWeight: 600,
                fontSize: 22,
                color: "var(--text)",
              }}
            >
              Inference over the last hour
            </div>
            <div style={{ flex: 1 }} />
            <span
              style={{
                fontFamily: JBMONO,
                fontWeight: 700,
                fontSize: 12,
                color: "var(--text3)",
              }}
            >
              GPU % · LAST HOUR
            </span>
          </div>
          {hasHistory ? (
            <>
              <div
                style={{
                  flex: 1,
                  minHeight: 180,
                  display: "flex",
                  alignItems: "flex-end",
                  gap: 6,
                }}
              >
                {buckets.map((b, i) => (
                  <div
                    key={i}
                    title={b !== null ? `${Math.round(b)}%` : "no data"}
                    style={
                      b !== null
                        ? {
                            flex: 1,
                            height: `${Math.max(4, Math.min(100, b))}%`,
                            borderRadius: "8px 8px 0 0",
                            background: `linear-gradient(180deg, var(${BAR_COLORS[i]}), transparent)`,
                            transition: "height .6s ease",
                          }
                        : {
                            flex: 1,
                            height: 4,
                            borderRadius: "8px 8px 0 0",
                            background: "var(--chip)",
                          }
                    }
                  />
                ))}
              </div>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontFamily: JBMONO,
                  fontWeight: 600,
                  fontSize: 12,
                  color: "var(--text3)",
                  marginTop: 12,
                }}
              >
                {axis.map((a) => (
                  <span key={a}>{a}</span>
                ))}
              </div>
            </>
          ) : (
            <div
              style={{
                flex: 1,
                minHeight: 180,
                borderRadius: 18,
                background: HATCH,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <span style={{ ...microLabel, fontSize: 12 }}>
                {telemetry
                  ? "Collecting samples…"
                  : "Live GPU telemetry shows on NVIDIA Jetson boards"}
              </span>
            </div>
          )}
        </div>

        {/* Models resident */}
        <div
          style={{
            ...panelBase,
            display: "flex",
            flexDirection: "column",
            gap: 14,
            minHeight: 320,
          }}
        >
          <div
            style={{
              fontFamily: FREDOKA,
              fontWeight: 600,
              fontSize: 22,
              color: "var(--text)",
              marginBottom: 2,
            }}
          >
            Models resident
          </div>

          {llmRows ? (
            llmRows.map((m, i) => (
              <ModelRow
                key={m.name}
                icon={m.loaded ? "bolt" : "hub"}
                iconColor={m.loaded ? "var(--pink)" : "var(--text3)"}
                name={m.name}
                sub={[
                  m.quant,
                  m.sizeGb > 0 ? `${m.sizeGb} GB` : null,
                  m.loaded && i === 0 ? "primary" : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
                status={m.loaded ? "LOADED" : "IDLE"}
                statusColor={m.loaded ? "var(--mint)" : "var(--text3)"}
                dim={!m.loaded}
              />
            ))
          ) : (
            <>
              <div
                style={{ height: 74, borderRadius: 20, background: HATCH }}
              />
              <div
                style={{ height: 74, borderRadius: 20, background: HATCH }}
              />
              <div style={{ ...microLabel, fontSize: 10 }}>
                {ollama === null
                  ? "Checking model server…"
                  : "Model server unreachable"}
              </div>
            </>
          )}

          {voice && (
            <>
              <ModelRow
                icon="record_voice_over"
                iconColor="var(--sky)"
                name={pretty(voice.stt_engine) ?? "STT engine"}
                sub={[
                  voice.stt_sample_rate
                    ? `${Math.round(voice.stt_sample_rate / 1000)} kHz`
                    : null,
                  "speech in",
                ]
                  .filter(Boolean)
                  .join(" · ")}
                status={voice.stt_available ? "LOADED" : "OFF"}
                statusColor={
                  voice.stt_available ? "var(--mint)" : "var(--text3)"
                }
                dim={!voice.stt_available}
              />
              <ModelRow
                icon="graphic_eq"
                iconColor="var(--lav)"
                name={pretty(voice.tts_engine) ?? "TTS engine"}
                sub={[
                  voice.tts_sample_rate
                    ? `${Math.round(voice.tts_sample_rate / 1000)} kHz`
                    : null,
                  "speech out",
                ]
                  .filter(Boolean)
                  .join(" · ")}
                status={voice.tts_available ? "LOADED" : "OFF"}
                statusColor={
                  voice.tts_available ? "var(--mint)" : "var(--text3)"
                }
                dim={!voice.tts_available}
              />
            </>
          )}
        </div>
      </div>
    </section>
  );
}
