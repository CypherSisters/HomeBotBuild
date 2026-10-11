#!/usr/bin/env python3
"""
PuffPAI Setup Script
"""

from setuptools import setup, find_packages
import os

# Read README for long description
with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

# Read requirements
with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="puffpai",
    version="1.0.0",
    author="Cypher Sisters",
    author_email="307375596+CypherSisters@users.noreply.github.com",
    description="HomeBot — a local-first family and home AI assistant you install on your own hardware",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/CypherSisters/HomeBotBuild",
    project_urls={
        "Bug Reports": "https://github.com/CypherSisters/HomeBotBuild/issues",
        "Source": "https://github.com/CypherSisters/HomeBotBuild",
        "Documentation": "https://github.com/CypherSisters/HomeBotBuild/tree/main/docs",
    },
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: End Users/Desktop",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Home Automation",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Operating System :: POSIX :: Linux",
        "Operating System :: Microsoft :: Windows",
    ],
    python_requires=">=3.10",
    install_requires=requirements,
    extras_require={
        "dev": [
            "pytest>=9.0.0,<10.0",
            "black>=26.0.0,<27.0",
            "flake8>=7.0.0,<8.0",
            "mypy>=1.5.0,<2.0",
        ],
        # Single-file desktop build. Only needed to PRODUCE an executable, never
        # to run HomeBot, so it stays out of the base install.
        "desktop": [
            "pyinstaller>=6.0.0,<7.0",
        ],
        # Legacy embedding backend (all-MiniLM-L6-v2 via sentence-transformers).
        # Optional because it pulls PyTorch, which is a multi-gigabyte download
        # on x86 and on Jetson/ARM64 must come from NVIDIA's wheel index. The
        # default backend is Ollama /api/embed — see src/puffpai/embeddings.py.
        "embeddings-legacy": [
            "sentence-transformers>=5.0.0,<6.0",
        ],
        # Second-stage device classifier (security/device_ml.py). Optional
        # because the module guards the import and returns UNKNOWN without it:
        # the shipped classifier is the OUI table alone.
        "ml": [
            "scikit-learn",
        ],
        # OCR for image uploads and scanned PDFs. Both packages are thin
        # wrappers over system binaries that the OS must also provide:
        # pytesseract needs `tesseract-ocr`, pdf2image needs `poppler-utils`
        # (Windows: the Tesseract installer and a poppler build on PATH).
        # Without this extra, image OCR and image-only-PDF extraction return
        # empty text rather than failing.
        "ocr": [
            "pytesseract>=0.3.10",
            "pdf2image>=1.17.0",
        ],
        # Voice: microphone capture, wake word, speech-to-text, speech-out.
        # Out of the base install on purpose —
        #   * sounddevice needs PortAudio (bundled in the Windows wheel;
        #     `apt-get install libportaudio2` on Linux),
        #   * faster-whisper pulls CTranslate2, a compiled CPU/CUDA runtime with
        #     no wheel for some platforms and a ~200 MB footprint where there is
        #     one,
        #   * openwakeword pulls onnxruntime.
        # Voice is optional in HomeBotBuild; every one of these is imported
        # lazily inside a function, so their absence disables voice and nothing
        # else. Piper (TTS) is installed by the voice setup script, not pip, on
        # platforms with no wheel.
        "voice": [
            "sounddevice>=0.4.6,<1.0",
            "faster-whisper>=1.0.0",
            "openwakeword>=0.6.0",
            "vosk>=0.3.45",
        ],
        # Zone 3 (network edge) bootstrap — LINUX ONLY. bcrypt hashes the
        # generated AdGuard Home admin password before the seed config is
        # written (scripts/zone3_bootstrap_adguard.py). It is here and not in
        # the base because Zone 3 is opt-in and Linux-only, and bcrypt is the
        # one base-list package that needs a Rust toolchain wherever no wheel
        # is published.
        "zone3": [
            "bcrypt>=4.1.0,<5.0",
        ],
        # The Founders-appliance superset: what a full Jetson Orin reference
        # build installs. Nothing here is Jetson-specific by itself — it is a
        # convenience alias so `docs/INSTALL_ORIN.md` stays one line.
        "orin": [
            "sounddevice>=0.4.6,<1.0",
            "faster-whisper>=1.0.0",
            "openwakeword>=0.6.0",
            "vosk>=0.3.45",
            "pytesseract>=0.3.10",
            "pdf2image>=1.17.0",
            "bcrypt>=4.1.0,<5.0",
            "scikit-learn",
        ],
    },
    entry_points={
        "console_scripts": [
            "puffpai=puffpai.cli:main",
            "puffpai-service=puffpai.service:run_service",
        ],
    },
    include_package_data=True,
    package_data={
        "puffpai": [
            "config/*.json.template",
            "config/*.txt",
            # Offline directions: the Texas towns lists.
            "maps_texas_places*.txt",
        ],
    },
)
