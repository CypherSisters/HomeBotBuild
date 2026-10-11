"use client";

/**
 * The rail: what is selected, and the one control that matters for it.
 *
 * Deliberately not a device card. Label, value, control, then the secondary
 * entities folded away. Sliders commit on release rather than on every input
 * event, because a brightness drag would otherwise fire thirty service calls
 * at the bridge on the way from 10% to 90%.
 *
 * A refusal from the backend is printed verbatim under the control. The owner
 * needs the bridge's own words, not this component's guess at them.
 */

import { useEffect, useState } from "react";
import { mdiClose, mdiMinus, mdiPlus, mdiPause, mdiPlay, mdiSkipNext, mdiSkipPrevious } from "@mdi/js";
import Icon from "../../../_components/Icon";
import type {
  ControlIntent,
  HomeDevice,
  HomeLayoutRoom,
  LanOnlyRow,
  TrafficRow,
} from "../../../_lib/services/homeService";
import { TrafficDetail } from "./TrafficOverlay";
import type { HaTicketProposal } from "../../../_lib/services/haConfirmService";
import { actionSentence, stakeLines } from "../../../_lib/services/haConfirmService";
import { TIER_LABEL, TIER_TONE } from "./glyphs";
import {
  brightnessPct,
  isUnavailable,
  litLightEntities,
  primaryEntity,
  stateOf,
} from "./view";

/* ── small parts ─────────────────────────────────────────────────────── */

function Label({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="text-[10px] font-semibold tracking-[0.16em] uppercase"
      style={{ color: "var(--text3)" }}
    >
      {children}
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span
        className="text-[10px] font-semibold tracking-[0.12em] uppercase"
        style={{ color: "var(--text3)" }}
      >
        {k}
      </span>
      <span className="truncate text-right text-xs" style={{ color: "var(--text2)" }}>
        {v}
      </span>
    </div>
  );
}

function Mono({ children }: { children: React.ReactNode }) {
  return (
    <span className="font-mono text-[11px]" style={{ color: "var(--text2)" }}>
      {children}
    </span>
  );
}

function ActionButton({
  label,
  onClick,
  disabled,
  tone,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  tone?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-sm border px-3 py-1.5 text-[11px] font-semibold tracking-wider uppercase transition-colors disabled:opacity-40"
      style={{
        borderColor: tone ?? "var(--line)",
        color: tone ?? "var(--text)",
        background: "var(--chip)",
      }}
    >
      {label}
    </button>
  );
}

/** Slider that reports only on release. */
function CommitSlider({
  value,
  min,
  max,
  onCommit,
  disabled,
}: {
  value: number;
  min: number;
  max: number;
  onCommit: (v: number) => void;
  disabled?: boolean;
}) {
  const [local, setLocal] = useState(value);
  const [dragging, setDragging] = useState(false);
  useEffect(() => {
    if (!dragging) setLocal(value);
  }, [value, dragging]);
  return (
    <div className="flex items-center gap-2">
      <input
        type="range"
        min={min}
        max={max}
        value={local}
        disabled={disabled}
        onChange={(e) => {
          setDragging(true);
          setLocal(Number(e.target.value));
        }}
        onPointerUp={() => {
          setDragging(false);
          onCommit(local);
        }}
        onKeyUp={() => {
          setDragging(false);
          onCommit(local);
        }}
        onBlur={() => setDragging(false)}
        className="h-1 flex-1 accent-[var(--pink)]"
      />
      <span className="w-9 text-right font-mono text-[11px]" style={{ color: "var(--text2)" }}>
        {local}
      </span>
    </div>
  );
}

/* ── confirm sheet ───────────────────────────────────────────────────── */

export function ConfirmSheet({
  proposal,
  busy,
  onConfirm,
  onCancel,
}: {
  proposal: HaTicketProposal;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const stakes = stakeLines(proposal.safety_critical_domains, proposal.action);
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-4 sm:items-center">
      <div
        className="w-full max-w-md rounded border p-4"
        style={{ borderColor: "var(--line)", background: "var(--puff-surface-deep)" }}
      >
        <Label>Confirm</Label>
        <p className="mt-2 text-sm font-semibold" style={{ color: "var(--text)" }}>
          {actionSentence(proposal)}
        </p>
        {stakes.map((s) => (
          <p key={s} className="mt-2 text-xs" style={{ color: "var(--text2)" }}>
            {s}
          </p>
        ))}
        <div className="mt-3 space-y-0.5">
          <Row k="Target" v={<Mono>{proposal.entity_id ?? "none"}</Mono>} />
          <Row k="Action" v={<Mono>{proposal.action}</Mono>} />
          <Row k="Domains" v={<Mono>{(proposal.domains ?? []).join(" ") || "none"}</Mono>} />
        </div>
        {proposal.note && (
          <p className="mt-2 text-xs" style={{ color: "var(--text3)" }}>
            {proposal.note}
          </p>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <ActionButton label="Cancel" onClick={onCancel} />
          <ActionButton label={busy ? "Working" : "Do it"} onClick={onConfirm} disabled={busy} tone="var(--pink)" />
        </div>
      </div>
    </div>
  );
}

/* ── inspector ───────────────────────────────────────────────────────── */

/** Collapsed by default: traffic is context, not the reason you tapped. */
function TrafficSection({ row }: { row: TrafficRow }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-3 border-t pt-2" style={{ borderColor: "var(--line)" }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-baseline justify-between gap-2 text-[10px] font-semibold tracking-[0.16em] uppercase"
        style={{ color: "var(--text3)" }}
      >
        <span>Traffic</span>
        <span className="font-mono tracking-normal normal-case">
          {row.total} {open ? "−" : "+"}
        </span>
      </button>
      {open && (
        <div className="mt-2">
          <TrafficDetail row={row} />
        </div>
      )}
    </div>
  );
}

export default function Inspector({
  device,
  room,
  roomDevices,
  areaName,
  busy,
  error,
  note,
  trafficRow,
  lanRow,
  trafficKnown,
  onControl,
  onLightsOff,
  onClose,
}: {
  device: HomeDevice | null;
  room: HomeLayoutRoom | null;
  roomDevices: HomeDevice[];
  areaName: (areaId: string | null) => string;
  busy: boolean;
  error: string;
  note: string;
  /** Traffic for the selected device, when there is a reading for it. */
  trafficRow: TrafficRow | null;
  /** Set instead of `device` when a LAN-only row is selected. */
  lanRow: LanOnlyRow | null;
  /** A traffic reading exists at all, so "no LAN match" means something. */
  trafficKnown: boolean;
  onControl: (intent: ControlIntent) => void;
  onLightsOff: () => void;
  onClose: () => void;
}) {
  const [showAll, setShowAll] = useState(false);

  if (lanRow) {
    return (
      <div className="p-4">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Label>On the LAN</Label>
            <h3 className="truncate text-base font-semibold" style={{ color: "var(--text)" }}>
              {lanRow.display_name}
            </h3>
          </div>
          <button type="button" onClick={onClose} style={{ color: "var(--text3)" }} aria-label="Close">
            <Icon path={mdiClose} size="16" />
          </button>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {(lanRow.is_gateway || lanRow.is_self) && (
            <span
              className="rounded-sm px-1.5 py-px text-[10px] font-semibold tracking-wider uppercase"
              style={{ background: "var(--chip)", color: "var(--lav)" }}
            >
              {lanRow.is_gateway ? "Gateway" : "This box"}
            </span>
          )}
          <span
            className="rounded-sm px-1.5 py-px text-[10px] font-semibold tracking-wider uppercase"
            style={{ background: "var(--chip)", color: "var(--text2)" }}
          >
            {lanRow.category || "unknown"}
          </span>
        </div>
        <div className="mt-3 space-y-0.5 border-t pt-2" style={{ borderColor: "var(--line)" }}>
          <Row k="Make" v={lanRow.manufacturer || "unknown"} />
          <Row k="Host" v={<Mono>{lanRow.hostname || "none"}</Mono>} />
          <Row k="Address" v={<Mono>{lanRow.ip}</Mono>} />
        </div>
        <div className="mt-3 border-t pt-2" style={{ borderColor: "var(--line)" }}>
          <Label>Traffic</Label>
          <div className="mt-2">
            <TrafficDetail row={lanRow} />
          </div>
          <p className="mt-2 text-xs" style={{ color: "var(--text3)" }}>
            This address is not one of the house&apos;s devices.
          </p>
        </div>
      </div>
    );
  }

  if (!device && !room) {
    return (
      <div className="p-4">
        <Label>Inspector</Label>
        <p className="mt-2 text-xs" style={{ color: "var(--text3)" }}>
          Pick a room or a device on the sheet.
        </p>
      </div>
    );
  }

  if (device) {
    const ent = primaryEntity(device);
    const dead = isUnavailable(device);
    const state = stateOf(device);
    const attrs = ent?.attributes ?? {};
    const eid = ent?.entity_id ?? "";
    const secondary = device.entities.filter((e) => e.entity_id !== eid);

    return (
      <div className="p-4">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Label>Device</Label>
            <h3 className="truncate text-base font-semibold" style={{ color: "var(--text)" }}>
              {device.name}
            </h3>
          </div>
          <button type="button" onClick={onClose} style={{ color: "var(--text3)" }} aria-label="Close">
            <Icon path={mdiClose} size="16" />
          </button>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span
            className="rounded-sm px-1.5 py-px text-[10px] font-semibold tracking-wider uppercase"
            style={{ background: "var(--chip)", color: "var(--text2)" }}
          >
            {device.integration || "unknown"}
          </span>
          <span
            className="rounded-sm px-1.5 py-px text-[10px] font-semibold tracking-wider uppercase"
            style={{ background: "var(--chip)", color: TIER_TONE[device.tier] }}
          >
            {TIER_LABEL[device.tier]}
          </span>
          {device.bridge_key && (
            <span
              className="rounded-sm px-1.5 py-px font-mono text-[10px]"
              style={{ background: "var(--chip)", color: "var(--text3)" }}
            >
              {device.bridge_key}
            </span>
          )}
        </div>

        <div className="mt-3 space-y-0.5 border-t pt-2" style={{ borderColor: "var(--line)" }}>
          <Row k="Room" v={areaName(device.area_id)} />
          <Row k="State" v={<Mono>{state}</Mono>} />
          <Row k="Entity" v={<Mono>{eid || "none"}</Mono>} />
          {(device.manufacturer || device.model) && (
            <Row k="Make" v={`${device.manufacturer} ${device.model}`.trim()} />
          )}
        </div>

        {/* Primary control. */}
        <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--line)" }}>
          <Label>Control</Label>
          {dead && (
            <p className="mt-2 text-xs" style={{ color: "var(--text3)" }}>
              This device is not answering.
            </p>
          )}

          {!dead && ent && (device.kind === "light" || device.kind === "switch") && (
            <div className="mt-2 space-y-3">
              <div className="flex gap-2">
                <ActionButton
                  label="On"
                  disabled={busy}
                  tone={state === "on" ? "var(--lemon)" : undefined}
                  onClick={() => onControl({ kind: "toggle", entityId: eid, action: "turn_on" })}
                />
                <ActionButton
                  label="Off"
                  disabled={busy}
                  tone={state === "off" ? "var(--text2)" : undefined}
                  onClick={() => onControl({ kind: "toggle", entityId: eid, action: "turn_off" })}
                />
              </div>
              {device.kind === "light" && state === "on" && (
                <div>
                  <Label>Brightness</Label>
                  <CommitSlider
                    value={brightnessPct(device)}
                    min={1}
                    max={100}
                    disabled={busy}
                    onCommit={(pct) => onControl({ kind: "brightness", entityId: eid, pct })}
                  />
                </div>
              )}
            </div>
          )}

          {!dead && ent && device.kind === "climate" && (
            <div className="mt-2 space-y-3">
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    onControl({
                      kind: "climate",
                      entityId: eid,
                      temperature: Number(attrs.temperature ?? 68) - 1,
                    })
                  }
                  className="rounded-sm border p-1.5 disabled:opacity-40"
                  style={{ borderColor: "var(--line)", color: "var(--text)" }}
                  aria-label="Cooler"
                >
                  <Icon path={mdiMinus} size="14" />
                </button>
                <span className="font-mono text-2xl" style={{ color: "var(--text)" }}>
                  {attrs.temperature ?? "--"}
                </span>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    onControl({
                      kind: "climate",
                      entityId: eid,
                      temperature: Number(attrs.temperature ?? 68) + 1,
                    })
                  }
                  className="rounded-sm border p-1.5 disabled:opacity-40"
                  style={{ borderColor: "var(--line)", color: "var(--text)" }}
                  aria-label="Warmer"
                >
                  <Icon path={mdiPlus} size="14" />
                </button>
                <span className="font-mono text-[11px]" style={{ color: "var(--text3)" }}>
                  now {attrs.current_temperature ?? "--"}
                </span>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {((attrs.hvac_modes as string[]) ?? ["off", "heat", "cool"]).map((m) => (
                  <ActionButton
                    key={m}
                    label={m}
                    disabled={busy}
                    tone={state === m ? "var(--pink)" : undefined}
                    onClick={() => onControl({ kind: "climate", entityId: eid, hvac_mode: m })}
                  />
                ))}
              </div>
            </div>
          )}

          {!dead && ent && device.kind === "media" && (
            <div className="mt-2 space-y-3">
              <div className="flex items-center gap-2">
                {[
                  { icon: mdiSkipPrevious, service: "media_previous_track", label: "Previous" },
                  { icon: mdiPlay, service: "media_play", label: "Play" },
                  { icon: mdiPause, service: "media_pause", label: "Pause" },
                  { icon: mdiSkipNext, service: "media_next_track", label: "Next" },
                ].map((b) => (
                  <button
                    key={b.service}
                    type="button"
                    disabled={busy}
                    aria-label={b.label}
                    onClick={() =>
                      onControl({
                        kind: "service",
                        entityId: eid,
                        domain: "media_player",
                        service: b.service,
                      })
                    }
                    className="rounded-sm border p-1.5 disabled:opacity-40"
                    style={{ borderColor: "var(--line)", color: "var(--text)" }}
                  >
                    <Icon path={b.icon} size="14" />
                  </button>
                ))}
              </div>
              <div>
                <Label>Volume</Label>
                <CommitSlider
                  value={Math.round(Number(attrs.volume_level ?? 0) * 100)}
                  min={0}
                  max={100}
                  disabled={busy}
                  onCommit={(v) =>
                    onControl({
                      kind: "service",
                      entityId: eid,
                      domain: "media_player",
                      service: "volume_set",
                      data: { volume_level: v / 100 },
                    })
                  }
                />
              </div>
              {attrs.media_title && <Row k="Playing" v={String(attrs.media_title)} />}
            </div>
          )}

          {!dead && ent && (device.kind === "lock" || device.kind === "cover") && (
            <div className="mt-2 flex gap-2">
              {/* HA has no lock.turn_on / cover.turn_off: these domains only
                  answer to their own verbs. The pink button is the one that
                  opens the house up (unlock, open). */}
              <ActionButton
                label={device.kind === "lock" ? "Lock" : "Close"}
                disabled={busy}
                onClick={() =>
                  onControl({
                    kind: "service",
                    domain: device.kind,
                    service: device.kind === "lock" ? "lock" : "close_cover",
                    entityId: eid,
                  })
                }
              />
              <ActionButton
                label={device.kind === "lock" ? "Unlock" : "Open"}
                disabled={busy}
                tone="var(--pink)"
                onClick={() =>
                  onControl({
                    kind: "service",
                    domain: device.kind,
                    service: device.kind === "lock" ? "unlock" : "open_cover",
                    entityId: eid,
                  })
                }
              />
            </div>
          )}

          {!dead &&
            ent &&
            !["light", "switch", "climate", "media", "lock", "cover"].includes(device.kind) && (
              <p className="mt-2 text-xs" style={{ color: "var(--text3)" }}>
                Read only.
              </p>
            )}

          {error && (
            <p className="mt-2 text-xs" style={{ color: "var(--puff-red)" }}>
              {error}
            </p>
          )}
          {note && !error && (
            <p className="mt-2 text-xs" style={{ color: "var(--text3)" }}>
              {note}
            </p>
          )}
        </div>

        {trafficRow && <TrafficSection row={trafficRow} />}
        {trafficKnown && !trafficRow && !device.bridge_key && (
          <p className="mt-3 border-t pt-2 text-[11px]" style={{ borderColor: "var(--line)", color: "var(--text3)" }}>
            No LAN match, so there is nothing to watch leave it.
          </p>
        )}

        {secondary.length > 0 && (
          <div className="mt-3 border-t pt-2" style={{ borderColor: "var(--line)" }}>
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              className="text-[10px] font-semibold tracking-[0.16em] uppercase"
              style={{ color: "var(--text3)" }}
            >
              {showAll ? "Hide" : "Show"} {secondary.length} more {secondary.length === 1 ? "entity" : "entities"}
            </button>
            {showAll && (
              <div className="mt-2 space-y-1">
                {secondary.map((e) => (
                  <div key={e.entity_id} className="flex items-baseline justify-between gap-2">
                    <Mono>{e.entity_id}</Mono>
                    <span className="font-mono text-[11px]" style={{ color: "var(--text)" }}>
                      {e.state}
                      {e.attributes?.unit_of_measurement ? ` ${e.attributes.unit_of_measurement}` : ""}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  /* Room summary. */
  const lit = litLightEntities(roomDevices);
  const lights = roomDevices.filter((d) => d.kind === "light");
  const dead = roomDevices.filter(isUnavailable);
  return (
    <div className="p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <Label>Room</Label>
          <h3 className="truncate text-base font-semibold" style={{ color: "var(--text)" }}>
            {room?.name}
          </h3>
        </div>
        <button type="button" onClick={onClose} style={{ color: "var(--text3)" }} aria-label="Close">
          <Icon path={mdiClose} size="16" />
        </button>
      </div>
      <div className="mt-3 space-y-0.5 border-t pt-2" style={{ borderColor: "var(--line)" }}>
        <Row k="Devices" v={<Mono>{roomDevices.length}</Mono>} />
        <Row k="Lights on" v={<Mono>{`${lit.length} / ${lights.length}`}</Mono>} />
        <Row k="Not answering" v={<Mono>{dead.length}</Mono>} />
        <Row k="Area id" v={<Mono>{room?.area_id}</Mono>} />
      </div>
      <div className="mt-3">
        <ActionButton
          label={busy ? "Working" : `Lights off (${lit.length})`}
          disabled={busy || lit.length === 0}
          onClick={onLightsOff}
        />
        <p className="mt-2 text-xs" style={{ color: "var(--text3)" }}>
          One call per light. The bridge refuses whole-area commands.
        </p>
        {error && (
          <p className="mt-2 text-xs" style={{ color: "var(--puff-red)" }}>
            {error}
          </p>
        )}
        {note && !error && (
          <p className="mt-2 text-xs" style={{ color: "var(--text3)" }}>
            {note}
          </p>
        )}
      </div>
    </div>
  );
}
