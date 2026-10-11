"use client";

/**
 * The one-time Home Assistant provisioning credential, handed over on a form.
 *
 * The backend has POST /vault/secrets/ha_admin_token: it takes
 * an admin long-lived token, writes it into the vault under the PIN-derived key
 * (session tier), and never echoes or logs it. GET /vault/item/ha_admin_token.json
 * is 403, so there is no read-back path. This card replaces a hand-written
 * curl call with a form on the dashboard.
 *
 * Three deliberate choices:
 *
 *  - The card only exists while the vault is UNLOCKED, because the route only
 *    works with a live vault session. Rendering it on the locked page would be
 *    an invitation to type a credential into something that will 403.
 *
 *  - The input is UNCONTROLLED. A controlled input would park the token in React
 *    state for as long as the card is mounted; here the value lives in the DOM
 *    node, is read once at submit, and the field is cleared the moment the
 *    request resolves. Only a "is there anything typed" boolean is state.
 *
 *  - POST body only. Never a query string, never a path segment, so the token
 *    cannot reach an access log or a proxy URL. It rides the same /api/pai proxy
 *    and the same X-Vault-Token header as every other call on this page.
 *
 * There is no status route: ha_admin_token_status() exists in the backend but is
 * not wired to HTTP, so the card cannot say whether a credential is
 * already stored. Storing again simply replaces it.
 *
 * puff puff pass
 */

import { useRef, useState } from "react";
import { mdiShieldKeyOutline, mdiShieldCheck } from "@mdi/js";
import CardBox from "../../_components/CardBox";
import Button from "../../_components/Button";
import Icon from "../../_components/Icon";
import { API_URL } from "../../_lib/api";

export default function HaProvisioningCard({ vaultToken }: { vaultToken: string }) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [hasValue, setHasValue] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stored, setStored] = useState(false);
  const [error, setError] = useState("");

  const handleStore = async () => {
    const field = inputRef.current;
    const token = field ? field.value.trim() : "";
    if (!token) {
      setError("Paste the long-lived token first.");
      return;
    }
    setBusy(true);
    setError("");
    setStored(false);
    try {
      const res = await fetch(`${API_URL}/vault/secrets/ha_admin_token`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Vault-Token": vaultToken,
        },
        body: JSON.stringify({ token }),
      });
      const data: { success?: boolean; message?: string } = await res
        .json()
        .catch(() => ({}));
      if (res.ok && data.success) {
        setStored(true);
      } else {
        setError(data.message || "Could not store the credential.");
      }
    } catch {
      setError("Vault service unavailable.");
    } finally {
      // Clear the field whatever happened: on success it is in the vault, and
      // on failure leaving a credential sitting in a DOM node buys nothing.
      if (inputRef.current) inputRef.current.value = "";
      setHasValue(false);
      setBusy(false);
    }
  };

  return (
    <CardBox className="mb-6">
      <div className="mb-3 flex items-center gap-3">
        <Icon path={mdiShieldKeyOutline} size="20" className="text-purple-500" />
        <h3 className="text-sm font-semibold dark:text-white">
          Home Assistant provisioning credential
        </h3>
      </div>

      <p className="mb-4 text-sm text-gray-500 dark:text-slate-400">
        PuffPAI&apos;s everyday Home Assistant account can&apos;t change Home
        Assistant&apos;s own setup.
        <br />
        When you approve a setup step, it uses this credential from the Vault, then
        forgets it.
      </p>

      <label
        className="mb-1 block text-xs font-medium text-gray-500 dark:text-slate-400"
        htmlFor="ha-provisioning-token"
      >
        Long-lived access token
      </label>
      <input
        id="ha-provisioning-token"
        ref={inputRef}
        type="password"
        autoComplete="off"
        spellCheck={false}
        disabled={busy}
        onChange={(e) => setHasValue(e.target.value.trim().length > 0)}
        placeholder="Paste the token here"
        className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 font-mono text-sm focus:border-purple-400 focus:ring-1 focus:ring-purple-400 focus:outline-none disabled:opacity-60 dark:border-slate-600 dark:bg-slate-800 dark:text-white"
      />
      <p className="mt-2 text-xs text-gray-400 dark:text-slate-500">
        Create one in Home Assistant: your profile → Security → Long-lived access
        tokens → Create token, name it PuffPAI provisioning
      </p>

      {stored && (
        <p className="mt-3 text-sm text-green-600 dark:text-green-400">
          Stored. It&apos;s in the Vault; it&apos;s never shown again.
        </p>
      )}
      {error && <p className="mt-3 text-sm text-red-500">{error}</p>}

      <div className="mt-4">
        <Button
          label={busy ? "Storing..." : "Store in Vault"}
          icon={mdiShieldCheck}
          color="info"
          small
          onClick={handleStore}
          disabled={busy || !hasValue}
        />
      </div>
    </CardBox>
  );
}
