"""
PuffPAI Audio Device Detection & Diagnostics

Enumerate microphones and speakers on the current system, test audio
capture and playback, and report device capabilities.

Requires ``sounddevice`` (optional dependency — gracefully degrades).

Usage:
    from puffpai.voice.audio_devices import AudioDeviceManager

    mgr = AudioDeviceManager()
    print(mgr.list_devices())
    print(mgr.get_default_input())
    mgr.test_microphone(duration=3)
    mgr.test_speaker()

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Graceful import — sounddevice is an optional dependency
try:
    import sounddevice as sd
    _HAS_SOUNDDEVICE = True
except (ImportError, OSError):
    sd = None  # type: ignore[assignment]
    _HAS_SOUNDDEVICE = False

# Try numpy for test tone generation
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False


_SAMPLE_RATE = 16000   # 16 kHz
_CHANNELS = 1          # mono


@dataclass
class DeviceInfo:
    """Parsed audio device information."""
    index: int
    name: str
    kind: str                       # "input", "output", or "duplex"
    channels_in: int = 0
    channels_out: int = 0
    sample_rate: float = 0.0
    is_default_input: bool = False
    is_default_output: bool = False


@dataclass
class MicTestResult:
    """Result from a microphone test recording."""
    duration_s: float
    rms_energy: float
    peak_energy: float
    has_audio: bool               # True if energy suggests speech/sound
    device_name: str = ""
    error: str | None = None


class AudioDeviceManager:
    """Detect and test audio devices on the current system.

    All methods degrade gracefully when ``sounddevice`` is not installed.
    """

    def __init__(self):
        self._available = _HAS_SOUNDDEVICE

    @property
    def is_available(self) -> bool:
        """Whether the sounddevice library is installed and usable."""
        return self._available

    def list_devices(self) -> list[DeviceInfo]:
        """Enumerate all audio devices on the system.

        Returns:
            List of DeviceInfo dataclasses, one per device.
            Empty list if sounddevice is not available.
        """
        if not self._available:
            logger.warning("sounddevice not available — cannot list devices")
            return []

        devices = []
        try:
            raw = sd.query_devices()
            default_in = sd.default.device[0] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
            default_out = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
        except Exception as e:
            logger.error("Failed to query audio devices: %s", e)
            return []

        if not isinstance(raw, list):
            raw = [raw]

        for i, d in enumerate(raw):
            ch_in = d.get('max_input_channels', 0)
            ch_out = d.get('max_output_channels', 0)
            if ch_in > 0 and ch_out > 0:
                kind = 'duplex'
            elif ch_in > 0:
                kind = 'input'
            elif ch_out > 0:
                kind = 'output'
            else:
                continue  # skip devices with no channels

            devices.append(DeviceInfo(
                index=i,
                name=d.get('name', f'Device {i}'),
                kind=kind,
                channels_in=ch_in,
                channels_out=ch_out,
                sample_rate=d.get('default_samplerate', 0.0),
                is_default_input=(i == default_in),
                is_default_output=(i == default_out),
            ))

        return devices

    def get_default_input(self) -> DeviceInfo | None:
        """Return info for the default input (microphone) device."""
        for d in self.list_devices():
            if d.is_default_input:
                return d
        return None

    def get_default_output(self) -> DeviceInfo | None:
        """Return info for the default output (speaker) device."""
        for d in self.list_devices():
            if d.is_default_output:
                return d
        return None

    def test_microphone(self, duration: float = 3.0,
                        device: int | None = None) -> MicTestResult:
        """Record a short clip and measure audio energy levels.

        Args:
            duration: Recording duration in seconds.
            device: Device index to use (None = default input).

        Returns:
            MicTestResult with energy levels and whether sound was detected.
        """
        if not self._available:
            return MicTestResult(
                duration_s=duration, rms_energy=0, peak_energy=0,
                has_audio=False, error="sounddevice not available")

        try:
            frames = int(duration * _SAMPLE_RATE)
            logger.info("Recording %0.1fs from device %s...", duration, device)

            recording = sd.rec(
                frames, samplerate=_SAMPLE_RATE,
                channels=_CHANNELS, dtype='int16', device=device)
            sd.wait()

            # Compute energy
            pcm = recording.tobytes()
            rms = self._compute_rms(pcm)
            peak = self._compute_peak(pcm)

            dev_name = ""
            if device is not None:
                devs = self.list_devices()
                for d in devs:
                    if d.index == device:
                        dev_name = d.name
                        break
            else:
                default = self.get_default_input()
                dev_name = default.name if default else "default"

            return MicTestResult(
                duration_s=duration,
                rms_energy=rms,
                peak_energy=peak,
                has_audio=(rms > 50),  # threshold for "some sound present"
                device_name=dev_name,
            )

        except Exception as e:
            logger.error("Microphone test failed: %s", e)
            return MicTestResult(
                duration_s=duration, rms_energy=0, peak_energy=0,
                has_audio=False, error=str(e))

    def test_speaker(self, duration: float = 1.0,
                     frequency: float = 440.0,
                     device: int | None = None) -> bool:
        """Play a test tone through the speakers.

        Args:
            duration: Tone duration in seconds.
            frequency: Tone frequency in Hz.
            device: Device index to use (None = default output).

        Returns:
            True if the tone was played successfully.
        """
        if not self._available:
            logger.warning("sounddevice not available — cannot play test tone")
            return False

        if not _HAS_NUMPY:
            logger.warning("numpy not available — cannot generate test tone")
            return False

        try:
            t = np.linspace(0, duration, int(44100 * duration), endpoint=False)
            tone = (0.3 * np.sin(2 * np.pi * frequency * t)).astype(np.float32)

            logger.info("Playing %0.1fs tone at %dHz on device %s",
                        duration, frequency, device)
            sd.play(tone, samplerate=44100, device=device)
            sd.wait()
            return True

        except Exception as e:
            logger.error("Speaker test failed: %s", e)
            return False

    def get_diagnostics(self) -> dict:
        """Return a diagnostic summary of the audio subsystem.

        Returns:
            Dict with: available, device_count, default_input, default_output,
            input_devices, output_devices.
        """
        diag: dict = {
            'available': self._available,
            'has_numpy': _HAS_NUMPY,
            'device_count': 0,
            'input_count': 0,
            'output_count': 0,
            'default_input': None,
            'default_output': None,
        }

        if not self._available:
            return diag

        devices = self.list_devices()
        diag['device_count'] = len(devices)
        diag['input_count'] = sum(1 for d in devices if d.kind in ('input', 'duplex'))
        diag['output_count'] = sum(1 for d in devices if d.kind in ('output', 'duplex'))

        default_in = self.get_default_input()
        default_out = self.get_default_output()
        diag['default_input'] = default_in.name if default_in else None
        diag['default_output'] = default_out.name if default_out else None

        return diag

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _compute_rms(pcm_bytes: bytes) -> float:
        """Compute RMS energy of 16-bit PCM audio."""
        if len(pcm_bytes) < 2:
            return 0.0
        n_samples = len(pcm_bytes) // 2
        samples = struct.unpack(f'<{n_samples}h', pcm_bytes[:n_samples * 2])
        if not samples:
            return 0.0
        sumsq = sum(s * s for s in samples)
        return (sumsq / n_samples) ** 0.5

    @staticmethod
    def _compute_peak(pcm_bytes: bytes) -> float:
        """Compute peak absolute amplitude of 16-bit PCM audio."""
        if len(pcm_bytes) < 2:
            return 0.0
        n_samples = len(pcm_bytes) // 2
        samples = struct.unpack(f'<{n_samples}h', pcm_bytes[:n_samples * 2])
        if not samples:
            return 0.0
        return float(max(abs(s) for s in samples))
