/**
 * Voice service for PuffPAI dashboard.
 *
 * Provides TTS (text-to-speech) and STT (speech-to-text) with
 * automatic fallback from backend engines to browser Web Speech APIs.
 *
 * Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
 */

import { apiGet, apiGetRaw, apiPost } from "../api";

/* ── Types ──────────────────────────────────────────────────── */

export interface VoiceCapabilities {
  tts_available: boolean;
  stt_available: boolean;
  tts_engine: string;
  stt_engine: string;
  tts_sample_rate: number | null;
  stt_sample_rate: number | null;
  fallback: string;
}

export interface SynthesizeResult {
  success: boolean;
  audio_base64?: string;
  sample_rate?: number;
  format?: string;
  fallback?: string;
  message?: string;
}

export interface TranscribeResult {
  success: boolean;
  text?: string;
  fallback?: string;
  message?: string;
}

/* ── Capabilities probe ─────────────────────────────────────── */

let _cachedCapabilities: VoiceCapabilities | null = null;

export async function getVoiceCapabilities(): Promise<VoiceCapabilities> {
  if (_cachedCapabilities) return _cachedCapabilities;
  try {
    const caps = await apiGet<VoiceCapabilities>("/voice/capabilities");
    _cachedCapabilities = caps;
    return caps;
  } catch {
    // Backend unreachable — assume browser-only fallback
    return {
      tts_available: false,
      stt_available: false,
      tts_engine: "none",
      stt_engine: "none",
      tts_sample_rate: null,
      stt_sample_rate: null,
      fallback: "browser",
    };
  }
}

/** Reset cached capabilities (call after backend reconnects). */
export function resetVoiceCapabilities() {
  _cachedCapabilities = null;
}

/* ── Stop ─────────────────────────────────── */

/**
 * What the appliance's mouth is doing right now.
 *
 * `since` is the epoch seconds the CURRENT utterance began, so a caller can
 * tell "still the sentence I asked you to stop" from "a new one started" —
 * which is what makes an optimistic hide safe (useVoiceState).
 */
export interface VoiceSpeakingState {
  speaking: boolean;
  since: number | null;
  /** False when this machine has no voice unit (voice is optional). A backend
   *  that does not say is taken to have one. */
  voiceService: boolean;
}

/** `GET /voice/state`. */
export async function getVoiceState(): Promise<VoiceSpeakingState> {
  const s = await apiGet<Partial<VoiceSpeakingState> & { voice_service?: boolean }>(
    "/voice/state",
    { cache: "no-store" },
  );
  return {
    voiceService: s?.voice_service !== false,
    speaking: s?.speaking === true,
    // A missing/!=number `since` must not be reported as 0 — 0 is a real epoch
    // and would make every optimistic stop look stale.
    since: typeof s?.since === "number" ? s.since : null,
  };
}

/* ── The turn happening AT THE BOX ──────────────────────────── */

/**
 * The in-flight on-box voice turn (wake word → speech), as /voice/live
 * reports it. `reply` grows while Puff answers, and the record stays readable
 * for a while after `done` so someone walking up to the wall can still read
 * what was just said.
 */
export interface VoiceLiveTurn {
  active: boolean;
  turnId: string | null;
  heard: string;
  thinking: string;
  reply: string;
  done: boolean;
}

/**
 * `GET /voice/live`, status kept.
 *
 * A box that predates the route answers 404, which is not an error condition:
 * `turn` is simply null and the caller shows nothing. The status is returned
 * so the poller can back off instead of asking a route that isn't there twice
 * a second forever.
 */
export async function getVoiceLive(): Promise<{
  status: number;
  turn: VoiceLiveTurn | null;
}> {
  try {
    const { status, ok, data } = await apiGetRaw<Record<string, unknown>>(
      "/voice/live",
      { cache: "no-store" },
    );
    if (!ok || !data) return { status, turn: null };
    const str = (v: unknown) => (typeof v === "string" ? v : "");
    const turnId = typeof data.turn_id === "string" ? data.turn_id : null;
    return {
      status,
      turn: {
        active: data.active === true,
        turnId,
        heard: str(data.heard),
        thinking: str(data.thinking),
        reply: str(data.reply),
        done: data.done === true,
      },
    };
  } catch {
    // Backend unreachable: same answer as "no turn", with a status that tells
    // the poller to slow down.
    return { status: 0, turn: null };
  }
}

/**
 * `POST /voice/stop`. `music: true` also stops the radio ("stop everything").
 *
 * Deliberately THROWS on failure rather than swallowing like synthesize():
 * this is the control the owner reaches for when they want quiet, so a failure
 * has to reach her eyes instead of being hidden behind an optimistic UI.
 */
export async function stopVoice(music = false): Promise<void> {
  await apiPost("/voice/stop", { music });
}

/* ── Backend TTS ────────────────────────────────────────────── */

export async function synthesize(text: string): Promise<SynthesizeResult> {
  try {
    return await apiPost<SynthesizeResult>("/voice/synthesize", { text });
  } catch {
    return { success: false, fallback: "browser", message: "Backend unreachable" };
  }
}

/* ── Backend STT ────────────────────────────────────────────── */

export async function transcribe(audioBase64: string): Promise<TranscribeResult> {
  try {
    return await apiPost<TranscribeResult>("/voice/transcribe", {
      audio_base64: audioBase64,
    });
  } catch {
    return { success: false, fallback: "browser", message: "Backend unreachable" };
  }
}

/* ── Browser TTS Fallback ───────────────────────────────────── */

/**
 * Speak text using the browser's built-in SpeechSynthesis API.
 * Returns a promise that resolves when speech ends.
 */
export function browserSpeak(
  text: string,
  onEnd?: () => void
): SpeechSynthesisUtterance | null {
  if (typeof window === "undefined" || !window.speechSynthesis) return null;

  // Cancel any in-progress speech
  window.speechSynthesis.cancel();

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 1.0;
  utterance.pitch = 1.0;
  utterance.volume = 1.0;

  // Try to pick a good English voice
  const voices = window.speechSynthesis.getVoices();
  const preferred = voices.find(
    (v) =>
      v.lang.startsWith("en") &&
      (v.name.includes("Google") || v.name.includes("Samantha") || v.name.includes("Natural"))
  );
  if (preferred) utterance.voice = preferred;

  utterance.onend = () => onEnd?.();
  utterance.onerror = () => onEnd?.();

  window.speechSynthesis.speak(utterance);
  return utterance;
}

/** Stop any active browser speech. */
export function browserSpeakStop() {
  if (typeof window !== "undefined" && window.speechSynthesis) {
    window.speechSynthesis.cancel();
  }
}

/* ── Browser STT Fallback ───────────────────────────────────── */

// SpeechRecognition type for TypeScript
type SpeechRecognitionType = typeof window extends { SpeechRecognition: infer T }
  ? T
  : any;

/**
 * Start browser-based speech recognition.
 * Returns a controller object to stop recognition.
 */
export function browserListen(
  onResult: (text: string) => void,
  onError?: (error: string) => void,
  onEnd?: () => void
): { stop: () => void } | null {
  if (typeof window === "undefined") return null;

  const SpeechRecognition =
    (window as any).SpeechRecognition ||
    (window as any).webkitSpeechRecognition;

  if (!SpeechRecognition) {
    onError?.("SpeechRecognition not supported in this browser");
    return null;
  }

  const recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.lang = "en-US";
  recognition.maxAlternatives = 1;

  recognition.onresult = (event: any) => {
    const transcript = event.results[0]?.[0]?.transcript || "";
    onResult(transcript);
  };

  recognition.onerror = (event: any) => {
    if (event.error !== "aborted") {
      onError?.(event.error || "Recognition error");
    }
  };

  recognition.onend = () => {
    onEnd?.();
  };

  recognition.start();

  return {
    stop: () => {
      try {
        recognition.stop();
      } catch {
        // Already stopped
      }
    },
  };
}

/* ── Audio Playback Helpers ─────────────────────────────────── */

/**
 * Play raw PCM 16-bit audio from a base64 string via Web Audio API.
 * Returns a promise that resolves when playback ends.
 */
export async function playPcmAudio(
  base64Audio: string,
  sampleRate = 22050
): Promise<void> {
  const raw = atob(base64Audio);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) {
    bytes[i] = raw.charCodeAt(i);
  }

  // Convert 16-bit PCM to Float32 for Web Audio
  const int16 = new Int16Array(bytes.buffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 32768;
  }

  const ctx = new AudioContext();
  const buffer = ctx.createBuffer(1, float32.length, sampleRate);
  buffer.copyToChannel(float32, 0);

  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(ctx.destination);

  return new Promise<void>((resolve) => {
    source.onended = () => {
      ctx.close();
      resolve();
    };
    source.start();
  });
}

/**
 * Convert a Blob (from MediaRecorder) to a base64 string.
 */
export async function audioBlobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      const dataUrl = reader.result as string;
      // Strip the data:audio/...;base64, prefix
      const base64 = dataUrl.split(",")[1] || "";
      resolve(base64);
    };
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

/* ── High-Level Speak Function ──────────────────────────────── */

/* ── Avatar lipsync sink ────────────────────────────────────── */

/**
 * When the avatar overlay is on screen it registers a sink here; server-TTS
 * audio then plays through the avatar (with mouth movement) instead of a
 * bare AudioContext. Unregister with null.
 */
type AvatarSink = (
  pcmBase64: string,
  sampleRate: number,
  text: string
) => Promise<void>;

let _avatarSink: AvatarSink | null = null;

export function registerAvatarSink(sink: AvatarSink | null) {
  _avatarSink = sink;
}

/**
 * Smart speak: backend PiperTTS through the avatar's mouth when she's
 * visible, plain audio otherwise, browser TTS as the last resort.
 */
export async function speak(
  text: string,
  onStart?: () => void,
  onEnd?: () => void
): Promise<void> {
  onStart?.();

  // Try backend TTS first
  const result = await synthesize(text);

  if (result.success && result.audio_base64) {
    const rate = result.sample_rate || 22050;
    // Preferred: route through the avatar for lipsync
    if (_avatarSink) {
      try {
        await _avatarSink(result.audio_base64, rate, text);
        onEnd?.();
        return;
      } catch {
        // Avatar not ready — fall through to plain playback
      }
    }
    try {
      await playPcmAudio(result.audio_base64, rate);
      onEnd?.();
      return;
    } catch {
      // Fall through to browser TTS
    }
  }

  // Browser fallback
  return new Promise<void>((resolve) => {
    browserSpeak(text, () => {
      onEnd?.();
      resolve();
    });
    // Safety timeout — if browser TTS fails silently
    setTimeout(() => {
      onEnd?.();
      resolve();
    }, 30000);
  });
}

/* ── High-Level Listen Function ─────────────────────────────── */

/**
 * Smart listen: uses browser SpeechRecognition (most reliable for real-time).
 * Backend Vosk is better for batch audio processing.
 */
export function listen(
  onResult: (text: string) => void,
  onError?: (error: string) => void,
  onEnd?: () => void
): { stop: () => void } | null {
  return browserListen(onResult, onError, onEnd);
}

/* ── Browser Support Detection ──────────────────────────────── */

export function isBrowserTtsSupported(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

export function isBrowserSttSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    ("SpeechRecognition" in window || "webkitSpeechRecognition" in window)
  );
}
