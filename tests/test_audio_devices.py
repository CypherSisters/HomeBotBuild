"""Tests for puffpai.voice.audio_devices — audio device detection."""

import struct
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from puffpai.voice.audio_devices import (
    AudioDeviceManager, DeviceInfo, MicTestResult,
    _HAS_SOUNDDEVICE,
)


class TestDeviceInfo:
    """DeviceInfo dataclass."""

    def test_input_device(self):
        d = DeviceInfo(index=0, name="Mic", kind="input", channels_in=1)
        assert d.kind == "input"
        assert d.channels_in == 1

    def test_output_device(self):
        d = DeviceInfo(index=1, name="Speaker", kind="output", channels_out=2)
        assert d.kind == "output"
        assert d.channels_out == 2


class TestMicTestResult:
    """MicTestResult dataclass."""

    def test_with_audio(self):
        r = MicTestResult(duration_s=3.0, rms_energy=250.0,
                          peak_energy=1000.0, has_audio=True)
        assert r.has_audio is True
        assert r.error is None

    def test_without_audio(self):
        r = MicTestResult(duration_s=3.0, rms_energy=10.0,
                          peak_energy=30.0, has_audio=False,
                          error="sounddevice not available")
        assert r.has_audio is False
        assert r.error is not None


class TestAudioDeviceManagerMocked:
    """AudioDeviceManager with mocked sounddevice."""

    def test_list_devices_with_mock(self):
        """list_devices() returns parsed DeviceInfo when sounddevice works."""
        mgr = AudioDeviceManager()
        mock_devices = [
            {'name': 'Test Mic', 'max_input_channels': 1,
             'max_output_channels': 0, 'default_samplerate': 16000.0},
            {'name': 'Test Speaker', 'max_input_channels': 0,
             'max_output_channels': 2, 'default_samplerate': 44100.0},
        ]

        with patch('puffpai.voice.audio_devices.sd') as mock_sd, \
             patch('puffpai.voice.audio_devices._HAS_SOUNDDEVICE', True):
            mock_sd.query_devices.return_value = mock_devices
            mock_sd.default.device = (0, 1)
            mgr._available = True

            devices = mgr.list_devices()

        assert len(devices) == 2
        assert devices[0].name == 'Test Mic'
        assert devices[0].kind == 'input'
        assert devices[1].name == 'Test Speaker'
        assert devices[1].kind == 'output'

    def test_list_devices_unavailable(self):
        """list_devices() returns empty list when sounddevice not available."""
        mgr = AudioDeviceManager()
        mgr._available = False
        assert mgr.list_devices() == []

    def test_test_microphone_unavailable(self):
        """test_microphone() returns error result when sounddevice not available."""
        mgr = AudioDeviceManager()
        mgr._available = False
        result = mgr.test_microphone()
        assert result.has_audio is False
        assert "not available" in result.error

    def test_test_speaker_unavailable(self):
        """test_speaker() returns False when sounddevice not available."""
        mgr = AudioDeviceManager()
        mgr._available = False
        assert mgr.test_speaker() is False


class TestComputeEnergy:
    """RMS and peak energy computation."""

    def test_rms_silence(self):
        """Silent audio has zero energy."""
        pcm = struct.pack('<4h', 0, 0, 0, 0)
        assert AudioDeviceManager._compute_rms(pcm) == 0.0

    def test_rms_loud_signal(self):
        """Loud audio has high energy."""
        pcm = struct.pack('<4h', 10000, -10000, 10000, -10000)
        rms = AudioDeviceManager._compute_rms(pcm)
        assert rms > 5000

    def test_peak_signal(self):
        """Peak returns max absolute amplitude."""
        pcm = struct.pack('<4h', 100, -500, 300, -200)
        peak = AudioDeviceManager._compute_peak(pcm)
        assert peak == 500.0

    def test_empty_pcm(self):
        """Empty audio returns zero."""
        assert AudioDeviceManager._compute_rms(b'') == 0.0
        assert AudioDeviceManager._compute_peak(b'') == 0.0
