"use client";

import MsIcon from "../../_components/MsIcon";
import { setMemberPreferences } from "../../_lib/services/householdService";
import { chooseTheme, THEMES, type ThemeChoice } from "../../_lib/theme";

/**
 * Save a member's palette, and repaint only if this screen is theirs.
 *
 * Repainting unconditionally would fight ThemeSync: setting another member's theme from
 * the owner's browser would flip the owner's screen, and the next roster poll
 * would flip it back. So the paint is gated on "am I looking at my own
 * screen" and the save is a merge patch either way.
 */
export async function saveMemberTheme(
  memberId: string,
  choice: ThemeChoice,
  isThisScreen: boolean,
): Promise<void> {
  if (isThisScreen) chooseTheme(choice);
  await setMemberPreferences(memberId, { theme: choice });
}

/**
 * ThemePicker — the one way a palette gets chosen, used in three places:
 * the profile page (the owner's own), the household page (per member, compact)
 * and the Settings appearance tile (the active profile, plus "Auto").
 *
 * Each swatch is a live miniature of the real thing rather than a colour dot:
 * the ground wash, one glass tile, and the halo the tile grid actually wears.
 * That is the whole difference between the three themes, so it is the whole
 * preview - no copy beyond the name and a four-word hint.
 *
 * The caller owns persistence; this only reports the pick.
 */

export default function ThemePicker({
  value,
  onPick,
  includeAuto = false,
  size = "card",
  busy = false,
}: {
  /** The stored choice; null reads as "nothing picked yet". */
  value: ThemeChoice | null;
  onPick: (choice: ThemeChoice) => void;
  includeAuto?: boolean;
  size?: "card" | "chip";
  busy?: boolean;
}) {
  const chip = size === "chip";

  return (
    <div style={{ opacity: busy ? 0.55 : 1 }}>
      <div className="flex" style={{ gap: chip ? 8 : 14 }}>
        {THEMES.map((t) => {
          const on = value === t.id;
          return (
            <button
              key={t.id}
              type="button"
              onClick={() => onPick(t.id)}
              aria-pressed={on}
              title={`${t.label} - ${t.hint}`}
              className="flex flex-1 cursor-pointer flex-col items-center justify-center overflow-hidden"
              style={{
                height: chip ? 44 : 120,
                minWidth: chip ? 52 : 0,
                borderRadius: chip ? 12 : 22,
                border: on ? "2px solid var(--pink)" : "1px solid var(--line)",
                boxShadow: on ? "0 0 0 4px var(--glow1)" : "none",
                background: t.ground,
                color: t.ink,
                gap: chip ? 0 : 8,
                padding: chip ? 0 : "10px 6px",
              }}
            >
              {/* The glowing tile, at scale. */}
              <span
                aria-hidden
                style={{
                  display: "block",
                  width: chip ? 26 : 54,
                  height: chip ? 16 : 32,
                  borderRadius: chip ? 6 : 10,
                  background: t.card,
                  boxShadow: `0 0 ${chip ? 10 : 18}px 0 ${t.halo}, 0 0 0 1px ${t.halo}`,
                }}
              />
              {!chip && (
                <>
                  <span
                    style={{
                      font: "700 15px var(--font-nunito), sans-serif",
                      lineHeight: 1.1,
                    }}
                  >
                    {t.label}
                  </span>
                  <span
                    style={{
                      font: "600 11px var(--font-nunito), sans-serif",
                      opacity: 0.68,
                      textAlign: "center",
                      lineHeight: 1.2,
                    }}
                  >
                    {t.hint}
                  </span>
                </>
              )}
            </button>
          );
        })}
      </div>

      {includeAuto && (
        <button
          type="button"
          onClick={() => onPick("auto")}
          aria-pressed={value === "auto" || value === null}
          className="mt-3.5 flex w-full cursor-pointer items-center justify-center gap-2"
          style={{
            height: 46,
            borderRadius: 99,
            border:
              value === "auto" || value === null
                ? "1px solid transparent"
                : "1px solid var(--line)",
            background:
              value === "auto" || value === null
                ? "linear-gradient(140deg,var(--pink),var(--magenta))"
                : "var(--card2)",
            color: value === "auto" || value === null ? "#fff" : "var(--text2)",
            font: "700 15px var(--font-nunito), sans-serif",
          }}
        >
          <MsIcon name="schedule" size={20} />
          Auto — follow the clock
        </button>
      )}
    </div>
  );
}
