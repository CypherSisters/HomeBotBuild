"use client";

import { useEffect, useState } from "react";
import MsIcon from "../../../_components/MsIcon";

/**
 * Topbar weather chip.
 *
 * Same data source as the dashboard Weather tile: GET
 * /api/pai/dashboard/weather (HA OpenWeatherMap + open-meteo),
 * refreshed every 10 minutes. Icon follows real conditions; clear-night shows
 * the moon. No data → dimmed "--°" (honest empty state, never a fake number).
 */

const API_BASE = "/api/pai";

interface WeatherData {
  temp?: number | null;
  condition?: string | null;
}

function glyphFor(condition: string | null | undefined, hour: number): string {
  const c = (condition ?? "").toLowerCase();
  if (/thunder|storm/.test(c)) return "thunderstorm";
  if (/rain|drizzle|shower/.test(c)) return "rainy";
  if (/snow|sleet|ice|hail/.test(c)) return "weather_snowy";
  if (/fog|mist|haze|smoke/.test(c)) return "foggy";
  if (/cloud|overcast/.test(c)) return "cloud";
  // clear / sunny / unknown-but-present
  return hour >= 7 && hour < 19 ? "wb_sunny" : "bedtime";
}

export default function WeatherChip() {
  const [wx, setWx] = useState<WeatherData | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetch(`${API_BASE}/dashboard/weather`, { signal: AbortSignal.timeout(15000) })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (!cancelled && d) setWx(d);
        })
        .catch(() => {});
    };
    load();
    const iv = setInterval(load, 10 * 60_000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, []);

  const hasTemp = wx?.temp != null;
  const glyph = glyphFor(wx?.condition, new Date().getHours());

  return (
    <div
      className="flex flex-none items-center"
      style={{ gap: 10 }}
      title={wx?.condition ?? "Weather unavailable"}
    >
      <MsIcon
        name={glyph}
        size={24}
        style={{ color: hasTemp ? "var(--sky)" : "var(--text3)" }}
      />
      <div
        style={{
          font: "700 26px var(--font-fredoka), sans-serif",
          color: hasTemp ? "var(--text)" : "var(--text3)",
        }}
      >
        {hasTemp ? `${Math.round(wx!.temp as number)}°` : "--°"}
      </div>
    </div>
  );
}
