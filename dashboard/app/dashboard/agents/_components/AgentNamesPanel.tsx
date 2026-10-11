"use client";

/**
 * AgentNamesPanel — what this household calls its agents.
 *
 * The backend already carries the idea (agents.name_preset / agents.names, and
 * TeamAgent.display_name is served from it), but nothing in the dashboard could
 * SET it: renaming meant editing config on the box. This panel is that control.
 *
 * THE ONE RULE HERE
 * -----------------
 * The internal id is what the bus routes on. It is shown, because a household
 * that renames "sentinel" to "Drawbridge" needs to know which agent that is when
 * a log line says `sentinel`, but it is never editable — an id typo would break
 * routing silently, and no amount of UI politeness would tell anybody why. Only
 * the DISPLAY name is a text field.
 *
 * A 404 from GET/PUT /agents/names is not an error to report as a failure: it
 * means this build does not carry the feature, and the panel says exactly that
 * instead of offering a Save that cannot land.
 */

import { useCallback, useEffect, useState } from "react";
import { mdiContentSave, mdiTagOutline } from "@mdi/js";
import CardBox from "../../../_components/CardBox";
import Icon from "../../../_components/Icon";
import Button from "../../../_components/Button";
import type { TeamAgent } from "../../../_lib/services/agentService";

const API_BASE = "/api/pai";

/** What GET /agents/names answers. Every field optional: this reads whatever a
 *  given build happens to serve and shows the rest as unknown. */
interface NamesAnswer {
  /** The active preset's id, e.g. "default". */
  preset?: string;
  /** Presets to choose from. Either ids, or {id,label} — both accepted. */
  presets?: (string | { id: string; label?: string })[];
  /** Per-agent overrides, internal id -> display name. */
  names?: Record<string, string>;
  /** The box own ceiling on a display name. Enforced here so the field cannot
   *  accept what the PUT will reject. */
  max_name_length?: number;
}

type Load =
  /** The endpoint answered. */
  | { state: "ready"; data: NamesAnswer }
  /** 404 — this build has no such endpoint. Not a failure to shout about. */
  | { state: "absent" }
  /** Anything else, with the backend's own words. */
  | { state: "error"; message: string }
  | { state: "loading" };

function presetId(entry: string | { id: string; label?: string }): string {
  return typeof entry === "string" ? entry : entry.id;
}

function presetLabel(entry: string | { id: string; label?: string }): string {
  if (typeof entry !== "string" && entry.label) return entry.label;
  const id = presetId(entry);
  return id.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function AgentNamesPanel({ team }: { team: TeamAgent[] }) {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  /** Edits in hand. Absent key = no override for that agent. */
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [preset, setPreset] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const read = useCallback(async () => {
    setLoad({ state: "loading" });
    try {
      const res = await fetch(`${API_BASE}/agents/names`, { cache: "no-store" });
      if (res.status === 404) {
        setLoad({ state: "absent" });
        return;
      }
      const data = (await res.json().catch(() => ({}))) as NamesAnswer & {
        error?: string;
      };
      if (!res.ok) {
        setLoad({
          state: "error",
          message: data.error || `Could not read the names (HTTP ${res.status}).`,
        });
        return;
      }
      setLoad({ state: "ready", data });
      setDraft({ ...(data.names ?? {}) });
      setPreset(data.preset ?? "");
    } catch {
      setLoad({ state: "error", message: "Could not reach the box to read the names." });
    }
  }, []);

  useEffect(() => {
    void read();
  }, [read]);

  const save = async () => {
    setSaving(true);
    setSaved(null);
    setSaveError(null);
    try {
      // Only non-empty overrides are sent. A cleared field means "no override",
      // which is a different instruction from "call it the empty string".
      const names: Record<string, string> = {};
      for (const [id, value] of Object.entries(draft)) {
        const trimmed = value.trim();
        if (trimmed) names[id] = trimmed;
      }
      const body: { preset?: string; names: Record<string, string> } = { names };
      if (preset) body.preset = preset;

      const res = await fetch(`${API_BASE}/agents/names`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await res.json().catch(() => ({}))) as { error?: string };
      if (!res.ok) {
        setSaveError(data.error || `The box refused the change (HTTP ${res.status}).`);
        return;
      }
      setSaved("Saved. The names update on the next refresh of this page.");
      await read();
    } catch {
      setSaveError("Could not reach the box to save the change.");
    } finally {
      setSaving(false);
    }
  };

  if (load.state === "loading") {
    return (
      <CardBox className="mb-6">
        <div className="animate-pulse p-4">
          <div className="h-4 w-32 rounded bg-gray-200 dark:bg-slate-700" />
        </div>
      </CardBox>
    );
  }

  if (load.state === "absent") {
    return (
      <CardBox className="mb-6">
        <div className="p-4">
          <h3 className="mb-1 flex items-center gap-2 font-bold">
            <Icon path={mdiTagOutline} size="18" /> Names
          </h3>
          <p className="text-sm text-gray-600 dark:text-slate-400">
            Renaming the agents is not in this build. The names on the cards
            below are the ones it was built with.
          </p>
        </div>
      </CardBox>
    );
  }

  const presets = load.state === "ready" ? (load.data.presets ?? []) : [];

  return (
    <CardBox className="mb-6">
      <div className="p-4">
        <h3 className="mb-1 flex items-center gap-2 font-bold">
          <Icon path={mdiTagOutline} size="18" /> Names
        </h3>
        <p className="mb-4 text-sm text-gray-600 dark:text-slate-400">
          What this household calls each agent. The id underneath each name is what
          the bus routes on — it never changes.
        </p>

        {load.state === "error" && (
          <p className="mb-4 text-sm text-red-600 dark:text-red-400">{load.message}</p>
        )}

        {presets.length > 0 && (
          <div className="mb-4">
            <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-slate-400">
              Name set
            </label>
            <select
              value={preset}
              onChange={(e) => setPreset(e.target.value)}
              className="rounded-lg border border-gray-200 bg-white/70 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-800/70 dark:text-white"
            >
              {/* Empty option stays available so a household can hand the choice
                  back to the build's own default rather than pinning a preset. */}
              <option value="">(build default)</option>
              {presets.map((entry) => (
                <option key={presetId(entry)} value={presetId(entry)}>
                  {presetLabel(entry)}
                </option>
              ))}
            </select>
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          {team.map((agent) => (
            <div key={agent.name}>
              <input
                type="text"
                value={draft[agent.name] ?? ""}
                placeholder={agent.display_name || agent.name}
                onChange={(e) =>
                  setDraft((prev) => ({ ...prev, [agent.name]: e.target.value }))
                }
                maxLength={load.state === "ready" ? (load.data.max_name_length ?? 40) : 40}
                aria-label={`Display name for ${agent.name}`}
                className="w-full rounded-lg border border-gray-200 bg-white/70 px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-800/70 dark:text-white"
              />
              {/* Shown, never editable: the routing id. */}
              <p className="mt-1 font-mono text-[11px] text-gray-400">{agent.name}</p>
            </div>
          ))}
        </div>

        {team.length === 0 && (
          <p className="text-sm text-gray-600 dark:text-slate-400">
            The team list has not loaded, so there is nothing to rename yet.
          </p>
        )}

        <div className="mt-4 flex items-center gap-3">
          <Button
            icon={mdiContentSave}
            label={saving ? "Saving…" : "Save names"}
            color="info"
            small
            disabled={saving || team.length === 0}
            onClick={save}
          />
          {saved && (
            <span className="text-sm text-emerald-600 dark:text-emerald-400">{saved}</span>
          )}
          {saveError && (
            <span className="text-sm text-red-600 dark:text-red-400">{saveError}</span>
          )}
        </div>
      </div>
    </CardBox>
  );
}
