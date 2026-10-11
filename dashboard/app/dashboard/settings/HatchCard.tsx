"use client";

/**
 * HatchCard — Settings entry for the hatch ritual and the adoption
 * certificate.
 *
 * Reads GET /hatch/status (real state, not a guess): an unhatched build gets
 * a single "Begin the hatch" action; a hatched one shows who lives here,
 * links to the replayable ritual (/dashboard/hatch), and offers the
 * certificate downloads — PNG only when the backend reports Pillow is
 * actually installed (certificate_formats), otherwise the SVG stands alone
 * and no dead button pretends otherwise.
 *
 * Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import MsIcon from "../../_components/MsIcon";
import { apiGet } from "@/app/_lib/api";

interface HatchStatus {
  hatched: boolean;
  name: string | null;
  hatch_date: string | null;
  replays: number;
  certificate_formats: string[];
}

const API_BASE = "/api/pai";

const cardStyle: React.CSSProperties = {
  borderRadius: 30,
  border: "1px solid var(--line)",
  background: "var(--card)",
  backdropFilter: "blur(18px)",
  WebkitBackdropFilter: "blur(18px)",
  padding: "26px 28px",
  boxShadow: "var(--shadow)",
};

const pillLink: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  height: 48,
  padding: "0 22px",
  borderRadius: 99,
  border: "1px solid var(--line)",
  background: "var(--card2)",
  color: "var(--text2)",
  fontFamily: "var(--font-nunito), sans-serif",
  fontWeight: 700,
  fontSize: 15,
  textDecoration: "none",
};

export default function HatchCard() {
  const [status, setStatus] = useState<HatchStatus | null>(null);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    let mounted = true;
    apiGet<HatchStatus>("/hatch/status")
      .then((s) => {
        if (mounted) setStatus(s);
      })
      .catch(() => {
        if (mounted) setUnreachable(true);
      });
    return () => {
      mounted = false;
    };
  }, []);

  const pngAvailable = (status?.certificate_formats || []).includes("png");

  return (
    <div style={cardStyle}>
      <div
        style={{
          fontFamily: "var(--font-fredoka), sans-serif",
          fontWeight: 600,
          fontSize: 24,
          color: "var(--text)",
          marginBottom: 6,
        }}
      >
        The Hatch
      </div>
      <p
        style={{
          margin: "0 0 18px",
          fontFamily: "var(--font-nunito), sans-serif",
          fontWeight: 500,
          fontSize: 15,
          lineHeight: 1.5,
          color: "var(--text2)",
        }}
      >
        {unreachable ? (
          <>
            The backend doesn&rsquo;t answer for the hatch yet — it may be
            asleep, or running a build older than this dashboard.
          </>
        ) : status === null ? (
          <>Checking who lives here…</>
        ) : status.hatched ? (
          <>
            <span style={{ fontWeight: 700, color: "var(--text)" }}>
              {status.name}
            </span>{" "}
            hatched here on {status.hatch_date}
            {status.replays > 0
              ? ` — the ritual has been replayed ${status.replays} time${
                  status.replays === 1 ? "" : "s"
                }.`
              : "."}{" "}
            Replay it any time; replays never rename.
          </>
        ) : (
          <>
            This build hasn&rsquo;t hatched yet. Wake it up, name it, and let
            it introduce the crew.
          </>
        )}
      </p>
      <div className="flex flex-wrap gap-3">
        <Link
          href="/dashboard/hatch"
          style={{
            ...pillLink,
            border: "none",
            background: "linear-gradient(140deg,var(--pink),var(--magenta))",
            color: "#fff",
          }}
        >
          <MsIcon name="egg" size={20} />
          {status?.hatched ? "Replay the hatch" : "Begin the hatch"}
        </Link>
        {status?.hatched && (
          <a href={`${API_BASE}/hatch/certificate?format=svg&download=1`} style={pillLink}>
            <MsIcon name="workspace_premium" size={20} />
            Certificate (SVG)
          </a>
        )}
        {status?.hatched && pngAvailable && (
          <a href={`${API_BASE}/hatch/certificate?format=png&download=1`} style={pillLink}>
            PNG
          </a>
        )}
      </div>
      {status?.hatched && !pngAvailable && (
        <p
          style={{
            margin: "12px 0 0",
            fontFamily: "var(--font-jbmono), monospace",
            fontSize: 11.5,
            letterSpacing: ".06em",
            color: "var(--text3)",
          }}
        >
          SVG only on this build — PNG export needs Pillow on the backend.
        </p>
      )}
    </div>
  );
}
