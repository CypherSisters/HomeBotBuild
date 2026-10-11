"""
PuffPAI Document & Image Ingestion
====================================
Handles uploads of recipes (photos), school flyers (images/PDFs),
study material (PDFs), schedules (images), and other personal documents.

Pipeline:
    1. Detect file type (image / pdf / docx / txt / xlsx)
    2. Extract raw text via the appropriate engine:
        - PDFs → pdfplumber (text layer) or pytesseract (scanned)
        - Images → pytesseract OCR
        - DOCX → python-docx
        - TXT  → utf-8 read
        - XLSX → openpyxl
    3. Use the local LLM to classify/parse the extracted text into a
       structured payload (recipe, event, study notes, etc.)
    4. Route the payload to the right app data store
    5. ALSO chunk + embed the raw text into the `user_uploads` ChromaDB
       collection so the unified RAG pipeline can find it semantically

This module is designed for OFFLINE use. All OCR + LLM happens locally.

Architecture note:
    Every uploaded file becomes part of PuffPAI's "brain" via ChromaDB.
    Structured data (recipes → FreshTable, events → Calendar) is a
    fast-access projection. The vector DB is the source of truth for
    semantic retrieval and is queried by both _get_rag_context() (RAG
    path) and search_my_documents (agent path).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from puffpai.inference import model_options as _model_options
from puffpai.statedir import resolve_runtime_dir

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# No _UPLOAD_DIR.mkdir() at module scope. On the appliance the code tree is
# mounted read-only (ProtectSystem=strict), so such a line would turn a plain
# `import puffpai.ingestion` into EROFS, the whole module, and every route
# that imports it, dies before anything is even uploaded. The directory is
# created at the write site, where a failure is honestly the upload's failure
# and not the import's.
#
# Both names below are override hooks for tests/ops: set one to a path and the
# accessor hands it back verbatim (tests/test_vault_encryption.py redirects
# _DOC_INDEX this way). None means "resolve", and with $PUFFPAI_DATA_DIR unset
# that resolves to the historical in-tree paths.
_UPLOAD_DIR = None
_DOC_INDEX = None


def _upload_dir() -> Path:
    """Directory holding uploaded files. Resolved per call, never at import.

    ``env_subdir`` gives uploads their own subtree of a relocated data dir
    ($PUFFPAI_DATA_DIR/uploads) while the unset-env default stays exactly
    <repo>/data/uploads.
    """
    if _UPLOAD_DIR:
        return Path(_UPLOAD_DIR)
    return Path(resolve_runtime_dir(
        str(_REPO_ROOT / "data" / "uploads"), env_subdir="uploads"))


def _doc_index() -> Path:
    """The JSON metadata catalog of extracted text — same hook-then-resolve.

    No ``env_subdir``: the catalog is a single file, so it lands directly in
    the state root rather than earning a directory of its own.
    """
    if _DOC_INDEX:
        return Path(_DOC_INDEX)
    return Path(resolve_runtime_dir(
        str(_REPO_ROOT / "data"))) / "ingested_docs.json"


# ── File-type detection ───────────────────────────────────────────────────────

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"})
PDF_EXTS = frozenset({".pdf"})
DOC_EXTS = frozenset({".docx", ".doc"})
TEXT_EXTS = frozenset({".txt", ".md", ".markdown"})
SHEET_EXTS = frozenset({".xlsx", ".xls", ".csv"})


def detect_file_type(filename: str) -> str:
    """Return one of: 'image', 'pdf', 'docx', 'txt', 'xlsx', 'unknown'."""
    ext = os.path.splitext(filename.lower())[1]
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in DOC_EXTS:
        return "docx"
    if ext in TEXT_EXTS:
        return "txt"
    if ext in SHEET_EXTS:
        return "xlsx"
    return "unknown"


# ── OCR detection (lazy) ──────────────────────────────────────────────────────

_ocr_available: bool | None = None
_ocr_check_done = False


def _check_ocr_available() -> bool:
    """Detect whether Tesseract is installed. Cache the result."""
    global _ocr_available, _ocr_check_done
    if _ocr_check_done:
        return bool(_ocr_available)
    _ocr_check_done = True
    try:
        import pytesseract
        # PATH first — this is the Linux/appliance path (`apt install
        # tesseract-ocr` puts it at /usr/bin/tesseract) and it also wins on a
        # Windows box where tesseract was added to PATH. The hardcoded
        # Windows install locations below are the fallback for the laptop,
        # where the installer does not touch PATH by default.
        common_paths = [
            shutil.which("tesseract") or "",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
        ]
        for path in common_paths:
            if path and os.path.exists(path):
                pytesseract.pytesseract.tesseract_cmd = path
                _ocr_available = True
                logger.info("Tesseract OCR found at: %s", path)
                return True
        # Try plain command (in case it's on PATH)
        try:
            pytesseract.get_tesseract_version()
            _ocr_available = True
            return True
        except Exception:
            pass
    except ImportError:
        pass
    _ocr_available = False
    logger.info("Tesseract OCR not available — image OCR disabled")
    return False


# ── Text extraction engines ───────────────────────────────────────────────────

def extract_text_from_image(file_path: str) -> str:
    """OCR an image file with handwriting-friendly settings.

    Strategy:
      1. Open the image and upscale 2× — handwriting OCR works much
         better on larger inputs.
      2. Convert to grayscale and boost contrast slightly.
      3. Use PSM 6 (uniform block of text) which works best for recipes,
         flyers, and any document with structured text rows.
      4. Try multiple PSM modes and pick the result with the most words.

    Returns empty string if OCR is unavailable.
    """
    if not _check_ocr_available():
        return "[OCR NOT AVAILABLE — install Tesseract OCR to enable image text extraction]"
    try:
        import pytesseract
        from PIL import Image, ImageOps, ImageEnhance

        original = Image.open(file_path)

        # Upscale 2× — handwriting OCR is way more accurate on larger images
        upscaled = original.resize(
            (original.width * 2, original.height * 2),
            Image.LANCZOS,
        )

        # Light grayscale + contrast boost helps faded handwriting
        grayscale = ImageOps.grayscale(upscaled)
        enhanced = ImageEnhance.Contrast(grayscale).enhance(1.4)

        # Try multiple page-segmentation modes and keep the best result.
        # PSM 6 (uniform block) works best for recipes and structured docs.
        # PSM 11 (sparse text) works better for scattered/handwritten text.
        # PSM 3 (default fully-automatic) is the catch-all.
        candidates = []
        for psm in (6, 11, 3):
            try:
                text = pytesseract.image_to_string(
                    enhanced,
                    config=f"--psm {psm}",
                ).strip()
                candidates.append((psm, text, len(text.split())))
            except Exception:
                continue

        if not candidates:
            return ""

        # Pick the result with the most words (usually highest-quality OCR)
        candidates.sort(key=lambda c: c[2], reverse=True)
        best_psm, best_text, best_words = candidates[0]
        logger.info("OCR for %s: psm=%d words=%d", file_path, best_psm, best_words)
        return best_text
    except Exception as e:
        logger.error("OCR failed for %s: %s", file_path, e)
        return f"[OCR error: {e}]"


def extract_text_from_pdf(file_path: str) -> str:
    """Extract text from PDF. Tries pdfplumber first (for text-layer PDFs),
    then falls back to OCR each page if no text was found."""
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            pages = []
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                pages.append(page_text)
            text = "\n\n".join(pages).strip()
    except Exception as e:
        logger.warning("pdfplumber failed for %s: %s", file_path, e)

    # If we got nothing, try OCR (scanned PDF)
    if len(text) < 50 and _check_ocr_available():
        try:
            import pdfplumber
            import pytesseract
            from PIL import Image
            import io
            ocr_pages = []
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    img = page.to_image(resolution=200).original
                    page_text = pytesseract.image_to_string(img)
                    ocr_pages.append(page_text)
            ocr_text = "\n\n".join(ocr_pages).strip()
            if len(ocr_text) > len(text):
                text = ocr_text
        except Exception as e:
            logger.warning("PDF OCR fallback failed: %s", e)

    return text


def extract_text_from_docx(file_path: str) -> str:
    """Extract text from a Word document."""
    try:
        from docx import Document
        doc = Document(file_path)
        paras = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paras)
    except ImportError:
        return "[python-docx not installed — cannot read .docx files]"
    except Exception as e:
        return f"[docx read error: {e}]"


def extract_text_from_txt(file_path: str) -> str:
    """Read a plain text file."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"[text read error: {e}]"


def extract_text_from_xlsx(file_path: str) -> str:
    """Extract a flat string representation of all sheets in a workbook."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_path, data_only=True)
        out = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            out.append(f"### Sheet: {sheet_name}")
            for row in ws.iter_rows(values_only=True):
                vals = [str(v) if v is not None else "" for v in row]
                if any(v.strip() for v in vals):
                    out.append("\t".join(vals))
        return "\n".join(out)
    except ImportError:
        return "[openpyxl not installed — cannot read .xlsx files]"
    except Exception as e:
        return f"[xlsx read error: {e}]"


def extract_text(file_path: str) -> tuple[str, str]:
    """Detect file type and extract text. Returns (file_type, extracted_text)."""
    ftype = detect_file_type(file_path)
    if ftype == "image":
        return ftype, extract_text_from_image(file_path)
    if ftype == "pdf":
        return ftype, extract_text_from_pdf(file_path)
    if ftype == "docx":
        return ftype, extract_text_from_docx(file_path)
    if ftype == "txt":
        return ftype, extract_text_from_txt(file_path)
    if ftype == "xlsx":
        return ftype, extract_text_from_xlsx(file_path)
    return ftype, "[unknown file type]"


# Markers used by the extractors above when extraction fails (missing parser
# library, read error, OCR error, unknown type). Each extractor returns a
# SINGLE bracketed placeholder string on failure; that placeholder must NEVER
# be indexed as if it were real document content.
_EXTRACTION_FAILURE_MARKERS = (
    "not installed",
    "read error",
    "ocr error",
    "unknown file type",
    "cannot read",
)


def _extraction_failure_reason(text: str) -> str | None:
    """Return a user-facing reason if `text` is an extractor error placeholder
    (e.g. '[python-docx not installed — cannot read .docx files]'), else None."""
    s = (text or "").strip()
    if s.startswith("[") and s.endswith("]") and len(s) < 200:
        low = s.lower()
        if any(m in low for m in _EXTRACTION_FAILURE_MARKERS):
            return s.strip("[]").strip()
    return None


# ── LLM-driven classification & parsing ───────────────────────────────────────

def _llm_chat(prompt: str, max_tokens: int = 600, temperature: float = 0.1) -> str:
    """Send a prompt to the local Ollama LLM and return the response."""
    try:
        import requests
        from puffpai.config import get_config
        cfg = get_config()
        request = {
            "model": cfg.ollama.model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            # num_ctx and the pin come from the shared resolver; without
            # num_ctx ollama sizes the runner from the model's trained
            # context and starts a second copy of the model.
            "keep_alive": _model_options.keep_alive(),
            "options": _model_options.chat_options(
                temperature=temperature, num_predict_override=max_tokens),
        }
        # A thinking model otherwise spends its whole token budget reasoning
        # and returns EMPTY content. The lever is per family (qwen3 boolean,
        # gpt-oss effort level), so it comes from the shared resolver.
        request.update(_model_options.thinking_control(cfg.ollama.model))
        resp = requests.post(
            f"{cfg.ollama.url}/api/chat",
            json=request,
            timeout=120,
        )
        return resp.json().get("message", {}).get("content", "")
    except Exception as e:
        logger.error("LLM chat failed: %s", e)
        return ""


def classify_document(text: str) -> str:
    """Use the LLM to classify what KIND of document this is.

    Returns one of: 'recipe', 'event', 'schedule', 'study', 'shopping_list',
                    'note', 'unknown'.
    """
    if not text or len(text.strip()) < 10:
        return "unknown"

    snippet = text[:1500]  # Don't waste tokens on a huge doc
    prompt = (
        "Classify this document into ONE of these categories. "
        "Respond with ONLY the single category name, nothing else.\n\n"
        "Categories:\n"
        "- recipe (cooking instructions with ingredients)\n"
        "- event (a single dated event like a concert, play, appointment)\n"
        "- schedule (a multi-day calendar/agenda)\n"
        "- study (textbook, lecture notes, study guide)\n"
        "- shopping_list (a list of items to buy)\n"
        "- note (general notes/memo)\n"
        "- unknown (none of the above)\n\n"
        f"Document text:\n---\n{snippet}\n---\n\n"
        "Category:"
    )
    response = _llm_chat(prompt, max_tokens=20, temperature=0.0)
    response = response.strip().lower()
    for cat in ("recipe", "event", "schedule", "study", "shopping_list", "note"):
        if cat in response:
            return cat
    return "unknown"


def parse_recipe(text: str) -> dict | None:
    """Use the LLM to extract a structured recipe from raw text.

    CRITICAL: This is for OCR'd handwriting which is often noisy. The LLM
    is instructed to ONLY copy text that is actually present, never to
    invent or correct ingredients. If the text is too messy, returns the
    raw text as the instructions field rather than risking hallucination.
    """
    snippet = text[:3000]
    prompt = (
        "You are extracting a recipe from raw OCR'd text that may contain "
        "errors from scanning handwritten or photographed paper. Your job is "
        "to ONLY transcribe what is literally in the text — NEVER invent or "
        "correct ingredients, quantities, or steps. If a quantity is unclear, "
        "include it as-is (with a [?]) rather than guessing.\n\n"
        "RULES:\n"
        "- Copy ingredient lines verbatim from the text. Don't change '1 tap' "
        "to '1 cup' even if you think that's what was meant.\n"
        "- Don't invent ingredients that aren't in the text.\n"
        "- Don't add 'salt to taste' or any 'standard' steps.\n"
        "- Don't normalize units or fix typos.\n"
        "- If the text is mostly garbled, leave INSTRUCTIONS empty and put "
        "the raw cleaned text in INGREDIENTS.\n\n"
        "Respond in EXACTLY this format:\n"
        "NAME: <recipe name as written, or 'Untitled Recipe'>\n"
        "INGREDIENTS: <one ingredient per line, copied verbatim>\n"
        "INSTRUCTIONS: <only the literal cooking steps from the text, or empty>\n\n"
        f"Text:\n---\n{snippet}\n---"
    )
    response = _llm_chat(prompt, max_tokens=800, temperature=0.0)
    if not response:
        return None
    name_m = re.search(r"NAME:\s*(.+?)(?:\n|$)", response)
    ing_m = re.search(r"INGREDIENTS:\s*(.+?)(?:\nINSTRUCTIONS:|$)", response, re.DOTALL)
    instr_m = re.search(r"INSTRUCTIONS:\s*(.+?)$", response, re.DOTALL)
    if not name_m:
        return None
    return {
        "name": name_m.group(1).strip(),
        "ingredients": ing_m.group(1).strip() if ing_m else "",
        "instructions": instr_m.group(1).strip() if instr_m else "",
        # Always preserve the raw OCR text so the user can verify
        "raw_text": text[:2000],
    }


def parse_event(text: str) -> dict | None:
    """Extract a calendar event (title, date, time, location) from text."""
    snippet = text[:2000]
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        f"Today is {today}. Extract ONE calendar event from this text. "
        f"Use ISO 8601 for the date (YYYY-MM-DDTHH:MM). If the year is "
        f"missing, assume current year ({datetime.now().year}). "
        f"If no time is given, use 09:00.\n\n"
        f"Respond in EXACTLY this format:\n"
        f"TITLE: <event name>\n"
        f"START: <YYYY-MM-DDTHH:MM>\n"
        f"LOCATION: <location or empty>\n"
        f"DESCRIPTION: <short description or empty>\n\n"
        f"Text:\n---\n{snippet}\n---"
    )
    response = _llm_chat(prompt, max_tokens=300, temperature=0.0)
    if not response:
        return None
    title_m = re.search(r"TITLE:\s*(.+?)(?:\n|$)", response)
    start_m = re.search(r"START:\s*(.+?)(?:\n|$)", response)
    loc_m = re.search(r"LOCATION:\s*(.+?)(?:\n|$)", response)
    desc_m = re.search(r"DESCRIPTION:\s*(.+?)(?:\n|$)", response)
    if not title_m or not start_m:
        return None
    return {
        "title": title_m.group(1).strip(),
        "start": start_m.group(1).strip(),
        "location": loc_m.group(1).strip() if loc_m else "",
        "description": desc_m.group(1).strip() if desc_m else "",
    }


def parse_schedule(text: str) -> list[dict]:
    """Extract MULTIPLE events from a schedule document."""
    snippet = text[:3500]
    today = datetime.now().strftime("%Y-%m-%d")
    prompt = (
        f"Today is {today}. Extract ALL calendar events from this schedule. "
        f"Use ISO 8601 dates (YYYY-MM-DDTHH:MM). If year missing, assume "
        f"{datetime.now().year}.\n\n"
        f"Respond as JSON array. Each event has: title, start, location.\n"
        f"Example: [{{\"title\":\"Math Class\",\"start\":\"2026-04-10T09:00\",\"location\":\"Room 101\"}}]\n\n"
        f"Text:\n---\n{snippet}\n---\n\nJSON:"
    )
    response = _llm_chat(prompt, max_tokens=800, temperature=0.0)
    if not response:
        return []
    # Extract JSON array from response
    try:
        match = re.search(r"\[.*?\]", response, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except Exception as e:
        logger.warning("Schedule JSON parse failed: %s", e)
    return []


# ── Main ingestion entry points ───────────────────────────────────────────────

@dataclass
class IngestionResult:
    file_id: str
    filename: str
    file_type: str
    classification: str
    text_length: int
    structured_data: dict | None = None
    actions_taken: list[str] = field(default_factory=list)
    error: str | None = None
    pii_detected: bool = False
    pii_summary: str | None = None
    vaulted: bool = False

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "file_type": self.file_type,
            "classification": self.classification,
            "text_length": self.text_length,
            "structured_data": self.structured_data,
            "actions_taken": self.actions_taken,
            "error": self.error,
            "pii_detected": self.pii_detected,
            "pii_summary": self.pii_summary,
            "vaulted": self.vaulted,
        }


def save_uploaded_file(file_bytes: bytes, original_filename: str) -> str:
    """Persist an uploaded file to disk and return its full path."""
    safe_name = re.sub(r"[^\w\.\-]", "_", original_filename)
    timestamp = int(time.time() * 1000)
    file_id = f"{timestamp}_{safe_name}"
    target = _upload_dir() / file_id
    # Replaces the old import-time mkdir: create it here, on the write path.
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(file_bytes)
    return str(target)


def _resolve_shared_vault():
    """Return the process-wide Vault singleton that holds live sessions.

    Document/PII vaulting must use the SAME Vault instance the API server
    unlocked, because sessions (and their in-memory encryption keys) are
    instance-scoped — a fresh ``Vault()`` would see no live session and could
    never encrypt. Falls back to a fresh Vault only if the app server isn't
    importable; that instance has no sessions, so vault writes fail closed
    (locked) rather than writing plaintext.
    """
    try:
        from puffpai.api_server import _get_vault
        v = _get_vault()
        if v is not None:
            return v
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Shared vault unavailable, using fresh instance: %s", e)
    from puffpai.security.vault import Vault
    return Vault()


# ─────────────────────────────────────────────────────────────────────
#  Shared PII redact-and-index gate (used by BOTH upload paths)
# ─────────────────────────────────────────────────────────────────────
#
# Product intent: when a document contains PII, the sensitive VALUES go to
# the encrypted Zone 1 Vault, and the REST of the document stays searchable
# via RAG. Concretely: a personal-info doc with a birthday → birthday vaulted,
# redacted document indexed and retrievable.
#
#   severity            outcome
#   ────────────────    ─────────────────────────────────────────────────────
#   none                pass through, index original text
#   medium (email/      pass through, index original text, NO vault
#     phone/name)       (redacting every name would destroy document utility)
#   high/critical       vault ORIGINAL text (encrypted) + append PII record,
#     (SSN/CC/addr/DOB) index REDACTED text
#
# Fail-closed invariants (never relaxed):
#   * vault uninitialized/locked while high/critical PII present  → refuse
#   * secure write fails                                          → refuse
#   * PII scan raises (classifier crash / unavailable)            → refuse
#   Refusal = raise PiiVaultRefusal; the caller must NOT index any text and
#   must remove the plaintext staging copy. Unscanned or unredacted-high-PII
#   text must never reach ChromaDB or the JSON catalogs.

# Human-readable labels for the actionable refusal message. Only the
# high/critical types can trigger a refusal, so only those are mapped.
_HIGH_CRITICAL_LABELS = {
    'ssn': 'SSN',
    'credit_card': 'credit card',
    'address': 'address',
    'date_of_birth': 'DOB',
}

_HIGH_CRITICAL_SEVERITIES = ('high', 'critical')


class PiiVaultRefusal(Exception):
    """Raised when a document contains high/critical PII but it cannot be
    securely vaulted (vault uninitialized/locked, or the secure write failed),
    OR when the PII scan itself could not run. Fail-closed contract: on this
    exception the caller MUST refuse the upload and index NOTHING. Carries an
    actionable, user-facing ``message`` (no PII values in it)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class PiiScreenResult:
    """Outcome of :func:`screen_text_for_pii`.

    ``text_for_index`` is the ONLY text a caller may index: redacted when the
    document held high/critical PII, the original otherwise.
    """
    text_for_index: str
    pii_detected: bool = False
    pii_vaulted: bool = False
    pii_types: list = field(default_factory=list)
    severity: str = "none"
    summary: str = "No PII detected."


def _high_critical_label_str(detection) -> str:
    """Comma-joined human labels for the high/critical PII types present, for
    the actionable refusal message (e.g. "DOB", "SSN, address"). Falls back to
    the severity word if no type maps."""
    labels = [
        _HIGH_CRITICAL_LABELS[t]
        for t in detection.pii_types
        if t in _HIGH_CRITICAL_LABELS
    ]
    return ', '.join(labels) if labels else detection.severity


def _vault_original_text(
    original_text: str,
    detection,
    *,
    filename: str,
    file_id: str,
    vault=None,
    source: str = "document_upload",
) -> None:
    """Encrypt-store ``original_text`` in the Zone 1 Vault and append a PII
    record. FAIL CLOSED: raises :class:`PiiVaultRefusal` if the vault is
    uninitialized/locked or the secure write fails. NEVER writes plaintext,
    NEVER logs PII values. Shared by the ingest path and the rescan endpoint so
    the vault-write + refusal semantics cannot drift apart.
    """
    active_vault = vault if vault is not None else _resolve_shared_vault()
    labels = _high_critical_label_str(detection)

    if not active_vault.is_initialized:
        logger.warning("Refusing PII content %s — vault not initialized", filename)
        raise PiiVaultRefusal(
            f"Document contains sensitive data ({labels}). Set up and unlock "
            f"the Zone 1 Vault, then re-upload."
        )

    stem = os.path.splitext(os.path.basename(filename))[0]
    vault_copy_name = f"doc_{file_id}_{stem}.txt"

    # ENCRYPTED write only. store_document_secure returns False when the vault
    # is locked and raises only on a genuine write error — both fail closed.
    try:
        stored = active_vault.store_document_secure(vault_copy_name, original_text)
    except Exception as e:  # genuine write error
        logger.warning(
            "Secure vaulting failed for %s — refusing (no plaintext written): %s",
            filename, e)
        raise PiiVaultRefusal(
            f"Document contains sensitive data ({labels}). Could not securely "
            f"store it. Unlock the vault and re-upload."
        ) from e

    if not stored:
        logger.warning(
            "Refusing PII content %s — vault locked (no plaintext written)",
            filename)
        raise PiiVaultRefusal(
            f"Document contains sensitive data ({labels}). Unlock the vault "
            f"and re-upload."
        )

    # Document is encrypted at rest. Append the index record (best-effort;
    # append_auto_pii never raises and quarantines a corrupt log). A missed log
    # entry is logged, never downgraded to a plaintext write.
    pii_record = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'filename': filename,
        'file_id': file_id,
        'severity': detection.severity,
        'pii_types': detection.pii_types,
        'summary': detection.summary(),
    }
    if not active_vault.append_auto_pii(pii_record):
        logger.warning(
            "PII log append skipped for %s — vault locked or log unwritable "
            "(document is encrypted; index entry missed)", filename)


def _scrub_uploads_medium_enabled() -> bool:
    """Whether MEDIUM-tier PII (email / phone / labeled name) in UPLOADED documents
    should also be scrubbed/vaulted. DEFAULT OFF, because redacting every name,
    email and phone in a document destroys the document's usefulness — a
    reference like "Dr. Smith recommends 500 mg" becomes "[REDACTED-NAME]
    recommends 500 mg" and the RAG index can no longer answer "what did my
    doctor recommend?". So by default the upload path preserves the current,
    readable medium behavior.

    The owner may set ``PUFFPAI_SCRUB_UPLOADS_MEDIUM`` (1/true/yes/on) to opt in
    to also scrubbing/vaulting medium-tier PII in docs. Only consulted when
    ``PUFFPAI_SCRUB_AND_VAULT`` is also on. High/critical PII in uploads is
    scrubbed/vaulted regardless of this flag.
    """
    return os.environ.get(
        "PUFFPAI_SCRUB_UPLOADS_MEDIUM", "").strip().lower() in (
        "1", "true", "yes", "on")


def _screen_via_scrub_and_vault(
    text: str,
    filename: str,
    *,
    file_id: str,
    vault,
    source: str,
) -> PiiScreenResult:
    """Route upload text through the SAME scrub-and-vault choke point the chat
    path uses (:func:`puffpai.security.scrub_and_vault.scrub_and_vault`).

    High/critical PII → GOOD values vaulted behind a ``{{vault:<ref-id>}}``
    handle via the Archivist quality gate, test/bad values → ``[REDACTED-*]``;
    the RAW value never lands in ``text_for_index``. Medium-tier is preserved by
    default unless :func:`_scrub_uploads_medium_enabled` is on. One gate,
    one match list, consistent handles across chat and uploads.

    Fail-closed: a classifier crash, or real high/critical PII that could not be
    secured because the vault was locked, both raise :class:`PiiVaultRefusal` so
    the caller indexes NOTHING (matching the legacy refusal contract).
    """
    from puffpai.security.scrub_and_vault import (
        scrub_and_vault, STATUS_CLASSIFIER_ERROR, STATUS_VAULT_LOCKED)

    include_tiers = {"critical", "high"}
    if _scrub_uploads_medium_enabled():
        include_tiers.add("medium")

    result = scrub_and_vault(
        text, source=source, vault=vault,
        include_tiers=frozenset(include_tiers))

    # Fail closed: the scan itself could not run — never index unscanned text.
    if result.status == STATUS_CLASSIFIER_ERROR:
        raise PiiVaultRefusal(
            "PII scan failed — the document was not indexed. No unscanned "
            "document is ever stored in plaintext. Try re-uploading.")

    # Fail closed: real, vault-worthy high/critical PII was present but the vault
    # was locked, so it was scrubbed to a plain [REDACTED-*] rather than vaulted.
    # The clean_text is already safe (no raw value), but we refuse anyway so the
    # sensitive value is not silently lost to an unrecoverable redaction — the
    # user unlocks the Zone 1 Vault and re-uploads, matching legacy behavior.
    if result.status == STATUS_VAULT_LOCKED:
        raise PiiVaultRefusal(
            "Document contains sensitive data that could not be secured. "
            "Unlock the Zone 1 Vault and re-upload.")

    return PiiScreenResult(
        text_for_index=result.clean_text,
        pii_detected=result.pii_detected,
        pii_vaulted=result.vaulted_count > 0,
        pii_types=result.pii_types,
        severity=result.severity,
        summary=result.pii_summary or "No PII detected.",
    )


def screen_text_for_pii(
    text: str,
    filename: str,
    *,
    file_id: str | None = None,
    vault=None,
    source: str = "document_upload",
) -> PiiScreenResult:
    """Scan ``text``; vault + redact high/critical PII, pass everything else
    through. See the module-level matrix. Returns a :class:`PiiScreenResult`
    whose ``text_for_index`` is the only text the caller may index.

    Raises :class:`PiiVaultRefusal` on every fail-closed condition (scan crash,
    vault locked/uninitialized while high/critical PII present, secure-write
    failure). Callers translate that into a refusal + staging cleanup.

    When scrub-and-vault is ACTIVE (``PUFFPAI_SCRUB_AND_VAULT`` on AND a vault
    PIN-recovery code enrolled — the go-live interlock, see
    ``scrub_and_vault_active``), uploads are routed through the unified
    scrub-and-vault choke point (per-span ``{{vault:}}`` handles for good
    high/critical PII, shared classifier + Archivist quality gate) via
    :func:`_screen_via_scrub_and_vault`. Otherwise (flag off, or flag on but no
    recovery code) the legacy behavior below runs UNCHANGED.
    """
    if file_id is None:
        file_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")

    # NEW (Increment 2, flag-gated, default OFF): unify uploads onto the same
    # scrub-and-vault contract the chat path uses. Skipped entirely when off.
    try:
        from puffpai.security.scrub_and_vault import scrub_and_vault_active
        # Go-live interlock: route uploads through the vaulting choke point only
        # when the flag is on AND a PIN-recovery code exists; otherwise the legacy
        # screen below runs unchanged (no vaulting without a recovery net).
        _sv_on = scrub_and_vault_active(vault=vault)
    except Exception:
        _sv_on = False
    if _sv_on:
        return _screen_via_scrub_and_vault(
            text, filename, file_id=file_id, vault=vault, source=source)

    # ── LEGACY behavior (flag OFF): unchanged ──────────────────────────────
    # FAIL CLOSED around the scan. If the classifier can't run (import failure,
    # mid-scan crash), we do NOT know whether this text holds PII, so it must
    # never reach the index — refuse. (This is stricter than the old Path A,
    # which skipped on ImportError; the classifier is a hard local dependency
    # and a broken import should be loud, not a silent PII bypass.)
    try:
        from puffpai.security.pii_classifier import PIIClassifier
        classifier = PIIClassifier()
        redacted, detection = classifier.redact_pii(text)
    except Exception as e:
        logger.warning(
            "PII scan failed for %s — refusing (text not indexed): %s",
            filename, e)
        raise PiiVaultRefusal(
            "PII scan failed — the document was not indexed. No unscanned "
            "document is ever stored in plaintext. Try re-uploading."
        ) from e

    if not detection.has_pii:
        return PiiScreenResult(text_for_index=text)

    if detection.severity in _HIGH_CRITICAL_SEVERITIES:
        # Vault the ORIGINAL (raises PiiVaultRefusal if it can't be secured),
        # then index only the REDACTED text.
        _vault_original_text(
            text, detection,
            filename=filename, file_id=file_id, vault=vault, source=source)
        logger.info(
            "PII in %s: %s — original vaulted, redacted copy indexed",
            filename, detection.summary())
        return PiiScreenResult(
            text_for_index=redacted,
            pii_detected=True,
            pii_vaulted=True,
            pii_types=detection.pii_types,
            severity=detection.severity,
            summary=detection.summary(),
        )

    # Medium/low only (email/phone/name): index as-is, no vault — redacting
    # every name would gut document utility. Detection is still reported.
    return PiiScreenResult(
        text_for_index=text,
        pii_detected=True,
        pii_vaulted=False,
        pii_types=detection.pii_types,
        severity=detection.severity,
        summary=detection.summary(),
    )


def ingest_file(
    file_path: str,
    original_filename: str | None = None,
    user_hint: str | None = None,
    auto_apply: bool = True,
    *,
    vault=None,
) -> IngestionResult:
    """End-to-end pipeline: extract text, classify, parse, route to apps.

    Args:
        file_path: Path to the saved upload.
        original_filename: Original name from the user (for display).
        user_hint: Optional hint from the user about what this is
            (e.g. "this is mom's lasagna recipe", "school play flyer").
            Skips classification if provided.
        auto_apply: When True, automatically save the parsed result to
            the appropriate app store (recipe → freshtable, event →
            calendar). When False, just return the parsed payload.
        vault: Optional Vault instance (dependency injection for tests). When
            None, the process-wide singleton is used so live unlock sessions
            are visible.

    Returns:
        IngestionResult with the final state.
    """
    filename = original_filename or os.path.basename(file_path)
    file_id = os.path.basename(file_path)
    result = IngestionResult(
        file_id=file_id,
        filename=filename,
        file_type="",
        classification="",
        text_length=0,
    )

    try:
        ftype, text = extract_text(file_path)
        result.file_type = ftype
        result.text_length = len(text)

        # Hard-fail on extractor error placeholders — never index the
        # literal "[python-docx not installed …]" / "[OCR error …]" string.
        fail_reason = _extraction_failure_reason(text)
        if fail_reason:
            result.text_length = 0
            result.error = f"Could not read the file: {fail_reason}"
            return result

        if not text or len(text) < 5:
            result.error = "No text could be extracted from the file."
            return result

        # ── PII scan — vault sensitive VALUES, index the REDACTED rest ──
        # Product intent: a doc with PII → the sensitive values go to the
        # encrypted Zone 1 Vault and the REDACTED document stays searchable via
        # RAG. High/critical PII (SSN/CC/address/DOB) → vault original + index
        # redacted; medium-only (email/phone/name) → index as-is (redacting
        # every name would gut utility); none → pass through. Shared gate keeps
        # the fail-closed invariants identical across both upload paths.
        def _remove_staging():
            # Best-effort: delete the unencrypted staging copy so the plaintext
            # original does not linger in /uploads once its sensitive values
            # live only in the vault (or the upload is refused).
            try:
                staging = Path(file_path)
                if staging.exists():
                    staging.unlink()
            except Exception as _e:
                logger.debug("Could not remove staging copy: %s", _e)

        try:
            screen = screen_text_for_pii(
                text, filename, file_id=file_id, vault=vault,
                source="document_upload")
        except PiiVaultRefusal as refusal:
            # FAIL CLOSED: high/critical PII that could not be secured, or a
            # scan crash. Index NOTHING; remove the plaintext staging copy.
            result.pii_detected = True
            result.error = refusal.message
            result.actions_taken.append(f"⚠️ Upload refused — {refusal.message}")
            _remove_staging()
            return result

        if screen.pii_detected:
            result.pii_detected = True
            result.pii_summary = screen.summary
        if screen.pii_vaulted:
            result.vaulted = True
            result.actions_taken.append(
                f"⚠️ PII detected ({screen.summary}) — sensitive values secured "
                f"in Zone 1 Vault; a redacted copy is indexed and searchable."
            )
            # The original (unredacted) staging copy must not linger as
            # plaintext now that its sensitive values live only in the vault.
            _remove_staging()

        # Everything downstream — classify, parse, index — operates on the
        # screened text: redacted when vaulted, original otherwise. Nothing
        # unredacted-high-PII reaches ingested_docs.json or ChromaDB.
        text = screen.text_for_index

        # If user gave a hint, use it; otherwise classify
        if user_hint:
            hint_lower = user_hint.lower()
            if any(k in hint_lower for k in ("recipe", "ingredient", "cook")):
                classification = "recipe"
            elif any(k in hint_lower for k in ("event", "flyer", "party", "play", "concert", "appointment")):
                classification = "event"
            elif any(k in hint_lower for k in ("schedule", "calendar", "agenda", "syllabus")):
                classification = "schedule"
            elif any(k in hint_lower for k in ("study", "textbook", "notes", "homework", "lecture")):
                classification = "study"
            elif any(k in hint_lower for k in ("shopping", "grocer", "buy")):
                classification = "shopping_list"
            else:
                classification = classify_document(text)
        else:
            classification = classify_document(text)

        result.classification = classification

        # ALWAYS index the raw text so it can be searched later, regardless
        # of classification. This way recipes/events/etc are still findable
        # by `search_my_documents` even after they've been routed to apps.
        # A failure here must NOT be swallowed — an un-indexed document that
        # the UI reports as "indexed" is exactly the bug this path had. Let it
        # propagate to the outer handler so `result.error` is set honestly.
        _index_study_doc(file_id, filename, text, classification=classification)

        # Parse + (optionally) apply
        if classification == "recipe":
            parsed = parse_recipe(text)
            result.structured_data = parsed
            if auto_apply and parsed:
                from puffpai.app_data import AppData
                app = AppData()
                app.fresh_create_recipe(
                    parsed["name"],
                    parsed["ingredients"],
                    parsed["instructions"],
                )
                result.actions_taken.append(f"Added recipe '{parsed['name']}' to FreshTable")

        elif classification == "event":
            parsed = parse_event(text)
            result.structured_data = parsed
            if auto_apply and parsed:
                from puffpai.app_data import AppData
                app = AppData()
                app.cal_add_event(
                    title=parsed["title"],
                    start_iso=parsed["start"],
                    location=parsed.get("location") or None,
                )
                result.actions_taken.append(f"Added event '{parsed['title']}' to Calendar")

        elif classification == "schedule":
            events = parse_schedule(text)
            result.structured_data = {"events": events}
            if auto_apply and events:
                from puffpai.app_data import AppData
                app = AppData()
                for ev in events:
                    try:
                        app.cal_add_event(
                            title=ev.get("title", "Untitled"),
                            start_iso=ev.get("start", ""),
                            location=ev.get("location") or None,
                        )
                    except Exception as e:
                        logger.warning("Failed to add event %s: %s", ev, e)
                result.actions_taken.append(
                    f"Added {len(events)} events to Calendar from schedule"
                )

        elif classification == "study":
            # Already indexed above; just record the action.
            result.actions_taken.append(f"Indexed '{filename}' for study Q&A")
            result.structured_data = {
                "preview": text[:500],
                "word_count": len(text.split()),
            }

        elif classification == "shopping_list":
            # Parse line-by-line for items
            items = _parse_shopping_lines(text)
            result.structured_data = {"items": items}
            if auto_apply and items:
                from puffpai.app_data import AppData
                app = AppData()
                for item in items:
                    try:
                        app.fresh_add_shopping(
                            item.get("name", ""),
                            item.get("quantity", 1),
                            0,
                        )
                    except Exception as e:
                        logger.warning("Failed to add shopping item %s: %s", item, e)
                result.actions_taken.append(
                    f"Added {len(items)} items to shopping list"
                )

        else:  # 'note' or 'unknown'
            result.actions_taken.append(f"Saved '{filename}' as a searchable note")
            result.structured_data = {"preview": text[:500]}

    except Exception as e:
        logger.exception("Ingestion failed for %s", file_path)
        result.error = str(e)

    return result


def _parse_shopping_lines(text: str) -> list[dict]:
    """Naive parsing of a shopping list — one item per line."""
    items = []
    for line in text.splitlines():
        line = line.strip().strip("-*•").strip()
        if not line or len(line) > 200:
            continue
        # Try to extract leading quantity
        m = re.match(r"^(\d+)\s+(.+)$", line)
        if m:
            items.append({"name": m.group(2).strip(), "quantity": int(m.group(1))})
        else:
            items.append({"name": line, "quantity": 1})
    return items[:50]  # Cap at 50 items per upload


def _index_study_doc(
    file_id: str, filename: str, text: str, classification: str = "study"
):
    """Append a doc to the JSON index AND chunk it into the ChromaDB
    `user_uploads` collection so semantic search works.

    The JSON index is kept as a fast metadata catalog (filename,
    classification, indexed_at) and as the source of truth for the full
    raw text. ChromaDB is the search index.
    """
    # ── 1. JSON metadata catalog ────────────────────────────────────────
    doc_index = _doc_index()
    try:
        if doc_index.exists():
            with open(doc_index, "r", encoding="utf-8") as f:
                index = json.load(f)
        else:
            index = []
    except Exception:
        index = []

    entry = {
        "id": file_id,
        "filename": filename,
        "classification": classification,
        "indexed_at": datetime.now().isoformat(),
        "text": text,
        "word_count": len(text.split()),
    }
    # Replace existing if same ID, else append
    index = [e for e in index if e.get("id") != file_id]
    index.append(entry)

    doc_index.parent.mkdir(parents=True, exist_ok=True)
    with open(doc_index, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    # ── 2. ChromaDB semantic index ──────────────────────────────────────
    # Chunk the text and embed it into the user_uploads collection so
    # the existing RAG pipeline can find it semantically. Failures propagate
    # to the caller (ingest_file) so the upload is honestly reported as failed
    # rather than silently un-searchable.
    _add_to_user_uploads_collection(file_id, filename, text, classification)


# ─────────────────────────────────────────────────────────────────────
#  ChromaDB user_uploads collection
# ─────────────────────────────────────────────────────────────────────

USER_UPLOADS_COLLECTION = "user_uploads"

# Conservative chunk size — fits comfortably in the LLM context window
# and gives good semantic granularity. ~150 words per chunk with overlap.
_CHUNK_SIZE_CHARS = 900
_CHUNK_OVERLAP_CHARS = 150


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks at sentence/paragraph boundaries.

    Tries to break on \\n\\n first, then \\n, then ". ", then falls back
    to a hard split. Each chunk has roughly 150-word coherent context.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= _CHUNK_SIZE_CHARS:
        return [text]

    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + _CHUNK_SIZE_CHARS, n)
        # Try to find a natural break near the end
        if end < n:
            for boundary in ("\n\n", "\n", ". ", " "):
                b = text.rfind(boundary, start + _CHUNK_SIZE_CHARS // 2, end)
                if b > 0:
                    end = b + len(boundary)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        # Step forward, leaving overlap
        start = max(start + 1, end - _CHUNK_OVERLAP_CHARS)
        if start >= n:
            break
    return chunks


_upload_embedder = None


def _get_upload_embedder():
    """Cached embedding function used to PIN new upload collections to the
    same model the RAG query path uses (`_get_query_embedder` in
    api_server). Avoids the default-ONNX vs sentence-transformers mismatch
    that produced subtly different vectors.

    Read and write sides share ONE instance via
    `puffpai.embeddings.get_shared_embedding_function()`. Note that this alone
    does not prevent drift: chroma embeds by whatever function each collection
    persisted (nothing, for the legacy ones), so every add/query must also
    pass explicit `embeddings=`/`query_embeddings=` built from it. Default
    stays all-MiniLM-L6-v2 until a full re-index switches backends."""
    global _upload_embedder
    if _upload_embedder is None:
        from puffpai.embeddings import get_shared_embedding_function
        _upload_embedder = get_shared_embedding_function()
    return _upload_embedder


def get_or_create_upload_collection(
    client, name, extra_metadata=None, embedding_function=None
):
    """Get or create a Chroma collection for user-uploaded content.

    NEW collections are pinned to the configured embedding function (see
    puffpai.embeddings; all-MiniLM-L6-v2 today) + cosine space, so the
    0.75/0.25 relevance math in `_get_rag_context` is exact. The space-aware
    relevance filter in `_get_rag_context` handles legacy collections' l2
    space.

    For EXISTING collections we re-attach the embedding function when chroma
    permits it (`open_collection`), but that is only a best effort — chroma
    1.5.8 rejects an explicit EF on any collection whose persisted config is
    default/absent, which is all of the legacy ones. **Callers on the write
    path must therefore pass explicit `embeddings=`**; the returned handle's
    own embedding function cannot be trusted to be ours. Metadata is still
    ignored by chroma on get, so it is only applied at create.
    """
    from chromadb.errors import NotFoundError
    try:
        from puffpai.embeddings import open_collection
        return open_collection(client, name, embedding_function)
    except NotFoundError:
        pass  # Does not exist yet — create it pinned below.
    # Anything else (notably ImportError from loading the embedding model)
    # is a real failure and must NOT be mistaken for "collection missing" —
    # swallowing it here would send us into create_collection, fail again,
    # and report a confusing race-condition error instead of the true cause.

    ef = embedding_function or _get_upload_embedder()
    metadata = {"hnsw:space": "cosine"}
    if extra_metadata:
        metadata.update(extra_metadata)
    try:
        return client.create_collection(
            name=name, embedding_function=ef, metadata=metadata)
    except Exception:
        # Lost a create race with a concurrent request — return the existing.
        from puffpai.embeddings import open_collection
        return open_collection(client, name, embedding_function)


def _get_user_uploads_collection():
    """Return (or lazily create) the ChromaDB collection for user uploads.

    Raises on failure (import/client/creation) — callers on the WRITE path
    must surface this so an upload is never reported "indexed" when it was
    not. Read-only callers (query/delete) catch and degrade gracefully.
    """
    from puffpai.knowledge.rag import DualZoneRAG

    rag = DualZoneRAG()
    client = rag.client
    if client is None:
        raise RuntimeError("ChromaDB client unavailable (DualZoneRAG.client is None)")
    return get_or_create_upload_collection(
        client,
        USER_UPLOADS_COLLECTION,
        extra_metadata={
            "description": "User-uploaded documents indexed by PuffPAI ingestion"
        },
    )


def _add_to_user_uploads_collection(
    file_id: str, filename: str, text: str, classification: str
):
    """Chunk text and add it to the user_uploads ChromaDB collection.

    First removes any existing chunks for this file_id (so re-uploads
    update cleanly), then adds the new chunks. Raises on any failure so the
    caller can report the upload as failed rather than falsely "indexed".
    """
    collection = _get_user_uploads_collection()

    # Remove old chunks for this file (idempotent re-upload). A delete of a
    # non-existent file_id is a no-op, but a real failure here should surface.
    collection.delete(where={"file_id": file_id})

    chunks = _chunk_text(text)
    if not chunks:
        raise RuntimeError(
            f"No indexable text chunks produced for {filename}"
        )

    ids = [f"{file_id}_chunk_{i}" for i in range(len(chunks))]
    indexed_at = datetime.now().isoformat()
    metadatas = [
        {
            "file_id": file_id,
            "filename": filename,
            "classification": classification,
            "chunk_index": i,
            "total_chunks": len(chunks),
            "indexed_at": indexed_at,
            "source": "user_upload",
        }
        for i in range(len(chunks))
    ]
    # embeddings= is not optional here — see _get_upload_embedder and
    # puffpai.embeddings.open_collection: chroma would otherwise embed with
    # whatever the collection persisted, which for legacy collections is its
    # default ONNX model rather than the configured one.
    from puffpai.embeddings import embed_documents
    collection.add(ids=ids, documents=chunks, metadatas=metadatas,
                   embeddings=embed_documents(chunks))

    # Verify the chunks actually landed before claiming success.
    stored = collection.get(where={"file_id": file_id}, include=[])
    stored_ids = stored.get("ids", []) if stored else []
    if len(stored_ids) < len(chunks):
        raise RuntimeError(
            f"Indexing verification failed for {filename}: "
            f"stored {len(stored_ids)} of {len(chunks)} chunks"
        )
    logger.info(
        "Added %d chunks of %s to user_uploads collection",
        len(chunks), filename,
    )


def _delete_from_user_uploads_collection(file_id: str):
    """Remove all chunks belonging to a file from the user_uploads collection."""
    try:
        collection = _get_user_uploads_collection()
        collection.delete(where={"file_id": file_id})
        logger.info("Deleted chunks for %s from user_uploads", file_id)
    except Exception as e:
        logger.warning("ChromaDB delete failed for %s: %s", file_id, e)


def query_user_uploads(query: str, n_results: int = 5) -> list[dict]:
    """Semantic vector search across all user-uploaded documents.

    Returns a list of {filename, classification, snippet, score, file_id}
    dicts, one per matching chunk, sorted by relevance.
    """
    try:
        collection = _get_user_uploads_collection()
        # query_embeddings=, not query_texts= — this runs on the main chat RAG
        # path (api_server._get_rag_context). The write side embeds these
        # chunks with the configured function, so the query must too.
        from puffpai.embeddings import embed_documents
        results = collection.query(
            query_embeddings=embed_documents([query]),
            n_results=max(1, min(n_results, 20)),
        )
    except Exception as e:
        logger.warning("ChromaDB query failed: %s", e)
        return []

    # Expose the collection's distance space so callers can normalize
    # distances to a comparable relevance score (l2 is squared, cosine is not).
    space = (collection.metadata or {}).get("hnsw:space", "l2")

    if not results or not results.get("documents"):
        return []

    docs = results["documents"][0] if results["documents"] else []
    metas = results["metadatas"][0] if results.get("metadatas") else []
    distances = results["distances"][0] if results.get("distances") else []

    out = []
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else {}
        distance = distances[i] if i < len(distances) else 1.0
        out.append({
            "file_id": meta.get("file_id", ""),
            "filename": meta.get("filename", "(unknown)"),
            "classification": meta.get("classification", "unknown"),
            "snippet": doc,
            "score": round(1.0 - min(distance, 1.0), 3),
            "distance": distance,
            "space": space,
            "chunk_index": meta.get("chunk_index", 0),
        })
    return out


def reindex_all_uploads():
    """Re-chunk and re-embed every doc in ingested_docs.json into ChromaDB.

    Used as a one-shot migration when the schema changes or after a
    fresh checkout. Safe to call repeatedly — each file_id is replaced
    in place.
    """
    doc_index = _doc_index()
    if not doc_index.exists():
        return {"reindexed": 0}
    with open(doc_index, "r", encoding="utf-8") as f:
        index = json.load(f)

    count = 0
    for entry in index:
        try:
            _add_to_user_uploads_collection(
                entry["id"],
                entry["filename"],
                entry.get("text", ""),
                entry.get("classification", "unknown"),
            )
            count += 1
        except Exception as e:
            logger.warning("Failed to reindex %s: %s", entry.get("id"), e)
    return {"reindexed": count, "total": len(index)}


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "from", "by", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "this", "that", "these",
    "those", "i", "me", "my", "you", "your", "we", "our", "it", "its",
    "what", "where", "when", "why", "how", "who", "which",
    "tell", "show", "give", "find", "search", "list", "please",
    "about", "into", "than", "then", "out", "up", "down",
})


def search_indexed_docs(query: str, max_results: int = 5) -> list[dict]:
    """Hybrid search across user-uploaded documents.

    Combines:
      1. ChromaDB vector search (semantic, primary)
      2. Filename substring boost (catches acronyms/exact-name queries
         where the body never contains the term)
      3. JSON metadata catalog (for word_count, indexed_at, etc.)

    Returns one result per UNIQUE document, with the best matching
    chunk as the snippet.
    """
    if not query or not query.strip():
        return []

    # ── 1. Vector search (semantic) ────────────────────────────────────
    vector_hits = query_user_uploads(query, n_results=max_results * 3)

    # ── 2. Filename match — boost docs whose name contains query words ─
    raw_words = re.findall(r"\w+", query.lower())
    query_words = [w for w in raw_words if w not in _STOPWORDS and len(w) >= 3]
    if not query_words:
        query_words = raw_words

    # Load metadata catalog for filename matching + word_count
    catalog = {}
    doc_index = _doc_index()
    if doc_index.exists():
        try:
            with open(doc_index, "r", encoding="utf-8") as f:
                for entry in json.load(f):
                    catalog[entry["id"]] = entry
        except Exception:
            pass

    # ── 3. Aggregate per-document, keeping the best chunk ─────────────
    by_doc: dict[str, dict] = {}
    for hit in vector_hits:
        fid = hit.get("file_id", "")
        if not fid:
            continue
        # Score combines semantic relevance + filename hit boost
        fname_lower = hit.get("filename", "").lower()
        fname_hits = sum(1 for w in query_words if w in fname_lower)
        combined_score = hit.get("score", 0) + fname_hits * 0.3

        if fid not in by_doc or combined_score > by_doc[fid]["score"]:
            meta = catalog.get(fid, {})
            by_doc[fid] = {
                "id": fid,
                "filename": hit.get("filename", "(unknown)"),
                "classification": hit.get("classification", "study"),
                "indexed_at": meta.get("indexed_at", ""),
                "word_count": meta.get("word_count", 0),
                "score": combined_score,
                "snippet": hit.get("snippet", "")[:800],
            }

    # ── 4. Filename-only fallback (for acronym matches like "HOA") ───
    # Pull in any docs from the catalog whose filename matches but
    # weren't returned by the vector search.
    for fid, entry in catalog.items():
        if fid in by_doc:
            continue
        fname_lower = entry.get("filename", "").lower()
        fname_hits = sum(1 for w in query_words if w in fname_lower)
        if fname_hits == 0:
            continue
        # Use the start of the document as a snippet
        text = entry.get("text", "")
        snippet = text[:600].strip()
        if len(text) > 600:
            snippet += "…"
        by_doc[fid] = {
            "id": fid,
            "filename": entry["filename"],
            "classification": entry.get("classification", "study"),
            "indexed_at": entry.get("indexed_at", ""),
            "word_count": entry.get("word_count", 0),
            "score": fname_hits * 0.5,  # filename-only is a moderate hit
            "snippet": snippet,
        }

    # Sort by score, return top N
    results = sorted(by_doc.values(), key=lambda r: r["score"], reverse=True)
    return results[:max_results]


def fetch_document_text(doc_id_or_filename: str, max_chars: int = 6000) -> dict | None:
    """Return the FULL text of a single indexed document, looked up by
    either its file_id or its filename (case-insensitive partial match).

    Used by `fetch_my_document` so the LLM can get the entire content
    of a known doc instead of relying on snippets.
    """
    doc_index = _doc_index()
    if not doc_index.exists():
        return None
    try:
        with open(doc_index, "r", encoding="utf-8") as f:
            index = json.load(f)
    except Exception:
        return None

    needle = doc_id_or_filename.lower().strip()
    # Exact id match wins
    for entry in index:
        if entry.get("id", "").lower() == needle:
            return _trim_doc_for_llm(entry, max_chars)
    # Otherwise filename substring match
    for entry in index:
        if needle in entry.get("filename", "").lower():
            return _trim_doc_for_llm(entry, max_chars)
    return None


def _trim_doc_for_llm(entry: dict, max_chars: int) -> dict:
    text = entry.get("text", "")
    truncated = len(text) > max_chars
    return {
        "id": entry.get("id"),
        "filename": entry.get("filename"),
        "classification": entry.get("classification"),
        "word_count": entry.get("word_count", 0),
        "text": text[:max_chars] + ("\n…[truncated]" if truncated else ""),
        "truncated": truncated,
    }


def list_ingested_docs() -> list[dict]:
    """Return a summary of all ingested documents."""
    doc_index = _doc_index()
    if not doc_index.exists():
        return []
    try:
        with open(doc_index, "r", encoding="utf-8") as f:
            index = json.load(f)
    except Exception:
        return []
    return [
        {
            "id": e.get("id"),
            "filename": e.get("filename"),
            # Display name; falls back to the filename. See set_doc_title.
            "title": e.get("title") or e.get("filename"),
            "classification": e.get("classification"),
            "indexed_at": e.get("indexed_at"),
            "word_count": e.get("word_count", 0),
        }
        for e in index
    ]


def set_doc_title(doc_id: str, title: str) -> bool:
    """Rename one catalog document. Its id never changes.

    The id is what the chunks in ``user_uploads`` are keyed by, so renaming
    the row is the only rename that can be safe.
    """
    doc_index = _doc_index()
    if not doc_index.exists():
        return False
    try:
        with open(doc_index, "r", encoding="utf-8") as f:
            index = json.load(f)
    except Exception:
        return False
    hit = False
    for entry in index:
        if entry.get("id") == doc_id:
            entry["title"] = str(title)
            hit = True
    if not hit:
        return False
    doc_index.parent.mkdir(parents=True, exist_ok=True)
    with open(doc_index, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    return True


def delete_ingested_doc(doc_id: str) -> bool:
    """Remove an indexed document by ID — from JSON catalog, ChromaDB,
    AND the uploads directory on disk.

    Some documents only exist as chunks in the user_uploads ChromaDB
    collection (an "index-only" row, e.g. surfaced by
    upload_index.all_uploaded_docs) with no entry in the JSON catalog.
    For those, fall through to a Chroma-only delete rather than returning
    False just because the catalog has nothing to remove.
    """
    doc_index = _doc_index()
    index = []
    if doc_index.exists():
        try:
            with open(doc_index, "r", encoding="utf-8") as f:
                index = json.load(f)
        except Exception:
            index = []

    new_index = [e for e in index if e.get("id") != doc_id]
    catalog_hit = len(new_index) != len(index)

    if catalog_hit:
        doc_index.parent.mkdir(parents=True, exist_ok=True)
        with open(doc_index, "w", encoding="utf-8") as f:
            json.dump(new_index, f, indent=2, ensure_ascii=False)
    else:
        # Not in the catalog — only proceed if there are actually chunks
        # for this id in the collection, so an unknown id still returns
        # False.
        try:
            collection = _get_user_uploads_collection()
            found = collection.get(where={"file_id": doc_id}, limit=1)
            has_chunks = bool(found and found.get("ids"))
        except Exception:
            has_chunks = False
        if not has_chunks:
            return False

    # Remove all chunks from ChromaDB
    try:
        _delete_from_user_uploads_collection(doc_id)
    except Exception:
        pass

    # Also try to remove the file from uploads dir
    try:
        upload_path = _upload_dir() / doc_id
        if upload_path.exists():
            upload_path.unlink()
    except Exception:
        pass
    return True
