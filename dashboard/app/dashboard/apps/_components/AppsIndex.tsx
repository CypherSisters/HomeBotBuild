"use client";

/**
 * The apps index the sidebar's Apps submenu never had.
 *
 * A client component so it can ask the box which apps are installed; the page
 * around it stays a server component and keeps the page title. The built-in list
 * is read from the sidebar's own menu, so this page and the nav cannot disagree.
 *
 * BuildBot's apps index in this shell. The DATA contract is BuildBot's
 * (`listApps`); the chrome is this shell's — SectionMain / CardBox / Icon, the
 * same components every other page here uses.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  mdiChevronRight,
  mdiApps,
  mdiAlertCircleOutline,
  mdiPlus,
} from "@mdi/js";
import SectionMain from "@/app/_components/Section/Main";
import SectionTitleLineWithButton from "@/app/_components/Section/TitleLineWithButton";
import CardBox from "@/app/_components/CardBox";
import Icon from "@/app/_components/Icon";
import {
  getInstalledApps,
  type InstalledApp,
} from "@/app/_lib/services/installedAppsService";
import { onBuildChanged } from "@/app/_lib/services/buildEvents";
import { listApps } from "../_lib/appsIndex";

export default function AppsIndex() {
  // null = the box has not answered (or cannot). listApps() then shows the
  // built-ins, so the FIRST paint is already correct and there is no spinner:
  // the only thing the answer can add is extra apps and better blurbs.
  const [installed, setInstalled] = useState<InstalledApp[] | null>(null);

  const load = useCallback(() => {
    let cancelled = false;
    getInstalledApps().then((apps) => {
      if (!cancelled) setInstalled(apps);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(load, [load]);

  // The builder writes an app and the box rescans, but this component is already
  // mounted and holding the list it fetched. Refetch when it says so.
  useEffect(() => onBuildChanged(() => void load()), [load]);

  const apps = listApps(installed);

  return (
    <SectionMain>
      <SectionTitleLineWithButton icon={mdiApps} title="Apps" main>
        <span className="text-xs text-gray-500 dark:text-slate-400">
          {apps.length} {apps.length === 1 ? "app" : "apps"} on this build
        </span>
      </SectionTitleLineWithButton>

      <p className="mb-5 text-sm text-gray-600 dark:text-slate-400">
        Each one keeps its own data on this box. They start empty — an app is
        worth something once there is something in it.
      </p>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {/* The way an owner adds one. It goes FIRST because the thing this page
            could not do was the thing worth doing: an app is a folder with an
            app.json in it, and until the builder existed that meant a terminal. */}
        <Link href="/dashboard/apps/new" className="block">
          <CardBox
            className="h-full border-dashed transition hover:scale-[1.01]"
            isHoverable
          >
            <div className="flex items-start gap-3 p-4">
              <Icon
                path={mdiPlus}
                size="22"
                className="mt-0.5 shrink-0 text-[#00B4D8]"
              />
              <div className="min-w-0 flex-1">
                <p className="font-semibold text-[#0C2F44] dark:text-white">
                  New
                </p>
                <p className="mt-1 text-sm text-gray-600 dark:text-slate-400">
                  Build one of your own: a name, one sentence about what it is
                  for, and the lists you want kept. It gets its own page and its
                  words start working in chat.
                </p>
              </div>
              <Icon
                path={mdiChevronRight}
                size="18"
                className="mt-1 shrink-0 text-gray-400"
              />
            </div>
          </CardBox>
        </Link>

        {apps.map((app) => (
          <Link key={app.id} href={app.href} className="block">
            <CardBox className="h-full transition hover:scale-[1.01]" isHoverable>
              <div className="flex items-start gap-3 p-4">
                {app.icon && (
                  <Icon
                    path={app.icon}
                    size="22"
                    className="mt-0.5 shrink-0 text-[#00B4D8]"
                  />
                )}
                <div className="min-w-0 flex-1">
                  <p className="font-semibold text-[#0C2F44] dark:text-white">
                    {app.label}
                    {!app.builtIn && (
                      /* Said out loud: this one has no page of its own, so the
                         generic one is what opens. Nobody should have to guess
                         why it looks different from Calendar. */
                      <span className="ml-2 rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-semibold tracking-wide text-gray-500 uppercase dark:bg-slate-700 dark:text-slate-300">
                        added
                      </span>
                    )}
                  </p>
                  {app.description && (
                    <p className="mt-1 text-sm text-gray-600 dark:text-slate-400">
                      {app.description}
                    </p>
                  )}
                  {app.degradedReason && (
                    <p className="mt-2 flex items-start gap-1.5 text-xs text-amber-600 dark:text-amber-400">
                      <Icon
                        path={mdiAlertCircleOutline}
                        size="14"
                        className="mt-px shrink-0"
                      />
                      {app.degradedReason}
                    </p>
                  )}
                </div>
                <Icon
                  path={mdiChevronRight}
                  size="18"
                  className="mt-1 shrink-0 text-gray-400"
                />
              </div>
            </CardBox>
          </Link>
        ))}
      </div>
    </SectionMain>
  );
}
