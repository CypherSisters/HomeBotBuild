"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  getAppData,
  setAppData,
  appAction,
  getAppUpdateStatus,
  type AppName,
} from "../services/appService";

type UseAppDataReturn<T> = {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => Promise<void>;
  update: (newData: T) => Promise<void>;
  runAction: (action: string, params?: Record<string, unknown>) => Promise<unknown>;
};

/**
 * useAppData
 *
 * Loads app data on mount and lightly polls for updates so PuffPAI agent
 * actions (timers, tasks, etc. created via chat) appear in the dashboard
 * without a manual refresh.
 *
 * Polling strategy:
 *   - Hit /apps/<name>/updated every 2s (cheap, just returns last_updated)
 *   - Only reload the full payload when last_updated changes
 *   - Pause polling when the tab is hidden to save battery
 */
export function useAppData<T = any>(appName: AppName): UseAppDataReturn<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const lastUpdatedRef = useRef<string | null>(null);

  const reload = useCallback(async () => {
    setError(null);
    try {
      const result = await getAppData(appName);
      setData(result);
      // Track last_updated so polling knows when to refetch
      if (result && typeof result === "object" && "last_updated" in result) {
        lastUpdatedRef.current = (result as any).last_updated;
      }
    } catch (err: any) {
      setError(err.message || "Failed to load app data");
    } finally {
      setLoading(false);
    }
  }, [appName]);

  const update = useCallback(
    async (newData: T) => {
      try {
        await setAppData(appName, newData);
        setData(newData);
        if (newData && typeof newData === "object" && "last_updated" in newData) {
          lastUpdatedRef.current = (newData as any).last_updated;
        }
      } catch (err: any) {
        setError(err.message || "Failed to update app data");
        throw err;
      }
    },
    [appName],
  );

  const runAction = useCallback(
    async (action: string, params?: Record<string, unknown>) => {
      try {
        const result = await appAction(appName, action, params);
        await reload();
        return result;
      } catch (err: any) {
        setError(err.message || "Action failed");
        throw err;
      }
    },
    [appName, reload],
  );

  // Initial load
  useEffect(() => {
    reload();
  }, [reload]);

  // Household edits change the `family` list the calendar and FitFam render
  // from. The backend syncs those app stores, and the 2 s poll below would
  // pick it up on the next last_updated bump — but only if the backend bumps
  // it. This makes the refresh immediate and unconditional for the two apps
  // that carry a family list, so a newly added member shows in the calendar
  // without a reload. Fired by the household page after every mutation.
  useEffect(() => {
    if (appName !== "calendar" && appName !== "fitfam") return;
    const onChanged = () => {
      reload();
    };
    window.addEventListener("puffpai:household-changed", onChanged);
    return () =>
      window.removeEventListener("puffpai:household-changed", onChanged);
  }, [appName, reload]);

  // Lightweight polling for external updates (e.g. PuffPAI chat actions)
  useEffect(() => {
    let cancelled = false;
    let intervalId: ReturnType<typeof setInterval> | null = null;

    const poll = async () => {
      if (cancelled) return;
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        const status = await getAppUpdateStatus(appName);
        if (cancelled) return;
        const newStamp = status?.last_updated;
        if (newStamp && newStamp !== lastUpdatedRef.current) {
          await reload();
        }
      } catch {
        // Network blip — ignore and try next tick
      }
    };

    intervalId = setInterval(poll, 2000);

    const handleVisibility = () => {
      if (!document.hidden) {
        // Tab returned to focus — refresh immediately
        poll();
      }
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", handleVisibility);
    }

    return () => {
      cancelled = true;
      if (intervalId) clearInterval(intervalId);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", handleVisibility);
      }
    };
  }, [appName, reload]);

  return { data, loading, error, reload, update, runAction };
}
