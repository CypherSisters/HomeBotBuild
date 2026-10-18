"""The owner's egress-door control surface: POST /system/egress/door + ledger read.

The dashboard's Settings > Egress card has per-door switches; without a write
route they could only render DISABLED, with a footer telling the owner to go
edit ``config/puffpai_config.json`` by hand. These tests pin that write route,
and they lead with the property
that matters most: **a door that opens on an unauthenticated POST is worse than
no switch at all.**

Three boundaries get the attention here:

* **Auth.** 401 with no key. 401 with no key *from loopback* — this is a write,
  and "any process that can reach 127.0.0.1" is not the owner. The route is
  deliberately absent from both ``_PUBLIC_ENDPOINTS`` and
  ``endpoint_gate.LOCAL_ONLY_ENDPOINTS`` and both absences are asserted.
* **Honesty of the switch.** A toggle that appears to work and changes nothing
  is the defect the whole egress registry exists to remove, so every door whose
  flag is not load-bearing is refused with 409 and a reason, never written.
* **The ledger read leaks nothing.** The trail's own schema carries ``host``,
  ``path`` and (from dashboard_widgets) ``url``. The reader is an allowlist
  projection and the test feeds it a record with a query string in it to prove
  the string never comes back out.

No real credential, PIN or household filename appears here; the API key is the
synthetic one the session conftest seeds.
"""

import importlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from unittest.mock import MagicMock

from tests.modpatch import patched_modules

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


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
        # A paired screen: the identity the dashboard proxy asserts from the
        # pairing cookie, which switching an outside service ON needs.
        yield c, {"X-API-Key": api_key, "X-Puff-Device-Id": "a" * 32}, mod


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A throwaway config file + ledger, wired into BOTH the reader and writer.

    ``egress_admin.config_path`` delegates to ``egress_doors._config_path``, so
    repointing the one private makes the write and the subsequent
    ``/system/status`` read agree by construction — which is the point of the
    delegation.
    """
    from puffpai.security import egress_doors, egress_ledger

    cfg = tmp_path / "puffpai_config.json"
    cfg.write_text(json.dumps({
        "identity": {"name": "TestBot"},
        "egress_doors": {"news_rss": False},
    }, indent=2), encoding="utf-8")

    ledger = tmp_path / "egress.jsonl"
    overlay = tmp_path / "state" / "egress_doors.json"

    monkeypatch.setattr(egress_doors, "_config_path", lambda: str(cfg))
    egress_doors.set_overlay_override(str(overlay))
    egress_doors.reset_cache()
    egress_ledger.set_path_override(str(ledger))
    # The door registry keeps its own last-used snapshot beside the ledger;
    # point that at the sandbox too so no test writes into the real state dir.
    monkeypatch.setattr(egress_doors, "_ledger_path",
                        lambda: str(tmp_path / "egress_doors.json"))
    try:
        yield Sandbox(cfg, ledger, overlay)
    finally:
        egress_ledger.set_path_override(None)
        egress_doors.set_overlay_override(None)
        egress_doors.reset_cache()


class Sandbox(NamedTuple):
    """The three files a toggle can touch, so a test can name the one it means."""
    cfg: "Path"
    ledger: "Path"
    overlay: "Path"


def _flags(cfg):
    """The door flags in the CONFIG file (the low-precedence tier)."""
    return json.loads(cfg.read_text(encoding="utf-8"))["egress_doors"]


def _overlay_flags(overlay):
    """The door flags in the OWNER OVERLAY (what the API actually writes)."""
    return json.loads(overlay.read_text(encoding="utf-8"))["doors"]


# ── auth ─────────────────────────────────────────────────────────────────────

class TestAuthentication:
    """The switch that opens a door to the internet carries the key or fails."""

    def test_toggle_without_a_key_is_401(self, client, sandbox):
        c, _headers, _mod = client
        resp = c.post("/system/egress/door",
                      json={"id": "news_rss", "open": True})
        assert resp.status_code == 401

    def test_toggle_from_loopback_without_a_key_is_still_401(self, client, sandbox):
        """A WRITE is not exempted by being local.

        Several read endpoints are answered for a keyless loopback peer via
        endpoint_gate.LOCAL_ONLY_ENDPOINTS, because the Next proxy cannot
        always carry the key. Opening an egress door must NOT join that list:
        every process on the box, and anything behind a misconfigured proxy,
        is a loopback peer.
        """
        c, _headers, _mod = client
        resp = c.post("/system/egress/door",
                      json={"id": "news_rss", "open": True},
                      environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert resp.status_code == 401

    def test_ledger_read_without_a_key_is_401(self, client, sandbox):
        c, _headers, _mod = client
        assert c.get("/system/egress/ledger").status_code == 401
        assert c.get("/system/egress/ledger",
                     environ_overrides={"REMOTE_ADDR": "127.0.0.1"}
                     ).status_code == 401

    def test_neither_route_is_public_or_loopback_exempt(self, api_app):
        """Asserted on the sets themselves, not only on a response code."""
        from puffpai.security import endpoint_gate
        _app, _key, mod = api_app
        for endpoint in ("system_egress_door_set", "system_egress_ledger"):
            assert endpoint not in mod._PUBLIC_ENDPOINTS
            assert endpoint not in endpoint_gate.LOCAL_ONLY_ENDPOINTS

    def test_a_keyed_toggle_is_accepted(self, client, sandbox):
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True})
        assert resp.status_code == 200, resp.get_data(as_text=True)


# ── refusals ─────────────────────────────────────────────────────────────────

#: The two externally enforced doors whose switch is a download.
_DOWNLOAD_DOORS = ("model_pull", "embedding_model_download")

#: The line(s) of code that make each member of
#: ``egress_doors._FLAG_HONOURED_EXTERNAL`` true, keyed by door id. A test
#: below asserts the keys ARE that set, so a door cannot join it without a
#: claim here, and each claim is grepped for in the source.
_HONOURED_CALL_SITES = {
    "web_search": (
        ("assistant_tools.py", 'require_open("web_search")'),
        ("agents/toolsmith.py", '_require_door("web_search")'),
    ),
    "web_fetch": (
        ("agents/toolsmith.py", '_require_door("web_fetch")'),
    ),
    # The dashboard server's reader route, outside src/puffpai.
    "page_reader": (
        ("../../dashboard/app/api/reader/route.ts",
         'checkBridgeSwitch("page_reader")'),
    ),
    "tv_free_channels": (
        ("tv_cast.py", 'require_open("tv_free_channels")'),
    ),
    "model_pull": (
        ("brain_health.py", 'egress_allowed("model_pull")'),
        ("setup_cli.py", 'egress_allowed("model_pull")'),
    ),
    "embedding_model_download": (
        ("embeddings.py", "egress_allowed(ONNX_DOWNLOAD_DOOR)"),
        ("embeddings.py", "if not chroma_default_may_run():"),
        ("api_server.py", "text_fallback_ok = chroma_default_may_run()"),
    ),
}


class TestRefusals:
    @pytest.mark.parametrize("door_id", ["connectivity_probe"])
    def test_doors_this_registry_does_not_control_are_409(self, client, sandbox,
                                                          door_id):
        """connectivity_probe is derived.

        Writing its flag would hand the owner a switch that stops nothing, so
        the route refuses instead of pretending.
        """
        c, headers, _mod = client
        before = dict(_flags(sandbox.cfg))
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": door_id, "open": True})
        assert resp.status_code == 409
        assert resp.get_json()["error"]
        assert _flags(sandbox.cfg) == before, "a refused toggle must not write"
        assert not sandbox.overlay.exists(), "nor create an overlay"

    def test_an_unenforced_door_is_409(self, client, sandbox, monkeypatch):
        """An ``enforcement="none"`` entry is listed and never a switch.

        No built-in entry is unenforced, so the case is made with a synthetic
        one: the refusal must still hold for the next path that is listed
        before it can be gated.
        """
        import dataclasses
        from puffpai.security import egress_doors as ed
        listed = dataclasses.replace(
            ed.door("news_rss"), id="listed_only", config_flag="listed_only",
            env_var="PUFFPAI_EGRESS_LISTED_ONLY", enforcement="none",
            hosts=("listed-only.example.org",))
        monkeypatch.setattr(ed, "DOORS", ed.DOORS + (listed,))
        monkeypatch.setitem(ed._BY_ID, listed.id, listed)
        c, headers, _mod = client
        before = dict(_flags(sandbox.cfg))
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": listed.id, "open": True})
        assert resp.status_code == 409
        assert "does not enforce" in resp.get_json()["error"]
        assert _flags(sandbox.cfg) == before, "a refused toggle must not write"
        assert not sandbox.overlay.exists(), "nor create an overlay"

    def test_no_built_in_door_is_unenforced(self):
        from puffpai.security import egress_doors as ed
        assert [d.id for d in ed.DOORS if d.enforcement == "none"] == []

    def test_spotify_is_toggleable_because_its_calls_go_through_the_registry(
            self, client, sandbox):
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "spotify", "open": True})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        row = resp.get_json()["door"]
        assert row["open"] is True
        assert row["enforced"] is True and row["writable"] is True

    def test_web_fetch_is_toggleable_because_the_toolsmith_asks_the_door(
            self, client, sandbox):
        """web_fetch IS toggleable: the Toolsmith asks the door.

        If the Toolsmith's only gate were Budget.allow_egress, which never
        reads this registry, the switch would move the status display and
        nothing else, and 409 would be the honest answer. ``agents/toolsmith.py``
        calls ``require_open("web_fetch")`` before its own SSRF checks, so the
        switch is a real control and refusing it would be the dishonest answer.
        """
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "web_fetch", "open": True})
        assert resp.status_code == 200
        assert resp.get_json()["door"]["open"] is True

    def test_web_search_is_toggleable_because_its_call_site_asks_the_registry(
            self, client, sandbox):
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "web_search", "open": True})
        assert resp.status_code == 200

    @pytest.mark.parametrize("door_id", _DOWNLOAD_DOORS)
    def test_the_download_doors_are_toggleable_because_their_call_sites_ask(
            self, client, sandbox, monkeypatch, door_id):
        """Both switches are real controls, not a 409 "does not read this switch".

        Their call sites ask the registry, so the route writes them and the
        row it hands back says the switch is a real control, rather than the
        Settings card showing a disabled "Not enforced" switch.
        """
        from puffpai.security import egress_admin, egress_doors

        monkeypatch.delenv(egress_doors.door(door_id).env_var, raising=False)
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": door_id, "open": True})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        row = resp.get_json()["door"]
        assert row["open"] is True
        assert row["enforced"] is True
        assert row["writable"] is True
        assert _overlay_flags(sandbox.overlay)[door_id] is True
        assert egress_admin.toggleable(egress_doors.door(door_id)) == (True, "")

    def test_the_route_write_is_what_pull_model_reads(self, client, sandbox,
                                                      monkeypatch):
        """Open through the route and the pull is asked for; shut, it is not."""
        import requests

        from puffpai import brain_health
        from puffpai.security.egress_doors import egress_allowed

        monkeypatch.delenv("PUFFPAI_EGRESS_MODEL_PULL", raising=False)
        posts = []

        def _post(*a, **_k):
            posts.append(a)
            raise requests.ConnectionError("test stub: no Ollama here")

        monkeypatch.setattr(requests, "post", _post)
        c, headers, _mod = client

        assert c.post("/system/egress/door", headers=headers,
                      json={"id": "model_pull", "open": True}).status_code == 200
        assert egress_allowed("model_pull")[0] is True
        brain_health.pull_model("not-here:8b", "http://127.0.0.1:11434")
        assert len(posts) == 1, "an open door should reach Ollama's /api/pull"

        assert c.post("/system/egress/door", headers=headers,
                      json={"id": "model_pull", "open": False}).status_code == 200
        assert egress_allowed("model_pull")[0] is False
        outcome = brain_health.pull_model("not-here:8b",
                                          "http://127.0.0.1:11434")
        assert outcome["ok"] is False
        assert len(posts) == 1, "a shut door must not POST /api/pull"

    def test_the_route_write_is_what_the_embedding_gate_reads(
            self, client, sandbox, monkeypatch, tmp_path):
        """Same for the ONNX model files, with them absent from this box."""
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
            ONNXMiniLM_L6_V2)

        from puffpai import embeddings
        from puffpai.security.egress_doors import egress_allowed

        monkeypatch.delenv("PUFFPAI_EGRESS_EMBEDDING_MODEL_DOWNLOAD",
                           raising=False)
        monkeypatch.setattr(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH",
                            tmp_path / "no_onnx_here")
        fetches = []

        def _no_fetch(self):
            fetches.append(1)
            raise RuntimeError("test stub: chroma reached its model download")

        monkeypatch.setattr(ONNXMiniLM_L6_V2, "_download_model_if_not_exists",
                            _no_fetch)
        assert not embeddings.onnx_model_present()
        c, headers, _mod = client

        assert c.post("/system/egress/door", headers=headers,
                      json={"id": "embedding_model_download",
                            "open": True}).status_code == 200
        assert egress_allowed("embedding_model_download")[0] is True
        ef = embeddings._chroma_default_ef()

        assert c.post("/system/egress/door", headers=headers,
                      json={"id": "embedding_model_download",
                            "open": False}).status_code == 200
        assert egress_allowed("embedding_model_download")[0] is False
        with pytest.raises(RuntimeError) as excinfo:
            embeddings._chroma_default_ef()
        assert embeddings.ONNX_DOWNLOAD_DOOR in str(excinfo.value)
        # And the function built while it was open stops too.
        with pytest.raises(RuntimeError) as excinfo:
            ef(["built while the door was open"])
        assert embeddings.ONNX_DOWNLOAD_DOOR in str(excinfo.value)
        assert fetches == []

    def test_the_call_site_claims_cover_exactly_the_honoured_set(self):
        """A door in the set with no claim below is a switch nothing checks."""
        from puffpai.security import egress_admin
        from puffpai.security import egress_doors as ed

        assert set(_HONOURED_CALL_SITES) == set(ed._FLAG_HONOURED_EXTERNAL)
        assert egress_admin.EXTERNAL_DOORS_HONOURING_FLAG is \
            ed._FLAG_HONOURED_EXTERNAL
        assert ed._FLAG_HONOURED_EXTERNAL == frozenset({
            "web_search", "web_fetch", "page_reader", "tv_free_channels",
            "model_pull", "embedding_model_download"})

    def test_the_onnx_door_constant_names_its_door(self):
        """The embeddings.py claim below greps for the constant, not the id."""
        from puffpai import embeddings

        assert embeddings.ONNX_DOWNLOAD_DOOR == "embedding_model_download"

    @pytest.mark.parametrize("door_id, module_name, call", [
        (door_id, module_name, call)
        for door_id, sites in sorted(_HONOURED_CALL_SITES.items())
        for module_name, call in sites
    ])
    def test_the_honoured_external_doors_really_are_gated(self, door_id,
                                                          module_name, call):
        """Keeps EXTERNAL_DOORS_HONOURING_FLAG honest.

        Every member of that set is a claim about a specific line of code. If a
        refactor ever drops one, the set becomes a lie and the owner gets a
        dead switch — a toggle that reports success and stops nothing, which is
        strictly worse than a switch that says no. Fail here instead.

        ``_require_door`` is toolsmith.py's thin wrapper around
        ``egress_doors.require_open`` (it translates EgressBlocked into the
        agent's EgressDenied so the reply carries the "[Egress door closed]"
        sentence). Grepping for the wrapper rather than the underlying call is
        deliberate: the wrapper is what the fetch paths actually invoke.
        """
        path = os.path.join(os.path.dirname(_src_dir), "puffpai",
                            *module_name.split("/"))
        source = open(path, encoding="utf-8").read()
        assert call in source, (f"{module_name} no longer calls {call}, so "
                                f"the {door_id} switch may stop nothing")

    def test_the_toolsmith_wrapper_reaches_the_registry(self):
        """…and the wrapper the test above greps for is not itself a no-op."""
        path = os.path.join(os.path.dirname(_src_dir), "puffpai", "agents",
                            "toolsmith.py")
        source = open(path, encoding="utf-8").read()
        assert "egress_doors.require_open(door_id)" in source

    def test_every_honoured_external_door_is_really_external(self):
        """The set names external doors only; a registry door needs no entry."""
        from puffpai.security import egress_doors as ed
        for door_id in ed._FLAG_HONOURED_EXTERNAL:
            d = ed.door(door_id)
            assert d is not None, door_id
            assert d.enforcement == "external", door_id

    def test_unknown_door_is_404(self, client, sandbox):
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "no_such_door", "open": True})
        assert resp.status_code == 404

    @pytest.mark.parametrize("body", [
        {"open": True},                       # no id
        {"id": "news_rss"},                   # no open
        {"id": "news_rss", "open": "true"},   # a string, not a boolean
        {"id": "news_rss", "open": 1},        # an int, not a boolean
        {"id": "", "open": True},
        [],
    ])
    def test_malformed_bodies_are_400(self, client, sandbox, body):
        """``open`` is never coerced: guessing what "0" meant is how a door
        gets opened by a typo."""
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers, json=body)
        assert resp.status_code == 400

    def test_an_env_override_is_refused_rather_than_written_uselessly(
            self, client, sandbox, monkeypatch):
        """PUFFPAI_EGRESS_* beats the config file, so the write would be inert."""
        c, headers, _mod = client
        cfg = sandbox.cfg
        monkeypatch.setenv("PUFFPAI_EGRESS_NEWS_RSS", "false")
        before = dict(_flags(cfg))
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True})
        assert resp.status_code == 409
        assert "PUFFPAI_EGRESS_NEWS_RSS" in resp.get_json()["error"]
        assert _flags(cfg) == before
        assert not sandbox.overlay.exists()

    def test_an_unparseable_overlay_is_refused_never_rebuilt(self, client,
                                                             sandbox):
        """Rebuilding it would discard every OTHER door the owner had set.

        Those settings are the record of what this household consented to, so
        losing them silently is worse than refusing the toggle.
        """
        c, headers, _mod = client
        from puffpai.security import egress_doors
        sandbox.overlay.parent.mkdir(parents=True, exist_ok=True)
        sandbox.overlay.write_text("{ not json at all", encoding="utf-8")
        egress_doors.reset_cache()
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True})
        assert resp.status_code == 409
        assert "will not parse" in resp.get_json()["error"]
        assert sandbox.overlay.read_text(encoding="utf-8") == "{ not json at all"

    def test_an_unparseable_overlay_leaves_every_door_shut(self, client,
                                                           sandbox):
        """A file we cannot read is not consent — including for doors the
        config file had open."""
        c, headers, _mod = client
        from puffpai.security import egress_doors
        sandbox.cfg.write_text(json.dumps(
            {"egress_doors": {"news_rss": True, "weather": True}}),
            encoding="utf-8")
        sandbox.overlay.parent.mkdir(parents=True, exist_ok=True)
        sandbox.overlay.write_text("{{{", encoding="utf-8")
        egress_doors.reset_cache()
        rows = {d["id"]: d for d in
                c.get("/system/status", headers=headers).get_json()["egress_doors"]}
        assert rows["news_rss"]["open"] is False
        assert rows["weather"]["open"] is False
        assert rows["news_rss"]["source"] == "overlay"

    def test_an_unparseable_config_is_never_clobbered(self, client, sandbox,
                                                      monkeypatch):
        """Still true on the dev-box fallback path, where the config IS written."""
        c, headers, _mod = client
        from puffpai.security import egress_admin, egress_doors

        def no_overlay(*_a, **_k):
            raise OSError("read-only file system")

        monkeypatch.setattr(egress_admin, "_write_overlay", no_overlay)
        sandbox.cfg.write_text("{ not json at all", encoding="utf-8")
        egress_doors.reset_cache()
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True})
        assert resp.status_code == 503
        assert sandbox.cfg.read_text(encoding="utf-8") == "{ not json at all"


# ── the round trip ───────────────────────────────────────────────────────────

class TestToggleRoundTrip:
    def test_toggle_writes_the_flag_and_keeps_every_other_setting(
            self, client, sandbox):
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True})
        assert resp.status_code == 200
        # The OVERLAY is what changed; the appliance config is untouched,
        # because on a hardened box it is not writable at all.
        assert _overlay_flags(sandbox.overlay)["news_rss"] is True
        assert _flags(sandbox.cfg)["news_rss"] is False
        assert json.loads(sandbox.cfg.read_text(encoding="utf-8"))["identity"]             == {"name": "TestBot"}

    def test_a_second_toggle_preserves_the_first_doors_setting(self, client,
                                                               sandbox):
        """Read-modify-write on the overlay, not a one-key rewrite."""
        c, headers, _mod = client
        c.post("/system/egress/door", headers=headers,
               json={"id": "news_rss", "open": True})
        c.post("/system/egress/door", headers=headers,
               json={"id": "weather", "open": True})
        doors = _overlay_flags(sandbox.overlay)
        assert doors["news_rss"] is True and doors["weather"] is True

    def test_the_response_echoes_third_party_and_data_sent(self, client, sandbox):
        """The UI must be able to put the disclosure in front of the owner.

        Every toggle answer carries BOTH keys, so a UI that renders the
        disclosure never has to guess whether this door has one: a door that
        names no third party still has to say what leaves.
        """
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "radio_browser", "open": True})
        door = resp.get_json()["door"]
        assert "third_party" in door, "the row must carry the party field"
        assert door["data_sent"], "the row must say what leaves"
        assert door["open"] is True
        # web_search names no third party but still discloses what leaves.
        door = c.post("/system/egress/door", headers=headers,
                      json={"id": "web_search", "open": True}
                      ).get_json()["door"]
        assert "third_party" in door and door["data_sent"]
        assert "query" in door["data_sent"].lower()

    def test_the_change_is_visible_in_system_status_without_a_restart(
            self, client, sandbox):
        """reset_cache is the whole point: the registry caches the config."""
        c, headers, mod = client

        def door_state():
            body = c.get("/system/status", headers=headers).get_json()
            rows = {d["id"]: d for d in body.get("egress_doors", [])}
            return rows["news_rss"]["open"]

        assert door_state() is False
        assert c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True}).status_code == 200
        assert door_state() is True
        assert c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": False}).status_code == 200
        assert door_state() is False

    @pytest.mark.skipif(os.name == "nt",
                        reason="POSIX permission bits are not modelled on Windows")
    def test_the_overlay_is_written_owner_readable_only(self, client, sandbox):
        """0640: it records what this household chose to expose itself to."""
        c, headers, _mod = client
        assert c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True}).status_code == 200
        assert stat.S_IMODE(os.stat(sandbox.overlay).st_mode) == 0o640
        assert stat.S_IMODE(
            os.stat(sandbox.overlay.parent).st_mode) == 0o750

    def test_the_config_file_is_not_touched_at_all(self, client, sandbox):
        """Mode, mtime and bytes: the hardened box cannot write it, and the
        normal path must not try."""
        c, headers, _mod = client
        before_stat = os.stat(sandbox.cfg)
        before_bytes = sandbox.cfg.read_bytes()
        assert c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True}).status_code == 200
        after = os.stat(sandbox.cfg)
        assert sandbox.cfg.read_bytes() == before_bytes
        assert after.st_mode == before_stat.st_mode
        assert after.st_mtime == before_stat.st_mtime

    def test_the_writer_chmods_the_temp_file_before_the_rename(
            self, tmp_path, monkeypatch):
        """Unit-level, so the mode contract is pinned even where the OS is not.

        Order is load-bearing: chmod must land on the temp file BEFORE
        os.replace, or the config is briefly readable at the default umask.
        """
        from puffpai.security import egress_admin

        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"egress_doors": {"news_rss": False}}),
                       encoding="utf-8")
        calls = []
        real_chmod, real_replace = os.chmod, os.replace
        monkeypatch.setattr(os, "chmod",
                            lambda p, m: (calls.append(("chmod", m)),
                                          real_chmod(p, m))[1])
        monkeypatch.setattr(os, "replace",
                            lambda a, b: (calls.append(("replace", None)),
                                          real_replace(a, b))[1])
        monkeypatch.setattr(os, "stat",
                            lambda p, *a, **k: type("S", (), {"st_mode": 0o100600})())

        egress_admin._write_config(str(cfg), "news_rss", True)

        assert [c[0] for c in calls] == ["chmod", "replace"]
        assert calls[0][1] == 0o600
        assert json.loads(cfg.read_text(encoding="utf-8"))["egress_doors"][
            "news_rss"] is True

    def test_no_temp_file_is_left_behind(self, client, sandbox):
        c, headers, _mod = client
        c.post("/system/egress/door", headers=headers,
               json={"id": "news_rss", "open": True})
        for directory in (sandbox.cfg.parent, sandbox.overlay.parent):
            assert [n for n in os.listdir(directory) if ".tmp" in n] == []


# ── audit ────────────────────────────────────────────────────────────────────

class TestLedgerLine:
    def test_a_toggle_writes_a_door_toggled_line(self, client, sandbox):
        c, headers, _mod = client
        ledger = sandbox.ledger
        c.post("/system/egress/door", headers=headers,
               json={"id": "news_rss", "open": True})
        lines = [json.loads(l) for l in
                 ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        toggles = [l for l in lines if l.get("event") == "door_toggled"]
        assert len(toggles) == 1
        entry = toggles[0]
        assert entry["door"] == "news_rss"
        assert entry["open"] is True
        assert entry["by"] == "owner-api"
        assert entry["ts"]

    def test_a_refused_toggle_writes_no_ledger_line(self, client, sandbox):
        c, headers, _mod = client
        ledger = sandbox.ledger
        c.post("/system/egress/door", headers=headers,
               json={"id": "connectivity_probe", "open": True})
        text = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
        assert "door_toggled" not in text


# ── the ledger read ──────────────────────────────────────────────────────────

class TestLedgerRead:
    def _seed(self, ledger, count=5, extra=None):
        rows = []
        for i in range(count):
            row = {"ts": f"2026-09-03T0{i}:00:00+00:00", "door": "news_rss",
                   "result": "allowed", "kind": "request",
                   "purpose": "News headlines.", "third_party": None,
                   "host": "feeds.example.org",
                   "path": "/rss/politics",
                   "bytes_in": 1234, "bytes_out": 0, "status": 200,
                   "caller": "puffpai.widgets.news"}
            if extra:
                row.update(extra)
            rows.append(row)
        ledger.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_it_returns_the_documented_fields(self, client, sandbox):
        c, headers, _mod = client
        ledger = sandbox.ledger
        self._seed(ledger, 3)
        body = c.get("/system/egress/ledger?limit=10", headers=headers).get_json()
        assert body["success"] is True
        assert body["count"] == 3
        row = body["records"][-1]
        for field in ("door", "result", "purpose", "third_party", "ts",
                      "bytes_in", "bytes_out"):
            assert field in row

    def test_it_never_returns_a_url_a_path_or_a_query_string(self, client, sandbox):
        """The projection is an allowlist, so a field nobody anticipated is
        dropped rather than forwarded."""
        c, headers, _mod = client
        ledger = sandbox.ledger
        self._seed(ledger, 2, extra={
            "path": "/search",
            "url": "https://example.org/search?q=household-secret-term",
            "query": "q=household-secret-term",
            "some_future_field": "household-secret-term",
        })
        raw = c.get("/system/egress/ledger?limit=10",
                    headers=headers).get_data(as_text=True)
        assert "household-secret-term" not in raw
        assert "/search" not in raw
        assert "feeds.example.org" not in raw, "no host either"
        for record in json.loads(raw)["records"]:
            assert set(record).issubset(set(
                __import__("puffpai.security.egress_ledger", fromlist=["x"]
                           ).PUBLIC_FIELDS))

    def test_limit_is_capped_at_100(self, client, sandbox):
        c, headers, _mod = client
        ledger = sandbox.ledger
        self._seed(ledger, 150)
        body = c.get("/system/egress/ledger?limit=5000",
                     headers=headers).get_json()
        assert body["limit"] == 100
        assert body["count"] == 100

    @pytest.mark.parametrize("raw,expected", [
        ("0", 1), ("-4", 1), ("banana", 10), ("", 10), ("7", 7),
    ])
    def test_a_junk_limit_falls_back_instead_of_erroring(self, client, sandbox,
                                                         raw, expected):
        c, headers, _mod = client
        ledger = sandbox.ledger
        self._seed(ledger, 20)
        body = c.get(f"/system/egress/ledger?limit={raw}",
                     headers=headers).get_json()
        assert body["limit"] == expected

    def test_a_missing_ledger_is_an_empty_list_not_a_500(self, client, sandbox):
        c, headers, _mod = client
        ledger = sandbox.ledger
        if ledger.exists():
            ledger.unlink()
        body = c.get("/system/egress/ledger", headers=headers).get_json()
        assert body == {"success": True, "limit": 10, "count": 0, "records": []}

    def test_a_half_written_last_line_is_skipped(self, client, sandbox):
        """Rotation and a concurrent append must not break the owner's view."""
        c, headers, _mod = client
        ledger = sandbox.ledger
        self._seed(ledger, 2)
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-09-03T09:00:00+00:00", "door": "wea')
        body = c.get("/system/egress/ledger", headers=headers).get_json()
        assert body["count"] == 2

    def test_the_toggle_shows_up_in_the_recent_decisions_list(self, client, sandbox):
        """End to end: the card's switch and the card's list agree."""
        c, headers, _mod = client
        c.post("/system/egress/door", headers=headers,
               json={"id": "news_rss", "open": True})
        records = c.get("/system/egress/ledger",
                        headers=headers).get_json()["records"]
        toggles = [r for r in records if r.get("event") == "door_toggled"]
        assert toggles and toggles[-1]["door"] == "news_rss"
        assert toggles[-1]["open"] is True
        assert toggles[-1]["by"] == "owner-api"


# ── throttle ─────────────────────────────────────────────────────────────────

class TestRateLimit:
    def test_a_burst_of_toggles_is_throttled_in_its_own_bucket(self, client,
                                                               sandbox):
        """Tighter than the global 600/min, and separate from it, so a flood
        of writes cannot hide inside the dashboard's polling budget."""
        c, headers, mod = client
        codes = [c.post("/system/egress/door", headers=headers,
                        json={"id": "news_rss", "open": True}).status_code
                 for _ in range(mod._EGRESS_WRITE_LIMIT + 3)]
        assert codes[0] == 200
        assert codes[-1] == 429
        assert codes.count(429) == 3

    def test_the_ledger_read_is_not_throttled_by_the_write_bucket(self, client,
                                                                  sandbox):
        c, headers, mod = client
        for _ in range(mod._EGRESS_WRITE_LIMIT + 3):
            c.post("/system/egress/door", headers=headers,
                   json={"id": "news_rss", "open": True})
        assert c.get("/system/egress/ledger",
                     headers=headers).status_code == 200


# ── precedence: env > overlay > config > default ─────────────────────────────

class TestPrecedence:
    """The chain exists because the API can only write ONE of these tiers.

    On a hardened box (``ProtectSystem=strict``) the appliance config in
    /opt/puffpai/config is not writable by the service at all
    measured the 500. So the owner's switches land in the overlay, and the
    overlay has to outrank the config or a shipped ``false`` would pin the
    switch off forever. The environment still outranks everything, so an
    operator can force a door shut from the unit file and nothing the running
    service writes can undo it.
    """

    def test_the_overlay_beats_the_config_file(self, client, sandbox):
        c, headers, _mod = client
        from puffpai.security import egress_doors
        sandbox.cfg.write_text(json.dumps(
            {"egress_doors": {"news_rss": False}}), encoding="utf-8")
        sandbox.overlay.parent.mkdir(parents=True, exist_ok=True)
        sandbox.overlay.write_text(json.dumps(
            {"version": 1, "doors": {"news_rss": True}}), encoding="utf-8")
        egress_doors.reset_cache()

        row = [d for d in c.get("/system/status", headers=headers)
               .get_json()["egress_doors"] if d["id"] == "news_rss"][0]
        assert row["open"] is True
        assert row["source"] == "overlay"

    def test_the_config_still_decides_when_the_overlay_is_silent(self, client,
                                                                 sandbox):
        """The overlay is per-door: a door it does not mention falls through."""
        c, headers, _mod = client
        from puffpai.security import egress_doors
        sandbox.cfg.write_text(json.dumps(
            {"egress_doors": {"weather": True}}), encoding="utf-8")
        sandbox.overlay.parent.mkdir(parents=True, exist_ok=True)
        sandbox.overlay.write_text(json.dumps(
            {"version": 1, "doors": {"news_rss": True}}), encoding="utf-8")
        egress_doors.reset_cache()

        rows = {d["id"]: d for d in c.get("/system/status", headers=headers)
                .get_json()["egress_doors"]}
        assert (rows["weather"]["open"], rows["weather"]["source"]) == (True, "config")
        assert (rows["news_rss"]["open"], rows["news_rss"]["source"]) == (True, "overlay")

    def test_the_environment_beats_the_overlay(self, client, sandbox,
                                               monkeypatch):
        """An operator's unit-file force must survive anything the service writes."""
        c, headers, _mod = client
        from puffpai.security import egress_doors
        sandbox.overlay.parent.mkdir(parents=True, exist_ok=True)
        sandbox.overlay.write_text(json.dumps(
            {"version": 1, "doors": {"news_rss": True}}), encoding="utf-8")
        monkeypatch.setenv("PUFFPAI_EGRESS_NEWS_RSS", "false")
        egress_doors.reset_cache()

        row = [d for d in c.get("/system/status", headers=headers)
               .get_json()["egress_doors"] if d["id"] == "news_rss"][0]
        assert row["open"] is False
        assert row["source"] == "env"

    def test_nothing_set_anywhere_is_default_and_shut(self, client, sandbox):
        c, headers, _mod = client
        from puffpai.security import egress_doors
        sandbox.cfg.write_text(json.dumps({"egress_doors": {}}),
                               encoding="utf-8")
        egress_doors.reset_cache()
        rows = {d["id"]: d for d in c.get("/system/status", headers=headers)
                .get_json()["egress_doors"]}
        for door_id in ("weather", "news_rss", "radio_browser"):
            assert rows[door_id]["open"] is False
            assert rows[door_id]["source"] == "default"

    def test_status_publishes_a_source_for_every_door(self, client, sandbox):
        c, headers, _mod = client
        from puffpai.security import egress_doors
        for row in c.get("/system/status", headers=headers) \
                .get_json()["egress_doors"]:
            assert row["source"] in egress_doors.FLAG_SOURCES
            assert isinstance(row["writable"], bool)

    def test_the_toggle_moves_the_source_to_overlay(self, client, sandbox):
        """The card can show "you set this" against "this shipped that way"."""
        c, headers, _mod = client
        first = c.post("/system/egress/door", headers=headers,
                       json={"id": "weather", "open": True}).get_json()["door"]
        assert first["source"] == "overlay"
        assert first["open"] is True

    def test_writable_matches_what_the_route_will_actually_accept(self, client,
                                                                  sandbox):
        """``writable`` is the field the UI disables switches on, so it must not
        disagree with the route that answers 409."""
        c, headers, mod = client
        for row in c.get("/system/status", headers=headers) \
                .get_json()["egress_doors"]:
            # More switches than one window's toggle budget: the throttle is
            # not what this test is about.
            mod._rate_limit_store.clear()
            code = c.post("/system/egress/door", headers=headers,
                          json={"id": row["id"], "open": False}).status_code
            assert (code == 200) is row["writable"], row["id"]


# ── nowhere writable -> 503, never 500 ───────────────────────────────────────

def _raise_readonly(*_args, **_kwargs):
    raise OSError("read-only file system")


class TestNowhereWritable:
    """A read-only target: neither the overlay nor the config is writable.

    503 rather than 500 because this is a deployment condition with a plain
    remedy an operator can act on, not a bug in the appliance -- and the door
    state is provably unchanged either way.
    """

    @pytest.fixture()
    def sealed(self, sandbox, tmp_path, monkeypatch):
        """Make BOTH targets genuinely unwritable, with no mocking.

        A file where a directory needs to be makes os.makedirs fail on every
        platform, which is a real OSError out of the real code path rather
        than a patched one.
        """
        from puffpai.security import egress_doors
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        egress_doors.set_overlay_override(str(blocker / "state" / "e.json"))
        monkeypatch.setattr(egress_doors, "_config_path",
                            lambda: str(blocker / "cfg" / "puffpai_config.json"))
        egress_doors.reset_cache()
        return sandbox

    def test_it_is_503_with_a_plain_message_not_500(self, client, sealed):
        c, headers, _mod = client
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True})
        assert resp.status_code == 503, resp.get_data(as_text=True)
        error = resp.get_json()["error"]
        assert "unchanged" in error
        assert "writable" in error
        # No traceback, no internal exception text leaked to the caller.
        assert "Traceback" not in error and "Errno" not in error

    def test_the_door_state_really_did_not_move(self, client, sealed):
        c, headers, _mod = client
        before = [d for d in c.get("/system/status", headers=headers)
                  .get_json()["egress_doors"] if d["id"] == "news_rss"][0]
        c.post("/system/egress/door", headers=headers,
               json={"id": "news_rss", "open": True})
        after = [d for d in c.get("/system/status", headers=headers)
                 .get_json()["egress_doors"] if d["id"] == "news_rss"][0]
        assert after["open"] == before["open"] is False

    def test_no_ledger_line_claims_a_toggle_that_did_not_happen(self, client,
                                                                sealed):
        c, headers, _mod = client
        c.post("/system/egress/door", headers=headers,
               json={"id": "news_rss", "open": True})
        records = c.get("/system/egress/ledger",
                        headers=headers).get_json()["records"]
        assert [r for r in records if r.get("event") == "door_toggled"] == []


# ── the dev-box fallback ─────────────────────────────────────────────────────

class TestConfigFallback:
    def test_an_unwritable_overlay_falls_back_to_a_writable_config(
            self, client, sandbox, monkeypatch):
        """Dev checkouts have no data dir but do have a writable tree."""
        c, headers, _mod = client
        from puffpai.security import egress_admin

        monkeypatch.setattr(egress_admin, "_write_overlay", _raise_readonly)
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True})
        assert resp.status_code == 200
        assert _flags(sandbox.cfg)["news_rss"] is True
        assert resp.get_json()["door"]["source"] == "config"

    def test_the_fallback_refuses_when_an_overlay_already_shadows_the_door(
            self, client, sandbox, monkeypatch):
        """Writing the config under a shadowing overlay would be inert."""
        c, headers, _mod = client
        from puffpai.security import egress_admin, egress_doors

        sandbox.overlay.parent.mkdir(parents=True, exist_ok=True)
        sandbox.overlay.write_text(json.dumps(
            {"version": 1, "doors": {"news_rss": False}}), encoding="utf-8")
        egress_doors.reset_cache()
        monkeypatch.setattr(egress_admin, "_write_overlay", _raise_readonly)
        resp = c.post("/system/egress/door", headers=headers,
                      json={"id": "news_rss", "open": True})
        assert resp.status_code == 503
        assert "precedence" in resp.get_json()["error"]
        assert _flags(sandbox.cfg)["news_rss"] is False

    def test_the_ledger_records_where_the_flag_landed(self, client, sandbox):
        c, headers, _mod = client
        c.post("/system/egress/door", headers=headers,
               json={"id": "news_rss", "open": True})
        lines = [json.loads(l) for l in
                 sandbox.ledger.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        toggle = [l for l in lines if l.get("event") == "door_toggled"][-1]
        assert toggle["stored"] == "overlay"


# ── the overlay path itself ──────────────────────────────────────────────────

class TestOverlayLocation:
    def test_it_lands_under_the_data_dir_the_hardened_unit_grants(
            self, monkeypatch, tmp_path):
        """Not /opt/puffpai/config, which ProtectSystem=strict keeps read-only.

        Widening ReadWritePaths to the config directory would let an RCE in the
        API server rewrite every appliance setting; the overlay confines that
        write permission to a file of booleans.
        """
        from puffpai.security import egress_doors
        egress_doors.set_overlay_override(None)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
        try:
            path = egress_doors.overlay_path()
        finally:
            egress_doors.set_overlay_override(None)
        assert path.startswith(str(tmp_path))
        assert path.endswith(os.path.join("state", "egress_doors.json"))

    def test_a_snapshot_file_in_the_overlay_slot_is_ignored_not_obeyed(
            self, client, sandbox):
        """The last-used snapshot shares this basename one directory over.

        Pointed at the wrong one, the reader must not read telemetry as
        consent -- and must not fail every door shut over an operator's typo.
        """
        c, headers, _mod = client
        from puffpai.security import egress_doors
        sandbox.overlay.parent.mkdir(parents=True, exist_ok=True)
        sandbox.overlay.write_text(json.dumps(
            {"version": 2,
             "doors": {"news_rss": {"last": {"result": "allowed"}}}}),
            encoding="utf-8")
        sandbox.cfg.write_text(json.dumps(
            {"egress_doors": {"news_rss": True}}), encoding="utf-8")
        egress_doors.reset_cache()
        row = [d for d in c.get("/system/status", headers=headers)
               .get_json()["egress_doors"] if d["id"] == "news_rss"][0]
        # A dict is not a flag value, so this door stays shut. The point is
        # that it fails SHUT rather than reading "allowed" as consent.
        assert row["open"] is False
