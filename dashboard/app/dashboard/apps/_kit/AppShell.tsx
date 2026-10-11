"use client";

import React, { ReactNode } from "react";

type Props = {
  /** The app's plain name. Nothing else — no tagline, no product line. */
  title: string;
  /**
   * One short line under the title. Optional, and at most one line: the
   * house style is dense, not explanatory.
   */
  hint?: string;
  /** Header controls. Each should be >=48px on the wall (.puff-tap). */
  actions?: ReactNode;
  children: ReactNode;
};

/**
 * AppShell — the frame every /dashboard/apps/<id> route renders inside.
 *
 * - Title in the theme's display face (Fredoka via the h1 rule in
 *   css/_theme.css), sized off the viewport so it holds the wall type floor
 *   at 2560 and still fits 390.
 * - No mascot and no assistant character inside an app's content area
 *   The shell around this — Puff bottom-right, the PuffPAI logo,
 *   the footer — is the product and stays where it is.
 * - Full width rather than the shell's 1720px container cap, so the 2560
 *   wall is a designed surface instead of a centred column on wallpaper.
 */
export default function AppShell({ title, hint, actions, children }: Props) {
  return (
    <section className="puff-app">
      <header className="puff-app-head">
        <div className="min-w-0">
          <h1 className="puff-app-title">{title}</h1>
          {hint ? <p className="puff-app-hint">{hint}</p> : null}
        </div>
        {actions ? <div className="puff-app-actions">{actions}</div> : null}
      </header>
      {children}
    </section>
  );
}
