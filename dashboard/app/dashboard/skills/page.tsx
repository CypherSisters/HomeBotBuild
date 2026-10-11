"use client";

/**
 * Skills — every sandboxed adapter the owner has approved, and the two
 * verdicts that take one back.
 *
 * One dense list, not a grid of cards: a household will have a handful of
 * these, and the questions being answered from across the room are "what did I
 * let in, what does it reach, is it on". Reach is stated on the row itself
 * rather than behind a disclosure, because reach is the whole bargain.
 *
 * Nothing on this page can ADD a skill. Installing happens only through the
 * consent card, where the code is shown first; this page is the standing
 * record and the off switch.
 *
 * puff puff pass
 */

import { useCallback, useEffect, useState } from "react";
import { mdiPuzzleOutline } from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import SectionTitleLineWithButton from "../../_components/Section/TitleLineWithButton";
import SkillCodeModal, { HostList } from "../../_components/Skills/SkillCodeModal";
import {
  getSkills,
  removeSkill,
  setSkillEnabled,
  shortSha,
  type SkillRow,
} from "../../_lib/services/skillService";

const nunito = "var(--font-nunito), sans-serif";

const btn: React.CSSProperties = {
  font: `700 11px ${nunito}`,
  color: "var(--text2)",
  background: "transparent",
  border: "1px solid var(--line)",
  borderRadius: 999,
  padding: "4px 10px",
};

function formatDate(iso?: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  return new Date(t).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/**
 * The status chip.
 *
 * Three states, in the order they override each other: a skill whose files no
 * longer hash to what was approved is TAMPERED and says so loudest; a box with
 * no sandbox makes every skill OFFLINE, because a skill must never fall back
 * to running unsandboxed; then plain on/off.
 */
function StatusChip({ row, sandbox }: { row: SkillRow; sandbox: boolean }) {
  const [label, color] = !row.intact
    ? ["Code changed", "var(--pink)"]
    : !sandbox
      ? ["Offline", "var(--text2)"]
      : row.enabled
        ? ["Enabled", "var(--mint, #4ade80)"]
        : ["Off", "var(--text2)"];
  return (
    <span
      style={{
        font: `800 10px ${nunito}`,
        color,
        border: `1px solid ${color}`,
        borderRadius: 999,
        padding: "2px 8px",
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </span>
  );
}

function SkillRowView({
  row,
  sandbox,
  refresh,
  onError,
}: {
  row: SkillRow;
  sandbox: boolean;
  refresh: () => Promise<void>;
  onError: (msg: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [showCode, setShowCode] = useState(false);
  const sha = row.sha256?.["skill.py"] || "";

  const flip = async () => {
    setBusy(true);
    try {
      await setSkillEnabled(row.id, !row.enabled);
      await refresh();
    } catch (e: any) {
      onError(e?.message || "That didn't change.");
    } finally {
      setBusy(false);
    }
  };

  const drop = async () => {
    setBusy(true);
    try {
      await removeSkill(row.id);
      setConfirming(false);
      await refresh();
    } catch (e: any) {
      onError(e?.message || "That didn't remove.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="flex flex-wrap items-start gap-3 px-4 py-3"
      style={{ borderTop: "1px solid var(--line)" }}
    >
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span style={{ font: `800 14px ${nunito}`, color: "var(--text)" }}>{row.name}</span>
          <StatusChip row={row} sandbox={sandbox} />
        </div>
        <p className="mt-0.5" style={{ font: `600 12px ${nunito}`, color: "var(--text2)" }}>
          {row.description}
        </p>
        <p className="mt-1" style={{ font: `600 11px ${nunito}`, color: "var(--text2)" }}>
          Reaches <HostList hosts={row.hosts} size={11} />
          {row.installed_at ? ` · added ${formatDate(row.installed_at)}` : ""}
          {sha ? ` · sha256 ${shortSha(sha)}` : ""}
        </p>
        {!row.intact && (
          <p className="mt-1" style={{ font: `700 11px ${nunito}`, color: "var(--pink)" }}>
            The installed files are not the ones you approved, so this skill will not run.
          </p>
        )}
        {row.last_refusal && (
          <p className="mt-1" style={{ font: `600 11px ${nunito}`, color: "var(--text2)" }}>
            Last refusal: {row.last_refusal}
          </p>
        )}
      </div>

      <div className="flex flex-shrink-0 flex-wrap items-center gap-2">
        <button type="button" style={btn} onClick={() => setShowCode(true)}>
          View code
        </button>
        <button type="button" style={btn} disabled={busy} onClick={flip}>
          {row.enabled ? "Disable" : "Enable"}
        </button>
        {confirming ? (
          <>
            <button
              type="button"
              style={{ ...btn, color: "var(--pink)", borderColor: "var(--pink)" }}
              disabled={busy}
              onClick={drop}
            >
              Remove for good
            </button>
            <button type="button" style={btn} disabled={busy} onClick={() => setConfirming(false)}>
              Keep
            </button>
          </>
        ) : (
          <button type="button" style={btn} disabled={busy} onClick={() => setConfirming(true)}>
            Remove
          </button>
        )}
      </div>

      {showCode && (
        <SkillCodeModal
          skillId={row.id}
          title={`${row.name} — code`}
          onClose={() => setShowCode(false)}
        />
      )}
    </div>
  );
}

export default function SkillsPage() {
  const [rows, setRows] = useState<SkillRow[]>([]);
  const [sandbox, setSandbox] = useState(true);
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [banner, setBanner] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const listing = await getSkills();
      setRows(listing.skills);
      setSandbox(listing.enabled);
      setReason(listing.reason);
      setBanner(null);
    } catch (e: any) {
      setBanner(e?.message || "Couldn't read the skills list.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onError = useCallback((msg: string) => {
    setBanner(msg);
    setTimeout(() => setBanner(null), 6000);
  }, []);

  return (
    <SectionMain>
      <SectionTitleLineWithButton icon={mdiPuzzleOutline} title="Skills" main />

      <p className="mb-4" style={{ font: `600 13px ${nunito}`, color: "var(--text2)" }}>
        <span style={{ font: `800 13px ${nunito}`, color: "var(--text)" }}>
          How this works.{" "}
        </span>
        Each skill runs in a sandbox with no files of yours, no Home Assistant and no
        passwords. It reaches only the hosts you approved, through a door that is shut
        otherwise. You see the code before anything is installed.
      </p>

      {banner && (
        <div
          className="mb-3 px-3 py-2"
          style={{
            font: `700 13px ${nunito}`,
            color: "var(--pink)",
            border: "1px solid var(--line)",
            borderRadius: 12,
            background: "var(--card)",
          }}
        >
          {banner}
        </div>
      )}

      {!sandbox && (
        <div
          className="mb-3 px-3 py-2"
          style={{
            font: `600 12px ${nunito}`,
            color: "var(--text2)",
            border: "1px solid var(--line)",
            borderRadius: 12,
            background: "var(--card2)",
          }}
        >
          Skills are offline on this box{reason ? `: ${reason}` : "."} They stay listed and
          stay switched off rather than running without the sandbox.
        </div>
      )}

      <div
        style={{
          border: "1px solid var(--line)",
          borderRadius: 16,
          background: "var(--card)",
          overflow: "hidden",
        }}
      >
        <div
          className="px-4 py-2"
          style={{ font: `800 11px ${nunito}`, color: "var(--text2)", background: "var(--card2)" }}
        >
          Installed skills
        </div>
        {loading ? (
          <div className="px-4 py-6" style={{ color: "var(--text2)", font: `600 13px ${nunito}` }}>
            Loading&hellip;
          </div>
        ) : rows.length === 0 ? (
          <div
            className="px-4 py-6"
            style={{ color: "var(--text2)", font: `600 13px ${nunito}`, borderTop: "1px solid var(--line)" }}
          >
            No skills yet. Skills are small sandboxed adapters you approve one at a time.
          </div>
        ) : (
          rows.map((row) => (
            <SkillRowView
              key={row.id}
              row={row}
              sandbox={sandbox}
              refresh={refresh}
              onError={onError}
            />
          ))
        )}
      </div>
    </SectionMain>
  );
}
