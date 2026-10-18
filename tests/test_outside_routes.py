"""The /outside routes, the town-lookup switch and the live-card yes.

OFF needs nothing. ON needs a ticket minted for exactly that switch on the
same paired device, spent once. No PIN anywhere.
"""

from __future__ import annotations

import json

import pytest

from livedata_support import (FakeResponse, fake_network, feed_manifest,
                              install, jargon_in, set_posture)
from puffpai import api_server
from puffpai.security import (consent_tickets, egress_doors, egress_ledger,
                              outside_switches)

AUTH = {"Authorization": f"Bearer {api_server._API_KEY}"}
DEV_A = {**AUTH, "X-Puff-Device-Id": "deviceaaaa000001"}
DEV_B = {**AUTH, "X-Puff-Device-Id": "devicebbbb000002"}
ANON = {**AUTH, "X-Puff-Anonymous": "1"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv(egress_doors.APPS_KILL_ENV, raising=False)
    for d in egress_doors.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    egress_doors.clear_app_doors()
    outside_switches.forget()
    consent_tickets.reset()
    egress_doors.reset_cache()
    api_server.app.config.update(TESTING=True)
    yield api_server.app.test_client()
    consent_tickets.reset()
    egress_doors.clear_app_doors()
    egress_doors.reset_cache()


class JsonResponse(FakeResponse):
    def json(self):
        return json.loads(self._body)


def _confirm(client, sid, headers=DEV_A):
    return client.post(f"/outside/{sid}/confirm", headers=headers)


def test_every_outside_route_needs_the_key(client):
    for method, path in (("get", "/outside"), ("get", "/outside/log"),
                         ("post", "/outside/weather/off"),
                         ("post", "/outside/weather/confirm"),
                         ("post", "/outside/weather/on")):
        assert getattr(client, method)(path).status_code == 401, path


def test_the_list_and_the_log_are_plain_words(client):
    set_posture("enumerated")
    body = client.get("/outside", headers=AUTH).get_json()
    assert body["title"] == "What leaves your computer"
    assert body["services"][0]["how_to_switch"] in ("Switch on", "Switch off")
    for row in body["services"]:
        assert not jargon_in(" ".join([row["name"], row["what_leaves"],
                                       row["state_line"]]))
    assert client.get("/outside/log", headers=AUTH).get_json()["rows"] == []


def test_off_is_one_tap_and_the_next_request_is_refused_and_logged(client,
                                                                   monkeypatch):
    set_posture("enumerated")
    outside_switches.set_switch("weather", True, by="owner")
    egress_doors.reset_cache()
    net = fake_network(monkeypatch)
    net.on("https://api.open-meteo.com/", FakeResponse(200, b"{}"))
    r = client.post("/outside/weather/off", headers=AUTH)
    assert r.status_code == 200 and r.get_json()["service"]["state"] == "off"
    with pytest.raises(egress_doors.EgressBlocked):
        egress_doors.egress_get("weather", "https://api.open-meteo.com/v1/f")
    assert net.calls == []
    rows = client.get("/outside/log", headers=AUTH).get_json()["rows"]
    assert rows[0]["result"] == "stopped: switched off"


def test_on_needs_a_ticket_from_the_same_device_used_once(client):
    set_posture("enumerated")
    # No ticket at all.
    r = client.post("/outside/weather/on", headers=DEV_A, json={})
    assert r.status_code == 403
    # An unpaired caller cannot even ask.
    assert _confirm(client, "weather", ANON).status_code == 403
    assert _confirm(client, "weather", AUTH).status_code == 403
    # Device A asks and gets the server's own sentence.
    asked = _confirm(client, "weather").get_json()
    assert "rough location" in asked["sentence"] and asked["expires_in"] == 600
    ticket = asked["ticket"]
    # Device B cannot spend A's ticket, and that burns it.
    r = client.post("/outside/weather/on", headers=DEV_B, json={"ticket": ticket})
    assert r.status_code == 403 and "different device" in r.get_json()["message"]
    r = client.post("/outside/weather/on", headers=DEV_A, json={"ticket": ticket})
    assert r.status_code == 403
    # A fresh ticket from A works once...
    ticket = _confirm(client, "weather").get_json()["ticket"]
    r = client.post("/outside/weather/on", headers=DEV_A, json={"ticket": ticket})
    assert r.status_code == 200 and r.get_json()["service"]["state"] == "on"
    # ...and a replay is refused.
    client.post("/outside/weather/off", headers=AUTH)
    r = client.post("/outside/weather/on", headers=DEV_A, json={"ticket": ticket})
    assert r.status_code == 403 and "already used" in r.get_json()["message"]
    assert egress_doors.egress_allowed("weather")[0] is False


def test_a_ticket_for_one_service_does_not_switch_another(client):
    set_posture("enumerated")
    ticket = _confirm(client, "weather").get_json()["ticket"]
    r = client.post("/outside/air_quality/on", headers=DEV_A, json={"ticket": ticket})
    assert r.status_code == 403
    assert egress_doors.egress_allowed("air_quality")[0] is False


def test_fixed_and_unknown_services_cannot_be_switched(client):
    set_posture("enumerated")
    assert _confirm(client, "brain_downloads").status_code == 409
    assert client.post("/outside/nothere/off", headers=AUTH).status_code == 404
    assert client.post("/outside/..%2Fx/off", headers=AUTH).status_code == 404


def _draft(cid="pbdb_taxa"):
    from livedata_support import candidate, fixture_probe, sample_bytes
    from puffpai import feed_generator
    cand = candidate(cid)
    return feed_generator.draft_feed_app(
        cand, build_online=True, probe=fixture_probe(sample_bytes(cand)))["id"]


def _installed(draft_id):
    from puffpai import app_registry
    return draft_id in app_registry.installed_ids(app_registry.rescan())

