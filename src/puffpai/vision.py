"""
PuffPAI local vision-model image understanding.

Escalation target for messy / handwritten / angled / photographed documents
(schedules, recipes, notes) that plain tesseract OCR reads poorly. The image is
downscaled, JPEG+base64 encoded, and sent to a local Ollama vision model
(qwen2.5vl) via /api/chat; the model transcribes the visible text.

Deliberately dependency-light (PIL, base64, io, requests) and it never imports
api_server — so file_processor's caller can inject this without a circular
import. Nothing here raises: every failure path returns ("", {..error}).

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

import requests

from puffpai.inference import model_options as _model_options

logger = logging.getLogger(__name__)

# HEIC/HEIF (iPhone photos): pillow-heif registers an opener into PIL globally.
# file_processor already does this at import, but register defensively here too
# so this module opens HEIC even when used standalone.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

# Longest-side cap keeps the upload (and therefore latency + VRAM) bounded while
# staying large enough to read small handwriting on a phone photo.
_MAX_SIDE = 1600
_JPEG_QUALITY = 90

_EXTRACTION_PROMPT = (
    "You are a precise OCR and document-understanding engine. Transcribe ALL "
    "text visible in this image exactly as written, preserving structure. Keep "
    "headings, dates, times, tables, and lists intact — one item per line. If "
    "it is a schedule, put each event on its own line with its date, time, and "
    "location. If it is a recipe, output the title, then an 'Ingredients:' "
    "list, then a 'Steps:' list. Do not summarize, explain, or add commentary "
    "— output only the transcribed text."
)


def _encode_image(image_path: str) -> str:
    """Open, RGB-convert, downscale, and JPEG+base64 encode an image.

    Returns a base64 str with NO ``data:`` URL prefix (what Ollama expects).
    """
    from PIL import Image

    with Image.open(image_path) as img:
        img = img.convert("RGB")
        longest = max(img.width, img.height)
        if longest > _MAX_SIDE:
            scale = _MAX_SIDE / float(longest)
            img = img.resize(
                (max(1, round(img.width * scale)),
                 max(1, round(img.height * scale))),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def extract_text_from_image(
    image_path: str,
    *,
    ollama_url: str,
    model: str,
    timeout: int = 180,
) -> tuple[str, dict[str, Any]]:
    """Transcribe an image with a local Ollama vision model.

    Returns ``(text, meta)``. ``meta`` always carries ``extractor="qwen_vl"``
    and ``model``. Never raises: any exception / timeout / non-200 yields
    ``("", {"extractor": "qwen_vl", "model": model, "error": ...})``.
    """
    try:
        b64 = _encode_image(image_path)
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": _EXTRACTION_PROMPT,
                "images": [b64],
            }],
            "stream": False,
            # On-demand pin, NOT the resident one: the vision model is a
            # 5.97 GB blob used only when an image needs OCR escalation, so
            # holding it forever would spend a tenth of the box's RAM on a
            # path that may not run for days. Deliberate, named exception —
            # see model_options.keep_alive_on_demand.
            "keep_alive": _model_options.keep_alive_on_demand(),
            "options": {"temperature": 0.1},
        }
        resp = requests.post(
            f"{ollama_url}/api/chat", json=payload, timeout=timeout)
        if resp.status_code != 200:
            return "", {
                "extractor": "qwen_vl",
                "model": model,
                "error": f"ollama /api/chat returned {resp.status_code}",
            }
        text = resp.json()["message"]["content"]
        return text.strip(), {"extractor": "qwen_vl", "model": model}
    except Exception as e:  # timeout, connection, JSON/KeyError — never raise
        logger.warning("vision extract failed for %s: %s", image_path, e)
        return "", {"extractor": "qwen_vl", "model": model, "error": str(e)}
