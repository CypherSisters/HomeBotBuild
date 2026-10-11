"""The widget egress ledger's ``detail`` field must not leak the household's
coordinates.

If ``dashboard_widgets._log_egress`` wrote ``str(exc)[:200]`` raw, the
record's ``url`` would be sanitised but its ``detail`` would not: a
``requests`` exception message quotes the request URL, and these widget URLs
carry ``latitude``/``longitude`` in the query string. Every flaky fetch would
append the family's location to ``security_logs/widget_egress.jsonl``, a file
that exists precisely so egress can be audited.

Note the shape a real ``requests`` failure has: the URL appears as a BARE PATH
inside ``HTTPSConnectionPool(...): Max retries exceeded with url: /v1/forecast?
latitude=..&longitude=..`` — with no scheme — which is why stripping only
``https://...`` substrings would not catch it.
"""

import json

import pytest

from puffpai import dashboard_widgets

#: Obviously-fake coordinates, in the format the real widget URLs use.
FAKE_LAT = "41.8781"
FAKE_LON = "-87.6298"


class FakeConnectionError(Exception):
    """Stands in for requests.ConnectionError, which is not a hard dependency
    of the test environment. Only its message shape matters here."""


def _requests_style_message(short: bool = False) -> str:
    query = (f"lat={FAKE_LAT}&lon={FAKE_LON}" if short
             else f"latitude={FAKE_LAT}&longitude={FAKE_LON}")
    return ("HTTPSConnectionPool(host='api.open-meteo.com', port=443): Max "
            f"retries exceeded with url: /v1/forecast?{query} (Caused by "
            "NewConnectionError('Failed to establish a new connection'))")


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """Point the widget egress ledger at a temp file and hand it back."""
    path = tmp_path / "widget_egress.jsonl"
    monkeypatch.setattr(dashboard_widgets, "_EGRESS_LOG", str(path))
    return path


def _records(path):
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestCoordinatesNeverReachTheLedger:
    @pytest.mark.parametrize("short", [False, True])
    def test_connection_error_message_is_not_written(self, ledger, short):
        """The leaking input: a connection error quoting the query."""
        exc = FakeConnectionError(_requests_style_message(short=short))
        dashboard_widgets._log_egress(
            f"https://api.open-meteo.com/v1/forecast?latitude={FAKE_LAT}"
            f"&longitude={FAKE_LON}",
            False, str(exc))

        raw = ledger.read_text(encoding="utf-8")
        assert FAKE_LAT not in raw
        assert FAKE_LON not in raw
        assert "latitude=" not in raw
        assert "longitude=" not in raw
        assert "lat=" not in raw
        assert "lon=" not in raw

    def test_the_url_field_is_still_sanitised(self, ledger):
        dashboard_widgets._log_egress(
            f"https://api.open-meteo.com/v1/forecast?latitude={FAKE_LAT}"
            f"&longitude={FAKE_LON}",
            True, "daily hi/lo")
        record = _records(ledger)[0]
        assert record["url"] == "https://api.open-meteo.com/v1/forecast"
        assert record["detail"] == "daily hi/lo"

    def test_exception_detail_is_the_type_name_only(self):
        exc = FakeConnectionError(_requests_style_message())
        assert dashboard_widgets._exc_detail(exc) == "FakeConnectionError"

    def test_a_type_name_still_reaches_the_log(self, ledger):
        """Diagnostics must not be destroyed by the scrub: a reader still needs
        to tell a refused door from a dead network."""
        exc = FakeConnectionError(_requests_style_message())
        dashboard_widgets._log_egress(
            "https://api.open-meteo.com/v1/forecast", False,
            dashboard_widgets._exc_detail(exc))
        assert _records(ledger)[0]["detail"] == "FakeConnectionError"

    def test_useful_details_survive_untouched(self, ledger):
        for detail in ("egress door closed", "us_aqi=42", "6 items",
                       "daily hi/lo"):
            dashboard_widgets._log_egress("https://example.com/f", True, detail)
        assert [r["detail"] for r in _records(ledger)] == [
            "egress door closed", "us_aqi=42", "6 items", "daily hi/lo"]


class TestDetailScrubber:
    """Belt and braces: a future caller must not be able to reintroduce this
    by reaching for str(exc) again."""

    def test_full_url_is_reduced_to_host_and_path(self):
        out = dashboard_widgets._safe_detail(
            f"failed: https://api.open-meteo.com/v1/forecast?latitude={FAKE_LAT}")
        assert FAKE_LAT not in out
        assert "api.open-meteo.com/v1/forecast" in out

    def test_bare_query_is_dropped(self):
        out = dashboard_widgets._safe_detail(
            f"url: /v1/forecast?latitude={FAKE_LAT}&longitude={FAKE_LON}")
        assert FAKE_LAT not in out and FAKE_LON not in out
        assert "/v1/forecast" in out

    def test_credentials_in_a_url_do_not_survive(self):
        out = dashboard_widgets._safe_detail(
            "https://user:hunter2@example.com/api?key=abc")
        assert "hunter2" not in out
        assert "abc" not in out

    def test_detail_stays_bounded(self):
        assert len(dashboard_widgets._safe_detail("x" * 5000)) <= 200

    @pytest.mark.parametrize("value", [None, "", 0, [], {}])
    def test_empty_details_are_harmless(self, value):
        assert isinstance(dashboard_widgets._safe_detail(value), str)


class TestNoCallSiteStillPassesRawExceptionText:
    def test_module_has_no_str_exc_in_a_log_call(self):
        """A grep-style guard on the module itself: the pattern that caused
         must not come back."""
        import inspect
        source = inspect.getsource(dashboard_widgets)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("#", '"', "'")):
                continue
            if "_log_egress(" in stripped or "False, str(exc)" in stripped:
                assert "str(exc)" not in stripped, line
