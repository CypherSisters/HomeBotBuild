"use client";

/**
 * Egress doors — "what can leave my house", on the screen every refusal
 * message points at, with the switches live.
 *
 * WHY THIS CARD EXISTS
 * --------------------
 * `src/puffpai/security/egress_doors.py` refuses a shut door with a sentence
 * that ends "…turn it on in Settings > Egress." That destination did not
 * exist. Every enumerated-egress claim in the pitch was therefore unverifiable
 * by the person who owns the appliance: the doors were real, the ledger was
 * real, and there was no way to look at either — let alone act.
 *
 * Backend contract:
 *   GET  /system/status               → `egress_doors: []` (door_status())
 *   POST /system/egress/door          {id, open:bool} → {success, door}
 *   GET  /system/egress/ledger?limit= → {success, limit, count, records[]}
 * All three through the /api/pai proxy, which attaches the appliance key and
 * re-checks pairing. Refusals arrive with their status and body intact.
 *
 * THREE RULES THIS CARD DOES NOT BREAK
 * ------------------------------------
 * 1. **No door is hidden.** Including the ones nothing in the registry stops
 *    (`enforced:false`). Omitting them would reproduce, in the UI, the exact
 *    bug the registry was written to make impossible — a contract that does
 *    not enumerate every door is a claim, not a contract.
 * 2. **`enforced:false` is stated in WORDS.** The backend ships
 *    `enforcement_note` as a full sentence for precisely this reason; an owner
 *    reads "nothing in this registry stops this call", not a false-y boolean
 *    in a field beside eleven others.
 * 3. **The SERVER decides which doors are controls. This file does not.**
 *    There is no list of toggleable ids here, and there must not be: the
 *    honest answer changes as call sites are routed through the registry, and
 *    a hardcoded copy would rot into a switch that lies. The appliance now
 *    says so directly — `writable` on each row, which the backend asserts
 *    agrees with what the route accepts door by door — and a 409 remains the
 *    fallback for an appliance too old to publish it. The axes are
 *    independent: `web_search` is `enforced:false` and yet a real control,
 *    `connectivity_probe` is enforced and yet not one, which is exactly why
 *    neither may be inferred from the other.
 *
 * 4. **A door the operator pinned is not a door the owner can move.** Flags
 *    resolve env → overlay → config → default, and only the overlay tier is
 *    writable by this appliance. So a door with `source: "env"` renders
 *    disabled and says who set it: without that, a door the unit file forces
 *    shut is pixel-identical to one the owner shut, and the switch is a lie.
 *
 * DISCLOSURE BEFORE THE REQUEST, NOT AFTER
 * ----------------------------------------
 * A door with a `third_party` sends this house's traffic to someone who is not
 * the site named on the row, for `tunein`, a commercial radio-directory
 * company. Opening one asks first, names the party, and quotes
 * the door's own `data_sent`. Consent that is inferred from a tap on a switch
 * is not consent.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import MsIcon from "../../_components/MsIcon";
import { useAppDispatch, useAppSelector } from "@/app/_stores/hooks";
import { fetchSystemStatus } from "@/app/_stores/systemSlice";
import {
  setEgressDoor,
  getEgressLedger,
} from "@/app/_lib/services/egressService";
import type {
  EgressDoor,
  EgressEvent,
  EgressLedgerRecord,
} from "@/app/_interfaces/api";

const FREDOKA = "var(--font-fredoka), sans-serif";
const NUNITO = "var(--font-nunito), sans-serif";
const MONO = "var(--font-jbmono), monospace";

const cardStyle: React.CSSProperties = {
  borderRadius: 30,
  border: "1px solid var(--line)",
  background: "var(--card)",
  backdropFilter: "blur(18px)",
  WebkitBackdropFilter: "blur(18px)",
  padding: "26px 28px",
  boxShadow: "var(--shadow)",
};

const microLabel: React.CSSProperties = {
  fontFamily: MONO,
  fontWeight: 700,
  fontSize: 11,
  letterSpacing: ".14em",
  textTransform: "uppercase",
  color: "var(--text3)",
};

/** Owner-facing names. Anything not listed falls back to the id, de-snaked. */
const DOOR_LABELS: Record<string, string> = {
  weather: "Weather",
  air_quality: "Air quality",
  news_rss: "News feeds",
  geocode: "Place-name lookup",
  geoip: "Location from IP",
  radio_browser: "Radio station directory",
  tunein: "TuneIn station search",
  web_search: "Web search",
  web_fetch: "Toolsmith page fetch",
  page_reader: "Web page reader",
  tv_free_channels: "Free TV channels",
  connectivity_probe: "Connectivity probe",
  spotify: "Spotify",
};

function doorLabel(door: { id: string }): string {
  return (
    DOOR_LABELS[door.id] ??
    door.id.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())
  );
}

/** "3 Sep, 14:02" from an ISO timestamp; the raw string if unparsable. */
function stampText(ts: string | undefined | null): string | null {
  if (!ts) return null;
  const t = new Date(ts);
  if (Number.isNaN(t.getTime())) return ts;
  return t.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function stamp(event: EgressEvent | null | undefined): string | null {
  return stampText(event?.ts);
}

/** Where this door's value comes from, in the owner's words rather than the
 *  field's. Shown on every row: provenance is the difference between "shut
 *  because you shut it" and "shut because the operator pinned it", and an
 *  owner cannot audit a switch whose history is invisible. */
const SOURCE_LABELS: Record<string, string> = {
  env: "Locked by the operator",
  overlay: "You set this",
  config: "Shipped this way",
  default: "Never set",
};

/** Why this switch cannot move, or undefined when it can.
 *
 *  `writable:false` is the appliance's own answer and is trusted as such.
 *  `source:"env"` is refused separately: the door is writable in principle,
 *  but the environment outranks the tier the API writes, so the write would
 *  land and be inert. Older appliances publish neither field — there we stay
 *  optimistic and let the 409 fallback answer, which is why `undefined` must
 *  never be read as `false`. */
function lockReason(door: EgressDoor): string | undefined {
  if (door.source === "env") {
    return door.env_var
      ? `Set by the operator in the unit file (${door.env_var}). The switch cannot change it.`
      : "Set by the operator in the unit file. The switch cannot change it.";
  }
  if (door.writable === false) {
    return "Not a control: the appliance reports this door cannot be set here.";
  }
  return undefined;
}

type StateChip = { text: string; fg: string; bg: string };

/** The three states an owner has to tell apart, in that order of importance:
 *  a door nothing enforces is neither "open" nor "shut" and must not be
 *  painted as either. */
function stateChip(door: EgressDoor): StateChip {
  if (!door.enforced) {
    return {
      text: "Not enforced",
      fg: "var(--lemon)",
      bg: "rgba(252,217,122,.16)",
    };
  }
  return door.open
    ? { text: "Open", fg: "var(--mint)", bg: "rgba(126,231,178,.16)" }
    : { text: "Shut", fg: "var(--text3)", bg: "var(--chip)" };
}

/* ── The switch ─────────────────────────────────────────────── */

function DoorSwitch({
  door,
  disabled,
  disabledReason,
  busy,
  onToggle,
}: {
  door: EgressDoor;
  disabled: boolean;
  disabledReason?: string;
  busy: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={door.open}
      aria-label={doorLabel(door)}
      aria-disabled={disabled || busy}
      disabled={disabled || busy}
      title={disabledReason}
      onClick={onToggle}
      className="shrink-0"
      style={{
        width: 52,
        height: 30,
        borderRadius: 99,
        background: door.open ? "var(--pink)" : "var(--chip)",
        border: "1px solid var(--line)",
        position: "relative",
        padding: 0,
        opacity: disabled ? 0.4 : busy ? 0.7 : 1,
        cursor: disabled ? "not-allowed" : busy ? "progress" : "pointer",
        transition: "background .2s ease, opacity .2s ease",
      }}
    >
      <span
        style={{
          position: "absolute",
          top: 3,
          left: door.open ? 25 : 3,
          width: 22,
          height: 22,
          borderRadius: "50%",
          background: door.open ? "#fff" : "var(--text3)",
          transition: "left .2s ease, background .2s ease",
        }}
      />
    </button>
  );
}

/* ── One door ───────────────────────────────────────────────── */

function DoorRow({
  door,
  refusedReason,
  errorText,
  busy,
  confirming,
  onToggle,
  onConfirm,
  onCancel,
}: {
  door: EgressDoor;
  refusedReason?: string;
  errorText?: string;
  busy: boolean;
  confirming: boolean;
  onToggle: () => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const chip = stateChip(door);
  const used = stamp(door.last_used);
  const refused = stamp(door.last_refused);
  /* A 409 already collected outranks the pre-emptive rule: it is the same
     server answering, in more words. */
  const blocked = refusedReason ?? lockReason(door);
  const provenance = door.source ? SOURCE_LABELS[door.source] : undefined;

  return (
    <div
      style={{
        padding: "14px 16px",
        borderRadius: 20,
        background: "var(--card2)",
        border: confirming ? "1px solid var(--lemon)" : "1px solid var(--line)",
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              style={{
                fontFamily: NUNITO,
                fontWeight: 700,
                fontSize: 16,
                color: "var(--text)",
              }}
            >
              {doorLabel(door)}
            </span>
            <span
              style={{
                ...microLabel,
                color: chip.fg,
                background: chip.bg,
                padding: "3px 9px",
                borderRadius: 99,
                fontSize: 10,
              }}
            >
              {chip.text}
            </span>
            {provenance && (
              <span
                style={{
                  ...microLabel,
                  fontSize: 10,
                  color:
                    door.source === "env" ? "var(--lemon)" : "var(--text3)",
                }}
              >
                {provenance}
              </span>
            )}
          </div>
          <div
            style={{
              marginTop: 2,
              fontFamily: NUNITO,
              fontWeight: 500,
              fontSize: 14,
              lineHeight: 1.4,
              color: "var(--text2)",
            }}
          >
            {door.purpose}
          </div>
        </div>

        {/* Every door gets a switch. Whether it IS a control is the server's
            answer, learned from a 409 — never a list kept here. */}
        <DoorSwitch
          door={door}
          disabled={!!blocked}
          disabledReason={blocked}
          busy={busy}
          onToggle={onToggle}
        />
      </div>

      {/* Third party — the disclosure that decides consent, so it is never
          folded into the host list. */}
      {door.third_party && (
        <div
          className="mt-2 flex items-start gap-1.5"
          style={{
            fontFamily: NUNITO,
            fontWeight: 600,
            fontSize: 13,
            lineHeight: 1.4,
            color: "var(--lemon)",
          }}
        >
          <MsIcon name="corporate_fare" size={16} style={{ marginTop: 1 }} />
          <span>Third party: {door.third_party}</span>
        </div>
      )}

      {/* Ask before the request, naming the party and quoting the door's own
          data_sent. Opening this is a disclosure, not a preference. */}
      {confirming && (
        <div
          className="mt-2"
          style={{
            padding: "12px 14px",
            borderRadius: 16,
            background: "rgba(252,217,122,.10)",
            border: "1px solid var(--lemon)",
          }}
        >
          <div
            style={{
              fontFamily: NUNITO,
              fontWeight: 700,
              fontSize: 14,
              color: "var(--text)",
            }}
          >
            {door.open ? "Shut this door?" : "Open this door?"}
          </div>
          <div
            style={{
              marginTop: 4,
              fontFamily: NUNITO,
              fontWeight: 500,
              fontSize: 13,
              lineHeight: 1.45,
              color: "var(--text2)",
            }}
          >
            {door.open ? (
              <>Requests to {door.third_party} stop.</>
            ) : (
              <>
                Requests go to {door.third_party}.
                {door.data_sent ? <> What leaves: {door.data_sent}</> : null}
              </>
            )}
          </div>
          <div className="mt-2.5 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onConfirm}
              className="cursor-pointer"
              style={{
                height: 36,
                padding: "0 16px",
                borderRadius: 99,
                border: "none",
                background:
                  "linear-gradient(140deg,var(--pink),var(--magenta))",
                color: "#fff",
                fontFamily: NUNITO,
                fontWeight: 700,
                fontSize: 14,
              }}
            >
              {door.open ? "Shut it" : "Open it"}
            </button>
            <button
              type="button"
              onClick={onCancel}
              className="cursor-pointer"
              style={{
                height: 36,
                padding: "0 16px",
                borderRadius: 99,
                border: "1px solid var(--line)",
                background: "var(--card2)",
                color: "var(--text2)",
                fontFamily: NUNITO,
                fontWeight: 700,
                fontSize: 14,
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* An unenforced door says so in a sentence, not as a boolean. */}
      {!door.enforced && door.enforcement_note && (
        <div
          className="mt-2"
          style={{
            fontFamily: NUNITO,
            fontWeight: 600,
            fontSize: 13,
            lineHeight: 1.4,
            color: "var(--lemon)",
          }}
        >
          {door.enforcement_note}
        </div>
      )}

      {/* The server's own words, verbatim. A refusal the owner cannot read is
          a switch that "just doesn't work". */}
      {(blocked || errorText) && (
        <div
          className="mt-2 flex items-start gap-1.5"
          style={{
            fontFamily: NUNITO,
            fontWeight: 600,
            fontSize: 13,
            lineHeight: 1.4,
            color: blocked ? "var(--text3)" : "var(--pink)",
          }}
        >
          <MsIcon name="info" size={16} style={{ marginTop: 1 }} />
          <span>{blocked || errorText}</span>
        </div>
      )}

      <div
        className="mt-2 flex flex-wrap gap-x-4 gap-y-1"
        style={{
          fontFamily: MONO,
          fontSize: 11,
          lineHeight: 1.5,
          color: "var(--text3)",
          wordBreak: "break-word",
        }}
      >
        <span>
          {door.hosts?.length ? door.hosts.join("  ") : "no fixed hosts"}
        </span>
      </div>

      <div
        className="mt-1 flex flex-wrap gap-x-4"
        style={{ ...microLabel, fontSize: 10 }}
      >
        <span>Last used {used ?? "never"}</span>
        <span style={{ color: refused ? "var(--pink)" : "var(--text3)" }}>
          Last refused {refused ?? "never"}
        </span>
      </div>
    </div>
  );
}

/* ── Recent decisions ───────────────────────────────────────── */

/** What a record says happened, in the owner's words rather than the field's.
 *  A toggle and a request share the ledger and must not read alike. */
function recordVerb(rec: EgressLedgerRecord): { text: string; color: string } {
  if (rec.event === "door_toggled" || rec.kind === "door_toggled") {
    return rec.open
      ? { text: "opened by you", color: "var(--mint)" }
      : { text: "shut by you", color: "var(--text2)" };
  }
  if (rec.result === "refused") return { text: "refused", color: "var(--pink)" };
  if (rec.result === "allowed") return { text: "allowed", color: "var(--mint)" };
  return { text: rec.result || "recorded", color: "var(--text2)" };
}

function LedgerList({ records }: { records: EgressLedgerRecord[] }) {
  // The route returns newest LAST; an owner reads newest first.
  const rows = [...records].reverse();
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((rec, i) => {
        const verb = recordVerb(rec);
        return (
          <div
            key={`${rec.ts}-${rec.door}-${i}`}
            style={{
              padding: "10px 14px",
              borderRadius: 16,
              background: "var(--card2)",
              border: "1px solid var(--line)",
            }}
          >
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span
                style={{
                  fontFamily: NUNITO,
                  fontWeight: 700,
                  fontSize: 14,
                  color: "var(--text)",
                }}
              >
                {doorLabel({ id: rec.door })}
              </span>
              <span
                style={{
                  fontFamily: NUNITO,
                  fontWeight: 700,
                  fontSize: 13,
                  color: verb.color,
                }}
              >
                {verb.text}
              </span>
              <span style={{ ...microLabel, fontSize: 10, marginLeft: "auto" }}>
                {stampText(rec.ts) ?? ""}
              </span>
            </div>
            {rec.purpose && (
              <div
                style={{
                  fontFamily: NUNITO,
                  fontWeight: 500,
                  fontSize: 13,
                  lineHeight: 1.35,
                  color: "var(--text2)",
                }}
              >
                {rec.purpose}
              </div>
            )}
            {rec.third_party && (
              <div
                style={{
                  fontFamily: NUNITO,
                  fontWeight: 600,
                  fontSize: 12,
                  lineHeight: 1.35,
                  color: "var(--lemon)",
                }}
              >
                Third party: {rec.third_party}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ── Card ───────────────────────────────────────────────────── */

const LEDGER_LIMIT = 10;

export default function EgressDoorsCard() {
  const dispatch = useAppDispatch();
  const status = useAppSelector((state) => state.system.status);
  const loading = useAppSelector((state) => state.system.loading);
  const [asked, setAsked] = useState(false);

  /* Doors the server refused with a 409: not a control, or an environment
     variable is forcing them. Keyed by id, holding the server's own sentence.
     Session-scoped on purpose — it is the server's answer to remember, not a
     preference to persist, and it must be re-learned after a deploy that makes
     a door toggleable. */
  const [refused, setRefused] = useState<Record<string, string>>({});
  /* Transient failures (400/429/500) — cleared on the next attempt. */
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [ledger, setLedger] = useState<EgressLedgerRecord[] | null>(null);
  const [ledgerFailed, setLedgerFailed] = useState(false);

  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  /* The settings page already dispatches this on mount; ask once more only if
     nothing has landed, so the card works when it is mounted anywhere else. */
  useEffect(() => {
    if (!status && !asked) {
      setAsked(true);
      dispatch(fetchSystemStatus());
    }
  }, [status, asked, dispatch]);

  const loadLedger = useCallback(async () => {
    try {
      const recs = await getEgressLedger(LEDGER_LIMIT);
      if (!alive.current) return;
      setLedger(recs);
      setLedgerFailed(false);
    } catch {
      if (!alive.current) return;
      setLedgerFailed(true);
    }
  }, []);

  useEffect(() => {
    void loadLedger();
  }, [loadLedger]);

  const send = useCallback(
    async (door: EgressDoor, next: boolean) => {
      setConfirmId(null);
      setBusyId(door.id);
      setErrors((e) => {
        const copy = { ...e };
        delete copy[door.id];
        return copy;
      });

      const res = await setEgressDoor(door.id, next);

      if (!alive.current) return;
      if (res.ok) {
        // Re-read the registry rather than trusting the optimistic value —
        // the backend re-reads it too, after the write.
        await dispatch(fetchSystemStatus());
        await loadLedger();
      } else if (res.status === 409) {
        // Not a control (or an env var is forcing it). The server's sentence
        // becomes this switch's reason for the rest of the session.
        setRefused((r) => ({ ...r, [door.id]: res.error }));
      } else {
        setErrors((e) => ({ ...e, [door.id]: res.error }));
      }
      if (alive.current) setBusyId(null);
    },
    [dispatch, loadLedger]
  );

  const onToggle = useCallback(
    (door: EgressDoor) => {
      if (refused[door.id] || lockReason(door) || busyId) return;
      // A named third party gets asked about before anything is sent.
      if (door.third_party) {
        setConfirmId((cur) => (cur === door.id ? null : door.id));
        return;
      }
      void send(door, !door.open);
    },
    [refused, busyId, send]
  );

  const doors = status?.egress_doors;
  const openCount = doors?.filter((d) => d.enforced && d.open).length ?? 0;
  const unenforced = doors?.filter((d) => !d.enforced).length ?? 0;

  return (
    <div id="egress-doors" style={cardStyle} className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between gap-3">
        <div
          style={{
            fontFamily: FREDOKA,
            fontWeight: 600,
            fontSize: 24,
            color: "var(--text)",
          }}
        >
          Egress doors
        </div>
        {doors && doors.length > 0 && (
          <div style={microLabel}>
            {openCount} open / {doors.length}
            {unenforced > 0 ? ` · ${unenforced} unenforced` : ""}
          </div>
        )}
      </div>

      <div
        style={{
          fontFamily: NUNITO,
          fontWeight: 500,
          fontSize: 15,
          lineHeight: 1.45,
          color: "var(--text2)",
        }}
      >
        Every way out of this house, listed — including the ones nothing here
        stops. Shut is the default; a shut door is refused before a socket
        opens.
      </div>

      {!doors && (
        <div
          style={{
            padding: "16px 18px",
            borderRadius: 20,
            background: "var(--card2)",
            border: "1px solid var(--line)",
            fontFamily: NUNITO,
            fontWeight: 500,
            fontSize: 15,
            color: "var(--text3)",
          }}
        >
          {loading
            ? "Reading the door registry…"
            : "Door registry unavailable — the appliance did not report egress_doors."}
        </div>
      )}

      {doors?.map((door) => (
        <DoorRow
          key={door.id}
          door={door}
          refusedReason={refused[door.id]}
          errorText={errors[door.id]}
          busy={busyId === door.id}
          confirming={confirmId === door.id}
          onToggle={() => onToggle(door)}
          onConfirm={() => void send(door, !door.open)}
          onCancel={() => setConfirmId(null)}
        />
      ))}

      {doors && doors.length > 0 && (
        <>
          <div style={{ ...microLabel, margin: "6px 2px 0" }}>
            Recent decisions
          </div>
          {ledger && ledger.length > 0 ? (
            <LedgerList records={ledger} />
          ) : (
            <div
              style={{
                fontFamily: NUNITO,
                fontWeight: 500,
                fontSize: 14,
                color: "var(--text3)",
              }}
            >
              {ledgerFailed
                ? "The decision log could not be read."
                : "Nothing recorded yet."}
            </div>
          )}
          <div
            style={{
              fontFamily: NUNITO,
              fontWeight: 600,
              fontSize: 13,
              lineHeight: 1.45,
              color: "var(--text3)",
            }}
          >
            Your switches are kept on the box and survive a restart. Every
            decision is recorded in security_logs/ — the last {LEDGER_LIMIT} are
            shown. Opening a door takes effect at once; nothing is sent until
            something asks.
          </div>
        </>
      )}
    </div>
  );
}
