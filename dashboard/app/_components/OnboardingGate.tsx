"use client";

/**
 * First-run gate. Mounted inside the dashboard shell: on load it asks the
 * backend whether setup is complete (the fast check — no Ollama probe) and, if
 * it is not, sends the browser to /onboarding.
 *
 * Three things it deliberately does NOT do:
 *   • trap anybody. Any error, any unreachable backend, any unpaired 401 →
 *     the dashboard renders. A flaky status call must never hide a working
 *     install behind a wizard.
 *   • flash the dashboard before redirecting. While the check is in flight it
 *     shows one quiet line in the dashboard's own type.
 *   • re-ask on every navigation. `complete` flips once per box, so the check
 *     runs once per mount of the shell.
 *
 * PAIRING: GET /api/pai/setup/status is a read under /api/pai/setup, which the
 * pairing gate does not cover at the default "knowledge" enforcement stage —
 * so a fresh, unpaired first device gets a real answer here and lands on the
 * wizard. At enforcement "all" the call 401s; that is treated exactly like an
 * error (render the dashboard, which shows its own pairing UI), so the gate
 * never fights the pairing card for the screen.
 */

import { useEffect, useState, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { getSetupStatus } from "../_lib/services/setupService";

export default function OnboardingGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let active = true;
    (async () => {
      const reply = await getSetupStatus(false);
      if (!active) return;
      if (reply.ok && reply.data?.complete === false) {
        router.replace("/onboarding");
        return; // keep the splash up through the redirect
      }
      setChecking(false);
    })();
    return () => {
      active = false;
    };
    // Checked once per mount of the dashboard shell; `pathname` is here only so
    // a client-side navigation back into the shell re-checks after a re-run.
  }, [pathname, router]);

  if (checking) {
    return (
      <div
        className="flex min-h-dvh items-center justify-center"
        style={{
          fontFamily: "var(--font-jbmono), monospace",
          fontSize: 11,
          letterSpacing: ".14em",
          textTransform: "uppercase",
          color: "var(--text3)",
        }}
      >
        Loading…
      </div>
    );
  }

  return <>{children}</>;
}
