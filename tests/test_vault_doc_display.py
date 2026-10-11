"""
Synthetic-data-only regression tests for the Zone-1 vault document display
feature (vault_view ephemeral serve, vault doc index, dashboard_display
taxonomy + ephemeral overlay + pending slot).

NO real PII anywhere. All bytes/PINs/labels below are throwaway synthetic
fixtures (b"%PDF-1.4 synthetic", PIN "vault-pin-0000", "Test Owner document", etc.).
"""

from __future__ import annotations

import time

import pytest

from puffpai.security.vault import (
    Vault,
    VaultError,
    VAULT_DOC_MIME_ALLOWLIST,
    STRICT_DOC_TYPES,
    HEALTH_DOC_TYPES,
)
from puffpai.security import vault_view
from puffpai import dashboard_display as dd


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_vault_view():
    """The vault_view slot is a module singleton — prevent BUSY bleed
    across tests by clearing it before AND after each test."""
    vault_view.clear_all()
    yield
    vault_view.clear_all()


@pytest.fixture
def unlocked_vault(tmp_path):
    """A freshly initialized+authenticated Vault backed by tmp_path."""
    v = Vault(vault_dir=str(tmp_path))
    v.initialize("vault-pin-0000")
    s = v.authenticate("vault-pin-0000")
    return v, s.session_id


# ── A. vault_view module ─────────────────────────────────────────────────

def test_vault_view_mint_fetch_round_trips_exact_bytes_and_mime():
    data = b"%PDF-1.4 synthetic"
    token = vault_view.mint(data, "application/pdf", "Test Owner document",
                            "doc-a", ttl=30)
    assert isinstance(token, str)
    result = vault_view.fetch(token)
    assert result is not None
    fetched_bytes, fetched_mime = result
    assert fetched_bytes == data
    assert fetched_mime == "application/pdf"


def test_vault_view_fetch_returns_none_after_clear():
    token = vault_view.mint(b"%PDF-1.4 synthetic", "application/pdf",
                            "Test Owner document", "doc-a", ttl=30)
    assert vault_view.clear(token) is True
    assert vault_view.fetch(token) is None


def test_vault_view_mint_busy_while_active_then_succeeds_after_clear():
    token1 = vault_view.mint(b"%PDF-1.4 synthetic", "application/pdf",
                             "Test Owner document A", "doc-a", ttl=30)
    assert isinstance(token1, str)

    second = vault_view.mint(b"%PDF-1.4 synthetic B", "application/pdf",
                             "Test Owner document B", "doc-b", ttl=30)
    assert second is vault_view.BUSY

    assert vault_view.clear(token1) is True

    token2 = vault_view.mint(b"%PDF-1.4 synthetic B", "application/pdf",
                             "Test Owner document B", "doc-b", ttl=30)
    assert isinstance(token2, str)
    assert token2 != token1


def test_vault_view_mint_busy_then_succeeds_after_clear_all():
    token1 = vault_view.mint(b"%PDF-1.4 synthetic", "application/pdf",
                             "Test Owner document A", "doc-a", ttl=30)
    assert vault_view.mint(b"x", "application/pdf", "T", "doc-b",
                           ttl=30) is vault_view.BUSY
    vault_view.clear_all()
    token2 = vault_view.mint(b"x", "application/pdf", "T", "doc-b", ttl=30)
    assert isinstance(token2, str)
    assert token1 != token2


def test_vault_view_max_fetches_caps_then_returns_none():
    token = vault_view.mint(b"%PDF-1.4 synthetic", "application/pdf",
                            "Test Owner document", "doc-a", ttl=30,
                            max_fetches=3)
    successes = 0
    for _ in range(10):
        result = vault_view.fetch(token)
        if result is None:
            break
        successes += 1
    assert successes == 3
    assert vault_view.fetch(token) is None


def test_vault_view_expiry():
    token = vault_view.mint(b"%PDF-1.4 synthetic", "application/pdf",
                            "Test Owner document", "doc-a", ttl=0.05)
    time.sleep(0.1)
    assert vault_view.fetch(token) is None


# ── B. normalize_* taxonomy regression ───────────────────────────────────

@pytest.mark.parametrize("phrase,expected", [
    ("show me fakeadult's birth certificate", "birth_certificate"),
    ("show me fakeadult's death certificate", "death_certificate"),
    ("show me fakeadult's marriage certificate", "marriage_certificate"),
    ("show me fakeadult's marriage license", "marriage_certificate"),
    ("show me fakeadult's living will", "will"),
    ("show me fakeadult's will", "will"),
    ("show me fakeadult's title deed", "deed"),
    ("show me fakeadult's deed", "deed"),
    ("show me fakeadult's mortgage", "mortgage"),
    ("show me fakeadult's passport", "passport"),
    ("show me fakeadult's social security card", "ssn"),
    ("show me fakeadult's driver's license", "drivers_license"),
    ("show me fakeadult's medical record", "medical_record"),
    ("show me fakeadult's insurance card", "insurance_card"),
    ("show me fakeadult's tax return", "tax_return"),
    ("show me fakeadult's w-2", "tax_return"),
    ("show me fakeadult's 1099", "tax_return"),
    ("show me fakeadult's bank statement", "bank_statement"),
    ("show me fakeadult's account number", "bank_account"),
])
def test_normalize_doc_type_maps_phrases(phrase, expected):
    assert dd.normalize_doc_type(phrase) == expected


def test_normalize_doc_type_distinguishes_birth_will_deed():
    # Regression: these must NOT all collapse to "other_vault_doc".
    birth = dd.normalize_doc_type("show me fakeadult's birth certificate")
    will = dd.normalize_doc_type("show me fakeadult's will")
    deed = dd.normalize_doc_type("show me fakeadult's deed")
    assert birth == "birth_certificate"
    assert will == "will"
    assert deed == "deed"
    assert len({birth, will, deed}) == 3
    assert "other_vault_doc" not in {birth, will, deed}


def test_normalize_doc_type_no_match_returns_none():
    assert dd.normalize_doc_type("show me the weather") is None


def test_normalize_owner_possessive_name():
    assert dd.normalize_owner("fakeadult's birth certificate") == "fakeadult"


def test_normalize_owner_self_possessive():
    assert dd.normalize_owner("my passport") == "self"


def test_normalize_owner_bare_his_is_none():
    assert dd.normalize_owner("his social security card") is None


def test_normalize_owner_drivers_license_guard():
    # Key guard: "driver's" in "driver's license" must NOT be read as an owner.
    assert dd.normalize_owner("driver's license") is None


def test_normalize_vault_query():
    assert dd.normalize_vault_query("fakeadult's birth certificate") == (
        "fakeadult", "birth_certificate")


# ── C. vault doc index ───────────────────────────────────────────────────

def test_store_document_returns_doc_id(unlocked_vault):
    v, sid = unlocked_vault
    doc_id = v.store_document(owner="fakeadult", doc_type="birth_certificate",
                              label="Test A", data=b"%PDF-1.4 synthetic A",
                              mime="application/pdf", session_id=sid)
    assert isinstance(doc_id, str)
    assert doc_id


def test_find_documents_distinguishes_same_owner_by_type(unlocked_vault):
    v, sid = unlocked_vault
    birth_id = v.store_document(owner="fakeadult", doc_type="birth_certificate",
                                label="Test A", data=b"%PDF-1.4 synthetic A",
                                mime="application/pdf", session_id=sid)
    will_id = v.store_document(owner="fakeadult", doc_type="will",
                               label="Test W", data=b"%PDF-1.4 synthetic W",
                               mime="application/pdf", session_id=sid)

    birth_hits = v.find_documents("fakeadult", "birth_certificate", sid)
    assert len(birth_hits) == 1
    assert birth_hits[0]["doc_id"] == birth_id

    will_hits = v.find_documents("fakeadult", "will", sid)
    assert len(will_hits) == 1
    assert will_hits[0]["doc_id"] == will_id


def test_find_documents_owner_none_matches_any_owner(unlocked_vault):
    v, sid = unlocked_vault
    v.store_document(owner="fakeadult", doc_type="birth_certificate",
                     label="Test A", data=b"%PDF-1.4 synthetic A",
                     mime="application/pdf", session_id=sid)
    hits = v.find_documents(None, "birth_certificate", sid)
    assert len(hits) == 1


def test_find_documents_no_cross_owner_leak(unlocked_vault):
    v, sid = unlocked_vault
    v.store_document(owner="fakeadult", doc_type="birth_certificate",
                     label="Test A", data=b"%PDF-1.4 synthetic A",
                     mime="application/pdf", session_id=sid)
    hits = v.find_documents("someone_else", "birth_certificate", sid)
    assert hits == []


def test_get_document_bytes_returns_exact_bytes_and_mime(unlocked_vault):
    v, sid = unlocked_vault
    birth_id = v.store_document(owner="fakeadult", doc_type="birth_certificate",
                                label="Test A", data=b"%PDF-1.4 synthetic A",
                                mime="application/pdf", session_id=sid)
    result = v.get_document_bytes(birth_id, sid)
    assert result is not None
    data, mime, meta = result
    assert data == b"%PDF-1.4 synthetic A"
    assert mime == "application/pdf"


def test_get_document_bytes_byte_exact_non_utf8(unlocked_vault):
    """A lossy text read must not corrupt binary docs. This
    must round-trip EXACT non-UTF-8 bytes."""
    v, sid = unlocked_vault
    raw = b"\xff\xd8\xff\xe0JFIF\x00\x01\x02\xff"
    doc_id = v.store_document(owner="fakeadult", doc_type="passport",
                              label="Test Owner document",
                              data=raw, mime="image/jpeg", session_id=sid)
    data, mime, meta = v.get_document_bytes(doc_id, sid)
    assert data == raw
    assert mime == "image/jpeg"

    blob_name = meta["blob"]
    assert v.read_secure_blob_bytes(blob_name, sid) == raw


def test_store_document_rejects_svg(unlocked_vault):
    v, sid = unlocked_vault
    with pytest.raises(VaultError):
        v.store_document(owner="fakeadult", doc_type="other_vault_doc",
                         label="Test SVG", data=b"<svg></svg>",
                         mime="image/svg+xml", session_id=sid)


def test_get_document_bytes_fails_closed_after_logout(unlocked_vault):
    v, sid = unlocked_vault
    doc_id = v.store_document(owner="fakeadult", doc_type="birth_certificate",
                              label="Test A", data=b"%PDF-1.4 synthetic A",
                              mime="application/pdf", session_id=sid)
    v.logout(sid)
    with pytest.raises(VaultError):
        v.get_document_bytes(doc_id, sid)


# ── D. ephemeral overlay ttl/strict emission ─────────────────────────────

def test_ephemeral_display_ttl_strict_secure_kind(tmp_path):
    dd._STATE_PATH = str(tmp_path / "display.json")
    try:
        dd.set_ephemeral_display(kind="vault", url="/vault/view/tok123",
                                 title="Passport — Alex", ttl=45,
                                 secure=True, strict=True)
        st = dd.get_state()
        assert st["current"]["ttl"] == 45
        assert st["current"]["strict"] is True
        assert st["current"]["secure"] is True
        assert st["current"]["kind"] == "vault"
    finally:
        dd.clear_ephemeral_display()
        dd._STATE_PATH = None


def test_ephemeral_display_never_persisted_to_disk(tmp_path):
    state_path = tmp_path / "display.json"
    dd._STATE_PATH = str(state_path)
    try:
        dd.set_ephemeral_display(kind="vault", url="/vault/view/tok123",
                                 title="Passport — Alex", ttl=45,
                                 secure=True, strict=True)
        dd.get_state()
        if state_path.exists():
            text = state_path.read_text(encoding="utf-8")
            assert "tok123" not in text
    finally:
        dd.clear_ephemeral_display()
        dd._STATE_PATH = None


def test_ephemeral_display_clear_falls_back(tmp_path):
    dd._STATE_PATH = str(tmp_path / "display.json")
    try:
        dd.set_ephemeral_display(kind="vault", url="/vault/view/tok123",
                                 title="Passport — Alex", ttl=45,
                                 secure=True, strict=True)
        dd.clear_ephemeral_display()
        current = dd.get_state()["current"]
        assert current is None or current.get("kind") != "vault"
    finally:
        dd.clear_ephemeral_display()
        dd._STATE_PATH = None


# ── E. pending slot ───────────────────────────────────────────────────────

def test_pending_vault_set_and_take():
    dd.clear_pending_vault()
    dd.set_pending_vault("fakeadult's birth certificate")
    assert dd.pending_vault_active() is True
    assert dd.take_pending_vault() == "fakeadult's birth certificate"
    assert dd.take_pending_vault() is None


def test_pending_vault_expiry():
    dd.clear_pending_vault()
    original_ttl = dd._PENDING_VAULT_TTL
    dd._PENDING_VAULT_TTL = 0.05
    try:
        dd.set_pending_vault("fakeadult's birth certificate")
        time.sleep(0.1)
        assert dd.pending_vault_active() is False
    finally:
        dd._PENDING_VAULT_TTL = original_ttl
        dd.clear_pending_vault()


# ── F. access-log token redaction ─────────────────────────────────────────
# GET/DELETE /vault/view/<token> put a short-lived bearer token in the URL
# path. The production server is the werkzeug dev server, whose per-request
# access line (logged on the "werkzeug" logger, carried to journald) would
# otherwise record that token for its ~50 s life. install_access_log_redaction()
# attaches a filter to the werkzeug logger that rewrites the path to
# /vault/view/<redacted> before any handler formats the record.

import io as _io
import logging as _logging

from puffpai.logging_config import install_access_log_redaction

#: A throwaway synthetic token_urlsafe(32)-shaped string. NOT a real token.
_SYNTH_TOKEN = "Vt0k3n-ABCdef_GHIjkl-MNOpqr_STUvwx-YZ0123456"


def _emit_werkzeug_access_line(wlog, request_line, code="200", size="-"):
    """Emit a record shaped exactly as werkzeug's WSGIRequestHandler.log_request
    does: message '<addr> - - [<date>] "%s" %s %s' with the request line as the
    first deferred arg (so the token lives in record.args, not record.msg)."""
    wlog.info('%s - - [%s] "%s" %s %s',
              "127.0.0.1", "22/Aug/2026 12:00:00", request_line, code, size)


def test_access_log_redacts_vault_view_token():
    wlog = _logging.getLogger("werkzeug")
    saved_filters = list(wlog.filters)
    saved_handlers = list(wlog.handlers)
    saved_level = wlog.level
    saved_propagate = wlog.propagate
    try:
        # Idempotent + self-installing: two calls add exactly one filter.
        install_access_log_redaction()
        install_access_log_redaction()
        n = sum(1 for f in wlog.filters
                if type(f).__name__ == "_AccessLogVaultTokenFilter")
        assert n == 1

        buf = _io.StringIO()
        cap = _logging.StreamHandler(buf)
        cap.setLevel(_logging.INFO)
        cap.setFormatter(_logging.Formatter("%(message)s"))
        wlog.addHandler(cap)
        wlog.setLevel(_logging.INFO)
        wlog.propagate = False

        _emit_werkzeug_access_line(wlog, f"GET /vault/view/{_SYNTH_TOKEN} HTTP/1.1", "200")
        _emit_werkzeug_access_line(wlog, f"DELETE /vault/view/{_SYNTH_TOKEN} HTTP/1.1", "204")
        # A 404 (non-loopback/expired) still logs the path — must also redact.
        _emit_werkzeug_access_line(wlog, f"GET /vault/view/{_SYNTH_TOKEN} HTTP/1.1", "404")
        # Controls: unrelated paths, and a different vault route, stay intact.
        _emit_werkzeug_access_line(wlog, "GET /health HTTP/1.1", "200")
        _emit_werkzeug_access_line(wlog, "GET /vault/contents HTTP/1.1", "200")

        out = buf.getvalue()
        # The token never survives, on any vault-view line.
        assert _SYNTH_TOKEN not in out
        assert out.count("/vault/view/<redacted>") == 3
        # Redaction preserves the surrounding fields (method/code/size/addr).
        assert ('127.0.0.1 - - [22/Aug/2026 12:00:00] '
                '"GET /vault/view/<redacted> HTTP/1.1" 200 -') in out
        assert ('"DELETE /vault/view/<redacted> HTTP/1.1" 204 -') in out
        # Unrelated request lines are left byte-for-byte alone.
        assert "GET /health HTTP/1.1" in out
        assert "GET /vault/contents HTTP/1.1" in out
    finally:
        # Restore the process-global werkzeug logger for other tests.
        wlog.handlers[:] = saved_handlers
        wlog.filters[:] = saved_filters
        wlog.setLevel(saved_level)
        wlog.propagate = saved_propagate


def test_access_log_redaction_leaves_non_string_args_untouched():
    """The filter must pass werkzeug's non-string args (code/size can be ints)
    through unchanged rather than choking on them."""
    from puffpai.logging_config import _AccessLogVaultTokenFilter
    filt = _AccessLogVaultTokenFilter()
    rec = _logging.LogRecord(
        name="werkzeug", level=_logging.INFO, pathname=__file__, lineno=1,
        msg='%s - - [%s] "%s" %s %s',
        args=("127.0.0.1", "22/Aug/2026 12:00:00",
              f"GET /vault/view/{_SYNTH_TOKEN} HTTP/1.1", 200, 18),
        exc_info=None)
    assert filt.filter(rec) is True
    assert rec.args[3] == 200 and rec.args[4] == 18  # ints preserved
    assert _SYNTH_TOKEN not in rec.getMessage()
    assert "/vault/view/<redacted>" in rec.getMessage()


# ── G. Flask.log_exception token redaction on the "puffpai" sinks ──────────
# The werkzeug filter (section F) only covers werkzeug's own access line. A
# separate leak path is Flask.log_exception: on any unhandled 500 it does
# self.logger.error("Exception on <request.path> [<method>]", ...) on
# app.logger, which is "puffpai.api_server" — a CHILD of the "puffpai" logger,
# a DIFFERENT tree from "werkzeug". That record propagates up to the "puffpai"
# console handler (journald) AND the on-disk RotatingFileHandler, where the
# werkzeug filter never runs. setup_logging() now attaches the SAME stateless
# _AccessLogVaultTokenFilter to each "puffpai" handler, so a propagated
# /vault/view/<token> exception line is redacted in BOTH sinks. This test
# exercises both the deferred-%s-args form and the pre-formatted f-string form
# (what real Flask.log_exception emits), and confirms an ordinary log line is
# untouched.

import puffpai.logging_config as _logging_config
from puffpai.logging_config import setup_logging


def test_flask_log_exception_token_redacted_in_puffpai_sinks(tmp_path):
    puffpai_logger = _logging.getLogger("puffpai")
    saved_handlers = list(puffpai_logger.handlers)
    saved_level = puffpai_logger.level
    saved_propagate = puffpai_logger.propagate
    saved_configured = _logging_config._configured
    try:
        setup_logging("INFO", log_file=str(tmp_path / "puffpai.log"),
                      force=True)

        # Redirect the console handler to an in-memory buffer. The console
        # handler is the StreamHandler that is NOT a FileHandler — the
        # RotatingFileHandler is itself a StreamHandler subclass, so the
        # not-FileHandler guard is required to pick the right one.
        console = next(
            h for h in puffpai_logger.handlers
            if isinstance(h, _logging.StreamHandler)
            and not isinstance(h, _logging.FileHandler))
        console_buf = _io.StringIO()
        console.setStream(console_buf)

        # Flask.log_exception fires on app.logger, named "puffpai.api_server".
        api_log = _logging.getLogger("puffpai.api_server")
        # Deferred-args form (exercises the record.args branch of the filter).
        api_log.error("Exception on %s [%s]",
                      f"/vault/view/{_SYNTH_TOKEN}", "GET")
        # Pre-formatted / f-string form — the real Flask.log_exception shape
        # (exercises the record.msg branch of the filter).
        api_log.error(f"Exception on /vault/view/{_SYNTH_TOKEN} [GET]")
        # Ordinary control line that must pass through untouched.
        api_log.info("Loaded %d vault docs", 5)

        # Flush every puffpai handler, capture the console text, then CLOSE the
        # file handler before reading the log (Windows keeps the append handle
        # open; close-then-read avoids a share-mode conflict).
        for h in puffpai_logger.handlers:
            h.flush()
        console_text = console_buf.getvalue()
        for h in puffpai_logger.handlers:
            if isinstance(h, _logging.FileHandler):
                h.close()
        file_text = (tmp_path / "puffpai.log").read_text(encoding="utf-8")

        for text in (file_text, console_text):
            assert _SYNTH_TOKEN not in text
            assert text.count("/vault/view/<redacted>") == 2
            assert "Loaded 5 vault docs" in text
    finally:
        # Release the tmp file lock on Windows so pytest can clean tmp_path.
        for h in list(puffpai_logger.handlers):
            h.close()
        puffpai_logger.handlers[:] = saved_handlers
        puffpai_logger.setLevel(saved_level)
        puffpai_logger.propagate = saved_propagate
        _logging_config._configured = saved_configured
