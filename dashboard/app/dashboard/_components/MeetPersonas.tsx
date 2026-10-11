"use client";

/**
 * "Meet <bot>" — the bot the owner built, at the top of the home page.
 *
 * The home board is arrangeable, and TileGrid drops a never-seen tile at the
 * BOTTOM of a saved layout, so a persona tile would sink out of sight on every
 * box that already has a layout. This sits above the board for the same reason
 * the consent queue does.
 *
 * Renders NOTHING when there are no owner apps, or when the box could not say:
 * a failed call must not put an empty "you have none" state on the home page.
 * The words are the owner's own, out of GET /persona/list.
 *
 * Copyright (c) Cypher Sisters. Apache-2.0 License.
 */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import PersonaCard from "@/app/_components/Widgets/PersonaCard";
import {
  getOwnerApps,
  type OwnerApp,
} from "@/app/_lib/services/personaService";
import { onBuildChanged } from "@/app/_lib/services/buildEvents";
import { CHAT_ROUTE, stashChatQuery } from "@/app/_lib/chatHandoff";

export default function MeetPersonas() {
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

  const ask = useCallback(
    (text: string) => {
      // Nothing is navigated when the handoff could not be stashed: a prompt
      // that silently drops the question is worse than one that does nothing.
      if (stashChatQuery(text)) router.push(CHAT_ROUTE);
    },
    [router],
  );

  if (!apps || apps.length === 0) return null;

  return (
    <>
      {apps.map((app) => (
        <PersonaCard key={app.id} app={app} onAsk={ask} />
      ))}
    </>
  );
}
