"use client";

/**
 * Full-size avatar panel for the dedicated avatar page.
 * Shows the 3D digital human with personality trait sliders, voice controls, and info.
 *
 * Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
 */

import { useRef, useCallback, useState, useEffect } from "react";
import {
  mdiVolumeHigh,
  mdiVolumeOff,
  mdiMessageText,
  mdiHeart,
  mdiFlash,
  mdiEmoticonHappy,
  mdiTie,
  mdiMagnify,
  mdiShieldCheck,
  mdiHandshake,
  mdiPalette,
} from "@mdi/js";
import Icon from "../Icon";
import CardBox from "../CardBox";
import Button from "../Button";
import { useAppSelector, useAppDispatch } from "@/app/_stores/hooks";
import { setAvatarSpeaking, setPersonalityTrait } from "@/app/_stores/avatarSlice";
import { speak as voiceSpeak } from "@/app/_lib/services/voiceService";
import type { PersonalityTrait, PersonalityTraitConfig } from "@/app/_interfaces/avatar";
import AvatarStage from "./AvatarStage";
import { resolveEngine } from "./engines/registry";
import type { AvatarEngineHandle } from "./engines/types";
import useAvatarCharacter from "./useAvatarCharacter";

/* ── Personality trait definitions ──────────────────────────── */

const PERSONALITY_TRAITS: PersonalityTraitConfig[] = [
  { key: "warmth", label: "Warmth", lowLabel: "Cold", highLabel: "Warm", icon: mdiHeart, color: "text-rose-500" },
  { key: "energy", label: "Energy", lowLabel: "Calm", highLabel: "Energetic", icon: mdiFlash, color: "text-amber-500" },
  { key: "humor", label: "Humor", lowLabel: "Serious", highLabel: "Playful", icon: mdiEmoticonHappy, color: "text-yellow-500" },
  { key: "formality", label: "Formality", lowLabel: "Casual", highLabel: "Formal", icon: mdiTie, color: "text-blue-500" },
  { key: "curiosity", label: "Curiosity", lowLabel: "Practical", highLabel: "Curious", icon: mdiMagnify, color: "text-cyan-500" },
  { key: "confidence", label: "Confidence", lowLabel: "Humble", highLabel: "Confident", icon: mdiShieldCheck, color: "text-purple-500" },
  { key: "empathy", label: "Empathy", lowLabel: "Analytical", highLabel: "Empathetic", icon: mdiHandshake, color: "text-green-500" },
  { key: "creativity", label: "Creativity", lowLabel: "Logical", highLabel: "Creative", icon: mdiPalette, color: "text-orange-500" },
];

/* ── Slider component ──────────────────────────────────────── */

function PersonalitySlider({
  config,
  value,
  onChange,
}: {
  config: PersonalityTraitConfig;
  value: number;
  onChange: (val: number) => void;
}) {
  return (
    <div className="py-2">
      <div className="flex items-center gap-2 mb-1">
        <Icon path={config.icon} size="16" className={config.color} />
        <span className="text-xs font-semibold text-gray-700 dark:text-slate-300">
          {config.label}
        </span>
        <span className="ml-auto text-[10px] font-medium tabular-nums text-gray-400">
          {value}
        </span>
      </div>
      <input
        type="range"
        min={0}
        max={100}
        step={1}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full h-1.5 rounded-full appearance-none cursor-pointer bg-gray-200 dark:bg-gray-700
          [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:h-3.5
          [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-cyan-500 [&::-webkit-slider-thumb]:shadow-sm
          [&::-webkit-slider-thumb]:hover:bg-cyan-400 [&::-webkit-slider-thumb]:transition-colors"
      />
      <div className="flex justify-between mt-0.5">
        <span className="text-[9px] text-gray-400">{config.lowLabel}</span>
        <span className="text-[9px] text-gray-400">{config.highLabel}</span>
      </div>
    </div>
  );
}

/* ── Main panel ────────────────────────────────────────────── */

export default function AvatarPanel() {
  const avatarRef = useRef<AvatarEngineHandle>(null);
  const character = useAvatarCharacter();
  const dispatch = useAppDispatch();
  const { avatarUrl, mood, speaking, personality } = useAppSelector(
    (state) => state.avatar
  );

  const [muted, setMuted] = useState(false);
  const [testText, setTestText] = useState("");
  const [avatarReady, setAvatarReady] = useState(false);

  // Persist personality to localStorage
  useEffect(() => {
    const saved = localStorage.getItem("puffpai_personality");
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        Object.entries(parsed).forEach(([key, val]) => {
          dispatch(
            setPersonalityTrait({
              trait: key as PersonalityTrait,
              value: val as number,
            })
          );
        });
      } catch {
        // ignore corrupt data
      }
    }
  }, [dispatch]);

  useEffect(() => {
    localStorage.setItem("puffpai_personality", JSON.stringify(personality));
  }, [personality]);

  const handleLoad = useCallback(() => {
    setAvatarReady(true);
  }, []);

  const handleError = useCallback((error: string) => {
    console.error("[AvatarPanel] Avatar error:", error);
    setAvatarReady(false);
  }, []);

  const handleTraitChange = useCallback(
    (trait: PersonalityTrait, value: number) => {
      dispatch(setPersonalityTrait({ trait, value }));
    },
    [dispatch]
  );

  // Derive dominant traits for the info card
  const dominantTraits = PERSONALITY_TRAITS.filter(
    (t) => personality[t.key] >= 65
  )
    .sort((a, b) => personality[b.key] - personality[a.key])
    .slice(0, 3);

  const handleTestSpeak = useCallback(() => {
    if (!testText.trim()) return;
    // Use TalkingHead's built-in lip-synced speech if avatar is ready
    if (avatarRef.current?.isReady()) {
      avatarRef.current.speakText(testText);
    } else {
      // Fallback to voice service without lip-sync
      voiceSpeak(
        testText,
        () => dispatch(setAvatarSpeaking(true)),
        () => dispatch(setAvatarSpeaking(false))
      );
    }
  }, [testText, dispatch]);

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
      {/* Main avatar viewport — spans 2 columns on large screens */}
      <div className="lg:col-span-2">
        <CardBox className="overflow-hidden">
          <div className="relative" style={{ height: "400px" }}>
            <AvatarStage
              engineId={character}
              ref={avatarRef}
              avatarUrl={avatarUrl}
              mood={mood}
              className="h-full w-full"
              style={{ height: "400px" }}
              onLoad={handleLoad}
              onError={handleError}
              onSpeakStart={() => dispatch(setAvatarSpeaking(true))}
              onSpeakEnd={() => dispatch(setAvatarSpeaking(false))}
            />

            {/* Status overlay */}
            <div className="absolute left-4 top-4 flex items-center gap-2">
              <span
                className={`flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium shadow-sm backdrop-blur-sm ${
                  avatarReady
                    ? "bg-green-100/90 text-green-700 dark:bg-green-900/60 dark:text-green-300"
                    : "bg-yellow-100/90 text-yellow-700 dark:bg-yellow-900/60 dark:text-yellow-300"
                }`}
              >
                <span
                  className={`h-2 w-2 rounded-full ${
                    avatarReady ? "bg-green-500" : "bg-yellow-500 animate-pulse"
                  }`}
                />
                {avatarReady ? "Ready" : "Loading..."}
              </span>

              {speaking && (
                <span className="flex items-center gap-1.5 rounded-full bg-cyan-500/90 px-3 py-1 text-xs font-medium text-white shadow-sm backdrop-blur-sm">
                  <span className="relative flex h-2 w-2">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-white opacity-75" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-white" />
                  </span>
                  Speaking
                </span>
              )}
            </div>

            {/* Mute toggle */}
            <button
              onClick={() => setMuted((prev) => !prev)}
              className="absolute right-4 top-4 rounded-full bg-white/80 p-2 shadow-sm transition hover:bg-white dark:bg-slate-800/80 dark:hover:bg-slate-700"
              title={muted ? "Unmute" : "Mute"}
            >
              <Icon
                path={muted ? mdiVolumeOff : mdiVolumeHigh}
                size="20"
                className={muted ? "text-red-500" : "text-gray-600 dark:text-slate-300"}
              />
            </button>
          </div>
        </CardBox>
      </div>

      {/* Controls sidebar */}
      <div className="space-y-6">
        {/* Personality Traits */}
        <CardBox>
          <h3 className="mb-2 text-sm font-semibold text-gray-700 dark:text-slate-300">
            Personality
          </h3>
          <p className="mb-3 text-[10px] text-gray-400">
            Adjust how your PuffPAI communicates
          </p>
          <div className="space-y-0.5 max-h-[420px] overflow-y-auto pr-1">
            {PERSONALITY_TRAITS.map((config) => (
              <PersonalitySlider
                key={config.key}
                config={config}
                value={personality[config.key]}
                onChange={(val) => handleTraitChange(config.key, val)}
              />
            ))}
          </div>
        </CardBox>

        {/* Test Speech */}
        <CardBox>
          <h3 className="mb-3 text-sm font-semibold text-gray-700 dark:text-slate-300">
            Test Speech
          </h3>
          <textarea
            value={testText}
            onChange={(e) => setTestText(e.target.value)}
            placeholder="Type something for the avatar to say..."
            rows={3}
            className="w-full resize-none rounded-xl border border-gray-200 bg-gray-50 px-3 py-2 text-sm focus:border-cyan-400 focus:ring-1 focus:ring-cyan-400 focus:outline-none dark:border-slate-600 dark:bg-slate-800 dark:text-white"
          />
          <div className="mt-3 flex gap-2">
            <Button
              icon={mdiMessageText}
              label={speaking ? "Speaking..." : "Speak"}
              color="info"
              small
              roundedFull
              onClick={handleTestSpeak}
              disabled={!testText.trim() || speaking}
            />
          </div>
        </CardBox>

        {/* Avatar Info */}
        <CardBox>
          <h3 className="mb-3 text-sm font-semibold text-gray-700 dark:text-slate-300">
            Avatar Info
          </h3>
          <div className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span className="text-gray-500 dark:text-slate-400">Status</span>
              <span className="font-medium text-gray-700 dark:text-slate-200">
                {avatarReady ? "Ready" : "Loading"}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500 dark:text-slate-400">Profile</span>
              <span className="font-medium text-gray-700 dark:text-slate-200">
                {dominantTraits.length > 0
                  ? dominantTraits.map((t) => t.label).join(", ")
                  : "Balanced"}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500 dark:text-slate-400">Engine</span>
              <span className="font-medium text-gray-700 dark:text-slate-200">
                {resolveEngine(character).label}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500 dark:text-slate-400">Voice</span>
              <span className="font-medium text-gray-700 dark:text-slate-200">
                Browser TTS
              </span>
            </div>
          </div>
        </CardBox>
      </div>
    </div>
  );
}
