"""puffpai.voice.service — on-Orin voice assistant loop.

Direct, self-contained voice pipeline that runs entirely on the appliance:

    ReSpeaker mic ─▶ wake word (hey_puffpai.onnx, onnxruntime)
                  ─▶ record utterance (energy VAD)
                  ─▶ STT (Vosk)
                  ─▶ POST /api/chat  (WITH rolling conversation memory)
                  ─▶ Piper TTS ─▶ Edifier (PulseAudio default sink)

No Home Assistant round-trip and no openWakeWord dependency — the wake model is
a raw-audio Conv1D classifier run straight on onnxruntime. HA remains PuffPAI's
smart-home actuator via the brain; it is not in the voice path.

Run:
    python -m puffpai.voice.service            # live service
    python -m puffpai.voice.service --selftest # exercise components, no live mic
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import glob
import wave
import logging
import tempfile
import threading
import subprocess

import numpy as np

from puffpai import fragments
from puffpai.platform_paths import data_path
from puffpai.voice import ducking
from puffpai.voice import log_privacy


def _model_path(name: str) -> str:
    """Default location of a voice model asset.

    The Founders appliance keeps these in ``/data/puffpai/models``; that is
    simply what ``$PUFFPAI_DATA_DIR`` is set to there. On any other box the
    same layout lands under the per-user state root, so voice models are found
    without editing code — and every one of these is still overridable by its
    own ``PUFF_*_MODEL`` environment variable.
    """
    return data_path("models", name)


# ─────────────────────────── configuration (env-overridable) ────────────────
SAMPLE_RATE      = 16000
BLOCK            = 2000                      # 0.125 s capture blocks
WAKE_MODEL       = os.environ.get("PUFF_WAKE_MODEL", _model_path("hey_puffpai.onnx"))
WAKE_THRESHOLD   = float(os.environ.get("PUFF_WAKE_THRESHOLD", "0.6"))     # real wakes score 0.7-1.0; noise well below
WAKE_COOLDOWN    = float(os.environ.get("PUFF_WAKE_COOLDOWN", "1.5"))
# Auto-gain: the ReSpeaker's processed output is ~6% of full scale but the wake
# model trained near full scale. Gate on RMS first (never amplify silence — that
# false-fires the model), then normalise real speech up to the target peak.
WAKE_GATE        = float(os.environ.get("PUFF_WAKE_GATE", "150"))          # RMS below this ⇒ skip inference
WAKE_TARGET_PEAK = float(os.environ.get("PUFF_WAKE_TARGET_PEAK", "29491")) # 0.9 * 32768
WAKE_MAX_GAIN    = float(os.environ.get("PUFF_WAKE_MAX_GAIN", "25"))
WAKE_NEARMISS    = float(os.environ.get("PUFF_WAKE_NEARMISS", "0.4"))      # log sub-threshold scores (tuning aid)
# Stage-2 confirmation: the raw-audio wake model is a weak discriminator and
# fires at ~1.0 on any full-scale audio (TV, radio, speech). Before actually
# waking, transcribe the trigger buffer and require the wake word to be heard.
WAKE_CONFIRM     = os.environ.get("PUFF_WAKE_CONFIRM", "1") == "1"
WAKE_CONFIRM_KEYS = tuple(k for k in os.environ.get(
    "PUFF_WAKE_CONFIRM_KEYS",
    "puff,puf,pup,poof,pof,puppy,hip hop,hip-hop,puff pie,puff by,"
    "puff pi,puffpai,puff eye,puff hi").lower().split(",") if k)
#: Clipped forms of the name that Whisper writes when it drops the "puff"
#: ("Hey, pie, what time is it"). They cannot live in the comma-separated env
#: list, and the trailing punctuation is what makes a bare tail an ADDRESS
#: rather than an ordinary word ("apple pie and ice cream").
WAKE_CONFIRM_TAILS = ("pie,", "pie.", "eye,", "eye.", "high.", "high,", "pi,", "pi.")
#: A bare tail is the WEAKEST evidence there is: room talk such as
#: ``heard="Very over to the eye."`` would confirm on "eye." alone, pass the
#: address gate as ``trusted`` and get a spoken answer. So a tail only confirms
#: on audio that is SHAPED like an address: short, or opening with the greeting
#: that precedes her name.
WAKE_TAIL_MAX_WORDS = int(os.environ.get("PUFF_WAKE_TAIL_WORDS", "4"))
WAKE_TAIL_HEADS = frozenset(("hey", "hay", "hi", "a", "okay", "ok"))
#: Seconds of audio captured AFTER the trigger buffer for the confirm pass.
#: Together with the 1 s trigger window this is the span that must contain
#: "hey puff pie" — keep it short: every wake pays for this transcription.
WAKE_CONFIRM_WINDOW = float(os.environ.get("PUFF_WAKE_CONFIRM_WINDOW", "1.2"))
#: Name bias for the CONFIRM pass only. faster-whisper >= 1.0.2 takes
#: ``hotwords``; on older builds the same string is used as ``initial_prompt``.
#: The full-command transcription stays unbiased — biasing it would put her
#: name into ordinary sentences. Empty string disables.
#: DEFAULT EMPTY. The biased pass is a loaded die, so the code default leaves
#: it off. Set the env to re-enable the
#: two-pass path, which is kept intact below.
WAKE_CONFIRM_HOTWORDS = os.environ.get("PUFF_WAKE_CONFIRM_HOTWORDS", "")
#: The hotwords bias is a LOADED DIE: told to expect "Hey Puff Pie", Whisper
#: writes it onto room noise ("Puff", "Puff Puff Pie", "Hello my name is Puff
#: Pie and I am here to submit to you ple"), and such false wakes score
#: conf <= -0.79, i.e. the decoder itself says it is guessing. So the confirm
#: is TWO passes: an honest unbiased
#: one first, and the biased one only as a tie-breaker on a short pass that
#: heard *something*, with a confidence floor and a length cap under it.
#: A hotwords-assisted match must be short (the name, not a paragraph around
#: it) and must not be a guess.
WAKE_CONFIRM_MIN_CONF = float(os.environ.get("PUFF_WAKE_CONFIRM_MIN_CONF", "-0.75"))
#: Hard floor under BOTH passes: below this the transcript is noise whatever
#: it says, including an unbiased "puff".
WAKE_CONFIRM_FLOOR = float(os.environ.get("PUFF_WAKE_CONFIRM_FLOOR", "-1.0"))
#: Pass 1 longer than this is a sentence from the room — not a missed name, so
#: no second, biased look at it.
WAKE_CONFIRM_PASS1_MAX_WORDS = int(os.environ.get("PUFF_WAKE_CONFIRM_PASS1_WORDS", "4"))
#: …and a hotwords-assisted transcript longer than this is the bias running on
#: ("Hello my name is Puff Pie and I am here to submit to you").
WAKE_CONFIRM_PASS2_MAX_WORDS = int(os.environ.get("PUFF_WAKE_CONFIRM_PASS2_WORDS", "6"))
#: Safety cap on the BRIDGE - the audio captured while the stage-2 confirm is
#: transcribing. It bounds a pathological transcription (a wedged engine must
#: not turn into a minute-long buffer), not normal operation: the confirm pass
#: costs ~1.1 s on a Jetson AGX Orin.
WAKE_BRIDGE_MAX  = float(os.environ.get("PUFF_WAKE_BRIDGE_MAX", "4.0"))
#: Two stage-2 rejects this close together mean she is TRYING and we keep
#: missing her — that alone is evidence enough to arm the peek duck.
WAKE_RETRY_WINDOW = float(os.environ.get("PUFF_WAKE_RETRY_WINDOW", "12"))
# Barge-in: keep the wake model running DURING a capture so a confirmed
# "hey puff pie, stop" aborts a runaway recording instead of queueing behind it.
BARGE_IN         = os.environ.get("PUFF_BARGE_IN", "1") == "1"
#: The wake model fires on the speaker's own voice, so a barge-in check is only
#: worth its transcription once a capture has already run long.
BARGE_IN_AFTER   = float(os.environ.get("PUFF_BARGE_IN_AFTER", "3.0"))
BARGE_IN_COOLDOWN = float(os.environ.get("PUFF_BARGE_IN_COOLDOWN", "1.5"))
BARGE_IN_MAX_RESTARTS = int(os.environ.get("PUFF_BARGE_IN_MAX_RESTARTS", "2"))
CAPTURE_DEVICE   = os.environ.get("PUFF_CAPTURE_DEVICE", "pulse")   # PortAudio device
# Capture watchdog: PortAudio's blocking read has no timeout, so a wedged ALSA/
# Pulse capture PCM would otherwise park the whole loop forever.
CAPTURE_STALL_TIMEOUT = float(os.environ.get("PUFF_CAPTURE_STALL_TIMEOUT", "15"))
CAPTURE_MAX_REOPEN    = int(os.environ.get("PUFF_CAPTURE_MAX_REOPEN", "2"))
OUTPUT_SINK      = os.environ.get("PUFF_SINK", "")                   # "" = Pulse default sink
BRAIN_URL        = os.environ.get("PUFF_BRAIN_URL", "http://127.0.0.1:8000/api/chat")
# Who the brain should treat this satellite as, for the 300-second
# vault-unlocked chat window it keys on X-Puff-Device-Id
# (api_server._get_chat_client_id). A fixed, non-secret, non-guessable-shape
# label: the backend honours it only from a trusted proxy peer (loopback —
# which is where this service runs), so it is not a credential and asserting it
# from the LAN buys nothing. The point is separation, not authentication: the
# shared speaker gets its OWN bucket instead of sharing the loopback bucket
# with dashboard-proxy requests that carry no verified pairing cookie. Anyone
# who can speak in the room shares this one session, which is the same trust
# boundary the microphone already has.
BRAIN_SESSION_ID = os.environ.get("PUFF_BRAIN_SESSION_ID", "puff-voice-satellite")
VOSK_MODEL       = os.environ.get("PUFF_VOSK_MODEL", _model_path("vosk-model-en-us-0.22-lgraph"))
PIPER_MODEL      = os.environ.get("PUFF_PIPER_MODEL", _model_path("en_US-hfc_female-medium.onnx"))
STT_ENGINE       = os.environ.get("PUFF_STT_ENGINE", "whisper")       # "whisper" | "vosk"
WHISPER_MODEL    = os.environ.get("PUFF_WHISPER_MODEL", "base.en")     # tiny.en faster / small.en better
WHISPER_DEVICE   = os.environ.get("PUFF_WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE  = os.environ.get("PUFF_WHISPER_COMPUTE", "int8")

VAD_THRESHOLD    = float(os.environ.get("PUFF_VAD_THRESHOLD", "100"))  # RMS on the quiet ReSpeaker feed; ambient ~35
# A fixed VAD threshold is right for a quiet room and wrong for a room with
# ducked music or a fan in it: ambient sits ABOVE the threshold, so the VAD
# never sees silence and every capture runs to the hard cap. So
# each capture samples the ambient level it actually starts in and lifts its
# own threshold to floor+margin, never below PUFF_VAD_THRESHOLD.
VAD_CALIBRATE    = os.environ.get("PUFF_VAD_CALIBRATE", "1") == "1"
VAD_CALIBRATE_SECONDS = float(os.environ.get("PUFF_VAD_CALIBRATE_SECONDS", "0.3"))
VAD_MARGIN       = float(os.environ.get("PUFF_VAD_MARGIN", "80"))
#: A calibration block louder than this is SPEECH, not ambient — she was
#: answered in one breath ("hey puff pie, what time is it"). Calibration stops
#: there and the block starts the utterance, so a one-breath command can never
#: calibrate the threshold up to its own level and be discarded.
VAD_CALIBRATE_MAX = float(os.environ.get("PUFF_VAD_CALIBRATE_MAX", "400"))
STT_MAX_GAIN     = float(os.environ.get("PUFF_STT_MAX_GAIN", "20"))    # peak-normalise the utterance before STT
SILENCE_HANG     = float(os.environ.get("PUFF_SILENCE_HANG", "1.0"))   # s of silence ends an utterance
# A 1.0 s hang is right for a whole sentence and wrong for one that starts with
# a fronted time phrase: in "every Tuesday, <pause> Wednesday and
# Thursday..." the pause closes the utterance. Raising SILENCE_HANG globally
# would tax EVERY turn with
# the same latency, so instead the hang stays at 1.0 s and a captured utterance
# that comes back as an obvious FRAGMENT re-opens the mic exactly once for
# PUFF_FRAGMENT_EXTEND seconds. Set it to 0 to disable the guard.
FRAGMENT_EXTEND  = float(os.environ.get("PUFF_FRAGMENT_EXTEND", "1.5"))
START_TIMEOUT    = float(os.environ.get("PUFF_START_TIMEOUT", "6.0"))  # s to wait for speech after wake
FOLLOWUP_WINDOW  = float(os.environ.get("PUFF_FOLLOWUP_WINDOW", "10.0"))# s of follow-up window, from the END of TTS
# Hard cap on one utterance. A single 12 s cap is both too short and too long:
# an 8 s calendar sentence can be cut mid-word, while every wake on room
# conversation records a full 12 s before the address gate throws it away. The
# cap follows the evidence: a wake-word-CONFIRMED capture gets room for a
# long sentence, an unconfirmed or peek capture is cut short.
_MAX_UTT_ENV     = os.environ.get("PUFF_MAX_UTTERANCE", "")
MAX_UTTERANCE    = float(_MAX_UTT_ENV or "12.0")                       # default for untyped callers
MAX_UTTERANCE_CONFIRMED   = float(_MAX_UTT_ENV or os.environ.get("PUFF_MAX_UTTERANCE_CONFIRMED", "20.0"))
MAX_UTTERANCE_UNCONFIRMED = float(_MAX_UTT_ENV or os.environ.get("PUFF_MAX_UTTERANCE_UNCONFIRMED", "6.0"))
HISTORY_TURNS    = int(os.environ.get("PUFF_HISTORY_TURNS", "6"))      # user+assistant pairs kept
FOLLOWUP         = os.environ.get("PUFF_FOLLOWUP", "0") == "1"         # hands-free follow-up window (default OFF)
MAX_FOLLOWUPS    = int(os.environ.get("PUFF_MAX_FOLLOWUPS", "3"))      # cap consecutive follow-ups if enabled
#: WHEN a follow-up window may open. ``question`` (default) opens one only
#: after a reply that ASKED something; ``always`` opens one after every reply.
#: With ``always``, ``wake=followup addressed=false`` captures of ordinary
#: room talk ("So, they can't go.", "Yep.", "I'm ready.") are acted on: every
#: reply re-opens the window, so room talk chains indefinitely and she ends up
#: listening and answering all day. A window is a CONVERSATIONAL TURN she is
#: owed, and she is only owed one when she asked.
#:
#: ``question`` has the opposite cost: natural follow-ups such as "Can you find
#: a different jazz radio?", "stop the music." or "Add a reminder to do the
#: dish." all die as ``wake=rejected reason=no-wakeword`` when the reply before
#: them was a STATEMENT. So the default is ``request``: a window opens after ANY spoken
#: reply, and what is acted on inside it is decided by the WORDS, not by the
#: punctuation of the previous sentence. Room talk does not sound like a
#: request; "Can you …", "Play …", "Add …" do.
FOLLOWUP_MODE    = os.environ.get("PUFF_FOLLOWUP_MODE", "request").strip().lower()
#: A follow-up capture this uncertain is the room, not something said to her
#: (room talk acted on as a follow-up scores between -0.7 and -1.2).
#: The floor is -0.8 rather than -0.75: a genuine "Play K exp radio" can score
#: ``conf=-0.78``, and a -0.75 floor would throw that request away.
FOLLOWUP_MIN_CONF = float(os.environ.get("PUFF_FOLLOWUP_MIN_CONF", "-0.8"))
#: How many wake-LESS turns may chain before she asks for her name again.
FOLLOWUP_MAX_CHAIN = int(os.environ.get("PUFF_FOLLOWUP_MAX_CHAIN", "3"))
#: Answers that mean "we are done" — the window closes with no brain call and
#: no reply, so saying "thanks" cannot start another exchange.
FOLLOWUP_CLOSE_WORDS = frozenset((
    "stop", "never mind", "nevermind", "cancel", "that's all", "thats all",
    "thanks", "thank you", "no thanks", "no thank you",
))
# ── interrupting her mid-sentence ──────────────────────────────────────────
#: Barge-in during a CAPTURE alone is not enough: "stop" spoken over a long
#: answer would not be heard until she had finished saying it.
#: Playback runs as a subprocess we can kill while the wake model keeps
#: eating mic blocks — the XVF3800's echo canceller removes her own voice, so
#: the mic floor during playback is ~25 RMS against a 150 gate.
TTS_INTERRUPT    = os.environ.get("PUFF_TTS_INTERRUPT", "1") == "1"
#: Ignore triggers in the first moments of playback — that is her own onset
#: leaking through before the canceller settles, not a person.
TTS_INTERRUPT_GRACE = float(os.environ.get("PUFF_TTS_INTERRUPT_GRACE", "0.4"))
#: Audio captured after an interrupt trigger, transcribed UNBIASED.
TTS_INTERRUPT_CAPTURE = float(os.environ.get("PUFF_TTS_INTERRUPT_CAPTURE", "0.9"))
#: Kept from the trigger BEFORE the check starts: a shouted "stop" is over
#: before a capture that begins at the trigger can hold all of it.
TTS_INTERRUPT_PREROLL = float(os.environ.get("PUFF_TTS_INTERRUPT_PREROLL", "0.3"))
#: The check ends early on this much silence — a one-syllable word must not
#: cost the whole capture window.
TTS_INTERRUPT_SILENCE = float(os.environ.get("PUFF_TTS_INTERRUPT_SILENCE", "0.25"))
#: How long the mic must stay loud before a check is worth a transcription.
TTS_INTERRUPT_RUN = float(os.environ.get("PUFF_TTS_INTERRUPT_RUN", "0.2"))
#: Minimum gap between two checks, so one sentence spoken over her does not
#: cost a transcription per block.
TTS_INTERRUPT_COOLDOWN = float(os.environ.get("PUFF_TTS_INTERRUPT_COOLDOWN", "0.6"))
#: ABSOLUTE RMS floor for "someone is talking over her".
#: Gating the interrupt on the WAKE MODEL firing during playback does not work:
#: a one-syllable "stop" does not trip a model trained on "hey puffpai", so a
#: 16-second answer would survive repeated "stop"s.
#: Energy, not the name, opens the check. Measured on the appliance: mic RMS
#: during her own playback sits at p50≈25 (the XVF3800 cancels the far end),
#: so 180 is far above her own voice and well below a person in the room.
TTS_INTERRUPT_RMS = float(os.environ.get("PUFF_TTS_INTERRUPT_RMS", "180"))
#: …and the gate is also at least this many times the ambient floor.
TTS_INTERRUPT_RMS_FACTOR = float(os.environ.get("PUFF_TTS_INTERRUPT_FACTOR", "2.0"))
#: If the AEC is NOT keeping her own voice down (p95 of the grace window above
#: the absolute floor), the gate is derived from what she herself measures at,
#: or she interrupts herself on every sentence.
TTS_SELFRMS_FACTOR = float(os.environ.get("PUFF_TTS_SELFRMS_FACTOR", "1.6"))
#: "Be quiet" — spoken over her, with no wake word. Deliberately usable ONLY
#: while she is speaking; there is no always-listening stop command.
TTS_STOP_WORDS = (
    "stop", "quiet", "be quiet", "shut up", "enough", "cancel", "never mind",
    "nevermind", "okay okay", "ok okay", "that's enough", "thats enough",
    "stop it", "stop stop", "okay stop", "ok stop", "puff stop",
    "puff pie stop", "hush", "shush", "pause",
)
#: What Whisper writes when a short, shouted "stop" is all it gets. Tolerated
#: ONLY as the whole transcript — "top" inside a sentence is a word, and
#: "stopped" inside one is somebody telling a story.
TTS_STOP_WHOLE_ONLY = ("stopped", "top", "stahp", "stopp")
#: A stop is a word or three shouted over her. Anything longer is the room
#: talking, and obeying it would make her stop at every passing sentence.
TTS_INTERRUPT_MAX_WORDS = int(os.environ.get("PUFF_TTS_INTERRUPT_MAX_WORDS", "5"))
ANNOUNCE_ONLINE  = os.environ.get("PUFF_ANNOUNCE", "1") == "1"
READY_PATH       = "/tmp/puff_ready.wav"   # rising chime: "talk now"
SLEEP_PATH       = "/tmp/puff_sleep.wav"   # falling tone: "done, back to sleep"

STOP_PHRASES = {
    "stop", "stop listening", "stop puff", "never mind", "nevermind",
    "cancel", "quiet", "shut up", "go away", "that's all", "thats all",
    "thank you that's all", "goodbye", "bye",
}

LOG_DIR = data_path("logs")

# ── the stop that cannot fail: a control file both units can reach ─────────
# Energy-VAD + Whisper is still a guess about a room. A dashboard button, the
# kiosk, or a phone must be able to stop her with NO acoustics involved at all:
# the API writes voice_control.json, this loop polls it every 100 ms while it
# speaks. Same state dir as announcements.jsonl, which the API unit already
# writes to, so no new sandbox path is needed.
VOICE_CONTROL_FILE = "voice_control.json"
VOICE_STATE_FILE = "voice_state.json"
#: How often the control file is read while she talks.
CONTROL_POLL_SECONDS = float(os.environ.get("PUFF_VOICE_CONTROL_POLL", "0.1"))


def _state_dir() -> str:
    """Resolved at call time: a test moving ``$PUFFPAI_DATA_DIR`` must be seen.

    Same resolver as the API's ``/voice/stop`` writer and the announcement
    queue, so both processes agree on one directory on every OS.
    """
    from puffpai.statedir import resolve_runtime_dir
    return resolve_runtime_dir(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), "data"))


def voice_control_path() -> str:
    return os.path.join(_state_dir(), VOICE_CONTROL_FILE)


def voice_state_path() -> str:
    return os.path.join(_state_dir(), VOICE_STATE_FILE)

# ── transcript retention (privacy contract) ────────────────────────────────
# The wake model is a SPEECH detector, not a name detector: it fires at ~1.0 on
# any full-scale audio, so every conversation in the room wakes the loop and
# gets transcribed by Whisper before ``parse_address`` decides we were not being
# spoken to. Writing that transcript to disk turns the appliance into a verbatim
# recorder of household speech that was never addressed to it — a Zone-1
# violation, whatever the retention window. Transcript TEXT is therefore logged
# only for utterances that passed the address gate; everything else gets a
# no-text event (wake=fired addressed=false len=… conf=…) that still supports
# tuning the gate, because a character count and a confidence score are not
# speech.
#
# The opt-in below exists for developing the wake/address gate on a box with no
# household on it. It defaults OFF and must stay OFF on a family appliance.
LOG_TRANSCRIPTS  = os.environ.get("PUFF_LOG_TRANSCRIPTS", "0") == "1"
# voice.log is size-capped, not time-capped: 3 generations of 2 MB is a hard
# 6 MB ceiling regardless of how chatty the room is.
VOICE_LOG_MAX_BYTES = int(os.environ.get("PUFF_VOICE_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
VOICE_LOG_BACKUPS   = int(os.environ.get("PUFF_VOICE_LOG_BACKUPS", "2"))

logger = logging.getLogger("puffpai.voice")


def _setup_logging() -> None:
    logger.setLevel(logging.DEBUG if LOG_TRANSCRIPTS else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if LOG_TRANSCRIPTS:
        logger.warning(
            "PUFF_LOG_TRANSCRIPTS=1 — verbatim transcripts of ALL speech the "
            "mic hears, addressed or not, are being written to voice.log and "
            "the journal. Development only; do not enable on a family box.")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        # Rotating + 0640 via the shared project handler: an unrotated,
        # world-readable voice.log would retain speech without limit.
        from puffpai.logging_config import make_rotating_file_handler
        fh = make_rotating_file_handler(
            os.path.join(LOG_DIR, "voice.log"),
            max_bytes=VOICE_LOG_MAX_BYTES, backup_count=VOICE_LOG_BACKUPS,
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as exc:  # logging to file is best-effort
        logger.warning("file log unavailable: %s", exc)


def _utterance_event(**fields) -> str:
    """Render a no-text utterance event: ``k=v k=v`` in the order given."""
    return " ".join(f"{k}={v}" for k, v in fields.items())


#: Hard ceiling on how much of a stage-2 transcript ever reaches a log line.
#: The confirm window is ~2.2 s of audio and the ONLY question asked of it is
#: "was her name in there", so a bounded excerpt is enough to debug a reject
#: without turning the journal into a transcript of the room.
HEARD_MAX = 60


def _heard(text: str) -> str:
    """Render a stage-2 transcript for a log line: quoted, escaped, bounded.

    Stage-2 audio is the trigger window, not a command, and without it a
    ``no-wakeword`` reject is unfalsifiable — nobody can tell "puppy" from
    "buffet" from silence. Newlines collapse so one event stays one line.

    A PIN-SHAPED excerpt, and anything at all while a vault challenge is
    armed, is replaced by a constant instead: this journal is durable and
    every caller here fires BEFORE the brain could say otherwise. Ordinary
    speech stays readable -- these lines are how a missed wake or a failed
    stop is diagnosed (see voice.log_privacy).
    """
    flat = log_privacy.safe_heard(text, limit=HEARD_MAX,
                                  hide=hide_utterance(text))
    return '"' + flat.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fmt_conf(value) -> str:
    """Render one mean log-probability for a log line."""
    return "n/a" if value is None else f"{value:.2f}"


def _conf_of(stt) -> str:
    """Last transcription's mean log-probability, or ``n/a``.

    A confidence score is a property of the audio, not its content, so it is
    safe to log for un-addressed utterances and is the one number that makes
    the address gate tunable without the text.
    """
    return _fmt_conf(getattr(stt, "last_avg_logprob", None))


def _log_unaddressed(stt, text: str) -> None:
    """Log a wake that did not pass the address gate — WITHOUT the transcript."""
    logger.info("%s", _utterance_event(
        wake="fired", addressed="false", len=len(text or ""), conf=_conf_of(stt)))
    if LOG_TRANSCRIPTS:
        logger.debug("  (opt-in) not addressed: %r",
                     log_privacy.safe_heard(text,
                                            hide=hide_utterance(text)))


# ─────────────────────────────── wake word ─────────────────────────────────
class WakeWord:
    """Raw-audio wake detector: 1 s window of normalised float32 → score."""

    def __init__(self, path: str, threshold: float):
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.log_severity_level = 3
        self.sess = ort.InferenceSession(path, sess_options=so,
                                         providers=["CPUExecutionProvider"])
        self.iname = self.sess.get_inputs()[0].name
        self.oname = self.sess.get_outputs()[0].name
        self.threshold = threshold
        self.buf = np.zeros(SAMPLE_RATE, dtype=np.int16)

    def push(self, samples: np.ndarray) -> float:
        n = len(samples)
        if n >= SAMPLE_RATE:
            self.buf = samples[-SAMPLE_RATE:].astype(np.int16).copy()
        elif n:
            self.buf = np.roll(self.buf, -n)
            self.buf[-n:] = samples
        buf = self.buf.astype(np.float32)
        # Energy gate BEFORE gain: never normalise ambient/silence up to full
        # scale — the model false-fires on that. Below the gate it cannot be the
        # wake word at speaking volume anyway.
        if np.sqrt(np.mean(buf * buf)) < WAKE_GATE:
            return 0.0
        gain = min(WAKE_MAX_GAIN, WAKE_TARGET_PEAK / (np.max(np.abs(buf)) + 1e-9))
        x = (np.clip(buf * gain, -32768, 32767) / 32768.0).reshape(1, SAMPLE_RATE, 1)
        return float(self.sess.run([self.oname], {self.iname: x})[0].ravel()[0])

    def reset(self) -> None:
        self.buf[:] = 0

    def buffer_bytes(self) -> bytes:
        """The current 1 s window as PCM16 — fed to STT for stage-2 confirm."""
        return self.buf.astype(np.int16).tobytes()


# ─────────────────────────────── audio helpers ─────────────────────────────
def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(x.astype(np.float32) ** 2)))


def normalize_utterance(pcm: bytes, target: float = 0.6) -> bytes:
    """Peak-normalise a quiet ReSpeaker utterance toward full scale so STT hears
    a normal speaking level."""
    if not pcm:
        return pcm
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    peak = float(np.max(np.abs(a))) + 1e-9
    gain = min(STT_MAX_GAIN, target * 32768.0 / peak)
    return np.clip(a * gain, -32768, 32767).astype(np.int16).tobytes()


def _write_tone(path: str, notes) -> None:
    """notes = [(freq_hz, dur_s, amplitude), ...] rendered with soft fades."""
    sr = 22050
    parts = []
    for freq, dur, amp in notes:
        t = np.linspace(0, dur, int(sr * dur), endpoint=False)
        env = np.minimum(1.0, np.minimum(t * 60, (dur - t) * 60))
        parts.append((amp * env * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16))
    pcm = np.concatenate(parts)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def make_cues() -> None:
    """Two distinct, mirror-image cues so the interaction has clear boundaries."""
    _write_tone(READY_PATH, [(587, 0.10, 0.34), (880, 0.15, 0.36)])  # rising: talk now
    _write_tone(SLEEP_PATH, [(587, 0.10, 0.22), (392, 0.15, 0.22)])  # falling: back to sleep


def _paplay_cmd(path: str) -> list:
    cmd = ["paplay"]
    if OUTPUT_SINK:
        cmd += ["--device", OUTPUT_SINK]
    cmd.append(path)
    return cmd


def _paplay(path: str) -> None:
    try:
        subprocess.run(_paplay_cmd(path), check=False)
    except Exception as exc:
        logger.error("paplay failed: %s", exc)


def _paplay_async(path: str):
    """Start playback and hand back the handle — an interruptible ``_paplay``."""
    return subprocess.Popen(_paplay_cmd(path))


# ── music ducking: lower playback while she is TALKING TO US ────────────────
# We snapshot the CURRENT sink-inputs (World Radio/mpv, Jellyfin, dashboard
# audio) and lower them; Puff's own reply — a sink-input started AFTER the
# snapshot — is not ducked and plays at full over the dip. Restored on every
# exit path. Best-effort; never raises.
# NOTE: Spotify (librespot) drives bare ALSA (hw:APE,0),
# NOT this sink, so they are ducked by neither this lever nor the XVF3800's
# echo canceller — that needs the librespot->PulseAudio routing change.
#
# The policy itself lives in puffpai.voice.ducking — see that module's docstring
# for why we no longer duck the moment the wake model fires (it is a speech
# detector, so that dipped the music every time anyone in the room spoke) and
# how the peek path keeps waking over loud music working anyway.
_DUCK = ducking.DuckPolicy()
#: A transcript at or below this mean avg_logprob is treated as "we probably
#: mis-heard her", which is one of the triggers for arming a peek duck.
STT_WEAK_LOGPROB = float(os.environ.get("PUFF_STT_WEAK_LOGPROB", "-0.8"))


def _duck_playback() -> None:
    """Deep duck for an addressed exchange (kept for callers/tests)."""
    _DUCK.on_addressed()


def _restore_playback() -> None:
    _DUCK.restore()


#: A pass this long is a sentence, and a sentence sung or spoken over music is
#: not a wake we mis-heard. Words, after punctuation is stripped.
WAKE_SHAPED_MAX_WORDS = int(os.environ.get("PUFF_WAKE_SHAPED_MAX_WORDS", "5"))
#: …and a pass this short is an interjection ("Oh", "Hello", "I do", "Ahhhh",
#: the kind of thing singing along produces). Only a NAME fragment rescues
#: those; low confidence alone does not, or every held note arms a peek.
WAKE_SHAPED_MIN_WORDS = int(os.environ.get("PUFF_WAKE_SHAPED_MIN_WORDS", "3"))
#: Half-heard pieces of "hey puff pie". Deliberately looser than
#: ``WAKE_CONFIRM_KEYS`` (which must be strict enough to START a capture):
#: these only say "something name-shaped was in the room".
_NAME_FRAGMENTS = frozenset((
    "hey", "hay", "puff", "puf", "pof", "poof", "pup", "puppy", "puffy",
    "pie", "pi", "pye", "py", "pa", "pai", "pah",
))


#: Punctuation Whisper hangs off the edges of a word.
_EDGE_PUNCT = " ,.!?-'\"“”‘’…"


def _clean_tokens(tokens) -> list[str]:
    """Strip edge punctuation from each token, KEEPING the 1:1 positions.

    Empties are kept so a cleaned list can be indexed alongside the raw one —
    that is what lets the address parser match on clean words but rebuild the
    command from the original text.
    """
    return [t.strip(_EDGE_PUNCT) for t in tokens]


def _words(text: str) -> list[str]:
    """Transcript words, punctuation and quoting stripped."""
    return [w for w in _clean_tokens((text or "").lower().split()) if w]


def _name_fragment_heard(text: str) -> bool:
    """Was any piece of her name in this transcript?"""
    low = (text or "").lower()
    if _fuzzy_name_heard(low):
        return True
    words = _words(low)
    if any(w in _NAME_FRAGMENTS for w in words):
        return True
    return any(_FUZZY_HEAD.match(w) for w in words)


def _looks_like_missed_wake(stt, text: str) -> bool:
    """Did this un-addressed pass look like a wake we failed to hear?

    Two things can make that true, and both need the transcript to be SHORT:
    anything longer, over music, is someone singing:

    * a fragment of her name IS in there (late in the utterance, so
      ``parse_address()`` could not take it, or clipped past the confirm keys);
    * or it is command-length (3–5 words) and the confidence is poor, which is
      the case of "Hey PuffPAI, what time is it?" decoded over loud
      music as "What time is it?" with her name eaten.

    Anything else — an empty pass, a one-word interjection, a whole sung line —
    is NOT evidence. Those are exactly what belting out a chorus produces.
    """
    low = (text or "").lower()
    if any(key in low for key in _ADDRESS_KEYS):
        return True
    words = _words(low)
    if not words or len(words) > WAKE_SHAPED_MAX_WORDS:
        return False
    if _name_fragment_heard(low):
        return True
    if len(words) < WAKE_SHAPED_MIN_WORDS:
        return False
    conf = getattr(stt, "last_avg_logprob", None)
    return conf is not None and conf <= STT_WEAK_LOGPROB


def play_ready() -> None:
    if os.path.exists(READY_PATH):
        _paplay(READY_PATH)


def play_sleep() -> None:
    if os.path.exists(SLEEP_PATH):
        _paplay(SLEEP_PATH)


def _tts_wav(tts, text: str) -> str | None:
    """Render ``text`` to a temp wav path, or None when there is nothing to say."""
    text = (text or "").strip()
    if not text:
        return None
    pcm = tts.speak(text)
    if not pcm:
        logger.warning("TTS produced no audio for: %s",
                       log_privacy.safe_reply(text, limit=60,
                                              private=LAST_REPLY_PRIVATE))
        return None
    path = tempfile.mktemp(suffix=".wav")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(tts.sample_rate))
        w.writeframes(pcm)
    return path


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def speak(tts, text: str) -> None:
    path = _tts_wav(tts, text)
    if path is None:
        return
    try:
        _paplay(path)
    finally:
        _unlink(path)


def is_tts_stop(text: str) -> bool:
    """Is this short transcript someone telling her to be quiet?

    Matched on normalised tokens anywhere inside a transcript of at most
    ``TTS_INTERRUPT_MAX_WORDS`` words: a shouted "stop" comes back from Whisper
    as "Stop!", "Stop.", "Okay, stop", "Puff, stop" — the word is rarely alone
    and never at a fixed place. The tolerant forms ("stopped", "top") are the
    same syllable mangled by a half-captured shout, so they count ONLY as the
    entire transcript, where nothing else could have been meant.
    """
    words = _words(text)
    if not words or len(words) > TTS_INTERRUPT_MAX_WORDS:
        return False
    joined = " ".join(words)
    if joined in TTS_STOP_WHOLE_ONLY:
        return True
    padded = " %s " % joined
    return any(" %s " % w in padded for w in TTS_STOP_WORDS)


def _stop_player(player) -> None:
    """Kill playback now. Best-effort: a dead player must not raise."""
    try:
        if player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=1.0)
            except Exception:
                player.kill()
    except Exception as exc:
        logger.debug("player terminate failed: %s", exc)


def capture_until_silence(stream, max_seconds: float, *, threshold: float,
                          silence_seconds: float) -> bytes:
    """Read up to ``max_seconds``, stopping early after ``silence_seconds`` quiet.

    A stop word is one syllable; waiting out a fixed window after it would add
    most of a second to every interrupt.
    """
    frames = bytearray()
    need = int(max_seconds * SAMPLE_RATE)
    quiet = 0.0
    block_seconds = BLOCK / float(SAMPLE_RATE)
    while len(frames) // 2 < need:
        data, _ = stream.read(BLOCK)
        samples = data[:, 0] if data.ndim > 1 else data
        frames += samples.tobytes()
        quiet = 0.0 if rms(samples) >= threshold else quiet + block_seconds
        if quiet >= silence_seconds:
            break
    return bytes(frames)


def interrupt_heard(stream, stt, *, pre: bytes = b"",
                    threshold: float = 0.0) -> tuple:
    """Transcribe the moment around a trigger: ``(match, text, ms)``.

    ``match`` is ``stop`` | ``name`` | ``""``. Unbiased on purpose — the
    hotwords bias is exactly what writes her name onto room noise (see
    ``WAKE_CONFIRM_MIN_CONF``). ``pre`` is the pre-roll held from before the
    trigger: the loudest part of a shouted "stop" happens BEFORE the energy
    run is long enough to be believed, so a check that starts at the trigger
    hears only its tail.
    """
    t0 = time.perf_counter()
    try:
        tail = capture_until_silence(
            stream, TTS_INTERRUPT_CAPTURE,
            threshold=threshold or VAD_THRESHOLD,
            silence_seconds=TTS_INTERRUPT_SILENCE)
        text = stt.transcribe(normalize_utterance(pre + tail)).strip()
    except Exception as exc:
        logger.debug("interrupt check failed: %s", exc)
        return "", "", (time.perf_counter() - t0) * 1000.0
    ms = (time.perf_counter() - t0) * 1000.0
    if is_tts_stop(text):
        return "stop", text, ms
    if wake_word_heard(text):
        return "name", text, ms
    return "", text, ms


def _percentile(values, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return float(ordered[idx])


def interrupt_gate(floor: float | None, selfrms_p95: float) -> float:
    """The RMS a block must beat to be a person talking over her.

    Three claims, highest wins: twice the ambient floor of the room she is
    speaking into, the absolute ``PUFF_TTS_INTERRUPT_RMS`` floor, and — only
    when the echo canceller is NOT doing its job (her own voice measured above
    that floor during the grace) — a multiple of what she herself measures at.
    """
    gate = max(TTS_INTERRUPT_RMS,
               (floor or 0.0) * TTS_INTERRUPT_RMS_FACTOR)
    if selfrms_p95 > TTS_INTERRUPT_RMS:
        gate = max(gate, TTS_SELFRMS_FACTOR * selfrms_p95)
    return gate


# ── the control channel ────────────────────────────────────────────────────
def read_stop_control(since: float):
    """A stop command newer than ``since``? Returns its record or None.

    The file is consumed either way: a stale command left on disk would fire
    at the start of the NEXT thing she says.
    """
    path = voice_control_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.debug("voice control unreadable: %s", exc)
        record = None
    try:
        os.unlink(path)
    except OSError:
        pass
    if not isinstance(record, dict) or record.get("cmd") != "stop":
        return None
    try:
        ts = float(record.get("ts") or 0.0)
    except (TypeError, ValueError):
        return None
    return record if ts >= since else None


def write_speaking_state(speaking: bool, since: float) -> None:
    """Publish whether the room's speaker is busy (the dashboard reads this)."""
    try:
        path = voice_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"speaking": bool(speaking), "since": since}, fh)
        os.replace(tmp, path)
    except Exception as exc:               # state publishing is a courtesy
        logger.debug("voice state write failed: %s", exc)


def speak_interruptible(tts, text: str, stream, wake, stt,
                        *, floor: float | None = None) -> str:
    """Speak, listening the whole time. Returns ``""`` | ``stop`` | ``name``.

    ``stop``  — she was told to be quiet: playback dies, nothing is said.
    ``name``  — she was re-addressed mid-sentence: playback dies and the caller
                treats it as a fresh confirmed wake.

    Two independent ways to be stopped, because acoustics are a guess:

    * **Energy, not the wake model**. Requiring the wake model to fire
      during playback before a check runs does not work: a one-syllable
      "stop" does not trip a model trained on "hey puffpai", so a long answer
      would survive repeated "stop"s. A run of
      loud mic blocks is enough to buy one short, unbiased transcription, and
      EVERY such check logs ``tts=check`` — a failure nobody can see is a
      failure nobody can fix.
    * **The control file**, polled every 100 ms: a POST to ``/voice/stop``
      kills playback with no microphone involved at all.
    """
    path = _tts_wav(tts, text)
    if path is None:
        return ""
    started = time.time()
    write_speaking_state(True, started)
    try:
        player = _paplay_async(path)
    except Exception as exc:
        logger.error("paplay failed: %s", exc)
        _unlink(path)
        write_speaking_state(False, started)
        return ""
    reason = ""
    gate = interrupt_gate(floor, 0.0)
    pre_roll = bytearray()
    pre_roll_max = int(TTS_INTERRUPT_PREROLL * SAMPLE_RATE) * 2
    grace_levels: list = []
    graced = False
    loud = 0.0
    # Grace, run length and cooldown are measured in AUDIO CONSUMED, not wall
    # clock: the mic is the clock here (one block is 0.125 s of real time by
    # construction), and a wall-clock grace would be over before the first
    # block on a box under load. The control poll below is the one thing that
    # IS wall-clock — latency is exactly what it is about.
    heard_seconds = 0.0
    last_check = -10.0 ** 9
    last_control = 0.0
    block_seconds = BLOCK / float(SAMPLE_RATE)
    t0 = time.monotonic()
    wake.reset()
    try:
        while player.poll() is None:
            now = time.monotonic()
            # The control channel first: it costs one stat() and it is the one
            # path that cannot mis-hear.
            if now - last_control >= CONTROL_POLL_SECONDS:
                last_control = now
                if read_stop_control(started) is not None:
                    # The caller only distinguishes "be quiet" from "new turn",
                    # so a control stop IS a stop; the journal line keeps the
                    # distinction that matters for debugging.
                    reason = "stop"
                    _stop_player(player)
                    logger.info("%s", _utterance_event(
                        tts="interrupted", reason="control"))
                    break
            data, _ = stream.read(BLOCK)
            samples = data[:, 0] if data.ndim > 1 else data
            level = rms(samples)
            pre_roll += samples.tobytes()
            if len(pre_roll) > pre_roll_max:
                del pre_roll[:len(pre_roll) - pre_roll_max]
            # Her own onset leaks through before the canceller settles. The
            # grace is also the measurement: how loud HER voice is at this
            # mic, published once per utterance so the gate can be tuned from
            # the journal instead of from a guess.
            heard_seconds += block_seconds
            if heard_seconds <= TTS_INTERRUPT_GRACE:
                grace_levels.append(level)
                continue
            if not graced:
                graced = True
                p50 = _percentile(grace_levels, 50)
                p95 = _percentile(grace_levels, 95)
                gate = interrupt_gate(floor, p95)
                logger.info("%s", _utterance_event(
                    tts="selfrms", p50=f"{p50:.0f}", p95=f"{p95:.0f}",
                    gate=f"{gate:.0f}"))
            if level < gate:
                loud = 0.0
                continue
            loud += block_seconds
            if loud < TTS_INTERRUPT_RUN:
                continue
            loud = 0.0
            if heard_seconds - last_check < TTS_INTERRUPT_COOLDOWN:
                continue
            match, heard, ms = interrupt_heard(
                stream, stt, pre=bytes(pre_roll), threshold=gate)
            # The check ate audio of its own; the cooldown runs from its END.
            heard_seconds += TTS_INTERRUPT_CAPTURE
            last_check = heard_seconds
            pre_roll.clear()
            wake.reset()
            logger.info("%s", _utterance_event(
                tts="check", rms=f"{level:.0f}", ms=f"{ms:.0f}",
                heard=_heard(heard), match=match or "none"))
            if match:
                reason = match
                _stop_player(player)
                logger.info("%s", _utterance_event(tts="interrupted",
                                                   reason=reason))
                break
    except Exception as exc:                 # never let listening break speech
        logger.debug("interruptible playback fell back: %s", exc)
        try:
            player.wait(timeout=MAX_UTTERANCE_CONFIRMED)
        except Exception:
            pass
    finally:
        _stop_player(player)
        _unlink(path)
        write_speaking_state(False, started)
    flush_stream(stream)
    wake.reset()
    return reason


def say_reply(tts, text: str, stream=None, wake=None, stt=None) -> str:
    """Speak a reply — interruptibly when we have a mic to listen with."""
    if not (TTS_INTERRUPT and stream is not None
            and wake is not None and stt is not None):
        speak(tts, text)
        return ""
    return speak_interruptible(tts, text, stream, wake, stt)


# ── proactive speech: the room's speaker, driven by the API ────────────────
# The API process CANNOT reach PulseAudio (system unit, ProtectHome=true, no
# session bus), so the server-side alarm scheduler cannot ring anything itself.
# It queues the sentence through puffpai.announcer and this loop — a user unit
# that already owns the mic, Piper and the duck policy — says it. That is what
# makes an alarm ring when the kiosk is showing some other app.
#
# An announcement is an ADDRESSED event by the duck policy's own taxonomy: the
# house is being spoken to on purpose, so the music dips to the addressed level
# for the sentence and is restored on every exit path — exactly what an
# answered question does.
ANNOUNCE_POLL_SECONDS = float(os.environ.get("PUFF_ANNOUNCE_POLL", "2"))


def _newest_announcement_ts(ann) -> str:
    latest = ann.recent(1)
    return str(latest[0].get("ts") or "") if latest else ""


def drain_announcements(tts, *, ann=None, duck=None, speaker=None,
                        stream=None, wake=None, stt=None) -> int:
    """Speak everything the API queued for the room. Returns how many.

    Best-effort: a failure here must never take the wake loop down.

    Spoken INTERRUPTIBLY when the caller hands over the mic: on the deaf
    ``speak()``, the one kind of speech the household never asked for would
    also be the one they could not stop. Alarm RINGING logic is separate: this
    is the sentence, not the siren.
    """
    if ann is None:
        from puffpai import announcer as ann
    duck = _DUCK if duck is None else duck
    if speaker is not None:
        say = speaker
    elif stream is not None and wake is not None and stt is not None:
        def say(tts_, text_):
            say_reply(tts_, text_, stream, wake, stt)
    else:
        say = speak
    try:
        pending = ann.pending_speech()
    except Exception as exc:
        logger.debug("announcement drain failed: %s", exc)
        return 0
    if not pending:
        # Nothing to say, but skipped/stale records must not be reconsidered
        # on every poll — park the cursor at the head of the queue.
        try:
            newest = _newest_announcement_ts(ann)
            if newest and newest != ann.read_cursor():
                ann.write_cursor(newest)
        except Exception as exc:
            logger.debug("announcement cursor park failed: %s", exc)
        return 0

    spoken = 0
    try:
        duck.on_addressed()
        for record in pending:
            text = str(record.get("text") or "").strip()
            if not text:
                continue
            # Outbound speech, so it gets the REPLY-side test (shape AND
            # vocabulary): a reminder can read a code or a password out
            # loud, and nothing marks an announcement private. Silent
            # while a PIN challenge is open, like every other line.
            logger.info("ANNOUNCE (%s): %s", record.get("source", "system"),
                        log_privacy.safe_reply(
                            text, private=vault_challenge_armed()))
            try:
                say(tts, text)
                spoken += 1
            except Exception as exc:
                logger.warning("announcement speech failed: %s", exc)
            else:
                # Playback receipt: the backend flips ``spoken`` so other
                # screens stop re-reading what the room already heard.
                mark = getattr(ann, "mark_spoken", None)
                if callable(mark):
                    try:
                        mark(record.get("ts") or "")
                    except Exception as exc:
                        logger.debug("announcement receipt failed: %s", exc)
            try:
                ann.write_cursor(record.get("ts") or "")
            except Exception as exc:
                logger.debug("announcement cursor write failed: %s", exc)
    finally:
        duck.restore()
    return spoken


def flush_stream(stream) -> None:
    """Discard buffered input (e.g. Puff's own voice tail) before listening."""
    try:
        drain = getattr(stream, "flush", None)
        if callable(drain):
            drain()
            return
        avail = stream.read_available
        while avail > 0:
            stream.read(min(avail, BLOCK))
            avail = stream.read_available
    except Exception:
        pass


# ─────────────────────────────── brain ─────────────────────────────────────
def clean_for_voice(text: str) -> str:
    text = (text or "").strip()
    for ch in ("*", "`", "#", "_"):
        text = text.replace(ch, "")
    return " ".join(text.split())


#: True when the LAST capture stopped because it hit the hard cap, i.e. the
#: sentence was cut off rather than finished. Set by ``record_utterance``,
#: read by ``brain_payload``.
LAST_CAPTURE_TRUNCATED = False


def brain_payload(msgs, truncated=None) -> dict:
    """The body of the POST to ``/api/chat``.

    A capture that ran into the cap sends ``"truncated": true``; the brain
    already honours it (``_try_voice_fragment_guard``) and asks the sentence
    back instead of answering the piece it got.
    """
    cut = LAST_CAPTURE_TRUNCATED if truncated is None else bool(truncated)
    payload = {"messages": list(msgs)}
    if cut:
        payload["truncated"] = True
    return payload


#: Did the last brain reply leave a question open? ``/api/chat`` carries no
#: such flag today (its body is the Ollama shape: model / message / done), so
#: this stays False and ``followup_invited`` falls back to the reply text. It
#: is read from ``pending_clarifier`` if the brain ever starts sending one, so
#: an alarm's "AM or PM?" can hold the window open by contract, not by grammar.
LAST_REPLY_PENDING = False

#: Was the last brain turn PRIVATE -- a spoken PIN, vault contents, a turn
#: the wall panel refused? ``/api/chat`` now says so in its body; a backend
#: that does not is not a reason to write a secret down, so the verdict
#: falls back to the text on both sides (``log_privacy.reply_is_private``).
#: Starts True: nothing has been answered yet, and closed is the safe state.
LAST_REPLY_PRIVATE = True

#: Is the household about to be asked to SAY a PIN? While this holds, an
#: utterance that looks like nothing at all may BE the secret, so every
#: utterance-side log line withholds its text -- including a REJECTED wake,
#: because a PIN said without the wake word landing is still a PIN.
VAULT_CHALLENGE_PENDING = False
VAULT_CHALLENGE_SINCE = 0.0
#: How long a voice-side copy of that state may outlive its last update.
#: The backend's own windows are ``api_server._VAULT_CHAT_TIMEOUT`` (300 s)
#: and ``dashboard_display._PENDING_VAULT_TTL`` (45 s); the larger one is
#: what has to be outlived. Without it, a challenge that lapses with no
#: further brain call would silence the journal for the rest of the day.
VAULT_CHALLENGE_TTL = float(os.environ.get("PUFF_VAULT_CHALLENGE_TTL",
                                           "300"))


def vault_challenge_armed(now: float | None = None) -> bool:
    """Is a vault PIN challenge open right now? Expires on its own."""
    if not VAULT_CHALLENGE_PENDING:
        return False
    now = time.monotonic() if now is None else now
    return (now - VAULT_CHALLENGE_SINCE) <= VAULT_CHALLENGE_TTL


def hide_utterance(*texts) -> bool:
    """Keep this utterance out of the journal?

    Shape, or an armed challenge -- never vocabulary: "unlock the front
    door" and "what's my wifi password" are exactly the lines a missed
    wake is debugged from, and neither carries a secret. The PIN that
    ANSWERS a challenge does, and so does anything PIN-shaped.
    """
    return (vault_challenge_armed()
            or log_privacy.utterance_withholds_any(*texts))


def ask_brain(history: list, text: str) -> str:
    global LAST_REPLY_PENDING, LAST_REPLY_PRIVATE
    global VAULT_CHALLENGE_PENDING, VAULT_CHALLENGE_SINCE
    import requests
    text = strip_lead_fillers(text)          # "the play glass animals" -> "play glass animals"
    # remember the last TWO exchanges, to detect a runaway repeat loop below
    prev_replies = [m["content"] for m in history
                    if m["role"] == "assistant"][-2:]
    prev_asks = [m["content"] for m in history
                 if m["role"] == "user"][-2:]
    history.append({"role": "user", "content": text})
    msgs = history[-(HISTORY_TURNS * 2):]
    try:
        r = requests.post(BRAIN_URL, json=brain_payload(msgs), timeout=120,
                          headers={"X-Puff-Device-Id": BRAIN_SESSION_ID})
        r.raise_for_status()
        data = r.json()
        reply = data.get("message", {}).get("content", "").strip()
        LAST_REPLY_PENDING = bool(data.get("pending_clarifier"))
        LAST_REPLY_PRIVATE = log_privacy.reply_is_private(data, text, reply)
        # Armed or disarmed by every answer; on a brain ERROR the previous
        # value stands (below) until its own timeout, because a challenge
        # we failed to ask about is a challenge that may still be open.
        if log_privacy.challenge_armed(data, reply):
            VAULT_CHALLENGE_PENDING = True
            VAULT_CHALLENGE_SINCE = time.monotonic()
        else:
            VAULT_CHALLENGE_PENDING = False
        if not reply:
            reply = "I'm not sure how to answer that."
    except Exception as exc:
        logger.error("brain error: %s", exc)
        LAST_REPLY_PENDING = False
        # No verdict arrived, so the utterance decides: an unanswered
        # "what's my wifi password" keeps its canned reply out of the log.
        LAST_REPLY_PRIVATE = log_privacy.withholds(text)
        history.pop()  # don't poison memory with an unanswered turn
        return "Sorry, I couldn't reach my brain just now."
    reply = clean_for_voice(reply)
    # A reasoning/tool-trace leak must NEVER enter history: once stored, she reads
    # her own planning note next turn and parrots it forever (the echo loop).
    # Drop the unanswered user turn and speak a clean fallback instead of storing.
    safe = guard_reply(reply)
    if safe != reply:
        history.pop()
        return safe
    # Repeat-breaker: only when the SAME line comes back a 3rd time in a row (the
    # real leak-loop spammed it 5+ times). Two in a row is fine — media controls
    # ("stop", "next", "pause") legitimately repeat their reply.
    # The SAME question asked three times legitimately earns the same answer
    # (a consent prompt, a status read-back), that is not a loop: "pair my
    # bulbs" x3 must get the pairing prompt, not "I caught myself repeating".
    rl = reply.strip().lower()
    tl = text.strip().lower()
    same_ask = (len(prev_asks) >= 2
                and all(tl == a.strip().lower() for a in prev_asks))
    if (len(prev_replies) >= 2 and not same_ask
            and all(rl == p.strip().lower() for p in prev_replies)):
        history.pop()
        logger.warning("loop detected (3rd identical reply) — breaking out")
        return ("I caught myself repeating that. Let's start fresh — "
                "what would you like me to do?")
    history.append({"role": "assistant", "content": reply})
    return reply


def is_stop(text: str) -> bool:
    t = text.strip().lower().rstrip(".!?")
    return t in STOP_PHRASES


#: The stop phrases the BRAIN also understands as "silence the music"
#: (``_MEDIA_BARE_STOP`` in api_server.py). Said after her name they must reach
#: it: if ``is_stop`` swallowed them here, "hey puff pie, stop" would end
#: the exchange and leave the radio playing. Every other stop phrase ("never
#: mind", "go away") still just ends the turn without a word.
MEDIA_STOP_PHRASES = frozenset((
    "stop", "pause", "halt", "silence", "stop now", "stop please",
    "please stop", "just stop",
))


def is_media_stop(text: str) -> bool:
    t = " ".join((text or "").lower().split()).strip(" ,.!?-'\"")
    return t in MEDIA_STOP_PHRASES


# ── "stop the music" with no wake word, and ONLY while music is playing ─────
#: With the radio on, a plain "stop the music." is otherwise rejected as
#: ``wake=rejected reason=no-wakeword``, and the household has to say a name
#: over her own speakers to silence them. Stopping playback is the one command
#: where the CONTEXT does the addressing: nothing else in the room can be meant
#: by "stop the music" while the music is on, and the whole gate evaporates the
#: moment nothing is playing.
MEDIA_STOP_MAX_WORDS = int(os.environ.get("PUFF_MEDIA_STOP_WORDS", "5"))
#: Said toward the speakers, not muttered two rooms away.
MEDIA_STOP_MIN_RMS = float(os.environ.get("PUFF_MEDIA_STOP_RMS", "250"))
MEDIA_STOP_MIN_CONF = float(os.environ.get("PUFF_MEDIA_STOP_CONF", "-0.8"))
_MEDIA_STOP_RE = re.compile(
    r"^(please )?(stop|pause|turn off|shut off|kill) "
    r"(the |that |this )?(music|radio|song|station|it)\.?$")
_MEDIA_BARE_STOP_RE = re.compile(r"^stop( it| now)?\.?$")


def is_bare_media_stop(text: str, conf, level, *, playing: bool) -> bool:
    """Is this stage-2 transcript "silence the speakers", said with no name?"""
    if not playing:
        return False
    if conf is not None and conf < MEDIA_STOP_MIN_CONF:
        return False
    if level is not None and level < MEDIA_STOP_MIN_RMS:
        return False
    low = " ".join((text or "").lower().split())
    if not low or len(low.split()) > MEDIA_STOP_MAX_WORDS:
        return False
    return bool(_MEDIA_STOP_RE.match(low) or _MEDIA_BARE_STOP_RE.match(low))


def playback_active(duck=None) -> bool:
    """Is anything playing through the sink? Never raises — unknown is False."""
    try:
        return bool((_DUCK if duck is None else duck).playback_active())
    except Exception as exc:                  # pragma: no cover - env
        logger.debug("playback probe failed: %s", exc)
        return False


def followup_invited(reply: str, pending=None) -> bool:
    """May a hands-free window open after THIS reply?

    Only if she asked something. An answer is the end of a turn: leaving the
    mic open after one turns room talk like "So, they can't go." into a
    command. ``PUFF_FOLLOWUP_MODE=always`` opens a window after every reply.
    """
    if FOLLOWUP_MODE == "always":
        return True
    if LAST_REPLY_PENDING if pending is None else pending:
        return True
    return (reply or "").strip().endswith("?")


def closes_followup(text: str) -> bool:
    """Is this follow-up "we're done"? Closes the window silently — no brain."""
    t = " ".join((text or "").lower().split()).strip(" ,.!?-'\"")
    return bool(t) and (t in FOLLOWUP_CLOSE_WORDS or is_stop(t))


def followup_accepted(text: str, conf) -> bool:
    """Is this capture an ANSWER, or the room? Empty and uncertain are the room."""
    if not (text or "").strip():
        return False
    return conf is None or conf >= FOLLOWUP_MIN_CONF


# ── "does that sound like a request?" ───────────────────────────────────────
#: Warm-up noise in front of a request. "please" is deliberately NOT stripped —
#: it is an opener in its own right.
FOLLOWUP_LEAD_FILLERS = frozenset(("okay", "ok", "um", "so", "and", "hey"))
#: Two-word openers: "can" alone is a modal in the middle of room talk ("you
#: can illustrate them"), "can you" is somebody asking for something.
FOLLOWUP_OPENER_PAIRS = frozenset((
    ("can", "you"), ("could", "you"), ("would", "you"), ("will", "you"),
    ("look", "up"),
))
#: An imperative verb at the head is a command, whatever follows it.
FOLLOWUP_OPENER_VERBS = frozenset((
    "please", "play", "stop", "pause", "resume", "skip", "next", "turn", "set",
    "add", "remove", "delete", "show", "open", "close", "tell", "read",
    "remind", "call", "find", "search", "give", "start", "cancel", "make",
    "put", "change", "switch", "tune", "mute", "unmute", "louder", "quieter",
    "volume",
))
FOLLOWUP_OPENER_WH = frozenset((
    "what", "what's", "whats", "when", "where", "who", "how", "why",
))
#: Auxiliaries open a question — and also open half the sentences in a room
#: ("Did the bus?"). They count only when a SUBJECT follows, or when the
#: utterance is long enough to be a real question ("are the lights still on").
FOLLOWUP_OPENER_AUX = frozenset(("is", "are", "do", "does", "did"))
FOLLOWUP_AUX_SUBJECTS = frozenset((
    "you", "i", "we", "it", "they", "there", "that", "this", "he", "she",
))
#: The one-word utterances that are still commands. Everything else said in one
#: word ("What?", "Yep.", "Alright.") is the room.
FOLLOWUP_SOLO_COMMANDS = frozenset((
    "stop", "pause", "next", "skip", "resume", "louder", "quieter", "mute",
    "unmute", "cancel",
))
#: An aux-opened utterance this long is a question, not a fragment of room talk.
FOLLOWUP_AUX_MIN_WORDS = 4


def followup_is_request(text: str, conf) -> bool:
    """Does this capture, heard with no wake word, SOUND like a request?

    The whole follow-up policy in one predicate. A follow-up window is open
    after every reply now, so the words have to carry the decision:

    * accepted — "Can you find a different jazz radio?", "Play K exp radio",
      "Add a reminder to do the dish.", "stop the music.";
    * ignored: "So, they can't go.", "I'm ready. I'm ready.", "Yep.",
      "Alright.", "What?", "Did the bus?", "My pace is so much money. You can
      illustrate them. Yeah." (ordinary room talk).

    Uncertain is the room: below ``FOLLOWUP_MIN_CONF`` nothing is a request.
    """
    if conf is not None and conf < FOLLOWUP_MIN_CONF:
        return False
    words = _words(text)
    while len(words) > 1 and words[0] in FOLLOWUP_LEAD_FILLERS:
        words = words[1:]
    if not words:
        return False
    if len(words) == 1:
        return words[0] in FOLLOWUP_SOLO_COMMANDS
    head = words[0]
    if (head, words[1]) in FOLLOWUP_OPENER_PAIRS:
        return True
    if head in FOLLOWUP_OPENER_VERBS or head in FOLLOWUP_OPENER_WH:
        return True
    if head in FOLLOWUP_OPENER_AUX:
        return (words[1] in FOLLOWUP_AUX_SUBJECTS
                or len(words) >= FOLLOWUP_AUX_MIN_WORDS)
    return False


def classify_followup(text: str, conf, *, question: bool = False) -> str:
    """``act`` | ``close`` | ``ignore`` for one capture inside the window.

    ``question`` is the path kept intact: when the reply that opened
    the window ASKED something, she is owed a turn and any confident,
    non-empty answer takes it.
    """
    if not (text or "").strip():
        return "ignore"
    if closes_followup(text):
        return "close"
    if not followup_accepted(text, conf):
        return "ignore"
    if FOLLOWUP_MODE == "always" or question:
        return "act"
    if FOLLOWUP_MODE == "question":
        return "ignore"
    return "act" if followup_is_request(text, conf) else "ignore"


# ── the window itself: open from the END of TTS, and it does not creep ──────
#: Monotonic deadline of the current hands-free window (0.0 = shut), whether
#: the reply that opened it asked a question, and how many wake-LESS turns have
#: chained so far. Module state because a window by definition spans wakes.
_FOLLOWUP_UNTIL = 0.0
_FOLLOWUP_QUESTION = False
_FOLLOWUP_CHAIN = 0


def close_followup_window() -> None:
    global _FOLLOWUP_UNTIL, _FOLLOWUP_QUESTION
    _FOLLOWUP_UNTIL = 0.0
    _FOLLOWUP_QUESTION = False


def reset_followup_chain() -> None:
    """A wake-word turn starts the chain over."""
    global _FOLLOWUP_CHAIN
    _FOLLOWUP_CHAIN = 0


def followup_window_open(now: float | None = None) -> bool:
    return _FOLLOWUP_UNTIL > 0.0 and (
        time.monotonic() if now is None else now) < _FOLLOWUP_UNTIL


def followup_question_pending() -> bool:
    return _FOLLOWUP_QUESTION


def followup_chain() -> int:
    return _FOLLOWUP_CHAIN


def open_followup_window(reply: str = "", *, now: float | None = None,
                         chained: bool = False) -> bool:
    """Open the hands-free window at the END of a spoken reply.

    ``chained`` says this reply answered a WAKE-LESS turn; that is what the
    ``PUFF_FOLLOWUP_MAX_CHAIN`` cap counts, so an exchange she never started
    cannot run on forever.
    """
    global _FOLLOWUP_UNTIL, _FOLLOWUP_QUESTION, _FOLLOWUP_CHAIN
    if FOLLOWUP_MODE not in ("request", "question", "always"):
        close_followup_window()
        return False
    _FOLLOWUP_CHAIN = _FOLLOWUP_CHAIN + 1 if chained else 0
    if _FOLLOWUP_CHAIN >= FOLLOWUP_MAX_CHAIN:
        close_followup_window()
        logger.info("%s", _utterance_event(
            wake="followup", acted="false", reason="chain-cap",
            chain=_FOLLOWUP_CHAIN))
        return False
    question = bool(LAST_REPLY_PENDING) or (reply or "").strip().endswith("?")
    if FOLLOWUP_MODE == "question" and not question:
        close_followup_window()
        return False
    _FOLLOWUP_QUESTION = question
    _FOLLOWUP_UNTIL = (time.monotonic() if now is None else now) + FOLLOWUP_WINDOW
    return True


# Vosk emits filler words ("the", "a", "huh") on silence/noise. Never forward
# those to the brain — they drove the runaway loop and trigger its clarify path.
_FILLERS = {"the", "a", "an", "uh", "um", "huh", "hmm", "mm", "and", "to", "i",
            "you", "it", "yeah", "yes", "no", "oh", "ah", "so", "of", "in", "is",
            "he", "the the", "a a", "and the"}


#: Tokens that make a two- or three-word utterance an actual REQUEST. Split in
#: two because position carries the meaning: "is" opens a question when it
#: LEADS ("is it raining"), and is a copula in the middle of room talk ("here
#: is this"). An action verb anywhere is a command ("play jazz", "stop").
JUNK_GUARD_LEAD_WORDS = frozenset((
    "what", "whats", "when", "where", "who", "how", "is", "are", "can",
    "do", "does", "did", "will", "would", "could", "should", "any",
))
JUNK_GUARD_VERBS = frozenset((
    "play", "stop", "set", "add", "show", "tell", "turn", "open", "close",
    "remind", "call", "give", "read", "pause", "resume", "skip", "start",
    "cancel", "repeat", "time", "weather", "timer", "alarm", "lights",
))
#: Above this many words an utterance is a sentence, and a sentence that
#: reached us after a CONFIRMED wake gets the benefit of the doubt.
JUNK_GUARD_MAX_WORDS = int(os.environ.get("PUFF_JUNK_GUARD_WORDS", "3"))


def is_junk_command(text: str) -> bool:
    """Is this confirmed wake's "command" just room noise?

    A false wake (``conf=-0.60``, ``heard="Here in Puff"``) can hand the brain
    "here is this", which then spends 24 s producing a 16-second spoken
    dictionary answer nobody had asked for. Three words with no request
    in them is not a command, and the cheapest fix is to never send it: chime
    down and go back to listening.

    Deliberately narrow — it only ever looks at utterances of at most three
    words, so the cost of being wrong is one repeat, and "stop", "play jazz"
    and "what time is it" all pass straight through.
    """
    words = _words(text)
    if not words or len(words) > JUNK_GUARD_MAX_WORDS:
        return False
    if words[0] in JUNK_GUARD_LEAD_WORDS:
        return False
    if any(w in JUNK_GUARD_VERBS for w in words):
        return False
    return True


def is_garbage(text: str) -> bool:
    t = text.strip().lower().rstrip(".!?,")
    if len(t) < 2:
        return True
    words = t.split()
    return len(words) <= 2 and all(w in _FILLERS for w in words)


# STT reliably prepends these to real commands ("the play glass animals radio",
# "the war", "the also"). Stripping a leading run of them keeps the actual
# command intact and stops it tipping the brain into its confused clarify path.
# Deliberately excludes content-ish fillers (i/you/it/is/no/yes) — only true
# noise-prefixes, so we never mangle a real query like "is it raining".
_LEAD_NOISE = {"the", "uh", "uhh", "um", "umm", "er", "err", "ah", "hmm", "mm"}


def strip_lead_fillers(text: str) -> str:
    """Drop a leading run of STT noise-words, never emptying the string."""
    words = (text or "").split()
    i = 0
    while i < len(words) - 1 and words[i].lower().strip(".,!?") in _LEAD_NOISE:
        i += 1
    return " ".join(words[i:]) if i else (text or "")


# ── STT spelling repair, voice side only ───────────────────────────────────
#: Observed: "Hey Puff Pie, play KEXP radio" came back as
#: ``command='playk exp radio'`` — the verb glued to the station's first letter
#: and the call sign split in half. Nothing downstream could recognise it, so
#: the junk guard threw the whole thing away. These are STT artefacts, not
#: words, and they are repaired here rather than in the brain: the brain never
#: sees this microphone.
_STT_WORD_FIXES = {
    "playk": "play k", "plaik": "play", "plake": "play",
}
#: Call signs Whisper segments in a way the generic letter-run rule cannot see.
_STT_CALL_LETTER_FIXES = (
    (re.compile(r"\bk\s+exp\b", re.IGNORECASE), "kexp"),
    (re.compile(r"\bkex\s+p\b", re.IGNORECASE), "kexp"),
    (re.compile(r"\bk\s+x\s+p\b", re.IGNORECASE), "kexp"),
)
#: A run of this many bare letters is somebody spelling a call sign out
#: ("w o s u", "k e x p"). "a" and "i" are words, so a run holding either is
#: left alone.
_LETTER_RUN_MIN = 3
_LETTER_RUN_MAX = 4
_NOT_LETTERS = frozenset(("a", "i"))


def _join_letter_runs(words: list[str]) -> list[str]:
    """Glue maximal 3-4 long runs of single letters into one token."""
    out: list[str] = []
    i = 0
    while i < len(words):
        j = i
        while (j < len(words) and len(words[j]) == 1 and words[j].isalpha()
               and words[j].lower() not in _NOT_LETTERS):
            j += 1
        run = j - i
        if _LETTER_RUN_MIN <= run <= _LETTER_RUN_MAX:
            out.append("".join(words[i:j]).lower())
            i = j
        else:
            out.append(words[i])
            i += 1
    return out


def normalize_command(text: str) -> str:
    """Repair the STT artefacts that make a real command unrecognisable.

    Idempotent, and deliberately tiny: a fixed word table, hyphenated spellings
    split back into letters, letter runs re-joined, and three call-sign
    segmentations. It never invents a word that was not already on the line,
    and it leaves the CASE of every word it does not repair alone — "seven PM"
    must not come back as "seven pm".
    """
    if not (text or "").strip():
        return text or ""
    tokens: list[str] = []
    for token in text.split():
        core = token.strip(_EDGE_PUNCT).lower()
        fix = _STT_WORD_FIXES.get(core)
        if fix:
            tokens.extend(fix.split())
            continue
        parts = [p for p in core.split("-") if p]
        if len(parts) > 1 and all(len(p) == 1 and p.isalpha() for p in parts):
            tokens.extend(parts)
            continue
        tokens.append(token)
    joined = " ".join(tokens)
    for pattern, replacement in _STT_CALL_LETTER_FIXES:
        joined = pattern.sub(replacement, joined)
    return " ".join(_join_letter_runs(joined.split()))


# Stopgap: the brain sometimes returns its internal planning notes instead of a
# user-facing reply (reasoning leak). Refuse to speak obvious ones. Real fix is
# brain-side (concierge clean_for_voice).
_LEAK_MARKERS = ("need to ask for clarification", "to proceed with any action",
                 "incomplete and unclear", "user input '", "the user's request",
                 "the user wants me", "planned step", "a step failed", "toolsmith",
                 "allow_egress", "stopped there", "step(s):",
                 # media/clarify family — the echo-loop leaks
                 "the user hasn't", "hasn't specified", "haven't specified",
                 "didn't specify", "the user mentioned", "the user wants to",
                 "the user's question", "the user's input", "need to clarify",
                 "clarify if the user", "clarify which", "clarify the user",
                 "need to get the specific", "ask for the specific",
                 "to proceed with playing", "unclear and might")


def guard_reply(reply: str) -> str:
    low = reply.lower()
    if any(m in low for m in _LEAK_MARKERS):
        logger.warning("suppressed reasoning/tool-trace leak: %s",
                       log_privacy.safe_reply(reply, limit=100,
                                              private=LAST_REPLY_PRIVATE))
        return "Sorry, I couldn't complete that one. Could you try rephrasing?"
    return reply


# The wake model fires on ANY speech, so we decide from the TRANSCRIPTION whether
# we were actually addressed by name. This is what stops her replying to ambient
# room conversation. Whisper transcribes real speech reliably, so keying on the
# name in the text is far more robust than a raw wake-buffer confirm.
_ADDRESS_KEYS = tuple(k for k in os.environ.get(
    "PUFF_ADDRESS_KEYS", "puff,puppy,poof,pup,hip hop,hip-hop").lower().split(",") if k)
# Leftover fragments of "puffPAI" that Whisper tacks on after the "puff" match.
_NAME_TAILS = ("pai", "pie", "py", "by", "fly", "pi", "pay", "pine", "bye")


def parse_address(text: str):
    """Return (addressed, command). Addressed only if her name appears near the
    start of the utterance; command is whatever follows the name."""
    t = text.strip().lower()
    if not t:
        return False, ""
    words = t.split()
    head = " ".join(words[:5])           # only the opening counts as the address
    for key in _ADDRESS_KEYS:
        i = head.find(key)
        if i < 0:
            continue
        rest = (head[i + len(key):] + " " + " ".join(words[5:])).strip(" ,.!?-'\"")
        first, _, tail = rest.partition(" ")
        # Strip punctuation before matching: Whisper writes the address as
        # "Hey puff pie, what time is it?", so the tail arrives as "pie," and an
        # exact match missed it, leaving "pie, what time is it" as the command.
        if first.strip(" ,.!?-'\"") in _NAME_TAILS:   # drop the "...pai" tail
            rest = tail.strip(" ,.!?-'\"")
        return True, rest
    return False, ""


#: Leading noise in front of a command: the greeting Whisper keeps from the
#: address, and the word a speaker warms up with. Stripped, never matched on.
_ADDRESS_FILLERS = frozenset((
    "hey", "hay", "hi", "hello", "ok", "okay", "um", "uh", "er", "yo"))
#: How deep the TOLERANT pass looks for her name. ``parse_address`` counts five
#: words; a filler or two in front ("um, okay, hey puff pie …") pushes the name
#: one slot further right, so the fuzzy pass gets six — still the opening of the
#: utterance, which is what makes it an address rather than a mention.
FUZZY_ADDRESS_HEAD_WORDS = int(os.environ.get("PUFF_FUZZY_ADDRESS_WORDS", "6"))


def _strip_leading_fillers(tokens: list[str]) -> list[str]:
    """Drop "hey/um/okay …" from the front of a command."""
    out = list(tokens)
    while out and _clean_tokens(out[:1])[0] in _ADDRESS_FILLERS:
        out = out[1:]
    return out


def fuzzy_parse_address(text: str):
    """``parse_address`` with the stage-2 name tolerance: (addressed, command).

    Same matcher as ``wake_word_heard``/``_fuzzy_name_heard`` (they share
    ``_fuzzy_name_span``), so any spelling of her name the confirm stage would
    have accepted is also strippable here.
    """
    raw = (text or "").strip().lower().split()
    clean = _clean_tokens(raw)
    span = _fuzzy_name_span(clean)
    if span is None or span[0] >= FUZZY_ADDRESS_HEAD_WORDS:
        return False, ""
    rest = raw[span[1]:]
    # A third spelling of the tail ("puff pie pi, …") is name, not command.
    if rest and _clean_tokens(rest[:1])[0] in _NAME_TAILS + _FUZZY_TAILS:
        rest = rest[1:]
    return True, " ".join(_strip_leading_fillers(rest)).strip(" ,.!?-'\"")


def resolve_address(text: str, confirmed: bool):
    """Address decision for one utterance: (addressed, command, how).

    ``how`` is ``exact`` | ``fuzzy`` | ``trusted`` | ``none``.

    When stage 2 CONFIRMED the wake, her name was already proved to be in this
    very audio, so the address decision must not hang on a second exact match:
    the full-command decode is a longer, unbiased pass that writes the name
    differently ("Pufpie", "puf pie", "pop pie") often enough that a strict
    re-check drops most confirmed wakes as un-addressed. The
    confirm stage IS the gate; this re-check is only a name STRIPPER, so
    a confirmed utterance whose name cannot be located is still hers — we just
    keep the whole text as the command. Without a confirm (PUFF_WAKE_CONFIRM=0,
    or the model fired alone) nothing has proved her name yet and the strict
    parse remains the only way in.
    """
    addressed, command = parse_address(text)
    if addressed:
        return True, command, "exact"
    if not confirmed or not (text or "").strip():
        return False, "", "none"
    addressed, command = fuzzy_parse_address(text)
    if addressed:
        return True, command, "fuzzy"
    return True, " ".join(_strip_leading_fillers(
        (text or "").strip().lower().split())).strip(" ,.!?-'\""), "trusted"


# ── stage-2 wake confirmation (pure helpers) ───────────────────────────────
def wake_word_heard(text: str, keys=None, tails=None) -> bool:
    """Is her name actually in this short transcript?

    The onnx wake model is a SPEECH detector — it fires at ~1.0 on any
    full-scale audio — so "did someone say the wake word" can only be answered
    from words. This is the cheap stage-2 check run on the trigger window
    alone; the full address decision still belongs to ``parse_address``.
    """
    low = (text or "").lower()
    if not low.strip():
        return False
    if any(k and k in low for k in (WAKE_CONFIRM_KEYS if keys is None else keys)):
        return True
    if tail_confirms(low, WAKE_CONFIRM_TAILS if tails is None else tails):
        return True
    return _fuzzy_name_heard(low)


def tail_confirms(low: str, tails) -> bool:
    """Does a bare "…pie."/"…eye." tail count as her name in THIS transcript?

    Only on address-shaped audio: at most ``WAKE_TAIL_MAX_WORDS`` words, or
    opening with the greeting that comes before her name. "Very over to the
    eye." is neither — it is a sentence from the room that happens to end in a
    homophone, and must not buy a spoken answer.
    """
    if not any(t in low for t in tails):
        return False
    words = _words(low)
    if not words:
        return False
    return len(words) <= WAKE_TAIL_MAX_WORDS or words[0] in WAKE_TAIL_HEADS


#: A "puff"-shaped token: optional greeting glued on, then p + one vowel + one
#: or two f's + at most two trailing letters ("puff", "pof", "poffy", "paff").
_FUZZY_HEAD = re.compile(r"^(?:hey|hay|a|hi)?[,\s]*p[uoa]f{1,2}[a-z]{0,2}$")
#: The "pie" half, as Whisper variously writes it.
_FUZZY_TAILS = ("pie", "pi", "pye", "py", "eye", "high", "hi",
                "bye", "by", "pa", "pay")
#: The same two halves glued into one word — "puffpie", "hey puffpi".
_FUZZY_GLUED = re.compile(
    r"^(?:hey|hay|a|hi)?[,\s]*p[uoa]f{1,2}(?:%s)$" % "|".join(_FUZZY_TAILS))


def _fuzzy_name_span(tokens: list[str]) -> tuple[int, int] | None:
    """Index range ``[start, end)`` of a clipped "puff … pie" in these tokens.

    The single place the fuzzy name rule lives: ``_fuzzy_name_heard`` asks
    whether it matched, the fuzzy address parse asks WHERE, so the tolerance
    that stage 2 accepted and the tolerance that strips the name are by
    construction the same. Tokens must already be punctuation-stripped and
    lower-cased (``_words``); empties are skipped so a position-preserving
    ``_clean_tokens`` list can be passed straight in.

    The rule stays tight on purpose: BOTH halves must be present and the tail
    must follow the head within two tokens, so "apple pie and ice cream" (no
    head) and "puffy jacket" (no tail) are still rejected.
    """
    for i, token in enumerate(tokens):
        if not token:
            continue
        if _FUZZY_GLUED.match(token):
            return i, i + 1
        if not _FUZZY_HEAD.match(token):
            continue
        for j in range(i + 1, min(i + 3, len(tokens))):
            if tokens[j] in _FUZZY_TAILS:
                return i, j + 1
    return None


def _fuzzy_name_heard(low: str) -> bool:
    """Was a clipped "puff … pie" spoken here?

    Whisper writes her name a dozen ways over music ("hey puffpie", "Hey, puff
    pi"), and an exact key list can never hold them all.
    """
    return _fuzzy_name_span(_words(low)) is not None


#: Monotonic timestamp of the last stage-2 reject (0.0 = none yet) and whether
#: that reject was wake-SHAPED (short) / carried a name fragment. Module state
#: because the retry signal spans wake triggers by definition.
_LAST_WAKE_REJECT = 0.0
_LAST_WAKE_REJECT_SHORT = False
_LAST_WAKE_REJECT_FRAGMENT = False


def wake_retry_pressure(heard: str = "", now: float | None = None,
                        window: float | None = None) -> bool:
    """Is this stage-2 reject the SECOND *wake-shaped* one in quick succession?

    Two rejects inside ``PUFF_WAKE_RETRY_WINDOW`` mean she may be standing there
    repeating herself while we keep saying no. The streak alone is not
    evidence: singing fires the wake model every few seconds, so a chorus is a
    permanent streak and the music would dip through the whole song. So the
    streak has to LOOK like someone trying:
    BOTH rejects short (``PUFF_WAKE_SHAPED_MAX_WORDS`` words or fewer, and an
    empty pass counts as short) and at least one of the two carrying a fragment
    of her name. Records this reject either way — the streak must not depend on
    which of the pair the fragment landed in.
    """
    global _LAST_WAKE_REJECT, _LAST_WAKE_REJECT_SHORT, _LAST_WAKE_REJECT_FRAGMENT
    now = time.monotonic() if now is None else now
    span = WAKE_RETRY_WINDOW if window is None else window
    short = len(_words(heard)) <= WAKE_SHAPED_MAX_WORDS
    fragment = _name_fragment_heard(heard)
    in_window = _LAST_WAKE_REJECT > 0.0 and (now - _LAST_WAKE_REJECT) <= span
    retry = (in_window and short and _LAST_WAKE_REJECT_SHORT
             and (fragment or _LAST_WAKE_REJECT_FRAGMENT))
    _LAST_WAKE_REJECT = now
    _LAST_WAKE_REJECT_SHORT = short
    _LAST_WAKE_REJECT_FRAGMENT = fragment
    return retry


def clear_wake_retry() -> None:
    """Forget the reject streak — called when a wake finally confirms."""
    global _LAST_WAKE_REJECT, _LAST_WAKE_REJECT_SHORT, _LAST_WAKE_REJECT_FRAGMENT
    _LAST_WAKE_REJECT = 0.0
    _LAST_WAKE_REJECT_SHORT = False
    _LAST_WAKE_REJECT_FRAGMENT = False


def max_utterance_for(confirmed: bool) -> float:
    """Hard cap for a capture, by whether the wake word was actually heard."""
    return MAX_UTTERANCE_CONFIRMED if confirmed else MAX_UTTERANCE_UNCONFIRMED


def calibrated_threshold(floor: float, min_threshold: float | None = None,
                         margin: float | None = None) -> float:
    """VAD threshold for a capture that starts in ``floor`` of ambient noise."""
    base = VAD_THRESHOLD if min_threshold is None else min_threshold
    return max(base, floor + (VAD_MARGIN if margin is None else margin))


def ambient_floor(pcm: bytes, seconds: float | None = None) -> float | None:
    """Loudest QUIET block in the oldest ``seconds`` of ``pcm``, or None.

    Calibrating from audio recorded BEFORE the trigger is the only honest
    sample of the room. In-line calibration (``record_utterance``) samples the
    room the capture STARTS in, which after a wake confirm is the middle of the
    speaker's own sentence: over the radio that yields e.g. ``floor=161
    threshold=241`` and throws the word "play" away as ambient. The head of
    the wake ring buffer predates the name, so it is what the room actually
    sounds like. Blocks at or above ``VAD_CALIBRATE_MAX`` are speech and are
    excluded; if every block is speech there is no honest floor and None says so.
    """
    span = VAD_CALIBRATE_SECONDS if seconds is None else seconds
    samples = np.frombuffer(pcm, dtype=np.int16)[:int(span * SAMPLE_RATE)]
    quiet = [e for e in (rms(samples[i:i + BLOCK])
                         for i in range(0, len(samples) - BLOCK + 1, BLOCK))
             if e < VAD_CALIBRATE_MAX]
    return max(quiet) if quiet else None


def has_speech(pcm: bytes, threshold: float) -> bool:
    """True when any block of ``pcm`` is at or above ``threshold``."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    if 0 < len(samples) < BLOCK:
        return rms(samples) >= threshold
    return any(rms(samples[i:i + BLOCK]) >= threshold
               for i in range(0, len(samples) - BLOCK + 1, BLOCK))


class BargeIn(Exception):
    """A CONFIRMED wake word was heard while a capture was running."""

    def __init__(self, pre: bytes = b""):
        super().__init__("wake word during capture")
        self.pre = pre          # the 1 s trigger window that carried the name


class Capture:
    """Result of one capture attempt.

    ``pre`` is a replacement pre-roll: after a barge-in the audio holding the
    name is the NEW trigger window, not the one the caller started with.
    """

    __slots__ = ("pcm", "pre", "barged")

    def __init__(self, pcm, pre=None, barged=False):
        self.pcm, self.pre, self.barged = pcm, pre, barged


# ─────────────────────────────── record + STT ──────────────────────────────
def record_utterance(stream, first: bool, wait_override: float = 0.0, *,
                     max_utterance: float | None = None,
                     wake=None, confirm=None, resume: bool = False,
                     threshold_override: float | None = None) -> bytes | None:
    """Capture one utterance via energy VAD. Returns PCM bytes or None on timeout.

    ``wait_override`` replaces the wait-for-speech window for this call only —
    used by the fragment guard to re-open the mic for a short, bounded moment
    after a cut sentence, without touching START_TIMEOUT or FOLLOWUP_WINDOW.

    ``resume`` means the utterance is ALREADY IN PROGRESS in audio the caller
    holds (the wake-confirm bridge): there is nothing to wait for and nothing
    to calibrate, so the capture starts in the speaking state and simply runs
    to the silence hang. ``threshold_override`` supplies the VAD threshold in
    that case, calibrated from audio that predates the trigger.

    ``max_utterance`` overrides the hard cap (see ``max_utterance_for``).
    ``wake``/``confirm`` enable barge-in: the wake model keeps running on the
    live stream, and a wake whose trigger window ``confirm(pcm)`` accepts
    raises ``BargeIn`` so the caller can discard this capture and start over.
    """
    global LAST_CAPTURE_TRUNCATED
    LAST_CAPTURE_TRUNCATED = False
    frames = bytearray()
    started = resume
    silence = 0.0
    t0 = time.time()
    cap = MAX_UTTERANCE if max_utterance is None else max_utterance
    wait = wait_override if wait_override > 0 else (
        START_TIMEOUT if first else FOLLOWUP_WINDOW)
    threshold = VAD_THRESHOLD if threshold_override is None else threshold_override
    # Not on an override window: the fragment guard's 1.5 s re-open is too
    # short to spend 0.3 s of it calibrating, and its behaviour stays as-is.
    # Not on a resume either: the words it would calibrate over are hers.
    calibrating = VAD_CALIBRATE and wait_override <= 0 and not resume
    ambient: list[float] = []
    last_barge = 0.0
    while True:
        data, _ = stream.read(BLOCK)
        samples = data[:, 0] if data.ndim > 1 else data
        e = rms(samples)
        if calibrating:
            # Sample the room we are actually starting in. Speech ends the
            # calibration immediately (that block is the start of the
            # utterance), so this never eats a word.
            if e < VAD_CALIBRATE_MAX and len(ambient) * BLOCK / SAMPLE_RATE < VAD_CALIBRATE_SECONDS:
                ambient.append(e)
                continue
            calibrating = False
            # The LOUDEST ambient block, not the mean: the VAD has to sit above
            # the peaks of ducked music, or the hang never elapses.
            floor = max(ambient) if ambient else 0.0
            threshold = calibrated_threshold(floor)
            logger.info("%s", _utterance_event(
                vad="calibrated", floor=f"{floor:.0f}", threshold=f"{threshold:.0f}"))
        if started and wake is not None and confirm is not None:
            score = wake.push(samples)
            now = time.time()
            if (score >= WAKE_THRESHOLD and now - t0 >= BARGE_IN_AFTER
                    and now - last_barge >= BARGE_IN_COOLDOWN):
                last_barge = now
                trigger = wake.buffer_bytes()
                if confirm(trigger):
                    raise BargeIn(trigger)
        if not started:
            if e >= threshold:
                started = True
                frames += samples.tobytes()
            elif time.time() - t0 > wait:
                return None
        else:
            frames += samples.tobytes()
            if e < threshold:
                silence += BLOCK / SAMPLE_RATE
            else:
                silence = 0.0
            if silence >= SILENCE_HANG:
                break
            if len(frames) / 2 / SAMPLE_RATE >= cap:
                logger.info("max utterance length reached (cap=%.0fs)", cap)
                # The sentence did not END, it was CUT. The brain is told,
                # so it asks for the sentence again instead of answering the
                # fragment it was handed.
                LAST_CAPTURE_TRUNCATED = True
                break
    return bytes(frames)


def capture_fixed(stream, seconds: float) -> bytes:
    """Read exactly ``seconds`` of audio — no VAD, no gating."""
    frames = bytearray()
    need = int(seconds * SAMPLE_RATE)
    while len(frames) // 2 < need:
        data, _ = stream.read(BLOCK)
        samples = data[:, 0] if data.ndim > 1 else data
        frames += samples.tobytes()
    return bytes(frames)


def confirm_transcribe(stt, pcm: bytes, bias: str = "") -> str:
    """One stage-2 transcription, biased toward her name only when asked."""
    if bias and getattr(stt, "supports_bias", False):
        return stt.transcribe(pcm, bias=bias).strip()
    return stt.transcribe(pcm).strip()


def hotwords_pass_allowed(text: str) -> bool:
    """Is an unbiased pass 1 worth a second, BIASED look?

    Only when it heard something short and name-less: an empty pass is a room
    with no speech in it (biasing that is how "Puff" gets written onto a fan),
    and a sentence is the family talking.
    """
    words = _words(text)
    return (bool(words) and len(words) <= WAKE_CONFIRM_PASS1_MAX_WORDS
            and not wake_word_heard(text))


def confirm_decision(text1: str, conf1, text2=None, conf2=None):
    """Was her name REALLY said? ``(ok, text, which_pass, reason, conf)``.

    Pass 1 is unbiased, so hearing her name there is evidence on its own — it
    only has to clear the hard floor. Pass 2 had the answer whispered to it,
    so it must clear the higher ``WAKE_CONFIRM_MIN_CONF`` *and* stay short: a
    biased decoder handed room noise writes whole sentences around the name.
    """
    if wake_word_heard(text1):
        if conf1 is not None and conf1 < WAKE_CONFIRM_FLOOR:
            return False, text1, 1, "low-confidence", conf1
        return True, text1, 1, "wakeword", conf1
    if text2 is None:
        return False, text1, 1, "no-wakeword", conf1
    heard = text2 or text1
    if not wake_word_heard(text2):
        return False, heard, 2, "no-wakeword", conf2
    if len(_words(text2)) > WAKE_CONFIRM_PASS2_MAX_WORDS:
        return False, heard, 2, "hotword-run-on", conf2
    if conf2 is None or conf2 < max(WAKE_CONFIRM_MIN_CONF, WAKE_CONFIRM_FLOOR):
        return False, heard, 2, "low-confidence", conf2
    return True, text2, 2, "wakeword", conf2


def confirm_wake(stream, stt, pre: bytes):
    """Stage 2: transcribe the trigger window + a short tail and require the name.

    Returns ``(confirmed, extra_pcm, text, stt_ms, bridge_pcm, which_pass,
    reason, conf)`` — see ``confirm_decision`` for the two passes.

    The transcription runs on a worker thread while THIS thread keeps pulling
    frames off the mic into ``bridge``, so the ~1.1 s the confirm costs is
    recorded rather than merely buffered. Left in the capture queue it would
    be eaten by the next capture's calibration and wait-for-speech ("hey puff
    pie, play some music" would reach the brain as "some music"). The caller prepends
    ``extra + bridge`` to the command audio - the confirmation costs a
    transcription, never a word of the command.
    """
    extra = capture_fixed(stream, WAKE_CONFIRM_WINDOW)
    t0 = time.perf_counter()
    done: dict = {}

    def _transcribe() -> None:
        try:
            pcm = normalize_utterance(pre + extra)
            text1 = confirm_transcribe(stt, pcm)
            conf1 = getattr(stt, "last_avg_logprob", None)
            text2 = conf2 = None
            if (WAKE_CONFIRM_HOTWORDS and getattr(stt, "supports_bias", False)
                    and hotwords_pass_allowed(text1)):
                text2 = confirm_transcribe(stt, pcm, bias=WAKE_CONFIRM_HOTWORDS)
                conf2 = getattr(stt, "last_avg_logprob", None)
            done["decision"] = confirm_decision(text1, conf1, text2, conf2)
        except BaseException as exc:            # re-raised on this thread below
            done["exc"] = exc

    worker = threading.Thread(target=_transcribe, name="puff-confirm", daemon=True)
    worker.start()
    bridge = bytearray()
    while worker.is_alive() and len(bridge) / 2 / SAMPLE_RATE < WAKE_BRIDGE_MAX:
        data, _ = stream.read(BLOCK)
        samples = data[:, 0] if data.ndim > 1 else data
        bridge += samples.tobytes()
    worker.join()
    if "exc" in done:
        raise done["exc"]
    ok, text, which, reason, conf = done.get(
        "decision", (False, "", 1, "no-wakeword", None))
    return (ok, extra, text, (time.perf_counter() - t0) * 1000.0, bytes(bridge),
            which, reason, conf)


def record_capture(stream, stt, wake, *, first: bool, confirmed: bool,
                   wait_override: float = 0.0, resume: bool = False,
                   threshold_override: float | None = None) -> Capture:
    """Record one utterance, restarting it if a confirmed wake barges in.

    This is what makes "hey puff pie, stop" win over a recording already in
    flight instead of queueing behind it for the rest of the cap.
    """
    use_wake = wake if (BARGE_IN and wake is not None and stt is not None) else None

    def _confirm(pcm: bytes) -> bool:
        try:
            return wake_word_heard(stt.transcribe(normalize_utterance(pcm)))
        except Exception as exc:      # a confirm failure must not kill a capture
            logger.debug("barge-in confirm failed: %s", exc)
            return False

    restarts = 0
    pre = None
    while True:
        try:
            pcm = record_utterance(stream, first=first, wait_override=wait_override,
                                   max_utterance=max_utterance_for(confirmed),
                                   wake=use_wake,
                                   confirm=_confirm if use_wake is not None else None,
                                   resume=resume,
                                   threshold_override=threshold_override)
            return Capture(pcm, pre, restarts > 0)
        except BargeIn as barge:
            restarts += 1
            logger.info("%s", _utterance_event(wake="barge-in", restart=restarts))
            wake.reset()
            if restarts > BARGE_IN_MAX_RESTARTS:
                return Capture(None, barge.pre, True)
            # A barge-in is a NEW utterance in a room we have not sampled:
            # back to a normal first capture, calibration and all.
            pre, confirmed, first, wait_override = barge.pre, True, True, 0.0
            resume, threshold_override = False, None
            play_ready()          # "I dropped that — talk now"


def extend_if_fragment(stream, stt, command: str) -> str:
    """Give a cut-off sentence one more bounded chance to finish.

    SILENCE_HANG (1.0 s) can close the utterance at a natural pause after a
    fronted time phrase, so "every week." and "every Tuesday." would reach
    the brain as whole turns and each get an encyclopedia answer. The rule for
    what counts as a fragment lives in ``puffpai.fragments`` and is shared with
    the brain-side guard, so the two can never drift apart.

    Exactly ONE extension per utterance, capped at FRAGMENT_EXTEND seconds of
    waiting for speech to start. If nothing more is said, the original words go
    through unchanged and the brain-side guard asks for the sentence back.
    """
    if FRAGMENT_EXTEND <= 0 or not fragments.is_fragment(command):
        return command
    # The words are NOT logged here: an unaddressed extension window records
    # whatever the room says next, which is what the address gate exists to
    # keep off disk.
    logger.info("fragment guard: re-opening mic for %.1fs (len=%d)",
                FRAGMENT_EXTEND, len(command or ""))
    pcm = record_utterance(stream, first=False, wait_override=FRAGMENT_EXTEND)
    if not pcm:
        return command
    try:
        tail = stt.transcribe(normalize_utterance(pcm)).strip()
    except Exception:
        return command
    if not tail:
        return command
    return fragments.join_extension(command, tail)


def converse(stream, stt, tts, history: list, initial_command: str = "",
             *, wake=None, confirmed: bool = True, address: str = "",
             followup: bool = False) -> None:
    """Handle an exchange after she's been addressed by name.

    If the name and command arrived in one breath ("hey puffpai, set a timer"),
    ``initial_command`` holds the command and we answer straight away. If only
    the name was said, we chime and listen for the command.

    ``address`` is how the gate decided this turn was hers (``exact`` |
    ``fuzzy`` | ``trusted`` | ``followup`` | ``media-stop``). It is what the
    junk guard keys on: with ``exact`` her name is provably IN the transcript,
    and a command that survived that is never thrown away (otherwise "playk
    exp radio" after an exact address would be dropped as junk).

    ``followup`` says this turn came in with no wake word, so its reply chains
    against ``PUFF_FOLLOWUP_MAX_CHAIN``.

    Every spoken reply ends by OPENING a hands-free window
    (``open_followup_window``); ``PUFF_FOLLOWUP_MODE`` decides what may be
    acted on inside it. The in-``converse`` window below is the older,
    single-exchange path and is unchanged.

    Her replies are spoken INTERRUPTIBLY: "stop" over the top of a long answer
    kills playback within a block, and her name mid-answer starts a fresh turn.
    """
    play_ready()  # rising chime: "got it — you're talking to me"
    try:
        command = initial_command.strip()
        followups = 0
        first_turn = True
        while True:
            if not command:
                # Only the wake-confirmed first turn gets the long cap; a
                # hands-free follow-up window is unconfirmed by construction.
                cap = record_capture(stream, stt, wake, first=first_turn,
                                     confirmed=confirmed and first_turn)
                if cap.pcm is None:
                    logger.info("addressed, but no command followed" if first_turn
                                else "follow-up window closed (no speech)")
                    return
                command = stt.transcribe(
                    normalize_utterance((cap.pre or b"") + cap.pcm)).strip()
                if cap.barged:
                    # A barge-in capture opens with her name — strip it so
                    # "hey puff pie, stop" reaches is_stop() as "stop".
                    addressed, after_name = parse_address(command)
                    if addressed:
                        command = after_name
                # A sentence cut at a pause gets one bounded second chance
                # BEFORE it is logged or sent anywhere.
                command = extend_if_fragment(stream, stt, command)
            if first_turn or LOG_TRANSCRIPTS:
                # First turn only: the user said her name, so this utterance is
                # addressed to her and logging it is within the contract.
                logger.info("COMMAND: %r", log_privacy.safe_heard(
                    command, hide=hide_utterance(command)))
            else:
                # A hands-free follow-up (PUFF_FOLLOWUP=1) is captured with NO
                # address check. It is nevertheless audio the owner opened by
                # addressing Puff seconds earlier — not ambient room speech —
                # and it is ACTED ON, so a bounded excerpt is logged: without
                # it, a follow-up logged as ``addressed=false len=34`` and
                # obeyed as "stop the music" leaves nothing on disk to say
                # what it actually was. Bounded by HEARD_MAX, same
                # ceiling as a stage-2 reject.
                accepted = followup_accepted(command, getattr(
                    stt, "last_avg_logprob", None))
                logger.info("%s", _utterance_event(
                    wake="followup", addressed="false",
                    len=len(command), conf=_conf_of(stt),
                    acted="true" if accepted else "false",
                    heard=_heard(command)))
                # An uncertain or empty capture inside the window is the room
                # talking past her, not the answer she asked for.
                if not accepted or closes_followup(command):
                    return
            # STT artefacts are repaired BEFORE anything judges the command:
            # "playk exp radio" is "play kexp radio" and always was.
            command = normalize_command(command)
            if not command or is_garbage(command):
                return
            if (first_turn and confirmed and address != "exact"
                    and is_junk_command(command)):
                # A hallucinated wake with room noise behind it. The brain is
                # never called, so it cannot answer a question nobody asked.
                # NOT applied to an exact address: her name is provably in that
                # transcript, so whatever follows it is a command she meant.
                logger.info("%s", _utterance_event(
                    wake="dropped", reason="junk", address=address or "none",
                    heard=_heard(command)))
                return          # the finally below chimes down
            if is_stop(command) and not (first_turn and is_media_stop(command)):
                close_followup_window()
                return          # "never mind"/"thanks" just ends the exchange
            reply = guard_reply(ask_brain(history, command))
            logger.info("PUFF: %s",
                        log_privacy.safe_reply(reply,
                                               private=LAST_REPLY_PRIVATE))
            interrupted = say_reply(tts, reply, stream, wake, stt)
            if interrupted == "stop":
                close_followup_window()
                return          # told to be quiet: say nothing, go back to sleep
            # END OF TTS: the hands-free window starts here, not at the start
            # of the sentence — a long answer must not eat its own window.
            open_followup_window(reply, chained=followup or not first_turn)
            if interrupted == "name":
                # Re-addressed mid-sentence: this is a new turn, chime and all.
                command = ""
                first_turn = True
                followups = 0
                followup = False        # her name started this turn
                reset_followup_chain()
                play_ready()
                continue

            # ── hands-free follow-up window (no wake word needed) ──
            if (not FOLLOWUP or followups >= MAX_FOLLOWUPS
                    or not followup_invited(reply)):
                return
            followups += 1
            first_turn = False
            command = ""            # force a fresh listen on the next iteration
            flush_stream(stream)    # drop the tail of Puff's own reply
            play_ready()            # chime: the follow-up window is open
    finally:
        play_sleep()  # falling tone: "done, back to sleep"


# ─────────────────────────────── main loop ─────────────────────────────────
class WhisperSTT:
    """faster-whisper STT — dramatically better than Vosk on natural speech.

    Takes the same PCM-bytes interface as VoskSTT. vad_filter drops non-speech
    so trailing silence doesn't hallucinate; condition_on_previous_text=False
    keeps each utterance independent.
    """

    #: Mean ``avg_logprob`` over the last transcription's segments, or None.
    #: Exposed so an un-addressed utterance can be logged with a confidence
    #: number instead of its text (see ``_log_unaddressed``).
    last_avg_logprob: float | None = None

    #: This engine can be biased toward her name (see ``transcribe(bias=...)``).
    supports_bias: bool = True

    def __init__(self):
        from faster_whisper import WhisperModel
        self._m = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE,
                               compute_type=WHISPER_COMPUTE)
        # ``hotwords`` (faster-whisper >= 1.0.2) biases the decoder toward a
        # phrase without putting it in the output; older builds only take an
        # ``initial_prompt``, which is weaker but the same idea.
        try:
            import inspect
            params = inspect.signature(self._m.transcribe).parameters
            self._bias_kw = "hotwords" if "hotwords" in params else "initial_prompt"
        except Exception:                      # never let introspection break STT
            self._bias_kw = "initial_prompt"

    def transcribe(self, pcm: bytes, bias: str = "") -> str:
        """Transcribe PCM. ``bias`` nudges the decoder toward a known phrase.

        Only the stage-2 wake confirm passes a bias: biasing the COMMAND pass
        would sprinkle her name through ordinary sentences.
        """
        self.last_avg_logprob = None
        if not pcm:
            return ""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kw = {}
        if bias:
            kw[self._bias_kw] = bias if self._bias_kw == "hotwords" else bias + "."
        segments, _ = self._m.transcribe(
            audio, language="en", beam_size=1, vad_filter=True,
            condition_on_previous_text=False, **kw,
        )
        # segments is a generator; consume once, keeping text and confidence.
        parts, probs = [], []
        for seg in segments:
            parts.append(seg.text)
            prob = getattr(seg, "avg_logprob", None)
            if prob is not None:
                probs.append(prob)
        if probs:
            self.last_avg_logprob = sum(probs) / len(probs)
        return " ".join(parts).strip()

    def is_available(self) -> bool:
        return True


def _load_engines():
    from puffpai.voice.tts import PiperTTS
    tts = PiperTTS(model_path=PIPER_MODEL)
    stt = None
    if STT_ENGINE == "whisper":
        try:
            stt = WhisperSTT()
            logger.info("STT engine: faster-whisper (%s, %s)", WHISPER_MODEL, WHISPER_COMPUTE)
        except Exception as exc:
            logger.error("Whisper init failed (%s); falling back to Vosk", exc)
    if stt is None:
        from puffpai.voice.stt import VoskSTT
        stt = VoskSTT(model_path=VOSK_MODEL)
        logger.info("STT engine: Vosk (%s)", VOSK_MODEL)
    if not stt.is_available():
        logger.error("STT engine unavailable")
    if not tts.is_available():
        logger.error("Piper TTS unavailable (model: %s)", PIPER_MODEL)
    return stt, tts


def handle_wake(stream, stt, tts, wake, history: list) -> bool:
    """Handle one wake trigger. Returns True if we were actually addressed.

    Split out of ``main()`` so the ducking policy is testable without a mic.
    """
    # Normally does NOTHING audible: the wake model fires on any speech, so
    # ducking here dipped the music every time anyone in the room spoke or sang
    # along. It ducks only if PUFF_DUCK_ON_LISTEN=1 or a peek is armed (a
    # previous pass looked like a wake we missed over playback). Restored in the
    # finally on EVERY exit path.
    _DUCK.on_wake()
    try:
        # The onnx model fires on ANY speech, so it only means "someone
        # spoke". Grab the 1 s that triggered (holds the name if it was
        # spoken) + the continuation, then decide from the transcription
        # whether we were actually addressed — this is what keeps her from
        # answering ambient room conversation.
        pre = wake.buffer_bytes()
        wake.reset()
        # Stage 2, cheap: before committing to a full capture, transcribe ONLY
        # the trigger window (+ a short tail) and require her name in it. Room
        # conversation ends here — no chime, no 12 s recording of the family,
        # no Whisper pass over it, and no full capture blocking a real "hey
        # puff pie, stop" behind it.
        confirmed = False
        resume = False
        threshold_override = None
        followup = False
        if WAKE_CONFIRM:
            (ok, extra, ctext, stt_ms, bridge,
             which, reason, cconf) = confirm_wake(stream, stt, pre)
            level = rms(np.frombuffer(pre + extra, dtype=np.int16))
            if not ok:
                # ── two ways in WITHOUT the wake word ──
                # 1. Silencing playback. Nothing else in the room can be meant
                #    by "stop the music" while the music is on, and the gate
                #    does not exist when nothing is playing.
                if is_bare_media_stop(ctext, cconf, level,
                                      playing=playback_active()):
                    logger.info("%s", _utterance_event(
                        wake="confirmed", reason="media-stop",
                        conf=_fmt_conf(cconf), rms=f"{level:.0f}",
                        heard=_heard(ctext)))
                    clear_wake_retry()
                    close_followup_window()
                    _DUCK.on_addressed()
                    converse(stream, stt, tts, history,
                             initial_command=normalize_command(ctext),
                             wake=wake, confirmed=True, address="media-stop")
                    flush_stream(stream)
                    wake.reset()
                    return True
                # 2. A REQUEST inside the window her last reply opened. The
                #    full utterance is still captured below, bridge and all —
                #    this stage-2 text is only the decision, never the command.
                verdict = classify_followup(
                    ctext, cconf, question=followup_question_pending()
                ) if followup_window_open() else "ignore"
                if verdict == "close":
                    close_followup_window()
                    logger.info("%s", _utterance_event(
                        wake="followup", acted="false", reason="closed",
                        conf=_fmt_conf(cconf), heard=_heard(ctext)))
                    flush_stream(stream)
                    wake.reset()
                    return False
                if verdict != "act":
                    if followup_window_open():
                        # Ignored, and the window is NOT extended or re-opened:
                        # room talk must not keep its own door propped.
                        logger.info("%s", _utterance_event(
                            wake="followup", acted="false",
                            reason="not-a-request", conf=_fmt_conf(cconf),
                            heard=_heard(ctext)))
                    else:
                        # WITH the transcript, bounded: a reject that says
                        # nothing is unfalsifiable, and cannot tell "puppy"
                        # from "buffet" from a dead mic (hence rms too).
                        logger.info("%s", _utterance_event(
                            wake="rejected", **{"pass": which},
                            conf=_fmt_conf(cconf), ms=f"{stt_ms:.0f}",
                            rms=f"{level:.0f}", reason=reason,
                            heard=_heard(ctext)))
                    # A second WAKE-SHAPED reject inside the retry window is
                    # evidence that she is trying and we keep missing her — arm
                    # the peek so her next attempt is heard over ducked
                    # playback. A streak of sung notes is not.
                    retry = wake_retry_pressure(ctext)
                    missed = _looks_like_missed_wake(stt, ctext)
                    # This reject also disproves any peek duck taken a moment
                    # ago, so on_unaddressed lets the music straight back up
                    # rather than holding it through the rest of the pass.
                    _DUCK.on_unaddressed(missed or retry,
                                         reason="missed" if missed else "retry")
                    flush_stream(stream)
                    wake.reset()
                    return False
                followup = True
                logger.info("%s", _utterance_event(
                    wake="followup", acted="true", conf=_fmt_conf(cconf),
                    rms=f"{level:.0f}", chain=followup_chain() + 1,
                    heard=_heard(ctext)))
            clear_wake_retry()                # a confirm ends the retry streak
            # The room as it sounded BEFORE the name — the only part of this
            # audio that is not her. If the bridge already carries speech the
            # command is in flight, so the capture resumes it rather than
            # waiting for an onset that has already happened and calibrating
            # over her own words.
            floor = ambient_floor(pre)
            if floor is not None:
                threshold_override = calibrated_threshold(floor)
                resume = has_speech(bridge, threshold_override)
                if not resume:
                    threshold_override = None
            bridge_ms = len(bridge) / 2 / SAMPLE_RATE * 1000.0
            if not followup:
                reset_followup_chain()   # her name starts the chain over
                logger.info("%s", _utterance_event(
                    wake="confirmed", **{"pass": which}, conf=_fmt_conf(cconf),
                    ms=f"{stt_ms:.0f}",
                    rms=f"{level:.0f}", bridge_ms=f"{bridge_ms:.0f}",
                    resume="true" if resume else "false", heard=_heard(ctext)))
            pre += extra + bridge
            confirmed = True
        cap = record_capture(stream, stt, wake, first=True, confirmed=confirmed,
                             resume=resume, threshold_override=threshold_override)
        if cap.pre is not None:
            pre = cap.pre        # a barge-in replaced the audio holding the name
        full = pre + (cap.pcm or b"")
        text = stt.transcribe(normalize_utterance(full)).strip()
        if followup:
            # No name was said and none is expected: the whole utterance is the
            # command. The capture above already ran the wake path's bridge and
            # resume logic, so "…dishes tonight before 11" arrives whole.
            addressed, how = True, "followup"
            command = " ".join(_strip_leading_fillers(
                text.lower().split())).strip(" ,.!?-'\"")
        else:
            addressed, command, how = resolve_address(text, confirmed)
        if not addressed:
            # Nothing audible happened here unless a peek was armed. If this
            # looked like a wake we mis-heard over playback, arm one so her
            # next attempt is captured cleanly.
            _DUCK.on_unaddressed(_looks_like_missed_wake(stt, text))
            _log_unaddressed(stt, text)
            flush_stream(stream)
            wake.reset()
            return False
        # One verdict for both strings: the command is carved out of the
        # transcript, so withholding one and printing the other leaks it.
        hide = hide_utterance(text, command)
        logger.info("ADDRESSED: %r  (command=%r address=%s)",
                    log_privacy.safe_heard(text, hide=hide),
                    log_privacy.safe_heard(command, hide=hide), how)
        # Duck for the whole exchange — the reply and, if enabled, the
        # hands-free follow-up window — restored when the exchange ends.
        _DUCK.on_addressed()
        converse(stream, stt, tts, history, initial_command=command,
                 wake=wake, confirmed=confirmed, address=how,
                 followup=followup)
        flush_stream(stream)
        wake.reset()
        logger.info("listening for 'hey puffpai' …")
        return True
    finally:
        _DUCK.restore()


def main() -> int:
    from puffpai.voice.capture import CaptureStream, CaptureStalled
    _setup_logging()
    make_cues()
    logger.info("loading wake model: %s (threshold=%.2f)", WAKE_MODEL, WAKE_THRESHOLD)
    wake = WakeWord(WAKE_MODEL, WAKE_THRESHOLD)
    stt, tts = _load_engines()
    history: list = []

    if ANNOUNCE_ONLINE:
        speak(tts, "Puff voice is online.")

    logger.info("opening capture device: %s", CAPTURE_DEVICE)
    try:
      with CaptureStream(device=CAPTURE_DEVICE, channels=1, samplerate=SAMPLE_RATE,
                         dtype="int16", blocksize=BLOCK,
                         stall_timeout=CAPTURE_STALL_TIMEOUT,
                         max_reopen=CAPTURE_MAX_REOPEN) as stream:
        logger.info("listening for 'hey puffpai' …")
        last_trigger = 0.0
        last_announce = 0.0
        while True:
            data, _ = stream.read(BLOCK)
            samples = data[:, 0] if data.ndim > 1 else data
            score = wake.push(samples)
            now = time.time()
            if score >= WAKE_THRESHOLD and (now - last_trigger) > WAKE_COOLDOWN:
                last_trigger = now
                handle_wake(stream, stt, tts, wake, history)
                continue
            # Proactive speech (alarms, timers, missed-alarm notices) between
            # wake checks. Throttled to one cheap file read every couple of
            # seconds; only a real announcement costs anything more.
            if (now - last_announce) >= ANNOUNCE_POLL_SECONDS:
                last_announce = now
                if drain_announcements(tts, stream=stream, wake=wake, stt=stt):
                    # Our own voice is in the capture buffer now — drop it, or
                    # the next push() hears Puff and wakes on her.
                    flush_stream(stream)
                    wake.reset()
    except CaptureStalled as exc:
        logger.error("%s", exc)
        return 1


# ─────────────────────────────── self test ─────────────────────────────────
def selftest() -> int:
    _setup_logging()
    logger.info("=== SELFTEST ===")
    make_cues()

    # 1) wake model on real positive fixtures
    wake = WakeWord(WAKE_MODEL, WAKE_THRESHOLD)
    fixtures = sorted(glob.glob("/home/puffpai/wwtest/*.wav"))
    for f in fixtures:
        with wave.open(f, "rb") as w:
            ch = w.getnchannels()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if ch > 1:
            a = a[::ch]
        peak = 0.0
        wake.reset()
        for i in range(0, len(a), BLOCK):
            peak = max(peak, wake.push(a[i:i + BLOCK]))
        logger.info("wake fixture %-14s peak=%.3f  %s",
                    os.path.basename(f), peak, "DETECT" if peak >= WAKE_THRESHOLD else "miss")

    # 2) STT + TTS engines
    stt, tts = _load_engines()
    if fixtures:
        with wave.open(fixtures[0], "rb") as w:
            ch = w.getnchannels()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if ch > 1:
            a = a[::ch]
        # Wake fixtures under wwtest/ are recordings of a household member
        # saying the wake word. Report that STT ran and how confidently, not
        # what it heard, unless transcripts are explicitly opted in.
        fixture_text = stt.transcribe(a.tobytes())
        if LOG_TRANSCRIPTS:
            logger.info("STT on fixture -> %r", fixture_text)
        else:
            logger.info("STT on fixture -> %s", _utterance_event(
                len=len(fixture_text), conf=_conf_of(stt)))

    # 3) brain round-trip with memory
    hist: list = []
    logger.info("brain #1 -> %s", ask_brain(hist, "In three words, say hello."))
    logger.info("brain #2 (memory) -> %s", ask_brain(hist, "What did I just ask you to do?"))

    # 4) TTS playback (audible confirmation)
    speak(tts, "Puff voice self test complete.")
    logger.info("=== SELFTEST DONE ===")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
