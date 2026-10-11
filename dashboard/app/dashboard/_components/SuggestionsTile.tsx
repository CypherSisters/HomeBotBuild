"use client";

/* ═══════════════════════════════════════════════════════════════
   WHAT PUFF FOUND — the pending-suggestion strip.

   Something arrived by /send, the backend classified it, and there is an
   offer waiting. FloatingPuff already raises the oldest one out loud; this
   is the silent, always-there version for whoever walks up to the kiosk or
   opens the dashboard on a laptop — the whole queue, two answers each.

   NOTHING here writes. Accept resolves the suggestion server-side and hands
   back its candidates, which open the SAME review-and-confirm modal a manual
   import opens; the calendar and FreshTable are written only after the boxes
   are ticked.

   Placement: above the arrangeable board, alongside ConsentRequestsBanner,
   for the reason recorded there — TileGrid drops never-seen tiles at the
   BOTTOM of a saved layout, and a suggestion that expires in minutes must
   not appear below the fold on a kiosk that already has a saved board.
   Renders nothing when the queue is empty.
   ═══════════════════════════════════════════════════════════════ */

import { useCallback, useState } from "react";
import dynamic from "next/dynamic";
import MsIcon from "@/app/_components/MsIcon";
import { usePendingSuggestions } from "@/app/_lib/hooks/usePendingSuggestions";
import { appAction } from "@/app/_lib/services/appService";
import {
  acceptSuggestion,
  dismissSuggestion,
  isResolved,
  suggestionLabel,
  suggestionToIngestResult,
  suggestionToRecipeResult,
  type Suggestion,
} from "@/app/_lib/suggestions";
import type { IngestResult } from "../apps/calendar/ImportEvents";
import type { RecipeResult } from "../apps/freshtable/ImportRecipes";

const ImportEvents = dynamic(() => import("../apps/calendar/ImportEvents"), {
  ssr: false,
});
const ImportRecipes = dynamic(() => import("../apps/freshtable/ImportRecipes"), {
  ssr: false,
});

/** The one line under the title. Puff's own `message` is the fallback so a
 *  new backend kind still says something sensible without a UI change. */
function prompt(s: Suggestion): string {
  switch (s.action?.type) {
    case "recipe_import":
      return "Looks like a recipe — save it to FreshTable?";
    case "calendar_import":
      return "Looks like a schedule — add the events to your calendar?";
    default:
      return s.message || "Filed in Puff's documents.";
  }
}

function KindBadge({ kind }: { kind: string }) {
  if (!kind) return null;
  return (
    <span
      className="flex-none uppercase"
      style={{
        padding: "2px 7px",
        borderRadius: 999,
        border: "1px solid var(--line)",
        background: "var(--card2)",
        color: "var(--text3)",
        fontFamily: "var(--font-jbmono)",
        fontSize: 10,
        letterSpacing: "0.06em",
      }}
    >
      {kind.replace(/_/g, " ")}
    </span>
  );
}

export default function SuggestionsTile() {
  const { suggestions } = usePendingSuggestions();
  /** Server-resolved here; the next poll (5 s) drops them for good. */
  const [resolvedIds, setResolvedIds] = useState<string[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [importing, setImporting] = useState<{
    label: string;
    result: IngestResult;
  } | null>(null);
  const [importingRecipe, setImportingRecipe] = useState<{
    label: string;
    result: RecipeResult;
  } | null>(null);

  const calRunAction = useCallback(
    (action: string, params?: Record<string, unknown>) =>
      appAction("calendar", action, params),
    [],
  );
  const freshRunAction = useCallback(
    (action: string, params?: Record<string, unknown>) =>
      appAction("freshtable", action, params),
    [],
  );

  const fail = (id: string, message: string) =>
    setErrors((e) => ({ ...e, [id]: message }));
  const clearError = (id: string) =>
    setErrors((e) => {
      if (!(id in e)) return e;
      const next = { ...e };
      delete next[id];
      return next;
    });

  /* Rows disappear only when the SERVER resolved them — a 401 or a network
     failure keeps the row and says why it stayed. */
  const accept = useCallback(async (s: Suggestion) => {
    setBusyId(s.id);
    clearError(s.id);
    const res = await acceptSuggestion(s.id);
    setBusyId(null);
    if (!isResolved(res.status)) {
      fail(s.id, "Couldn’t open that — try again.");
      return;
    }
    setResolvedIds((r) => (r.includes(s.id) ? r : [...r, s.id]));
    if (res.status === "ok" && res.suggestion) {
      const record = res.suggestion;
      const label = suggestionLabel(record) || suggestionLabel(s);
      if (record.action?.type === "recipe_import") {
        setImportingRecipe({ label, result: suggestionToRecipeResult(record) });
      } else if (record.action?.type === "calendar_import") {
        setImporting({ label, result: suggestionToIngestResult(record) });
      }
    }
  }, []);

  const dismiss = useCallback(async (s: Suggestion) => {
    setBusyId(s.id);
    clearError(s.id);
    const res = await dismissSuggestion(s.id);
    setBusyId(null);
    if (!isResolved(res.status)) {
      fail(s.id, "Couldn’t file that — try again.");
      return;
    }
    setResolvedIds((r) => (r.includes(s.id) ? r : [...r, s.id]));
  }, []);

  const rows = suggestions.filter((s) => !resolvedIds.includes(s.id));

  return (
    <>
      {rows.length > 0 && (
        <div
          className="mb-3 overflow-hidden"
          style={{
            borderRadius: 18,
            border: "1px solid color-mix(in srgb, var(--pink) 28%, transparent)",
            background: "color-mix(in srgb, var(--pink) 6%, var(--card))",
          }}
        >
          <div
            className="flex items-center gap-2 px-4 py-2"
            style={{ borderBottom: "1px solid var(--line)" }}
          >
            <MsIcon name="auto_awesome" size={16} style={{ color: "var(--pink)" }} />
            <span
              style={{
                fontFamily: "var(--font-nunito)",
                fontWeight: 800,
                fontSize: 13,
                color: "var(--text)",
              }}
            >
              Puff found something
            </span>
            <span
              style={{
                fontFamily: "var(--font-jbmono)",
                fontSize: 11,
                color: "var(--text3)",
              }}
            >
              {rows.length}
            </span>
          </div>

          {rows.map((s, i) => (
            <div
              key={s.id}
              className="flex items-center gap-3 px-4 py-2.5"
              style={{ borderTop: i === 0 ? undefined : "1px solid var(--line)" }}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline gap-2">
                  <span
                    className="truncate"
                    title={suggestionLabel(s)}
                    style={{
                      fontFamily: "var(--font-fredoka)",
                      fontSize: 15,
                      fontWeight: 600,
                      color: "var(--text)",
                    }}
                  >
                    {suggestionLabel(s)}
                  </span>
                  <KindBadge kind={s.kind} />
                </div>
                <div
                  className="truncate"
                  style={{
                    fontFamily: "var(--font-nunito)",
                    fontSize: 12,
                    color: errors[s.id] ? "var(--puff-red)" : "var(--text2)",
                  }}
                  role={errors[s.id] ? "alert" : undefined}
                >
                  {errors[s.id] ?? prompt(s)}
                </div>
              </div>

              <button
                type="button"
                onClick={() => void dismiss(s)}
                disabled={busyId === s.id}
                className="flex-none transition disabled:opacity-40"
                style={{
                  padding: "6px 12px",
                  borderRadius: 999,
                  border: "1px solid var(--line)",
                  background: "var(--card2)",
                  color: "var(--text2)",
                  fontFamily: "var(--font-nunito)",
                  fontWeight: 700,
                  fontSize: 12,
                }}
              >
                Dismiss
              </button>
              <button
                type="button"
                onClick={() => void accept(s)}
                disabled={busyId === s.id}
                className="flex-none transition disabled:opacity-40"
                style={{
                  padding: "6px 14px",
                  borderRadius: 999,
                  border: "1px solid transparent",
                  background:
                    "linear-gradient(135deg, var(--pink), var(--magenta))",
                  color: "#fff",
                  fontFamily: "var(--font-nunito)",
                  fontWeight: 700,
                  fontSize: 12,
                }}
              >
                {busyId === s.id ? "Opening…" : "Accept"}
              </button>
            </div>
          ))}
        </div>
      )}

      {/* The same review-and-confirm screens /send and FloatingPuff open. */}
      {importing && (
        <ImportEvents
          runAction={calRunAction}
          initialResult={importing.result}
          initialLabel={importing.label}
          onClose={() => setImporting(null)}
        />
      )}
      {importingRecipe && (
        <ImportRecipes
          runAction={freshRunAction}
          initialResult={importingRecipe.result}
          initialLabel={importingRecipe.label}
          onClose={() => setImportingRecipe(null)}
        />
      )}
    </>
  );
}
