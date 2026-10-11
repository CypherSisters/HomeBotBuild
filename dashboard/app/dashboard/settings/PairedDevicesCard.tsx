"use client";

/**
 * Paired devices — the household's view of who can drive the house.
 *
 * Backend contract (all same-origin, all paired-only):
 *   GET  /api/pair/devices          → {devices:[{id,name,createdAt,lastSeenAt}],
 *                                      currentDeviceId}
 *   POST /api/pair/devices/rename   {id, name} → {ok}
 *   POST /api/pair/devices/revoke   {id}       → {ok, self}
 *
 * WHY THIS SCREEN EXISTS
 * ----------------------
 * Until now the only revocation lever was deleting the pairing state file on
 * the appliance, which mints a new secret and un-pairs the wall display and
 * every phone at once. A household that wanted to drop one old laptop had to
 * take everything down and re-pair it. Per-device rows make revocation a
 * one-tap, one-device act.
 *
 * The card also carries the pairing code + QR (<PairDeviceCard />). That was
 * previously only on the Knowledge page, which is a strange place to look for
 * "how do I add a device" — Settings is where people go.
 *
 * SECURITY NOTES
 * --------------
 *  - Device ids shown here are public handles, not credentials. Nothing on this
 *    screen is derived from the HMAC secret or from any cookie value.
 *  - Names come back from the server already sanitised, and React escapes them
 *    on render regardless; nothing here uses dangerouslySetInnerHTML.
 *  - Revoke is two-step by design. It is irreversible for the device in
 *    question (it has to re-pair with a code), and on the kiosk a stray tap is
 *    a real hazard — the confirm step is what stands between a passing elbow
 *    and a wall display that needs someone to walk over with a code.
 *  - A caller holding a legacy v1 cookie has no device id, so it cannot show
 *    "this device" and says so plainly instead of guessing.
 */

import { useCallback, useEffect, useState } from "react";
import MsIcon from "../../_components/MsIcon";
import PairDeviceCard from "../../_components/PairDeviceCard";

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

const microLabel: React.CSSProperties = {
  fontFamily: "var(--font-jbmono), monospace",
  fontWeight: 700,
  fontSize: 11,
  letterSpacing: ".14em",
  textTransform: "uppercase",
  color: "var(--text3)",
};

const rowStyle: React.CSSProperties = {
  padding: "14px 16px",
  borderRadius: 20,
  background: "var(--card2)",
  border: "1px solid var(--line)",
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  height: 40,
  padding: "0 12px",
  borderRadius: 12,
  border: "1px solid var(--line)",
  background: "var(--card)",
  color: "var(--text)",
  fontFamily: NUNITO,
  fontWeight: 600,
  fontSize: 15,
  outline: "none",
};

const iconBtn: React.CSSProperties = {
  height: 34,
  padding: "0 12px",
  borderRadius: 99,
  border: "1px solid var(--line)",
  background: "var(--card)",
  color: "var(--text2)",
  fontFamily: NUNITO,
  fontWeight: 700,
  fontSize: 13,
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
};

const dangerBtn: React.CSSProperties = {
  ...iconBtn,
  border: "1px solid var(--magenta)",
  color: "#fff",
  background: "linear-gradient(140deg,var(--pink),var(--magenta))",
};

interface PairedDevice {
  id: string;
  name: string;
  createdAt: string;
  lastSeenAt: string | null;
}

type LoadState = "loading" | "ready" | "locked" | "error";

/** "today" / "yesterday" / a plain date. Never a raw ISO string. */
function friendlyDay(iso: string | null): string {
  if (!iso) return "not yet";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "unknown";
  const then = new Date(t);
  const now = new Date();
  const days = Math.floor(
    (new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() -
      new Date(
        then.getFullYear(),
        then.getMonth(),
        then.getDate(),
      ).getTime()) /
      86_400_000,
  );
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  return then.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function shortDate(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "unknown";
  return new Date(t).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export default function PairedDevicesCard() {
  const [devices, setDevices] = useState<PairedDevice[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  /** Row currently being renamed, and its draft text. */
  const [editId, setEditId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  /** Row awaiting the second tap of the two-step revoke. */
  const [confirmId, setConfirmId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/pair/devices", { cache: "no-store" });
      if (res.status === 401) {
        setState("locked");
        return;
      }
      if (!res.ok) {
        setState("error");
        return;
      }
      const data = (await res.json()) as {
        devices?: PairedDevice[];
        currentDeviceId?: string | null;
      };
      setDevices(Array.isArray(data?.devices) ? data.devices : []);
      setCurrentId(data?.currentDeviceId ?? null);
      setState("ready");
    } catch {
      setState("error");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const saveName = useCallback(
    async (id: string) => {
      const name = draft.trim();
      if (!name) return;
      setBusyId(id);
      setNote(null);
      try {
        const res = await fetch("/api/pair/devices/rename", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id, name }),
        });
        if (!res.ok) {
          setNote("That rename didn't stick. Try again.");
        } else {
          setEditId(null);
          await load();
        }
      } catch {
        setNote("That rename didn't stick. Try again.");
      } finally {
        setBusyId(null);
      }
    },
    [draft, load],
  );

  const revoke = useCallback(
    async (id: string) => {
      setBusyId(id);
      setNote(null);
      try {
        const res = await fetch("/api/pair/devices/revoke", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id }),
        });
        if (!res.ok) {
          setNote("Couldn't remove that device. Try again.");
          return;
        }
        const data = (await res.json()) as { self?: boolean };
        setConfirmId(null);
        if (data?.self) {
          // This screen just removed itself. Reloading lands on the pairing
          // prompt, which is the honest thing to show.
          window.location.reload();
          return;
        }
        await load();
      } catch {
        setNote("Couldn't remove that device. Try again.");
      } finally {
        setBusyId(null);
      }
    },
    [load],
  );

  if (state === "locked") {
    return (
      <div style={cardStyle}>
        <div
          style={{
            fontFamily: FREDOKA,
            fontWeight: 600,
            fontSize: 24,
            color: "var(--text)",
          }}
        >
          Paired devices
        </div>
        <p
          style={{
            margin: "10px 0 0",
            fontFamily: NUNITO,
            fontWeight: 500,
            fontSize: 15,
            color: "var(--text3)",
          }}
        >
          Pair this screen to see and manage the household&rsquo;s devices.
        </p>
      </div>
    );
  }

  return (
    <div style={cardStyle}>
      <div
        style={{
          fontFamily: FREDOKA,
          fontWeight: 600,
          fontSize: 24,
          color: "var(--text)",
          marginBottom: 6,
        }}
      >
        Paired devices
      </div>
      <p
        style={{
          margin: "0 0 16px",
          fontFamily: NUNITO,
          fontWeight: 500,
          fontSize: 15,
          color: "var(--text2)",
        }}
      >
        Every screen and phone that can talk to PuffPAI. Remove one and it stops
        working straight away.
      </p>

      {state === "loading" && (
        <div style={{ ...microLabel, padding: "8px 2px" }}>Loading…</div>
      )}
      {state === "error" && (
        <div
          style={{
            ...rowStyle,
            fontFamily: NUNITO,
            fontSize: 15,
            color: "var(--text2)",
          }}
        >
          Device list unavailable right now.
        </div>
      )}

      {state === "ready" && (
        <div className="flex flex-col gap-2.5">
          {devices.length === 0 && (
            <div
              style={{
                ...rowStyle,
                fontFamily: NUNITO,
                fontSize: 15,
                color: "var(--text2)",
              }}
            >
              No devices listed yet.
            </div>
          )}

          {devices.map((d) => {
            const isCurrent = currentId !== null && d.id === currentId;
            const editing = editId === d.id;
            const confirming = confirmId === d.id;
            const busy = busyId === d.id;
            return (
              <div key={d.id} style={rowStyle}>
                <div className="flex items-center gap-3">
                  <MsIcon
                    name={isCurrent ? "smartphone" : "devices"}
                    size={22}
                    style={{ color: isCurrent ? "var(--pink)" : "var(--text3)" }}
                  />

                  <div className="min-w-0 flex-1">
                    {editing ? (
                      <input
                        style={inputStyle}
                        value={draft}
                        maxLength={64}
                        autoFocus
                        aria-label={`Rename ${d.name || "device"}`}
                        onChange={(e) => setDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") void saveName(d.id);
                          if (e.key === "Escape") setEditId(null);
                        }}
                      />
                    ) : (
                      <>
                        <div
                          className="truncate"
                          style={{
                            fontFamily: NUNITO,
                            fontWeight: 700,
                            fontSize: 16,
                            color: "var(--text)",
                          }}
                        >
                          {d.name || "Unnamed device"}
                          {isCurrent && (
                            <span
                              style={{
                                marginLeft: 8,
                                padding: "2px 10px",
                                borderRadius: 99,
                                background: "var(--chip)",
                                color: "var(--pink)",
                                fontFamily: "var(--font-jbmono), monospace",
                                fontSize: 10,
                                letterSpacing: ".12em",
                                textTransform: "uppercase",
                              }}
                            >
                              This device
                            </span>
                          )}
                        </div>
                        <div
                          style={{
                            fontFamily: NUNITO,
                            fontWeight: 500,
                            fontSize: 13,
                            color: "var(--text3)",
                          }}
                        >
                          Paired {shortDate(d.createdAt)} · last seen{" "}
                          {friendlyDay(d.lastSeenAt)}
                        </div>
                      </>
                    )}
                  </div>

                  <div className="flex shrink-0 items-center gap-2">
                    {editing ? (
                      <>
                        <button
                          type="button"
                          style={iconBtn}
                          disabled={busy}
                          onClick={() => void saveName(d.id)}
                        >
                          <MsIcon name="check" size={16} />
                          Save
                        </button>
                        <button
                          type="button"
                          style={iconBtn}
                          onClick={() => setEditId(null)}
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          style={iconBtn}
                          aria-label={`Rename ${d.name || "device"}`}
                          onClick={() => {
                            setConfirmId(null);
                            setEditId(d.id);
                            setDraft(d.name);
                          }}
                        >
                          <MsIcon name="edit" size={16} />
                        </button>
                        {confirming ? (
                          <>
                            <button
                              type="button"
                              style={dangerBtn}
                              disabled={busy}
                              onClick={() => void revoke(d.id)}
                            >
                              Confirm removal?
                            </button>
                            <button
                              type="button"
                              style={iconBtn}
                              onClick={() => setConfirmId(null)}
                            >
                              Keep
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            style={iconBtn}
                            onClick={() => {
                              setEditId(null);
                              setConfirmId(d.id);
                            }}
                          >
                            Remove
                          </button>
                        )}
                      </>
                    )}
                  </div>
                </div>

                {confirming && isCurrent && (
                  <div
                    style={{
                      marginTop: 10,
                      padding: "10px 12px",
                      borderRadius: 14,
                      background: "var(--chip)",
                      border: "1px solid var(--magenta)",
                      fontFamily: NUNITO,
                      fontWeight: 700,
                      fontSize: 14,
                      color: "var(--text)",
                    }}
                  >
                    This is the screen you are using. Removing it signs this
                    screen out — you will need the pairing code to get back in.
                  </div>
                )}
              </div>
            );
          })}

          {currentId === null && devices.length > 0 && (
            <p
              style={{
                margin: "2px 4px 0",
                fontFamily: NUNITO,
                fontWeight: 500,
                fontSize: 13,
                color: "var(--text3)",
              }}
            >
              This screen is still using an older pairing, so it isn&rsquo;t
              marked above. It will appear in the list on its own after the next
              page load.
            </p>
          )}

          {note && (
            <p
              style={{
                margin: "2px 4px 0",
                fontFamily: NUNITO,
                fontWeight: 600,
                fontSize: 13,
                color: "var(--magenta)",
              }}
            >
              {note}
            </p>
          )}
        </div>
      )}

      {/* Add a device: the household code + QR, previously only on Knowledge. */}
      <div style={{ ...microLabel, margin: "20px 4px 0" }}>Add a device</div>
      <PairDeviceCard />
    </div>
  );
}
