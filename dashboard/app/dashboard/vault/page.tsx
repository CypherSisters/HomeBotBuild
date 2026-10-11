"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import {
  mdiShieldLock,
  mdiLock,
  mdiLockOpen,
  mdiKey,
  mdiFileDocument,
  mdiShieldCheck,
  mdiClockOutline,
  mdiEye,
  mdiEyeOff,
  mdiEyeOutline,
  mdiDelete,
  mdiPlus,
  mdiAlertCircle,
  mdiRefresh,
  mdiClose,
  mdiShieldPlusOutline,
} from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import SectionTitleLineWithButton from "../../_components/Section/TitleLineWithButton";
import CardBox from "../../_components/CardBox";
import CardBoxWidget from "../../_components/CardBox/Widget";
import Icon from "../../_components/Icon";
import Button from "../../_components/Button";
import NotificationBar from "../../_components/NotificationBar";
import { useAppDispatch, useAppSelector } from "../../_stores/hooks";
import {
  unlock,
  lock,
  fetchContents,
  removeItem,
  clearVaultError,
} from "../../_stores/vaultSlice";
import { getVaultStatus } from "../../_lib/services/vaultService";
import { API_URL } from "../../_lib/api";
import HaProvisioningCard from "./HaProvisioningCard";

const AUTO_LOCK_SECONDS = 15 * 60; // 15 minutes
const MAX_ATTEMPTS = 5;
const LOCKOUT_SECONDS = 5 * 60; // 5 minutes
const MIN_PIN_LENGTH = 4;

/**
 * Vault lifecycle as far as the UI is concerned.
 *  - "checking"  we have not yet heard back from /vault/mfa-status
 *  - "new"       no vault has ever been created; nothing is locked
 *  - "existing"  a vault exists and needs a PIN (or is already unlocked)
 */
type SetupState = "checking" | "new" | "existing";

export default function VaultPage() {
  const dispatch = useAppDispatch();
  const {
    isUnlocked,
    contents,
    loading: vaultLoading,
    error: reduxError,
  } = useAppSelector((s) => s.vault);

  // Local UI state
  const [pin, setPin] = useState("");
  const [showPin, setShowPin] = useState(false);
  const [error, setError] = useState("");
  const [successBanner, setSuccessBanner] = useState(false);
  const [vaultToken, setVaultToken] = useState<string | null>(null);
  const [failedAttempts, setFailedAttempts] = useState(0);
  const [lockoutUntil, setLockoutUntil] = useState<number | null>(null);
  const [lockoutRemaining, setLockoutRemaining] = useState(0);
  const [shakeError, setShakeError] = useState(false);
  const [backendOffline, setBackendOffline] = useState(false);

  // Vault setup state
  const [setupState, setSetupState] = useState<SetupState>("checking");
  const [confirmPin, setConfirmPin] = useState("");
  const [createLoading, setCreateLoading] = useState(false);

  // Auto-lock timer
  const [autoLockRemaining, setAutoLockRemaining] = useState(AUTO_LOCK_SECONDS);
  const autoLockRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const unlockTimeRef = useRef<number>(0);
  const handleLockRef = useRef<() => void>(() => {});

  // Store item form
  const [showStoreForm, setShowStoreForm] = useState(false);
  const [storeKey, setStoreKey] = useState("");
  const [storeValue, setStoreValue] = useState("");
  const [storeType, setStoreType] = useState("text");
  const [storeLoading, setStoreLoading] = useState(false);
  const [storeError, setStoreError] = useState("");
  const [deleteLoading, setDeleteLoading] = useState<string | null>(null);

  // View item modal state
  const [viewKey, setViewKey] = useState<string | null>(null);
  const [viewContent, setViewContent] = useState<string>("");
  const [viewContentType, setViewContentType] = useState<string>("text");
  const [viewLoading, setViewLoading] = useState<boolean>(false);
  const [viewError, setViewError] = useState<string>("");

  const handleViewItem = useCallback(
    async (key: string) => {
      if (!vaultToken) {
        setError("Vault session expired — please unlock again.");
        return;
      }
      setViewKey(key);
      setViewLoading(true);
      setViewError("");
      setViewContent("");
      try {
        const headers: HeadersInit = {};
        headers["X-Vault-Token"] = vaultToken;
        const res = await fetch(
          `${API_URL}/vault/item/${encodeURIComponent(key)}`,
          { method: "GET", headers },
        );
        const data = await res.json();
        if (!res.ok || !data.success) {
          setViewError(data.message || "Failed to read item");
          return;
        }
        setViewContentType(data.content_type || "text");
        if (data.content_type === "json") {
          setViewContent(JSON.stringify(data.content, null, 2));
        } else {
          setViewContent(String(data.content ?? ""));
        }
      } catch (e) {
        setViewError(String(e));
      } finally {
        setViewLoading(false);
      }
    },
    [vaultToken],
  );

  const closeViewModal = useCallback(() => {
    setViewKey(null);
    setViewContent("");
    setViewError("");
    setViewContentType("text");
  }, []);

  // Determine on load whether a vault has ever been created. Without this
  // the page showed a PIN prompt for a vault that does not exist, which
  // reads as "you forgot a PIN" when in fact none was ever set.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await getVaultStatus();
        if (cancelled) return;
        if (status.vault_available === false) {
          setBackendOffline(true);
          setSetupState("existing");
        } else if (status.vault_initialized === false) {
          setSetupState("new");
        } else {
          // ``true``, or absent on older backends: assume a vault exists.
          // An unlock attempt returning needs_initialization corrects this.
          setSetupState("existing");
        }
      } catch {
        if (cancelled) return;
        setBackendOffline(true);
        setSetupState("existing");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Sync redux error to local error
  useEffect(() => {
    if (reduxError) {
      setError(reduxError);
      dispatch(clearVaultError());
    }
  }, [reduxError, dispatch]);

  // Lockout countdown
  useEffect(() => {
    if (!lockoutUntil) return;
    const id = setInterval(() => {
      const remaining = Math.max(0, Math.ceil((lockoutUntil - Date.now()) / 1000));
      setLockoutRemaining(remaining);
      if (remaining <= 0) {
        setLockoutUntil(null);
        setFailedAttempts(0);
        setError("");
      }
    }, 1000);
    return () => clearInterval(id);
  }, [lockoutUntil]);

  // Auto-lock countdown
  const startAutoLockTimer = useCallback(() => {
    if (autoLockRef.current) clearInterval(autoLockRef.current);
    unlockTimeRef.current = Date.now();
    setAutoLockRemaining(AUTO_LOCK_SECONDS);
    autoLockRef.current = setInterval(() => {
      const elapsed = Math.floor((Date.now() - unlockTimeRef.current) / 1000);
      const remaining = Math.max(0, AUTO_LOCK_SECONDS - elapsed);
      setAutoLockRemaining(remaining);
      if (remaining <= 0) {
        handleLockRef.current();
      }
    }, 1000);
  }, []);

  const stopAutoLockTimer = useCallback(() => {
    if (autoLockRef.current) {
      clearInterval(autoLockRef.current);
      autoLockRef.current = null;
    }
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => stopAutoLockTimer();
  }, [stopAutoLockTimer]);

  const formatTime = (seconds: number): string => {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${s.toString().padStart(2, "0")}`;
  };

  const fetchVaultContents = useCallback(
    async (token: string) => {
      try {
        // Pass the unlock session token through Redux so the list
        // actually populates — without this, getVaultContents gets a 403
        // and state.contents stays [].
        await dispatch(fetchContents(token)).unwrap();
      } catch {
        setError("Failed to load vault contents");
      }
    },
    [dispatch]
  );

  // Shared success path for both unlocking an existing vault and creating
  // a new one — /vault/unlock serves both, with `initialize: true` for setup.
  const openSession = useCallback(
    (sessionToken: string, enteredPin: string) => {
      setVaultToken(sessionToken || null);
      setFailedAttempts(0);
      setPin("");
      setConfirmPin("");
      setSetupState("existing");
      setSuccessBanner(true);

      // Dispatch to redux to sync state
      dispatch(unlock(enteredPin));

      // Fetch contents with token
      fetchVaultContents(sessionToken || "");

      // Start auto-lock timer
      startAutoLockTimer();

      // Hide success banner after 5 seconds
      setTimeout(() => setSuccessBanner(false), 5000);
    },
    [dispatch, fetchVaultContents, startAutoLockTimer],
  );

  const postUnlock = async (body: Record<string, unknown>) => {
    const headers: HeadersInit = { "Content-Type": "application/json" };

    const res = await fetch(`${API_URL}/vault/unlock`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    return res.json();
  };

  const handleUnlock = async () => {
    if (lockoutUntil && Date.now() < lockoutUntil) return;
    if (!pin.trim()) {
      setError("Please enter your PIN");
      return;
    }

    setError("");
    setBackendOffline(false);

    try {
      const data = await postUnlock({ pin });

      if (data.success) {
        openSession(data.session_token || "", pin);
        return;
      }

      // Backend telling us no vault exists yet. Switch to the setup view
      // rather than counting this as a failed PIN attempt.
      if (data.needs_initialization) {
        setSetupState("new");
        setError("");
        setPin("");
        return;
      }

      const newAttempts = failedAttempts + 1;
      setFailedAttempts(newAttempts);
      setError(data.message || data.error || "Invalid PIN");

      // Trigger shake animation
      setShakeError(true);
      setTimeout(() => setShakeError(false), 600);

      // Lockout after MAX_ATTEMPTS
      if (newAttempts >= MAX_ATTEMPTS) {
        const until = Date.now() + LOCKOUT_SECONDS * 1000;
        setLockoutUntil(until);
        setLockoutRemaining(LOCKOUT_SECONDS);
        setError("");
      }
    } catch {
      setBackendOffline(true);
      setError("Vault service unavailable");
    }
  };

  const handleCreateVault = async () => {
    if (pin.trim().length < MIN_PIN_LENGTH) {
      setError(`Choose a PIN of at least ${MIN_PIN_LENGTH} digits.`);
      return;
    }
    if (pin !== confirmPin) {
      setError("The two PINs do not match.");
      return;
    }

    setError("");
    setBackendOffline(false);
    setCreateLoading(true);

    try {
      const data = await postUnlock({ pin, initialize: true });
      if (data.success) {
        openSession(data.session_token || "", pin);
      } else {
        setError(data.message || data.error || "Could not create the vault");
      }
    } catch {
      setBackendOffline(true);
      setError("Vault service unavailable");
    } finally {
      setCreateLoading(false);
    }
  };

  const handleLock = useCallback(async () => {
    stopAutoLockTimer();
    setVaultToken(null);
    setSuccessBanner(false);
    setShowStoreForm(false);

    try {
      dispatch(lock());
    } catch {
      // Lock locally even if backend fails
    }
  }, [dispatch, stopAutoLockTimer]);

  // Keep the ref in sync so the interval can call the latest handleLock
  useEffect(() => {
    handleLockRef.current = handleLock;
  }, [handleLock]);

  const handleStoreItem = async () => {
    if (!storeKey.trim()) {
      setStoreError("Key is required");
      return;
    }
    if (!storeValue.trim()) {
      setStoreError("Value is required");
      return;
    }

    setStoreLoading(true);
    setStoreError("");

    try {
      const headers: HeadersInit = { "Content-Type": "application/json" };
      if (vaultToken) headers["X-Vault-Token"] = vaultToken;

      const res = await fetch(`${API_URL}/vault/store`, {
        method: "POST",
        headers,
        body: JSON.stringify({ key: storeKey, value: storeValue, type: storeType }),
      });

      const data = await res.json();
      if (data.success) {
        setStoreKey("");
        setStoreValue("");
        setStoreType("text");
        setShowStoreForm(false);
        // Refresh contents
        dispatch(fetchContents());
      } else {
        setStoreError(data.error || "Failed to store item");
      }
    } catch {
      setStoreError("Vault service unavailable");
    } finally {
      setStoreLoading(false);
    }
  };

  const handleDeleteItem = async (key: string) => {
    setDeleteLoading(key);
    try {
      const headers: HeadersInit = { "Content-Type": "application/json" };
      if (vaultToken) headers["X-Vault-Token"] = vaultToken;

      const res = await fetch(`${API_URL}/vault/item/${encodeURIComponent(key)}`, {
        method: "DELETE",
        headers,
      });

      const data = await res.json();
      if (data.success) {
        // Refetch the full list through the tokened thunk so Redux state
        // actually updates. DO NOT dispatch removeItem — that thunk calls
        // DELETE a second time without the vault token and 403s, which
        // leaves state.contents stale and makes the row look undeletable.
        if (vaultToken) {
          await dispatch(fetchContents(vaultToken));
        }
      } else {
        setError(data.message || data.error || "Failed to delete item");
      }
    } catch {
      setError("Vault service unavailable");
    } finally {
      setDeleteLoading(null);
    }
  };

  const handleRetry = () => {
    setBackendOffline(false);
    setError("");
  };

  const typeIcon = (type: string) => {
    switch (type) {
      case "credential":
        return mdiKey;
      case "json":
        return mdiFileDocument;
      default:
        return mdiFileDocument;
    }
  };

  const typeBadgeColor = (type: string) => {
    switch (type) {
      case "credential":
        return "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400";
      case "json":
        return "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400";
      default:
        return "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400";
    }
  };

  return (
    <SectionMain>
      <SectionTitleLineWithButton icon={mdiShieldLock} title="Vault (Zone 1)" main>
        {isUnlocked && (
          <Button
            label="Lock Vault"
            icon={mdiLock}
            color="danger"
            small
            roundedFull
            onClick={() => handleLock()}
          />
        )}
      </SectionTitleLineWithButton>

      {/* Status Widgets */}
      <div className="mb-6 grid grid-cols-1 gap-6 sm:grid-cols-3">
        <CardBoxWidget
          trendLabel={
            setupState === "new"
              ? "Not set up"
              : isUnlocked
                ? "Unlocked"
                : "Locked"
          }
          trendType={
            setupState === "new" ? "info" : isUnlocked ? "success" : "warning"
          }
          trendColor={
            setupState === "new" ? "info" : isUnlocked ? "success" : "warning"
          }
          icon={
            setupState === "new"
              ? mdiShieldPlusOutline
              : isUnlocked
                ? mdiLockOpen
                : mdiLock
          }
          iconColor={
            setupState === "new" ? "info" : isUnlocked ? "success" : "danger"
          }
          number={isUnlocked ? 1 : 0}
          label="Vault Status"
        />
        <CardBoxWidget
          trendLabel={
            setupState === "new"
              ? "None yet"
              : isUnlocked
                ? "Accessible"
                : "Encrypted"
          }
          trendType="info"
          trendColor="info"
          icon={mdiShieldCheck}
          iconColor="info"
          number={contents.length}
          label="Stored Items"
        />
        <CardBoxWidget
          trendLabel="AES-256-GCM"
          trendType="success"
          trendColor="success"
          icon={mdiKey}
          iconColor="warning"
          number={256}
          label="Encryption (bits)"
          numberSuffix="-bit"
        />
      </div>

      {/* Backend Offline Banner */}
      {backendOffline && (
        <NotificationBar color="danger" icon={mdiAlertCircle}>
          <div className="flex items-center gap-3">
            <span>
              <b>Vault service unavailable.</b> The backend may be offline or unreachable.
            </span>
            <Button label="Retry" icon={mdiRefresh} color="danger" small roundedFull onClick={handleRetry} />
          </div>
        </NotificationBar>
      )}

      {setupState === "checking" ? (
        /* --- Still reading vault state: say nothing about locks yet --- */
        <CardBox>
          <div className="mx-auto max-w-md py-10 text-center text-sm text-gray-500 dark:text-slate-400">
            <Icon path={mdiRefresh} size="28" className="mx-auto mb-3 animate-spin opacity-60" />
            <p>Checking vault status.</p>
          </div>
        </CardBox>
      ) : setupState === "new" ? (
        /* --- No vault has ever been created --- */
        <CardBox>
          <div className="mx-auto max-w-md py-8 text-center">
            <div className="mx-auto mb-6 flex h-20 w-20 items-center justify-center rounded-full bg-gradient-to-br from-purple-400 to-purple-700 text-white">
              <Icon path={mdiShieldPlusOutline} size="40" />
            </div>
            <h2 className="mb-2 text-xl font-bold dark:text-white">No vault yet</h2>
            <p className="mb-2 text-sm text-gray-500 dark:text-slate-400">
              This device has no vault. Nothing is locked and there is no PIN to
              remember yet. Set one now to start storing personal data encrypted
              with AES-256-GCM, kept on this device.
            </p>
            <p className="mb-6 text-xs text-gray-400 dark:text-slate-500">
              The PIN is the only key. It is not stored anywhere and cannot be
              recovered or reset, so keep a copy somewhere safe.
            </p>

            <div className="mx-auto max-w-xs space-y-3 text-left">
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-slate-400">
                  Choose a PIN
                </label>
                <div className="relative">
                  <input
                    type={showPin ? "text" : "password"}
                    value={pin}
                    onChange={(e) => {
                      setPin(e.target.value);
                      setError("");
                    }}
                    placeholder="At least 4 digits"
                    maxLength={8}
                    disabled={createLoading}
                    className="w-full rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-center text-lg tracking-widest focus:border-purple-400 focus:ring-1 focus:ring-purple-400 focus:outline-none disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-white"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPin(!showPin)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 dark:hover:text-slate-200"
                    aria-label={showPin ? "Hide PIN" : "Show PIN"}
                  >
                    <Icon path={showPin ? mdiEyeOff : mdiEye} size="20" />
                  </button>
                </div>
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-slate-400">
                  Confirm PIN
                </label>
                <input
                  type={showPin ? "text" : "password"}
                  value={confirmPin}
                  onChange={(e) => {
                    setConfirmPin(e.target.value);
                    setError("");
                  }}
                  onKeyDown={(e) => e.key === "Enter" && handleCreateVault()}
                  placeholder="Re-enter PIN"
                  maxLength={8}
                  disabled={createLoading}
                  className="w-full rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-center text-lg tracking-widest focus:border-purple-400 focus:ring-1 focus:ring-purple-400 focus:outline-none disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-white"
                />
              </div>
            </div>

            {error && !backendOffline && (
              <p className="mt-3 text-sm text-red-500">{error}</p>
            )}

            <div className="mt-5">
              <Button
                label={createLoading ? "Creating..." : "Create vault"}
                icon={mdiShieldPlusOutline}
                color="info"
                onClick={handleCreateVault}
                disabled={createLoading}
              />
            </div>
          </div>
        </CardBox>
      ) : !isUnlocked ? (
        /* --- PIN Entry --- */
        <CardBox>
          <div className="mx-auto max-w-md py-8 text-center">
            <div className="mx-auto mb-6 flex h-20 w-20 items-center justify-center rounded-full bg-gradient-to-br from-purple-400 to-purple-700 text-white">
              <Icon path={mdiLock} size="40" />
            </div>
            <h2 className="mb-2 text-xl font-bold dark:text-white">Vault is Locked</h2>
            <p className="mb-6 text-sm text-gray-500 dark:text-slate-400">
              Enter your PIN to access encrypted personal data in Zone 1. Your vault data is
              encrypted with AES-256-GCM and never leaves this device.
            </p>

            {/* Lockout Message */}
            {lockoutUntil && Date.now() < lockoutUntil ? (
              <div className="mb-6 rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-900/20">
                <div className="flex items-center justify-center gap-2 text-red-600 dark:text-red-400">
                  <Icon path={mdiAlertCircle} size="20" />
                  <span className="text-sm font-medium">
                    Too many failed attempts. Try again in {formatTime(lockoutRemaining)}.
                  </span>
                </div>
              </div>
            ) : (
              <>
                <div className={`mb-4 ${shakeError ? "animate-shake" : ""}`}>
                  <div className="relative mx-auto max-w-xs">
                    <input
                      type={showPin ? "text" : "password"}
                      value={pin}
                      onChange={(e) => {
                        setPin(e.target.value);
                        setError("");
                      }}
                      onKeyDown={(e) => e.key === "Enter" && handleUnlock()}
                      placeholder="Enter PIN"
                      maxLength={8}
                      disabled={vaultLoading}
                      className="w-full rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-center text-lg tracking-widest focus:border-purple-400 focus:ring-1 focus:ring-purple-400 focus:outline-none disabled:opacity-50 dark:border-slate-600 dark:bg-slate-800 dark:text-white"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPin(!showPin)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 dark:hover:text-slate-200"
                    >
                      <Icon path={showPin ? mdiEyeOff : mdiEye} size="20" />
                    </button>
                  </div>
                  {error && !backendOffline && (
                    <p className="mt-2 text-sm text-red-500">
                      {error}
                      {failedAttempts > 0 && failedAttempts < MAX_ATTEMPTS && (
                        <span className="ml-1 text-xs text-gray-400">
                          ({failedAttempts}/{MAX_ATTEMPTS} attempts)
                        </span>
                      )}
                    </p>
                  )}
                </div>

                <Button
                  label={vaultLoading ? "Unlocking..." : "Unlock Vault"}
                  icon={mdiLockOpen}
                  color="info"
                  onClick={handleUnlock}
                  disabled={vaultLoading}
                />
              </>
            )}
          </div>
        </CardBox>
      ) : (
        /* --- Unlocked Vault Contents --- */
        <>
          {/* Success Banner */}
          {successBanner && (
            <div className="mb-6 rounded-xl border border-green-200 bg-green-50 p-4 dark:border-green-800 dark:bg-green-900/20">
              <div className="flex items-center gap-3">
                <Icon path={mdiLockOpen} size="20" className="text-green-600 dark:text-green-400" />
                <span className="text-sm font-medium text-green-700 dark:text-green-400">
                  Vault unlocked successfully — Personal data is accessible for RAG queries.
                </span>
              </div>
            </div>
          )}

          {/* Auto-lock Timer */}
          <CardBox className="mb-6">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3 text-sm text-gray-500 dark:text-slate-400">
                <Icon path={mdiClockOutline} size="18" />
                <span>
                  Auto-lock in{" "}
                  <span
                    className={`font-mono font-semibold ${
                      autoLockRemaining <= 60
                        ? "text-red-500"
                        : autoLockRemaining <= 300
                          ? "text-yellow-500"
                          : "text-green-500"
                    }`}
                  >
                    {formatTime(autoLockRemaining)}
                  </span>
                </span>
              </div>
              {vaultToken && (
                <span className="rounded-full bg-purple-100 px-3 py-1 text-xs text-purple-700 dark:bg-purple-900/30 dark:text-purple-400">
                  Session active
                </span>
              )}
            </div>
          </CardBox>

          {/* One-time HA provisioning credential. Needs a live vault session,
              so it renders only once the unlock has handed us a token. */}
          {vaultToken && <HaProvisioningCard vaultToken={vaultToken} />}

          {/* Vault Contents Header + Add Button */}
          <SectionTitleLineWithButton icon={mdiFileDocument} title="Vault Contents">
            <Button
              label="Store Item"
              icon={mdiPlus}
              color="info"
              small
              roundedFull
              onClick={() => setShowStoreForm(!showStoreForm)}
            />
          </SectionTitleLineWithButton>

          {/* Store Item Form */}
          {showStoreForm && (
            <CardBox className="mb-6">
              <h3 className="mb-4 text-sm font-semibold dark:text-white">Store New Item</h3>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-slate-400">
                    Key / Name
                  </label>
                  <input
                    type="text"
                    value={storeKey}
                    onChange={(e) => setStoreKey(e.target.value)}
                    placeholder="e.g. ha_api_token"
                    className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm focus:border-purple-400 focus:ring-1 focus:ring-purple-400 focus:outline-none dark:border-slate-600 dark:bg-slate-800 dark:text-white"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-slate-400">
                    Type
                  </label>
                  <select
                    value={storeType}
                    onChange={(e) => setStoreType(e.target.value)}
                    className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm focus:border-purple-400 focus:ring-1 focus:ring-purple-400 focus:outline-none dark:border-slate-600 dark:bg-slate-800 dark:text-white"
                  >
                    <option value="text">Text</option>
                    <option value="json">JSON</option>
                    <option value="credential">Credential</option>
                  </select>
                </div>
              </div>
              <div className="mt-4">
                <label className="mb-1 block text-xs font-medium text-gray-500 dark:text-slate-400">
                  Value
                </label>
                <textarea
                  value={storeValue}
                  onChange={(e) => setStoreValue(e.target.value)}
                  placeholder="Enter the value to encrypt and store..."
                  rows={3}
                  className="w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm focus:border-purple-400 focus:ring-1 focus:ring-purple-400 focus:outline-none dark:border-slate-600 dark:bg-slate-800 dark:text-white"
                />
              </div>
              {storeError && <p className="mt-2 text-sm text-red-500">{storeError}</p>}
              <div className="mt-4 flex gap-2">
                <Button
                  label={storeLoading ? "Storing..." : "Encrypt & Store"}
                  icon={mdiShieldCheck}
                  color="info"
                  small
                  onClick={handleStoreItem}
                  disabled={storeLoading}
                />
                <Button
                  label="Cancel"
                  color="whiteDark"
                  small
                  onClick={() => {
                    setShowStoreForm(false);
                    setStoreError("");
                  }}
                />
              </div>
            </CardBox>
          )}

          {/* Contents Table */}
          <CardBox hasTable>
            <table>
              <thead>
                <tr>
                  <th>Item</th>
                  <th>Type</th>
                  <th>Size</th>
                  <th>Last Modified</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {contents.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="py-8 text-center text-gray-400 dark:text-slate-500">
                      <Icon path={mdiShieldLock} size="32" className="mx-auto mb-2 opacity-40" />
                      <p>No items stored in vault yet.</p>
                      <p className="text-xs">Use the &quot;Store Item&quot; button to add encrypted data.</p>
                    </td>
                  </tr>
                ) : (
                  contents.map((item) => {
                    // Infer a display type for items the API returns without one
                    // (raw doc_* files from the PII auto-vault pipeline).
                    const displayType =
                      item.type ||
                      (item.key.startsWith("doc_") ? "document" : "text");
                    const modifiedRaw = item.modified || item.last_modified || "";
                    const modifiedDisplay = modifiedRaw
                      ? new Date(modifiedRaw).toLocaleString()
                      : "—";
                    const sizeDisplay =
                      typeof item.size === "number"
                        ? `${(item.size / 1024).toFixed(1)} KB`
                        : String(item.size);
                    return (
                    <tr key={item.key}>
                      <td data-label="Item" className="flex items-center gap-2">
                        <Icon path={typeIcon(displayType)} size="18" className="text-purple-400" />
                        <button
                          type="button"
                          className="font-medium dark:text-white text-left hover:text-blue-400 hover:underline transition-colors"
                          onClick={() => handleViewItem(item.key)}
                          title="Click to view contents"
                        >
                          {item.key}
                        </button>
                      </td>
                      <td data-label="Type">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs ${typeBadgeColor(displayType)}`}
                        >
                          {displayType}
                        </span>
                      </td>
                      <td data-label="Size">{sizeDisplay}</td>
                      <td data-label="Last Modified">{modifiedDisplay}</td>
                      <td data-label="Actions">
                        <div className="flex gap-2">
                          <Button
                            icon={mdiEyeOutline}
                            color="info"
                            small
                            onClick={() => handleViewItem(item.key)}
                            title="View contents"
                          />
                          <Button
                            icon={mdiDelete}
                            color="danger"
                            small
                            disabled={deleteLoading === item.key}
                            onClick={() => handleDeleteItem(item.key)}
                            title="Delete item"
                          />
                        </div>
                      </td>
                    </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </CardBox>
        </>
      )}

      {/* ── View Item Modal ────────────────────────────────────────────── */}
      {viewKey && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
          onClick={closeViewModal}
        >
          <div
            className="w-full max-w-4xl max-h-[85vh] flex flex-col bg-white dark:bg-slate-900 rounded-lg shadow-2xl border border-slate-200 dark:border-slate-700"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-center justify-between border-b border-slate-200 dark:border-slate-700 px-6 py-4">
              <div className="flex items-center gap-3 min-w-0">
                <Icon path={mdiShieldLock} size="20" className="text-purple-500 shrink-0" />
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
                    Vault Item
                  </h3>
                  <p className="text-base font-medium text-slate-900 dark:text-white truncate">
                    {viewKey}
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={closeViewModal}
                className="p-2 rounded hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-500 dark:text-slate-400"
                aria-label="Close"
              >
                <Icon path={mdiClose} size="20" />
              </button>
            </div>

            {/* Body */}
            <div className="flex-1 overflow-auto p-6">
              {viewLoading && (
                <div className="text-center py-8 text-slate-500 dark:text-slate-400">
                  <Icon path={mdiRefresh} size="32" className="mx-auto mb-2 animate-spin opacity-60" />
                  <p className="text-sm">Decrypting and loading content…</p>
                </div>
              )}
              {!viewLoading && viewError && (
                <div className="p-4 rounded bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 text-red-800 dark:text-red-300">
                  <div className="flex items-center gap-2 mb-1 font-semibold">
                    <Icon path={mdiAlertCircle} size="18" />
                    Could not load item
                  </div>
                  <div className="text-sm">{viewError}</div>
                </div>
              )}
              {!viewLoading && !viewError && (
                <pre className="text-xs md:text-sm font-mono whitespace-pre-wrap break-words bg-slate-50 dark:bg-slate-950 text-slate-900 dark:text-slate-100 p-4 rounded border border-slate-200 dark:border-slate-800">
                  {viewContent}
                </pre>
              )}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-between border-t border-slate-200 dark:border-slate-700 px-6 py-3 bg-slate-50 dark:bg-slate-950/50 rounded-b-lg">
              <div className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                <Icon path={mdiShieldCheck} size="14" className="text-emerald-500" />
                <span>
                  {viewContentType === "json" ? "Structured JSON" : "Plain text"} ·
                  Decrypted locally · Never left this device
                </span>
              </div>
              <Button color="info" small label="Close" onClick={closeViewModal} />
            </div>
          </div>
        </div>
      )}

      {/* Shake animation style */}
      <style jsx>{`
        @keyframes shake {
          0%,
          100% {
            transform: translateX(0);
          }
          10%,
          30%,
          50%,
          70%,
          90% {
            transform: translateX(-4px);
          }
          20%,
          40%,
          60%,
          80% {
            transform: translateX(4px);
          }
        }
        .animate-shake {
          animation: shake 0.5s ease-in-out;
        }
      `}</style>
    </SectionMain>
  );
}
