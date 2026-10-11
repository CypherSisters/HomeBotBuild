#!/usr/bin/env python3
"""
PuffPAI Voice Pipeline Live Test Harness
==========================================
Manual test harness for exercising the full voice pipeline on real hardware:
  mic -> Vosk STT -> Ollama agent -> Piper TTS -> speaker

Usage:
    python scripts/test_voice_live.py --full    # Full pipeline test
    python scripts/test_voice_live.py --stt     # STT only (mic -> text)
    python scripts/test_voice_live.py --tts     # TTS only (text -> speaker)
    python scripts/test_voice_live.py --devices # List audio devices
    python scripts/test_voice_live.py --mic     # Microphone test (record + energy)

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import sys
import os
import argparse
import logging

# Make src/ importable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))


def cmd_devices():
    """List all audio devices."""
    from puffpai.voice.audio_devices import AudioDeviceManager

    mgr = AudioDeviceManager()
    if not mgr.is_available:
        print("ERROR: sounddevice not installed.")
        print("Install with: pip install sounddevice")
        return

    diag = mgr.get_diagnostics()
    print("\nAudio Subsystem Diagnostics")
    print("=" * 50)
    print(f"  sounddevice available : {diag['available']}")
    print(f"  numpy available       : {diag['has_numpy']}")
    print(f"  Total devices         : {diag['device_count']}")
    print(f"  Input devices         : {diag['input_count']}")
    print(f"  Output devices        : {diag['output_count']}")
    print(f"  Default input         : {diag['default_input']}")
    print(f"  Default output        : {diag['default_output']}")

    print("\nAll devices:")
    for d in mgr.list_devices():
        default = ""
        if d.is_default_input:
            default += " [DEFAULT INPUT]"
        if d.is_default_output:
            default += " [DEFAULT OUTPUT]"
        print(f"  [{d.index}] {d.name} ({d.kind}){default}")
        print(f"       In: {d.channels_in}ch  Out: {d.channels_out}ch  "
              f"Rate: {d.sample_rate:.0f}Hz")
    print()


def cmd_mic(duration: float = 3.0):
    """Test the microphone by recording and measuring energy."""
    from puffpai.voice.audio_devices import AudioDeviceManager

    mgr = AudioDeviceManager()
    if not mgr.is_available:
        print("ERROR: sounddevice not installed.")
        return

    print(f"\nMicrophone Test ({duration}s)")
    print("=" * 40)
    print("Speak into your microphone now...")

    result = mgr.test_microphone(duration=duration)

    print(f"\n  Device   : {result.device_name}")
    print(f"  Duration : {result.duration_s:.1f}s")
    print(f"  RMS      : {result.rms_energy:.1f}")
    print(f"  Peak     : {result.peak_energy:.1f}")
    print(f"  Audio?   : {'YES' if result.has_audio else 'NO (too quiet)'}")
    if result.error:
        print(f"  Error    : {result.error}")
    print()


def cmd_stt(duration: float = 5.0):
    """Test STT: record from mic, transcribe with Vosk."""
    from puffpai.voice.audio_devices import AudioDeviceManager
    from puffpai.voice.stt import VoskSTT

    mgr = AudioDeviceManager()
    if not mgr.is_available:
        print("ERROR: sounddevice not installed.")
        return

    print(f"\nSTT Test ({duration}s)")
    print("=" * 40)

    stt = VoskSTT()
    if not stt.is_available():
        print("ERROR: Vosk not available. Install with: pip install vosk")
        print("Also need model in: models/vosk-model-small-en-us/")
        return

    import sounddevice as sd
    print("Speak now...")

    frames = int(duration * 16000)
    audio = sd.rec(frames, samplerate=16000, channels=1, dtype='int16')
    sd.wait()
    pcm = audio.tobytes()

    print(f"Recorded {len(pcm)} bytes")
    text = stt.transcribe(pcm)
    print(f"\nTranscription: {text or '(empty)'}")
    print()


def cmd_tts(text: str = "Hello! I am PuffPAI, your privacy-first AI assistant."):
    """Test TTS: synthesize speech and play through speakers."""
    from puffpai.voice.tts import PiperTTS

    print("\nTTS Test")
    print("=" * 40)

    tts = PiperTTS()
    if not tts.is_available():
        print("ERROR: Piper TTS not available.")
        print("Need model at: models/en_US-lessac-medium.onnx")
        return

    print(f"Speaking: {text}")
    tts.speak(text)
    print("Done.\n")


def cmd_full(duration: float = 5.0):
    """Full pipeline test: mic -> STT -> agent -> TTS -> speaker."""
    from puffpai.voice.pipeline import VoicePipeline
    from puffpai.voice.stt import VoskSTT
    from puffpai.voice.tts import PiperTTS

    print("\nFull Pipeline Test")
    print("=" * 40)

    stt = VoskSTT()
    tts = PiperTTS()

    def simple_agent(text):
        """Simple echo agent for testing (avoids Ollama dependency)."""
        return f"I heard you say: {text}"

    pipeline = VoicePipeline(
        stt=stt, tts=tts, agent_fn=simple_agent)

    print(f"STT ready  : {stt.is_available()}")
    print(f"TTS ready  : {tts.is_available()}")
    print(f"Pipeline   : {'READY' if pipeline.is_ready else 'NOT READY'}")

    if not pipeline.is_ready:
        print("\nPipeline not ready. Testing with text input instead...")
        result = pipeline.process_single_test(text="Hello PuffPAI")
        print(f"  Agent response: {result['response']}")
        print(f"  TTS OK: {result['tts_ok']}")
        return

    # Record from mic
    import sounddevice as sd
    print(f"\nSpeak now ({duration}s)...")
    frames = int(duration * 16000)
    audio = sd.rec(frames, samplerate=16000, channels=1, dtype='int16')
    sd.wait()
    pcm = audio.tobytes()

    print(f"Recorded {len(pcm)} bytes")
    result = pipeline.process_single_test(audio=pcm)
    print(f"\nResults:")
    print(f"  STT OK         : {result['stt_ok']}")
    print(f"  Transcription  : {result['transcription']}")
    print(f"  Agent OK       : {result['agent_ok']}")
    print(f"  Response       : {result['response']}")
    print(f"  TTS OK         : {result['tts_ok']}")
    if result['error']:
        print(f"  Error          : {result['error']}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="PuffPAI Voice Pipeline Live Test Harness")
    parser.add_argument('--full', action='store_true',
                        help='Full pipeline test (mic -> STT -> agent -> TTS)')
    parser.add_argument('--stt', action='store_true',
                        help='STT only (mic -> text)')
    parser.add_argument('--tts', action='store_true',
                        help='TTS only (text -> speaker)')
    parser.add_argument('--devices', action='store_true',
                        help='List audio devices')
    parser.add_argument('--mic', action='store_true',
                        help='Microphone test (record + energy)')
    parser.add_argument('--duration', type=float, default=5.0,
                        help='Recording duration in seconds (default: 5)')
    parser.add_argument('--text', type=str, default=None,
                        help='Custom text for TTS test')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose logging')
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format='%(name)s: %(message)s')

    if args.devices:
        cmd_devices()
    elif args.mic:
        cmd_mic(args.duration)
    elif args.stt:
        cmd_stt(args.duration)
    elif args.tts:
        cmd_tts(args.text or "Hello! I am PuffPAI, your privacy-first AI assistant.")
    elif args.full:
        cmd_full(args.duration)
    else:
        # Default: show devices
        cmd_devices()


if __name__ == "__main__":
    main()
