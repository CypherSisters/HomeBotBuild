"""Knowledge pack downloads: the switch, Kiwix's redirect chain, its checksum.

Every test here runs on a fake transport and a fake resolver: nothing reaches
the network. The fake answers the way Kiwix does: ``download.kiwix.org``
redirects to ``lb.download.kiwix.org``, the balancer redirects to the dated
file, the dated file redirects to a mirror, and the balancer serves the
``.sha256`` for the dated file itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import source_fetch as sf  # noqa: E402
from puffpai import source_registry  # noqa: E402
from puffpai.security import egress_doors as ed  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SOURCE_ID = "kiwix_wikem_maxi"
ORIGIN = "https://download.kiwix.org/zim/wikem_en_all_maxi.zim"
LB_SAME = "https://lb.download.kiwix.org/zim/wikem_en_all_maxi.zim"
LB_DATED = "https://lb.download.kiwix.org/zim/other/wikem_en_all_maxi_2026-07.zim"
MIRROR = "https://ftp.nluug.nl/pub/kiwix/zim/other/wikem_en_all_maxi_2026-07.zim"
BODY = b"ZIM\x04" + b"x" * 4096
POSTURE = {"build_network": "online", "run_egress": "island"}


class _Resp:
    def __init__(self, status, body=b"", location=None):
        self.status = status
        self._body = body
        self.headers = {}
        if location:
            self.headers["Location"] = location
        if status == 200:
            self.headers["Content-Length"] = str(len(body))
        self._pos = 0
        self.closed = False

    def read(self, amt=-1):
        if amt is None or amt < 0:
            amt = len(self._body)
        chunk = self._body[self._pos:self._pos + amt]
        self._pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


class _Kiwix:
    """A fake Kiwix: a route table from URL to response, and a call log."""

    def __init__(self, body=BODY, digest=None, mirror=MIRROR):
        digest = digest or hashlib.sha256(body).hexdigest()
        self.calls = []
        self.routes = {
            "https://download.kiwix.org/robots.txt": (404, b"", None),
            ORIGIN: (301, b"", LB_SAME),
            LB_SAME: (301, b"", LB_DATED),
            LB_DATED: (302, b"", mirror),
            mirror: (200, body, None),
            ORIGIN + ".sha256": (301, b"", LB_SAME + ".sha256"),
            LB_SAME + ".sha256": (301, b"", LB_DATED + ".sha256"),
            LB_DATED + ".sha256": (
                200, f"{digest}  wikem_en_all_maxi_2026-07.zim\n".encode(), None),
        }

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if url not in self.routes:
            return _Resp(404)
        status, body, location = self.routes[url]
        return _Resp(status, body, location)


def _public(_host):
    return ["93.184.216.34"]


@pytest.fixture
def switch_open(monkeypatch):
    door = ed.door(sf.DOOR_ID)
    monkeypatch.setenv(door.env_var, "1")
    ed.reset_cache()
    sf.clear_caches()
    yield
    ed.reset_cache()
    sf.clear_caches()


def _fetch(kiwix, tmp_path, **kw):
    return sf.fetch(SOURCE_ID, POSTURE, size_acknowledged=True,
                    dest_dir=str(tmp_path), transport=kiwix, resolver=_public,
                    sleeper=lambda s: None, **kw)


# ── the switch ───────────────────────────────────────────────────────────────

def test_the_downloads_switch_ships_closed():
    door = ed.door(sf.DOOR_ID)
    assert door is not None and door.default is False
    assert door.enforcement == "registry" and door.probe_ok is False


def test_a_shut_switch_downloads_nothing_and_opens_no_socket(monkeypatch, tmp_path):
    monkeypatch.delenv(ed.door(sf.DOOR_ID).env_var, raising=False)
    ed.reset_cache()
    kiwix = _Kiwix()
    out = _fetch(kiwix, tmp_path)
    assert out["ok"] is False and out["status"] == "switched_off"
    assert "knowledge_pack_downloads" in out["reason"]
    assert kiwix.calls == []
    assert os.listdir(tmp_path) == []


def test_the_default_transport_goes_through_the_switch(monkeypatch):
    seen = {}

    class _R:
        status_code = 302
        headers = {"Location": LB_SAME}

    def fake_request(door_id, method, url, **kw):
        seen.update(door=door_id, method=method, url=url, **kw)
        return _R()

    monkeypatch.setattr(ed, "egress_request", fake_request)
    resp = sf._default_transport(ORIGIN, {"User-Agent": sf.USER_AGENT}, 5.0)
    assert seen["door"] == sf.DOOR_ID and seen["method"] == "GET"
    assert seen["follow_redirects"] is False and seen["stream"] is True
    assert resp.status == 302


def test_the_switch_declares_every_host_the_fetcher_can_reach():
    door = ed.door(sf.DOOR_ID)
    assert sf.KIWIX_HOSTS <= set(door.hosts)
    for row in source_registry.load_registry().get("sources", []):
        host = (row.get("source_url") or "").split("/")[2].lower()
        assert host in door.hosts, row.get("id")


# ── the chain ────────────────────────────────────────────────────────────────

def test_a_kiwix_pack_follows_the_chain_and_installs(switch_open, tmp_path):
    kiwix = _Kiwix()
    out = _fetch(kiwix, tmp_path)
    assert out["ok"] is True, out["reason"]
    assert out["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert out["provenance"]["final_url"] == MIRROR
    assert out["provenance"]["sha256_published"] is True
    assert (tmp_path / "wikem_en_all_maxi.zim").read_bytes() == BODY
    # The checksum is read first, and only from Kiwix's own servers.
    assert kiwix.calls.index(LB_DATED + ".sha256") < kiwix.calls.index(MIRROR)
    checksum_hosts = {u.split("/")[2] for u in kiwix.calls if u.endswith(".sha256")}
    assert checksum_hosts <= {sf.KIWIX_ORIGIN, sf.KIWIX_LB}


@pytest.mark.parametrize("mirror", [
    "https://ftp.nluug.nl/pub/kiwix/zim/other/wikem_en_all_maxi_2026-07.zim",
    "https://mirror.download.kiwix.org/zim/other/wikem_en_all_maxi_2026-07.zim",
    "https://dumps.wikimedia.org/kiwix/zim/other/wikem_en_all_maxi_2026-07.zim",
])
def test_every_listed_mirror_is_accepted_at_its_own_prefix(switch_open, tmp_path, mirror):
    out = _fetch(_Kiwix(mirror=mirror), tmp_path)
    assert out["ok"] is True, out["reason"]


@pytest.mark.parametrize("bad", [
    "https://evil.example/zim/other/wikem_en_all_maxi_2026-07.zim",
    "https://download.kiwix.org.evil.example/zim/other/wikem_en_all_maxi_2026-07.zim",
    "https://github.com/zim/other/wikem_en_all_maxi_2026-07.zim",
])
def test_a_redirect_to_a_non_kiwix_host_is_refused(switch_open, tmp_path, bad):
    kiwix = _Kiwix(mirror=bad)
    out = _fetch(kiwix, tmp_path)
    assert out["ok"] is False
    assert "not on Kiwix's download chain" in out["reason"]
    assert bad not in kiwix.calls, "the refused host must never be asked"
    assert not (tmp_path / "wikem_en_all_maxi.zim").exists()


@pytest.mark.parametrize("bad", [
    # A listed mirror, but not serving the path the balancer named.
    "https://ftp.nluug.nl/pub/kiwix/zim/other/something_else.zim",
    "https://ftp.nluug.nl/zim/other/wikem_en_all_maxi_2026-07.zim",
    # A listed mirror over plain http, or on another port, or with a query.
    "http://ftp.nluug.nl/pub/kiwix/zim/other/wikem_en_all_maxi_2026-07.zim",
    "https://ftp.nluug.nl:8443/pub/kiwix/zim/other/wikem_en_all_maxi_2026-07.zim",
    "https://ftp.nluug.nl/pub/kiwix/zim/other/wikem_en_all_maxi_2026-07.zim?x=1",
])
def test_a_mirror_must_serve_exactly_the_balancers_path(switch_open, tmp_path, bad):
    out = _fetch(_Kiwix(mirror=bad), tmp_path)
    assert out["ok"] is False
    assert not (tmp_path / "wikem_en_all_maxi.zim").exists()


def test_a_mirror_never_redirects_onward(switch_open, tmp_path):
    kiwix = _Kiwix()
    kiwix.routes[MIRROR] = (302, b"", "https://dumps.wikimedia.org/kiwix/zim/other/"
                                      "wikem_en_all_maxi_2026-07.zim")
    out = _fetch(kiwix, tmp_path)
    assert out["ok"] is False and "download chain" in out["reason"]


@pytest.mark.parametrize("cur, nxt, ok", [
    (ORIGIN, LB_SAME, True),
    (LB_SAME, LB_DATED, True),
    (LB_DATED, MIRROR, True),
    (ORIGIN, MIRROR, False),                       # the balancer is not skipped
    (ORIGIN, ORIGIN.replace("wikem", "other"), False),
    (LB_DATED, "https://lb.download.kiwix.org/zim/../etc/x.zim", False),
    (LB_DATED, LB_DATED + ".sha256", False),        # the file kind never changes
    (MIRROR, LB_DATED, False),                      # nothing after a mirror
])
def test_the_hop_rule(cur, nxt, ok):
    assert sf.kiwix_hop_allowed(cur, nxt)[0] is ok


# ── the checksum and the size ────────────────────────────────────────────────

def test_a_digest_that_does_not_match_kiwixs_is_set_aside(switch_open, tmp_path):
    out = _fetch(_Kiwix(digest="0" * 64), tmp_path)
    assert out["ok"] is False and out["status"] == "checksum_mismatch"
    assert "checksum Kiwix publishes" in out["reason"]
    assert not (tmp_path / "wikem_en_all_maxi.zim").exists()
    assert (tmp_path / "wikem_en_all_maxi.zim.rejected").exists()


def test_no_published_checksum_means_nothing_is_downloaded(switch_open, tmp_path):
    kiwix = _Kiwix()
    kiwix.routes[LB_DATED + ".sha256"] = (404, b"", None)
    out = _fetch(kiwix, tmp_path)
    assert out["ok"] is False and out["status"] == "checksum_unavailable"
    assert MIRROR not in kiwix.calls


@pytest.mark.parametrize("text", [
    b"not a checksum\n",
    b"%s  wikem_en_all_maxi_2026-01.zim\n" % (b"a" * 64),   # another file
    b"%s  wikem_en_all_maxi_2026-07.zim\n" % (b"a" * 63),   # too short
    b"a" * 5000,
])
def test_a_checksum_that_does_not_parse_or_names_another_file_is_refused(
        switch_open, tmp_path, text):
    kiwix = _Kiwix()
    kiwix.routes[LB_DATED + ".sha256"] = (200, text, None)
    out = _fetch(kiwix, tmp_path)
    assert out["ok"] is False and out["status"] == "checksum_unavailable"
    assert MIRROR not in kiwix.calls


def test_a_checksum_from_a_mirror_is_not_trusted(switch_open, tmp_path):
    kiwix = _Kiwix()
    moved = MIRROR + ".sha256"
    kiwix.routes[LB_DATED + ".sha256"] = (302, b"", moved)
    kiwix.routes[moved] = (200, f"{hashlib.sha256(BODY).hexdigest()}  "
                                f"wikem_en_all_maxi_2026-07.zim\n".encode(), None)
    out = _fetch(kiwix, tmp_path)
    assert out["ok"] is False and out["status"] == "checksum_unavailable"
    assert moved not in kiwix.calls


def test_a_reissue_between_checksum_and_file_installs_nothing(switch_open, tmp_path):
    kiwix = _Kiwix()
    newer_lb = LB_DATED.replace("2026-07", "2026-08")
    newer_mirror = MIRROR.replace("2026-07", "2026-08")
    kiwix.routes[LB_SAME] = (301, b"", newer_lb)
    kiwix.routes[newer_lb] = (302, b"", newer_mirror)
    kiwix.routes[newer_mirror] = (200, BODY, None)
    out = _fetch(kiwix, tmp_path)
    assert out["ok"] is False and out["status"] == "checksum_unavailable"
    assert not (tmp_path / "wikem_en_all_maxi.zim").exists()


def test_a_short_body_is_not_installed(switch_open, tmp_path):
    kiwix = _Kiwix()

    class _Short(_Resp):
        def __init__(self):
            super().__init__(200, BODY[:100])
            self.headers["Content-Length"] = str(len(BODY))

    real = kiwix.__call__

    def transport(url, headers, timeout):
        if url == MIRROR:
            kiwix.calls.append(url)
            return _Short()
        return real(url, headers, timeout)

    out = sf.fetch(SOURCE_ID, POSTURE, size_acknowledged=True,
                   dest_dir=str(tmp_path), transport=transport,
                   resolver=_public, sleeper=lambda s: None)
    assert out["ok"] is False and out["status"] == "incomplete"
    assert not (tmp_path / "wikem_en_all_maxi.zim").exists()


def test_robots_is_still_honoured_on_the_origin(switch_open, tmp_path):
    kiwix = _Kiwix()
    kiwix.routes["https://download.kiwix.org/robots.txt"] = (
        200, b"User-agent: *\nDisallow: /\n", None)
    out = _fetch(kiwix, tmp_path)
    assert out["ok"] is False and "robots.txt disallows" in out["reason"]
    assert MIRROR not in kiwix.calls


# ── the wrapper keeps its own guard per hop ──────────────────────────────────

def test_follow_redirects_false_hands_the_redirect_back(monkeypatch):
    door = ed.door(sf.DOOR_ID)
    monkeypatch.setenv(door.env_var, "1")
    ed.reset_cache()

    class _R:
        status_code = 301
        headers = {"Location": LB_SAME}

        def close(self):
            pass

    class _Session:
        def request(self, *a, **k):
            return _R()

        def close(self):
            pass

    monkeypatch.setattr(ed, "_pinned_session", lambda d: _Session())
    monkeypatch.setattr(ed, "_check_url", lambda d, url, hop=False: None)
    resp = ed.egress_request(sf.DOOR_ID, "GET", ORIGIN, follow_redirects=False)
    assert resp.status_code == 301
    ed.reset_cache()


# ── a confirmed registry pack download does not read robots.txt ──────────────

ROBOTS = "https://download.kiwix.org/robots.txt"
DISALLOW_ALL = (200, b"User-agent: *\nDisallow: /\n", None)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """The egress ledger and the switch snapshot, in this test's own folder."""
    from puffpai.security import egress_ledger

    path = tmp_path / "ledger" / "egress.jsonl"
    path.parent.mkdir()
    monkeypatch.setattr(ed, "_ledger_path",
                        lambda: str(tmp_path / "ledger" / "snapshot.json"))
    egress_ledger.set_path_override(str(path))
    try:
        yield egress_ledger
    finally:
        egress_ledger.set_path_override(None)


def _disallowing_kiwix(**kw):
    kiwix = _Kiwix(**kw)
    kiwix.routes[ROBOTS] = DISALLOW_ALL
    return kiwix


def _fetch_pack(kiwix, tmp_path, source=SOURCE_ID):
    dest = tmp_path / "packs"
    dest.mkdir(exist_ok=True)
    return sf.fetch_registry_pack(source, POSTURE, size_acknowledged=True,
                                  dest_dir=str(dest), transport=kiwix,
                                  resolver=_public, sleeper=lambda s: None)


def test_a_registry_pack_download_proceeds_past_a_disallow_all_robots(
        switch_open, ledger, tmp_path):
    kiwix = _disallowing_kiwix()
    out = _fetch_pack(kiwix, tmp_path)
    assert out["ok"] is True, out["reason"]
    assert ROBOTS not in kiwix.calls, "robots.txt is not read for this download"
    # The checksum is still read first, from Kiwix's own servers, and matched.
    assert kiwix.calls.index(LB_DATED + ".sha256") < kiwix.calls.index(MIRROR)
    assert out["sha256"] == hashlib.sha256(BODY).hexdigest()
    assert out["provenance"]["sha256_published"] is True
    assert (tmp_path / "packs" / "wikem_en_all_maxi.zim").read_bytes() == BODY


def test_a_registry_pack_download_still_rejects_a_wrong_checksum(
        switch_open, ledger, tmp_path):
    out = _fetch_pack(_disallowing_kiwix(digest="0" * 64), tmp_path)
    assert out["ok"] is False and out["status"] == "checksum_mismatch"
    assert not (tmp_path / "packs" / "wikem_en_all_maxi.zim").exists()


def test_the_same_url_through_the_generic_fetch_is_still_refused_by_robots(
        switch_open, ledger, tmp_path):
    kiwix = _disallowing_kiwix()
    out = sf.fetch(ORIGIN, POSTURE, size_acknowledged=True,
                   dest_dir=str(tmp_path), transport=kiwix, resolver=_public,
                   sleeper=lambda s: None)
    assert out["ok"] is False and "robots.txt disallows" in out["reason"]
    assert ROBOTS in kiwix.calls
    assert MIRROR not in kiwix.calls and LB_DATED + ".sha256" not in kiwix.calls
    allowed, _why = sf.robots_allows(ORIGIN, transport=_disallowing_kiwix(),
                                     resolver=_public, sleeper=lambda s: None)
    assert allowed is False
    assert not any(r.get("kind") == "robots_skipped" for r in ledger.read_records())


def test_a_url_is_not_accepted_as_a_registry_pack(switch_open, ledger, tmp_path):
    kiwix = _disallowing_kiwix()
    out = _fetch_pack(kiwix, tmp_path, source=ORIGIN)
    assert out["ok"] is False and "not a source on the vetted list" in out["reason"]
    assert kiwix.calls == []
    assert not any(r.get("kind") == "robots_skipped" for r in ledger.read_records())


def test_a_registry_pack_download_still_needs_the_confirmation(
        switch_open, ledger, tmp_path):
    kiwix = _disallowing_kiwix()
    out = sf.fetch_registry_pack(SOURCE_ID, POSTURE, dest_dir=str(tmp_path),
                                 transport=kiwix, resolver=_public,
                                 sleeper=lambda s: None)
    assert out["ok"] is False and out["status"] == "needs_confirmation"
    assert kiwix.calls == []


def test_a_registry_pack_download_refuses_when_the_switch_is_closed(
        monkeypatch, ledger, tmp_path):
    monkeypatch.delenv(ed.door(sf.DOOR_ID).env_var, raising=False)
    ed.reset_cache()
    sf.clear_caches()
    kiwix = _disallowing_kiwix()
    out = _fetch_pack(kiwix, tmp_path)
    assert out["ok"] is False and out["status"] == "switched_off"
    assert kiwix.calls == []
    assert os.listdir(tmp_path / "packs") == []
    assert not any(r.get("kind") == "robots_skipped" for r in ledger.read_records())


@pytest.mark.parametrize("bad", [
    "https://evil.example/zim/other/wikem_en_all_maxi_2026-07.zim",
    "https://download.kiwix.org.evil.example/zim/other/wikem_en_all_maxi_2026-07.zim",
])
def test_a_registry_pack_download_still_refuses_an_off_chain_redirect(
        switch_open, ledger, tmp_path, bad):
    kiwix = _disallowing_kiwix(mirror=bad)
    out = _fetch_pack(kiwix, tmp_path)
    assert out["ok"] is False and "not on Kiwix's download chain" in out["reason"]
    assert bad not in kiwix.calls
    assert not (tmp_path / "packs" / "wikem_en_all_maxi.zim").exists()


def test_a_non_kiwix_row_still_honours_robots_through_the_registry_call(
        switch_open, ledger, tmp_path):
    row = source_registry.get_source("medlineplus_health_topics")
    assert row is not None and row["kind"] != "zim"
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url == "https://medlineplus.gov/robots.txt":
            return _Resp(*DISALLOW_ALL[:2])
        return _Resp(200, b"<html></html>")

    out = sf.fetch_registry_pack(row["id"], POSTURE, size_acknowledged=True,
                                 dest_dir=str(tmp_path), transport=transport,
                                 resolver=_public, sleeper=lambda s: None)
    assert out["ok"] is False and "robots.txt disallows" in out["reason"]
    assert calls == ["https://medlineplus.gov/robots.txt"]


def test_the_ledger_and_provenance_record_the_robots_skip(
        switch_open, ledger, tmp_path):
    out = _fetch_pack(_disallowing_kiwix(), tmp_path)
    assert out["ok"] is True, out["reason"]
    skips = [r for r in ledger.read_records() if r.get("kind") == "robots_skipped"]
    assert len(skips) == 1
    skip = skips[0]
    assert skip["door"] == sf.DOOR_ID and skip["result"] == "allowed"
    assert skip["host"] == sf.KIWIX_ORIGIN
    assert skip["path"] == "/zim/wikem_en_all_maxi.zim"
    assert "robots.txt not read" in skip["reason"] and SOURCE_ID in skip["reason"]
    # The owner-facing view of the ledger shows the skip by its kind.
    assert any(r.get("kind") == "robots_skipped" for r in ledger.recent_public())
    assert out["provenance"]["robots_txt"] == "skipped_registry_pack"
    on_disk = sf.read_provenance(str(tmp_path / "packs" / "wikem_en_all_maxi.zim"))
    assert on_disk["robots_txt"] == "skipped_registry_pack"


def test_the_generic_fetch_records_robots_as_honoured(switch_open, ledger, tmp_path):
    out = _fetch(_Kiwix(), tmp_path)
    assert out["ok"] is True, out["reason"]
    assert out["provenance"]["robots_txt"] == "honoured"
    assert not any(r.get("kind") == "robots_skipped" for r in ledger.read_records())


def test_the_wizard_download_call_site_is_the_registry_pack_call(monkeypatch):
    from puffpai import setup_ops

    seen = {}

    def fake_pack(source_id, posture, **kw):
        seen.update(source_id=source_id, **kw)
        return {"ok": False}

    def generic(*a, **k):
        raise AssertionError("the confirmed wizard download must not use fetch()")

    monkeypatch.setattr(sf, "fetch_registry_pack", fake_pack)
    monkeypatch.setattr(sf, "fetch", generic)
    setup_ops.fetch_source(SOURCE_ID, POSTURE, size_acknowledged=True)
    assert seen == {"source_id": SOURCE_ID, "size_acknowledged": True, "dest_dir": None}


def test_the_registry_has_no_dead_hesperian_row():
    ids = {r.get("id") for r in source_registry.load_registry().get("sources", [])}
    assert "kiwix_where_there_is_no_doctor" not in ids
    raw = json.loads((REPO / "config" / "source_registry.json").read_text(encoding="utf-8"))
    assert all("hesperian_en_all.zim" not in (r.get("source_url") or "")
               for r in raw["sources"])
