"use client";

/**
 * The sheet's header strip: layer, floor, edit.
 *
 * Traffic brings its own window selector, which only appears while that layer
 * is up: the window is a property of the traffic reading and nothing else on
 * the sheet has an opinion about it.
 */

import { useEffect, useRef, useState } from "react";

import type { HomeFloor } from "../../../_lib/services/homeService";
import { TRAFFIC_WINDOWS, type LayerKey } from "./view";

/** Names travel to a backend that rejects control characters and anything over 64. */
function clean(raw: string) {
  const keep = Array.from(raw).filter((ch) => {
    const code = ch.codePointAt(0) ?? 0;
    return code >= 32 && code !== 127;
  });
  return keep.join("").trim().slice(0, 64);
}

type FieldKind = "room" | "floor" | "rename";

/**
 * Inline name field: no native dialogs, keyboard or touch.
 *
 * The value lives in the toolbar, not here, because Save has to be able to
 * commit whatever is typed before it writes the house. Blur commits rather
 * than discards: on the touch kiosk the tap that reaches Save blurs this input
 * first, and a field that threw the name away there is how a rename could be
 * typed, saved, and silently lost.
 */
function NameField({
  placeholder,
  selectOnFocus,
  value,
  onValue,
  onCommit,
  onDiscard,
}: {
  placeholder: string;
  selectOnFocus: boolean;
  value: string;
  onValue: (v: string) => void;
  onCommit: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="flex items-center gap-1">
      <input
        autoFocus
        value={value}
        maxLength={64}
        placeholder={placeholder}
        spellCheck={false}
        onChange={(e) => onValue(e.target.value)}
        onFocus={(e) => selectOnFocus && e.currentTarget.select()}
        onBlur={onCommit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            onCommit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            onDiscard();
          }
        }}
        className="w-36 shrink-0 border-b bg-transparent px-1 py-1 text-[11px] font-semibold tracking-wider uppercase outline-none"
        style={{ borderColor: "var(--pink)", color: "var(--text)" }}
      />
      <button
        type="button"
        onMouseDown={(e) => e.preventDefault()}
        onPointerDown={(e) => e.preventDefault()}
        onClick={onCommit}
        className="px-1 py-1 text-[11px] font-semibold tracking-wider uppercase"
        style={{ color: "var(--pink)" }}
      >
        OK
      </button>
      <button
        type="button"
        aria-label="Discard name"
        title="Discard name"
        onMouseDown={(e) => e.preventDefault()}
        onPointerDown={(e) => e.preventDefault()}
        onClick={onDiscard}
        className="px-1 py-1 text-[12px] leading-none font-semibold"
        style={{ color: "var(--text3)" }}
      >
        ×
      </button>
    </div>
  );
}

const LAYERS: { key: LayerKey; label: string; enabled: boolean }[] = [
  { key: "devices", label: "Devices", enabled: true },
  { key: "state", label: "State", enabled: true },
  { key: "traffic", label: "Traffic", enabled: true },
];

function Seg({
  label,
  active,
  disabled,
  hint,
  onClick,
}: {
  label: string;
  active: boolean;
  disabled?: boolean;
  hint?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      title={hint}
      className="rounded-sm px-2.5 py-1 text-[11px] font-semibold tracking-wider uppercase transition-colors disabled:cursor-default"
      style={{
        background: active ? "var(--chip)" : "transparent",
        color: disabled ? "var(--text3)" : active ? "var(--text)" : "var(--text2)",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {label}
      {disabled && hint ? <span className="ml-1 lowercase opacity-70">{hint}</span> : null}
    </button>
  );
}

export default function EditToolbar({
  layer,
  onLayer,
  floors,
  floorId,
  onFloor,
  editMode,
  dirty,
  saving,
  onEdit,
  onSave,
  onCancel,
  onAddRoom,
  onAddFloor,
  onRenameRoom,
  canRename,
  roomName,
  hours,
  onHours,
  renameRequest,
}: {
  layer: LayerKey;
  onLayer: (l: LayerKey) => void;
  floors: HomeFloor[];
  floorId: string;
  onFloor: (id: string) => void;
  editMode: boolean;
  dirty: boolean;
  saving: boolean;
  onEdit: () => void;
  /** Resolves true when the house was written. */
  onSave: () => Promise<boolean>;
  onCancel: () => void;
  onAddRoom: (name: string) => string | null;
  onAddFloor: (name: string) => string | null;
  onRenameRoom: (name: string) => string | null;
  canRename: boolean;
  roomName: string;
  /** Traffic window, in hours. */
  hours: number;
  onHours: (h: number) => void;
  /**
   * Bumped by the sheet when a room name was tapped. A counter rather than a
   * flag so tapping a second room re-opens the field on that one.
   */
  renameRequest: number;
}) {
  const [field, setField] = useState<FieldKind | null>(null);
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [saved, setSaved] = useState(false);

  // Refs because Save reads both inside a click handler that may run after the
  // field's own blur has already queued a state update.
  const fieldRef = useRef<FieldKind | null>(null);
  const valueRef = useRef("");
  const savedTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (savedTimer.current !== null) window.clearTimeout(savedTimer.current);
    },
    [],
  );

  const open = (kind: FieldKind) => {
    const initial = kind === "rename" ? roomName : "";
    setNote("");
    setSaved(false);
    fieldRef.current = kind;
    valueRef.current = initial;
    setField(kind);
    setValue(initial);
  };

  /**
   * The sheet asked for the rename field, because the owner tapped a room's
   * name. Selection and this counter change in the same render, so `roomName`
   * read here is already the tapped room's and the field opens pre-filled.
   */
  useEffect(() => {
    if (renameRequest <= 0 || !editMode || !canRename) return;
    open("rename");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renameRequest]);

  const close = () => {
    fieldRef.current = null;
    valueRef.current = "";
    setNote("");
    setField(null);
    setValue("");
  };

  const setFieldValue = (v: string) => {
    valueRef.current = v;
    setValue(v);
  };

  /**
   * Commit whatever is in the field. True means there is nothing left open:
   * empty closes quietly, unchanged closes quietly, a refusal keeps the field
   * up with the reason so the typed name is never thrown away.
   */
  const commitField = (): boolean => {
    const kind = fieldRef.current;
    if (!kind) return true;
    const name = clean(valueRef.current);
    if (!name || (kind === "rename" && name === clean(roomName))) {
      close();
      return true;
    }
    const fail =
      kind === "room" ? onAddRoom(name) : kind === "floor" ? onAddFloor(name) : onRenameRoom(name);
    if (fail) {
      setNote(fail);
      return false;
    }
    close();
    return true;
  };

  const flashSaved = () => {
    setSaved(true);
    if (savedTimer.current !== null) window.clearTimeout(savedTimer.current);
    savedTimer.current = window.setTimeout(() => setSaved(false), 2000);
  };

  const saveNow = async () => {
    // The open field first: a Save that wrote the house without the name just
    // typed is exactly the bug this order exists to prevent.
    if (!commitField()) return;
    const ok = await onSave();
    if (ok) flashSaved();
  };

  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-3 py-2"
      style={{ borderColor: "var(--line)" }}
    >
      <div className="flex items-center gap-0.5">
        {LAYERS.map((l) => (
          <Seg
            key={l.key}
            label={l.label}
            active={layer === l.key}
            disabled={!l.enabled}
            onClick={() => l.enabled && onLayer(l.key)}
          />
        ))}
      </div>

      {layer === "traffic" && (
        <div className="flex items-center gap-0.5">
          {TRAFFIC_WINDOWS.map((w) => (
            <Seg
              key={w.hours}
              label={w.label}
              active={hours === w.hours}
              onClick={() => onHours(w.hours)}
            />
          ))}
        </div>
      )}

      {floors.length > 1 && (
        <div className="flex items-center gap-0.5">
          {floors.map((f) => (
            <Seg
              key={f.floor_id}
              label={f.name}
              active={floorId === f.floor_id}
              onClick={() => onFloor(f.floor_id)}
            />
          ))}
        </div>
      )}

      {/* Save and Cancel are anchored at the far right and the middle segment
          has a fixed width, so opening the name field cannot shift the button
          out from under a finger already on its way down. */}
      <div className="ml-auto flex items-center gap-1.5">
        {(note || saved) && (
          <span
            className={`max-w-[10rem] truncate text-[10px] tracking-wider ${note ? "lowercase" : ""}`}
            style={{ color: note ? "var(--pink)" : "var(--mint)" }}
          >
            {note || "Saved"}
          </span>
        )}

        <div className="flex w-[15.5rem] shrink-0 items-center justify-end gap-1.5">
          {editMode && field && (
            <NameField
              key={field}
              placeholder={field === "floor" ? "floor name" : "room name"}
              selectOnFocus={field === "rename"}
              value={value}
              onValue={setFieldValue}
              onCommit={commitField}
              onDiscard={close}
            />
          )}
          {editMode && !field && (
            <>
              <Seg label="Add room" active={false} onClick={() => open("room")} />
              <Seg label="Add floor" active={false} onClick={() => open("floor")} />
              <Seg
                label="Rename"
                active={false}
                disabled={!canRename}
                onClick={() => canRename && open("rename")}
              />
            </>
          )}
          {!editMode && <Seg label="Edit plan" active={false} onClick={onEdit} />}
        </div>

        {editMode && (
          <>
            <button
              type="button"
              onPointerDown={(e) => e.preventDefault()}
              onMouseDown={(e) => e.preventDefault()}
              disabled={saving}
              onClick={() => {
                if (saving) return;
                close();
                onCancel();
              }}
              className="shrink-0 rounded-sm px-2.5 py-1 text-[11px] font-semibold tracking-wider uppercase disabled:opacity-40"
              style={{ color: "var(--text2)" }}
            >
              Cancel
            </button>
            <button
              type="button"
              onPointerDown={(e) => e.preventDefault()}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => void saveNow()}
              disabled={saving}
              className="shrink-0 rounded-sm border px-2.5 py-1 text-[11px] font-semibold tracking-wider uppercase disabled:opacity-40"
              style={{
                borderColor: dirty ? "var(--pink)" : "var(--line)",
                color: dirty ? "var(--pink)" : "var(--text2)",
              }}
            >
              {saving ? "Saving" : "Save"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
