"use client";

/**
 * Dashboard home tile: the PuffPAI digital human, now voice-first.
 *
 * Tap the mic → speak → PuffPAI answers out loud with lipsync, right on the
 * dashboard (no need to open the chat page). Full conversation loop:
 *
 *   mic (16 kHz PCM) → /voice/transcribe (Vosk, offline)
 *                    → /chat/send (Cortex agent)
 *                    → /voice/synthesize (Piper, offline) → speakPcm lipsync
 *
 * Offline-Core: the whole voice turn is local. Browser SpeechRecognition is
 * only used as an STT fallback when the backend Vosk engine is unavailable.
 *
 * Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
 */

import { useRef, useCallback, useState, useEffect } from "react";
import Link from "next/link";
import { mdiMicrophone, mdiStop, mdiOpenInNew } from "@mdi/js";
import Icon from "../Icon";
import { useAppSelector, useAppDispatch } from "@/app/_stores/hooks";
import {
  setAvatarSpeaking,
  setIsListening,
} from "@/app/_stores/avatarSlice";
import {
  transcribe,
  synthesize,
  browserListen,
  browserSpeak,
  browserSpeakStop,
  playPcmAudio,
  getVoiceCapabilities,
  isBrowserSttSupported,
  registerAvatarSink,
} from "@/app/_lib/services/voiceService";
import {
  startMicCapture,
  isMicCaptureSupported,
  type MicCaptureController,
} from "@/app/_lib/services/micCapture";
import { sendMessage } from "@/app/_lib/services/chatService";
import { withRoomSpeaker } from "@/app/_lib/services/roomSpeaker";
import AvatarStage from "./AvatarStage";
import type { AvatarEngineHandle } from "./engines/types";
import useAvatarCharacter from "./useAvatarCharacter";

type Phase = "idle" | "listening" | "thinking" | "speaking";

const PHASE_TEXT: Record<Phase, string> = {
  idle: "Tap the mic and talk to me",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

/** Strip markdown so Piper doesn't read asterisks and pound signs aloud. */
function sanitizeForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " code omitted. ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/[*_~>|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

export default function AvatarTile() {
  const avatarRef = useRef<AvatarEngineHandle>(null);
  const micRef = useRef<MicCaptureController | null>(null);
  const browserListenRef = useRef<{ stop: () => void } | null>(null);
  const convIdRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  const character = useAvatarCharacter();
  const dispatch = useAppDispatch();
  const { avatarUrl, mood } = useAppSelector((state) => state.avatar);

  const [phase, setPhase] = useState<Phase>("idle");
  const [caption, setCaption] = useState<string | null>(null);
  const [voiceReady, setVoiceReady] = useState(false);
  const [micLevel, setMicLevel] = useState(0);
  // Live transcript: what you said + her full reply, auto-scrolled in sync
  // with speech.
  const [lastUser, setLastUser] = useState<string | null>(null);
  const [replyText, setReplyText] = useState<string | null>(null);
  const [speechMs, setSpeechMs] = useState(4000);
  const transcriptRef = useRef<HTMLDivElement>(null);

  const handleLoad = useCallback(() => {
    // Engine finished loading — nothing to do; the stage handles its own state.
  }, []);

  // Voice availability: backend Vosk+mic capture, or browser STT fallback
  useEffect(() => {
    mountedRef.current = true;
    getVoiceCapabilities().then((caps) => {
      if (!mountedRef.current) return;
      setVoiceReady(
        (caps.stt_available && isMicCaptureSupported()) || isBrowserSttSupported()
      );
    });
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // While this tile is on screen, ALL server-TTS speech (chat, agents)
  // routes through her mouth — same contract as the chat page's overlay.
  useEffect(() => {
    registerAvatarSink(async (pcmBase64, sampleRate, text) => {
      const h = avatarRef.current;
      if (!h?.isReady()) throw new Error("avatar not ready");
      await h.speakPcm(pcmBase64, sampleRate, text);
    });
    return () => registerAvatarSink(null);
  }, []);

  // Cleanup on unmount: release mic, stop any speech
  useEffect(() => {
    return () => {
      micRef.current?.cancel();
      browserListenRef.current?.stop();
      browserSpeakStop();
      dispatch(setIsListening(false));
      dispatch(setAvatarSpeaking(false));
    };
  }, [dispatch]);

  const endTurn = useCallback(
    (message?: string) => {
      if (!mountedRef.current) return;
      setPhase("idle");
      setMicLevel(0);
      if (message) setCaption(message);
      dispatch(setIsListening(false));
      dispatch(setAvatarSpeaking(false));
    },
    [dispatch]
  );

  // Auto-scroll the transcript panel over the duration of her speech
  useEffect(() => {
    if (phase !== "speaking") return;
    const el = transcriptRef.current;
    if (!el) return;
    el.scrollTop = 0;
    const total = Math.max(1500, speechMs);
    const start = performance.now();
    let raf = 0;
    const step = (t: number) => {
      const p = Math.min(1, (t - start) / total);
      el.scrollTop = p * Math.max(0, el.scrollHeight - el.clientHeight);
      if (p < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [phase, speechMs]);

  /** One full voice turn: listen → transcribe → think → speak. */
  const runVoiceTurn = useCallback(async () => {
    setCaption(null);
    setReplyText(null);
    setPhase("listening");
    dispatch(setIsListening(true));

    // ── 1. Listen ──────────────────────────────────────────────
    let transcript = "";
    try {
      const caps = await getVoiceCapabilities();
      if (caps.stt_available && isMicCaptureSupported()) {
        // Offline path: local capture → backend Vosk
        const controller = await startMicCapture({
          maxMs: 20000,
          silenceMs: 1400,
          onLevel: (rms) => {
            if (mountedRef.current) setMicLevel(Math.min(1, rms * 12));
          },
        });
        micRef.current = controller;
        const audio = await controller.done;
        micRef.current = null;
        if (!audio || !audio.heardSpeech) {
          endTurn("Didn't catch that — tap the mic and try again.");
          return;
        }
        setPhase("thinking");
        dispatch(setIsListening(false));
        const stt = await transcribe(audio.pcmBase64);
        transcript = (stt.success && stt.text) || "";
      } else {
        // Fallback: browser SpeechRecognition (needs internet in Chrome)
        transcript = await new Promise<string>((resolve) => {
          const c = browserListen(
            (text) => resolve(text),
            () => resolve(""),
            () => resolve("")
          );
          browserListenRef.current = c;
          if (!c) resolve("");
        });
        browserListenRef.current = null;
        setPhase("thinking");
        dispatch(setIsListening(false));
      }
    } catch (e) {
      console.warn("[AvatarTile] mic error:", e);
      endTurn("Microphone unavailable — check browser permissions.");
      return;
    }

    transcript = transcript.trim();
    if (!transcript) {
      endTurn("Didn't catch that — tap the mic and try again.");
      return;
    }
    if (!mountedRef.current) return;
    setLastUser(transcript);
    setPhase("thinking");

    // ── 2. Think (full Cortex agent via /chat/send; persists to history) ──
    let reply = "";
    try {
      const result = await sendMessage(transcript, convIdRef.current ?? undefined);
      convIdRef.current = result.conversation_id;
      reply = result.response || "";
    } catch (e) {
      console.warn("[AvatarTile] chat error:", e);
      endTurn("I couldn't reach the backend — is PuffPAI running?");
      return;
    }
    if (!mountedRef.current) return;
    if (!reply.trim()) {
      endTurn("I didn't get a response. Try again?");
      return;
    }

    // ── 3. Speak (Piper TTS → lipsync; graceful fallbacks) ────────────────
    setReplyText(reply);
    setPhase("speaking");
    dispatch(setAvatarSpeaking(true));
    const speech = sanitizeForSpeech(reply);
    // Estimate duration for transcript auto-scroll (refined below for Piper)
    setSpeechMs((speech.split(/\s+/).length / 2.5) * 1000);
    try {
      // On the kiosk: only when the room speakers are free (lib/roomSpeech).
      await withRoomSpeaker("local-reply", async () => {
        const tts = await synthesize(speech);
        if (tts.success && tts.audio_base64) {
          const rate = tts.sample_rate || 22050;
          // Exact duration from the PCM payload: base64 → bytes → samples
          setSpeechMs(((tts.audio_base64.length * 0.75) / 2 / rate) * 1000);
          if (avatarRef.current?.isReady()) {
            await avatarRef.current.speakPcm(tts.audio_base64, rate, speech);
          } else {
            await playPcmAudio(tts.audio_base64, rate);
          }
        } else {
          await new Promise<void>((resolve) => {
            browserSpeak(speech, resolve);
            setTimeout(resolve, 30000); // safety net if browser TTS dies silently
          });
        }
      });
    } catch (e) {
      console.warn("[AvatarTile] TTS error:", e);
    }
    endTurn();
  }, [dispatch, endTurn]);

  const handleMicButton = useCallback(() => {
    if (phase === "listening") {
      // Finish the utterance now
      micRef.current?.stop();
      browserListenRef.current?.stop();
      return;
    }
    if (phase === "speaking") {
      // Interrupt her
      avatarRef.current?.stopSpeaking();
      browserSpeakStop();
      endTurn();
      return;
    }
    if (phase === "thinking") return; // in flight — hang tight
    void runVoiceTurn();
  }, [phase, runVoiceTurn, endTurn]);

  const micActive = phase === "listening";
  const busy = phase === "thinking";

  return (
    <div className="gloss-highlight group relative block h-full overflow-hidden rounded-2xl bg-gradient-to-br from-[#0096C7]/10 via-white/80 to-[#00B4D8]/10 shadow-sm backdrop-blur-sm transition dark:from-[#0096C7]/5 dark:via-slate-900/80 dark:to-[#00B4D8]/5">
      {/* Avatar viewport — tight head-and-shoulders crop */}
      <div className="relative h-80 w-full overflow-hidden">
        <AvatarStage
          engineId={character}
          ref={avatarRef}
          avatarUrl={avatarUrl}
          mood={mood}
          view="head"
          className="h-full w-full"
          style={{ height: "320px", minHeight: "320px" }}
          onLoad={handleLoad}
        />

        {/* Listening / speaking status chip */}
        {phase !== "idle" && (
          <div className="absolute left-3 top-3 z-10 flex items-center gap-1.5 rounded-full bg-cyan-500/90 px-2.5 py-1 text-[10px] font-medium text-white shadow-sm backdrop-blur-sm">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-white opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-white" />
            </span>
            {PHASE_TEXT[phase]}
          </div>
        )}

        {/* Open full avatar page */}
        <Link
          href="/dashboard/avatar"
          className="absolute right-3 top-3 z-10 rounded-full bg-white/70 p-1.5 text-gray-500 shadow-sm transition hover:bg-white hover:text-gray-700 dark:bg-slate-800/70 dark:text-slate-400 dark:hover:bg-slate-700"
          title="Open full Digital Human page"
        >
          <Icon path={mdiOpenInNew} size="14" />
        </Link>

        {/* Gradient fade at bottom */}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-white/90 to-transparent dark:from-slate-900/90" />
      </div>

      {/* Voice control bar */}
      <div className="flex items-center gap-3 px-4 py-3">
        <button
          onClick={handleMicButton}
          disabled={!voiceReady || busy}
          className={`relative flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-full shadow-sm transition ${
            micActive
              ? "bg-gradient-to-br from-rose-500 to-red-600 text-white"
              : phase === "speaking"
              ? "bg-gradient-to-br from-amber-400 to-orange-500 text-white"
              : "bg-gradient-to-br from-cyan-400 to-blue-600 text-white hover:scale-105"
          } ${!voiceReady || busy ? "cursor-not-allowed opacity-50" : ""}`}
          title={
            !voiceReady
              ? "Voice unavailable — backend STT offline and browser doesn't support speech"
              : micActive
              ? "Done talking (or pause and I'll notice)"
              : phase === "speaking"
              ? "Stop speaking"
              : busy
              ? "Thinking…"
              : "Talk to PuffPAI"
          }
        >
          {/* Mic level ring while listening */}
          {micActive && (
            <span
              className="absolute inset-0 rounded-full border-2 border-rose-300/80 transition-transform duration-100"
              style={{ transform: `scale(${1 + micLevel * 0.45})`, opacity: 0.7 }}
            />
          )}
          {busy ? (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
          ) : (
            <Icon
              path={micActive || phase === "speaking" ? mdiStop : mdiMicrophone}
              size="20"
            />
          )}
        </button>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold text-[#0C2F44] dark:text-white">
            {PHASE_TEXT[phase]}
          </p>
          <p className="truncate text-xs text-gray-500 dark:text-slate-400">
            {caption ?? "Voice runs locally — Vosk + Piper on this device"}
          </p>
        </div>
        <span
          className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
            voiceReady
              ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
              : "bg-gray-100 text-gray-500 dark:bg-slate-800 dark:text-slate-400"
          }`}
        >
          {voiceReady ? "Voice" : "Muted"}
        </span>
      </div>

      {/* Live transcript — full text, auto-scrolls while she speaks.
          Also makes STT mis-hearings visible ("hello past pie" problem). */}
      {(lastUser || replyText) && (
        <div
          ref={transcriptRef}
          className="mx-4 mb-3 max-h-28 overflow-y-auto rounded-lg border border-black/5 bg-black/5 p-2.5 text-xs leading-relaxed dark:border-white/5 dark:bg-black/30"
        >
          {lastUser && (
            <p className="mb-1.5 text-cyan-700 dark:text-cyan-300/90">
              <span className="font-semibold">You:</span> {lastUser}
            </p>
          )}
          {replyText && (
            <p className="whitespace-pre-wrap text-[#0C2F44] dark:text-slate-200">
              <span className="font-semibold text-cyan-700 dark:text-cyan-300">
                PuffPAI:
              </span>{" "}
              {replyText}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
