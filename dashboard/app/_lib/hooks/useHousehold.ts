"use client";

import { useCallback, useSyncExternalStore } from "react";
import {
  getHousehold,
  type HouseholdMember,
  type HouseholdPayload,
} from "../services/householdService";

/**
 * useHousehold — ONE identity source for the whole client.
 *
 * A module-level store rather than per-component state: the operator chip, the
 * welcome card, the dashboard greeting and the household page all want the same
 * roster, and three of them are mounted at once on every page. One fetch, one
 * poll, one cache; components subscribe through useSyncExternalStore.
 *
 * Polling is slow on purpose (30 s). The roster changes when a human edits it,
 * and every editing path calls refreshHousehold() itself, so the timer exists
 * only to catch changes made from another surface (voice, a second browser).
 *
 * `bust` is a monotonic counter appended to avatar URLs. The backend serves a
 * member's picture from one stable path, so after an upload the browser would
 * otherwise keep showing the old cached image.
 */

export type HouseholdSnapshot = {
  members: HouseholdMember[];
  ownerId: string | null;
  owner: HouseholdMember | null;
  /** Whose profile this screen shows; the owner when the backend has none. */
  activeId: string | null;
  active: HouseholdMember | null;
  loading: boolean;
  error: string | null;
  /** Cache-buster for avatar URLs; bumped on every successful refresh. */
  bust: number;
};

const EMPTY: HouseholdSnapshot = {
  members: [],
  ownerId: null,
  owner: null,
  activeId: null,
  active: null,
  loading: true,
  error: null,
  bust: 0,
};

let snapshot: HouseholdSnapshot = EMPTY;
const listeners = new Set<() => void>();
let inFlight: Promise<void> | null = null;
let timer: ReturnType<typeof setInterval> | null = null;

function ownerOf(payload: HouseholdPayload): HouseholdMember | null {
  const byId = payload.owner_id
    ? payload.members.find((m) => m.id === payload.owner_id)
    : undefined;
  return byId ?? payload.members.find((m) => m.role === "owner") ?? null;
}

/** The active member, or the owner: a screen always belongs to somebody. */
function activeOf(payload: HouseholdPayload): HouseholdMember | null {
  const byId = payload.active_id
    ? payload.members.find((m) => m.id === payload.active_id)
    : undefined;
  return byId ?? ownerOf(payload);
}

function publish(next: HouseholdSnapshot) {
  snapshot = next;
  listeners.forEach((l) => l());
}

/** Fetch now. Safe to call from anywhere; concurrent calls share one request. */
export function refreshHousehold(): Promise<void> {
  if (inFlight) return inFlight;
  inFlight = getHousehold()
    .then((payload) => {
      publish({
        members: payload.members,
        ownerId: payload.owner_id,
        owner: ownerOf(payload),
        activeId: payload.active_id,
        active: activeOf(payload),
        loading: false,
        error: null,
        bust: Date.now(),
      });
    })
    .catch((err: any) => {
      // Keep the last good roster on a blip — a network hiccup must not blank
      // the operator's own name out of the topbar.
      publish({
        ...snapshot,
        loading: false,
        error: err?.message || "Failed to load household",
      });
    })
    .finally(() => {
      inFlight = null;
    });
  return inFlight;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) {
    if (snapshot === EMPTY) refreshHousehold();
    timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      refreshHousehold();
    }, 30000);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer) {
      clearInterval(timer);
      timer = null;
    }
  };
}

const getSnapshot = () => snapshot;
const getServerSnapshot = () => EMPTY;

export function useHousehold(): HouseholdSnapshot & { refresh: () => Promise<void> } {
  const state = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  const refresh = useCallback(() => refreshHousehold(), []);
  return { ...state, refresh };
}

/** The owner row, or null until the roster arrives. */
export function useOwner(): HouseholdMember | null {
  return useHousehold().owner;
}

/**
 * The owner's display name, with a caller-supplied fallback used ONLY while
 * the store is empty. Every "Operator"/"PuffPAI User" literal in the app now
 * comes through here.
 */
export function useOwnerName(fallback: string): string {
  const owner = useOwner();
  return owner?.display_name?.trim() || fallback;
}

/** The member this screen belongs to, or null until the roster arrives. */
export function useActiveMember(): HouseholdMember | null {
  return useHousehold().active;
}
