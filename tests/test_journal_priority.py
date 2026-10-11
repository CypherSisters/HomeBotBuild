"""Journald must record a warning AS a warning.



    journalctl -u puffpai-api | grep "egress REFUSED"
      -> [W] puffpai.security.egress_doors: egress REFUSED: door=<id>...
    journalctl -u puffpai-api -o json |... PRIORITY
      -> 6 (informational)
    journalctl -u puffpai-api -p warning
      -> nothing

The Python level was already WARNING; what was missing was the journald
priority. systemd files EVERY line a service writes at PRIORITY=6 unless the
line starts with a ``<N>`` syslog-level marker, so an operator filtering the
journal by priority - the ordinary way to look for warnings - saw no egress
refusals at all. Only a text grep found them.

This is severity metadata, not a gate: no refusal is added, removed, widened
or reworded by it.
"""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import logging_config as lc  # noqa: E402


@pytest.mark.parametrize("level,priority", [
    (logging.CRITICAL, 2),
    (logging.ERROR, 3),
    (logging.WARNING, 4),
    (logging.INFO, 6),
    (logging.DEBUG, 7),
])
def test_each_level_maps_to_its_syslog_priority(level, priority):
    assert lc.journal_priority(level) == priority


def test_a_custom_level_is_never_filed_as_more_severe_than_it_is():
    # 25 sits between INFO (20) and WARNING (30).
    assert lc.journal_priority(25) == lc.journal_priority(logging.INFO)


def test_a_warning_line_carries_the_journald_warning_marker():
    fmt = lc._JournalLevelFormatter(lc._CONSOLE_FORMAT)
    record = logging.LogRecord(
        "puffpai.security.egress_doors", logging.WARNING, __file__, 1,
        "egress REFUSED: door=%s host=%s caller=%s reason=%s",
        ("radio_browser", "-", "puffpai.media_hub.radio_search",
         "egress_doors.radio_browser=False"), None)
    line = fmt.format(record)
    assert line.startswith("<4>")
    assert "egress REFUSED: door=radio_browser" in line
    # The human-readable prefix that log searches match on is unchanged.
    assert "[W] puffpai.security.egress_doors:" in line


def test_an_info_line_is_still_priority_six():
    fmt = lc._JournalLevelFormatter(lc._CONSOLE_FORMAT)
    record = logging.LogRecord("puffpai", logging.INFO, __file__, 1,
                               "just a note", (), None)
    assert fmt.format(record).startswith("<6>")


def test_every_line_of_a_traceback_is_marked():
    """journald reads the marker per LINE, not per record."""
    fmt = lc._JournalLevelFormatter(lc._CONSOLE_FORMAT)
    record = logging.LogRecord("puffpai", logging.ERROR, __file__, 1,
                               "line one\nline two\nline three", (), None)
    lines = fmt.format(record).split("\n")
    assert len(lines) == 3
    assert all(x.startswith("<3>") for x in lines)


def test_the_marker_is_only_installed_under_systemd(monkeypatch):
    """In a terminal nothing parses ``<4>``; it would just be noise."""
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    plain = lc._console_formatter()
    assert not isinstance(plain, lc._JournalLevelFormatter)
    monkeypatch.setenv("JOURNAL_STREAM", "8:12345")
    assert isinstance(lc._console_formatter(), lc._JournalLevelFormatter)


def test_the_refusal_is_still_logged_at_warning(caplog):
    """The Python level itself, at the real call site - unchanged, and the
    thing the priority now agrees with."""
    logger = logging.getLogger("puffpai.security.egress_doors")
    with caplog.at_level(logging.WARNING, logger=logger.name):
        logger.warning(
            "egress REFUSED: door=%s host=%s caller=%s reason=%s",
            "radio_browser", "-", "puffpai.media_hub.radio_search",
            "egress_doors.radio_browser=False")
    matching = [r for r in caplog.records if "egress REFUSED" in r.getMessage()]
    assert len(matching) == 1
    assert matching[0].levelno == logging.WARNING
    assert lc.journal_priority(matching[0].levelno) == 4
