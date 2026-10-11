"""The browser face must record the same build the terminal face records.

Three drifts between ``/setup/*`` and ``puffpai setup``, each of which made a
browser-built appliance quietly different from a terminal-built one:

* **The manifest dropped the household.** ``setup_manifest()`` copied a fixed
  list of sections out of the POST body, and ``household`` / ``persona`` /
  ``interview`` -- which ``setup_cli._step_manifest`` writes -- were not on it.
  A browser run therefore committed a manifest with no household block at all.
* **The Bridge step never recorded the edge.** ``POST /setup/bridge`` took a
  bare ``network_edge: bool`` and called the tuple form of
  ``reconcile_bridge``, so ``edge_block`` and ``record_zone3_pending`` were
  unreachable from the browser and ``manifest.bridge.edge`` always said nothing
  was at the edge.
* **``/setup/ollama`` judged its write at ``sealed``.** The LAN-posture write
  path falls back to ``sealed`` when nobody states a posture, which is right
  for the CLI and wrong for a face that never asks: an ``island`` build refused
  the owner's own LAN Ollama box.

Nothing here binds a port or contacts an address: the edge URL is data only,
and the conftest resolver stub means no name is looked up.

Run:
    pytest tests/test_setup_api_face_parity.py
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client():
    """A test client with the process-global vault handle dropped.

    Config dir, state dir, the egress overlays and the LAN-posture globals are
    all isolated per test by the autouse fixtures in ``tests/conftest.py``.
    """
    from puffpai import api_server
    api_server._vault_instance = None
    api_server._vault_sessions.clear()
    api_server.app.config.update(TESTING=True)
    return api_server.app.test_client()


def _auth():
    from puffpai import api_server
    return {"Authorization": f"Bearer {api_server._API_KEY}"}


def _post(client, path, body):
    return client.post(path, json=body, headers=_auth())


def _ok(client, path, body):
    resp = _post(client, path, body)
    assert resp.status_code == 200, (path, resp.status_code, resp.get_json())
    data = resp.get_json()
    assert data.get("ok") is True or data.get("success") is True, data
    return data


def _manifest(client, body):
    """POST /setup/manifest. It answers the manifest envelope, not ``ok``."""
    resp = _post(client, "/setup/manifest", body)
    assert resp.status_code == 200, (resp.status_code, resp.get_json())
    return resp.get_json()


# ── 1. the manifest carries every section the CLI writes ────────────────────

#: A body shaped the way the dashboard's review step sends it, household and
#: all. Values are the blocks the step-level routes hand back.
BROWSER_BODY = {
    "identity": {"identity": "homebot", "name": "HomeBot"},
    "household": {"profiles": 2, "owner_set": True},
    "persona": {"id": "homebot", "pack": "homebot", "name": "HomeBot"},
    "interview": {"goal": "run the house", "matched": ["lights"]},
}


class TestManifestSections:

    def test_a_committed_manifest_carries_the_household_block(self, client):
        written = _manifest(client, {**BROWSER_BODY, "commit": True})
        manifest = written["manifest"]
        assert written["written"] is True
        assert manifest["household"] == {"profiles": 2, "owner_set": True}, (
            "the browser wizard's household answer was dropped on the way to "
            "the manifest")
        assert manifest["persona"]["id"] == "homebot"
        assert manifest["interview"]["goal"] == "run the house"

        # And it is on DISK, not just in the response.
        from puffpai import build_manifest
        assert (build_manifest.read_manifest() or {}).get("household") == {
            "profiles": 2, "owner_set": True}

    def test_the_new_sections_go_through_the_same_sanitiser(self, client):
        """A forbidden contract key cannot ride in on the new blocks."""
        body = {
            **BROWSER_BODY,
            "household": {"profiles": 1, "owner_set": True,
                          "disable_vault": True},
            "interview": {"goal": "x", "matched": [], "phone_home": True},
            "commit": True,
        }
        manifest = _manifest(client, body)["manifest"]
        assert "disable_vault" not in manifest["household"]
        assert "phone_home" not in manifest["interview"]
        assert manifest["contracts"]


# ── 2. the Bridge step records the edge, in both shapes ─────────────────────

def _a_posture(client):
    """A Bridge posture id this build actually offers."""
    options = client.get("/setup/bridge", headers=_auth()).get_json()
    return str(options["postures"][0]["id"])


class TestBridgeEdge:

    def test_the_dashboard_boolean_shape_records_a_zone3_edge(self, client):
        body = _ok(client, "/setup/bridge",
                   {"posture": _a_posture(client), "network_edge": True})
        edge = body["edge"]
        assert edge["kind"] == "zone3"
        assert edge["declared"] is True
        assert edge["zone3"]["requested"] is True
        assert edge["contacted"] is False
        assert edge["zone3"]["applied"] is False
        # Recorded for the owner to run, under the one kind name.
        assert body["network_edge_recorded"] is True
        pending = client.get("/setup/pending-apply",
                             headers=_auth()).get_json()["pending"]
        assert [p["kind"] for p in pending] == ["zone3_network_edge"]

    def test_the_boolean_false_still_declares_an_answer(self, client):
        body = _ok(client, "/setup/bridge",
                   {"posture": _a_posture(client), "network_edge": False})
        assert body["edge"]["kind"] == "none"
        assert body["edge"]["declared"] is True, (
            "'nothing at my edge' is an ANSWER; it must not read as 'never "
            "asked'")
        assert body["network_edge_recorded"] is False

    def test_the_full_edge_shape_records_the_owners_own_gear(self, client):
        body = _ok(client, "/setup/bridge", {
            "posture": _a_posture(client),
            "edge": {"kind": "external", "external_kind": "adguard",
                     "external_url": "http://192.168.7.2:3000"},
        })
        edge = body["edge"]
        assert edge["kind"] == "external"
        assert edge["external_kind"] == "adguard"
        assert edge["external_url"] == "http://192.168.7.2:3000"
        assert edge["external_url_note"] == ""
        assert edge["contacted"] is False, "the wizard never fetches that page"

    def test_an_off_house_admin_url_is_not_recorded(self, client):
        """Data only, and LAN-or-loopback only: a public address is dropped."""
        body = _ok(client, "/setup/bridge", {
            "posture": _a_posture(client),
            "edge": {"kind": "external", "external_kind": "firewall",
                     "external_url": "http://93.184.216.34/admin"},
        })
        edge = body["edge"]
        assert edge["external_url"] == "", "an off-house address was recorded"
        assert "not on your own network" in edge["external_url_note"]
        assert edge["contacted"] is False

    def test_the_committed_manifest_carries_the_edge(self, client):
        saved = _ok(client, "/setup/bridge",
                    {"posture": _a_posture(client), "network_edge": True})
        manifest = _manifest(
            client, {"bridge": saved["bridge"], "commit": True})["manifest"]
        assert manifest["bridge"]["edge"]["kind"] == "zone3"
        assert manifest["bridge"]["edge"]["zone3"]["requested"] is True


# ── 3. /setup/ollama is judged at the settled posture ───────────────────────

LAN_OLLAMA = "http://192.168.7.40:11434"


def _forget_the_run_declaration():
    """Drop the posture THIS process declared, leaving only what is settled."""
    from puffpai.security import lan_posture
    lan_posture.clear_active_posture()


class TestOllamaPosture:

    def test_a_lan_url_is_accepted_at_the_settled_posture(self, client):
        """The failing case: nothing declared a posture in THIS process.

        ``_forget_the_run_declaration`` is what a service restart between wizard
        steps looks like - the owner answered the posture step, the API came
        back, and the process global is gone. The write path then fell back to
        ``sealed`` and told an ``island`` household its own Ollama box was off
        limits. The route now states the settled posture, exactly as
        ``/setup/ha`` does.
        """
        _ok(client, "/setup/posture", {"run_egress": "island"})
        _forget_the_run_declaration()
        body = _ok(client, "/setup/ollama",
                   {"model": "qwen3:4b", "url": LAN_OLLAMA})
        assert body["config_path"]
        from puffpai.config import get_config
        assert get_config().ollama.url == LAN_OLLAMA

    def test_a_lan_url_is_refused_under_sealed(self, client):
        """And the run's own answer outranks the settled default.

        A stated posture overrides ``lan_posture.write_path_posture()``
        outright, active declaration included, so a route that stated the
        default here would have overwritten the owner's 'sealed' with 'island'
        - loosening the very thing they had just tightened.
        """
        _ok(client, "/setup/posture", {"run_egress": "sealed"})
        resp = _post(client, "/setup/ollama",
                     {"model": "qwen3:4b", "url": LAN_OLLAMA})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body["ok"] is False
        assert body["error"] == "ollama_refused"
        assert "sealed" in body["message"]
        # Refused means NOT written.
        from puffpai.config import get_config
        assert get_config().ollama.url != LAN_OLLAMA

    def test_an_off_house_url_is_refused_under_island_too(self, client):
        """'island' widens the house, it does not open the internet."""
        _ok(client, "/setup/posture", {"run_egress": "island"})
        resp = _post(client, "/setup/ollama",
                     {"model": "qwen3:4b", "url": "http://93.184.216.34:11434"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "ollama_refused"
