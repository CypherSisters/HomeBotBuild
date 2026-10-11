"use client";

/**
 * SkillsCapabilityCard — says what this box can and cannot extend itself with.
 *
 * WHY IT EXISTS
 * -------------
 * Skills are code PuffPAI runs for itself, and they only run inside a Linux
 * sandbox (bubblewrap). A Windows box, or a Linux box without bubblewrap, cannot
 * run one at all. The dashboard must not offer a propose/approve flow on a box
 * where an approved skill would never execute: that is a consent prompt for
 * something that cannot happen, and the household would be agreeing to nothing.
 *
 * So this reads GET /capabilities/extend — `skills.sandbox_available`,
 * `skills.enabled`, `workspace_exec.available` — and states the answer in one
 * line. There is deliberately NO install or propose control here: this build has
 * no skills UI, and the card's job is to prevent one appearing that promises more
 * than the box can do. If a propose flow is ever added, it goes behind
 * `sandbox_available === true` and nowhere else.
 *
 * A 404 means the endpoint is not in this build, which is UNKNOWN, not false. The
 * card says it does not know rather than inventing either answer.
 */

import { useEffect, useState } from "react";
import MsIcon from "../../_components/MsIcon";

const API_BASE = "/api/pai";

interface ExtendCapabilities {
  skills?: {
    /** Whether a Linux sandbox (bubblewrap) is present and usable. */
    sandbox_available?: boolean;
    /** Whether the owner has switched skills on at all. */
    enabled?: boolean;
    /** The box own explanation. Preferred over any wording invented here. */
    reason?: string;
  };
  workspace_exec?: {
    available?: boolean;
    reason?: string;
  };
}

type Load =
  | { state: "loading" }
  | { state: "ready"; caps: ExtendCapabilities }
  /** Endpoint absent (404) or unreachable: the honest answer is "unknown". */
  | { state: "unknown" };

const cardStyle: React.CSSProperties = {
  borderRadius: 30,
  border: "1px solid var(--line)",
  background: "var(--card)",
  backdropFilter: "blur(18px)",
  WebkitBackdropFilter: "blur(18px)",
  padding: "26px 28px",
  boxShadow: "var(--shadow)",
};

const bodyText: React.CSSProperties = {
  fontFamily: "var(--font-nunito), sans-serif",
  fontWeight: 500,
  fontSize: 16,
  lineHeight: 1.5,
  color: "var(--text2)",
  margin: 0,
};

export default function SkillsCapabilityCard() {
  const [load, setLoad] = useState<Load>({ state: "loading" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/capabilities/extend`, { cache: "no-store" });
        if (!res.ok) {
          if (!cancelled) setLoad({ state: "unknown" });
          return;
        }
        const caps = (await res.json()) as ExtendCapabilities;
        if (!cancelled) setLoad({ state: "ready", caps });
      } catch {
        if (!cancelled) setLoad({ state: "unknown" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (load.state === "loading") return null;

  const sandbox =
    load.state === "ready" ? load.caps.skills?.sandbox_available : undefined;
  const enabled = load.state === "ready" ? load.caps.skills?.enabled : undefined;
  const workspace =
    load.state === "ready" ? load.caps.workspace_exec?.available : undefined;

  /** One line, and it is the line the box's own state justifies. */
  let line: string;
  let tone: "plain" | "off" = "plain";
  if (load.state === "unknown") {
    line =
      "This build does not report whether it can run skills, so nothing here offers to install one.";
  } else if (sandbox === false) {
    line =
      "Skills run in a Linux sandbox (bubblewrap). This box has none, so skills install switched off.";
    tone = "off";
  } else if (sandbox === true && enabled === false) {
    line =
      "This box can run skills in its Linux sandbox, but skills are switched off — nothing installs until the owner turns them on.";
    tone = "off";
  } else if (sandbox === true) {
    line =
      "Skills run in a Linux sandbox (bubblewrap), which this box has. Anything installed still needs your consent first.";
  } else {
    line =
      "This box did not say whether it has the Linux sandbox skills need, so nothing here offers to install one.";
  }

  return (
    <div style={cardStyle}>
      <div className="mb-3 flex items-center gap-2.5">
        <MsIcon name="extension" size={22} style={{ color: "var(--text2)" }} />
        <h3
          style={{
            margin: 0,
            fontFamily: "var(--font-fredoka), sans-serif",
            fontWeight: 600,
            fontSize: 20,
            color: "var(--text)",
          }}
        >
          Skills
        </h3>
      </div>

      <p style={{ ...bodyText, color: tone === "off" ? "var(--text3)" : "var(--text2)" }}>
        {line}
      </p>

      {workspace === false && (
        /* The other half of self-extension: the sandboxed shell. Same rule — say
           it is unavailable rather than leaving a control that would fail. The
           box own reason is preferred when it gives one; it knows which helper
           is missing and this file does not. */
        <p style={{ ...bodyText, marginTop: 10, color: "var(--text3)" }}>
          {load.state === "ready" && load.caps.workspace_exec?.reason
            ? `The sandboxed workspace shell is unavailable too: ${load.caps.workspace_exec.reason}`
            : "The sandboxed workspace shell is unavailable on this box too, so tools that would run commands there are not offered."}
        </p>
      )}
    </div>
  );
}
