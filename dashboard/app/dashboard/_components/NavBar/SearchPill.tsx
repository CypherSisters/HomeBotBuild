"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import MsIcon from "../../../_components/MsIcon";
import menuAside from "../../_lib/menuAside";
import { PENDING_CHAT_QUERY_KEY } from "../QuickSearch";
import type { MenuAsideItem } from "../../../_interfaces";

/**
 * Topbar search pill with a mic in its right edge (mic → /dashboard/talk).
 *
 * Same behavior as QuickSearch: type-to-filter destinations from
 * menuAside; no match → hand the raw text to chat via sessionStorage.
 */

type Entry = {
  href: string;
  label: string;
  msIcon?: string;
  keywords: string[];
};

const ALIASES: Record<string, string[]> = {
  "/dashboard/homeassistant": ["smart home", "home", "lights", "devices", "thermostat"],
  "/dashboard/apps/alarmflow": ["timers", "alarms", "alarm", "timer", "reminders"],
  "/dashboard/media": [
    "movies",
    "music",
    "video",
    "watch",
    "listen",
    "jellyfin",
    "books",
    "calibre",
    "bookshelf",
    "gutenberg",
  ],
  "/dashboard/agents": ["agents", "team", "workflows"],
  "/dashboard/chat": ["assistant", "ask"],
  "/dashboard/talk": ["voice", "speak", "microphone"],
  "/dashboard/device": ["jetson", "orin", "gpu", "system", "stats"],
  "/dashboard/vault": ["passwords", "secrets", "private", "credentials"],
  "/dashboard/knowledge": ["docs", "library", "shelf"],
  "/dashboard/documents": ["files", "uploads"],
  "/dashboard/firewall": ["security", "network", "drawbridge", "sentinel"],
  "/dashboard/workflows": ["automation", "node-red", "nodered"],
};

function buildEntries(items: MenuAsideItem[]): Entry[] {
  const out: Entry[] = [];
  for (const item of items) {
    if (item.href) {
      out.push({
        href: item.href,
        label: item.label,
        msIcon: item.msIcon,
        keywords: ALIASES[item.href] ?? [],
      });
    }
    if (item.menu) out.push(...buildEntries(item.menu));
  }
  return out;
}

const ENTRIES = buildEntries(menuAside);

function scoreEntry(entry: Entry, q: string): number {
  const label = entry.label.toLowerCase();
  if (label === q) return 100;
  if (label.startsWith(q)) return 80;
  if (label.includes(q)) return 60;
  if (entry.keywords.some((k) => k === q)) return 55;
  if (entry.keywords.some((k) => k.startsWith(q))) return 50;
  if (entry.keywords.some((k) => k.includes(q))) return 40;
  return -1;
}

export default function SearchPill() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);

  const results = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [] as Entry[];
    return ENTRIES.map((e) => ({ e, score: scoreEntry(e, q) }))
      .filter((x) => x.score >= 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 8)
      .map((x) => x.e);
  }, [query]);

  useEffect(() => {
    setSelected(0);
  }, [results.length]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const goTo = (href: string) => {
    setQuery("");
    setOpen(false);
    router.push(href);
  };

  const handoffToChat = () => {
    const text = query.trim();
    if (!text) return;
    try {
      sessionStorage.setItem(PENDING_CHAT_QUERY_KEY, text);
    } catch {
      /* sessionStorage unavailable — plain navigation still lands on chat */
    }
    setQuery("");
    setOpen(false);
    router.push("/dashboard/chat");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      setOpen(false);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((i) => Math.min(i + 1, Math.max(results.length - 1, 0)));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((i) => Math.max(i - 1, 0));
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (results.length > 0) {
        goTo(results[Math.min(selected, results.length - 1)].href);
      } else {
        handoffToChat();
      }
    }
  };

  const showDropdown = open && query.trim().length > 0;

  return (
    <div
      ref={containerRef}
      className="relative min-w-0"
      // Basis 110 (design says 180) so the pill never crowds the operator
      // chip off a phone-width header; on desktop it grows to 420 regardless.
      style={{ flex: "1 1 110px", maxWidth: 420 }}
    >
      <div
        className="flex items-center overflow-hidden"
        style={{
          gap: 12,
          height: 52,
          padding: "0 8px 0 20px",
          borderRadius: 16,
          background: "var(--card)",
          border: "1px solid var(--line)",
        }}
      >
        <MsIcon name="search" size={22} style={{ color: "var(--text3)" }} />
        {/* No type attr: main.css forces `.dark input[type="text"]` to a slate
            background with !important; an attribute-less input (still text)
            dodges that selector and keeps the glass pill. */}
        <input
          placeholder="Search or ask PuffPAI…"
          className="puff-search-input min-w-0 flex-1"
          style={{
            font: "500 16px var(--font-nunito), sans-serif",
            color: "var(--text)",
            background: "transparent",
            border: "none",
            outline: "none",
            boxShadow: "none",
          }}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          aria-label="Search or ask PuffPAI"
          autoComplete="off"
        />
        {/* Old NavBar mic, absorbed here: tap to talk */}
        <button
          type="button"
          onClick={() => router.push("/dashboard/talk")}
          title="Talk to Puff"
          aria-label="Talk to Puff"
          className="grid flex-none cursor-pointer place-items-center transition-transform hover:scale-105"
          style={{
            width: 38,
            height: 38,
            borderRadius: 999,
            border: "none",
            background: "linear-gradient(150deg, var(--pink), var(--magenta))",
            color: "#fff",
          }}
        >
          <MsIcon name="mic" size={20} />
        </button>
      </div>

      {showDropdown && (
        <div
          className="puff-glass absolute left-0 top-full z-50 mt-2 w-72 overflow-hidden"
          style={{
            borderRadius: 16,
            border: "1px solid var(--line)",
            background: "var(--card)",
            backdropFilter: "blur(18px)",
            WebkitBackdropFilter: "blur(18px)",
            boxShadow: "var(--shadow)",
          }}
        >
          {results.length > 0 ? (
            <ul className="max-h-80 overflow-y-auto py-1">
              {results.map((entry, i) => (
                <li key={entry.href}>
                  <button
                    type="button"
                    onMouseEnter={() => setSelected(i)}
                    onClick={() => goTo(entry.href)}
                    className="flex w-full cursor-pointer items-center gap-2.5 px-3 py-2 text-left transition-colors"
                    style={{
                      font: "600 14px var(--font-nunito), sans-serif",
                      color: i === selected ? "var(--pink)" : "var(--text2)",
                      background: i === selected ? "var(--chip)" : "transparent",
                    }}
                  >
                    {entry.msIcon && (
                      <MsIcon
                        name={entry.msIcon}
                        size={18}
                        style={{ color: "var(--sky)" }}
                      />
                    )}
                    <span className="flex-1 truncate">{entry.label}</span>
                    <span
                      className="truncate"
                      style={{
                        font: "600 10px var(--font-jbmono), monospace",
                        color: "var(--text3)",
                      }}
                    >
                      {entry.href}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <button
              type="button"
              onClick={handoffToChat}
              className="flex w-full cursor-pointer items-center gap-2.5 px-3 py-2.5 text-left transition-colors"
              style={{
                font: "600 14px var(--font-nunito), sans-serif",
                color: "var(--text2)",
              }}
            >
              <MsIcon name="chat_bubble" size={18} style={{ color: "var(--sky)" }} />
              <span className="flex-1 truncate">
                Ask PuffPAI:{" "}
                <span style={{ color: "var(--pink)" }}>&ldquo;{query.trim()}&rdquo;</span>
              </span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}
