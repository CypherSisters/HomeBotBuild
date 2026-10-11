"use client";

import React from "react";
import Button from "@/app/_components/Button";

/**
 * AppButton — the themed button for the five app routes.
 *
 * The shared `Button` resolves its colour through `app/_lib/colors.ts`, which
 * is hard-coded stock Tailwind (`bg-blue-600`, `bg-red-600`, `bg-yellow-600`,
 * `bg-emerald-600`) with no theme token anywhere in it. Against the pastel day
 * palette those read as saturated blue/red/yellow blocks, and they are
 * identical in both palettes.
 *
 * `colors.ts` is shared with every other page in the dashboard, so it is not
 * ours to change. Instead this wraps the shared Button, passes the neutral
 * `white` base through it — so every behaviour, prop, `href` and handler is
 * still the shared component's — and adds a `.puff-btn` class that css/_apps.css
 * paints from theme tokens and sizes to the 48px wall floor.
 *
 * Tone map (--mint and --lemon are reserved for state, so a *state* button is
 * exactly where they belong):
 *
 *   primary  --sky       the main action
 *   ok       --mint      confirm, complete, add
 *   warn     --lemon     snooze, pause, stop
 *   danger   --magenta   delete and other irreversible actions
 *   quiet    --chip      secondary and unselected
 */
export type Tone = "primary" | "ok" | "warn" | "danger" | "quiet";

type ButtonProps = React.ComponentProps<typeof Button>;

type Props = Omit<ButtonProps, "color"> & {
  tone?: Tone;
  /** Selected state for tab/toggle rows. Renders the tone filled. */
  selected?: boolean;
};

export default function AppButton({
  tone = "quiet",
  selected,
  className = "",
  ...rest
}: Props) {
  const cls = [
    "puff-btn",
    `puff-btn-${tone}`,
    selected === false ? "puff-btn-off" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  // `color="white"` is the neutral base: it emits no stock accent utility for
  // .puff-btn to fight, so the token paint below is a clean override.
  return <Button {...rest} color="white" className={cls} />;
}
