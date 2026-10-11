#!/usr/bin/env python3
"""
PuffPAI Model Download Script

Downloads voice models for offline STT/TTS.
Idempotent — skips models that already exist.

Usage:
    python scripts/download_models.py              # download Vosk + Piper
    python scripts/download_models.py --vosk       # Vosk only
    python scripts/download_models.py --piper      # Piper only
    python scripts/download_models.py --llm        # LLM GGUF (TinyLlama Q4)
    python scripts/download_models.py --all        # everything

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
import zipfile
import shutil

# ── Model Definitions ────────────────────────────────────────────────────────

MODELS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models",
)

VOSK_MODEL = {
    "name": "vosk-model-small-en-us-0.15",
    "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    "target_dir": os.path.join(MODELS_DIR, "vosk-model-small-en-us"),
    "size_mb": 40,
    "description": "Vosk small English model (STT)",
}

PIPER_MODEL = {
    "name": "en_US-lessac-medium",
    "url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
    "target_path": os.path.join(MODELS_DIR, "en_US-lessac-medium.onnx"),
    "size_mb": 63,
    "description": "Piper lessac medium English voice (TTS)",
}

PIPER_CONFIG = {
    "name": "en_US-lessac-medium.json",
    "url": "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
    "target_path": os.path.join(MODELS_DIR, "en_US-lessac-medium.onnx.json"),
    "size_mb": 0.01,
    "description": "Piper model config JSON",
}

LLM_MODEL = {
    "name": "TinyLlama-1.1B-Chat-v1.0.Q4_K_M",
    "url": "https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf",
    "target_path": os.path.join(MODELS_DIR, "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"),
    "size_mb": 669,
    "description": "TinyLlama 1.1B Q4_K_M GGUF (test LLM)",
}


# ── Download Utilities ───────────────────────────────────────────────────────

def _download_file(url: str, dest: str, description: str = "") -> bool:
    """Download a file with progress reporting. Returns True on success."""
    if os.path.exists(dest):
        print(f"  [SKIP] {description or dest} — already exists")
        return True

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"  [DOWNLOAD] {description or os.path.basename(dest)}")
    print(f"    URL: {url}")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PuffPAI/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 1024 * 256  # 256 KB chunks

            tmp_dest = dest + ".tmp"
            with open(tmp_dest, "wb") as fh:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = downloaded * 100 // total
                        mb = downloaded / (1024 * 1024)
                        total_mb = total / (1024 * 1024)
                        print(
                            f"\r    Progress: {mb:.1f}/{total_mb:.1f} MB ({pct}%)",
                            end="", flush=True,
                        )

            # Atomic rename
            os.replace(tmp_dest, dest)
            print(f"\n    Saved to: {dest}")
            return True

    except Exception as exc:
        print(f"\n    [ERROR] Download failed: {exc}")
        # Clean up partial download
        for p in (dest + ".tmp", dest):
            if os.path.exists(p):
                os.remove(p)
        return False


def download_vosk() -> bool:
    """Download and extract the Vosk small English model."""
    info = VOSK_MODEL
    target = info["target_dir"]

    if os.path.isdir(target) and os.listdir(target):
        print(f"  [SKIP] {info['description']} — already exists at {target}")
        return True

    print(f"\n{'='*60}")
    print(f"Downloading: {info['description']} (~{info['size_mb']} MB)")
    print(f"{'='*60}")

    zip_path = os.path.join(MODELS_DIR, f"{info['name']}.zip")
    if not _download_file(info["url"], zip_path, f"{info['name']}.zip"):
        return False

    # Extract
    print(f"  [EXTRACT] Unzipping to {MODELS_DIR}")
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(MODELS_DIR)

        # Vosk zip extracts to vosk-model-small-en-us-0.15/
        # We want models/vosk-model-small-en-us/
        extracted = os.path.join(MODELS_DIR, info["name"])
        if os.path.isdir(extracted) and extracted != target:
            if os.path.exists(target):
                shutil.rmtree(target)
            os.rename(extracted, target)

        # Clean up zip
        os.remove(zip_path)
        print(f"  [OK] Vosk model ready at {target}")
        return True

    except Exception as exc:
        print(f"  [ERROR] Extraction failed: {exc}")
        return False


def download_piper() -> bool:
    """Download the Piper voice model and config."""
    info = PIPER_MODEL
    config = PIPER_CONFIG

    print(f"\n{'='*60}")
    print(f"Downloading: {info['description']} (~{info['size_mb']} MB)")
    print(f"{'='*60}")

    ok1 = _download_file(info["url"], info["target_path"], info["description"])
    ok2 = _download_file(config["url"], config["target_path"], config["description"])
    return ok1 and ok2


def download_llm() -> bool:
    """Download the TinyLlama GGUF model for testing."""
    info = LLM_MODEL

    print(f"\n{'='*60}")
    print(f"Downloading: {info['description']} (~{info['size_mb']} MB)")
    print(f"{'='*60}")

    return _download_file(info["url"], info["target_path"], info["description"])


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download PuffPAI voice and LLM models.",
    )
    parser.add_argument("--vosk", action="store_true", help="Download Vosk STT model")
    parser.add_argument("--piper", action="store_true", help="Download Piper TTS model")
    parser.add_argument("--llm", action="store_true", help="Download TinyLlama GGUF")
    parser.add_argument("--all", action="store_true", help="Download all models")

    args = parser.parse_args()

    # Default: download voice models (Vosk + Piper)
    if not (args.vosk or args.piper or args.llm or args.all):
        args.vosk = True
        args.piper = True

    if args.all:
        args.vosk = args.piper = args.llm = True

    os.makedirs(MODELS_DIR, exist_ok=True)
    print(f"Models directory: {MODELS_DIR}")

    results = []

    if args.vosk:
        results.append(("Vosk STT", download_vosk()))
    if args.piper:
        results.append(("Piper TTS", download_piper()))
    if args.llm:
        results.append(("TinyLlama GGUF", download_llm()))

    print(f"\n{'='*60}")
    print("Summary:")
    for name, ok in results:
        status = "OK" if ok else "FAILED"
        print(f"  {name}: {status}")
    print(f"{'='*60}")

    if all(ok for _, ok in results):
        print("\nAll downloads complete!")
        return 0
    else:
        print("\nSome downloads failed. Re-run to retry.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
