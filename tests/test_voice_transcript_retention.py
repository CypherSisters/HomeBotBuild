"""Voice transcript retention: the privacy contract.

Without it, ``/data/puffpai/state/logs/voice.log`` grows into megabytes of
UNROTATED, world-readable (0664) plaintext, about half of it lines like
``ignored, not addressed: '<verbatim household speech>'``: Whisper output for
conversation that was never spoken to the assistant, some of it hundreds of
characters long.

These tests pin the contract:
  1. an utterance that fails ``parse_address`` puts NO transcript text into any
     log record, in caplog or in the rotating file on disk;
  2. an addressed utterance still logs its text (the diagnostic value is kept);
  3. ``PUFF_LOG_TRANSCRIPTS=1`` flips (1) back on, and defaults OFF;
  4. voice.log rotates under a size cap instead of growing forever.

The phrasing below has the shape of such lines: ordinary household speech
that contains no wake word. No real household content is used.
"""

import importlib
import logging
import os

import pytest


# A sentence a family would actually say in a room, with no wake word in it.
# This is the exact class of text that must never be written verbatim to disk.
UNADDRESSED = "no I told him the closing is on the fourteenth and the deposit already cleared"
ADDRESSED = "hey puff turn on the kitchen light"


def _load_service(monkeypatch, **env):
    """Import puffpai.voice.service with a controlled environment.

    The module reads its config into module-level constants at import time, so
    each variation needs a reload.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import puffpai.voice.service as svc
    return importlib.reload(svc)


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PUFF_LOG_TRANSCRIPTS", raising=False)
    import puffpai.voice.service as svc
    svc = importlib.reload(svc)
    yield svc
    # Leave the module in its default state for any later test.
    monkeypatch.delenv("PUFF_LOG_TRANSCRIPTS", raising=False)
    importlib.reload(svc)


class _FakeSTT:
    """Stands in for the inline WhisperSTT: exposes the confidence attribute."""

    def __init__(self, avg_logprob=-0.42):
        self.last_avg_logprob = avg_logprob


def _all_text(records):
    """Every rendered message from a list of LogRecords, joined."""
    return "\n".join(r.getMessage() for r in records)


# ── 1. un-addressed speech leaves no text anywhere ─────────────────────────

def test_unaddressed_utterance_is_not_addressed(service):
    """Guard the premise: this sentence really does fail the address gate."""
    addressed, command = service.parse_address(UNADDRESSED)
    assert addressed is False
    assert command == ""


def test_unaddressed_logs_no_transcript_text(service, caplog):
    caplog.set_level(logging.DEBUG, logger="puffpai.voice")
    service._log_unaddressed(_FakeSTT(), UNADDRESSED)

    text = _all_text(caplog.records)
    assert UNADDRESSED not in text
    # Not just the whole sentence — no distinctive fragment of it either.
    for word in ("closing", "fourteenth", "deposit", "cleared"):
        assert word not in text, f"leaked {word!r}"


def test_unaddressed_logs_the_no_text_event(service, caplog):
    caplog.set_level(logging.INFO, logger="puffpai.voice")
    service._log_unaddressed(_FakeSTT(avg_logprob=-0.42), UNADDRESSED)

    text = _all_text(caplog.records)
    assert "wake=fired" in text
    assert "addressed=false" in text
    assert f"len={len(UNADDRESSED)}" in text
    assert "conf=-0.42" in text


def test_unaddressed_event_survives_missing_confidence(service, caplog):
    """A Vosk fallback exposes no avg_logprob; the event must still render."""
    caplog.set_level(logging.INFO, logger="puffpai.voice")

    class _NoConf:
        pass

    service._log_unaddressed(_NoConf(), UNADDRESSED)
    text = _all_text(caplog.records)
    assert "conf=n/a" in text
    assert UNADDRESSED not in text


def test_unaddressed_text_does_not_reach_the_file_on_disk(service, tmp_path, caplog):
    """The end-to-end assertion: nothing lands in voice.log itself.

    caplog only sees records; this exercises the real handler chain the service
    installs, which is where the 2.77 MB actually accumulated.
    """
    service._setup_logging()
    try:
        service._log_unaddressed(_FakeSTT(), UNADDRESSED)
        for handler in service.logger.handlers:
            handler.flush()

        log_path = tmp_path / "logs" / "voice.log"
        assert log_path.exists(), "voice.log was not created"
        on_disk = log_path.read_text(encoding="utf-8")
        assert UNADDRESSED not in on_disk
        assert "closing" not in on_disk
        assert "wake=fired addressed=false" in on_disk
    finally:
        for handler in list(service.logger.handlers):
            handler.close()
            service.logger.removeHandler(handler)


# ── 2. addressed speech is still logged in full ────────────────────────────

def test_addressed_utterance_still_logs_its_text(service, caplog):
    """The contract must not blind the log to speech that WAS meant for Puff."""
    addressed, command = service.parse_address(ADDRESSED)
    assert addressed is True

    caplog.set_level(logging.INFO, logger="puffpai.voice")
    service.logger.info("ADDRESSED: %r  (command=%r)", ADDRESSED, command)
    assert "kitchen light" in _all_text(caplog.records)


# ── 3. the opt-in env var ──────────────────────────────────────────────────

def test_transcript_logging_defaults_off(service):
    assert service.LOG_TRANSCRIPTS is False


def test_opt_in_env_var_restores_full_text(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    svc = _load_service(monkeypatch, PUFF_LOG_TRANSCRIPTS="1")
    try:
        assert svc.LOG_TRANSCRIPTS is True
        caplog.set_level(logging.DEBUG, logger="puffpai.voice")
        svc._log_unaddressed(_FakeSTT(), UNADDRESSED)
        text = _all_text(caplog.records)
        assert UNADDRESSED in text          # opted in, so the text is there
        assert "wake=fired" in text          # and the event is still emitted
    finally:
        monkeypatch.delenv("PUFF_LOG_TRANSCRIPTS", raising=False)
        importlib.reload(svc)


def test_opt_in_is_only_the_documented_value(monkeypatch, tmp_path):
    """Fail closed: anything that is not exactly "1" leaves transcripts off."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    for value in ("0", "", "true", "yes", "TRUE", "on"):
        svc = _load_service(monkeypatch, PUFF_LOG_TRANSCRIPTS=value)
        assert svc.LOG_TRANSCRIPTS is False, f"{value!r} enabled transcripts"
    monkeypatch.delenv("PUFF_LOG_TRANSCRIPTS", raising=False)
    importlib.reload(svc)


def test_opt_in_announces_itself_loudly(monkeypatch, tmp_path, caplog):
    """Enabling it must be visible in the log it is about to fill."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    svc = _load_service(monkeypatch, PUFF_LOG_TRANSCRIPTS="1")
    caplog.set_level(logging.WARNING, logger="puffpai.voice")
    try:
        svc._setup_logging()
        text = _all_text(caplog.records)
        assert "PUFF_LOG_TRANSCRIPTS=1" in text
        assert "do not enable on a family box" in text
    finally:
        for handler in list(svc.logger.handlers):
            handler.close()
            svc.logger.removeHandler(handler)
        monkeypatch.delenv("PUFF_LOG_TRANSCRIPTS", raising=False)
        importlib.reload(svc)


# ── 4. rotation caps the file ──────────────────────────────────────────────

def test_voice_log_handler_is_size_capped(service, tmp_path):
    from logging.handlers import RotatingFileHandler

    service._setup_logging()
    try:
        files = [h for h in service.logger.handlers
                 if isinstance(h, logging.FileHandler)]
        assert files, "no file handler installed"
        handler = files[0]
        assert isinstance(handler, RotatingFileHandler), \
            "voice.log must rotate; a plain FileHandler grows without bound"
        assert 0 < handler.maxBytes <= 5 * 1024 * 1024
        assert 0 < handler.backupCount <= 3
    finally:
        for handler in list(service.logger.handlers):
            handler.close()
            service.logger.removeHandler(handler)


def test_voice_log_rotation_actually_caps_total_size(monkeypatch, tmp_path):
    """Write far past the cap and confirm the bytes on disk stay bounded."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PUFF_LOG_TRANSCRIPTS", raising=False)
    svc = _load_service(monkeypatch,
                        PUFF_VOICE_LOG_MAX_BYTES="4096",
                        PUFF_VOICE_LOG_BACKUPS="2")
    svc._setup_logging()
    try:
        for _ in range(2000):
            svc.logger.info("wake=fired addressed=false len=57 conf=-0.42")
        for handler in svc.logger.handlers:
            handler.flush()

        log_dir = tmp_path / "logs"
        written = sorted(log_dir.glob("voice.log*"))
        # 1 active + at most 2 backups, each capped at ~4 KB.
        assert len(written) <= 3, [p.name for p in written]
        total = sum(p.stat().st_size for p in written)
        assert total < 32 * 1024, f"rotation did not cap the log: {total} bytes"
        # And rotation really happened — 2000 lines would be ~100 KB unrotated.
        assert len(written) > 1, "expected at least one rollover"
    finally:
        for handler in list(svc.logger.handlers):
            handler.close()
            svc.logger.removeHandler(handler)
        for key in ("PUFF_VOICE_LOG_MAX_BYTES", "PUFF_VOICE_LOG_BACKUPS"):
            monkeypatch.delenv(key, raising=False)
        importlib.reload(svc)


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX file modes; the appliance is Linux")
def test_voice_log_is_not_world_readable(service, tmp_path):
    """0664 would make household speech group- and world-readable."""
    import stat

    service._setup_logging()
    try:
        log_path = tmp_path / "logs" / "voice.log"
        service.logger.info("wake=fired addressed=false len=0 conf=n/a")
        for handler in service.logger.handlers:
            handler.flush()
        mode = stat.S_IMODE(log_path.stat().st_mode)
        assert not mode & stat.S_IROTH, f"world-readable: {oct(mode)}"
        assert not mode & stat.S_IWOTH, f"world-writable: {oct(mode)}"
    finally:
        for handler in list(service.logger.handlers):
            handler.close()
            service.logger.removeHandler(handler)


# ── the shared rotation helper ─────────────────────────────────────────────

def test_make_rotating_file_handler_defaults(tmp_path):
    from logging.handlers import RotatingFileHandler

    from puffpai.logging_config import make_rotating_file_handler

    path = tmp_path / "x.log"
    handler = make_rotating_file_handler(str(path), max_bytes=1024, backup_count=1)
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 1024
        assert handler.backupCount == 1
    finally:
        handler.close()
