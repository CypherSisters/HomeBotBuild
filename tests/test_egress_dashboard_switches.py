"""The dashboard server's outbound routes ask the Bridge, and the answer is real.

The dashboard's own Next.js routes (radio search, the radio play count, TuneIn
search and resolve, the page reader) reach the internet from a separate
process, so they cannot use the registry's request wrapper. Each one asks the
backend ``GET /system/egress/door/<id>`` first and refuses unless the answer is
a clear yes (``dashboard/app/_lib/server/bridgeSwitch.ts``; the node test
``bridge-gate.test.mjs`` exercises the routes themselves).

This module pins the backend half of that contract:

* **The question route.** Keyed like the rest of ``/system/egress``, read-only,
  ``no-store``, and it tracks a toggle, the environment override and a registry
  fault the same way ``egress_allowed`` does. Every failure answers
  ``open: false``.
* **The switches are real controls.** ``radio_browser``, ``tunein`` and the
  reader's ``page_reader`` are writable from Settings and reported enforced.
* **The routes still ask.** A static check over the dashboard route sources, in
  the spirit of ``test_no_outbound_call_site_outside_the_registry``: each
  outbound route names its switch before its first outbound call, and every
  public hostname in a dashboard server route belongs to a switch.
"""

import importlib
import json
import os
import re
import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from tests.modpatch import patched_modules

ROOT = Path(__file__).resolve().parents[1]
_src_dir = str(ROOT / "src" / "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

DASH_API = ROOT / "dashboard" / "app" / "api"
DASH_SERVER = ROOT / "dashboard" / "app" / "_lib" / "server"


@pytest.fixture(scope="module")
def api_app():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod.app, mod._API_KEY, mod


@pytest.fixture()
def client(api_app):
    app, api_key, mod = api_app
    mod._rate_limit_store.clear()
    with app.test_client() as c:
        yield c, {"X-API-Key": api_key}, mod


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A throwaway config, overlay and ledger, wired into reader and writer."""
    from puffpai.security import egress_doors, egress_ledger

    cfg = tmp_path / "puffpai_config.json"
    cfg.write_text(json.dumps({"identity": {"name": "TestBot"}}), encoding="utf-8")
    overlay = tmp_path / "state" / "egress_doors.json"
    monkeypatch.setattr(egress_doors, "_config_path", lambda: str(cfg))
    monkeypatch.setattr(egress_doors, "_ledger_path",
                        lambda: str(tmp_path / "egress_doors_snapshot.json"))
    for d in egress_doors.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    egress_doors.set_overlay_override(str(overlay))
    egress_ledger.set_path_override(str(tmp_path / "egress.jsonl"))
    egress_doors.reset_cache()
    try:
        yield overlay
    finally:
        egress_ledger.set_path_override(None)
        egress_doors.set_overlay_override(None)
        egress_doors.reset_cache()


DASHBOARD_SWITCHES = ("radio_browser", "tunein", "page_reader")


# ── the question route ───────────────────────────────────────────────────────

class TestSwitchQuestionRoute:
    def test_it_needs_the_key(self, client, sandbox):
        c, _headers, _mod = client
        assert c.get("/system/egress/door/radio_browser").status_code == 401

    def test_loopback_alone_is_not_enough(self, client, sandbox):
        c, _headers, _mod = client
        resp = c.get("/system/egress/door/radio_browser",
                     environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert resp.status_code == 401

    def test_it_is_neither_public_nor_loopback_exempt(self, api_app):
        from puffpai.security import endpoint_gate
        _app, _key, mod = api_app
        assert "system_egress_door_get" not in mod._PUBLIC_ENDPOINTS
        assert "system_egress_door_get" not in endpoint_gate.LOCAL_ONLY_ENDPOINTS

    @pytest.mark.parametrize("door_id", DASHBOARD_SWITCHES)
    def test_a_fresh_box_answers_closed(self, client, sandbox, door_id):
        from puffpai.security import egress_doors
        c, headers, _mod = client
        resp = c.get(f"/system/egress/door/{door_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.headers.get("Cache-Control") == "no-store"
        body = resp.get_json()
        assert body["success"] is True
        assert body["id"] == door_id
        assert body["open"] is False
        d = egress_doors.door(door_id)
        assert body["hosts"] == list(d.hosts)
        assert body["dynamic_hosts"] is d.dynamic_hosts

    def test_it_answers_no_reason_string(self, client, sandbox):
        """A reason can name a file path on the appliance; it stays home."""
        c, headers, _mod = client
        body = c.get("/system/egress/door/tunein", headers=headers).get_json()
        assert set(body) == {"success", "id", "open", "hosts", "dynamic_hosts"}

    @pytest.mark.parametrize("door_id", DASHBOARD_SWITCHES)
    def test_it_tracks_a_toggle_without_a_restart(self, client, sandbox,
                                                  door_id):
        c, headers, _mod = client
        assert c.post("/system/egress/door", headers=headers,
                      json={"id": door_id, "open": True}).status_code == 200
        assert c.get(f"/system/egress/door/{door_id}",
                     headers=headers).get_json()["open"] is True
        assert c.post("/system/egress/door", headers=headers,
                      json={"id": door_id, "open": False}).status_code == 200
        assert c.get(f"/system/egress/door/{door_id}",
                     headers=headers).get_json()["open"] is False

    def test_the_environment_still_wins(self, client, sandbox, monkeypatch):
        from puffpai.security import egress_doors
        c, headers, _mod = client
        c.post("/system/egress/door", headers=headers,
               json={"id": "tunein", "open": True})
        monkeypatch.setenv("PUFFPAI_EGRESS_TUNEIN", "0")
        egress_doors.reset_cache()
        assert c.get("/system/egress/door/tunein",
                     headers=headers).get_json()["open"] is False

    def test_an_unknown_switch_is_404_and_closed(self, client, sandbox):
        c, headers, _mod = client
        resp = c.get("/system/egress/door/no_such_switch", headers=headers)
        assert resp.status_code == 404
        assert resp.get_json()["open"] is False

    @pytest.mark.parametrize("raw", ["bad%20id", "tunein%3Bx", "a" * 161])
    def test_a_malformed_id_is_400_and_closed(self, client, sandbox, raw):
        c, headers, _mod = client
        resp = c.get(f"/system/egress/door/{raw}", headers=headers)
        assert resp.status_code == 400
        assert resp.get_json()["open"] is False

    def test_a_registry_fault_is_503_and_closed(self, client, sandbox,
                                                monkeypatch):
        from puffpai.security import egress_doors
        c, headers, _mod = client

        def boom(_door_id):
            raise RuntimeError("registry unreadable")

        monkeypatch.setattr(egress_doors, "egress_allowed", boom)
        resp = c.get("/system/egress/door/radio_browser", headers=headers)
        assert resp.status_code == 503
        assert resp.get_json()["open"] is False

    def test_asking_writes_nothing(self, client, sandbox):
        """A read must not create the overlay or record a door decision."""
        c, headers, _mod = client
        c.get("/system/egress/door/radio_browser", headers=headers)
        assert not sandbox.exists()


# ── the switches are real controls ───────────────────────────────────────────

class TestTheSwitchesAreControls:
    @pytest.mark.parametrize("door_id", DASHBOARD_SWITCHES)
    def test_status_reports_them_enforced_and_writable(self, client, sandbox,
                                                       door_id):
        c, headers, _mod = client
        rows = {r["id"]: r for r in c.get("/system/status", headers=headers)
                .get_json()["egress_doors"]}
        assert rows[door_id]["enforced"] is True
        assert rows[door_id]["writable"] is True
        assert rows[door_id]["open"] is False

    def test_page_reader_is_its_own_switch(self):
        from puffpai.security import egress_doors as ed
        d = ed.door("page_reader")
        assert d is not None
        assert d.default is False
        assert d.dynamic_hosts is True
        assert d.enforcement == "external"
        assert "page_reader" in ed._FLAG_HONOURED_EXTERNAL
        assert d.config_flag == "page_reader"
        assert d.env_var == "PUFFPAI_EGRESS_PAGE_READER"
        assert d is not ed.door("web_fetch")

    def test_new_owner_text_says_switch_not_door(self):
        from puffpai.security import egress_doors as ed
        d = ed.door("page_reader")
        for text in (d.purpose, d.data_sent, d.settings_hint, d.notes):
            # The route path is an identifier, not wording.
            words = text.replace("/system/egress/door/", "")
            assert "door" not in words.lower(), text
            assert "\u2014" not in text, text


# ── the dashboard routes still ask ───────────────────────────────────────────

#: Every dashboard server route that reaches the internet, and its switch.
GATED_ROUTES = {
    "radio/search/route.ts": "radio_browser",
    "radio/click/[uuid]/route.ts": "radio_browser",
    "tunein/search/route.ts": "tunein",
    "tunein/resolve/route.ts": "tunein",
    "reader/route.ts": "page_reader",
}

_OUTBOUND_RE = re.compile(r"\bfetch\(\s*`https://|\bfetchArticle\(|"
                          r"\bfetchPlaylist\(url\)")


@pytest.mark.parametrize("rel, door_id", sorted(GATED_ROUTES.items()))
def test_each_outbound_route_asks_its_switch_first(rel, door_id):
    source = (DASH_API / rel).read_text(encoding="utf-8")
    ask = f'checkBridgeSwitch("{door_id}")'
    assert ask in source, f"{rel} no longer asks the {door_id} switch"
    first_call = _OUTBOUND_RE.search(source[source.index("export async function"):])
    assert first_call, f"{rel}: no outbound call found; update GATED_ROUTES"
    body = source[source.index("export async function"):]
    assert body.index(ask) < first_call.start(), (
        f"{rel} reaches the network before it asks the {door_id} switch")


def test_every_outbound_dashboard_route_is_gated():
    """A new server route that fetches the internet must join GATED_ROUTES."""
    strays = []
    for path in sorted(DASH_API.rglob("route.ts")):
        rel = path.relative_to(DASH_API).as_posix()
        text = path.read_text(encoding="utf-8")
        if _OUTBOUND_RE.search(text) and rel not in GATED_ROUTES:
            strays.append(rel)
    assert not strays, ("dashboard routes that reach the internet without a "
                        "Bridge switch: " + ", ".join(strays))


def test_every_public_hostname_in_a_dashboard_server_route_has_a_switch():
    from puffpai.security import egress_doors as ed
    known = {h for d in ed.DOORS for h in d.hosts}
    # A literal https:// URL, or a bare quoted hostname on a web TLD (the way
    # the routes keep their mirror and host constants).
    url_re = re.compile(r"https://([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)")
    bare_re = re.compile(r"[\"'`]([a-z0-9\-]+(?:\.[a-z0-9\-]+)*"
                         r"\.(?:com|org|net|info|io|gov|fm|tv|co|uk|de|app))"
                         r"[\"'`]")
    strays = []
    files = sorted(DASH_API.rglob("route.ts")) + sorted(DASH_SERVER.glob("*.ts"))
    for path in files:
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith(("*", "//", "/*")):
                continue
            for host in url_re.findall(line) + bare_re.findall(line):
                host = host.lower()
                if host and host not in known:
                    strays.append(f"{path.relative_to(ROOT).as_posix()}:"
                                  f"{lineno}: {host}")
    assert not strays, "hosts no switch declares:\n  " + "\n  ".join(strays)


def test_the_radio_mirror_lists_stay_inside_the_switch():
    from puffpai.security import egress_doors as ed
    declared = set(ed.door("radio_browser").hosts)
    for rel in ("radio/search/route.ts", "radio/click/[uuid]/route.ts"):
        text = (DASH_API / rel).read_text(encoding="utf-8")
        mirrors = re.search(r"const MIRRORS = \[(.*?)\];", text, re.S)
        assert mirrors, rel
        hosts = set(re.findall(r"\"([^\"]+)\"", mirrors.group(1)))
        assert hosts and hosts <= declared, (rel, hosts - declared)
