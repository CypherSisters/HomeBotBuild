"use client";

/**
 * The apps the OWNER added, on the home board's Apps tile — with one sentence
 * that works.
 *
 * The six chips beside this are hardcoded, because the six things they point at
 * are hardcoded pages. An app somebody scaffolded has no page of its own and no
 * chip anybody wrote, so without this the home board says the build has six apps
 * when it has seven, and the owner's own app is only reachable from the index.
 *
 * The "Try:" line is the box's sentence, not ours: /persona/list serves one
 * phrase per owner app that is known to land in that app's store ("add tomatoes
 * to the plantings list"). Clicking it hands the sentence to the chat page the
 * same way the search pill does. Making one up here would risk suggesting words
 * the installed manifest does not carry, which is exactly what this must not
 * do.
 *
 * Renders NOTHING when there are no owner apps, or when the box could not say —
 * a failed call must not put an empty "you have none" state on the board.
 *
 * Copyright (c) Cypher Sisters. Apache-2.0 License.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import MsIcon from "@/app/_components/MsIcon";
import {
  getOwnerApps,
  type OwnerApp,
} from "@/app/_lib/services/personaService";
import { onBuildChanged } from "@/app/_lib/services/buildEvents";
import { CHAT_ROUTE, stashChatQuery } from "@/app/_lib/chatHandoff";

export default function OwnerAppChips() {
  const router = useRouter();
  const [apps, setApps] = useState<OwnerApp[] | null>(null);

  const load = useCallback(() => {
    let cancelled = false;
    getOwnerApps().then((reply) => {
      if (!cancelled) setApps(reply);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(load, [load]);
  useEffect(() => onBuildChanged(() => void load()), [load]);

  const say = useCallback(
    (phrase: string) => {
      if (stashChatQuery(phrase)) router.push(CHAT_ROUTE);
    },
    [router],
  );

  if (!apps || apps.length === 0) return null;

  // One "Try:" line, for the first owner app that has a list to be told about.
  const tryPhrase = apps.find((app) => app.try)?.try ?? "";

  return (
    <>
      {apps.map((app) => (
        <Link
          key={app.id}
          href={app.route || `/dashboard/apps/${app.id}`}
          className="group flex flex-col items-center justify-center gap-1 rounded-2xl border py-2 transition hover:opacity-80"
          style={{ borderColor: "var(--line)", background: "var(--card2)" }}
        >
          <div
            className="grid h-9 w-9 place-items-center rounded-[12px]"
            style={{
              background: "color-mix(in srgb, var(--mint) 16%, transparent)",
            }}
          >
            <MsIcon name="note_alt" size={22} style={{ color: "var(--mint)" }} />
          </div>
          <span
            className="max-w-full truncate px-1 text-center"
            style={{ fontSize: 12, fontWeight: 600, color: "var(--text2)" }}
          >
            {app.name}
          </span>
        </Link>
      ))}
      {tryPhrase && (
        <button
          type="button"
          onClick={() => say(tryPhrase)}
          className="col-span-full flex cursor-pointer items-center gap-1.5 rounded-full px-3 py-1.5 text-left transition hover:opacity-80"
          style={{ background: "var(--chip)" }}
        >
          <MsIcon name="chat" size={14} style={{ color: "var(--text3)" }} />
          <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text2)" }}>
            Try: &ldquo;{tryPhrase}&rdquo;
          </span>
        </button>
      )}
    </>
  );
}
