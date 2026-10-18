"use client";

/**
 * "Connect my Home Assistant", in one place for the Developer Brain page, the
 * Smart Home page, Settings and the wizard.
 *
 * Asks for Home Assistant's address and a long-lived access token, can test
 * them first (POST /setup/ha/test, which saves nothing), and saves them with
 * POST /setup/ha. Both go through the same-origin proxy with vaultFetch, so a
 * finished box asks for the Vault PIN when the backend wants it. The token is
 * typed into a hidden box, sent, cleared, and never shown again.
 */

import { useCallback, useEffect, useState } from "react";
import Button from "@/app/_components/Button";
import { DEFAULT_HA_URL, setupCodeHeaders } from "@/app/_lib/services/setupService";
import { vaultFetch } from "@/app/_lib/vaultSession";
import { Panel, PanelTitle, inputStyle } from "@/app/dashboard/apps/_components/ui";

/** Where the token comes from, in Home Assistant's own words. */
export const HA_TOKEN_HOW =
  "In Home Assistant, open your profile, then Security, and under Long-lived access tokens choose Create token. Copy it and paste it here.";

/** Turns a technical failure into a sentence a person can act on. */
export function plainHaWords(text: string | undefined | null): string {
  const raw = (text || "").trim();
  const t = raw.toLowerCase();
  if (!t) return "Home Assistant did not answer.";
  if (/\b401\b|unauthori[sz]ed|forbidden|\b403\b|invalid token/.test(t)) {
    return "Home Assistant did not accept that token. Make a new long-lived access token and paste it again.";
  }
  if (/timed? ?out/.test(t)) {
    return "Home Assistant took too long to answer. Check that it is switched on and the address is right.";
  }
  if (/name resolution|getaddrinfo|nodename|not known/.test(t)) {
    return "That address could not be found on your network. Check the spelling, or try its IP address.";
  }
  if (/refused|unreachable|no route|max retries|failed to establish/.test(t)) {
    return "Nothing answered at that address. Check that Home Assistant is running and the address and port are right.";
  }
  if (/ssl|certificate/.test(t)) {
    return "Home Assistant's secure connection did not check out. Try the http:// address instead.";
  }
  return raw;
}

interface Answer {
  ok: boolean;
  status: number;
  data: Record<string, unknown>;
  /** The backend's sentence (`message`), plain-worded; empty when ok. */
  error: string;
}

async function call(path: string, method: "GET" | "POST", body?: unknown): Promise<Answer> {
  let res: Response;
  try {
    res = await vaultFetch(`/api/pai${path}`, {
      method,
      headers: { "Content-Type": "application/json", ...setupCodeHeaders() },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    return { ok: false, status: 0, data: {}, error: "Cannot reach the backend." };
  }
  let data: Record<string, unknown> = {};
  try {
    data = (await res.json()) || {};
  } catch {
    /* empty or non-JSON body */
  }
  const ok = res.ok && data.ok !== false;
  // `error` is the backend's code; the sentence is in `message`.
  const said = typeof data.message === "string" ? data.message : "";
  return { ok, status: res.status, data, error: ok ? "" : plainHaWords(said || `HTTP ${res.status}`) };
}

function post(path: string, body: unknown): Promise<Answer> {
  return call(path, "POST", body);
}

const small: React.CSSProperties = {
  fontFamily: "var(--font-nunito), sans-serif",
  fontSize: 14,
  lineHeight: 1.5,
  color: "var(--text2)",
  margin: 0,
};

export default function HomeAssistantConnect({
  title = "Connect my Home Assistant",
  onSaved,
}: {
  title?: string;
  onSaved?: () => void;
}) {
  const [configured, setConfigured] = useState(false);
  const [address, setAddress] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    const r = await call("/setup/status?ollama=0", "GET");
    const ha = (r.ok ? r.data.ha : null) as { configured?: boolean; url?: string } | null;
    setConfigured(Boolean(ha?.configured));
    setAddress((now) => now || ha?.url || DEFAULT_HA_URL);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const test = async () => {
    setBusy(true);
    setNote(null);
    const r = await post("/setup/ha/test", { url: address.trim(), token });
    setBusy(false);
    if (!r.ok) {
      setNote({ ok: false, text: r.error });
      return;
    }
    const place = r.data.location_name;
    const where = typeof place === "string" && place ? ` (${place})` : "";
    setNote({ ok: true, text: `Home Assistant answered${where}.` });
  };

  const save = async () => {
    setBusy(true);
    setNote(null);
    const r = await post("/setup/ha", { present: true, url: address.trim(), token });
    setBusy(false);
    if (!r.ok) {
      setNote({ ok: false, text: r.error });
      return;
    }
    setToken("");
    setNote({ ok: true, text: "Home Assistant is connected." });
    await load();
    onSaved?.();
  };

  const ready = !busy && Boolean(address.trim()) && Boolean(token);

  return (
    <Panel>
      <PanelTitle>{title}</PanelTitle>
      <div style={{ display: "grid", gap: 8, maxWidth: 520 }}>
        {configured && (
          <p style={small}>Connected at {address}. To change it, type a new address or token and save again.</p>
        )}
        <p style={small}>{HA_TOKEN_HOW}</p>
        <input
          aria-label="Home Assistant address"
          placeholder={DEFAULT_HA_URL}
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          style={inputStyle}
        />
        <input
          aria-label="Home Assistant token"
          type="password"
          autoComplete="off"
          placeholder="Long-lived access token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          style={inputStyle}
        />
        <div className="flex gap-2">
          <Button
            label="Test it"
            ariaLabel="Test it: try this Home Assistant address and token"
            color="lightDark"
            small
            roundedFull
            disabled={!ready}
            onClick={test}
          />
          <Button
            label="Save"
            ariaLabel="Save this Home Assistant address and token"
            color="info"
            small
            roundedFull
            disabled={!ready}
            onClick={save}
          />
        </div>
        {note && (
          <p style={{ ...small, color: note.ok ? "var(--text)" : "var(--text2)" }} data-ha-note="">
            {note.text}
          </p>
        )}
      </div>
    </Panel>
  );
}
