"use client";

/**
 * The Vault PIN prompt for the build engine's changes, mounted once in the
 * dashboard shell. It appears when a change (installing or removing an app,
 * storing a key, connecting GitHub, opening the Developer terminal) is
 * refused because the Vault is locked, and the change goes on by itself once
 * the Vault opens (app/_lib/vaultSession.ts).
 *
 * The PIN lives in this component's state only, goes once to the existing
 * POST /vault/unlock (the route the Vault page uses), and is cleared before
 * the request is sent. Only the session the backend answers with is kept, in
 * memory. The backend owns the try limit; a refusal is said in plain words
 * (unlockRefusalWords), never as the backend's raw message.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  VAULT_NO_ANSWER_LINE,
  registerVaultAsker,
  setVaultSession,
  unlockRefusalWords,
} from "../_lib/vaultSession";

const NUNITO = "var(--font-nunito), sans-serif";

const button: React.CSSProperties = {
  padding: "8px 18px",
  borderRadius: 999,
  border: "2px solid var(--accent, #7c5cff)",
  background: "var(--card2)",
  fontFamily: NUNITO,
  fontWeight: 700,
  fontSize: 14,
  color: "var(--text)",
  cursor: "pointer",
};

/** POST /vault/unlock; the session token, or the words to show. */
export async function unlockForSession(pin: string): Promise<{ token: string; words: string }> {
  try {
    const res = await fetch("/api/pai/vault/unlock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ pin }),
    });
    const data = (await res.json().catch(() => ({}))) as {
      success?: boolean;
      session_token?: unknown;
    };
    const token = typeof data.session_token === "string" ? data.session_token : "";
    if (res.ok && data.success && token) return { token, words: "" };
    return { token: "", words: unlockRefusalWords(res.status, data) };
  } catch {
    return { token: "", words: VAULT_NO_ANSWER_LINE };
  }
}

export default function VaultPinPrompt() {
  const [line, setLine] = useState("");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState("");
  const settle = useRef<((token: string) => void) | null>(null);
  const field = useRef<HTMLInputElement | null>(null);

  useEffect(
    () =>
      registerVaultAsker(
        (text) =>
          new Promise<string>((resolve) => {
            settle.current = resolve;
            setSaid("");
            setPin("");
            setLine(text);
          }),
      ),
    [],
  );

  useEffect(() => {
    if (line) field.current?.focus();
  }, [line]);

  const finish = useCallback((token: string) => {
    const done = settle.current;
    settle.current = null;
    setLine("");
    setPin("");
    setSaid("");
    done?.(token);
  }, []);

  // Leaving the page must not leave a request waiting for an answer.
  useEffect(() => () => settle.current?.(""), []);

  async function submit() {
    const sending = pin;
    setPin("");
    if (!sending || busy) return;
    setBusy(true);
    const { token, words } = await unlockForSession(sending);
    setBusy(false);
    if (!token) {
      setSaid(words);
      return;
    }
    setVaultSession(token);
    finish(token);
  }

  if (!line) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="vault-pin-prompt-line"
      data-testid="vault-pin-prompt"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 80,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 16,
        background: "rgba(0,0,0,.45)",
      }}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
        autoComplete="off"
        style={{
          width: "min(420px, 100%)",
          border: "1px solid var(--line)",
          borderRadius: 18,
          background: "var(--card)",
          padding: "18px 20px",
          fontFamily: NUNITO,
          color: "var(--text)",
          display: "grid",
          gap: 12,
        }}
      >
        <p id="vault-pin-prompt-line" style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>
          {line}
        </p>
        <p style={{ margin: 0, fontSize: 14, color: "var(--text2)" }}>
          It is the PIN you chose when you set up the Vault.
        </p>
        <input
          ref={field}
          type="password"
          aria-label="Your Vault PIN"
          placeholder="Vault PIN"
          autoComplete="off"
          spellCheck={false}
          value={pin}
          onChange={(e) => setPin(e.target.value)}
          disabled={busy}
          style={{
            padding: "8px 12px",
            borderRadius: 10,
            border: "1px solid var(--line)",
            background: "var(--card2)",
            color: "var(--text)",
            fontSize: 15,
          }}
        />
        {said ? (
          <p role="status" style={{ margin: 0, fontSize: 14, color: "var(--text2)" }}>
            {said}
          </p>
        ) : null}
        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", flexWrap: "wrap" }}>
          <button type="button" style={{ ...button, borderColor: "var(--line)" }} onClick={() => finish("")} disabled={busy}>
            Not now
          </button>
          <button type="submit" style={button} disabled={busy || !pin}>
            {busy ? "Opening..." : "Unlock"}
          </button>
        </div>
      </form>
    </div>
  );
}
