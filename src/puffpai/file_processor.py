"""
PuffPAI Multi-Format File Processor — Extract text from various file types.

Supports: .txt, .md, .json, .pdf, .docx, .doc, .pptx, .csv, .xlsx, .html,
          .jpg, .jpeg, .png, .gif, .bmp, .tiff, .heic, .heif (OCR), .rtf

    from puffpai.file_processor import extract_text, SUPPORTED_EXTENSIONS

    text, metadata = extract_text("/path/to/document.docx")

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# HEIC/HEIF (iPhone photos) — PIL cannot open these on its own. pillow-heif
# registers an opener into PIL globally, so it must happen once at import
# time, before any Image.open() call. Optional, like the other extractors.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

# ── Supported file types ─────────────────────────────────────────────────

TEXT_EXTENSIONS = {'.txt', '.md', '.log', '.ini', '.cfg', '.yaml', '.yml',
                   '.toml', '.xml', '.py', '.js', '.html', '.css', '.sh',
                   '.bat', '.ps1', '.env'}

DOCUMENT_EXTENSIONS = {'.pdf', '.docx', '.doc', '.pptx', '.rtf', '.odt'}

SPREADSHEET_EXTENSIONS = {'.csv', '.tsv', '.xlsx', '.xls'}

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif',
                    '.webp', '.heic', '.heif'}

SUPPORTED_EXTENSIONS = (TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS |
                        SPREADSHEET_EXTENSIONS | IMAGE_EXTENSIONS |
                        {'.json'})


def _check_available(module_name: str) -> bool:
    """Check if a Python module is importable."""
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


# ── Extraction functions ─────────────────────────────────────────────────

def _extract_text_file(filepath: str) -> tuple[str, dict[str, Any]]:
    """Extract text from plain text files."""
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    return content, {"extractor": "plaintext", "encoding": "utf-8"}


def _extract_json_file(filepath: str) -> tuple[str, dict[str, Any]]:
    """Extract text from JSON files."""
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    content = json.dumps(data, indent=2, ensure_ascii=False)
    return content, {"extractor": "json", "keys": list(data.keys()) if isinstance(data, dict) else []}


def _extract_pdf(filepath: str) -> tuple[str, dict[str, Any]]:
    """Extract text from PDF files using available libraries."""
    # Try pypdf
    try:
        from pypdf import PdfReader
        reader = PdfReader(filepath)
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)
        if pages:
            return "\n\n".join(pages), {
                "extractor": "pypdf",
                "page_count": len(reader.pages),
                "pages_with_text": len(pages),
            }
    except ImportError:
        pass
    except Exception as e:
        logger.warning("pypdf failed: %s", e)

    # Try pdfplumber
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
            if pages:
                return "\n\n".join(pages), {
                    "extractor": "pdfplumber",
                    "page_count": len(pdf.pages),
                    "pages_with_text": len(pages),
                }
    except ImportError:
        pass
    except Exception as e:
        logger.warning("pdfplumber failed: %s", e)

    # If no text extracted, try OCR on image-based PDF
    if _check_available("pdf2image") and _check_available("pytesseract"):
        return _ocr_pdf(filepath)

    return "", {"extractor": "none", "error": "No PDF library available. Install pypdf: pip install pypdf"}


def _ocr_pdf(filepath: str) -> tuple[str, dict[str, Any]]:
    """OCR an image-based PDF using pdf2image + pytesseract."""
    try:
        from pdf2image import convert_from_path
        import pytesseract

        images = convert_from_path(filepath, dpi=200)
        pages = []
        for img in images:
            text = pytesseract.image_to_string(img)
            if text.strip():
                pages.append(text)

        return "\n\n".join(pages), {
            "extractor": "ocr_pdf",
            "page_count": len(images),
            "pages_with_text": len(pages),
        }
    except Exception as e:
        logger.warning("OCR PDF failed: %s", e)
        return "", {"extractor": "ocr_pdf", "error": str(e)}


def _extract_docx(filepath: str) -> tuple[str, dict[str, Any]]:
    """Extract text from Word .docx files."""
    try:
        from docx import Document
        doc = Document(filepath)
        paragraphs = []
        for para in doc.paragraphs:
            if para.text.strip():
                paragraphs.append(para.text)

        # Also extract tables
        tables_text = []
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                tables_text.append(" | ".join(cells))

        content = "\n\n".join(paragraphs)
        if tables_text:
            content += "\n\n--- Tables ---\n" + "\n".join(tables_text)

        return content, {
            "extractor": "python-docx",
            "paragraphs": len(paragraphs),
            "tables": len(doc.tables),
        }
    except ImportError:
        return "", {"extractor": "none", "error": "Install python-docx: pip install python-docx"}
    except Exception as e:
        logger.warning("DOCX extraction failed: %s", e)
        return "", {"extractor": "python-docx", "error": str(e)}


def _extract_pptx(filepath: str) -> tuple[str, dict[str, Any]]:
    """Extract text from PowerPoint .pptx files."""
    try:
        from pptx import Presentation
        prs = Presentation(filepath)
        slides_text = []

        for i, slide in enumerate(prs.slides, 1):
            parts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        text = paragraph.text.strip()
                        if text:
                            parts.append(text)
                if shape.has_table:
                    for row in shape.table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        parts.append(" | ".join(cells))
            if parts:
                slides_text.append(f"--- Slide {i} ---\n" + "\n".join(parts))

        return "\n\n".join(slides_text), {
            "extractor": "python-pptx",
            "slide_count": len(prs.slides),
            "slides_with_text": len(slides_text),
        }
    except ImportError:
        return "", {"extractor": "none", "error": "Install python-pptx: pip install python-pptx"}
    except Exception as e:
        logger.warning("PPTX extraction failed: %s", e)
        return "", {"extractor": "python-pptx", "error": str(e)}


def _extract_csv(filepath: str) -> tuple[str, dict[str, Any]]:
    """Extract text from CSV/TSV files."""
    ext = os.path.splitext(filepath)[1].lower()
    delimiter = '\t' if ext == '.tsv' else ','

    rows = []
    with open(filepath, 'r', encoding='utf-8', errors='replace', newline='') as f:
        reader = csv.reader(f, delimiter=delimiter)
        for row in reader:
            rows.append(" | ".join(row))

    return "\n".join(rows), {
        "extractor": "csv",
        "row_count": len(rows),
        "delimiter": "tab" if delimiter == '\t' else "comma",
    }


def _extract_xlsx(filepath: str) -> tuple[str, dict[str, Any]]:
    """Extract text from Excel .xlsx files."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(filepath, read_only=True, data_only=True)
        sheets_text = []

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(c.strip() for c in cells):
                    rows.append(" | ".join(cells))
            if rows:
                sheets_text.append(f"--- {sheet_name} ---\n" + "\n".join(rows))

        wb.close()
        return "\n\n".join(sheets_text), {
            "extractor": "openpyxl",
            "sheet_count": len(wb.sheetnames),
        }
    except ImportError:
        return "", {"extractor": "none", "error": "Install openpyxl: pip install openpyxl"}
    except Exception as e:
        logger.warning("XLSX extraction failed: %s", e)
        return "", {"extractor": "openpyxl", "error": str(e)}


def _ocr_text_is_weak(text: str, min_chars: int) -> bool:
    """Heuristic for tesseract output too poor to trust — triggers VLM escalation.

    Weak = too short (< ``min_chars``) OR mostly non-alphanumeric noise (the
    "|_ ~ \\ //" garbage tesseract emits on angled/low-contrast photos). The
    alphanumeric ratio is taken over non-whitespace characters so that normally
    spaced prose is not mistaken for garbage.
    """
    s = (text or "").strip()
    if len(s) < min_chars:
        return True
    dense = [c for c in s if not c.isspace()]
    if not dense:
        return True
    alnum = sum(c.isalnum() for c in dense)
    return (alnum / len(dense)) < 0.5


def _extract_image(filepath: str, vision_fn=None,
                   vision_min_chars: int = 120) -> tuple[str, dict[str, Any]]:
    """Extract text from images using tesseract OCR, escalating to a vision
    model when the OCR text is weak.

    Args:
        filepath: image path.
        vision_fn: optional callable ``(image_path) -> str``. The caller injects
            it (e.g. an Ollama vision-model wrapper) so this module stays free of
            any ollama/api_server import. When None, behaviour is tesseract-only
            and identical to the pre-vision version (backward compatible).
        vision_min_chars: OCR shorter than this is considered weak. Pass a very
            large value to force escalation on every image ("primary" VLM mode).
    """
    text = ""
    meta: dict[str, Any] = {"extractor": "tesseract_ocr", "vision_used": False}
    try:
        from PIL import Image
        import pytesseract

        img = Image.open(filepath)
        # pytesseract only accepts PIL images whose .format is tesseract-native;
        # HEIC/HEIF (opened via pillow-heif) fail that check. Converting drops
        # .format so pytesseract re-encodes the temp file as PNG.
        if (img.format or "").upper() not in {"BMP", "GIF", "JPEG", "PNG", "TIFF", "WEBP"}:
            img = img.convert("RGB")
        text = pytesseract.image_to_string(img).strip()
        meta["image_size"] = f"{img.width}x{img.height}"
        meta["image_mode"] = img.mode
    except ImportError as e:
        missing = "pytesseract" if "pytesseract" in str(e) else "Pillow"
        meta = {
            "extractor": "none",
            "vision_used": False,
            "error": f"Install {missing}: pip install {missing.lower()}",
        }
    except Exception as e:
        logger.warning("Image OCR failed: %s", e)
        meta = {"extractor": "tesseract_ocr", "vision_used": False,
                "error": str(e)}

    # Escalate to the vision model when OCR came out weak (or on every image in
    # "primary" mode). Only adopt the VLM text if it is actually richer.
    if vision_fn is not None and _ocr_text_is_weak(text, vision_min_chars):
        try:
            vtext = (vision_fn(filepath) or "").strip()
        except Exception as e:
            logger.warning("vision_fn failed: %s", e)
            vtext = ""
        if vtext and len(vtext) > len(text):
            meta["extractor"] = "qwen_vl"
            meta["vision_used"] = True
            meta["tesseract_chars"] = len(text)
            return vtext, meta

    return text, meta


def _extract_html(filepath: str) -> tuple[str, dict[str, Any]]:
    """Extract text from HTML files."""
    try:
        from bs4 import BeautifulSoup
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            soup = BeautifulSoup(f.read(), 'html.parser')

        # Remove script and style elements
        for tag in soup(['script', 'style', 'nav', 'footer']):
            tag.decompose()

        text = soup.get_text(separator='\n', strip=True)
        return text, {"extractor": "beautifulsoup", "title": soup.title.string if soup.title else ""}
    except ImportError:
        # Fallback: strip tags with regex
        import re
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        text = re.sub(r'<[^>]+>', ' ', content)
        text = re.sub(r'\s+', ' ', text).strip()
        return text, {"extractor": "regex_fallback"}


def _extract_rtf(filepath: str) -> tuple[str, dict[str, Any]]:
    """Extract text from RTF files."""
    try:
        from striprtf.striprtf import rtf_to_text
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        text = rtf_to_text(content)
        return text, {"extractor": "striprtf"}
    except ImportError:
        # Basic fallback — strip RTF control words
        import re
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        text = re.sub(r'\\[a-z]+\d*\s?', '', content)
        text = re.sub(r'[{}]', '', text)
        return text.strip(), {"extractor": "regex_fallback"}


# ── Main extraction function ─────────────────────────────────────────────

def extract_text(filepath: str, vision_fn=None,
                 vision_min_chars: int = 120) -> tuple[str, dict[str, Any]]:
    """Extract text content from any supported file type.

    Args:
        filepath: Path to the file.
        vision_fn: optional callable ``(image_path) -> str`` for vision-model
            escalation on image files. Ignored for non-image types. When None,
            image extraction is tesseract-only (backward compatible).
        vision_min_chars: OCR shorter than this counts as weak and triggers the
            vision escalation; pass a huge value to always escalate images.

    Returns:
        (text_content, metadata_dict) where metadata includes extractor used,
        page counts, errors, etc.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the file type is not supported.
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    ext = os.path.splitext(filepath)[1].lower()
    filename = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)

    base_meta = {
        "filename": filename,
        "extension": ext,
        "file_size": file_size,
    }

    if ext in TEXT_EXTENSIONS:
        text, meta = _extract_text_file(filepath)
    elif ext == '.json':
        text, meta = _extract_json_file(filepath)
    elif ext == '.pdf':
        text, meta = _extract_pdf(filepath)
    elif ext in ('.docx', '.doc'):
        if ext == '.doc':
            # .doc (legacy) — try converting to text
            text, meta = _extract_text_file(filepath)
            meta["warning"] = "Legacy .doc format — text extraction may be incomplete"
        else:
            text, meta = _extract_docx(filepath)
    elif ext == '.pptx':
        text, meta = _extract_pptx(filepath)
    elif ext in ('.csv', '.tsv'):
        text, meta = _extract_csv(filepath)
    elif ext in ('.xlsx', '.xls'):
        text, meta = _extract_xlsx(filepath)
    elif ext in IMAGE_EXTENSIONS:
        text, meta = _extract_image(filepath, vision_fn=vision_fn,
                                    vision_min_chars=vision_min_chars)
    elif ext == '.html':
        text, meta = _extract_html(filepath)
    elif ext == '.rtf':
        text, meta = _extract_rtf(filepath)
    else:
        raise ValueError(
            f"Unsupported file type: {ext}. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    base_meta.update(meta)
    base_meta["text_length"] = len(text)
    base_meta["has_content"] = bool(text.strip())

    return text, base_meta


def get_capabilities() -> dict[str, bool]:
    """Report which file type extractors are available."""
    return {
        "plaintext": True,
        "json": True,
        "csv": True,
        "pdf_pypdf": _check_available("pypdf"),
        "pdf_pdfplumber": _check_available("pdfplumber"),
        "docx": _check_available("docx"),
        "pptx": _check_available("pptx"),
        "xlsx": _check_available("openpyxl"),
        "ocr_tesseract": _check_available("pytesseract"),
        # HEIC/HEIF needs pillow-heif on top of pytesseract/PIL.
        "ocr_heic": (_check_available("pytesseract")
                     and _check_available("pillow_heif")),
        "ocr_pdf": _check_available("pdf2image") and _check_available("pytesseract"),
        "html_bs4": _check_available("bs4"),
        "rtf": _check_available("striprtf"),
    }
