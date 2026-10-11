"use client";

/**
 * Settings. Two-column layout:
 *   left  — H1 + "The three zones" explainer card (real knowledge-pack count
 *           from the same /system/status payload the Library tile uses, and a
 *           real firewall status line for the Zone-3 bridge row);
 *   right — Appearance (theme buttons + accessibility toggles), Wake word,
 *           and shortcut pills to the absorbed pages.
 *
 * Persistence contract (read by the rest of the app):
 *   puff-theme-override        'day'|'plum'|'light'|'auto'     → html[data-puff-theme]
 *                              (also the cache of the active profile's pick)
 *   puffpai_big_touch          '1' | '0' (default off)         → html.puff-big-touch
 *   puffpai_ambient_motion     '1' | '0' (default on)          → html.puff-no-motion (inverse)
 *   puffpai_display_mode       'kiosk' | absent (default)      → html.puff-kiosk
 *   puffpai_show_transcript    '1' | '0' (default on)          → dashboard transcript tile
 *   puffpai_show_thinking      '1' | '0' (default on)          → live reasoning block
 *                              (off also stops show_thinking being asked for)
 * Changes also fire window CustomEvent('puffpai-setting-changed', {detail:{key,value}}).
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import MsIcon from "../../_components/MsIcon";
import ConnectSpotifyCard from "./ConnectSpotifyCard";
import EgressDoorsCard from "./EgressDoorsCard";
import HatchCard from "./HatchCard";
import SkillsCapabilityCard from "./SkillsCapabilityCard";
import HomePointCard from "./HomePointCard";
import PairedDevicesCard from "./PairedDevicesCard";
import { useAppDispatch, useAppSelector } from "@/app/_stores/hooks";
import { fetchSystemStatus } from "@/app/_stores/systemSlice";
import { getFirewallStatus } from "@/app/_lib/services/firewallService";
import ThemePicker from "../_components/ThemePicker";
import { useHousehold } from "@/app/_lib/hooks/useHousehold";
import { setMemberPreferences } from "@/app/_lib/services/householdService";
import {
  chooseTheme,
  normalizeChoice,
  readStoredChoice,
  type ThemeChoice,
} from "@/app/_lib/theme";

/* ── localStorage helpers (SSR-guarded) ─────────────────────── */

function readFlag(key: string, fallback: boolean): boolean {
  try {
    const v = window.localStorage.getItem(key);
    if (v === "1") return true;
    if (v === "0") return false;
  } catch {
    /* storage unavailable */
  }
  return fallback;
}

function writeFlag(key: string, value: boolean) {
  try {
    window.localStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* storage unavailable */
  }
  try {
    window.dispatchEvent(
      new CustomEvent("puffpai-setting-changed", { detail: { key, value } })
    );
  } catch {
    /* older browsers */
  }
}


/** Root-class side effects for the accessibility toggles. */
function applyRootClasses(bigTouch: boolean, motion: boolean) {
  const root = document.documentElement;
  root.classList.toggle("puff-big-touch", bigTouch);
  root.classList.toggle("puff-no-motion", !motion);
}

/* ── Small building blocks ──────────────────────────────────── */

const microLabel: React.CSSProperties = {
  fontFamily: "var(--font-jbmono), monospace",
  fontWeight: 700,
  fontSize: 11,
  letterSpacing: ".14em",
  textTransform: "uppercase",
  color: "var(--text3)",
};

function ToggleRow({
  title,
  sub,
  on,
  onChange,
}: {
  title: string;
  sub: string;
  on: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div
      className="flex items-center justify-between gap-4"
      style={{ padding: "18px 0", borderTop: "1px solid var(--line)" }}
    >
      <div>
        <div
          style={{
            fontFamily: "var(--font-nunito), sans-serif",
            fontWeight: 700,
            fontSize: 17,
            color: "var(--text)",
          }}
        >
          {title}
        </div>
        <div
          style={{
            fontFamily: "var(--font-nunito), sans-serif",
            fontWeight: 500,
            fontSize: 14,
            color: "var(--text3)",
          }}
        >
          {sub}
        </div>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-label={title}
        onClick={() => onChange(!on)}
        className="shrink-0 cursor-pointer"
        style={{
          width: 68,
          height: 38,
          borderRadius: 99,
          border: "none",
          background: on ? "var(--pink)" : "var(--chip)",
          position: "relative",
          transition: "background .2s ease",
          padding: 0,
        }}
      >
        <span
          style={{
            position: "absolute",
            top: 4,
            left: on ? 34 : 4,
            width: 30,
            height: 30,
            borderRadius: "50%",
            background: on ? "#fff" : "var(--text3)",
            transition: "left .2s ease, background .2s ease",
          }}
        />
      </button>
    </div>
  );
}

function ZoneRow({
  icon,
  iconColor,
  tint,
  border,
  title,
  children,
}: {
  icon: string;
  iconColor: string;
  tint: string;
  border: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div
      style={{
        padding: 20,
        borderRadius: 22,
        background: `linear-gradient(140deg, ${tint}, transparent)`,
        border: `1px solid ${border}`,
      }}
    >
      <div className="mb-1.5 flex items-center gap-2.5">
        <MsIcon name={icon} size={22} style={{ color: iconColor }} />
        <div
          style={{
            fontFamily: "var(--font-nunito), sans-serif",
            fontWeight: 700,
            fontSize: 19,
            color: "var(--text)",
          }}
        >
          {title}
        </div>
      </div>
      <div
        style={{
          fontFamily: "var(--font-nunito), sans-serif",
          fontWeight: 500,
          fontSize: 16,
          lineHeight: 1.45,
          color: "var(--text2)",
        }}
      >
        {children}
      </div>
    </div>
  );
}

/* ── Page ───────────────────────────────────────────────────── */

const cardStyle: React.CSSProperties = {
  borderRadius: 30,
  border: "1px solid var(--line)",
  background: "var(--card)",
  backdropFilter: "blur(18px)",
  WebkitBackdropFilter: "blur(18px)",
  padding: "26px 28px",
  boxShadow: "var(--shadow)",
};

/** Real /firewall/status payload (the declared service type is stale —
 *  the live endpoint returns backend/lockdown, not status/rules). */
type FirewallProbe = { backend?: string; lockdown_active?: boolean } | null;

export default function SettingsPage() {
  const dispatch = useAppDispatch();
  const systemStatus = useAppSelector((state) => state.system.status);

  // Whose screen this is. The picker writes onto them, not onto the browser.
  const { active, refresh: refreshHousehold } = useHousehold();

  /* The palette this screen wears. Written to the ACTIVE profile when
     there is one (so the kiosk and the owner’s phone agree) and cached
     in localStorage either way. null = nothing stored = auto. */
  const [override, setOverride] = useState<ThemeChoice | null>(null);
  const [bigTouch, setBigTouch] = useState(false);
  const [showTranscript, setShowTranscript] = useState(true);
  const [showThinking, setShowThinking] = useState(true);
  const [ambientMotion, setAmbientMotion] = useState(true);
  const [kioskMode, setKioskMode] = useState(false);
  const [fwProbe, setFwProbe] = useState<FirewallProbe>(null);

  /* Mount: hydrate from localStorage, apply root classes, fetch real data. */
  useEffect(() => {
    try {
      setOverride(readStoredChoice());
    } catch {
      /* storage unavailable */
    }
    const bt = readFlag("puffpai_big_touch", false);
    const tr = readFlag("puffpai_show_transcript", true);
    const am = readFlag("puffpai_ambient_motion", true);
    setBigTouch(bt);
    setShowTranscript(tr);
    setShowThinking(readFlag("puffpai_show_thinking", true));
    setAmbientMotion(am);
    applyRootClasses(bt, am);
    try {
      setKioskMode(
        window.localStorage.getItem("puffpai_display_mode") === "kiosk"
      );
    } catch {
      /* storage unavailable */
    }

    dispatch(fetchSystemStatus());
    getFirewallStatus()
      .then((s) => setFwProbe(s as FirewallProbe))
      .catch(() => setFwProbe(null));
  }, [dispatch]);

  const profileTheme = normalizeChoice(active?.preferences?.theme);
  useEffect(() => {
    if (profileTheme) setOverride(profileTheme);
  }, [profileTheme]);

  const pickTheme = useCallback(
    (choice: ThemeChoice) => {
      setOverride(choice);
      chooseTheme(choice);
      // Persist onto the profile this screen belongs to, so the choice
      // follows the person rather than the browser. A merge patch: the
      // owner’s preferences also carry workout_level.
      if (active) {
        setMemberPreferences(active.id, { theme: choice })
          .then(() => refreshHousehold())
          .catch(() => {
            /* offline or backend blip - the local pick still stands */
          });
      }
    },
    [active, refreshHousehold],
  );

  const toggleBigTouch = useCallback((next: boolean) => {
    setBigTouch(next);
    writeFlag("puffpai_big_touch", next);
    document.documentElement.classList.toggle("puff-big-touch", next);
  }, []);

  const toggleTranscript = useCallback((next: boolean) => {
    setShowTranscript(next);
    writeFlag("puffpai_show_transcript", next);
  }, []);

  const toggleThinking = useCallback((next: boolean) => {
    setShowThinking(next);
    writeFlag("puffpai_show_thinking", next);
  }, []);

  const toggleMotion = useCallback((next: boolean) => {
    setAmbientMotion(next);
    writeFlag("puffpai_ambient_motion", next);
    document.documentElement.classList.toggle("puff-no-motion", !next);
  }, []);

  /* Same key ?puffMode=kiosk writes in the app/layout.tsx boot script —
     'kiosk' or absent, not '1'/'0', so writeFlag doesn't fit. The motion
     governor in css/_theme.css keeps the scene static under html.puff-kiosk
     (Puff still animates while she speaks). */
  const toggleKiosk = useCallback((next: boolean) => {
    setKioskMode(next);
    try {
      if (next) {
        window.localStorage.setItem("puffpai_display_mode", "kiosk");
      } else {
        window.localStorage.removeItem("puffpai_display_mode");
      }
    } catch {
      /* storage unavailable */
    }
    document.documentElement.classList.toggle("puff-kiosk", next);
    try {
      window.dispatchEvent(
        new CustomEvent("puffpai-setting-changed", {
          detail: { key: "puffpai_display_mode", value: next ? "kiosk" : "" },
        })
      );
    } catch {
      /* older browsers */
    }
  }, []);

  const packCount = systemStatus?.knowledge?.library_count;
  /* Bridge line: /system/status firewall summary (same payload the Library
     tile reads), enriched with the lockdown flag from /firewall/status. */
  const fw = systemStatus?.firewall;
  const fwActive = fw?.status === "active" || fw?.status === "enabled";
  const fwLockdown = fwProbe?.lockdown_active === true;
  const rawWake = systemStatus?.voice?.wake_word?.trim();
  const wakeWord =
    rawWake && rawWake.length > 0
      ? rawWake.replace(/\b\w/g, (c) => c.toUpperCase())
      : "Hey Puff";

  return (
    <section
      style={{
        padding:
          "clamp(16px,2.4vh,26px) clamp(18px,2vw,36px) clamp(18px,2.8vh,30px)",
        animation: "slide-up 0.3s ease-out",
      }}
    >
      <div
        className="grid grid-cols-1 items-start lg:grid-cols-2"
        style={{ gap: "clamp(14px,1.6vw,24px)" }}
      >
        {/* ── Left column ─────────────────────────────────── */}
        <div className="flex min-w-0 flex-col gap-[22px]">
          <div>
            <h1
              style={{
                margin: 0,
                fontFamily: "var(--font-fredoka), sans-serif",
                fontWeight: 600,
                fontSize: "clamp(28px,2.6vw,44px)",
                letterSpacing: "-.8px",
                color: "var(--text)",
              }}
            >
              Settings
            </h1>
            <p
              style={{
                margin: "6px 0 0",
                fontFamily: "var(--font-nunito), sans-serif",
                fontWeight: 500,
                fontSize: 19,
                color: "var(--text2)",
              }}
            >
              PuffPAI decides the zone for every request. You never have to.
            </p>
          </div>

          <div style={cardStyle} className="flex flex-col gap-4">
            <div
              style={{
                fontFamily: "var(--font-fredoka), sans-serif",
                fontWeight: 600,
                fontSize: 24,
                color: "var(--text)",
              }}
            >
              The three zones
            </div>

            <ZoneRow
              icon="shield_lock"
              iconColor="var(--magenta)"
              tint="rgba(198,75,216,.18)"
              border="var(--magenta)"
              title="Zone 1 · The Vault"
            >
              Personal details, secrets and the documents you file there.
              Encrypted, locked with your PIN, and never leaves this machine.
            </ZoneRow>

            <ZoneRow
              icon="psychology"
              iconColor="var(--sky)"
              tint="rgba(98,216,240,.14)"
              border="var(--line)"
              title="Zone 2 · The Brain"
            >
              Local models, agents, tools and Home Assistant, over{" "}
              {typeof packCount === "number" ? (
                <>
                  <span style={{ fontWeight: 700, color: "var(--text)" }}>
                    {packCount}
                  </span>{" "}
                  offline knowledge packs
                </>
              ) : (
                <>offline knowledge packs</>
              )}{" "}
              and the files you load. Open to every query, no personal data
              attached.
            </ZoneRow>

            <ZoneRow
              icon="public"
              iconColor="var(--lemon)"
              tint="rgba(252,217,122,.14)"
              border="var(--line)"
              title="Zone 3 · The Bridge"
            >
              The only zone that reaches outside. Every way out has a name and
              a switch, listed below, and stays shut until you open it.
            </ZoneRow>
          </div>

          {/* Egress doors — what can leave the house. Sits under the zones
              card because it IS the Bridge, itemised: every refusal message
              the appliance emits ends "turn it on in Settings > Egress". */}
          <EgressDoorsCard />

          {/* Firewall status — secondary to the door list above. It reports
              a real /system/status + /firewall/status summary and links to the
              firewall page, but it is NOT the answer to "what leaves my
              house": the doors are, which is why they come first. */}
          <Link
            href="/dashboard/firewall"
            className="group flex items-center gap-3 no-underline"
            style={{
              padding: "16px 20px",
              borderRadius: 20,
              background: fw
                ? "var(--card2)"
                : "repeating-linear-gradient(135deg, var(--chip) 0 8px, transparent 8px 16px)",
              border: "1px solid var(--line)",
            }}
          >
            <MsIcon
              name={fw ? (fwActive ? "check_circle" : "shield") : "shield_lock"}
              size={22}
              style={{
                color: fw
                  ? fwActive
                    ? "var(--mint)"
                    : "var(--lemon)"
                  : "var(--text3)",
              }}
            />
            <div
              className="min-w-0 flex-1"
              style={{
                fontFamily: "var(--font-nunito), sans-serif",
                fontWeight: 600,
                fontSize: 16,
                color: "var(--text2)",
              }}
            >
              {fw ? (
                <>
                  Firewall {fw.status}
                  {" · "}
                  {fw.rules_count} rule
                  {fw.rules_count === 1 ? "" : "s"} enforced
                  {fw.blocked_count > 0 ? ` · ${fw.blocked_count} blocked` : ""}
                  {fwLockdown ? " · LOCKDOWN" : ""}
                </>
              ) : (
                <>Firewall status unavailable</>
              )}
            </div>
            <span
              className="flex items-center gap-1 transition-opacity group-hover:opacity-100"
              style={{ ...microLabel, opacity: 0.8 }}
            >
              Firewall detail
              <MsIcon name="chevron_right" size={16} />
            </span>
          </Link>
        </div>

        {/* ── Right column ────────────────────────────────── */}
        <div className="flex min-w-0 flex-col gap-5">
          {/* Appearance */}
          <div style={cardStyle}>
            <div
              style={{
                fontFamily: "var(--font-fredoka), sans-serif",
                fontWeight: 600,
                fontSize: 24,
                color: "var(--text)",
                marginBottom: 20,
              }}
            >
              Appearance
            </div>

            <div className="mb-5">
              <ThemePicker
                value={override}
                onPick={pickTheme}
                includeAuto
              />
              {active && (
                <div
                  className="mt-2"
                  style={{
                    font: "600 12px var(--font-nunito), sans-serif",
                    color: "var(--text3)",
                  }}
                >
                  Saved to {active.display_name}’s profile.
                </div>
              )}
            </div>

            <ToggleRow
              title="Big touch mode"
              sub="Larger targets for wall displays"
              on={bigTouch}
              onChange={toggleBigTouch}
            />
            <ToggleRow
              title="Show live transcript"
              sub="Words appear as you speak"
              on={showTranscript}
              onChange={toggleTranscript}
            />
            <ToggleRow
              title="Show Puff’s thinking"
              sub="Her reasoning as she works. Replies start a little later."
              on={showThinking}
              onChange={toggleThinking}
            />
            <ToggleRow
              title="Ambient cloud motion"
              sub="Gentle drift when idle"
              on={ambientMotion}
              onChange={toggleMotion}
            />
            <ToggleRow
              title="Kiosk display"
              sub="A still scene for always-on screens, so the GPU stays free for Puff"
              on={kioskMode}
              onChange={toggleKiosk}
            />
          </div>

          {/* Wake word */}
          <div style={cardStyle}>
            <div
              style={{
                fontFamily: "var(--font-fredoka), sans-serif",
                fontWeight: 600,
                fontSize: 24,
                color: "var(--text)",
                marginBottom: 18,
              }}
            >
              Wake word
            </div>
            <div className="flex flex-wrap gap-3">
              <span
                style={{
                  padding: "14px 24px",
                  borderRadius: 99,
                  background:
                    "linear-gradient(140deg,var(--pink),var(--magenta))",
                  color: "#fff",
                  fontFamily: "var(--font-nunito), sans-serif",
                  fontWeight: 700,
                  fontSize: 17,
                }}
              >
                &ldquo;{wakeWord}&rdquo;
              </span>
              <span
                style={{
                  padding: "14px 24px",
                  borderRadius: 99,
                  background: "var(--card2)",
                  border: "1px solid var(--line)",
                  color: "var(--text2)",
                  fontFamily: "var(--font-nunito), sans-serif",
                  fontWeight: 700,
                  fontSize: 17,
                }}
              >
                Push to talk
              </span>
            </div>
            <div
              style={{
                marginTop: 22,
                padding: "18px 20px",
                borderRadius: 20,
                background: "var(--card2)",
                border: "1px solid var(--line)",
                fontFamily: "var(--font-nunito), sans-serif",
                fontWeight: 500,
                fontSize: 16,
                lineHeight: 1.5,
                color: "var(--text2)",
              }}
            >
              Wake-word detection runs on the CPU only — the microphone buffer
              is discarded every 2 seconds unless PuffPAI hears its name.
            </div>
          </div>

          {/* Home point: where the house is. Set once here, kept on the box,
              never shown back. Weather, alerts and directions read it. */}
          <HomePointCard />

          {/* Paired devices — per-device revocation + the household code/QR */}
          <PairedDevicesCard />

          {/* The Hatch — naming ritual replay + adoption certificate */}
          <HatchCard />

          {/* Skills — what this box can extend itself with, and what it cannot */}
          <SkillsCapabilityCard />

          {/* Connect Spotify — voice-driven Spotify setup */}
          <ConnectSpotifyCard />

          {/* Shortcuts to absorbed pages */}
          <div>
            <div style={{ ...microLabel, margin: "2px 4px 10px" }}>
              Shortcuts
            </div>
            <div className="flex flex-wrap gap-3">
              {[
                { href: "/dashboard/profile", icon: "person", label: "Profile" },
                {
                  href: "/dashboard/documents",
                  icon: "description",
                  label: "My Documents",
                },
                {
                  href: "/dashboard/workflows",
                  icon: "account_tree",
                  label: "Workflows",
                },
                {
                  href: "/dashboard/firewall",
                  icon: "shield_lock",
                  label: "Firewall",
                },
              ].map((l) => (
                <Link
                  key={l.href + l.label}
                  href={l.href}
                  className="flex items-center gap-2 no-underline transition-transform hover:-translate-y-0.5"
                  style={{
                    height: 52,
                    padding: "0 22px",
                    borderRadius: 99,
                    background: "var(--card2)",
                    border: "1px solid var(--line)",
                    backdropFilter: "blur(14px)",
                    WebkitBackdropFilter: "blur(14px)",
                    color: "var(--text2)",
                    fontFamily: "var(--font-nunito), sans-serif",
                    fontWeight: 700,
                    fontSize: 15,
                  }}
                >
                  <MsIcon name={l.icon} size={20} />
                  {l.label}
                </Link>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
