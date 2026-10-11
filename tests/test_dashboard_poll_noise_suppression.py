"""Dashboard poll-noise suppression in the API access log.

Unfiltered, the dashboard's own poll loop makes up about 92% of puffpai-api
journal lines with nobody using the box (~249 req/min, ~358,000 requests/day
at zero user activity). journald on the appliance is volatile, so the cost is
not disk (the RuntimeMaxUse=100M cap plus puffpai-logcap.timer bound that) but
HISTORY: only ~6 hours of journal is retained, and most of it would be this
noise.

The request lines below have the exact shape ``journalctl -u puffpai-api``
shows for the dashboard pollers.
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from puffpai import logging_config  # noqa: E402


def _record(request_line, status, size='-'):
    """A record shaped exactly like werkzeug's access log line."""
    record = logging.LogRecord(
        name='werkzeug', level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s %s %s', args=(request_line, str(status), str(size)),
        exc_info=None)
    record.args = (request_line, str(status), str(size))
    return record


@pytest.fixture
def poll_filter(monkeypatch):
    monkeypatch.delenv(logging_config.QUIET_POLLS_ENV, raising=False)
    monkeypatch.delenv(logging_config.QUIET_PATHS_ENV, raising=False)
    return logging_config._AccessLogPollFilter()


# ── the core contract: 200 GET on a poll path is dropped, 500 is kept ───────

def test_200_get_on_a_poll_path_is_not_logged(poll_filter):
    record = _record('GET /apps/alarmflow/updated HTTP/1.1', 200)
    assert poll_filter.filter(record) is False


def test_500_on_the_same_poll_path_is_logged(poll_filter):
    record = _record('GET /apps/alarmflow/updated HTTP/1.1', 500)
    assert poll_filter.filter(record) is True


@pytest.mark.parametrize('request_line', [
    'GET /apps/alarmflow/updated HTTP/1.1',
    'GET /apps/freshtable/updated HTTP/1.1',
    'GET /apps/calendar/updated HTTP/1.1',
    'GET /dashboard/display HTTP/1.1',
    'GET /knowledge/suggestions/pending HTTP/1.1',
    'GET /api/media/now-playing HTTP/1.1',
    'GET /api/spotify/status HTTP/1.1',
    'GET /devices/pending HTTP/1.1',
    'GET /announcements?limit=20 HTTP/1.1',
    'GET /system/status HTTP/1.1',
    'GET /voice/state HTTP/1.1',
    'GET /voice/live HTTP/1.1',
    'GET /agents/activity?limit=20 HTTP/1.1',
    'GET /household HTTP/1.1',
])
def test_every_dashboard_poller_is_suppressed_at_200(poll_filter, request_line):
    assert poll_filter.filter(_record(request_line, 200)) is False


@pytest.mark.parametrize('status', [301, 400, 401, 403, 404, 500, 502])
def test_non_2xx_is_always_logged(poll_filter, status):
    record = _record('GET /dashboard/display HTTP/1.1', status)
    assert poll_filter.filter(record) is True


def test_post_on_a_poll_path_is_always_logged(poll_filter):
    """POST /apps/mediahub/action polls 15x/min but changes state — keep it."""
    record = _record('POST /apps/mediahub/action HTTP/1.1', 200)
    assert poll_filter.filter(record) is True


def test_delete_is_always_logged(poll_filter):
    assert poll_filter.filter(_record('DELETE /apps/alarmflow/updated HTTP/1.1', 200)) is True


def test_a_non_poll_path_is_logged(poll_filter):
    assert poll_filter.filter(_record('GET /chat/history HTTP/1.1', 200)) is True
    assert poll_filter.filter(_record('POST /chat/send HTTP/1.1', 200)) is True


def test_camera_views_keep_their_audit_line(poll_filter):
    """A camera snapshot is a privacy-relevant read: it stays in the log."""
    assert poll_filter.filter(_record('GET /ha/camera/camera.front HTTP/1.1', 200)) is True


def test_vault_view_serve_keeps_its_audit_line(poll_filter):
    """The token-bearing serve is redacted, never dropped."""
    record = _record('GET /vault/view/<redacted> HTTP/1.1', 200)
    assert poll_filter.filter(record) is True


# ── configurability ────────────────────────────────────────────────────────

def test_filter_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv(logging_config.QUIET_POLLS_ENV, '0')
    flt = logging_config._AccessLogPollFilter()
    assert flt.filter(_record('GET /apps/alarmflow/updated HTTP/1.1', 200)) is True


def test_quiet_paths_are_overridable(monkeypatch):
    monkeypatch.setenv(logging_config.QUIET_PATHS_ENV, '/only/this')
    flt = logging_config._AccessLogPollFilter()
    assert flt.filter(_record('GET /apps/alarmflow/updated HTTP/1.1', 200)) is True
    assert flt.filter(_record('GET /only/this/path HTTP/1.1', 200)) is False


def test_empty_override_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv(logging_config.QUIET_PATHS_ENV, '  ,  ')
    assert logging_config.quiet_poll_paths() == logging_config.DEFAULT_QUIET_POLL_PATHS


# ── robustness: never drop what we cannot parse ────────────────────────────

@pytest.mark.parametrize('args', [
    None,
    ('not a request line', '200', '-'),
    ('GET /apps/x/updated HTTP/1.1',),
    ('GET /apps/x/updated HTTP/1.1', 'not-a-number', '-'),
    (object(), '200', '-'),
])
def test_unparseable_records_are_kept(poll_filter, args):
    record = logging.LogRecord(name='werkzeug', level=logging.INFO,
                               pathname=__file__, lineno=1,
                               msg='%s %s %s', args=None, exc_info=None)
    record.args = args
    assert poll_filter.filter(record) is True


# ── installation wiring ────────────────────────────────────────────────────

def test_install_is_idempotent_and_ordered_after_redaction():
    werkzeug = logging.getLogger('werkzeug')
    saved = werkzeug.filters[:]
    try:
        werkzeug.filters.clear()
        logging_config.install_access_log_redaction()
        logging_config.install_access_log_redaction()
        kinds = [type(f).__name__ for f in werkzeug.filters]
        assert kinds.count('_AccessLogVaultTokenFilter') == 1
        assert kinds.count('_AccessLogPollFilter') == 1
        assert (kinds.index('_AccessLogVaultTokenFilter')
                < kinds.index('_AccessLogPollFilter')), \
            "redaction must run before suppression so survivors are redacted"
    finally:
        werkzeug.filters[:] = saved


def test_end_to_end_through_the_logger(caplog):
    """A 200 poll GET produces no record; a 500 on the same path does."""
    werkzeug = logging.getLogger('werkzeug')
    saved = werkzeug.filters[:]
    try:
        werkzeug.filters.clear()
        logging_config.install_access_log_redaction()
        with caplog.at_level(logging.INFO, logger='werkzeug'):
            werkzeug.info('%s %s %s', 'GET /apps/alarmflow/updated HTTP/1.1', '200', '-')
            werkzeug.info('%s %s %s', 'GET /apps/alarmflow/updated HTTP/1.1', '500', '-')
        messages = [r.getMessage() for r in caplog.records]
        assert not any('200' in m for m in messages), messages
        assert any('500' in m for m in messages), messages
    finally:
        werkzeug.filters[:] = saved


def test_health_keeps_its_access_line(poll_filter):
    """boot -> /health is the readiness marker; never silence it.

    The journal only retains ~6 h, so boot -> API-ready is only measurable
    while this line survives. Suppressing the /health 200 would make it
    unmeasurable for good.
    """
    assert poll_filter.filter(_record('GET /health HTTP/1.1', 200)) is True
