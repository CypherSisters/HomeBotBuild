"""The monthly map data refresh.

Covers the "Map data updates" Bridge connection (egress door
``map_data_updates``) and ``puffpai.maps_refresh``:

* the connection ships closed, everywhere;
* closed means no network call at all and status OFF;
* open, every request goes through ``egress_doors.egress_request`` (the REAL
  wrapper: switch, host list, redirects, ledger), to exactly two hosts;
* an md5 mismatch fails with nothing built; data that is not newer is skipped;
* a failed build keeps the downloads for one retry and leaves ``current``
  alone; a passed build activates, removes the downloads and records OK;
* ``GET /maps/refresh/status`` is behind the API key.

HTTP is a fake transport under the real wrapper; the two deploy scripts are a
fake runner. Nothing here reaches the network or runs Docker.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import os
import re
import socket
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from puffpai import maps_refresh as mr                      # noqa: E402
from puffpai.security import egress_doors as ed             # noqa: E402
from puffpai.security import egress_ledger as el            # noqa: E402

TWO_HOSTS = {"download.geofabrik.de", "download1.graphhopper.com"}
_REAL_SERVICE_ENV_VALUE = mr._service_env_value
ENV_VAR = "PUFFPAI_EGRESS_MAP_DATA_UPDATES"


class NetworkAttempt(BaseException):
    """A real socket or DNS lookup. BaseException on purpose: a broad
    ``except Exception`` in the code under test cannot swallow it."""


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Every test in this file: real sockets and DNS raise, and any attempt
    fails the test at teardown even if something did catch it."""
    attempts: list[str] = []

    def blocked(name):
        def _raise(*args, **_kw):
            attempts.append(f"{name}{args[:2]!r}")
            raise NetworkAttempt(f"real network call: {name}")
        return _raise

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex",
                 "create_connection"):
        monkeypatch.setattr(socket, name, blocked(name))
    monkeypatch.setattr(socket.socket, "connect", blocked("connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", blocked("connect_ex"))
    yield
    assert attempts == [], f"test reached the real network: {attempts}"
ROADS = b"R" * 200
PLACES = b"P" * 300
LIVE_META = {"release": "20261011-0200", "data_date": "2026-10-08",
             "data_timestamp": "2026-10-08T20:21:02Z"}


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, headers=None, body=b"", on_chunk=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._on_chunk = on_chunk
        self.closed = False

    def iter_content(self, size):
        step = min(size, 50)
        for i in range(0, len(self._body), step):
            if self._on_chunk:
                self._on_chunk(i)
            yield self._body[i:i + step]

    def close(self):
        self.closed = True


class Upstream:
    """The two map sites, behind the pinned-session seam of the real wrapper."""

    def __init__(self):
        roads, places = mr.SOURCES
        self.files = {
            roads.url: ROADS,
            roads.md5_url: f"{_md5(ROADS)}  texas-latest.osm.pbf\n".encode(),
            places.url: PLACES,
            places.md5_url: f"{_md5(PLACES)}  photon.jsonl.zst\n".encode(),
        }
        self.last_modified = "Thu, 05 Nov 2026 21:30:00 GMT"
        self.redirects: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.responses: list[FakeResp] = []
        self.honor_range = True
        self.on_chunk = None

    def request(self, method, url, headers=None, **kw):
        resp = self._answer(method, url, headers)
        self.responses.append(resp)
        return resp

    def _answer(self, method, url, headers):
        self.calls.append((method, url, dict(headers or {})))
        if url in self.redirects:
            return FakeResp(302, {"Location": self.redirects[url]})
        if url not in self.files:
            return FakeResp(404)
        body = self.files[url]
        if method == "HEAD":
            return FakeResp(200, {"Last-Modified": self.last_modified,
                                  "Content-Length": str(len(body))})
        hook = None if url.endswith(".md5") else self.on_chunk
        rng = (headers or {}).get("Range")
        if rng and self.honor_range:
            start = int(rng[len("bytes="):-1])
            return FakeResp(206, {
                "Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}",
                "Content-Length": str(len(body) - start)}, body[start:],
                hook)
        return FakeResp(200, {"Content-Length": str(len(body))}, body,
                        hook)

    def close(self):
        return None

    def big_gets(self):
        return [c for c in self.calls
                if c[0] == "GET" and not c[1].endswith(".md5")]


class Scripts:
    """Stands in for build_release.sh and activate_release.sh."""

    def __init__(self, root: Path, *, build_rc=0, activate="OK",
                 release="20261106-0330", data_date="2026-11-05"):
        self.root = root
        self.build_rc = build_rc
        self.activate = activate
        self.release = release
        self.data_date = data_date
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, env, timeout, log_fh):
        assert env["MAPS_ROOT"] == str(self.root)
        self.calls.append(list(argv))
        name = os.path.basename(argv[1])
        if name == "build_release.sh":
            for path in argv[2:]:
                assert os.path.isfile(path), "build got a file that is not there"
            if self.build_rc:
                (self.root / "build.status").write_text(
                    "FAILED 2026-11-06T09:00:00Z 20261106-0330 exit 1 smoke\n")
                return self.build_rc
            rel = self.root / "releases" / self.release
            rel.mkdir(parents=True)
            (rel / "META.json").write_text(json.dumps({
                "release": self.release, "data_date": self.data_date,
                "data_timestamp": f"{self.data_date}T20:00:00Z"}))
            (rel / "READY").write_text(self.release + "\n")
            (self.root / "build.status").write_text(
                f"READY 2026-11-06T10:00:00Z {self.release}\n")
            return 0
        assert name == "activate_release.sh"
        if self.activate == "OK":
            (self.root / "current" / "META.json").write_bytes(
                (self.root / "releases" / argv[2] / "META.json").read_bytes())
            (self.root / "activate.status").write_text(
                f"OK 2026-11-06T10:05:00Z {argv[2]}\n")
            return 0
        (self.root / "activate.status").write_text(
            f"{self.activate} 2026-11-06T10:05:00Z {argv[2]} -> releases/x\n")
        return 1

    def names(self):
        return [os.path.basename(c[1]) for c in self.calls]


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def box(tmp_path, monkeypatch):
    """A maps root with live data, a sandboxed switch and ledger, small files."""
    root = tmp_path / "maps"
    (root / "current").mkdir(parents=True)
    (root / "current" / "META.json").write_text(json.dumps(LIVE_META))
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "build_release.sh").write_text("# fake\n")
    (deploy / "activate_release.sh").write_text("# fake\n")
    monkeypatch.setenv("PUFFPAI_MAPS_ROOT", str(root))
    monkeypatch.setenv("PUFFPAI_MAPS_DEPLOY_DIR", str(deploy))
    monkeypatch.delenv("PUFFPAI_MAPS_DOWNLOADS", raising=False)
    monkeypatch.delenv("PUFFPAI_EGRESS_MAP_DATA_UPDATES", raising=False)
    # The REAL service-env reader, pointed EXPLICITLY at a test api.env that
    # sets nothing for this switch (a missing file would read as forced shut).
    # systemctl is the deployer's, not this test's.
    api_env = tmp_path / "api.env"
    api_env.write_text("PUFFPAI_API_KEY=not-a-real-key\n")
    monkeypatch.setenv(mr.SERVICE_ENV_FILE_VAR, str(api_env))
    monkeypatch.setattr(mr, "_unit_env_value", lambda var: None)
    monkeypatch.setattr(mr, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(mr, "SOURCES", tuple(
        dataclasses.replace(s, min_bytes=100, max_bytes=1000)
        for s in mr.SOURCES))

    overlay = tmp_path / "state" / "egress_doors.json"
    ed.set_overlay_override(str(overlay))
    monkeypatch.setattr(ed, "_raw_config", lambda: {})
    ledger = tmp_path / "egress.jsonl"
    el.set_path_override(str(ledger))
    monkeypatch.setattr(ed, "_ledger_path",
                        lambda: str(tmp_path / "egress_doors_snapshot.json"))

    upstream = Upstream()
    monkeypatch.setattr(ed, "_pinned_session", lambda d: upstream)

    class Box:
        pass

    b = Box()
    b.root, b.overlay, b.ledger, b.upstream = root, overlay, ledger, upstream
    b.scripts = Scripts(root)

    def set_switch(value):
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(json.dumps({
            "version": 1, "kind": ed.OVERLAY_KIND,
            "doors": {"map_data_updates": value}}))
        ed.reset_cache()

    def run(now=None, force=False):
        return mr.run(mr.Paths.from_env(), run_script=b.scripts, now=now,
                      force=force)

    def ledger_lines():
        # The decision ledger's lines. The outside-traffic log ("What leaves
        # your computer") writes its own line per request to the same
        # overridden file; those carry "service", not "door".
        if not ledger.exists():
            return []
        lines = [json.loads(x) for x in ledger.read_text().splitlines() if x]
        return [x for x in lines if "door" in x]

    b.set_switch, b.run, b.ledger_lines = set_switch, run, ledger_lines
    b.live_bytes = (root / "current" / "META.json").read_bytes()
    try:
        yield b
    finally:
        el.set_path_override(None)
        ed.set_overlay_override(None)
        ed.reset_cache()


def _stage_files(root: Path) -> list[str]:
    dl = root / "downloads"
    if not dl.exists():
        return []
    return sorted(p.name for p in dl.rglob("*") if p.is_file())


# ── 1. the connection ────────────────────────────────────────────────────────

def test_connection_is_declared_with_two_hosts_and_ships_closed(monkeypatch):
    monkeypatch.delenv("PUFFPAI_EGRESS_MAP_DATA_UPDATES", raising=False)
    d = ed.door("map_data_updates")
    assert d is not None
    assert set(d.hosts) == TWO_HOSTS and len(d.hosts) == 2
    assert d.default is False
    assert d.enforcement == "registry"
    assert d.config_flag == "map_data_updates"
    assert not d.dynamic_hosts and not d.derived and not d.lan
    # Code default with no config, overlay or env: closed.
    monkeypatch.setattr(ed, "_raw_config", lambda: {})
    monkeypatch.setattr(ed, "_raw_overlay", lambda: {})
    ed.reset_cache()
    assert ed.egress_allowed("map_data_updates")[0] is False


@pytest.mark.skipif(
    not ((REPO / ".git").exists()
         and (REPO / "config" / "puffpai_config.json").exists()),
    reason="repo checkout only (no .git here; installed tree)")
def test_shipped_config_carries_no_open_setting_for_the_connection():
    # Shipped config never opens it.
    shipped = json.loads((REPO / "config" / "puffpai_config.json")
                         .read_text(encoding="utf-8"))
    assert (shipped.get("egress_doors") or {}).get("map_data_updates") \
        in (None, False)


def test_connection_reads_right_on_the_screen():
    d = ed.door("map_data_updates")
    # The dashboard has no label for it and de-snakes the id.
    assert d.id.replace("_", " ").capitalize() == mr.CONNECTION_NAME
    assert d.purpose == ("Downloads new map data for the region this box was "
                         "built for (the Texas example region unless you built "
                         "another) about once a month so directions stay "
                         "current. Your questions and trips never go out.")
    for text in (d.purpose, d.data_sent, d.settings_hint):
        assert "door" not in text.lower()
    assert d.settings_hint.startswith(mr.OFF_REASON)
    row = next(r for r in ed.door_status() if r["id"] == "map_data_updates")
    assert row["writable"] is True and row["enforced"] is True
    assert row["open"] is False


@pytest.mark.skipif(not (REPO / "deploy" / "maps" / "crontab.maps-refresh").exists(),
                    reason="repo checkout only (deploy/ not in this tree)")
def test_crontab_entry_is_nightly_at_330():
    text = (REPO / "deploy" / "maps" / "crontab.maps-refresh").read_text()
    lines = [x for x in text.splitlines() if x and not x.startswith("#")]
    assert len(lines) == 1
    assert lines[0].split()[:5] == ["30", "3", "*", "*", "*"]
    assert "@HOMEBOT_DIR@/deploy/maps/refresh.sh run " in lines[0]
    assert "PUFFPAI_DATA_DIR=@DATA_DIR@ " in lines[0]
    assert "/opt/" not in lines[0] and "/data/" not in lines[0]
    assert "date" not in lines[0]      # the job decides "due", not cron


# ── 2. closed ────────────────────────────────────────────────────────────────

def test_closed_makes_no_network_call_and_records_off(box, capsys):
    result = box.run()
    assert result["result"] == "OFF"
    assert result["reason"] == mr.OFF_REASON
    assert result["data_date"] == "2026-10-08"
    assert box.upstream.calls == []
    assert box.scripts.calls == []
    assert box.ledger_lines() == []
    on_disk = json.loads((box.root / "refresh.status").read_text())
    assert on_disk["result"] == "OFF"
    # The cron entry point is quiet while off.
    assert mr.main(["run"]) == 0
    assert capsys.readouterr().out == ""
    assert box.upstream.calls == []


def test_run_now_refuses_while_closed(box, monkeypatch, capsys):
    def no_spawn(*_a, **_k):
        raise AssertionError("run-now spawned a job with the switch off")

    monkeypatch.setattr(mr.subprocess, "Popen", no_spawn)
    assert mr.main(["run-now"]) == 3
    assert mr.OFF_REASON in capsys.readouterr().out
    assert box.upstream.calls == []


def test_a_switch_forced_shut_in_the_api_unit_binds_the_job(box, monkeypatch):
    box.set_switch(True)
    monkeypatch.setattr(mr, "_service_env_value",
                        lambda var: "0" if var ==
                        "PUFFPAI_EGRESS_MAP_DATA_UPDATES" else None)
    assert box.run()["result"] == "OFF"
    assert box.upstream.calls == []


def test_not_enough_disk_fails_before_any_request(box, monkeypatch):
    box.set_switch(True)
    monkeypatch.setattr(mr, "MIN_FREE_BYTES", 10 ** 18)
    result = box.run()
    assert result["result"] == "FAILED"
    assert "disk" in result["reason"]
    assert box.upstream.calls == []


# ── 3. open: through the Bridge, two hosts only ──────────────────────────────

def test_success_goes_through_the_bridge_to_only_the_two_hosts(box,
                                                               monkeypatch):
    box.set_switch(True)
    seen: list[tuple[str, str, str]] = []
    real = ed.egress_request

    def spy(door_id, method, url, **kw):
        seen.append((door_id, method, url))
        return real(door_id, method, url, **kw)

    monkeypatch.setattr(ed, "egress_request", spy)
    result = box.run()

    assert result["result"] == "OK", result
    assert result["data_date"] == "2026-11-05"
    assert result["release"] == "20261106-0330"
    # Every request: through the wrapper, on this door, to the two hosts.
    assert seen and {s[0] for s in seen} == {"map_data_updates"}
    assert {urlsplit(c[1]).hostname for c in box.upstream.calls} == TWO_HOSTS
    for _method, _url, headers in box.upstream.calls:
        assert headers.get("User-Agent") == "PuffPAI-maps/1.0"
        assert not {"Cookie", "Authorization"} & set(headers)
    # And the Bridge ledger saw each one, under this connection's name.
    lines = box.ledger_lines()
    assert len(lines) == len(box.upstream.calls)
    assert {x["door"] for x in lines} == {"map_data_updates"}
    assert {x["result"] for x in lines} == {"allowed"}
    # Build, then activate the release the build named.
    assert box.scripts.names() == ["build_release.sh", "activate_release.sh"]
    assert box.scripts.calls[1][2] == "20261106-0330"
    # Downloads removed after the build passed; status and state written.
    assert _stage_files(box.root) == []
    on_disk = json.loads((box.root / "refresh.status").read_text())
    assert on_disk["result"] == "OK" and on_disk["last_ok"]
    state = json.loads((box.root / "refresh.state.json").read_text())
    assert state["built_md5"] == {"roads": _md5(ROADS),
                                  "places": _md5(PLACES)}


def test_any_other_host_is_refused_without_a_request(box):
    box.set_switch(True)
    with pytest.raises(ed.EgressBlocked):
        ed.egress_request("map_data_updates", "GET",
                          "https://download.openstreetmap.fr/texas.osm.pbf")
    assert box.upstream.calls == []


def test_a_redirect_off_the_two_hosts_is_refused(box):
    box.set_switch(True)
    roads = mr.SOURCES[0]
    box.upstream.redirects[roads.md5_url] = "https://mirror.example.net/t.md5"
    result = box.run()
    assert result["result"] == "FAILED"
    assert "other than the two map sites" in result["reason"]
    assert all(urlsplit(c[1]).hostname in TWO_HOSTS
               for c in box.upstream.calls)
    assert box.scripts.calls == []
    assert box.ledger_lines()[-1]["result"] == "refused"
    # The refused hop's response was released, not left open.
    hops = [r for r in box.upstream.responses if r.status_code == 302]
    assert hops and all(r.closed for r in hops)


def test_a_redirect_within_the_host_is_followed(box):
    box.set_switch(True)
    roads = mr.SOURCES[0]
    dated = "https://download.geofabrik.de/north-america/us/texas-261105.osm.pbf"
    box.upstream.files[dated] = ROADS
    box.upstream.redirects[roads.url] = dated
    assert box.run()["result"] == "OK"
    assert any(c[1] == dated for c in box.upstream.big_gets())
    hops = [r for r in box.upstream.responses if r.status_code == 302]
    assert len(hops) == 2                      # the HEAD and the GET
    assert all(r.closed for r in hops)
    assert all(r.closed for r in box.upstream.responses)


# ── 4. integrity, newness, sizes ─────────────────────────────────────────────

def test_md5_mismatch_fails_and_nothing_is_built(box):
    box.set_switch(True)
    box.upstream.files[mr.SOURCES[0].url] = b"X" * 200  # not what md5 says
    result = box.run()
    assert result["result"] == "FAILED"
    assert "checksum" in result["reason"]
    assert box.scripts.calls == []
    assert (box.root / "current" / "META.json").read_bytes() == box.live_bytes
    assert "texas-latest.osm.pbf" not in _stage_files(box.root)
    assert "texas-latest.osm.pbf.part" not in _stage_files(box.root)


def test_same_checksums_as_the_last_build_skip(box):
    box.set_switch(True)
    (box.root / "refresh.state.json").write_text(json.dumps({"built_md5": {
        "roads": _md5(ROADS), "places": _md5(PLACES)}}))
    result = box.run()
    assert result["result"] == "SKIPPED"
    assert box.upstream.big_gets() == []
    assert box.scripts.calls == []


def test_upstream_not_newer_than_the_live_data_skips(box):
    box.set_switch(True)
    box.upstream.last_modified = "Fri, 09 Oct 2026 01:10:00 GMT"
    result = box.run()
    assert result["result"] == "SKIPPED"
    assert "2026-10-08" in result["reason"]
    assert [c[0] for c in box.upstream.calls].count("HEAD") == 1
    assert box.upstream.big_gets() == []
    assert box.scripts.calls == []


def test_a_file_outside_the_size_bounds_is_refused(box):
    box.set_switch(True)
    big = b"P" * 5000
    places = mr.SOURCES[1]
    box.upstream.files[places.url] = big
    box.upstream.files[places.md5_url] = f"{_md5(big)}  x\n".encode()
    result = box.run()
    assert result["result"] == "FAILED"
    assert "unexpected size" in result["reason"]
    assert box.scripts.calls == []
    assert not any(n.startswith("photon") for n in _stage_files(box.root))


def test_a_partial_download_resumes_with_range(box):
    box.set_switch(True)
    md5s = {"roads": _md5(ROADS), "places": _md5(PLACES)}
    stage, _m = mr.prepare_stage(mr.Paths.from_env(), md5s,
                                 mr._now())
    Path(stage, "texas-latest.osm.pbf.part").write_bytes(ROADS[:120])
    assert box.run()["result"] == "OK"
    ranged = [c for c in box.upstream.big_gets() if "Range" in c[2]]
    assert [c[2]["Range"] for c in ranged] == ["bytes=120-"]


def test_switching_off_mid_download_stops_it(box, monkeypatch):
    box.set_switch(True)
    monkeypatch.setattr(mr, "RECHECK_BYTES", 100)
    box.upstream.on_chunk = lambda offset: (box.set_switch(False)
                                            if offset == 50 else None)
    result = box.run()
    assert result["result"] == "OFF"
    assert "during the download" in result["reason"]
    assert box.scripts.calls == []
    assert box.upstream.big_gets()[-1][1] == mr.SOURCES[0].url


# ── 5. build and activation ──────────────────────────────────────────────────

def test_build_failure_keeps_downloads_and_current(box):
    box.set_switch(True)
    box.scripts.build_rc = 1
    result = box.run()
    assert result["result"] == "FAILED"
    assert "could not be built" in result["reason"]
    assert "2026-10-08" in result["reason"]          # still using live data
    assert result["data_date"] == "2026-10-08"
    assert box.scripts.names() == ["build_release.sh"]  # never activated
    assert (box.root / "current" / "META.json").read_bytes() == box.live_bytes
    kept = _stage_files(box.root)
    assert "texas-latest.osm.pbf" in kept
    assert "photon-dump-usa-1.0-latest.jsonl.zst" in kept

    # The retry reuses the kept files: no second big download.
    gets_before = len(box.upstream.big_gets())
    box.scripts.build_rc = 0
    assert box.run()["result"] == "OK"
    assert len(box.upstream.big_gets()) == gets_before
    assert _stage_files(box.root) == []


def test_kept_downloads_are_reused_for_one_retry_only(box):
    box.set_switch(True)
    box.scripts.build_rc = 1
    box.run()
    box.run()
    gets_after_two = len(box.upstream.big_gets())
    assert gets_after_two == 2                     # second run reused them
    box.run()                                      # third: fetched fresh
    assert len(box.upstream.big_gets()) == 4
    assert len(list((box.root / "downloads").iterdir())) == 1


def test_activation_rolled_back_is_failed_and_reports_last_good(box):
    box.set_switch(True)
    box.scripts.activate = "ROLLED_BACK"
    result = box.run()
    assert result["result"] == "FAILED"
    assert "went back to the last good map data" in result["reason"]
    assert result["data_date"] == "2026-10-08"
    assert (box.root / "current" / "META.json").read_bytes() == box.live_bytes
    # Not recorded as built, so next month tries again.
    assert not (box.root / "refresh.state.json").exists()


# ── 6. status for Puff and the screen ────────────────────────────────────────

def test_status_reads_switch_result_and_live_date(box):
    st = mr.status()
    assert st["connection"] == {"id": "map_data_updates",
                                "name": "Map data updates", "open": False}
    assert st["result"] == "NEVER" and st["data_date"] == "2026-10-08"
    assert mr.OFF_REASON in st["summary"]
    box.set_switch(True)
    box.run()
    st = mr.status()
    assert st["connection"]["open"] is True
    assert st["result"] == "OK" and st["data_date"] == "2026-11-05"
    assert st["last_run"] and st["last_ok"]


def test_status_endpoint_requires_the_api_key(box):
    from puffpai import api_server as api
    client = api.app.test_client()
    api._rate_limit_store.clear()
    assert client.get("/maps/refresh/status").status_code == 401
    resp = client.get("/maps/refresh/status",
                      headers={"X-API-Key": api._API_KEY})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["connection"]["name"] == "Map data updates"
    assert body["connection"]["open"] is False
    assert body["data_date"] == "2026-10-08"
    assert box.upstream.calls == []


# ── 7. never a connectivity-probe target ─────────────────────────────────────

def test_connectivity_check_never_borrows_its_hosts_even_when_only_open(
        box, monkeypatch):
    """Geofabrik and GraphHopper are contacted during a refresh run and at
    no other time: not by the liveness probe, even when "Map data updates"
    is the ONLY open connection, and not when asked for by name."""
    import socket
    from puffpai.offline_guard import NetworkStatus

    monkeypatch.setattr(ed, "_raw_overlay",
                        lambda: {"map_data_updates": True})
    monkeypatch.setattr(ed, "_raw_config", lambda: {})
    ed.reset_cache()
    assert ed.egress_allowed("map_data_updates")[0] is True
    others = [d.id for d in ed.all_doors()
              if d.id != "map_data_updates" and not d.derived
              and ed.egress_allowed(d.id)[0]]
    assert others == []                       # it really is the only one
    assert ed.door("map_data_updates").probe_ok is False

    assert ed.probe_targets() == ()
    assert ed.probe_targets("map_data_updates") == ()

    def boom(*_a, **_k):
        raise AssertionError("the connectivity check opened a socket")

    monkeypatch.setattr(socket, "create_connection", boom)
    assert NetworkStatus().has_internet() is False
    assert box.upstream.calls == []


def test_connectivity_check_still_uses_an_everyday_door(box, monkeypatch):
    monkeypatch.setattr(ed, "_raw_overlay",
                        lambda: {"map_data_updates": True, "weather": True})
    monkeypatch.setattr(ed, "_raw_config", lambda: {})
    ed.reset_cache()
    targets = ed.probe_targets()
    assert targets and {src for _h, _p, src in targets} == {"weather"}
    assert not {h for h, _p, _s in targets} & TWO_HOSTS


# ── 8. no network path except the Bridge, by construction ────────────────────

_NET_MODULES = {"requests", "urllib", "urllib3", "http", "httpx", "aiohttp",
                "socket", "ssl", "asyncio", "websocket", "websockets",
                "ftplib", "smtplib", "telnetlib", "pycurl"}
_SECURITY_ALLOWED = {"egress_doors"}
#: The only egress_doors names maps_refresh may touch. egress_request is the
#: one network call; the rest open no socket.
_EGRESS_ALLOWED = {"egress_request", "egress_allowed", "reset_cache", "door",
                   "EgressBlocked"}
_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output",
                     "getoutput", "getstatusoutput"}
_ALLOWED_PROGRAMS = {"systemctl", "bash"}
#: Inside any argument handed to a subprocess: a network tool, an inline
#: script, or a raw-socket path. Catches ["bash", "-c", "curl ..."] and
#: [sys.executable, "-c", "import urllib..."] as well as the bare program.
_SHELL_NET_RE = re.compile(
    r"(?<![\w.-])(?:curl|wget|nc|ncat|socat|ssh|scp|sftp|telnet|ftp|rsync|"
    r"python[0-9.]*|perl|ruby|node|php)(?![\w.-])"
    r"|/dev/(?:tcp|udp)/|urllib|requests|http\.client|httpx|socket|"
    r"https?://|ftp://")
#: Introspection that would reach an egress_doors member without naming it.
_REFLECTION = {"getattr", "setattr", "delattr", "hasattr", "vars", "dir",
               "globals", "locals"}
#: egress_doors members maps_refresh must never reach, by name or by string.
_EGRESS_FORBIDDEN = {n for n in dir(ed) if n not in _EGRESS_ALLOWED
                     and (n.startswith("_") or "egress" in n.lower()
                          or "session" in n.lower() or "probe" in n.lower())}


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


#: The one function allowed to hand subprocess a variable argv. It refuses at
#: run time anything but bash, and every call site of it must pass a literal
#: ["bash", ...] list (checked below).
_ARGV_RUNNERS = {"_run_script"}


def _enclosing_functions(tree) -> dict:
    """id(node) -> the innermost function it sits in (ast.walk is
    breadth-first, so a nested function overwrites its parent's claim)."""
    owner: dict = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(fn):
                owner[id(child)] = fn.name
    return owner


def _string_args_offenders(call, line: int, name: str) -> list[str]:
    """Inline scripts and network tools in any string handed to *call*."""
    bad: list[str] = []
    for const in ast.walk(call):
        if not (isinstance(const, ast.Constant)
                and isinstance(const.value, str)):
            continue
        if const.value.strip() in ("-c", "--command", "-e", "--eval"):
            bad.append(f"{line}: {name} runs an inline script "
                       f"({const.value.strip()})")
        hit = _SHELL_NET_RE.search(const.value)
        if hit:
            bad.append(f"{line}: {name} argument names {hit.group(0)!r}")
    return bad


def network_offenders(source: str) -> list[str]:
    """Every construct in *source* that could reach the network other than
    ``egress_doors.egress_request``."""
    bad: list[str] = []
    tree = ast.parse(source)
    owner = _enclosing_functions(tree)
    # `egress_doors` may appear only as `egress_doors.<allowed name>`: never
    # handed around as a value (that is how getattr/aliasing sneak past).
    attribute_bases = {id(n.value) for n in ast.walk(tree)
                       if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Name) and node.id == "egress_doors" and \
                id(node) not in attribute_bases:
            bad.append(f"{line}: egress_doors used as a value")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in _EGRESS_FORBIDDEN:
            bad.append(f"{line}: string names egress_doors.{node.value}")
        if isinstance(node, ast.Call) and _dotted(node.func) in _REFLECTION:
            for arg in node.args[:1]:
                if _dotted(arg) == "egress_doors" or (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value in _EGRESS_FORBIDDEN):
                    bad.append(f"{line}: {_dotted(node.func)}() on "
                               f"egress_doors")
        if isinstance(node, ast.Call) and _dotted(node.func) in (
                "run_script", "_run_script"):
            argv = node.args[0] if node.args else None
            first = argv.elts[0] if isinstance(argv, ast.List) and \
                argv.elts else None
            if not (isinstance(first, ast.Constant) and first.value == "bash"):
                bad.append(f"{line}: run_script without a literal bash argv")
            bad.extend(_string_args_offenders(node, line, "run_script"))
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _NET_MODULES:
                    bad.append(f"{line}: import {alias.name}")
                if alias.name.startswith("puffpai.security"):
                    bad.append(f"{line}: import {alias.name} (use "
                               f"'from puffpai.security import egress_doors')")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] in _NET_MODULES:
                bad.append(f"{line}: from {mod} import ...")
            if mod == "puffpai.security":
                for alias in node.names:
                    if alias.name not in _SECURITY_ALLOWED:
                        bad.append(f"{line}: from {mod} import {alias.name}")
                    elif alias.asname and alias.asname != alias.name:
                        bad.append(f"{line}: egress_doors imported as "
                                   f"{alias.asname}")
            if mod.startswith("puffpai.security."):
                bad.append(f"{line}: from {mod} import ...")
        elif isinstance(node, ast.Attribute) and _dotted(node.value) in (
                "egress_doors",):
            if node.attr not in _EGRESS_ALLOWED:
                bad.append(f"{line}: egress_doors.{node.attr}")
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            root = name.split(".")[0]
            if root in _NET_MODULES:
                bad.append(f"{line}: call {name}")
            if name in ("__import__", "importlib.import_module", "eval",
                        "exec", "os.system", "os.popen") or \
                    name.startswith(("os.exec", "os.spawn", "os.posix_spawn")):
                bad.append(f"{line}: call {name}")
            if root == "subprocess" and name.split(".")[-1] in \
                    _SUBPROCESS_CALLS:
                argv = node.args[0] if node.args else None
                first = argv.elts[0] if isinstance(argv, ast.List) and \
                    argv.elts else None
                ok = (isinstance(first, ast.Constant)
                      and first.value in _ALLOWED_PROGRAMS) or \
                    _dotted(first) == "sys.executable" or \
                    (isinstance(argv, ast.Name)
                     and owner.get(id(node)) in _ARGV_RUNNERS)
                if not ok:
                    bad.append(f"{line}: {name} runs something other than "
                               f"systemctl, bash or this Python")
                for const in ast.walk(node):
                    if isinstance(const, ast.Constant) and isinstance(
                            const.value, str) and os.path.basename(
                            const.value) in ("curl", "wget", "nc", "ncat",
                                             "socat", "ssh", "scp", "telnet"):
                        bad.append(f"{line}: {name} runs {const.value}")
                bad.extend(_string_args_offenders(node, line, name))
    return bad


def test_maps_refresh_has_no_network_path_but_the_bridge():
    source = (REPO / "src" / "puffpai" / "maps_refresh.py").read_text(
        encoding="utf-8")
    assert network_offenders(source) == []
    # And the one allowed call is really there, on this connection.
    assert "egress_doors.egress_request(DOOR_ID" in source


@pytest.mark.parametrize("snippet", [
    "import requests\nrequests.get('https://download.geofabrik.de/x.md5', timeout=5)\n",
    "from urllib.request import urlopen\n",
    "import http.client\n",
    "import socket\nsocket.create_connection(('x', 443))\n",
    "import httpx\n",
    "__import__('requests')\n",
    "import subprocess\nsubprocess.run(['curl', '-O', 'https://x'])\n",
    "import subprocess\nsubprocess.run(['/usr/bin/wget', 'https://x'])\n",
    "import subprocess\nsubprocess.Popen(prog)\n",
    "def _other(argv):\n    import subprocess\n    subprocess.Popen(argv)\n",
    "run_script(['curl', 'https://x'], env={}, timeout=1, log_fh=None)\n",
    "run_script(argv, env={}, timeout=1, log_fh=None)\n",
    "import os\nos.system('curl https://x')\n",
    "from puffpai.security import netaddr\n",
    "from puffpai.security import egress_doors\negress_doors.egress_get('d', 'u')\n",
    "from puffpai.security import egress_doors\negress_doors._pinned_session(d)\n",
    # Inline shell or Python through an allowed program.
    "import subprocess\nsubprocess.run(['bash', '-c', 'curl https://x'])\n",
    "import subprocess\nsubprocess.run(['bash', '-c', 'exec 3<>/dev/tcp/h/443'])\n",
    "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'import urllib.request'])\n",
    "run_script(['bash', '-c', 'wget -q x'], env={}, timeout=1, log_fh=None)\n",
    # Reflection and aliasing around egress_doors.
    "from puffpai.security import egress_doors\ngetattr(egress_doors, '_pinned_session')(d)\n",
    "from puffpai.security import egress_doors\nf = getattr(egress_doors, name)\n",
    "from puffpai.security import egress_doors\nm = egress_doors\nm._pinned_session(d)\n",
    "from puffpai.security import egress_doors as ed2\ned2._pinned_session(d)\n",
    "import puffpai.security.egress_doors as x\n",
    "from puffpai.security import egress_doors\nvars(egress_doors)['egress_get']('d', 'u')\n",
    "from puffpai.security import egress_doors\negress_doors.__dict__['_pinned_session']\n",
])
def test_the_scan_catches_each_kind_of_raw_network_call(snippet):
    assert network_offenders(snippet), snippet


# ── 9. the API unit's switch override, read safely ───────────────────────────

@pytest.mark.parametrize("line,expected", [
    (f"{ENV_VAR}=0", "0"),
    ("﻿" + f"{ENV_VAR}=0", "0"),
    (f"  ﻿export {ENV_VAR}=0", "0"),
    (f"   {ENV_VAR}=0", "0"),
    (f"\t{ENV_VAR}=off", "off"),
    (f"export {ENV_VAR}=0", "0"),
    (f"  export   {ENV_VAR}=0", "0"),
    (f"{ENV_VAR} = 0", "0"),
    (f"{ENV_VAR}= 0 ", "0"),
    (f'{ENV_VAR}="0"', "0"),
    (f"{ENV_VAR}='false'", "false"),
    (f"{ENV_VAR}=0\r\n", "0"),
    (f"  export {ENV_VAR} = 'off' \r\n", "off"),
    (f"{ENV_VAR}_X=1", None),
    (f"X_{ENV_VAR}=1", None),
    (f"# {ENV_VAR}=1", None),
    ("PUFFPAI_API_KEY=not-a-real-key", None),
    ("", None),
])
def test_env_line_forms(line, expected):
    assert mr._parse_env_assignment(line, ENV_VAR) == expected


def test_env_file_last_assignment_wins_and_nothing_else_is_kept(tmp_path):
    env = tmp_path / "api.env"
    env.write_bytes(("PUFFPAI_API_KEY=not-a-real-key\r\n"
                     f"{ENV_VAR}=1\r\n"
                     "OTHER_SECRET=also-not-real\r\n"
                     f"  export {ENV_VAR} = \"0\"\r\n").encode())
    assert mr._env_file_value(ENV_VAR, str(env)) == "0"
    with pytest.raises(mr.ServiceSettingsUnreadable, match="is missing"):
        mr._env_file_value(ENV_VAR, str(tmp_path / "missing.env"))


def _use_service_env(monkeypatch, path):
    monkeypatch.setattr(mr, "_service_env_value", _REAL_SERVICE_ENV_VALUE)
    monkeypatch.setattr(mr, "_unit_env_value", lambda var: None)
    monkeypatch.setenv(mr.SERVICE_ENV_FILE_VAR, str(path))
    monkeypatch.delenv(ENV_VAR, raising=False)


@pytest.mark.parametrize("content", [
    # BOM at the start of the file (an editor saved it as UTF-8 with BOM).
    "﻿" + ENV_VAR + "=0\n",
    # BOM in front of a later line (pasted in from elsewhere).
    "PUFFPAI_API_KEY=not-a-real-key\n﻿" + ENV_VAR + "=0\r\n",
])
def test_a_byte_order_mark_cannot_hide_a_force_shut_line(box, monkeypatch,
                                                         tmp_path, content):
    box.set_switch(True)
    env = tmp_path / "bom.env"
    env.write_bytes(content.encode("utf-8"))
    _use_service_env(monkeypatch, env)
    assert box.run()["result"] == "OFF"
    assert box.upstream.calls == []


def test_a_missing_api_env_is_forced_shut_never_open(box, monkeypatch,
                                                     tmp_path, capsys):
    box.set_switch(True)
    _use_service_env(monkeypatch, tmp_path / "no-such-api.env")
    result = box.run()
    assert result["result"] == "FAILED"
    assert "service settings file" in result["reason"]
    assert "is missing" in result["reason"]
    assert box.upstream.calls == [] and box.scripts.calls == []

    def no_spawn(*_a, **_k):
        raise AssertionError("run-now spawned with the settings file missing")

    monkeypatch.setattr(mr.subprocess, "Popen", no_spawn)
    assert mr.main(["run-now"]) == 3
    assert "is missing" in capsys.readouterr().out


def test_without_the_explicit_override_the_box_file_is_read(monkeypatch,
                                                           tmp_path):
    monkeypatch.delenv(mr.SERVICE_ENV_FILE_VAR, raising=False)
    assert mr.service_env_file() == "/etc/puffpai/api.env"


def _two_env_files(monkeypatch, tmp_path, appliance, user):
    """The appliance file and a per-user install's file in the data dir."""
    monkeypatch.setattr(mr, "_unit_env_value", lambda var: None)
    monkeypatch.delenv(mr.SERVICE_ENV_FILE_VAR, raising=False)
    data = tmp_path / "data"
    data.mkdir(parents=True)
    app_file = tmp_path / "api.env"
    if appliance is not None:
        app_file.write_text(appliance)
    if user is not None:
        (data / mr.USER_ENV_FILE_NAME).write_text(user)
    monkeypatch.setattr(mr, "SERVICE_ENV_FILE", str(app_file))
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(data))


def test_the_per_user_install_env_file_is_read_too(monkeypatch, tmp_path):
    _two_env_files(monkeypatch, tmp_path, None, f"{ENV_VAR}=0\n")
    assert mr.service_env_files()[-1].endswith(mr.USER_ENV_FILE_NAME)
    assert _REAL_SERVICE_ENV_VALUE(ENV_VAR) == "0"


def test_a_force_shut_in_either_env_file_wins(monkeypatch, tmp_path):
    _two_env_files(monkeypatch, tmp_path, f"{ENV_VAR}=1\n", f"{ENV_VAR}=0\n")
    assert _REAL_SERVICE_ENV_VALUE(ENV_VAR) == "0"
    _two_env_files(monkeypatch, tmp_path / "b", f"{ENV_VAR}=off\n",
                   f"{ENV_VAR}=yes\n")
    assert _REAL_SERVICE_ENV_VALUE(ENV_VAR) == "off"


def test_no_env_file_at_all_is_forced_shut(monkeypatch, tmp_path):
    _two_env_files(monkeypatch, tmp_path, None, None)
    with pytest.raises(mr.ServiceSettingsUnreadable, match="is missing"):
        _REAL_SERVICE_ENV_VALUE(ENV_VAR)


def test_the_maps_root_follows_the_data_dir(monkeypatch, tmp_path):
    from puffpai import maps_directions as md
    monkeypatch.delenv("PUFFPAI_MAPS_ROOT", raising=False)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    assert md.maps_root() == os.path.join(str(tmp_path), "maps")
    assert mr.Paths.from_env().root == md.maps_root()
    assert md.default_meta_path() == os.path.join(
        str(tmp_path), "maps", "current", "META.json")
    monkeypatch.setenv("PUFFPAI_MAPS_ROOT", str(tmp_path / "elsewhere"))
    assert mr.Paths.from_env().root == str(tmp_path / "elsewhere")


@pytest.mark.skipif(not (REPO / "deploy" / "maps").exists(),
                    reason="repo checkout only (deploy/ not in this tree)")
def test_the_maps_scripts_name_no_fixed_install_path():
    for name in ("maps_common.sh", "refresh.sh", "build_release.sh",
                 "activate_release.sh", "docker-compose.maps.yml"):
        text = (REPO / "deploy" / "maps" / name).read_text()
        code = [x for x in text.splitlines() if not x.lstrip().startswith("#")]
        assert not any("/opt/puffpai" in x or "/data/puffpai" in x
                       for x in code), name


@pytest.mark.skipif(not (REPO / "deploy" / "maps" / "refresh.sh").exists(),
                    reason="repo checkout only (deploy/ not in this tree)")
def test_the_refresh_wrapper_never_reads_the_env_file_itself():
    wrapper = (REPO / "deploy" / "maps" / "refresh.sh").read_text()
    assert f"unset {mr.SERVICE_ENV_FILE_VAR}" in wrapper
    # The shell path never reads the file itself (no BOM or parsing to get
    # wrong there): only comments mention it.
    code = [x for x in wrapper.splitlines() if not x.lstrip().startswith("#")]
    assert not any("api.env" in x for x in code)


def test_export_form_in_api_env_forces_the_job_shut(box, monkeypatch,
                                                     tmp_path):
    box.set_switch(True)
    env = tmp_path / "api.env"
    env.write_text(f"PUFFPAI_API_KEY=not-a-real-key\n  export {ENV_VAR} = '0'\n")
    _use_service_env(monkeypatch, env)
    result = box.run()
    assert result["result"] == "OFF"
    assert box.upstream.calls == []
    assert "not-a-real-key" not in (box.root / "refresh.status").read_text()


def test_unreadable_api_env_is_forced_shut_never_open(box, monkeypatch,
                                                      tmp_path, capsys):
    box.set_switch(True)
    unreadable = tmp_path / "unreadable.env"
    unreadable.mkdir()            # exists, but open() fails on every OS
    _use_service_env(monkeypatch, unreadable)
    result = box.run()
    assert result["result"] == "FAILED"
    assert "can't read the box's service settings" in result["reason"]
    assert box.upstream.calls == []
    assert box.scripts.calls == []

    def no_spawn(*_a, **_k):
        raise AssertionError("run-now spawned with unreadable settings")

    monkeypatch.setattr(mr.subprocess, "Popen", no_spawn)
    assert mr.main(["run-now"]) == 3
    assert "can't read the box's service settings" in capsys.readouterr().out


# ── 10. nightly cron, the job decides "due" ──────────────────────────────────

def _status(box) -> dict:
    return json.loads((box.root / "refresh.status").read_text())


def test_not_due_is_quiet_local_only_and_writes_nothing(box, monkeypatch,
                                                       capsys):
    box.set_switch(True)
    first = box.run()
    assert first["result"] == "OK"
    before = (box.root / "refresh.status").read_bytes()
    calls = len(box.upstream.calls)

    def no_switch_read(var):
        raise AssertionError("a not-due night read the service settings")

    monkeypatch.setattr(mr, "_service_env_value", no_switch_read)
    later = mr._parse_iso(first["last_run"]) + timedelta(days=10)
    result = box.run(now=later)
    assert result["result"] == "SKIPPED" and result.get("quiet")
    assert len(box.upstream.calls) == calls
    assert (box.root / "refresh.status").read_bytes() == before
    assert mr.main(["run"]) == 0
    assert capsys.readouterr().out == ""


def test_due_again_28_days_after_the_last_completed_check(box):
    box.set_switch(True)
    first = box.run()
    t0 = mr._parse_iso(first["last_run"])
    calls = len(box.upstream.calls)
    assert box.run(now=t0 + timedelta(days=27, hours=23))["quiet"]
    assert len(box.upstream.calls) == calls
    again = box.run(now=t0 + timedelta(days=28, minutes=1))
    assert not again.get("quiet")
    assert len(box.upstream.calls) > calls
    # An upstream "nothing newer" is a completed check too: it starts the
    # next window, so a box does not ask upstream every night.
    assert again["result"] == "SKIPPED"
    assert again["last_checked"] == mr._iso(t0 + timedelta(days=28,
                                                           minutes=1))


def test_failures_retry_nightly_then_wait_after_five(box):
    box.set_switch(True)
    box.scripts.build_rc = 1
    t0 = mr._now()
    for night in range(5):
        result = box.run(now=t0 + timedelta(days=night))
        assert result["result"] == "FAILED", night
        assert result["fail_streak"] == night + 1
    wait_until = mr._iso(t0 + timedelta(days=28))[:10]
    assert "5 nights in a row" in result["reason"]
    assert wait_until in result["reason"]
    assert mr.status()["next_check"][:10] == wait_until

    calls = len(box.upstream.calls)
    sixth = box.run(now=t0 + timedelta(days=5))
    assert sixth["result"] == "SKIPPED" and sixth.get("quiet")
    assert len(box.upstream.calls) == calls
    assert _status(box)["fail_streak"] == 5          # status kept as it was

    box.scripts.build_rc = 0
    back = box.run(now=t0 + timedelta(days=28, minutes=1))
    assert back["result"] == "OK"
    assert back["fail_streak"] == 0
    assert len(box.upstream.calls) > calls


def test_a_new_streak_after_the_wait_starts_from_one(box):
    box.set_switch(True)
    box.scripts.build_rc = 1
    t0 = mr._now()
    for night in range(5):
        box.run(now=t0 + timedelta(days=night))
    result = box.run(now=t0 + timedelta(days=28, minutes=1))
    assert result["result"] == "FAILED"
    assert result["fail_streak"] == 1
    assert "nights in a row" not in result["reason"]


def test_run_now_skips_the_due_check_but_not_the_switch(box):
    box.set_switch(True)
    first = box.run()
    calls = len(box.upstream.calls)
    t1 = mr._parse_iso(first["last_run"]) + timedelta(days=1)
    assert box.run(now=t1)["quiet"]
    forced = box.run(now=t1, force=True)
    assert not forced.get("quiet")
    assert len(box.upstream.calls) > calls
    box.set_switch(False)
    calls = len(box.upstream.calls)
    assert box.run(now=t1 + timedelta(days=40), force=True)["result"] == "OFF"
    assert len(box.upstream.calls) == calls
