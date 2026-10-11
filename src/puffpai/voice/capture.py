"""puffpai.voice.capture — stall-proof microphone capture.

Why this module exists
----------------------
``voice/service.py`` opens one ``sounddevice.InputStream`` for the life of the
process. Read with a bare, **untimed** ``stream.read(BLOCK)``, it can hang:
PortAudio's blocking read has no timeout, so if the underlying ALSA/PulseAudio
capture PCM stops producing frames, that call never returns and the whole voice
loop wedges: no log line, no error, no exit, so systemd's
``Restart=on-failure`` never fires because nothing failed. The service PID
stays the same (``NRestarts=0``), the Pulse source-output is never destroyed,
the kernel logs nothing, and only unplugging the microphone recovers it.

The guard has two halves:

* Reads happen on a dedicated daemon thread that feeds a bounded queue, so the
  caller's ``read()`` is a queue ``get`` with a timeout and can never block
  forever.
* A stall detector: if no frame has arrived for ``stall_timeout`` seconds while a
  caller is waiting for audio, close and reopen the stream (re-resolving the
  device name, so Pulse hands us the *current* default source).  If successive
  reopens still deliver no audio, raise :class:`CaptureStalled` so the service
  can exit non-zero and let the unit's ``Restart=`` policy bring it back.

The stall clock only runs while a caller is actually waiting for frames, which is
precisely "the service believes it is listening".  While Puff is speaking nobody
reads, so the watchdog cannot false-fire on her own TTS.
"""

from __future__ import annotations

import queue
import logging
import threading
import time

logger = logging.getLogger("puffpai.voice")


class CaptureStalled(RuntimeError):
    """Capture stopped delivering frames and could not be recovered."""


def _default_opener(device, samplerate, blocksize, channels, dtype):
    """Open and start a live PortAudio input stream."""
    import sounddevice as sd

    stream = sd.InputStream(device=device, channels=channels,
                            samplerate=samplerate, dtype=dtype,
                            blocksize=blocksize)
    stream.start()
    return stream


class CaptureStream:
    """A watchdogged microphone stream with the same read shape as sounddevice.

    ``read(frames)`` returns ``(data, overflowed)`` just like
    ``sounddevice.InputStream.read``, so callers need no change.
    """

    def __init__(self, device, samplerate, blocksize, channels=1,
                 dtype="int16", stall_timeout=15.0, max_reopen=2,
                 opener=None, queue_seconds=30.0):
        self._device = device
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._channels = channels
        self._dtype = dtype
        self._stall_timeout = float(stall_timeout)
        self._max_reopen = int(max_reopen)
        self._opener = opener or _default_opener

        depth = max(4, int(queue_seconds * samplerate / max(1, blocksize)))
        self._q: queue.Queue = queue.Queue(maxsize=depth)
        self._lock = threading.Lock()
        self._last_frame_at = time.monotonic()
        self._stream = None
        self._token = 0            # bumped to orphan a reader thread
        self._closing = False
        self._reopen_attempts = 0

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> "CaptureStream":
        self._stream = self._opener(self._device, self._samplerate,
                                    self._blocksize, self._channels,
                                    self._dtype)
        with self._lock:
            self._last_frame_at = time.monotonic()
        self._spawn_reader()
        return self

    def close(self) -> None:
        self._closing = True
        self._token += 1
        stream, self._stream = self._stream, None
        _shutdown(stream)

    def __enter__(self) -> "CaptureStream":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── reader thread ──────────────────────────────────────────────────────
    def _spawn_reader(self) -> None:
        token = self._token
        stream = self._stream
        threading.Thread(target=self._reader, args=(stream, token),
                         name="puff-capture", daemon=True).start()

    def _reader(self, stream, token) -> None:
        """Pull blocks forever. Exits quietly once orphaned by a reopen/close.

        An orphaned thread may still be parked inside ``stream.read`` — that is
        fine and is the whole point: it is no longer the loop the service waits
        on, it holds no lock, and ``_shutdown`` aborts its stream underneath it.
        """
        while True:
            if self._closing or token != self._token:
                return
            try:
                data, _ = stream.read(self._blocksize)
            except Exception as exc:
                if not self._closing and token == self._token:
                    logger.warning("capture read error: %s", exc)
                return
            if self._closing or token != self._token:
                return
            with self._lock:
                self._last_frame_at = time.monotonic()
            self._offer(data)

    def _offer(self, data) -> None:
        """Enqueue a block, dropping the oldest when the backlog is full."""
        try:
            self._q.put_nowait(data)
            return
        except queue.Full:
            pass
        try:
            self._q.get_nowait()
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(data)
        except queue.Full:
            pass

    # ── consumer API ───────────────────────────────────────────────────────
    def read(self, frames=None):
        """Return ``(block, overflowed)``; never blocks indefinitely.

        ``frames`` is accepted for signature compatibility and ignored — the
        stream is opened with a fixed ``blocksize`` and always yields whole
        blocks, which is what every caller in ``service.py`` asks for.
        """
        while True:
            try:
                data = self._q.get(timeout=0.5)
            except queue.Empty:
                self._check_stall()
                continue
            self._reopen_attempts = 0
            return data, False

    @property
    def read_available(self) -> int:
        return self._q.qsize() * self._blocksize

    def flush(self) -> None:
        """Discard buffered input (e.g. the tail of Puff's own reply)."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    # ── the watchdog ───────────────────────────────────────────────────────
    def _check_stall(self) -> None:
        with self._lock:
            idle = time.monotonic() - self._last_frame_at
        if idle < self._stall_timeout:
            return
        logger.warning("capture stalled (no frames %.0fs)", idle)
        self._reopen()

    def _reopen(self) -> None:
        self._reopen_attempts += 1
        if self._reopen_attempts > self._max_reopen:
            logger.error(
                "capture dead: %d reopen attempts of %r delivered no audio; "
                "exiting so the unit's Restart= policy recovers the service",
                self._reopen_attempts - 1, self._device)
            raise CaptureStalled(
                "capture device %r delivered no audio after %d reopen attempts"
                % (self._device, self._reopen_attempts - 1))

        self._token += 1                      # orphan the wedged reader thread
        stream, self._stream = self._stream, None
        _shutdown(stream)
        self.flush()
        try:
            self._stream = self._opener(self._device, self._samplerate,
                                        self._blocksize, self._channels,
                                        self._dtype)
        except Exception as exc:
            logger.error("capture reopen failed (attempt %d/%d): %s",
                         self._reopen_attempts, self._max_reopen, exc)
            with self._lock:
                self._last_frame_at = time.monotonic()
            return
        with self._lock:
            self._last_frame_at = time.monotonic()
        self._spawn_reader()
        logger.info("capture stalled … reopened (device=%s, attempt %d/%d)",
                    self._device, self._reopen_attempts, self._max_reopen)


def _shutdown(stream) -> None:
    """Best-effort abort+close. ``abort`` is what unblocks a parked reader."""
    if stream is None:
        return
    for name in ("abort", "close"):
        try:
            method = getattr(stream, name, None)
            if callable(method):
                method()
        except Exception as exc:
            logger.debug("capture %s failed: %s", name, exc)
