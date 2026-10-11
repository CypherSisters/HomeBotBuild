"use client";

/**
 * Connect Spotify — voice-driven Spotify setup for the appliance.
 *
 * One shared Spotify account is linked to PuffPAI so the household can say
 * "hey PuffPAI, play my Discover Weekly". The user pastes their own Spotify
 * developer app's Client ID + Secret, authorizes once, and the backend holds
 * the tokens from there on.
 *
 * Backend contract (all via the same-origin /api/pai proxy, which strips the
 * /api/pai prefix — hence the load-bearing double /api):
 *   GET  /api/pai/api/spotify/status     → {configured, connected, account,
 *                                           device_online, premium,
 *                                           bridge_open, bridge_message?}
 *   POST /api/pai/api/spotify/config     {client_id, client_secret}
 *                                        → {ok, authorize_url}
 *   POST /api/pai/api/spotify/exchange   {code, state?} → {ok, account} | {ok:false,error}
 *   POST /api/pai/api/spotify/disconnect {forget_app?}  → {ok}
 *
 * Secret handling: the secret field is write-only. It is cleared from state
 * after a successful save, never persisted, and never rendered back.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import MsIcon from "../../_components/MsIcon";

const FREDOKA = "var(--font-fredoka), sans-serif";
const NUNITO = "var(--font-nunito), sans-serif";

const cardStyle: React.CSSProperties = {
  borderRadius: 30,
  border: "1px solid var(--line)",
  background: "var(--card)",
  backdropFilter: "blur(18px)",
  WebkitBackdropFilter: "blur(18px)",
  padding: "26px 28px",
  boxShadow: "var(--shadow)",
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  height: 48,
  padding: "0 16px",
  borderRadius: 14,
  border: "1px solid var(--line)",
  background: "var(--card2)",
  color: "var(--text)",
  fontFamily: NUNITO,
  fontWeight: 600,
  fontSize: 15,
  outline: "none",
};

const labelStyle: React.CSSProperties = {
  display: "block",
  fontFamily: NUNITO,
  fontWeight: 700,
  fontSize: 13,
  color: "var(--text2)",
  marginBottom: 6,
};

const primaryBtn: React.CSSProperties = {
  height: 48,
  borderRadius: 99,
  border: "1px solid transparent",
  background: "linear-gradient(140deg,var(--pink),var(--magenta))",
  color: "#fff",
  fontFamily: NUNITO,
  fontWeight: 700,
  fontSize: 15,
  cursor: "pointer",
};

const secondaryBtn: React.CSSProperties = {
  height: 44,
  borderRadius: 99,
  border: "1px solid var(--line)",
  background: "var(--card2)",
  color: "var(--text2)",
  fontFamily: NUNITO,
  fontWeight: 700,
  fontSize: 14,
  cursor: "pointer",
};

interface SpotifyStatus {
  configured: boolean;
  connected: boolean;
  account: string | null;
  device_online: boolean;
  premium: boolean | null;
  /** The Bridge's `spotify` switch; null when the backend did not say. */
  bridge_open: boolean | null;
  bridge_message: string | null;
}

type Toast = { kind: "ok" | "err"; text: string } | null;

const HELP_STEPS = [
  "Go to developer.spotify.com → Dashboard → Create app.",
  "Add this exact Redirect URI: http://127.0.0.1:3003/api/spotify/callback",
  "Enable the Web API when asked which APIs you'll use.",
  "Copy the Client ID and Client Secret and paste them here.",
];

export default function ConnectSpotifyCard() {
  const [status, setStatus] = useState<SpotifyStatus | null>(null);
  const [loaded, setLoaded] = useState(false);

  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [authorizeUrl, setAuthorizeUrl] = useState<string | null>(null);

  const [showHelp, setShowHelp] = useState(false);
  const [showManual, setShowManual] = useState(false);
  const [manualCode, setManualCode] = useState("");

  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<Toast>(null);

  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flash = useCallback((t: Toast) => {
    setToast(t);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    if (t) toastTimer.current = setTimeout(() => setToast(null), 6000);
  }, []);

  const loadStatus = useCallback(async () => {
    try {
      const r = await fetch("/api/pai/api/spotify/status", {
        cache: "no-store",
        signal: AbortSignal.timeout(8000),
      });
      if (!r.ok) return;
      const d = (await r.json()) as Partial<SpotifyStatus>;
      setStatus({
        configured: Boolean(d.configured),
        connected: Boolean(d.connected),
        account: d.account ?? null,
        device_online: Boolean(d.device_online),
        premium: d.premium ?? null,
        bridge_open: typeof d.bridge_open === "boolean" ? d.bridge_open : null,
        bridge_message: d.bridge_message ?? null,
      });
    } catch {
      // Backend restarting or not yet deployed — keep the last known state.
    } finally {
      setLoaded(true);
    }
  }, []);

  /* Mount: read the ?spotify= round-trip flag, then poll status every 5s. */
  useEffect(() => {
    try {
      const params = new URLSearchParams(window.location.search);
      const flag = params.get("spotify");
      if (flag === "connected") {
        flash({ kind: "ok", text: "Spotify connected." });
      } else if (flag === "error") {
        const reason = params.get("reason");
        flash({
          kind: "err",
          text: reason
            ? `Couldn't connect: ${reason.replace(/_/g, " ")}`
            : "Couldn't connect to Spotify.",
        });
      }
      if (flag) {
        // Strip the flag so a refresh doesn't re-toast.
        params.delete("spotify");
        params.delete("reason");
        const qs = params.toString();
        window.history.replaceState(
          null,
          "",
          window.location.pathname + (qs ? `?${qs}` : ""),
        );
      }
    } catch {
      /* URL parsing unavailable */
    }

    loadStatus();
    const iv = setInterval(loadStatus, 5000);
    return () => {
      clearInterval(iv);
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, [loadStatus, flash]);

  const saveConfig = useCallback(async () => {
    if (!clientId.trim() || !clientSecret.trim() || busy) return;
    setBusy(true);
    try {
      const r = await fetch("/api/pai/api/spotify/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          client_id: clientId.trim(),
          client_secret: clientSecret.trim(),
        }),
        signal: AbortSignal.timeout(12000),
      });
      const d = (await r.json().catch(() => null)) as {
        ok?: boolean;
        authorize_url?: string;
        error?: string;
      } | null;
      if (r.ok && d?.ok && d.authorize_url) {
        setAuthorizeUrl(d.authorize_url);
        // Write-only secret: drop it from state the moment it's saved.
        setClientSecret("");
        flash({ kind: "ok", text: "Saved. Now authorize with Spotify." });
        loadStatus();
      } else {
        flash({
          kind: "err",
          text: d?.error
            ? `Save failed: ${d.error}`
            : "Save failed. Check the Client ID and Secret.",
        });
      }
    } catch {
      flash({ kind: "err", text: "Save failed — backend unreachable." });
    } finally {
      setBusy(false);
    }
  }, [clientId, clientSecret, busy, flash, loadStatus]);

  const exchangeCode = useCallback(async () => {
    const raw = manualCode.trim();
    if (!raw || busy) return;
    // Accept either the whole pasted address bar (…/callback?code=…&state=…)
    // or a bare code. The backend requires BOTH code and state (single-use
    // CSRF), so pull state out when it's there.
    let code = raw;
    let state: string | undefined;
    const q = raw.indexOf("?");
    const params = new URLSearchParams(q >= 0 ? raw.slice(q + 1) : raw);
    if (params.get("code")) {
      code = params.get("code") as string;
      state = params.get("state") ?? undefined;
    }
    setBusy(true);
    try {
      const r = await fetch("/api/pai/api/spotify/exchange", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state ? { code, state } : { code }),
        signal: AbortSignal.timeout(15000),
      });
      const d = (await r.json().catch(() => null)) as {
        ok?: boolean;
        account?: string;
        error?: string;
      } | null;
      if (r.ok && d?.ok) {
        setManualCode("");
        setShowManual(false);
        flash({ kind: "ok", text: "Spotify connected." });
        loadStatus();
      } else {
        flash({
          kind: "err",
          text: d?.error ? `Couldn't connect: ${d.error}` : "That code didn't work.",
        });
      }
    } catch {
      flash({ kind: "err", text: "Couldn't connect — backend unreachable." });
    } finally {
      setBusy(false);
    }
  }, [manualCode, busy, flash, loadStatus]);

  const disconnect = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await fetch("/api/pai/api/spotify/disconnect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
        signal: AbortSignal.timeout(12000),
      });
      if (r.ok) {
        setAuthorizeUrl(null);
        flash({ kind: "ok", text: "Spotify disconnected." });
        loadStatus();
      } else {
        flash({ kind: "err", text: "Disconnect failed." });
      }
    } catch {
      flash({ kind: "err", text: "Disconnect failed — backend unreachable." });
    } finally {
      setBusy(false);
    }
  }, [busy, flash, loadStatus]);

  /* ── header ─────────────────────────────────────────────────── */
  const header = (
    <div className="mb-1 flex items-center gap-2.5">
      <span
        className="grid h-10 w-10 place-items-center rounded-2xl"
        style={{ background: "var(--chip)", color: "var(--mint)" }}
      >
        <MsIcon name="graphic_eq" size={22} fill />
      </span>
      <div>
        <div
          style={{
            fontFamily: FREDOKA,
            fontWeight: 600,
            fontSize: 24,
            color: "var(--text)",
          }}
        >
          Connect Spotify
        </div>
        <div
          style={{
            fontFamily: NUNITO,
            fontWeight: 500,
            fontSize: 14,
            color: "var(--text3)",
          }}
        >
          Link one account, then just say &ldquo;play my Discover Weekly&rdquo;.
        </div>
      </div>
    </div>
  );

  const toastEl = toast && (
    <div
      role="status"
      className="flex items-center gap-2"
      style={{
        marginTop: 16,
        padding: "12px 16px",
        borderRadius: 16,
        background: toast.kind === "ok" ? "rgba(52,211,153,.14)" : "rgba(244,114,182,.14)",
        border: `1px solid ${toast.kind === "ok" ? "var(--mint)" : "var(--pink)"}`,
        fontFamily: NUNITO,
        fontWeight: 600,
        fontSize: 14,
        color: "var(--text2)",
      }}
    >
      <MsIcon
        name={toast.kind === "ok" ? "check_circle" : "error"}
        size={18}
        style={{ color: toast.kind === "ok" ? "var(--mint)" : "var(--pink)" }}
      />
      {toast.text}
    </div>
  );

  const bridgeEl = status?.bridge_open === false && (
    <div
      role="status"
      className="flex items-center gap-2"
      style={{
        marginTop: 16,
        padding: "12px 16px",
        borderRadius: 16,
        background: "rgba(252,217,122,.14)",
        border: "1px solid var(--lemon)",
        fontFamily: NUNITO,
        fontWeight: 600,
        fontSize: 14,
        color: "var(--text2)",
      }}
    >
      <MsIcon name="block" size={18} style={{ color: "var(--lemon)" }} />
      {status.bridge_message ||
        "Spotify is switched off. To use it, open Settings and switch on 'Spotify' in the Bridge section."}
    </div>
  );

  /* ── body by state ──────────────────────────────────────────── */
  let body: React.ReactNode;

  if (!loaded && !status) {
    body = (
      <div
        style={{
          marginTop: 18,
          fontFamily: NUNITO,
          fontWeight: 500,
          fontSize: 15,
          color: "var(--text3)",
        }}
      >
        Checking Spotify status…
      </div>
    );
  } else if (status?.connected) {
    /* ── Connected ── */
    body = (
      <div className="mt-4 flex flex-col gap-3.5">
        <div
          className="flex items-center gap-2.5"
          style={{
            padding: "14px 18px",
            borderRadius: 18,
            background: "rgba(52,211,153,.12)",
            border: "1px solid var(--mint)",
          }}
        >
          <MsIcon name="check_circle" size={22} style={{ color: "var(--mint)" }} fill />
          <div className="min-w-0">
            <div
              style={{
                fontFamily: NUNITO,
                fontWeight: 700,
                fontSize: 16,
                color: "var(--text)",
              }}
            >
              Connected{status.account ? ` · ${status.account}` : ""}
            </div>
            <div
              style={{
                fontFamily: NUNITO,
                fontWeight: 500,
                fontSize: 13.5,
                color: "var(--text2)",
              }}
            >
              PuffPAI speaker:{" "}
              {status.device_online ? "online" : "not signed in yet"}
            </div>
          </div>
        </div>

        {status.premium === false && (
          <div
            className="flex items-center gap-2"
            style={{
              padding: "12px 16px",
              borderRadius: 16,
              background: "rgba(252,217,122,.14)",
              border: "1px solid var(--lemon)",
              fontFamily: NUNITO,
              fontWeight: 600,
              fontSize: 14,
              color: "var(--text2)",
            }}
          >
            <MsIcon name="warning" size={18} style={{ color: "var(--lemon)" }} />
            Spotify Premium is required to play.
          </div>
        )}

        <button
          type="button"
          onClick={disconnect}
          disabled={busy}
          style={{ ...secondaryBtn, opacity: busy ? 0.5 : 1 }}
          className="self-start px-6"
        >
          Disconnect
        </button>
      </div>
    );
  } else if (status?.configured || authorizeUrl) {
    /* ── Configured, not connected: authorize step ── */
    body = (
      <div className="mt-4 flex flex-col gap-3.5">
        {authorizeUrl ? (
          <>
            <div
              style={{
                fontFamily: NUNITO,
                fontWeight: 500,
                fontSize: 15,
                lineHeight: 1.5,
                color: "var(--text2)",
              }}
            >
              Sign in to the Spotify account the whole house will share, then
              approve access.
            </div>
            {/* Top-level navigation (not a popup) so the redirect back to the
                callback route resolves cleanly on the appliance. */}
            <a
              href={authorizeUrl}
              className="flex items-center justify-center gap-2 no-underline"
              style={{ ...primaryBtn, padding: "0 24px" }}
            >
              <MsIcon name="login" size={20} />
              Authorize with Spotify
            </a>
          </>
        ) : (
          <div
            style={{
              fontFamily: NUNITO,
              fontWeight: 500,
              fontSize: 15,
              lineHeight: 1.5,
              color: "var(--text2)",
            }}
          >
            Your app credentials are saved. Re-enter them below to get a fresh
            authorize link.
          </div>
        )}

        {/* Manual code paste — for authorizing from another device. */}
        <button
          type="button"
          onClick={() => setShowManual((v) => !v)}
          className="flex items-center gap-1.5 self-start"
          style={{
            background: "none",
            border: "none",
            padding: 0,
            cursor: "pointer",
            fontFamily: NUNITO,
            fontWeight: 700,
            fontSize: 13,
            color: "var(--text3)",
          }}
        >
          <MsIcon name={showManual ? "expand_less" : "expand_more"} size={18} />
          Authorizing from another device? Paste the link
        </button>
        {showManual && (
          <div className="flex flex-col gap-2">
            <div
              style={{
                fontFamily: NUNITO,
                fontWeight: 500,
                fontSize: 13.5,
                lineHeight: 1.45,
                color: "var(--text3)",
              }}
            >
              If the page after login fails to load, copy the whole address
              from its bar (it starts with <code>http://127.0.0.1:3003</code>)
              and paste it here.
            </div>
            <div className="flex gap-2">
              <input
                type="text"
                value={manualCode}
                onChange={(e) => setManualCode(e.target.value)}
                placeholder="Paste the address…"
                style={inputStyle}
                autoComplete="off"
                spellCheck={false}
              />
              <button
                type="button"
                onClick={exchangeCode}
                disabled={busy || !manualCode.trim()}
                style={{
                  ...primaryBtn,
                  padding: "0 22px",
                  opacity: busy || !manualCode.trim() ? 0.5 : 1,
                }}
              >
                Connect
              </button>
            </div>
          </div>
        )}

        {/* Re-enter credentials to regenerate the authorize link. */}
        {!authorizeUrl && (
          <ConfigFields
            clientId={clientId}
            clientSecret={clientSecret}
            setClientId={setClientId}
            setClientSecret={setClientSecret}
            onSave={saveConfig}
            busy={busy}
            showHelp={showHelp}
            setShowHelp={setShowHelp}
          />
        )}
      </div>
    );
  } else {
    /* ── Not configured: enter credentials ── */
    body = (
      <div className="mt-4">
        <ConfigFields
          clientId={clientId}
          clientSecret={clientSecret}
          setClientId={setClientId}
          setClientSecret={setClientSecret}
          onSave={saveConfig}
          busy={busy}
          showHelp={showHelp}
          setShowHelp={setShowHelp}
        />
      </div>
    );
  }

  return (
    <div style={cardStyle}>
      {header}
      {bridgeEl}
      {body}
      {toastEl}
    </div>
  );
}

/* ── Shared config-fields block ─────────────────────────────────── */

function ConfigFields({
  clientId,
  clientSecret,
  setClientId,
  setClientSecret,
  onSave,
  busy,
  showHelp,
  setShowHelp,
}: {
  clientId: string;
  clientSecret: string;
  setClientId: (v: string) => void;
  setClientSecret: (v: string) => void;
  onSave: () => void;
  busy: boolean;
  showHelp: boolean;
  setShowHelp: (fn: (v: boolean) => boolean) => void;
}) {
  return (
    <div className="flex flex-col gap-3.5">
      <div
        style={{
          fontFamily: NUNITO,
          fontWeight: 500,
          fontSize: 14,
          color: "var(--text3)",
        }}
      >
        Paste these from your Spotify developer app.
      </div>

      <div>
        <label style={labelStyle} htmlFor="spotify-client-id">
          Client ID
        </label>
        <input
          id="spotify-client-id"
          type="text"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder="e.g. 4a1b2c3d…"
          style={inputStyle}
          autoComplete="off"
          spellCheck={false}
        />
      </div>

      <div>
        <label style={labelStyle} htmlFor="spotify-client-secret">
          Client Secret
        </label>
        <input
          id="spotify-client-secret"
          type="password"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          placeholder="Write-only — never shown again"
          style={inputStyle}
          autoComplete="new-password"
          spellCheck={false}
        />
      </div>

      <button
        type="button"
        onClick={() => setShowHelp((v) => !v)}
        className="flex items-center gap-1.5 self-start"
        style={{
          background: "none",
          border: "none",
          padding: 0,
          cursor: "pointer",
          fontFamily: NUNITO,
          fontWeight: 700,
          fontSize: 13,
          color: "var(--text3)",
        }}
      >
        <MsIcon name={showHelp ? "expand_less" : "expand_more"} size={18} />
        Where do I get these?
      </button>
      {showHelp && (
        <ol
          style={{
            margin: 0,
            paddingLeft: 22,
            display: "flex",
            flexDirection: "column",
            gap: 6,
            fontFamily: NUNITO,
            fontWeight: 500,
            fontSize: 13.5,
            lineHeight: 1.5,
            color: "var(--text2)",
          }}
        >
          {HELP_STEPS.map((s, i) => (
            <li key={i}>
              {s.includes("http://") ? (
                <>
                  {s.split("http://127.0.0.1:3003/api/spotify/callback")[0]}
                  <code
                    style={{
                      fontFamily: "var(--font-jbmono), monospace",
                      fontSize: 12.5,
                      color: "var(--text)",
                    }}
                  >
                    http://127.0.0.1:3003/api/spotify/callback
                  </code>
                </>
              ) : (
                s
              )}
            </li>
          ))}
        </ol>
      )}

      <button
        type="button"
        onClick={onSave}
        disabled={busy || !clientId.trim() || !clientSecret.trim()}
        style={{
          ...primaryBtn,
          padding: "0 24px",
          opacity: busy || !clientId.trim() || !clientSecret.trim() ? 0.5 : 1,
        }}
        className="self-start"
      >
        Save &amp; Continue
      </button>
    </div>
  );
}
