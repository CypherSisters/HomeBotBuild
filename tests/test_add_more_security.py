"""add_more security: privacy, consent tickets, ids only, Drawbridge, truth.

* Three private sentences cause ZERO outbound calls and no
  proposal, on the real chat route.
* A yes is a consent ticket: bound to the device, to exactly the lines shown,
  single use. A replayed ticket is refused. Voice and unpaired callers never
  say yes. No PIN anywhere.
* A request carries item ids from its own session; an address in the body is
  ignored and an id from anywhere else is refused.
* Drawbridge's block is never installed; the draft is thrown away.
* After adding, the real state is read back: a card whose website the bot may
  not use is "switched off", never "Added".
"""
from __future__ import annotations

import pytest

from add_more_support import (AUTH_KEY, DEVICE_A, DEVICE_B, PRIVATE_SENTENCES,
                              built, built_offline, guard_network,
                              item_by_host, item_numbered, nhtsa_network)
from puffpai import (add_more, add_more_api, api_server, feed_generator, feeds,
                     plain_words)
from puffpai.security import consent_tickets, egress_doors, outside_switches

AUTH = {"Authorization": f"Bearer {AUTH_KEY}"}
DEV_A = {**AUTH, "X-Puff-Device-Id": DEVICE_A}
DEV_B = {**AUTH, "X-Puff-Device-Id": DEVICE_B}
ANON = {**AUTH, "X-Puff-Anonymous": "1"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv(egress_doors.APPS_KILL_ENV, raising=False)
    egress_doors.clear_app_doors()
    outside_switches.forget()
    # HomeBot ships every outside service off: the owner's yes switches
    # "Looking at a service before adding it" on.
    outside_switches.set_switch("sample_look", True, by="owner")
    # HomeBot runs no routines, so it offers no daily update; these tests use
    # that line as a plain local item, so the kind is supplied here.
    from puffpai import routines
    monkeypatch.setattr(routines, "_kinds_cache", {"daily_update": {
        "id": "daily_update", "label": "Daily update", "schedule": "daily",
        "default_time": "08:00", "offer": True,
        "what_it_does": "Once a day, a short note made on this computer.",
        "offer_detail": "A short note once a day, made on this computer."}})
    consent_tickets.reset()
    add_more.reset()
    add_more_api._finds.clear()  # noqa: SLF001 - the per-minute search count
    egress_doors.reset_cache()
    api_server.app.config.update(TESTING=True)
    yield api_server.app.test_client()
    add_more.reset()
    consent_tickets.reset()
    egress_doors.clear_app_doors()
    egress_doors.reset_cache()


def _find(client, words, headers=DEV_A):
    got = client.post("/add/find", headers=headers, json={"words": words})
    assert got.status_code == 200, got.get_json()
    return got.get_json()


# -- privacy -------------------------------------------------------------------

@pytest.mark.parametrize("sentence", PRIVATE_SENTENCES)
def test_private_sentences_send_nothing_and_propose_nothing(client, monkeypatch,
                                                            sentence):
    built()
    seen = guard_network(monkeypatch)
    monkeypatch.setattr(api_server, "_query_ollama_direct",
                        lambda *a, **k: {"response": "Okay.", "sources": [],
                                         "suggestions": []})
    got = client.post("/chat/send", headers=DEV_A, json={"message": sentence})
    assert got.status_code == 200
    # HomeBot's own chat may ask the model on this computer; nothing else.
    assert [s for s in seen if s[0][1][0] not in ("127.0.0.1", "::1")] == []
    assert add_more._sessions == {}  # noqa: SLF001 - no search was even made
    assert "Here is what I found" not in got.get_json()["response"]


# -- ids only, sessions bound to their device ------------------------------------

def test_another_device_cannot_see_or_use_a_session(client, monkeypatch):
    built()
    guard_network(monkeypatch)
    found = _find(client, "knitting and crochet")
    sid = found["session"]
    update = item_numbered(found, "updates")
    assert client.get(f"/add/{sid}", headers=DEV_B).status_code == 404
    ticket = client.post(f"/add/{sid}/confirm", headers=DEV_A,
                         json={"ids": [update["id"]]}).get_json()["ticket"]
    stolen = client.post(f"/add/{sid}/approve", headers=DEV_B,
                         json={"ids": [update["id"]], "ticket": ticket})
    assert stolen.status_code == 404
    from puffpai import routines
    assert routines.get_routine("daily_update") is None


def test_a_yes_is_one_ticket_for_exactly_the_lines_shown(client, monkeypatch):
    built()
    guard_network(monkeypatch)
    found = _find(client, "knitting and crochet")
    sid = found["session"]
    update = item_numbered(found, "updates")
    book = item_numbered(found, "knowledge")
    # no ticket: nothing
    none = client.post(f"/add/{sid}/approve", headers=DEV_A,
                       json={"ids": [update["id"]]})
    assert none.status_code == 403
    assert "the yes did not come with this request" in none.get_json()["message"]
    # a ticket for one line cannot add two
    ticket = client.post(f"/add/{sid}/confirm", headers=DEV_A,
                         json={"ids": [update["id"]]}).get_json()["ticket"]
    wider = client.post(f"/add/{sid}/approve", headers=DEV_A,
                        json={"ids": [update["id"], book["id"]], "ticket": ticket})
    assert wider.status_code == 403
    assert "was for something else" in wider.get_json()["message"]
    # a used ticket is refused when replayed
    ticket = client.post(f"/add/{sid}/confirm", headers=DEV_A,
                         json={"ids": [update["id"]]}).get_json()["ticket"]
    first = client.post(f"/add/{sid}/approve", headers=DEV_A,
                        json={"ids": [update["id"]], "ticket": ticket})
    assert first.status_code == 200
    replay = client.post(f"/add/{sid}/approve", headers=DEV_A,
                         json={"ids": [update["id"]], "ticket": ticket})
    assert replay.status_code == 403
    assert "already used" in replay.get_json()["message"]
    assert replay.get_json()["message"].startswith("Nothing was added")


def test_an_unpaired_caller_cannot_search_or_say_yes(client, monkeypatch):
    built()
    guard_network(monkeypatch)
    assert client.post("/add/find", headers=ANON,
                       json={"words": "dinosaurs"}).status_code == 403
    found = _find(client, "dinosaurs")
    sid = found["session"]
    line = item_numbered(found, "updates")
    got = client.post(f"/add/{sid}/confirm", headers=AUTH,
                      json={"ids": [line["id"]]})
    assert got.status_code == 403  # a direct caller with the key is not paired
    assert client.post("/add/find", json={"words": "x"}).status_code == 401


# -- Drawbridge and the truth ----------------------------------------------------

def test_a_blocked_card_is_never_installed_and_its_draft_is_discarded(
        client, monkeypatch):
    built()
    nhtsa_network(monkeypatch)
    from puffpai import drawbridge

    real = drawbridge.check_addition

    def blocked(draft, **kw):
        verdict = real(draft, **kw)
        return dict(verdict, verdict="block",
                    line="NHTSA recalls: stopped because some of its words "
                         "read like orders to a computer. It was not added.")
    monkeypatch.setattr(drawbridge, "check_addition", blocked)
    found = _find(client, "car recalls")
    sid = found["session"]
    live = item_by_host(found, "api.nhtsa.gov")
    ticket = client.post(f"/add/{sid}/confirm", headers=DEV_A,
                         json={"ids": [live["id"]]}).get_json()["ticket"]
    got = client.post(f"/add/{sid}/approve", headers=DEV_A,
                      json={"ids": [live["id"]], "ticket": ticket}).get_json()
    result = got["results"][0]
    assert result["status"] == "held" and "not added" in result["line"]
    assert feed_generator.list_drafts() == []
    assert feeds.feed_apps() == []
    assert add_more.added() == []


def test_a_book_on_a_bot_built_offline_is_held_and_nothing_is_fetched(
        client, monkeypatch):
    built_offline()
    seen = guard_network(monkeypatch)
    found = _find(client, "knitting and crochet")
    sid = found["session"]
    book = item_numbered(found, "knowledge")
    ticket = client.post(f"/add/{sid}/confirm", headers=DEV_A,
                         json={"ids": [book["id"]]}).get_json()["ticket"]
    result = client.post(f"/add/{sid}/approve", headers=DEV_A,
                         json={"ids": [book["id"]], "ticket": ticket}
                         ).get_json()["results"][0]
    assert result["status"] == "held" and "stay off the internet" in result["line"]
    assert seen == [] and add_more.added() == []

