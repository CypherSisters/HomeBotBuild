"use client";

/**
 * Library tile (design 4b.8) — two big stat subcards.
 *
 * The design shows KNOWLEDGE PACKS + CATEGORIES; the live /knowledge/libraries
 * payload carries no category/content_type data (all null on the Orin), so
 * the second card honestly shows DOCUMENTS (user uploads, is_upload=true)
 * instead of inventing categories.
 */

import { useEffect, useState } from "react";
import { getLibraries } from "@/app/_lib/services/knowledgeService";
import DesignTile, { FREDOKA, HatchBlock, JBM } from "./DesignTile";

function StatCard({
  value,
  label,
  color,
}: {
  value: string;
  label: string;
  color: string;
}) {
  return (
    <div
      className="flex min-w-0 flex-col justify-center gap-0.5 overflow-hidden rounded-2xl px-3 py-2"
      style={{ background: "var(--card2)" }}
    >
      <div
        className="leading-none"
        style={{ fontFamily: FREDOKA, fontWeight: 600, fontSize: 24, color }}
      >
        {value}
      </div>
      <div
        className="truncate"
        style={{
          fontFamily: JBM,
          fontWeight: 700,
          fontSize: 9,
          letterSpacing: "0.1em",
          color: "var(--text3)",
          textTransform: "uppercase",
        }}
      >
        {label}
      </div>
    </div>
  );
}

export default function LibraryTile({ packs }: { packs: number }) {
  const [docs, setDocs] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      getLibraries()
        .then((libs) => {
          if (!cancelled) setDocs(libs.filter((l) => l.is_upload).length);
        })
        .catch(() => {});
    };
    load();
    const iv = setInterval(load, 5 * 60_000);
    return () => {
      cancelled = true;
      clearInterval(iv);
    };
  }, []);

  return (
    <DesignTile
      icon="menu_book"
      iconColor="var(--sky)"
      title="Library"
      img="/design-cloud-1.jpg"
      href="/dashboard/knowledge"
    >
      {/* auto-fit: at the kiosk's w2 the two stat cards sit side by side; if
          the tile is ever narrower they stack instead of squeezing. */}
      <div className="grid h-full min-h-0 grid-cols-[repeat(auto-fit,minmax(88px,1fr))] gap-2">
        <StatCard value={`${packs}`} label="Knowledge packs" color="var(--sky)" />
        {docs === null ? (
          <HatchBlock label="…" style={{ minHeight: 0 }} />
        ) : (
          <StatCard value={`${docs}`} label="Documents" color="var(--magenta)" />
        )}
      </div>
    </DesignTile>
  );
}
