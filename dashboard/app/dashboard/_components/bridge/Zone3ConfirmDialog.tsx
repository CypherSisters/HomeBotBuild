"use client";

/**
 * The one screen where a person authorises a change to the household's DNS
 * filter.
 *
 * Same protocol as `HaConfirmDialog`, different subject. That dialog is written
 * around Home Assistant's vocabulary — entity ids, service domains, scene
 * membership — none of which describes "start refusing your television's
 * telemetry at DNS". Reusing it would have meant either bending Zone 3 into
 * entity language or widening the HA dialog until it stopped being about
 * anything. The *protocol* is what matters and that is shared: the server
 * refuses with 409 and a proposal, the owner reads the server's own words, a
 * single-use ticket is minted and presented back within its TTL, and the ticket
 * is what actuates.
 *
 * Everything above the buttons is the SERVER's description, never this app's.
 * A mismatch between what the owner clicked and what the backend is about to do
 * is precisely what this screen exists to expose, and it can only be caught by
 * showing the far side's version.
 *
 * Not an error dialog. A 409 here is a question.
 *
 * puff puff pass
 */

import { mdiAlertOutline, mdiCheck, mdiClose } from "@mdi/js";
import Icon from "../../../_components/Icon";
import type { Zone3Controller } from "../../../_lib/hooks/useZone3Confirmation";

export default function Zone3ConfirmDialog({
  controller,
}: {
  controller: Zone3Controller;
}) {
  const { state, confirm, cancel } = controller;
  if (state.phase === "idle") return null;

  const { proposal, refusal, request } = state;
  const busy = state.phase === "minting" || state.phase === "redeeming";

  // The server's headline, with a last-resort fallback. A route that refuses
  // without a message is a bug, and saying "this needs your confirmation" is
  // the honest thing to render rather than inventing the consequence.
  const headline =
    proposal?.headline || refusal || "This change needs your confirmation.";
  const effects = proposal?.effects ?? [];
  const reassurances = proposal?.always_allowed ?? [];

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/75 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label="Confirm a change to the network filter"
    >
      <div
        className="w-full max-w-lg rounded border"
        style={{ borderColor: "var(--lemon)", background: "var(--card)" }}
      >
        <header
          className="flex items-center gap-2.5 border-b px-4 py-3"
          style={{ borderColor: "var(--line)" }}
        >
          <Icon path={mdiAlertOutline} size="20" />
          <span
            className="text-[11px] font-bold tracking-[0.18em] uppercase"
            style={{ color: "var(--lemon)" }}
          >
            Your call
          </span>
          <span className="ml-auto text-[10px]" style={{ color: "var(--text2)" }}>
            {request?.label}
          </span>
        </header>

        <div className="px-4 py-4">
          <p className="text-sm leading-relaxed" style={{ color: "var(--text)" }}>
            {headline}
          </p>

          {effects.length > 0 && (
            <ul className="mt-3 space-y-1">
              {effects.map((line) => (
                <li
                  key={line}
                  className="border-l-2 pl-2.5 text-xs leading-relaxed"
                  style={{ borderColor: "var(--pink)", color: "var(--text2)" }}
                >
                  {line}
                </li>
              ))}
            </ul>
          )}

          {reassurances.length > 0 && (
            <ul className="mt-3 space-y-1">
              {reassurances.map((line) => (
                <li
                  key={line}
                  className="border-l-2 pl-2.5 text-xs leading-relaxed"
                  style={{ borderColor: "var(--mint)", color: "var(--text2)" }}
                >
                  {line}
                </li>
              ))}
            </ul>
          )}

          {/* The honest footnote. The ticket proves a two-step protocol was
              completed against this API — not that a person was on the other
              end. Saying so here is cheaper than an owner assuming more. */}
          <p className="mt-4 text-[11px]" style={{ color: "var(--text2)" }}>
            Confirming mints a single-use ticket that authorises exactly this
            change, once. Nothing has happened yet.
          </p>

          {state.error && (
            <p className="mt-3 text-xs" style={{ color: "var(--pink)" }}>
              {state.error}
            </p>
          )}
        </div>

        <footer
          className="flex justify-end gap-2 border-t px-4 py-3"
          style={{ borderColor: "var(--line)" }}
        >
          <button
            onClick={cancel}
            disabled={busy}
            className="flex items-center gap-1.5 rounded px-3 py-1.5 text-[10px] font-semibold tracking-[0.12em] uppercase disabled:opacity-40"
            style={{ background: "var(--chip)", color: "var(--text2)" }}
          >
            <Icon path={mdiClose} size="14" />
            Leave it
          </button>
          <button
            onClick={confirm}
            disabled={busy}
            className="flex items-center gap-1.5 rounded px-3 py-1.5 text-[10px] font-semibold tracking-[0.12em] uppercase disabled:opacity-40"
            style={{ background: "var(--lemon)", color: "#111" }}
          >
            <Icon path={mdiCheck} size="14" />
            {busy ? "Working" : "Do it"}
          </button>
        </footer>
      </div>
    </div>
  );
}
