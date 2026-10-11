"""Playback ducking policy for the voice loop.

Why this module exists
======================
The wake model is a *speech* detector, not a name detector — its own comment
says it "fires at ~1.0 on any full-scale audio (TV, radio, speech)". Ducking
the music the instant it fires, i.e. BEFORE transcribing and BEFORE knowing
whether anyone has actually addressed Puff, dips the music dozens of times an
hour for every genuinely addressed turn. The household hears that as "the
music dips every time I sing along".

Moving the duck to after ``parse_address()`` does not work either: waking over
music then fails, because the *command* still transcribes but her wake NAME is
dropped, so ``parse_address()`` rejects it ("is it?", "What time is it?").
Whisper decodes ``pre + cont`` as ONE utterance, so a degraded continuation
degrades the whole decode and eats the leading name.

Two facts constrain the policy:

* The ReSpeaker XVF3800's echo canceller is wired *by hardware topology*: the
  same USB device is both the Pulse default sink and the capture source, so the
  DSP's AEC far-end reference is the audio we play. No ``module-echo-cancel`` is
  loaded and none is needed. Our own Pulse-routed playback (radio/mpv, Jellyfin,
  Piper TTS) is therefore NOT what the mic hears — measured: jazz at 100% gives
  mic RMS p50=25/max=135, below the room's own noise floor and below
  ``WAKE_GATE``(150). Ducking never bought us far-end rejection.
* But far-end rejection is not double-talk. The XVF3800's residual-echo
  suppressor attenuates near-end speech while the far end is active, which is
  the most likely reason the un-ducked continuation lost the name.

So the policy is two-stage and *reactive*, never pre-emptive by default:

1. **Listening** — no duck at all (``PUFF_DUCK_ON_LISTEN=0``, the default). An
   un-addressed utterance therefore changes nothing audible. Ever.
2. **Addressed** — duck to ``PUFF_DUCK_ADDRESSED_LEVEL`` (45%) for the whole
   exchange: the reply, and the hands-free follow-up window if it is open.
   Restored the moment the exchange ends.
3. **Peek** — if an un-addressed pass looks like a *missed* wake (a SHORT
   transcript that is weak or carries a fragment of her name) AND something is
   actually playing, arm a short peek window. The NEXT wake trigger inside that
   window ducks to ``PUFF_DUCK_PEEK_LEVEL`` (70%, much lighter than the
   addressed level) before recording, so when she repeats herself over loud music the
   capture is clean.

Why the peek rules are strict: singing along fires the wake model every few
seconds, each trigger runs the stage-2 confirm, and sung audio comes back as
``heard=""`` or a one-word interjection ("Ohhhh!", "Ahhhhhhhhhhh", "I do.") at
a poor confidence. A peek arm that treats that as "a wake we missed" arms on
every few notes, so the very next note dips the music and the singer hears
the volume drop right when they are belting it out. The arms therefore demand
wake-SHAPED evidence (see ``service`` for the two
predicates), a peek duck is never held on nothing (``PUFF_DUCK_PEEK_HOLD``),
and ``PUFF_DUCK_PEEK=0`` turns both arms off outright.

Note on what a duck can and cannot do: by the time we know a transcript was
weak, the audio it came from is already captured. Lowering the music at that
point cannot change that transcript — only the *next* capture. That is why the
peek is armed for the next trigger rather than applied retroactively.

Mixer notes: we lower sink-INPUTS, snapshotted at duck time, so Puff's own reply
— a sink-input created afterwards — plays at full over the ducked music.
Deepening re-uses the snapshot for the same reason. Spotify (librespot) and
drives bare ALSA (``hw:APE,0``), not this sink, so it is
outside both this lever and the XVF3800's AEC reference.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

logger = logging.getLogger("puffpai.voice")

#: Duck while merely listening (at the peek level). Off by default: an
#: un-addressed utterance must change nothing.
DUCK_ON_LISTEN = os.environ.get("PUFF_DUCK_ON_LISTEN", "0") == "1"
#: Light dip used for the peek path (and for DUCK_ON_LISTEN).
PEEK_LEVEL = os.environ.get("PUFF_DUCK_PEEK_LEVEL", "70")
#: Deeper dip once she has actually addressed us, so the reply is audible.
#: ``PUFF_DUCK_PCT`` is the legacy name and still wins if it is set (the
#: appliance drop-in sets it).
ADDRESSED_LEVEL = os.environ.get("PUFF_DUCK_ADDRESSED_LEVEL",
                                 os.environ.get("PUFF_DUCK_PCT", "45"))
#: Seconds a peek stays armed after a suspected missed wake.
PEEK_ARM = float(os.environ.get("PUFF_DUCK_PEEK_ARM", "20"))
#: Longest a PEEK duck may sit on the music with nothing confirmed. The reject
#: path restores explicitly; this is the backstop for any path that does not.
PEEK_HOLD = float(os.environ.get("PUFF_DUCK_PEEK_HOLD", "4"))
#: Kill switch for the whole peek path. ``PUFF_DUCK_PEEK=0`` means the music is
#: only ever touched by an ADDRESSED exchange.
PEEK_ENABLED = os.environ.get("PUFF_DUCK_PEEK", "1") != "0"


class DuckPolicy:
    """Stateful ducking policy for one voice loop. Best-effort; never raises."""

    def __init__(self, addressed_level: str | None = None,
                 peek_level: str | None = None,
                 duck_on_listen: bool | None = None,
                 peek_arm: float | None = None,
                 peek_hold: float | None = None,
                 peek_enabled: bool | None = None,
                 runner=None, clock=None):
        self.addressed_level = ADDRESSED_LEVEL if addressed_level is None else addressed_level
        self.peek_level = PEEK_LEVEL if peek_level is None else peek_level
        self.duck_on_listen = DUCK_ON_LISTEN if duck_on_listen is None else duck_on_listen
        self.peek_arm = PEEK_ARM if peek_arm is None else peek_arm
        self.peek_hold = PEEK_HOLD if peek_hold is None else peek_hold
        self.peek_enabled = PEEK_ENABLED if peek_enabled is None else peek_enabled
        self._run = runner or subprocess.run
        self._now = clock or time.time
        self._ducked: list[str] = []
        self._level: str | None = None
        self._peek_until = 0.0
        self._peek_reason = ""
        #: When the CURRENT duck started as a peek (0.0 = not a peek duck).
        self._peek_ducked_at = 0.0

    # ── mixer primitives ───────────────────────────────────────────────────
    def _sink_inputs(self) -> list[str]:
        try:
            out = self._run(["pactl", "list", "short", "sink-inputs"],
                            capture_output=True, text=True, timeout=2).stdout or ""
        except Exception as exc:                       # pragma: no cover - env
            logger.debug("sink-input listing failed: %s", exc)
            return []
        idxs = []
        for line in out.splitlines():
            idx = line.split("\t", 1)[0].strip()
            if idx.isdigit():
                idxs.append(idx)
        return idxs

    def playback_active(self) -> bool:
        """True if anything is currently playing through the Pulse sink."""
        return bool(self._sink_inputs())

    @property
    def active(self) -> bool:
        return bool(self._ducked)

    @property
    def level(self) -> str | None:
        return self._level

    def _set(self, idxs, level: str) -> None:
        for idx in idxs:
            try:
                self._run(["pactl", "set-sink-input-volume", idx, f"{level}%"],
                          check=False, timeout=2)
            except Exception as exc:                   # pragma: no cover - env
                logger.debug("duck failed: %s", exc)

    def duck(self, level: str, reason: str) -> int:
        """Snapshot the playing streams and lower them to ``level``."""
        if self._ducked:
            return self.deepen(level, reason)
        idxs = self._sink_inputs()
        if not idxs:
            return 0
        self._set(idxs, level)
        self._ducked = idxs
        self._level = level
        logger.info("ducked %d playback stream(s) to %s%% (%s)",
                    len(idxs), level, reason)
        return len(idxs)

    def deepen(self, level: str, reason: str) -> int:
        """Lower the ALREADY-snapshotted streams further (never re-snapshots,
        so Puff's own reply — started after the snapshot — stays at full)."""
        if not self._ducked or level == self._level:
            return len(self._ducked)
        self._set(self._ducked, level)
        self._level = level
        logger.info("ducked %d playback stream(s) to %s%% (%s)",
                    len(self._ducked), level, reason)
        return len(self._ducked)

    def restore(self) -> None:
        if not self._ducked:
            self._level = None
            self._peek_ducked_at = 0.0
            return
        self._set(self._ducked, "100")
        logger.info("duck=restored streams=%d", len(self._ducked))
        self._ducked = []
        self._level = None
        self._peek_ducked_at = 0.0

    # ── policy ─────────────────────────────────────────────────────────────
    def peek_armed(self) -> bool:
        return self._now() < self._peek_until

    @property
    def peeking(self) -> bool:
        """Is the CURRENT duck a peek (i.e. nothing is confirmed yet)?"""
        return bool(self._ducked) and self._peek_ducked_at > 0.0

    def expire_peek(self) -> bool:
        """Let go of a peek duck that has outlived ``peek_hold``.

        A peek is a guess. The reject path restores explicitly, but a guess must
        never be able to sit on the music because some other path forgot to.
        """
        if self.peeking and (self._now() - self._peek_ducked_at) >= self.peek_hold:
            self.restore()
            return True
        return False

    def on_wake(self) -> int:
        """Called the instant the wake model fires, before recording.

        Ducks ONLY if the owner opted into listen-ducking, or if a previous
        pass looked like a missed wake over music and armed a peek.
        """
        self.expire_peek()
        if self.duck_on_listen:
            return self.duck(self.peek_level, "listening")
        if self.peek_armed():
            reason = self._peek_reason or "missed"
            self._peek_until = 0.0
            self._peek_reason = ""
            n = self.duck(self.peek_level, "peek: retrying a possible missed wake")
            if n:
                self._peek_ducked_at = self._now()
                logger.info("duck=peek reason=%s level=%s", reason, self.peek_level)
            return n
        return 0

    def on_addressed(self) -> int:
        """Called once ``parse_address()`` confirms we were addressed."""
        self._peek_until = 0.0
        self._peek_reason = ""
        # A confirmed wake earns the deeper duck and turns the peek into a real
        # one, so the hold no longer applies to it.
        self._peek_ducked_at = 0.0
        return self.duck(self.addressed_level, "addressed")

    def on_unaddressed(self, weak: bool, reason: str = "missed") -> bool:
        """Called when the utterance was not addressed to us.

        Arms a peek (returns True) when the pass looked like a wake we failed to
        hear over playback — the CALLER decides that (see
        ``service._looks_like_missed_wake`` / ``service.wake_retry_pressure``),
        and both predicates demand wake-shaped evidence so
        singing along never arms anything. Does not duck: the audio that
        produced this transcript is already captured, so a duck now could only
        help the NEXT attempt — which is exactly what arming does.

        Also releases a peek duck that this very pass just disproved: an
        un-addressed pass is the end of that guess.
        """
        if self.peeking:
            self.restore()
        if not self.peek_enabled:
            return False
        if weak and not self.active and self.playback_active():
            self._peek_until = self._now() + self.peek_arm
            self._peek_reason = reason
            logger.info("duck=armed reason=%s for=%.0fs level=%s",
                        reason, self.peek_arm, self.peek_level)
            return True
        return False
