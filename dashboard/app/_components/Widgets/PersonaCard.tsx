"use client";

/**
 * The bot the owner built, on the home page.
 *
 * A persona's only rail home would otherwise be the search-only Apps submenu,
 * and a trimmed allowlist may load no tile that mentions it. This card is one
 * half of making it findable (the rail entry is in _lib/personaNav.ts,
 * `withPersonaNav`): the home page leads with the thing this build exists to
 * be.
 *
 * TWO STATES, ONE CARD. First run it introduces itself: "Meet Stargazer", what
 * it is for in the owner's own words, and three sentences that work. Dismissed,
 * it stays as a dense one-line strip — name, goal, lists, the way in. Stacking
 * a permanent tile under a dismissible banner that says the same thing is how
 * a home page turns into an onboarding funnel, so it is one card that gets
 * quieter.
 *
 * EVERYTHING HERE IS THE OWNER'S OWN DATA, out of the owner's own manifest via
 * GET /persona/list: the name, the goal (recorded in the scaffolded blurb), the
 * list labels. Rendered as text, escaped by React like any other string. The
 * prompts are derived from those same labels, so a suggestion can never name a
 * list the app does not keep.
 *
 * Copyright (c) Cypher Sisters. Apache-2.0 License.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import MsIcon from "../MsIcon";
import { FREDOKA, JBM, NUNITO } from "./DesignTile";
import {
  personaGoal,
  personaHref,
  personaPrompts,
} from "@/app/_lib/personaNav";
import type { OwnerApp } from "@/app/_lib/services/personaService";

const DISMISS_PREFIX = "puffpai_meet_persona_v1:";

type Props = {
  app: OwnerApp;
  /** Send a sentence to chat. Returns false when the handoff could not be
   *  stashed, and then nothing is navigated — a prompt that silently drops the
   *  question is worse than a prompt that does nothing. */
  onAsk: (text: string) => void;
  /** True when this build was trimmed by the interview, i.e. most tiles are
   *  gone and this card IS the home page. Only changes how much room it takes. */
  lead?: boolean;
};

export default function PersonaCard({ app, onAsk, lead = false }: Props) {
  const goal = personaGoal(app);
  const prompts = personaPrompts(app);
  const href = personaHref(app);
  const labels = (app.list_labels?.length ? app.list_labels : app.lists) ?? [];

  /** Undefined until the read has happened, so the intro does not flash on a
   *  box where it was dismissed months ago. */
  const [met, setMet] = useState<boolean | undefined>(undefined);
  useEffect(() => {
    try {
      setMet(localStorage.getItem(`${DISMISS_PREFIX}${app.id}`) === "1");
    } catch {
      // Private mode, or storage disabled. Treat it as "already met": the
      // introduction is a nicety, and showing it on every single load is worse
      // than never showing it.
      setMet(true);
    }
  }, [app.id]);

  const dismiss = useCallback(() => {
    setMet(true);
    try {
      localStorage.setItem(`${DISMISS_PREFIX}${app.id}`, "1");
    } catch {
      /* it stays dismissed for this page load, which is the best on offer */
    }
  }, [app.id]);

  if (met === undefined) return null;
  const intro = !met;

  return (
    <div
      className="relative mb-3 flex-none overflow-hidden rounded-[28px] border backdrop-blur-[18px]"
      style={{
        borderColor: "var(--line)",
        background: "var(--card)",
        boxShadow: "var(--shadow)",
        padding: intro || lead ? "14px 18px" : "10px 18px",
      }}
    >
      <div className="flex min-w-0 items-center gap-3">
        <MsIcon name="auto_awesome" size={21} style={{ color: "var(--magenta)" }} />
        <span
          className="truncate"
          style={{ font: "600 20px " + FREDOKA, color: "var(--text)" }}
        >
          {intro ? `Meet ${app.name}` : app.name}
        </span>
        {goal && (
          <span
            className="min-w-0 flex-1 truncate"
            style={{
              font: "500 13px " + NUNITO,
              color: "var(--text3)",
            }}
            title={goal}
          >
            {goal}
          </span>
        )}
        {!goal && <span className="min-w-0 flex-1" />}
        <Link
          href={href}
          className="flex-none whitespace-nowrap rounded-full px-3 py-1.5 transition hover:opacity-85"
          style={{
            background: "var(--chip)",
            font: "600 12px " + NUNITO,
            color: "var(--text2)",
          }}
        >
          Open
        </Link>
        <Link
          href="/dashboard/knowledge"
          className="flex-none whitespace-nowrap rounded-full px-3 py-1.5 transition hover:opacity-85"
          style={{
            background: "var(--chip)",
            font: "600 12px " + NUNITO,
            color: "var(--text2)",
          }}
        >
          Knowledge
        </Link>
        {intro && (
          <button
            type="button"
            onClick={dismiss}
            aria-label={`Dismiss the introduction to ${app.name}`}
            className="flex-none cursor-pointer rounded-full border-0 px-2 py-1 transition hover:opacity-85"
            style={{ background: "transparent", color: "var(--text3)" }}
          >
            <MsIcon name="close" size={18} />
          </button>
        )}
      </div>

      {/* The lists this bot keeps. The owner's labels, so the strip is empty
          on a persona that keeps none rather than inventing a shelf. */}
      {(intro || lead) && labels.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {labels.map((label) => (
            <Link
              key={label}
              href={href}
              className="truncate rounded-full px-2.5 py-1 transition hover:opacity-85"
              style={{
                background: "var(--chip)",
                font: "600 11px " + JBM,
                letterSpacing: "0.04em",
                color: "var(--text2)",
                maxWidth: 220,
              }}
            >
              {label}
            </Link>
          ))}
        </div>
      )}

      {/* Three sentences that work, sent to chat exactly as written. */}
      {intro && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span
            style={{
              font: "700 11px " + JBM,
              letterSpacing: "0.14em",
              color: "var(--text3)",
              textTransform: "uppercase",
            }}
          >
            Try saying
          </span>
          {prompts.map((prompt) => (
            <button
              key={prompt}
              type="button"
              onClick={() => onAsk(prompt)}
              className="max-w-full cursor-pointer truncate rounded-full border-0 px-3 py-1.5 transition hover:opacity-85"
              style={{
                background: "var(--chip)",
                font: "600 12px " + NUNITO,
                color: "var(--text2)",
              }}
              title={`Ask ${app.name}: ${prompt}`}
            >
              “{prompt}”
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
