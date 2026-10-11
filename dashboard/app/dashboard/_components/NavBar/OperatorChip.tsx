"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import MsIcon from "../../../_components/MsIcon";
import menuNavBar from "../../_lib/menuNavBar";
import { useHousehold } from "../../../_lib/hooks/useHousehold";
import { avatarSrc } from "../../../_lib/services/householdService";

/**
 * Operator chip: the active person's picture —
 * or initials on a pink→lav gradient when there is none — plus their name.
 *
 * Reads the household roster, not GET /profile: one identity source for the
 * chip, the welcome card and the dashboard greeting, so a saved name shows up
 * in all three at once. "Operator" survives only as the pre-load fallback.
 */

function initialsOf(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "OP";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

export default function OperatorChip() {
  const router = useRouter();
  const { owner, bust } = useHousehold();
  const name = owner?.display_name?.trim() || "Operator";
  const picture = owner ? avatarSrc(owner, bust) : null;
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

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

  return (
    <div ref={containerRef} className="relative flex-none">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex cursor-pointer items-center"
        aria-haspopup="menu"
        aria-expanded={open}
        style={{
          gap: 12,
          height: 52,
          padding: "0 18px 0 8px",
          borderRadius: 16,
          background: "var(--card)",
          border: "1px solid var(--line)",
          color: "var(--text)",
        }}
      >
        <div
          className="grid place-items-center overflow-hidden"
          style={{
            width: 38,
            height: 38,
            borderRadius: 12,
            background: picture
              ? "var(--card)"
              : "linear-gradient(140deg, var(--pink), var(--lav))",
            font: "800 14px var(--font-nunito), sans-serif",
            color: "#fff",
          }}
        >
          {picture ? (
            /* eslint-disable-next-line @next/next/no-img-element */
            <img
              src={picture}
              alt=""
              width={38}
              height={38}
              style={{ width: 38, height: 38, objectFit: "cover" }}
            />
          ) : (
            initialsOf(name)
          )}
        </div>
        <div
          className="hidden sm:block"
          style={{ font: "700 16px var(--font-nunito), sans-serif" }}
        >
          {name}
        </div>
      </button>

      {open && (
        <div
          role="menu"
          className="puff-glass absolute right-0 top-full z-50 mt-2 w-44 overflow-hidden py-1"
          style={{
            borderRadius: 16,
            border: "1px solid var(--line)",
            background: "var(--card)",
            backdropFilter: "blur(18px)",
            WebkitBackdropFilter: "blur(18px)",
            boxShadow: "var(--shadow)",
          }}
        >
          {menuNavBar.map((item) =>
            item.href ? (
              <Link
                key={item.label}
                href={item.href}
                role="menuitem"
                onClick={() => setOpen(false)}
                className="flex items-center gap-2.5 px-4 py-2.5 transition-colors hover:opacity-80"
                style={{
                  font: "600 14px var(--font-nunito), sans-serif",
                  color: "var(--text2)",
                }}
              >
                {item.msIcon && (
                  <MsIcon name={item.msIcon} size={18} style={{ color: "var(--sky)" }} />
                )}
                {item.label}
              </Link>
            ) : (
              <button
                key={item.label}
                type="button"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  // No real session exists to destroy (the old template item
                  // was inert) — route to /login so the action stays alive.
                  if (item.isLogout) router.push("/login");
                }}
                className="flex w-full cursor-pointer items-center gap-2.5 px-4 py-2.5 text-left transition-colors hover:opacity-80"
                style={{
                  font: "600 14px var(--font-nunito), sans-serif",
                  color: "var(--text2)",
                }}
              >
                {item.msIcon && (
                  <MsIcon name={item.msIcon} size={18} style={{ color: "var(--pink)" }} />
                )}
                {item.label}
              </button>
            )
          )}
        </div>
      )}
    </div>
  );
}
