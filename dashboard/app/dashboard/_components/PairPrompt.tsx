"use client";

/**
 * PairPrompt: one clear "Pair this device" box for a device the pairing gate
 * doesn't know yet.
 *
 * Before this, an unpaired phone loaded the dashboard and every feed sat
 * empty, because every /api/pai call answered 401 {"error":"pairing_required"}.
 * Now the dashboard asks once, in plain words, for the household code.
 *
 * When it shows: /api/pair/status says not paired while the gate is on, or any
 * same-origin /api/ call answers 401 with error "pairing_required" (the gate's
 * own body, so other 401s never trigger it). Pairing itself is the existing
 * POST /api/pair (same-origin, rate-limited, sets the device cookie). The code
 * is typed by hand: never prefilled, never shown, never logged here.
 *
 * puff puff pass
 */
import { FormEvent, useEffect, useState } from "react";
import MsIcon from "../../_components/MsIcon";

const ERRORS: Record<string, string> = {
  invalid_code: "That code didn't work. Check it and try again.",
  rate_limited: "Too many tries. Wait a minute, then try again.",
};

let fetchHooked = false;

export default function PairPrompt() {
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    fetch("/api/pair/status", { cache: "no-store" })
      .then((r) => r.json())
      .then((s: { paired?: boolean; enforce?: string }) => {
        if (live && s && s.paired === false && s.enforce && s.enforce !== "off") setOpen(true);
      })
      .catch(() => {});

    // Later 401s (a revoked or expired device cookie) open the same box.
    const show = () => live && setOpen(true);
    window.addEventListener("puffpai:pairing-required", show);
    if (!fetchHooked) {
      fetchHooked = true;
      const orig = window.fetch.bind(window);
      window.fetch = async (...args: Parameters<typeof fetch>) => {
        const res = await orig(...args);
        if (res.status === 401) {
          try {
            const raw = args[0];
            const url = new URL(
              typeof raw === "string" ? raw : raw instanceof URL ? raw.href : raw.url,
              window.location.href,
            );
            if (url.origin === window.location.origin && url.pathname.startsWith("/api/")) {
              const body = await res.clone().json();
              if (body?.error === "pairing_required") {
                window.dispatchEvent(new Event("puffpai:pairing-required"));
              }
            }
          } catch {
            /* not JSON or not ours: leave it */
          }
        }
        return res;
      };
    }
    return () => {
      live = false;
      window.removeEventListener("puffpai:pairing-required", show);
    };
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!code.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/pair", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: code.trim() }),
      });
      if (res.ok) {
        setCode("");
        window.location.reload();
        return;
      }
      let err = "";
      try {
        err = ((await res.json()) as { error?: string }).error ?? "";
      } catch {
        /* no body */
      }
      setError(ERRORS[err] ?? "Pairing isn't working right now. Try again in a moment.");
    } catch {
      setError("Couldn't reach the box. Are you on the home Wi-Fi?");
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="pair-prompt-title"
      className="fixed inset-0 z-[90] grid place-items-center p-4"
      style={{ background: "rgba(8,10,18,0.72)" }}
    >
      <form
        onSubmit={submit}
        className="w-full max-w-[420px] rounded-[28px] border p-5"
        style={{ background: "var(--card)", borderColor: "var(--line)", color: "var(--text)" }}
      >
        <div className="flex items-center gap-3">
          <MsIcon name="phonelink_lock" size={26} style={{ color: "var(--pink)" }} />
          <h2 id="pair-prompt-title" style={{ font: "700 20px var(--font-fredoka), sans-serif" }}>
            Pair this device
          </h2>
        </div>
        <p className="mt-2" style={{ font: "500 14px var(--font-nunito), sans-serif", color: "var(--text2)" }}>
          Type the pairing code. It&apos;s on the kiosk under Settings, Paired devices.
        </p>
        <input
          type="text"
          inputMode="text"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          maxLength={20}
          value={code}
          onChange={(e) => setCode(e.target.value)}
          aria-label="Pairing code"
          placeholder="XXXX-XXXX"
          className="mt-4 w-full rounded-2xl border px-4"
          style={{
            minHeight: 52,
            borderColor: "var(--line)",
            background: "var(--chip)",
            color: "var(--text)",
            font: "600 18px var(--font-jbmono), monospace",
            letterSpacing: "0.12em",
          }}
        />
        {error && (
          <p role="alert" className="mt-2" style={{ font: "600 13.5px var(--font-nunito), sans-serif", color: "var(--pink)" }}>
            {error}
          </p>
        )}
        <div className="mt-4 flex gap-2">
          <button
            type="submit"
            disabled={busy || !code.trim()}
            className="min-h-[48px] flex-1 rounded-2xl px-4 disabled:opacity-50"
            style={{ background: "linear-gradient(140deg, var(--pink), var(--magenta))", color: "#fff", font: "700 16px var(--font-nunito), sans-serif" }}
          >
            {busy ? "Pairing..." : "Pair"}
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="min-h-[48px] rounded-2xl border px-4"
            style={{ borderColor: "var(--line)", background: "var(--chip)", color: "var(--text)", font: "600 15px var(--font-nunito), sans-serif" }}
          >
            Not now
          </button>
        </div>
      </form>
    </div>
  );
}
