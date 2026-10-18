"use client";

// True while this screen is not paired and the pairing gate is on, so a page
// for paired screens can say "Pair this screen" instead of "did not answer".
// Asks GET /api/pair/status once (public, no secret in it) and also listens
// for the gate's own 401s (PAIRING_REQUIRED_EVENT), so a device that is
// un-paired while the page is open is told too.

import { useEffect, useState } from "react";
import { PAIRING_REQUIRED_EVENT } from "../pairingWords";

export function usePairingNeeded(): boolean {
  const [needed, setNeeded] = useState(false);

  useEffect(() => {
    let live = true;
    fetch("/api/pair/status", { cache: "no-store" })
      .then((r) => r.json())
      .then((s: { paired?: boolean; enforce?: string }) => {
        if (live && s && s.paired === false && s.enforce && s.enforce !== "off") setNeeded(true);
      })
      .catch(() => {});
    const on = () => {
      if (live) setNeeded(true);
    };
    window.addEventListener(PAIRING_REQUIRED_EVENT, on);
    return () => {
      live = false;
      window.removeEventListener(PAIRING_REQUIRED_EVENT, on);
    };
  }, []);

  return needed;
}
