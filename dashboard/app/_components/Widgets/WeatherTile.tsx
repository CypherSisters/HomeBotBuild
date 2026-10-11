"use client";

/**
 * Weather tile (design 4b.2) — standalone weather + air quality, split out
 * of the old combined reminders-weather tile. Live data from the backend's
 * /dashboard/weather (HA OpenWeatherMap + open-meteo air detail).
 * Shows the cloud photo more strongly than other tiles (--img3).
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import DesignTile, { FREDOKA, HatchBlock, JBM, MicroLabel, NUNITO } from "./DesignTile";

const API_BASE = "/api/pai";

interface AirRisk {
  level: number; // 0..3
  label: string; // Low / Moderate / High / Very High
  detail: string;
}

interface WeatherData {
  location?: string;
  temp?: number | null;
  feels_like?: number | null;
  humidity?: number | null;
  condition?: string | null;
  high?: number;
  low?: number;
  us_aqi?: number;
  aqi_label?: string;
  air_breakdown?: Record<string, AirRisk>;
  /** Present when the box has no home point yet: the forecast was not
   *  fetched, because no build ships a point to fetch it for. */
  unset?: string;
}

/* Design shows Pollen + Mold first; Dust + Asthma follow when present. */
const AIR_ORDER: { key: string; label: string }[] = [
  { key: "pollen", label: "Pollen" },
  { key: "mold", label: "Mold" },
  { key: "dust", label: "Dust" },
  { key: "asthma", label: "Asthma" },
];

const RISK_COLORS = ["var(--mint)", "var(--lemon)", "var(--lemon)", "var(--puff-red)"];

export default function WeatherTile() {
  const [wx, setWx] = useState<WeatherData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetch(`${API_BASE}/dashboard/weather`, { signal: AbortSignal.timeout(15000) })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (cancelled) return;
          if (d) {
            setWx(d);
            setFailed(false);
          } else setFailed(true);
        })
        .catch(() => {
          if (!cancelled) setFailed(true);
        });
    };
    load();
    const iv = setInterval(load, 10 * 60_000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, []);

  const cond = wx?.condition
    ? wx.condition.charAt(0).toUpperCase() + wx.condition.slice(1)
    : null;

  const chips = wx?.air_breakdown
    ? AIR_ORDER.filter(({ key }) => wx.air_breakdown![key]).slice(0, 4)
    : [];

  return (
    <DesignTile img="/design-cloud-4.jpg" imgOpacity="var(--img3)">
      <div className="flex h-full min-h-0 flex-col">
        {/* The town the owner set, or plain "Home": never an assumed city. */}
        <MicroLabel>{wx?.location || "Home"}</MicroLabel>

        {wx?.unset ? (
          <Link
            href="/dashboard/settings#home-point"
            className="mt-1 no-underline"
            style={{
              fontFamily: NUNITO,
              fontWeight: 600,
              fontSize: 13,
              lineHeight: 1.4,
              color: "var(--text2)",
            }}
          >
            Paste your home point in Settings to see the forecast here.
          </Link>
        ) : null}

        {!wx && failed ? (
          <div className="mt-3 flex-1">
            <HatchBlock label="Weather offline" style={{ height: "100%" }} />
          </div>
        ) : (
          <>
            <div className="mt-1.5 flex items-start justify-between">
              <div
                style={{
                  fontFamily: FREDOKA,
                  fontWeight: 600,
                  fontSize: 56,
                  lineHeight: 1,
                  color: "var(--text)",
                }}
              >
                {wx?.temp != null ? `${Math.round(wx.temp)}°` : "--°"}
              </div>
              <div
                className="text-right"
                style={{
                  fontFamily: NUNITO,
                  fontWeight: 600,
                  fontSize: 13,
                  lineHeight: 1.7,
                  color: "var(--text2)",
                }}
              >
                {wx?.high != null ? (
                  <>
                    H {Math.round(wx.high)}° L {wx.low != null ? Math.round(wx.low) : "--"}°
                    <br />
                  </>
                ) : null}
                {wx?.humidity != null ? (
                  <>
                    Hum {Math.round(wx.humidity)}%
                    <br />
                  </>
                ) : null}
                {wx?.us_aqi != null ? <>AQI {Math.round(wx.us_aqi)}</> : null}
              </div>
            </div>

            <div
              className="mt-1"
              style={{
                fontFamily: NUNITO,
                fontWeight: 600,
                fontSize: 16,
                color: "var(--text2)",
              }}
            >
              {cond
                ? wx?.feels_like != null
                  ? `${cond} · feels like ${Math.round(wx.feels_like)}°`
                  : cond
                : "…"}
            </div>

            <div className="flex-1" />

            <div className="flex flex-col gap-1.5">
              {chips.length > 0
                ? chips.map(({ key, label }) => {
                    const risk = wx!.air_breakdown![key];
                    return (
                      <div
                        key={key}
                        title={risk.detail}
                        className="rounded-xl px-3 py-2"
                        style={{
                          background: "var(--chip)",
                          fontFamily: NUNITO,
                          fontWeight: 700,
                          fontSize: 13,
                          color: "var(--text)",
                        }}
                      >
                        {label}{" "}
                        <span
                          style={{
                            color: RISK_COLORS[Math.min(3, Math.max(0, risk.level))],
                          }}
                        >
                          {risk.label}
                        </span>
                      </div>
                    );
                  })
                : wx?.us_aqi != null && (
                    <div
                      className="rounded-xl px-3 py-2"
                      style={{
                        background: "var(--chip)",
                        fontFamily: NUNITO,
                        fontWeight: 700,
                        fontSize: 13,
                        color: "var(--text)",
                      }}
                    >
                      Air quality{" "}
                      <span style={{ color: "var(--mint)" }}>
                        {wx.aqi_label ?? `AQI ${Math.round(wx.us_aqi)}`}
                      </span>
                    </div>
                  )}
            </div>
          </>
        )}
      </div>
    </DesignTile>
  );
}
