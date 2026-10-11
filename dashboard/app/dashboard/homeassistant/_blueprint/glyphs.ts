/**
 * Device kind to drafting glyph, and the small palette the sheet draws with.
 *
 * Everything here resolves to a theme token, never a literal colour, so the
 * sheet reads the same way on the near-black night palette and on paper-white
 * day without a second stylesheet.
 */

import {
  mdiLightbulbOutline,
  mdiToggleSwitchOutline,
  mdiThermostat,
  mdiSpeaker,
  mdiCctv,
  mdiLockOutline,
  mdiWindowShutter,
  mdiMotionSensor,
  mdiRouterNetwork,
  mdiCircleOutline,
} from "@mdi/js";

import type { HomeKind, HomeTier } from "../../../_lib/services/homeService";

export const KIND_GLYPH: Record<HomeKind, string> = {
  light: mdiLightbulbOutline,
  switch: mdiToggleSwitchOutline,
  climate: mdiThermostat,
  media: mdiSpeaker,
  camera: mdiCctv,
  lock: mdiLockOutline,
  cover: mdiWindowShutter,
  sensor: mdiMotionSensor,
  hub: mdiRouterNetwork,
  other: mdiCircleOutline,
};

export function glyphFor(kind: HomeKind): string {
  return KIND_GLYPH[kind] ?? mdiCircleOutline;
}

/** The 2px underline under every glyph: where the device's orders come from. */
export const TIER_TONE: Record<HomeTier, string> = {
  local: "var(--mint)",
  cloud: "var(--pink)",
  wifi_direct: "var(--sky)",
  unknown: "var(--text3)",
  system: "var(--text3)",
};

export const TIER_LABEL: Record<HomeTier, string> = {
  local: "local",
  cloud: "cloud",
  wifi_direct: "direct",
  unknown: "unknown",
  system: "system",
};

/** Legend rows in the title block. Only the three the owner cares about. */
export const TIER_LEGEND: HomeTier[] = ["local", "cloud", "unknown"];

/** A light that is on. Warm, and the only warm thing on the sheet. */
export const ON_TONE = "var(--lemon)";
