"""Speech-to-text engines for PuffPAI.

Implements:
  - STTEngine (ABC): common interface (``transcribe`` + ``transcribe_result``)
  - TranscriptionResult: text plus the confidence signals used by the gate
  - StubSTT: placeholder for dev/testing
  - VoskSTT: offline recognition via Vosk (Apache 2.0); exposes word confidence
  - WhisperSTT: faster-whisper / CTranslate2; exposes avg_logprob + no_speech_prob
  - create_stt(): config-driven engine selection with graceful fallback

Confidence gate
---------------
A voice turn that transcribes to garbage — a false wake catching room noise, a
half-caught word, a Whisper hallucination on non-speech — must NOT be routed to
the planner/RAG/LLM. Doing so burns a whole brain call on nothing and can make
Puff "answer" something the user never said (the exact failure seen on-box:
transcripts like "The first step is by the front door." reaching /api/chat).

``transcribe_result()`` returns a :class:`TranscriptionResult` whose
``low_confidence`` flag the caller checks BEFORE doing any work: when set,
respond "I didn't catch that — say it again?" and re-listen instead of calling
the brain. ``transcribe()`` keeps returning a bare string for existing callers.

Thresholds are calibrated on this appliance (Piper → resample 16k → faster-whisper
base.en int8): real speech (including noisy and simulated-distant
captures) sits at avg_logprob -0.30…-0.47 with no_speech_prob ≤ 0.08, while
hallucinated fragments/garbage drop to avg_logprob -0.68…-1.07. The config
defaults (avg_logprob < -0.65, no_speech_prob > 0.5) sit in that gap: they flag
the garbage while passing real commands and valid partials with margin.
faster-whisper's built-in ``vad_filter`` already reduces pure silence/noise to
zero segments (empty text), which the gate also treats as a miss.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Graceful import — vosk is an optional dependency
try:
    import vosk
    _HAS_VOSK = True
except ImportError:
    vosk = None  # type: ignore[assignment]
    _HAS_VOSK = False

# Graceful import — faster-whisper is an optional dependency. Kept lazy-friendly:
# importing this module never requires faster-whisper (dev machines without it
# still import fine and fall back to Vosk/Stub).
try:
    from faster_whisper import WhisperModel as _WhisperModel
    _HAS_WHISPER = True
except Exception:  # ImportError, or a broken CTranslate2 native load
    _WhisperModel = None  # type: ignore[assignment]
    _HAS_WHISPER = False


@dataclass
class TranscriptionResult:
    """A transcript plus the signals used to decide whether to trust it.

    ``low_confidence`` is the single field callers act on: when True, treat the
    utterance as unheard (respond "say it again?"), do not route to the brain.
    The raw signals are kept for logging / threshold tuning; which are populated
    depends on the engine (whisper sets logprob/no_speech; vosk sets word conf).
    """
    text: str = ""
    low_confidence: bool = False
    avg_logprob: float | None = None       # whisper: mean of segment avg_logprob
    no_speech_prob: float | None = None     # whisper: max of segment no_speech_prob
    word_confidence: float | None = None    # vosk: mean word confidence (0..1)
    n_segments: int = 0
    reason: str = ""                        # why it was flagged (for logs)

    def __bool__(self) -> bool:
        return bool(self.text.strip())


# ── Pure scoring helpers (no heavy deps — unit-testable in isolation) ────────
def evaluate_whisper(text: str, seg_logprobs, seg_no_speech, *,
                     min_avg_logprob: float, max_no_speech_prob: float,
                     n_segments: int | None = None) -> TranscriptionResult:
    """Score a faster-whisper result. ``seg_*`` are per-segment lists."""
    text = (text or "").strip()
    logps = [v for v in (seg_logprobs or []) if v is not None]
    nsps = [v for v in (seg_no_speech or []) if v is not None]
    n = n_segments if n_segments is not None else max(len(logps), len(nsps))
    mean_lp = sum(logps) / len(logps) if logps else None
    max_ns = max(nsps) if nsps else None

    low, reason = False, ""
    if not text:
        low, reason = True, "empty (no speech)"
    elif mean_lp is not None and mean_lp < min_avg_logprob:
        low, reason = True, f"avg_logprob {mean_lp:.2f} < {min_avg_logprob}"
    elif max_ns is not None and max_ns > max_no_speech_prob:
        low, reason = True, f"no_speech_prob {max_ns:.2f} > {max_no_speech_prob}"

    return TranscriptionResult(
        text=text, low_confidence=low,
        avg_logprob=round(mean_lp, 3) if mean_lp is not None else None,
        no_speech_prob=round(max_ns, 3) if max_ns is not None else None,
        n_segments=n, reason=reason,
    )


def evaluate_vosk(text: str, word_confs, *,
                  min_word_confidence: float) -> TranscriptionResult:
    """Score a Vosk result from its per-word confidences."""
    text = (text or "").strip()
    confs = [v for v in (word_confs or []) if v is not None]
    mean_conf = sum(confs) / len(confs) if confs else None

    low, reason = False, ""
    if not text:
        low, reason = True, "empty (no speech)"
    elif mean_conf is not None and mean_conf < min_word_confidence:
        low, reason = True, f"word_conf {mean_conf:.2f} < {min_word_confidence}"

    return TranscriptionResult(
        text=text, low_confidence=low,
        word_confidence=round(mean_conf, 3) if mean_conf is not None else None,
        n_segments=1 if text else 0, reason=reason,
    )


def _stt_config() -> dict:
    """Best-effort load of STT settings from config; safe defaults otherwise."""
    try:
        from puffpai.config import get_stt_config
        return get_stt_config()
    except Exception:
        return {}


class STTEngine(ABC):
    """Abstract speech-to-text interface.

    Subclasses implement ``transcribe()`` for a specific backend (faster-whisper,
    Vosk, etc.) and may override ``transcribe_result()`` to expose confidence.
    """

    @abstractmethod
    def transcribe(self, audio: bytes) -> str:
        """Convert raw audio to text.

        Parameters
        ----------
        audio : bytes
            Raw PCM audio data (16-bit, 16 kHz mono).

        Returns
        -------
        str
            Transcribed text.
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True when the backend model is loaded and ready."""
        ...

    def transcribe_result(self, audio: bytes) -> TranscriptionResult:
        """Transcribe and report confidence.

        Default implementation wraps ``transcribe()`` for backends that expose
        no confidence — an empty transcript is the only "miss" it can detect.
        """
        text = (self.transcribe(audio) or "").strip()
        return TranscriptionResult(
            text=text, low_confidence=not text,
            n_segments=1 if text else 0,
            reason="" if text else "empty (no speech)",
        )


class StubSTT(STTEngine):
    """Placeholder STT that always returns empty string.

    Used during development when no audio hardware / model is present.
    """

    def transcribe(self, audio: bytes) -> str:
        return ""

    def is_available(self) -> bool:
        return False


class VoskSTT(STTEngine):
    """Offline speech-to-text using Vosk (Apache 2.0).

    Requires:
        - ``vosk`` Python package (``pip install vosk``)
        - A Vosk model directory (e.g. ``models/vosk-model-en-us-0.22``)

    Args:
        model_path: Path to the Vosk model directory.
        sample_rate: Audio sample rate in Hz (default 16000).
        min_word_confidence: Mean word confidence below which a transcript is
            flagged ``low_confidence``. Defaults to the configured value.
    """

    def __init__(self, model_path: str | None = None,
                 sample_rate: int = 16000,
                 min_word_confidence: float | None = None):
        if model_path is None:
            try:
                from puffpai.config import get_voice_models
                model_path = get_voice_models()["vosk_model_dir"]
            except Exception:
                model_path = "models/vosk-model-small-en-us"
        self._model_path = model_path
        self._sample_rate = sample_rate
        if min_word_confidence is None:
            min_word_confidence = _stt_config().get("vosk_min_word_confidence", 0.5)
        self._min_word_confidence = min_word_confidence
        self._model = None
        self._loaded = False
        self._load_error: str | None = None

        if not _HAS_VOSK:
            self._load_error = "vosk not installed"
            logger.warning("vosk not installed; VoskSTT unavailable")
            return

        if not os.path.isdir(model_path):
            self._load_error = f"Model directory not found: {model_path}"
            logger.warning("Vosk model not found: %s", model_path)
            return

        try:
            # Suppress Vosk's own logging during model load
            vosk.SetLogLevel(-1)
            self._model = vosk.Model(model_path)
            self._loaded = True
            logger.info("Vosk model loaded: %s", model_path)
        except Exception as e:
            self._load_error = str(e)
            logger.error("Failed to load Vosk model %s: %s", model_path, e)

    def transcribe(self, audio: bytes) -> str:
        """Transcribe PCM 16-bit 16kHz mono audio to text."""
        return self.transcribe_result(audio).text

    def transcribe_result(self, audio: bytes) -> TranscriptionResult:
        if not self._loaded or self._model is None:
            return TranscriptionResult(text="", low_confidence=True,
                                       reason="engine unavailable")
        try:
            recognizer = vosk.KaldiRecognizer(self._model, self._sample_rate)
            # Ask for per-word confidence so the gate has a signal to act on.
            try:
                recognizer.SetWords(True)
            except Exception:
                pass
            recognizer.AcceptWaveform(audio)
            result = json.loads(recognizer.FinalResult())
            text = result.get("text", "").strip()
            words = result.get("result", []) or []
            confs = [w.get("conf") for w in words if isinstance(w, dict) and "conf" in w]
            return evaluate_vosk(text, confs,
                                 min_word_confidence=self._min_word_confidence)
        except Exception as e:
            logger.error("Vosk transcription error: %s", e)
            return TranscriptionResult(text="", low_confidence=True,
                                       reason=f"error: {e}")

    def is_available(self) -> bool:
        return self._loaded and self._model is not None


class WhisperSTT(STTEngine):
    """Offline speech-to-text using faster-whisper (CTranslate2).

    Dramatically better than Vosk on natural, connected speech. Takes the same
    PCM-bytes interface as :class:`VoskSTT`. ``vad_filter`` drops non-speech so
    trailing silence doesn't hallucinate; ``condition_on_previous_text=False``
    keeps each utterance independent.

    Args:
        model: faster-whisper model name/path (e.g. "base.en", "small.en").
        device: "cpu" or "cuda" (this box has no CUDA CTranslate2 → keep "cpu").
        compute_type: e.g. "int8" (CPU), "float16" (GPU).
        min_avg_logprob / max_no_speech_prob: confidence-gate thresholds.
    """

    def __init__(self, model: str | None = None, device: str | None = None,
                 compute_type: str | None = None, sample_rate: int = 16000,
                 min_avg_logprob: float | None = None,
                 max_no_speech_prob: float | None = None):
        cfg = _stt_config()
        self._model_name = model or cfg.get("whisper_model", "base.en")
        self._device = device or cfg.get("whisper_device", "cpu")
        self._compute = compute_type or cfg.get("whisper_compute", "int8")
        self._sample_rate = sample_rate
        self._min_avg_logprob = (min_avg_logprob if min_avg_logprob is not None
                                 else cfg.get("min_avg_logprob", -1.0))
        self._max_no_speech_prob = (max_no_speech_prob if max_no_speech_prob is not None
                                    else cfg.get("max_no_speech_prob", 0.6))
        self._model = None
        self._load_error: str | None = None

        if not _HAS_WHISPER or _WhisperModel is None:
            self._load_error = "faster-whisper not installed"
            logger.warning("faster-whisper not installed; WhisperSTT unavailable")
            return
        try:
            self._model = _WhisperModel(self._model_name, device=self._device,
                                        compute_type=self._compute)
            logger.info("faster-whisper loaded: %s (%s, %s)",
                        self._model_name, self._device, self._compute)
        except Exception as e:
            self._load_error = str(e)
            logger.error("Failed to load faster-whisper %s: %s",
                         self._model_name, e)

    def transcribe(self, audio: bytes) -> str:
        return self.transcribe_result(audio).text

    def transcribe_result(self, audio: bytes) -> TranscriptionResult:
        if self._model is None:
            return TranscriptionResult(text="", low_confidence=True,
                                       reason="engine unavailable")
        if not audio:
            return TranscriptionResult(text="", low_confidence=True,
                                       reason="empty (no speech)")
        try:
            import numpy as np
            arr = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _info = self._model.transcribe(
                arr, language="en", beam_size=1, vad_filter=True,
                condition_on_previous_text=False,
            )
            segs = list(segments)
            text = " ".join(s.text for s in segs).strip()
            logps = [getattr(s, "avg_logprob", None) for s in segs]
            nsps = [getattr(s, "no_speech_prob", None) for s in segs]
            return evaluate_whisper(
                text, logps, nsps,
                min_avg_logprob=self._min_avg_logprob,
                max_no_speech_prob=self._max_no_speech_prob,
                n_segments=len(segs),
            )
        except Exception as e:
            logger.error("faster-whisper transcription error: %s", e)
            return TranscriptionResult(text="", low_confidence=True,
                                       reason=f"error: {e}")

    def is_available(self) -> bool:
        return self._model is not None


def create_stt(engine: str | None = None, **overrides) -> STTEngine:
    """Build the configured STT engine, falling back gracefully.

    Selection order: the requested ``engine`` (or ``config.models.stt_engine``),
    then Vosk, then :class:`StubSTT`. A whisper engine that fails to load (no
    faster-whisper, bad model, no CUDA) transparently falls back to Vosk so the
    device is never left mute. ``overrides`` are passed to the chosen engine's
    constructor (e.g. ``model=``, ``device=``, ``min_avg_logprob=``).
    """
    cfg = _stt_config()
    engine = (engine or cfg.get("engine", "whisper")).lower()

    if engine == "whisper":
        stt = WhisperSTT(**overrides)
        if stt.is_available():
            return stt
        logger.error("WhisperSTT unavailable (%s); falling back to Vosk",
                     getattr(stt, "_load_error", "unknown"))

    vosk_kwargs = {}
    if "vosk_model_dir" in cfg:
        vosk_kwargs["model_path"] = cfg["vosk_model_dir"]
    if engine == "vosk":
        # honour explicit model/threshold overrides meant for vosk
        for k in ("model_path", "sample_rate", "min_word_confidence"):
            if k in overrides:
                vosk_kwargs[k] = overrides[k]
    stt = VoskSTT(**vosk_kwargs)
    if stt.is_available():
        return stt

    logger.error("No STT engine available (whisper + vosk both failed); using Stub")
    return StubSTT()
