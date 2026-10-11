"""The egress ledger, and the connectivity probe.

Two properties:

* **An audit trail.** A *last-used snapshot*, one entry per door overwritten by
  the next decision, leaves door refusals and in-process ``EgressBlocked``
  with no durable record, and without a journal line for a refusal an owner
  has no way to answer "did this appliance try to phone home". So every
  decision is appended to a ledger.
* **A probe that stays home.** ``offline_guard.has_internet`` must not open TCP
  to fixed resolvers or search hosts (``1.1.1.1:53``, ``8.8.8.8:53``,
  ``www.google.com:443``) behind a ``connectivity_probe`` door that reads
  ``open=false``: those are sockets to parties nobody in the household chose,
  from a box that advertises that it never needs the cloud.

What is asserted here, in the order it matters:

1. a refusal writes a ledger line **and** one journal WARNING;
2. a ledger line never carries a query string, a body, a header or a token;
3. the ledger is size-capped and rotates, so it cannot flood the data partition;
4. a fully shut appliance opens **zero** sockets when asked "are we online?" —
   proven by making every socket primitive in the process fatal;
5. the probe only ever reaches a host an already-open door contacts;
6. ``egress_doors.connectivity_probe: false`` is a hard off;
7. ``/system/status`` says "NOT ENFORCED" in words for an unenforced door.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.security import egress_doors as ed          # noqa: E402
from puffpai.security import egress_ledger as el         # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No config, no environment, and a ledger that lives in tmp_path."""
    for d in ed.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    ed.reset_cache()
    ed._LAST_USED.clear()
    ed._LAST_REFUSED.clear()
    ed._ledger_loaded = False
    monkeypatch.setattr(ed, "_ledger_path",
                        lambda: str(tmp_path / "egress_doors.json"))
    el.set_path_override(str(tmp_path / "egress.jsonl"))
    yield
    el.set_path_override(None)
    ed.reset_cache()
    ed._LAST_USED.clear()
    ed._LAST_REFUSED.clear()
    ed._ledger_loaded = False


def _use_config(monkeypatch, payload: dict) -> None:
    """Make *payload* the WHOLE of this appliance's egress configuration.

    The overlay tier OUTRANKS the config file, so neutralising only
    ``_raw_config`` would leave conftest's seeded Toolsmith doors (or a
    developer's stray local overlay) deciding the answer — and these tests are
    about what happens when nothing is set anywhere.
    """
    written = json.dumps(payload)
    monkeypatch.setattr(ed, "_raw_config", lambda: json.loads(written))
    monkeypatch.setattr(ed, "_raw_overlay", lambda: {})


class _SocketOpened(AssertionError):
    """Raised if anything in the process tries to reach the network."""


@pytest.fixture
def no_sockets(monkeypatch):
    def boom(*_a, **_kw):
        raise _SocketOpened("a socket was opened")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    return boom


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, url="", body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self.request = types.SimpleNamespace(body=body)

    def json(self):
        return {}


@pytest.fixture
def fake_requests(monkeypatch):
    calls: list = []
    responses: list = []

    def request(method, url, **kw):
        calls.append((method, url, kw))
        return responses.pop(0) if responses else _FakeResponse(url=url)

    class _Session:
        """Stands in for the pinned session ``egress_request`` builds.

        The pin itself is transport-level and has its own tests
        (``test_security_hostname_not_destination.py``); here the point is only that the
        wrapper's door/host/redirect logic is exercised without a socket.
        """

        def request(self, method, url, **kw):
            # Through the module object, not the closure: tests repoint
            # ``fake_requests.request`` to simulate a transport failure.
            return mod.request(method, url, **kw)

        def close(self):
            return None

    monkeypatch.setattr(ed, "_pinned_session", lambda d: _Session())

    mod = types.ModuleType("requests")
    mod.request = request
    mod.compat = types.SimpleNamespace(
        urljoin=__import__("urllib.parse", fromlist=["urljoin"]).urljoin)
    monkeypatch.setitem(sys.modules, "requests", mod)
    mod.calls = calls          # type: ignore[attr-defined]
    mod.responses = responses  # type: ignore[attr-defined]
    return mod


def _lines() -> list:
    return el.read_records(limit=1000)


# ── 1. a refusal is recorded, durably and in the journal ─────────────────────

def test_a_refused_door_writes_a_ledger_line(monkeypatch, no_sockets):
    """One refusal leaves one durable trace."""
    _use_config(monkeypatch, {})
    with pytest.raises(ed.EgressBlocked):
        ed.require_open("radio_browser")

    rows = _lines()
    assert len(rows) == 1
    row = rows[0]
    assert row["door"] == "radio_browser"
    assert row["result"] == "refused"
    assert row["kind"] == "door_check"
    assert "radio_browser" in row["reason"]
    assert row["ts"]
    # The door's third party is carried on the line, so the record is readable
    # without cross-referencing the registry (empty when it names no party).
    assert "third_party" in row


def test_four_refusals_leave_four_lines(monkeypatch, no_sockets):
    """A snapshot keeps one entry per door; an audit trail keeps every event.

    This is the whole difference the ledger is about: four refusals must not
    collapse into a single overwritten record.
    """
    _use_config(monkeypatch, {})
    for _ in range(4):
        with pytest.raises(ed.EgressBlocked):
            ed.require_open("radio_browser")
    assert len([r for r in _lines() if r["result"] == "refused"]) == 4


def test_a_refusal_logs_one_journal_warning(monkeypatch, caplog, no_sockets):
    _use_config(monkeypatch, {})
    with caplog.at_level(logging.WARNING, logger="puffpai.security.egress_doors"):
        with pytest.raises(ed.EgressBlocked):
            ed.require_open("radio_browser")
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "egress REFUSED" in text
    assert "door=radio_browser" in text


def test_an_allowed_call_does_not_warn(monkeypatch, caplog, fake_requests):
    """A weather poll every few minutes must not fill the journal."""
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    with caplog.at_level(logging.WARNING, logger="puffpai.security.egress_doors"):
        ed.egress_get("weather", "https://api.open-meteo.com/v1/forecast")
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_last_refused_survives_a_later_success(monkeypatch, fake_requests):
    """A door refused at 02:00 and allowed at 02:05 still shows the refusal."""
    _use_config(monkeypatch, {"egress_doors": {}})
    with pytest.raises(ed.EgressBlocked):
        ed.require_open("weather")
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    ed.egress_get("weather", "https://api.open-meteo.com/v1/forecast")

    assert ed.last_used("weather")["result"] == "allowed"
    refused = ed.last_refused("weather")
    assert refused and refused["result"] == "refused"

    row = [r for r in ed.door_status() if r["id"] == "weather"][0]
    assert row["last_refused"]["result"] == "refused"
    assert row["last_used"]["result"] == "allowed"


def test_an_undeclared_host_is_recorded_not_only_raised(monkeypatch,
                                                        no_sockets):
    """The most interesting line an owner can find: 'it tried to reach X'."""
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    with pytest.raises(ed.EgressBlocked):
        ed.egress_get("news_rss", "https://attacker.example/feed.xml")
    rows = [r for r in _lines() if r["result"] == "refused"]
    assert rows and rows[-1]["host"] == "attacker.example"
    assert rows[-1]["kind"] == "url_check"


# ── 2. the ledger never carries content ──────────────────────────────────────

def test_no_query_string_reaches_the_ledger(monkeypatch, fake_requests):
    """A query carries data — a search term, a place name, coordinates."""
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    ed.egress_get(
        "weather",
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=39.7817&longitude=-89.6501&secret=hunter2")
    blob = json.dumps(_lines())
    assert "?" not in blob
    assert "latitude" not in blob
    assert "hunter2" not in blob
    assert "39.7817" not in blob
    row = _lines()[-1]
    assert row["host"] == "api.open-meteo.com"
    assert row["path"] == "/v1/forecast"


def test_url_credentials_never_reach_the_ledger(monkeypatch, no_sockets):
    _use_config(monkeypatch, {"egress_doors": {"news_rss": True}})
    with pytest.raises(ed.EgressBlocked):
        ed.egress_get("news_rss",
                      "https://user:s3cr3t@feeds.npr.org/1001/rss.xml")
    blob = json.dumps(_lines())
    assert "s3cr3t" not in blob
    assert "user:" not in blob


def test_bytes_out_is_a_length_not_a_body(monkeypatch, fake_requests):
    payload = b'{"household":"secret"}'
    fake_requests.responses.append(          # type: ignore[attr-defined]
        _FakeResponse(headers={"Content-Length": "42"}, body=payload))
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    ed.egress_request("weather", "POST",
                      "https://api.open-meteo.com/v1/forecast", data=payload)
    row = _lines()[-1]
    assert row["bytes_out"] == len(payload)
    assert row["bytes_in"] == 42
    assert "household" not in json.dumps(_lines())


def test_the_ledger_names_the_caller(monkeypatch, no_sockets):
    """'Which part of the code wanted this' is half the value of the trail."""
    _use_config(monkeypatch, {})
    with pytest.raises(ed.EgressBlocked):
        ed.require_open("geoip")
    caller = _lines()[-1]["caller"]
    assert caller.endswith("test_the_ledger_names_the_caller")
    # Never the registry's own frames.
    assert "egress_doors" not in caller
    assert "egress_ledger" not in caller


@pytest.mark.parametrize("url,expected", [
    ("https://a.example/p?q=secret#frag", ("a.example", "/p")),
    ("https://u:p@a.example/p", ("a.example", "/p")),
    ("https://A.Example./P", ("a.example", "/P")),
    ("not a url", ("", "")),
    (None, ("", "")),
])
def test_safe_parts_discards_everything_that_carries_data(url, expected):
    assert el.safe_parts(url) == expected


# ── 3. size cap and rotation ─────────────────────────────────────────────────

def test_the_ledger_rotates_and_is_capped(monkeypatch, tmp_path):
    """A door being hammered costs the oldest generation, never the partition."""
    monkeypatch.setattr(el, "MAX_BYTES", 2048)
    path = Path(el.ledger_path())
    filler = "x" * 400
    for _ in range(400):
        el.append({"door": "weather", "result": "allowed", "pad": filler})

    assert path.exists()
    assert path.stat().st_size < el.MAX_BYTES + 4096
    generations = sorted(p.name for p in tmp_path.glob("egress.*.jsonl"))
    assert generations, "nothing rotated"
    assert len(generations) <= el.BACKUP_COUNT
    total = sum(p.stat().st_size for p in tmp_path.glob("egress*.jsonl"))
    assert total <= el.MAX_BYTES * (el.BACKUP_COUNT + 1) + 4096


def test_an_unwritable_ledger_does_not_break_a_refusal(monkeypatch,
                                                       no_sockets, tmp_path):
    """Losing a line loses evidence, not enforcement."""
    el.set_path_override(str(tmp_path / "nope" / "egress.jsonl"))

    def boom(*_a, **_kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(el.os, "makedirs", boom)
    _use_config(monkeypatch, {})
    with pytest.raises(ed.EgressBlocked):
        ed.require_open("radio_browser")   # still refuses, which is the point


# ── 4. a shut box opens zero sockets ─────────────────────────────────────────

def test_has_internet_opens_no_socket_with_every_door_shut(monkeypatch,
                                                           no_sockets):
    """The claim the appliance makes about itself, made testable.

    An unconditional ``has_internet`` would dial Cloudflare, Google DNS and
    google.com. ``no_sockets`` makes every socket primitive
    raise, so if anything is dialled this test errors rather than fails.
    """
    from puffpai.offline_guard import NetworkStatus

    _use_config(monkeypatch, {})
    assert ed.probe_targets() == ()
    assert NetworkStatus().has_internet() is False


@pytest.mark.parametrize("banned", ["1.1.1.1", "8.8.8.8", "www.google.com"])
def test_no_door_declares_a_public_resolver_any_more(banned):
    for d in ed.DOORS:
        assert banned not in d.hosts, (
            f"door {d.id} still names {banned}; the probe must borrow an open "
            f"door's host, not keep resolvers of its own")


def test_the_probe_only_reaches_hosts_an_open_door_already_reaches(monkeypatch):
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    targets = ed.probe_targets()
    assert targets
    weather = ed.door("weather")
    for host, port, source in targets:
        assert source == "weather"
        assert host in weather.hosts
        assert port == ed.PROBE_PORT


def test_the_probe_prefers_the_callers_own_door(monkeypatch):
    _use_config(monkeypatch,
                {"egress_doors": {"weather": True, "news_rss": True}})
    assert ed.probe_targets("news_rss")[0][2] == "news_rss"
    assert ed.probe_targets("weather")[0][2] == "weather"


def test_has_internet_probes_the_open_door(monkeypatch):
    from puffpai.offline_guard import NetworkStatus

    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    status = NetworkStatus()
    seen: list = []

    def fake_check(host, port, timeout):
        seen.append((host, port))
        return True

    monkeypatch.setattr(status, "_check_connection", fake_check)
    assert status.has_internet(door_id="weather") is True
    assert seen == [("api.open-meteo.com", ed.PROBE_PORT)]


def test_the_probe_flag_false_is_a_hard_off(monkeypatch, no_sockets):
    """An owner who wants zero probing gets zero probing, whatever else is on."""
    from puffpai.offline_guard import NetworkStatus

    _use_config(monkeypatch, {"egress_doors": {"weather": True,
                                               "connectivity_probe": False}})
    assert ed.probe_targets() == ()
    assert NetworkStatus().has_internet() is False
    row = [r for r in ed.door_status() if r["id"] == "connectivity_probe"][0]
    assert row["open"] is False
    assert "switched off" in row["reason"]


def test_env_can_force_the_probe_off(monkeypatch, no_sockets):
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    monkeypatch.setenv("PUFFPAI_EGRESS_CONNECTIVITY_PROBE", "0")
    assert ed.probe_targets() == ()


def test_the_probe_door_reports_honestly_in_status(monkeypatch):
    """It must not read 'closed' while it probes, nor 'open' while it cannot."""
    _use_config(monkeypatch, {})
    shut = [r for r in ed.door_status() if r["id"] == "connectivity_probe"][0]
    assert shut["open"] is False
    assert shut["hosts"] == []
    assert "contacts nobody" in shut["reason"]

    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    live = [r for r in ed.door_status() if r["id"] == "connectivity_probe"][0]
    assert live["open"] is True
    assert live["enforced"] is True
    assert any("api.open-meteo.com" in h for h in live["hosts"])


def test_an_unimportable_registry_makes_the_probe_fail_shut(monkeypatch,
                                                            no_sockets):
    """Fail closed: a broken registry must not restore the old resolver dial."""
    from puffpai.offline_guard import NetworkStatus

    monkeypatch.setitem(sys.modules, "puffpai.security.egress_doors", None)
    assert NetworkStatus().has_internet() is False


# ── 5. the unenforced doors say so in words ──────────────────────────────────

def test_unenforced_doors_say_not_enforced_in_plain_words(monkeypatch):
    """``enforced: false`` beside eleven other fields is not a disclosure.

    No built-in entry is unenforced, so a synthetic one stands in for the next
    path that is listed before it can be gated.
    """
    import dataclasses
    assert [d.id for d in ed.DOORS if d.enforcement == "none"] == []
    listed = dataclasses.replace(
        ed.door("news_rss"), id="listed_only", config_flag="listed_only",
        env_var="PUFFPAI_EGRESS_LISTED_ONLY", enforcement="none",
        hosts=("listed-only.example.org",))
    monkeypatch.setattr(ed, "DOORS", ed.DOORS + (listed,))
    monkeypatch.setitem(ed._BY_ID, listed.id, listed)
    door_id = listed.id
    _use_config(monkeypatch, {})
    row = [r for r in ed.door_status() if r["id"] == door_id][0]
    assert row["enforced"] is False
    assert row["enforcement"] == "none"
    note = row["enforcement_note"]
    assert "NOT ENFORCED" in note
    assert "the switch does not shut it" in note


def test_every_door_row_carries_an_enforcement_note(monkeypatch):
    _use_config(monkeypatch, {})
    for row in ed.door_status():
        assert row["enforcement_note"], f"{row['id']} does not say who stops it"
        assert "last_refused" in row


def test_a_transport_failure_is_recorded_without_the_url(monkeypatch,
                                                         fake_requests):
    """A requests error message quotes the whole URL; only its type is kept."""
    def boom(method, url, **_kw):
        raise RuntimeError(f"failed to connect to {url}")

    monkeypatch.setattr(fake_requests, "request", boom)
    _use_config(monkeypatch, {"egress_doors": {"weather": True}})
    with pytest.raises(RuntimeError):
        ed.egress_get("weather",
                      "https://api.open-meteo.com/v1/forecast?lat=39.7817")
    row = _lines()[-1]
    assert row["kind"] == "request_failed"
    assert row["host"] == "api.open-meteo.com"
    assert "RuntimeError" in row["reason"]
    blob = json.dumps(_lines())
    assert "39.7817" not in blob and "?" not in blob
