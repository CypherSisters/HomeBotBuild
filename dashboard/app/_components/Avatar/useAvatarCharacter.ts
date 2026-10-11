"use client";

/**
 * Resolves which avatar engine to render (Puff SVG, Digital Human, custom GLB)
 * and reconciles the user's saved model URL.
 *
 * localStorage answers instantly (no first-paint flicker of the wrong avatar);
 * the saved profile setting is then fetched in the background and wins if it
 * disagrees. Both are best-effort: if neither is available we keep the store
 * default (Puff). Any unknown/stale engine id resolves to Puff via the registry.
 *
 * Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
 */

import { useEffect } from "react";
import { useAppSelector, useAppDispatch } from "@/app/_stores/hooks";
import { setAvatarCharacter, setAvatarUrl } from "@/app/_stores/avatarSlice";
import { getAISettings } from "@/app/_lib/services/profileService";
import { isAvatarEngineId } from "./engines/registry";
import type { AvatarEngineId } from "./engines/types";

export const AVATAR_CHARACTER_KEY = "puffpai_avatar_character";
/** Per-user custom GLB/model URL for the 3D engines. */
export const AVATAR_MODEL_URL_KEY = "puffpai_avatar_model_url";

/** Back-compat alias — engine ids were historically called "characters". */
export type AvatarCharacter = AvatarEngineId;

export default function useAvatarCharacter(): AvatarEngineId {
  const dispatch = useAppDispatch();
  const character = useAppSelector((state) => state.avatar.character);

  useEffect(() => {
    let cancelled = false;

    // ── Instant answer from localStorage ──────────────────────────────
    let storedChar: string | null = null;
    let storedUrl: string | null = null;
    try {
      storedChar = localStorage.getItem(AVATAR_CHARACTER_KEY);
      storedUrl = localStorage.getItem(AVATAR_MODEL_URL_KEY);
    } catch {
      // private mode / storage disabled
    }
    if (isAvatarEngineId(storedChar) && storedChar !== character) {
      dispatch(setAvatarCharacter(storedChar));
    }
    // A missing key means "no custom model" — the store already starts empty,
    // so only a stored value needs applying.
    if (storedUrl) {
      dispatch(setAvatarUrl(storedUrl));
    }

    // ── Background reconcile with the saved profile setting ────────────
    void getAISettings()
      .then((settings) => {
        if (cancelled) return;
        const remote = settings?.avatar_character;
        if (isAvatarEngineId(remote)) {
          dispatch(setAvatarCharacter(remote));
          try {
            localStorage.setItem(AVATAR_CHARACTER_KEY, remote);
          } catch {
            // ignore
          }
        }
        // The saved profile is authoritative: an empty string is a real value
        // meaning "cleared", so it must wipe the store + localStorage rather
        // than be skipped (otherwise a cleared URL comes back on next load).
        const remoteUrl = settings?.avatar_3d_url;
        if (typeof remoteUrl === "string") {
          dispatch(setAvatarUrl(remoteUrl));
          try {
            if (remoteUrl) {
              localStorage.setItem(AVATAR_MODEL_URL_KEY, remoteUrl);
            } else {
              localStorage.removeItem(AVATAR_MODEL_URL_KEY);
            }
          } catch {
            // ignore
          }
        }
      })
      .catch(() => {
        // Offline or endpoint unavailable — localStorage already decided.
      });

    return () => {
      cancelled = true;
    };
    // Runs once per mount; `character` is only read for the initial compare.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch]);

  return character;
}
