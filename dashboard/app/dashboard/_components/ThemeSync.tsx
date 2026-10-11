"use client";

import { useEffect } from "react";
import { useHousehold } from "../../_lib/hooks/useHousehold";
import {
  applyTheme,
  normalizeChoice,
  readStoredChoice,
  resolveTheme,
  writeStoredChoice,
} from "../../_lib/theme";

/**
 * Makes the screen wear the active profile's palette.
 *
 * The inline boot script paints from localStorage before hydration (no flash);
 * this corrects it once the roster lands, and writes the result BACK to
 * localStorage so the next boot paints the right ground first time. Mounted in
 * the dashboard shell next to HouseholdSync, which is the only place the
 * roster is wanted - /login and the pairing page never fetch it.
 *
 * A member with no preferences.theme leaves the device choice alone: that is
 * how "I set Soft Light on my laptop" survives being the active profile.
 *
 * Refetch on puffpai:household-changed is what makes "use on this
 * screen" instant on the kiosk rather than up to 30 s later.
 */
export default function ThemeSync() {
  const { active, refresh } = useHousehold();
  const wanted = normalizeChoice(active?.preferences?.theme);

  useEffect(() => {
    if (!wanted) return;
    if (readStoredChoice() !== wanted) writeStoredChoice(wanted);
    applyTheme(resolveTheme(wanted));
  }, [wanted]);

  useEffect(() => {
    const onChanged = () => {
      refresh();
    };
    window.addEventListener("puffpai:household-changed", onChanged);
    return () => window.removeEventListener("puffpai:household-changed", onChanged);
  }, [refresh]);

  return null;
}
