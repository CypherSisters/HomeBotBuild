"use client";

import { useEffect, useState } from "react";
import { chooseTheme, type PuffTheme } from "@/app/_lib/theme";
import { usePathname } from "next/navigation";
import MsIcon from "../../../_components/MsIcon";
import SearchPill from "./SearchPill";
import WeatherChip from "./WeatherChip";
import OperatorChip from "./OperatorChip";
import { VoiceStopPill } from "../VoiceStop";
import { useAppDispatch, useAppSelector } from "../../../_stores/hooks";
import { setVoiceMode } from "../../../_stores/avatarSlice";

/**
 * Topbar: search pill · clock+date ·
 * weather · [Arrange on /dashboard] · TTS toggle · theme toggle · Operator chip.
 *
 * Arrange protocol: this bar dispatches
 *   window.dispatchEvent(new CustomEvent("puffpai:toggle-arrange"))
 * and listens for
 *   "puffpai:arrange-state"  (CustomEvent<{ arranging: boolean }>)
 * which the dashboard page should emit whenever its arrange state changes
 * (including once on mount) so the button highlight stays in sync.
 */

/** Live clock: JBMono time in sky + uppercase long date. */
/** How often the clock checks the time; it only re-renders when the shown
 *  minute or date actually changes (string state, so React bails out). */
const CLOCK_CHECK_MS = 5_000;

function ClockBlock() {
  const [time, setTime] = useState<string | null>(null);
  const [date, setDate] = useState("");
  useEffect(() => {
    const read = () => {
      const now = new Date();
      setTime(
        now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      );
      setDate(
        now
          .toLocaleDateString("en-US", {
            weekday: "long",
            month: "short",
            day: "numeric",
          })
          .toUpperCase()
      );
    };
    read();
    const iv = setInterval(read, CLOCK_CHECK_MS);
    return () => clearInterval(iv);
  }, []);
  return (
    <div className="flex-none text-right" style={{ lineHeight: 1.05 }}>
      <div
        style={{
          font: "700 clamp(22px,1.7vw,30px) var(--font-jbmono), monospace",
          color: "var(--sky)",
          letterSpacing: "-.02em",
        }}
      >
        {time ?? "--:--"}
      </div>
      <div
        style={{
          font: "700 11px var(--font-jbmono), monospace",
          letterSpacing: ".14em",
          color: "var(--text3)",
          marginTop: 4,
        }}
      >
        {date}
      </div>
    </div>
  );
}

function VDivider() {
  return (
    <div
      className="hidden flex-none md:block"
      style={{ width: 1, height: 44, background: "var(--line)" }}
    />
  );
}

/** 52×52 glass icon button (theme toggle, TTS toggle, hamburger). */
function IconButton({
  glyph,
  title,
  onClick,
  active = false,
  className = "",
}: {
  glyph: string;
  title: string;
  onClick: () => void;
  active?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      className={`grid flex-none cursor-pointer place-items-center transition-transform hover:scale-105 ${className}`}
      style={{
        width: 52,
        height: 52,
        borderRadius: 16,
        border: "1px solid var(--line)",
        background: active
          ? "linear-gradient(140deg, var(--pink), var(--magenta))"
          : "var(--card)",
        color: active ? "#fff" : "var(--text)",
      }}
    >
      <MsIcon name={glyph} size={24} />
    </button>
  );
}

/** Arrange button — only rendered on /dashboard. */
function ArrangeButton() {
  const [arranging, setArranging] = useState(false);

  useEffect(() => {
    const onState = (e: Event) => {
      const detail = (e as CustomEvent<{ arranging?: boolean }>).detail;
      if (detail && typeof detail.arranging === "boolean") {
        setArranging(detail.arranging);
      }
    };
    window.addEventListener("puffpai:arrange-state", onState);
    return () => window.removeEventListener("puffpai:arrange-state", onState);
  }, []);

  const toggle = () => {
    setArranging((a) => !a); // optimistic; page's arrange-state event corrects
    window.dispatchEvent(new CustomEvent("puffpai:toggle-arrange"));
  };

  return (
    <button
      type="button"
      onClick={toggle}
      className="hidden flex-none cursor-pointer items-center sm:flex"
      style={{
        gap: 9,
        height: 52,
        padding: "0 20px",
        borderRadius: 16,
        border: "1px solid var(--line)",
        font: "700 16px var(--font-nunito), sans-serif",
        background: arranging
          ? "linear-gradient(140deg, var(--pink), var(--magenta))"
          : "var(--card)",
        color: arranging ? "#fff" : "var(--text)",
      }}
    >
      <MsIcon name={arranging ? "check" : "open_with"} size={22} />
      {arranging ? "Done" : "Arrange"}
    </button>
  );
}

/**
 * Light/dark quick flip. Three themes now live on the profile (Settings,
 * Profile, Household); this button stays a one-tap "too bright / too dark"
 * escape hatch, so it only moves between the light palette this screen is
 * already wearing and Deep Plum. It writes the device cache, not the
 * profile - a glance-level flip should not rewrite who you are.
 */
function ThemeToggle() {
  const [theme, setTheme] = useState<PuffTheme>("plum");

  useEffect(() => {
    const root = document.documentElement;
    const read = () => {
      const attr = root.getAttribute("data-puff-theme");
      setTheme(attr === "day" || attr === "light" ? attr : "plum");
    };
    read();
    // Keep the icon honest if Settings / the auto-clock script flips the theme.
    const mo = new MutationObserver(read);
    mo.observe(root, { attributes: true, attributeFilter: ["data-puff-theme"] });
    return () => mo.disconnect();
  }, []);

  const isDark = theme === "plum";
  const toggle = () => {
    const next: PuffTheme = isDark ? "day" : "plum";
    chooseTheme(next);
    setTheme(next);
  };

  return (
    <IconButton
      glyph={isDark ? "light_mode" : "dark_mode"}
      title={isDark ? "Switch to Cloud Pink" : "Switch to Deep Plum"}
      onClick={toggle}
    />
  );
}

/** Old NavBar's TTS toggle, kept as a compact button left of the theme toggle. */
function TtsToggle() {
  const dispatch = useAppDispatch();
  const voiceMode = useAppSelector((s) => s.avatar.voiceMode);
  return (
    <IconButton
      glyph={voiceMode ? "record_voice_over" : "voice_over_off"}
      title={voiceMode ? "Voice replies: on" : "Voice replies: off"}
      onClick={() => dispatch(setVoiceMode(!voiceMode))}
      active={voiceMode}
      className="hidden sm:grid"
    />
  );
}

type Props = {
  isMobileAsideOpen: boolean;
  onToggleMobileAside: () => void;
};

export default function NavBar({ isMobileAsideOpen, onToggleMobileAside }: Props) {
  const pathname = usePathname();
  const isDashboard = pathname === "/dashboard";

  return (
    <header
      className="flex flex-none items-center"
      style={{
        height: "clamp(72px,9vh,96px)",
        gap: "clamp(10px,1.2vw,20px)",
        padding: "0 clamp(18px,2vw,36px)",
        borderBottom: "1px solid var(--line)",
      }}
    >
      {/* Mobile hamburger — the rail is an off-canvas drawer below lg */}
      <IconButton
        glyph={isMobileAsideOpen ? "close" : "menu"}
        title={isMobileAsideOpen ? "Close menu" : "Open menu"}
        onClick={onToggleMobileAside}
        className="lg:hidden"
      />

      <SearchPill />

      {/* Push the instrument cluster right even when the pill maxes out */}
      <div className="min-w-0 flex-1" />

      {/* (Puff's parking pocket is in the dashboard hero, next to the
          mic orb. FloatingPuff finds it by [data-puff-dock] wherever it
          lives.) */}

      <div className="hidden md:block">
        <ClockBlock />
      </div>
      <VDivider />
      <div className="hidden md:flex">
        <WeatherChip />
      </div>
      <VDivider />

      {isDashboard && <ArrangeButton />}

      {/* Nothing until she is actually speaking; then the loudest thing here. */}
      <VoiceStopPill />
      <TtsToggle />
      <ThemeToggle />
      <OperatorChip />
    </header>
  );
}
