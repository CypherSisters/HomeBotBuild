"use client";

/**
 * The build engine's own outside services, on the Bridge switches card.
 *
 * WHY THIS EXISTS
 * ---------------
 * Finding and installing apps, books and pictures uses switches of its own
 * (Code downloads, Code search, Picture search, the book library, the skill
 * shelves...), kept in `outside_switches.json` and read by the engine before
 * any connection. They all ship off. A refusal tells the owner "go to
 * Settings, Bridge switches, Code downloads", so they are listed here, under
 * the short name the backend gives each (`switch_name`), with an anchor
 * (`#switch-<id>`) a link can open.
 *
 * Backend: GET /outside (src/puffpai/outside_services.page). Only rows of kind
 * `as_built` (the engine's switches) and `mcp` (a tool server the owner added)
 * are shown: the built-in services and the apps' own sites are the registry
 * switches above this list (a row whose id is one of those is left out, never
 * shown twice).
 *
 * THE RULES, SAME AS THE BACKEND'S
 * --------------------------------
 * - OFF is one tap and asks nothing.
 * - ON shows the backend's own sentence (what leaves, and to which websites)
 *   and a one-time yes for this paired screen; the owner's tap on "Turn it on"
 *   spends it. After setup the backend also wants the unlocked Vault, and
 *   vaultFetch asks for the PIN.
 * - No sentence about a service is written here; every line is the backend's.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  askToSwitchOn,
  getOutsideServices,
  switchOutsideOff,
  switchOutsideOn,
  type OutsideService,
} from "@/app/_lib/services/outsideService";

const NUNITO = "var(--font-nunito), sans-serif";
const MONO = "var(--font-jbmono), monospace";

const microLabel: React.CSSProperties = {
  fontFamily: MONO,
  fontWeight: 700,
  fontSize: 11,
  letterSpacing: ".14em",
  textTransform: "uppercase",
  color: "var(--text3)",
};

const small: React.CSSProperties = {
  fontFamily: NUNITO,
  fontWeight: 500,
  fontSize: 13,
  lineHeight: 1.45,
  color: "var(--text2)",
};

/** The anchor a link to one switch opens: /dashboard/settings#switch-code_download. */
export function switchAnchor(id: string): string {
  return `switch-${id}`;
}

type Asking = { id: string; sentence: string; ticket: string };

/** The engine's own switches, and the tool servers the owner added. */
const SHOWN_KINDS: ReadonlySet<string> = new Set(["as_built", "mcp"]);

function Row({
  svc,
  busy,
  asking,
  error,
  onToggle,
  onYes,
  onCancel,
}: {
  svc: OutsideService;
  busy: boolean;
  asking: Asking | null;
  error?: string;
  onToggle: () => void;
  onYes: () => void;
  onCancel: () => void;
}) {
  const on = svc.state === "on";
  const label = svc.switch_name || svc.name;
  const blocked = svc.can_switch ? "" : svc.why_cannot_switch;
  return (
    <div
      id={switchAnchor(svc.id)}
      style={{
        padding: "14px 16px",
        borderRadius: 20,
        background: "var(--card2)",
        border: asking ? "1px solid var(--lemon)" : "1px solid var(--line)",
        scrollMarginTop: 80,
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span style={{ fontFamily: NUNITO, fontWeight: 700, fontSize: 16, color: "var(--text)" }}>
              {label}
            </span>
            <span
              style={{
                ...microLabel,
                fontSize: 10,
                padding: "3px 9px",
                borderRadius: 99,
                color: on ? "var(--mint)" : "var(--text3)",
                background: on ? "rgba(126,231,178,.16)" : "var(--chip)",
              }}
            >
              {on ? "On" : "Off"}
            </span>
          </div>
          <div style={{ ...small, fontSize: 14, marginTop: 2 }}>{svc.for}</div>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label={label}
          aria-disabled={!!blocked || busy}
          disabled={!!blocked || busy}
          title={blocked || undefined}
          onClick={onToggle}
          className="shrink-0"
          style={{
            width: 52,
            height: 30,
            borderRadius: 99,
            background: on ? "var(--pink)" : "var(--chip)",
            border: "1px solid var(--line)",
            position: "relative",
            padding: 0,
            opacity: blocked ? 0.4 : busy ? 0.7 : 1,
            cursor: blocked ? "not-allowed" : busy ? "progress" : "pointer",
          }}
        >
          <span
            style={{
              position: "absolute",
              top: 3,
              left: on ? 25 : 3,
              width: 22,
              height: 22,
              borderRadius: "50%",
              background: on ? "#fff" : "var(--text3)",
              transition: "left .2s ease",
            }}
          />
        </button>
      </div>

      {svc.what_leaves && (
        <div style={{ ...small, marginTop: 6 }}>What leaves: {svc.what_leaves}</div>
      )}

      {asking && (
        <div
          className="mt-2"
          style={{
            padding: "12px 14px",
            borderRadius: 16,
            background: "rgba(252,217,122,.10)",
            border: "1px solid var(--lemon)",
          }}
        >
          <div style={{ fontFamily: NUNITO, fontWeight: 700, fontSize: 14, color: "var(--text)" }}>
            Turn this switch on?
          </div>
          <div style={{ ...small, marginTop: 4 }}>{asking.sentence}</div>
          <div className="mt-2.5 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onYes}
              disabled={busy}
              className="cursor-pointer"
              style={{
                height: 36,
                padding: "0 16px",
                borderRadius: 99,
                border: "none",
                background: "linear-gradient(140deg,var(--pink),var(--magenta))",
                color: "#fff",
                fontFamily: NUNITO,
                fontWeight: 700,
                fontSize: 14,
              }}
            >
              Turn it on
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

      {(blocked || error || svc.state_line) && (
        <div
          className="mt-2"
          style={{
            ...small,
            fontWeight: 600,
            color: error ? "var(--pink)" : "var(--text3)",
          }}
        >
          {error || blocked || svc.state_line}
        </div>
      )}

      <div
        className="mt-2"
        style={{ fontFamily: MONO, fontSize: 11, lineHeight: 1.5, color: "var(--text3)", wordBreak: "break-word" }}
      >
        {svc.websites.length ? svc.websites.join("  ") : "no fixed websites"}
      </div>
    </div>
  );
}

export default function EngineSwitches({ shownAbove }: { shownAbove: string[] }) {
  const [services, setServices] = useState<OutsideService[] | null>(null);
  const [problem, setProblem] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [asking, setAsking] = useState<Asking | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const alive = useRef(true);
  const jumped = useRef(false);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    const got = await getOutsideServices();
    if (!alive.current) return;
    if (got.ok) {
      setServices(got.services);
      setProblem("");
    } else {
      setServices(null);
      setProblem(got.message);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const above = new Set(shownAbove);
  const rows = (services ?? []).filter((s) => SHOWN_KINDS.has(s.kind) && !above.has(s.id));

  // A link to one switch (#switch-code_download) opens at that switch once it is listed.
  useEffect(() => {
    if (jumped.current || rows.length === 0 || typeof window === "undefined") return;
    const hash = window.location.hash.replace(/^#/, "");
    if (!hash.startsWith("switch-")) return;
    jumped.current = true;
    document.getElementById(hash)?.scrollIntoView({ block: "center" });
  }, [rows.length]);

  const setError = (id: string, message: string) =>
    setErrors((e) => {
      const copy = { ...e };
      if (message) copy[id] = message;
      else delete copy[id];
      return copy;
    });

  const onToggle = async (svc: OutsideService) => {
    if (busyId || !svc.can_switch) return;
    setError(svc.id, "");
    setBusyId(svc.id);
    if (svc.state === "on") {
      setAsking(null);
      const got = await switchOutsideOff(svc.id);
      if (!alive.current) return;
      if (!got.ok) setError(svc.id, got.message);
      await load();
    } else if (asking?.id === svc.id) {
      setAsking(null);
    } else {
      const got = await askToSwitchOn(svc.id);
      if (!alive.current) return;
      if (got.ok) setAsking({ id: svc.id, sentence: got.sentence, ticket: got.ticket });
      else setError(svc.id, got.message);
    }
    if (alive.current) setBusyId(null);
  };

  const onYes = async () => {
    if (!asking || busyId) return;
    const { id, ticket } = asking;
    setBusyId(id);
    const got = await switchOutsideOn(id, ticket);
    if (!alive.current) return;
    setAsking(null);
    if (!got.ok) setError(id, got.message);
    await load();
    if (alive.current) setBusyId(null);
  };

  if (services === null && !problem) return null;

  return (
    <div className="flex flex-col gap-3">
      <div style={{ ...microLabel, margin: "6px 2px 0" }}>Finding and downloading</div>
      <div style={{ ...small, fontSize: 14 }}>
        What your bot uses to look for and download apps, code, books and pictures, and the tool
        servers you added. Each one is off until you switch it on, and switching one on shows what
        leaves first.
      </div>
      {problem ? (
        <div style={{ ...small, color: "var(--text3)" }}>{problem}</div>
      ) : (
        rows.map((svc) => (
          <Row
            key={svc.id}
            svc={svc}
            busy={busyId === svc.id}
            asking={asking?.id === svc.id ? asking : null}
            error={errors[svc.id]}
            onToggle={() => void onToggle(svc)}
            onYes={() => void onYes()}
            onCancel={() => setAsking(null)}
          />
        ))
      )}
    </div>
  );
}
