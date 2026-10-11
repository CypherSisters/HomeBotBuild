"use client";

/**
 * "Home Assistant" section of the Workflows page: EVERY Home Assistant
 * automation, with name, on/off state and last run, and the ones PuffPAI made
 * marked "made by Puff". Read-only: it uses the existing `/ha/entities` read
 * and offers no controls, so it can't be confused with saved workflows.
 *
 * puff puff pass
 */

import { useEffect, useState } from "react";
import { mdiHomeAutomation } from "@mdi/js";
import SectionTitleLineWithButton from "../../_components/Section/TitleLineWithButton";
import CardBox from "../../_components/CardBox";
import { getHAEntities } from "../../_lib/services/haService";
import { selectHaAutomations, type HaEntityLike } from "./haAutomationRows";

export default function HaAutomations() {
  const [entities, setEntities] = useState<HaEntityLike[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const data = await getHAEntities();
        if (alive) {
          setEntities(Array.isArray(data?.entities) ? data.entities : []);
          setFailed(false);
        }
      } catch {
        if (alive) setFailed(true);
      }
    };
    load();
    const t = setInterval(load, 30000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const { rows, madeByPuffCount } = selectHaAutomations(entities);

  return (
    <div className="mt-8">
      <SectionTitleLineWithButton
        icon={mdiHomeAutomation}
        title={`Home Assistant automations${rows.length ? ` · ${rows.length}` : ""}`}
      />
      <CardBox>
        <p className="mb-3 text-xs text-gray-500 dark:text-slate-400">
          Every automation in Home Assistant, including ones that don&apos;t use
          Node-RED. They live in Home Assistant, not in your saved workflows.
          {madeByPuffCount > 0 &&
            ` ${madeByPuffCount} ${madeByPuffCount === 1 ? "was" : "were"} made by Puff, after you approved.`}
        </p>
        {failed && (
          <p className="py-4 text-center text-sm text-gray-400">
            I can&apos;t reach Home Assistant right now.
          </p>
        )}
        {!failed && entities !== null && rows.length === 0 && (
          <p className="py-4 text-center text-sm text-gray-400">
            Home Assistant has no automations yet.
          </p>
        )}
        {rows.length > 0 && (
          <ul className="divide-y divide-gray-100 dark:divide-slate-700">
            {rows.map((r) => (
              <li key={r.entityId} className="flex flex-wrap items-center gap-3 py-2">
                <span className="rounded-full bg-[#00B4D8]/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-[#00B4D8]">
                  Home Assistant
                </span>
                <span className="min-w-0 flex-1 truncate text-sm font-medium text-[#0C2F44] dark:text-white">
                  {r.name}
                  {r.madeByPuffpai && (
                    <span className="ml-2 rounded-full bg-[#39ff14]/10 px-2 py-0.5 text-[10px] font-medium text-green-600 dark:text-[#39ff14]">
                      made by Puff
                    </span>
                  )}
                </span>
                <span
                  className={`text-xs font-semibold ${r.on ? "text-green-500" : "text-gray-400"}`}
                >
                  {r.stateLabel}
                </span>
                <span className="text-xs text-gray-500 dark:text-slate-400">
                  Last run: {r.lastRun}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardBox>
    </div>
  );
}
