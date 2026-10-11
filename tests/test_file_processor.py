"""
Tests for puffpai.file_processor — Multi-format file text extraction.
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from puffpai.file_processor import (
    extract_text,
    get_capabilities,
    SUPPORTED_EXTENSIONS,
    TEXT_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    SPREADSHEET_EXTENSIONS,
    IMAGE_EXTENSIONS,
)


class TestSupportedExtensions:
    """Verify the extension sets are properly defined."""

    def test_txt_in_text(self):
        assert '.txt' in TEXT_EXTENSIONS

    def test_md_in_text(self):
        assert '.md' in TEXT_EXTENSIONS

    def test_pdf_in_documents(self):
        assert '.pdf' in DOCUMENT_EXTENSIONS

    def test_docx_in_documents(self):
        assert '.docx' in DOCUMENT_EXTENSIONS

    def test_csv_in_spreadsheets(self):
        assert '.csv' in SPREADSHEET_EXTENSIONS

    def test_xlsx_in_spreadsheets(self):
        assert '.xlsx' in SPREADSHEET_EXTENSIONS

    def test_jpg_in_images(self):
        assert '.jpg' in IMAGE_EXTENSIONS

    def test_png_in_images(self):
        assert '.png' in IMAGE_EXTENSIONS

    def test_heic_in_images(self):
        """iPhone photos arrive as .heic — Send to Puff depends on this."""
        assert '.heic' in IMAGE_EXTENSIONS
        assert '.heic' in SUPPORTED_EXTENSIONS

    def test_heif_in_images(self):
        assert '.heif' in IMAGE_EXTENSIONS
        assert '.heif' in SUPPORTED_EXTENSIONS

    def test_json_in_supported(self):
        assert '.json' in SUPPORTED_EXTENSIONS

    def test_supported_is_union(self):
        """SUPPORTED_EXTENSIONS should be the union of all category sets + json."""
        expected = TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS | SPREADSHEET_EXTENSIONS | IMAGE_EXTENSIONS | {'.json'}
        assert SUPPORTED_EXTENSIONS == expected


class TestExtractTextFile:
    """Tests for plain text file extraction."""

    def test_extract_txt(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello, world!", encoding="utf-8")
        text, meta = extract_text(str(f))
        assert text == "Hello, world!"
        assert meta["extractor"] == "plaintext"
        assert meta["has_content"] is True
        assert meta["extension"] == ".txt"

    def test_extract_md(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# Title\n\nSome content.", encoding="utf-8")
        text, meta = extract_text(str(f))
        assert "# Title" in text
        assert meta["extractor"] == "plaintext"

    def test_extract_empty_txt(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        text, meta = extract_text(str(f))
        assert text == ""
        assert meta["has_content"] is False

    def test_extract_python_file(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("print('hello')", encoding="utf-8")
        text, meta = extract_text(str(f))
        assert "print" in text
        assert meta["extractor"] == "plaintext"


class TestExtractJson:
    """Tests for JSON file extraction."""

    def test_extract_json(self, tmp_path):
        f = tmp_path / "data.json"
        data = {"name": "PuffPAI", "version": "1.0"}
        f.write_text(json.dumps(data), encoding="utf-8")
        text, meta = extract_text(str(f))
        assert '"name": "PuffPAI"' in text
        assert meta["extractor"] == "json"
        assert "name" in meta["keys"]


class TestExtractCsv:
    """Tests for CSV file extraction."""

    def test_extract_csv(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("name,age\nAlice,30\nBob,25", encoding="utf-8")
        text, meta = extract_text(str(f))
        assert "name | age" in text
        assert "Alice | 30" in text
        assert meta["extractor"] == "csv"
        assert meta["row_count"] == 3

    def test_extract_tsv(self, tmp_path):
        f = tmp_path / "data.tsv"
        f.write_text("col1\tcol2\nval1\tval2", encoding="utf-8")
        text, meta = extract_text(str(f))
        assert "col1 | col2" in text
        assert meta["delimiter"] == "tab"


class TestExtractHtml:
    """Tests for HTML file extraction."""

    def test_extract_html_simple(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text("<html><body><p>Hello HTML</p></body></html>", encoding="utf-8")
        text, meta = extract_text(str(f))
        assert "Hello HTML" in text


class TestExtractRtf:
    """Tests for RTF file extraction."""

    def test_extract_rtf_fallback(self, tmp_path):
        f = tmp_path / "doc.rtf"
        f.write_text("{\\rtf1 Hello RTF}", encoding="utf-8")
        text, meta = extract_text(str(f))
        assert "Hello RTF" in text


class TestFileNotFound:
    """Tests for error cases."""

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            extract_text("/nonexistent/file.txt")

    def test_unsupported_extension(self, tmp_path):
        f = tmp_path / "data.xyz"
        f.write_text("content", encoding="utf-8")
        with pytest.raises(ValueError, match="Unsupported file type"):
            extract_text(str(f))


class TestMetadata:
    """Tests for metadata included in extraction results."""

    def test_metadata_fields(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello", encoding="utf-8")
        text, meta = extract_text(str(f))
        assert "filename" in meta
        assert "extension" in meta
        assert "file_size" in meta
        assert "text_length" in meta
        assert "has_content" in meta
        assert meta["filename"] == "test.txt"
        assert meta["text_length"] == 5


class TestGetCapabilities:
    """Tests for the capabilities reporter."""

    def test_always_available(self):
        caps = get_capabilities()
        assert caps["plaintext"] is True
        assert caps["json"] is True
        assert caps["csv"] is True

    def test_returns_booleans(self):
        caps = get_capabilities()
        for key, val in caps.items():
            assert isinstance(val, bool), f"{key} should be bool, got {type(val)}"

    def test_expected_keys(self):
        caps = get_capabilities()
        expected = {"plaintext", "json", "csv", "pdf_pypdf", "pdf_pdfplumber",
                    "docx", "pptx", "xlsx", "ocr_tesseract", "ocr_heic",
                    "ocr_pdf", "html_bs4", "rtf"}
        assert set(caps.keys()) == expected


class TestHeifOpener:
    """HEIC/HEIF support depends on pillow-heif registering into PIL."""

    def test_heif_opener_registered(self):
        pytest.importorskip("pillow_heif")
        from PIL import Image
        # Importing file_processor should already have registered the opener.
        assert '.heic' in Image.registered_extensions()
