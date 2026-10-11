#!/usr/bin/env bash
# ============================================================================
#  PuffPAI — Custom Wake Word Training Script
#  Run this inside WSL2 Ubuntu with an NVIDIA GPU accessible.
#
#  Usage:
#    bash train_wake_word.sh "hey puffpai"
#    bash train_wake_word.sh "hey puff" 2000
#
#  Arguments:
#    $1  Wake word phrase (required, e.g. "hey puffpai")
#    $2  Number of synthetic samples to generate (optional, default 1500)
# ============================================================================

set -euo pipefail

WAKE_PHRASE="${1:?Usage: bash train_wake_word.sh \"hey puffpai\" [num_samples]}"
NUM_SAMPLES="${2:-1500}"

WORK_DIR="$HOME/openwakeword-training"
SAMPLES_DIR="$WORK_DIR/samples"
AUGMENTED_DIR="$WORK_DIR/augmented"
OUTPUT_DIR="$WORK_DIR/output"

# Sanitize phrase into a filename-safe slug
SLUG=$(echo "$WAKE_PHRASE" | tr '[:upper:]' '[:lower:]' | tr ' ' '_' | tr -cd 'a-z0-9_')

echo ""
echo "============================================"
echo "  PuffPAI Wake Word Trainer"
echo "============================================"
echo "  Phrase:   \"$WAKE_PHRASE\""
echo "  Samples:  $NUM_SAMPLES"
echo "  Slug:     $SLUG"
echo "  Work dir: $WORK_DIR"
echo "============================================"
echo ""

# ── Step 1: Check GPU ────────────────────────────────────────────────────────

echo "[1/7] Checking GPU..."
if ! nvidia-smi > /dev/null 2>&1; then
    echo "ERROR: nvidia-smi not found. Make sure CUDA drivers are installed."
    echo "  On WSL2, the Windows NVIDIA driver handles this automatically."
    exit 1
fi
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
echo "  GPU: $GPU_NAME"

# ── Step 2: Install system dependencies ──────────────────────────────────────

echo ""
echo "[2/7] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip ffmpeg sox libsox-dev > /dev/null 2>&1
echo "  Done."

# ── Step 3: Set up virtual environment ───────────────────────────────────────

echo ""
echo "[3/7] Setting up Python virtual environment..."
mkdir -p "$WORK_DIR"

if [ ! -d "$WORK_DIR/venv" ]; then
    python3 -m venv "$WORK_DIR/venv"
    echo "  Created venv at $WORK_DIR/venv"
else
    echo "  Using existing venv at $WORK_DIR/venv"
fi

source "$WORK_DIR/venv/bin/activate"

# Install dependencies
echo "  Installing Python packages (this may take a few minutes on first run)..."
pip install --quiet --upgrade pip

# PyTorch with CUDA
pip install --quiet torch torchaudio --index-url https://download.pytorch.org/whl/cu121

# OpenWakeWord and training dependencies
pip install --quiet openwakeword

# Piper sample generator
if [ ! -d "$WORK_DIR/piper-sample-generator" ]; then
    echo "  Cloning piper-sample-generator..."
    cd "$WORK_DIR"
    git clone --quiet https://github.com/rhasspy/piper-sample-generator.git
    cd piper-sample-generator
    pip install --quiet -e .
else
    echo "  Using existing piper-sample-generator"
    cd "$WORK_DIR/piper-sample-generator"
fi

# Download a Piper voice model if not present
VOICE_DIR="$WORK_DIR/piper-sample-generator/voices"
mkdir -p "$VOICE_DIR"
VOICE_MODEL="$VOICE_DIR/en_US-lessac-medium.onnx"
VOICE_JSON="$VOICE_DIR/en_US-lessac-medium.onnx.json"

if [ ! -f "$VOICE_MODEL" ]; then
    echo "  Downloading Piper voice model (en_US-lessac-medium)..."
    wget -q -O "$VOICE_MODEL" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx"
    wget -q -O "$VOICE_JSON" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json"
    echo "  Downloaded."
else
    echo "  Voice model already present."
fi

echo "  Environment ready."

# ── Step 4: Generate synthetic samples ───────────────────────────────────────

echo ""
echo "[4/7] Generating $NUM_SAMPLES synthetic samples with Piper TTS..."
echo "  This will take a few minutes depending on your GPU..."

PHRASE_SAMPLES_DIR="$SAMPLES_DIR/$SLUG"
mkdir -p "$PHRASE_SAMPLES_DIR"

cd "$WORK_DIR/piper-sample-generator"
python3 generate_samples.py "$WAKE_PHRASE" \
    --model "$VOICE_MODEL" \
    --max-samples "$NUM_SAMPLES" \
    --output-dir "$PHRASE_SAMPLES_DIR" \
    2>&1 | tail -5

SAMPLE_COUNT=$(find "$PHRASE_SAMPLES_DIR" -name "*.wav" | wc -l)
echo "  Generated $SAMPLE_COUNT samples in $PHRASE_SAMPLES_DIR"

# ── Step 5: Augment samples ─────────────────────────────────────────────────

echo ""
echo "[5/7] Augmenting samples (room acoustics, volume variation)..."

PHRASE_AUGMENTED_DIR="$AUGMENTED_DIR/$SLUG"
mkdir -p "$PHRASE_AUGMENTED_DIR"

# Use the augment script if available, otherwise use basic sox augmentation
if [ -f "$WORK_DIR/piper-sample-generator/augment.py" ]; then
    python3 augment.py \
        --sample-rate 16000 \
        "$PHRASE_SAMPLES_DIR" \
        "$PHRASE_AUGMENTED_DIR" \
        2>&1 | tail -3
else
    echo "  augment.py not found, using basic augmentation with sox..."
    for wav in "$PHRASE_SAMPLES_DIR"/*.wav; do
        base=$(basename "$wav")
        # Original copy
        cp "$wav" "$PHRASE_AUGMENTED_DIR/$base"
        # Volume variation
        sox "$wav" "$PHRASE_AUGMENTED_DIR/vol_${base}" vol $(python3 -c "import random; print(f'{random.uniform(0.5, 1.5):.2f}')") 2>/dev/null || true
        # Speed variation
        sox "$wav" "$PHRASE_AUGMENTED_DIR/spd_${base}" speed $(python3 -c "import random; print(f'{random.uniform(0.9, 1.1):.2f}')") rate 16000 2>/dev/null || true
    done
fi

AUG_COUNT=$(find "$PHRASE_AUGMENTED_DIR" -name "*.wav" | wc -l)
echo "  Augmented to $AUG_COUNT samples in $PHRASE_AUGMENTED_DIR"

# ── Step 6: Train the model ─────────────────────────────────────────────────

echo ""
echo "[6/7] Training wake word model..."
echo "  This typically takes 30-60 minutes on a workstation NVIDIA GPU."
echo ""

mkdir -p "$OUTPUT_DIR"

# Create training config YAML
TRAIN_CONFIG="$WORK_DIR/${SLUG}_config.yaml"
cat > "$TRAIN_CONFIG" <<YAML
target_phrase: "$WAKE_PHRASE"
model_name: "$SLUG"

# Positive samples
positive_samples_directory: "$PHRASE_AUGMENTED_DIR"

# Training parameters
n_epochs: 100
batch_size: 64
learning_rate: 0.001
max_steps: 50000
target_accuracy: 0.95
target_false_positive_rate: 0.2

# Output
output_dir: "$OUTPUT_DIR"
YAML

echo "  Config written to: $TRAIN_CONFIG"
echo ""

# Try the openwakeword training script
if python3 -c "from openwakeword import train" 2>/dev/null; then
    python3 -m openwakeword.train \
        --training_config "$TRAIN_CONFIG" \
        2>&1 | tee "$WORK_DIR/training_log.txt"
else
    echo "  openwakeword.train module not directly available."
    echo "  Cloning openWakeWord repo for training scripts..."

    if [ ! -d "$WORK_DIR/openWakeWord" ]; then
        cd "$WORK_DIR"
        git clone --quiet https://github.com/dscripka/openWakeWord.git
    fi

    cd "$WORK_DIR/openWakeWord"
    pip install --quiet -e . 2>/dev/null || true

    python3 openwakeword/train.py \
        --training_config "$TRAIN_CONFIG" \
        --generate_clips \
        2>&1 | tee "$WORK_DIR/training_log.txt"
fi

# ── Step 7: Report results ──────────────────────────────────────────────────

echo ""
echo "============================================"
echo "  Training Complete!"
echo "============================================"

# Find output model files
TFLITE_FILE=$(find "$OUTPUT_DIR" -name "*.tflite" -type f 2>/dev/null | head -1)
ONNX_FILE=$(find "$OUTPUT_DIR" -name "*.onnx" -type f 2>/dev/null | head -1)

if [ -n "$TFLITE_FILE" ]; then
    TFLITE_SIZE=$(du -h "$TFLITE_FILE" | cut -f1)
    echo "  TFLite model: $TFLITE_FILE ($TFLITE_SIZE)"
    echo ""
    echo "  To deploy to Home Assistant:"
    echo "    1. Copy $TFLITE_FILE to your HA machine"
    echo "    2. Place it in /share/openwakeword/"
    echo "    3. Settings > Voice Assistants > Wake Word > select '$SLUG'"
    echo ""

    # Optional second copy, e.g. a Windows folder under /mnt/c when training
    # in WSL2. Set PUFFPAI_WAKE_COPY_DIR to enable it.
    COPY_DIR="${PUFFPAI_WAKE_COPY_DIR:-}"
    if [ -n "$COPY_DIR" ]; then
        mkdir -p "$COPY_DIR" 2>/dev/null || true
        if cp "$TFLITE_FILE" "$COPY_DIR/${SLUG}.tflite" 2>/dev/null; then
            echo "  Also copied to: $COPY_DIR/${SLUG}.tflite"
        fi
    fi
else
    echo "  WARNING: No .tflite file found in $OUTPUT_DIR"
    echo "  Check the training log: $WORK_DIR/training_log.txt"
    echo ""
    echo "  Output directory contents:"
    ls -la "$OUTPUT_DIR/" 2>/dev/null || echo "  (empty)"
fi

if [ -n "$ONNX_FILE" ]; then
    ONNX_SIZE=$(du -h "$ONNX_FILE" | cut -f1)
    echo "  ONNX model:   $ONNX_FILE ($ONNX_SIZE)"
fi

echo ""
echo "  Training log: $WORK_DIR/training_log.txt"
echo "  Samples dir:  $PHRASE_SAMPLES_DIR"
echo "============================================"
