"use client";

import { useState, useEffect, useCallback } from "react";
import {
  mdiAccount,
  mdiRobot,
  mdiKey,
  mdiLanConnect,
  mdiCheckCircle,
  mdiCloseCircle,
  mdiRefresh,
  mdiLoading,
} from "@mdi/js";
import SectionMain from "../../_components/Section/Main";
import SectionTitleLineWithButton from "../../_components/Section/TitleLineWithButton";
import CardBox from "../../_components/CardBox";
import CardBoxUser from "../../_components/CardBox/User";
import Icon from "../../_components/Icon";
import Button from "../../_components/Button";
import ProfileForm from "./_components/ProfileForm";
import AICustomizationForm from "./_components/AICustomizationForm";
import { getProfile, getAISettings } from "../../_lib/services/profileService";
import { checkHealth, API_URL } from "../../_lib/api";
import type { UserProfile, AISettings } from "../../_interfaces/api";
import ThemePicker, { saveMemberTheme } from "../_components/ThemePicker";
import { useHousehold } from "../../_lib/hooks/useHousehold";
import { normalizeChoice, type ThemeChoice } from "../../_lib/theme";

export default function ProfilePage() {
  // --- Profile & AI settings state ---
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [aiSettings, setAiSettings] = useState<AISettings | null>(null);
  const [loadingProfile, setLoadingProfile] = useState(true);
  const [loadingAI, setLoadingAI] = useState(true);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [aiError, setAiError] = useState<string | null>(null);

  // --- Connection state ---
  // There is deliberately no API-key state here. The dashboard server holds the
  // backend key (PUFFPAI_API_KEY) and attaches it in the /api/pai proxy; the
  // browser never receives or stores it, so there is no key card to fill in.
  const [testingConnection, setTestingConnection] = useState(false);
  const [connectionOk, setConnectionOk] = useState<boolean | null>(null);
  const [lastSync, setLastSync] = useState<string | null>(null);

  // --- Fetch profile ---
  const fetchProfile = useCallback(async () => {
    setLoadingProfile(true);
    setProfileError(null);
    try {
      const data = await getProfile();
      setProfile(data);
      setLastSync(new Date().toLocaleTimeString());
    } catch (err: any) {
      setProfileError(err.message || "Failed to load profile");
    } finally {
      setLoadingProfile(false);
    }
  }, []);

  // --- Fetch AI settings ---
  const fetchAISettings = useCallback(async () => {
    setLoadingAI(true);
    setAiError(null);
    try {
      const data = await getAISettings();
      setAiSettings(data);
    } catch (err: any) {
      setAiError(err.message || "Failed to load AI settings");
    } finally {
      setLoadingAI(false);
    }
  }, []);

  useEffect(() => {
    fetchProfile();
    fetchAISettings();
  }, [fetchProfile, fetchAISettings]);

  // --- Test connection ---
  const handleTestConnection = async () => {
    setTestingConnection(true);
    const healthy = await checkHealth();
    setConnectionOk(healthy);
    if (healthy) {
      setLastSync(new Date().toLocaleTimeString());
    }
    setTestingConnection(false);
  };

  // --- Callbacks from child forms to refresh data ---
  const onProfileSaved = (updated: UserProfile) => {
    setProfile(updated);
    setLastSync(new Date().toLocaleTimeString());
  };

  const onAISettingsSaved = (updated: AISettings) => {
    setAiSettings(updated);
    setLastSync(new Date().toLocaleTimeString());
  };

  return (
    <SectionMain>
      {/* User Profile Section */}
      <SectionTitleLineWithButton icon={mdiAccount} title="Your Profile" main />

      <CardBoxUser className="mb-6" />

      <ThemeCard />

      <div className="mb-10 grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Profile Form */}
        {loadingProfile ? (
          <CardBox>
            <div className="animate-pulse space-y-4 p-4">
              <div className="h-4 w-24 rounded bg-gray-200 dark:bg-slate-700" />
              <div className="h-10 rounded bg-gray-200 dark:bg-slate-700" />
              <div className="h-4 w-24 rounded bg-gray-200 dark:bg-slate-700" />
              <div className="h-10 rounded bg-gray-200 dark:bg-slate-700" />
            </div>
          </CardBox>
        ) : profileError ? (
          <CardBox>
            <div className="flex flex-col items-center py-8 text-center">
              <Icon path={mdiCloseCircle} size="36" className="mb-2 text-red-400" />
              <p className="mb-2 text-sm text-gray-500">{profileError}</p>
              <Button label="Retry" icon={mdiRefresh} color="info" small onClick={fetchProfile} />
            </div>
          </CardBox>
        ) : (
          <ProfileForm profile={profile!} onSaved={onProfileSaved} />
        )}

        {/* No change-password card: this build has no accounts or passwords.
            Devices are trusted by pairing instead (Settings > paired
            devices). */}
      </div>

      {/* API Key & Connection Section */}
      <SectionTitleLineWithButton icon={mdiKey} title="API Connection" main />

      <div className="mb-10 grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Backend connection. No key entry: see the note on the state above. */}
        <CardBox>
          <h3 className="mb-4 flex items-center gap-2 font-semibold text-[#0C2F44] dark:text-white">
            <Icon path={mdiKey} size="20" className="text-[#00B4D8]" />
            Backend Connection
          </h3>

          <p className="mb-4 text-sm text-gray-600 dark:text-slate-400">
            The backend key lives on the dashboard server and is attached to every
            request there. Your browser never receives it, so there is nothing on
            this page to copy, paste or leak.
          </p>

          <div className="flex gap-2">
            <Button
              label={testingConnection ? "Testing..." : "Test Connection"}
              icon={testingConnection ? mdiLoading : mdiLanConnect}
              color="info"
              small
              outline
              onClick={handleTestConnection}
              disabled={testingConnection}
            />
          </div>

          {connectionOk !== null && (
            <div
              className={`mt-3 flex items-center gap-2 rounded-lg p-2 text-sm ${
                connectionOk
                  ? "bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-400"
                  : "bg-red-50 text-red-700 dark:bg-red-900/20 dark:text-red-400"
              }`}
            >
              <Icon
                path={connectionOk ? mdiCheckCircle : mdiCloseCircle}
                size="16"
              />
              {connectionOk
                ? "Connection successful! Backend is reachable."
                : "Connection failed. Check that the backend is running."}
            </div>
          )}
        </CardBox>

        {/* Connection Status */}
        <CardBox>
          <h3 className="mb-4 flex items-center gap-2 font-semibold text-[#0C2F44] dark:text-white">
            <Icon path={mdiLanConnect} size="20" className="text-[#00B4D8]" />
            Connection Status
          </h3>

          <div className="space-y-3">
            <div className="flex items-center justify-between rounded-lg border border-gray-100 bg-gray-50/50 p-3 dark:border-slate-700 dark:bg-slate-800/50">
              <span className="text-sm text-gray-600 dark:text-slate-400">Backend URL</span>
              <span className="font-mono text-sm text-[#0C2F44] dark:text-white">{API_URL}</span>
            </div>

            <div className="flex items-center justify-between rounded-lg border border-gray-100 bg-gray-50/50 p-3 dark:border-slate-700 dark:bg-slate-800/50">
              <span className="text-sm text-gray-600 dark:text-slate-400">Status</span>
              <div className="flex items-center gap-2">
                <div
                  className={`h-2.5 w-2.5 rounded-full ${
                    connectionOk === true
                      ? "bg-green-500"
                      : connectionOk === false
                        ? "bg-red-500"
                        : "bg-gray-300"
                  }`}
                />
                <span className="text-sm font-medium dark:text-slate-300">
                  {connectionOk === true
                    ? "Connected"
                    : connectionOk === false
                      ? "Disconnected"
                      : "Unknown"}
                </span>
              </div>
            </div>

            <div className="flex items-center justify-between rounded-lg border border-gray-100 bg-gray-50/50 p-3 dark:border-slate-700 dark:bg-slate-800/50">
              <span className="text-sm text-gray-600 dark:text-slate-400">Last Sync</span>
              <span className="text-sm dark:text-slate-300">{lastSync || "Never"}</span>
            </div>
          </div>

          <div className="mt-4">
            <Button
              label="Refresh"
              icon={mdiRefresh}
              color="info"
              small
              outline
              onClick={() => {
                handleTestConnection();
                fetchProfile();
                fetchAISettings();
              }}
            />
          </div>
        </CardBox>
      </div>

      {/* AI Customization Section */}
      <SectionTitleLineWithButton icon={mdiRobot} title="AI Personality" main />

      {loadingAI ? (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <CardBox>
            <div className="animate-pulse space-y-4 p-4">
              <div className="mx-auto h-28 w-28 rounded-full bg-gray-200 dark:bg-slate-700" />
              <div className="h-4 w-24 rounded bg-gray-200 dark:bg-slate-700" />
              <div className="h-10 rounded bg-gray-200 dark:bg-slate-700" />
            </div>
          </CardBox>
          <CardBox>
            <div className="animate-pulse space-y-4 p-4">
              <div className="h-4 w-32 rounded bg-gray-200 dark:bg-slate-700" />
              <div className="h-10 rounded bg-gray-200 dark:bg-slate-700" />
              <div className="h-4 w-24 rounded bg-gray-200 dark:bg-slate-700" />
              <div className="h-10 rounded bg-gray-200 dark:bg-slate-700" />
            </div>
          </CardBox>
        </div>
      ) : aiError ? (
        <CardBox>
          <div className="flex flex-col items-center py-8 text-center">
            <Icon path={mdiCloseCircle} size="36" className="mb-2 text-red-400" />
            <p className="mb-2 text-sm text-gray-500">{aiError}</p>
            <Button
              label="Retry"
              icon={mdiRefresh}
              color="info"
              small
              onClick={fetchAISettings}
            />
          </div>
        </CardBox>
      ) : (
        <AICustomizationForm settings={aiSettings!} onSaved={onAISettingsSaved} />
      )}
    </SectionMain>
  );
}

/**
 * The owner’s palette. Sits directly under their avatar card because that
 * is what "your profile" means here: the name, the face, the theme.
 *
 * It repaints this browser only when the owner is also the active profile -
 * otherwise the kiosk’s active member owns the ground, and ThemeSync would
 * undo the flip on the next poll anyway.
 */
function ThemeCard() {
  const { owner, active, refresh } = useHousehold();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const choice = normalizeChoice(owner?.preferences?.theme);

  const pick = async (next: ThemeChoice) => {
    if (!owner || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await saveMemberTheme(owner.id, next, active?.id === owner.id);
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "That didn’t save.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <CardBox className="mb-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2 p-4 pb-2">
        <span
          style={{
            font: "700 17px var(--font-nunito), sans-serif",
            color: "var(--text)",
          }}
        >
          Theme
        </span>
        <span
          style={{
            font: "600 12px var(--font-nunito), sans-serif",
            color: err ? "var(--pink)" : "var(--text3)",
          }}
        >
          {err ?? "Follows you to every screen you are active on"}
        </span>
      </div>
      <div className="p-4 pt-0">
        <ThemePicker value={choice} onPick={pick} busy={busy || !owner} />
      </div>
    </CardBox>
  );
}
