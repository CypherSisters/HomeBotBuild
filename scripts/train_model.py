#!/usr/bin/env python3
"""
PuffPAI Wake Word Model Trainer
Trains a TFLite wake word model from augmented audio samples.
"""
import os
import sys
import glob
import numpy as np
import random
import wave
import struct

print("=== PuffPAI Wake Word Model Training ===")
print()

SAMPLE_DIR = os.path.expanduser("~/openwakeword-training/augmented/hey_puffpai")
OUTPUT_DIR = os.path.expanduser("~/openwakeword-training/output")
# Optional second copy of the model, e.g. a Windows folder under /mnt/c when
# training in WSL2. Unset = no copy.
COPY_DIR = os.environ.get("PUFFPAI_WAKE_COPY_DIR", "")
MAX_LEN = 16000  # 1 second at 16kHz
EPOCHS = 100
BATCH_SIZE = 64
PATIENCE = 10

# ── Install TensorFlow if needed ─────────────────────────────────────────────

try:
    import tensorflow as tf
except ImportError:
    print("Installing tensorflow-cpu...")
    os.system(f"{sys.executable} -m pip install --quiet tensorflow-cpu")
    import tensorflow as tf

print(f"TensorFlow {tf.__version__}")


# ── Load audio samples ───────────────────────────────────────────────────────

def load_wav(path, max_len=MAX_LEN):
    """Load WAV file as normalized numpy array, pad/truncate to max_len."""
    with wave.open(path, "rb") as wf:
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)
        samples = np.array(
            struct.unpack(f"{n_frames}h", raw), dtype=np.float32
        )
        samples = samples / 32768.0

        if len(samples) > max_len:
            samples = samples[:max_len]
        elif len(samples) < max_len:
            samples = np.pad(samples, (0, max_len - len(samples)))

        return samples


wav_files = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.wav")))
print(f"Loading {len(wav_files)} augmented samples...")

positives = []
for wf in wav_files:
    try:
        positives.append(load_wav(wf))
    except Exception:
        continue

positives = np.array(positives)
print(f"Loaded {len(positives)} positive samples, shape: {positives.shape}")


# ── Generate negative samples ────────────────────────────────────────────────

n_neg = len(positives)
negatives = []
for _ in range(n_neg):
    noise_type = random.choice(["silence", "white", "pink"])
    if noise_type == "silence":
        neg = np.random.randn(MAX_LEN).astype(np.float32) * 0.001
    elif noise_type == "white":
        neg = np.random.randn(MAX_LEN).astype(np.float32) * random.uniform(
            0.01, 0.1
        )
    else:
        neg = np.cumsum(np.random.randn(MAX_LEN).astype(np.float32))
        neg = neg / (np.max(np.abs(neg)) + 1e-8) * random.uniform(0.01, 0.1)
    negatives.append(neg)

negatives = np.array(negatives)
print(f"Generated {len(negatives)} negative samples")


# ── Prepare dataset ──────────────────────────────────────────────────────────

X = np.concatenate([positives, negatives], axis=0)
y = np.concatenate([np.ones(len(positives)), np.zeros(len(negatives))], axis=0)

indices = np.arange(len(X))
np.random.shuffle(indices)
X = X[indices]
y = y[indices]

X = X.reshape(-1, MAX_LEN, 1)

split = int(0.85 * len(X))
X_train, X_val = X[:split], X[split:]
y_train, y_val = y[:split], y[split:]

print(f"Train: {len(X_train)}, Val: {len(X_val)}")
print()


# ── Build model ──────────────────────────────────────────────────────────────

model = tf.keras.Sequential(
    [
        tf.keras.layers.Input(shape=(MAX_LEN, 1)),
        tf.keras.layers.Conv1D(16, 64, strides=4, activation="relu"),
        tf.keras.layers.MaxPooling1D(4),
        tf.keras.layers.Conv1D(32, 32, strides=2, activation="relu"),
        tf.keras.layers.MaxPooling1D(4),
        tf.keras.layers.Conv1D(64, 16, strides=2, activation="relu"),
        tf.keras.layers.GlobalAveragePooling1D(),
        tf.keras.layers.Dense(64, activation="relu"),
        tf.keras.layers.Dropout(0.3),
        tf.keras.layers.Dense(1, activation="sigmoid"),
    ]
)

model.compile(
    optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
    loss="binary_crossentropy",
    metrics=["accuracy"],
)

model.summary()
print()


# ── Train ────────────────────────────────────────────────────────────────────

print("Training...")
early_stop = tf.keras.callbacks.EarlyStopping(
    monitor="val_accuracy", patience=PATIENCE, restore_best_weights=True
)

history = model.fit(
    X_train,
    y_train,
    validation_data=(X_val, y_val),
    epochs=EPOCHS,
    batch_size=BATCH_SIZE,
    callbacks=[early_stop],
    verbose=1,
)

val_acc = max(history.history["val_accuracy"])
print(f"\nBest validation accuracy: {val_acc:.4f}")


# ── Export to TFLite ─────────────────────────────────────────────────────────

os.makedirs(OUTPUT_DIR, exist_ok=True)

converter = tf.lite.TFLiteConverter.from_keras_model(model)
converter.optimizations = [tf.lite.Optimize.DEFAULT]
tflite_model = converter.convert()

tflite_path = os.path.join(OUTPUT_DIR, "hey_puffpai.tflite")
with open(tflite_path, "wb") as f:
    f.write(tflite_model)

size_kb = os.path.getsize(tflite_path) / 1024
print(f"Saved TFLite model: {tflite_path} ({size_kb:.0f} KB)")

keras_path = os.path.join(OUTPUT_DIR, "hey_puffpai.keras")
model.save(keras_path)
print(f"Saved Keras model: {keras_path}")

if COPY_DIR:
    os.makedirs(COPY_DIR, exist_ok=True)
    import shutil

    shutil.copy2(tflite_path, os.path.join(COPY_DIR, "hey_puffpai.tflite"))
    print(f"Copied to: {COPY_DIR}/hey_puffpai.tflite")

print()
print("TRAINING COMPLETE")
