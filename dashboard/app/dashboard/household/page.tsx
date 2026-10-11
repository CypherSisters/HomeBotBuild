"use client";

import { useCallback, useEffect, useState } from "react";
import { mdiAccountGroup } from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import SectionTitleLineWithButton from "../../_components/Section/TitleLineWithButton";
import MsIcon from "../../_components/MsIcon";
import AvatarPicker from "../_components/AvatarPicker";
import { useHousehold } from "../../_lib/hooks/useHousehold";
import {
  ASSIGNABLE_ROLES,
  addMember,
  avatarSrc,
  removeMember,
  setActiveMember,
  updateMember,
  type HouseholdMember,
  type HouseholdRole,
} from "../../_lib/services/householdService";
import ThemePicker, { saveMemberTheme } from "../_components/ThemePicker";
import { normalizeChoice, type ThemeChoice } from "../../_lib/theme";

/**
 * Household — the roster the rest of the appliance reads.
 *
 * Deliberately one dense table-ish list, not a grid of cards: the owner is
 * here to add someone and leave, and every row should be legible at a glance
 * from the wall. Edits save on blur (or Enter) rather than behind a Save
 * button — there is nothing to batch.
 */

const SWATCHES = [
  "#7dd3fc",
  "#c4b5fd",
  "#f9a8d4",
  "#fca5a5",
  "#fcd34d",
  "#86efac",
  "#67e8f9",
  "#fdba74",
];

const ROLE_LABEL: Record<HouseholdRole, string> = {
  owner: "Owner",
  adult: "Adult",
  child: "Child",
  pet: "Pet",
};

const fieldStyle: React.CSSProperties = {
  font: "700 14px var(--font-nunito), sans-serif",
  color: "var(--text)",
  background: "var(--card2)",
  border: "1px solid var(--line)",
  borderRadius: 10,
  padding: "6px 10px",
};

export default function HouseholdPage() {
  const { members, ownerId, activeId, loading, error, bust, refresh } = useHousehold();
  const [banner, setBanner] = useState<string | null>(null);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onError = useCallback((err: any) => {
    setBanner(err?.message || "That didn't save.");
    setTimeout(() => setBanner(null), 5000);
  }, []);

  return (
    <SectionMain>
      <SectionTitleLineWithButton icon={mdiAccountGroup} title="Household" main />

      <p
        className="mb-4"
        style={{ font: "600 13px var(--font-nunito), sans-serif", color: "var(--text2)" }}
      >
        Everyone here shows up in Calendar and FitFam automatically. The screen
        wears the active member’s theme.
      </p>

      {banner && (
        <div
          className="mb-3 px-3 py-2"
          style={{
            font: "700 13px var(--font-nunito), sans-serif",
            color: "var(--pink)",
            border: "1px solid var(--line)",
            borderRadius: 12,
            background: "var(--card)",
          }}
        >
          {banner}
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
        {loading && members.length === 0 ? (
          <div className="px-4 py-6" style={{ color: "var(--text2)" }}>
            Loading&hellip;
          </div>
        ) : error && members.length === 0 ? (
          <div className="px-4 py-6" style={{ color: "var(--pink)" }}>
            {error}
          </div>
        ) : (
          members.map((m) => (
            <MemberRow
              key={m.id}
              member={m}
              isOwner={m.id === ownerId || m.role === "owner"}
              isActive={m.id === activeId}
              bust={bust}
              refresh={refresh}
              onError={onError}
            />
          ))
        )}
        <AddMemberRow refresh={refresh} onError={onError} />
      </div>
    </SectionMain>
  );
}

function MemberRow({
  member,
  isOwner,
  isActive,
  bust,
  refresh,
  onError,
}: {
  member: HouseholdMember;
  isOwner: boolean;
  isActive: boolean;
  bust: number;
  refresh: () => Promise<void>;
  onError: (err: any) => void;
}) {
  const [name, setName] = useState(member.display_name);
  const [emoji, setEmoji] = useState(member.emoji || "");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setName(member.display_name);
    setEmoji(member.emoji || "");
  }, [member.display_name, member.emoji]);

  const save = useCallback(
    async (patch: Partial<HouseholdMember>) => {
      setSaving(true);
      try {
        await updateMember(member.id, patch);
        await refresh();
      } catch (err) {
        onError(err);
      } finally {
        setSaving(false);
      }
    },
    [member.id, refresh, onError],
  );

  const commitName = () => {
    const next = name.trim();
    if (!next || next === member.display_name) {
      setName(member.display_name);
      return;
    }
    save({ display_name: next });
  };

  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-3 px-4 py-3"
      style={{ borderBottom: "1px solid var(--line)", opacity: saving ? 0.6 : 1 }}
    >
      <AvatarPicker
        target={member.id}
        name={member.display_name}
        src={avatarSrc(member, bust)}
        size={52}
        onChanged={refresh}
      />

      <div className="flex min-w-[190px] flex-1 items-center gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commitName}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            if (e.key === "Escape") setName(member.display_name);
          }}
          aria-label={`Name for ${member.display_name}`}
          style={{ ...fieldStyle, flex: 1, minWidth: 0 }}
        />
        <input
          value={emoji}
          onChange={(e) => setEmoji(e.target.value)}
          onBlur={() => {
            if ((emoji || "") !== (member.emoji || "")) save({ emoji: emoji || null });
          }}
          maxLength={2}
          aria-label={`Emoji for ${member.display_name}`}
          placeholder="🙂"
          style={{ ...fieldStyle, width: 48, textAlign: "center" }}
        />
      </div>

      <div className="flex items-center gap-1.5">
        {SWATCHES.map((c) => (
          <button
            key={c}
            type="button"
            aria-label={`Colour ${c} for ${member.display_name}`}
            onClick={() => save({ color: c })}
            className="cursor-pointer"
            style={{
              width: 16,
              height: 16,
              borderRadius: 5,
              background: c,
              border:
                (member.color || "").toLowerCase() === c
                  ? "2px solid var(--text)"
                  : "1px solid var(--line)",
            }}
          />
        ))}
      </div>

      <div className="flex items-center gap-2">
        <ThemePicker
          value={normalizeChoice(member.preferences?.theme)}
          size="chip"
          busy={saving}
          onPick={(next: ThemeChoice) => {
            setSaving(true);
            saveMemberTheme(member.id, next, isActive)
              .then(refresh)
              .catch(onError)
              .finally(() => setSaving(false));
          }}
        />
        <button
          type="button"
          disabled={isActive || saving}
          onClick={() => {
            setSaving(true);
            setActiveMember(member.id)
              .then(refresh)
              .catch(onError)
              .finally(() => setSaving(false));
          }}
          className={isActive ? "" : "cursor-pointer"}
          title={
            isActive
              ? `This screen is showing ${member.display_name}`
              : `Show ${member.display_name} on this screen`
          }
          style={{
            font: "800 11px var(--font-nunito), sans-serif",
            letterSpacing: "0.06em",
            textTransform: "uppercase",
            whiteSpace: "nowrap",
            color: isActive ? "var(--onAccent)" : "var(--text2)",
            background: isActive
              ? "linear-gradient(140deg, var(--pink), var(--lav))"
              : "var(--chip)",
            border: "1px solid var(--line)",
            borderRadius: 999,
            padding: "5px 11px",
          }}
        >
          {isActive ? "On this screen" : "Use here"}
        </button>
      </div>

      {isOwner ? (
        <span
          style={{
            font: "800 11px var(--font-nunito), sans-serif",
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: "var(--text2)",
            border: "1px solid var(--line)",
            borderRadius: 999,
            padding: "3px 10px",
          }}
        >
          {ROLE_LABEL.owner}
        </span>
      ) : (
        <select
          value={member.role}
          onChange={(e) => save({ role: e.target.value as HouseholdRole })}
          aria-label={`Role for ${member.display_name}`}
          style={fieldStyle}
        >
          {ASSIGNABLE_ROLES.map((r) => (
            <option key={r} value={r}>
              {ROLE_LABEL[r]}
            </option>
          ))}
        </select>
      )}

      {!isOwner && (
        <button
          type="button"
          onClick={async () => {
            if (!confirm(`Remove ${member.display_name} from the household?`)) return;
            setSaving(true);
            try {
              await removeMember(member.id);
              await refresh();
            } catch (err) {
              onError(err);
            } finally {
              setSaving(false);
            }
          }}
          className="cursor-pointer"
          aria-label={`Remove ${member.display_name}`}
          style={{ color: "var(--text2)" }}
        >
          <MsIcon name="delete" size={18} />
        </button>
      )}
    </div>
  );
}

function AddMemberRow({
  refresh,
  onError,
}: {
  refresh: () => Promise<void>;
  onError: (err: any) => void;
}) {
  const [name, setName] = useState("");
  const [role, setRole] = useState<HouseholdRole>("adult");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const display = name.trim();
    if (!display || busy) return;
    setBusy(true);
    try {
      await addMember({ display_name: display, role });
      setName("");
      setRole("adult");
      await refresh();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2 px-4 py-3">
      <div
        className="grid flex-none place-items-center"
        style={{
          width: 52,
          height: 52,
          borderRadius: 14,
          border: "1px dashed var(--line)",
          color: "var(--text2)",
        }}
      >
        <MsIcon name="person_add" size={22} />
      </div>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        placeholder="Add someone"
        aria-label="New member name"
        style={{ ...fieldStyle, flex: 1, minWidth: 160 }}
      />
      <select
        value={role}
        onChange={(e) => setRole(e.target.value as HouseholdRole)}
        aria-label="New member role"
        style={fieldStyle}
      >
        {ASSIGNABLE_ROLES.map((r) => (
          <option key={r} value={r}>
            {ROLE_LABEL[r]}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={submit}
        disabled={busy || !name.trim()}
        className="flex cursor-pointer items-center gap-1.5"
        style={{
          font: "800 13px var(--font-nunito), sans-serif",
          color: "#fff",
          background: "linear-gradient(140deg, var(--pink), var(--lav))",
          border: "1px solid var(--line)",
          borderRadius: 10,
          padding: "7px 14px",
          opacity: busy || !name.trim() ? 0.5 : 1,
        }}
      >
        {busy ? "Adding…" : "Add"}
      </button>
    </div>
  );
}
