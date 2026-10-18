"""The Add page over HTTP and the chat loop, end to end with a fake internet.

* G3 (car): the page finds a live card, one yes on a ticket, Drawbridge
  checks it, it installs, it fetches, and the request is on /outside/log.
* G2 (dinosaurs): chat proposes, "look online" is a separate step, the
  owner's number adds a book that starts learning with progress lines.
* The list sentences keep working: an add request never takes one.
* Added things are listed and taken out again; the notes can be cleared.
"""
from __future__ import annotations

import json
import os

import pytest

from add_more_support import (AUTH_KEY, DEVICE_A, built, guard_network,
                              item_by_host, item_numbered, nhtsa_network)
from livedata_support import install
from puffpai import (add_more, add_more_api, api_server, drawbridge, feeds,
                     plain_words)
from puffpai.security import consent_tickets, egress_doors, outside_switches

AUTH = {"Authorization": f"Bearer {AUTH_KEY}"}
DEV_A = {**AUTH, "X-Puff-Device-Id": DEVICE_A}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv(egress_doors.APPS_KILL_ENV, raising=False)
    egress_doors.clear_app_doors()
    outside_switches.forget()
    # HomeBot ships every outside service off: the owner's yes switches
    # "Looking at a service before adding it" on.
    outside_switches.set_switch("sample_look", True, by="owner")
    consent_tickets.reset()
    add_more.reset()
    add_more_api._finds.clear()  # noqa: SLF001
    egress_doors.reset_cache()
    api_server.app.config.update(TESTING=True)
    monkeypatch.setattr(api_server, "_query_ollama_direct",
                        lambda *a, **k: {"response": "Okay.", "sources": [],
                                         "suggestions": []})
    yield api_server.app.test_client()
    add_more.reset()
    consent_tickets.reset()
    egress_doors.clear_app_doors()
    egress_doors.reset_cache()


def test_the_page_says_its_title_in_plain_words(client):
    built(name="Wrench")
    body = client.get("/add", headers=DEV_A).get_json()
    assert body["title"] == "Find more for Wrench" and body["paired"] is True
    assert plain_words.check(body["line"]) == []
    assert client.get("/add").status_code == 401


def test_g3_car_card_from_the_add_page(client, monkeypatch):
    built(name="Wrench")
    net = nhtsa_network(monkeypatch)
    found = client.post("/add/find", headers=DEV_A,
                        json={"words": "car recalls"}).get_json()
    assert net.calls == []  # finding sends nothing
    live = item_by_host(found, "api.nhtsa.gov")
    sid = found["session"]
    asked = client.post(f"/add/{sid}/confirm", headers=DEV_A,
                        json={"ids": [live["id"]]}).get_json()
    assert "api.nhtsa.gov" in asked["sentence"]
    assert plain_words.check(asked["sentence"]) == []
    got = client.post(f"/add/{sid}/approve", headers=DEV_A,
                      json={"ids": [live["id"]], "ticket": asked["ticket"]}).get_json()
    result = got["results"][0]
    assert result["status"] == "added", result
    assert result["line"].startswith("Added NHTSA recalls: a live card")
    assert "Right now it shows" in result["line"]
    assert result["drawbridge"] in ("allow", "warn")
    # the card really works: it fetched and holds data
    state = feeds.card_state(result["app_id"])
    assert state["status"] == "ok" and state["allowed"]
    # the one sample look and the fetch both went to that website only
    hosts = {c["url"].split("/")[2] for c in net.calls}
    assert hosts == {"api.nhtsa.gov"}
    # Drawbridge's verdict is on record, by the add page
    checks = [json.loads(line) for line in open(drawbridge.log_path(),
                                                encoding="utf-8")]
    assert any(c.get("by") == "add:page" for c in checks)
    # the request is on the owner's log
    log = client.get("/outside/log", headers=AUTH).get_json()["rows"]
    assert any(r["website"] == "api.nhtsa.gov" for r in log)
    # it is listed, and can be taken out again
    rows = client.get("/add/added", headers=DEV_A).get_json()["rows"]
    assert rows[0]["label"] == "Live: NHTSA recalls"
    assert rows[0]["websites"] == ["api.nhtsa.gov"]
    out = client.post(f"/add/added/{rows[0]['entry']}/remove",
                      headers=DEV_A).get_json()
    assert out["ok"] and "no longer fetches" in out["line"]
    assert feeds.feed_apps() == []
    assert client.get("/add/added", headers=DEV_A).get_json()["rows"] == []


def test_progress_is_read_back_for_the_session(client, monkeypatch):
    built()
    guard_network(monkeypatch)
    from puffpai.knowledge import find_more
    monkeypatch.setattr(find_more, "_learn_rest_in_background", lambda folder: None)
    found = client.post("/add/find", headers=DEV_A,
                        json={"words": "dinosaurs"}).get_json()
    sid = found["session"]
    book = next(i for g in found["groups"] for i in g["items"]
                if i["id"] == "knowledge:wikipedia:en:dinosaur")
    asked = client.post(f"/add/{sid}/confirm", headers=DEV_A,
                        json={"ids": [book["id"]]}).get_json()
    result = client.post(f"/add/{sid}/approve", headers=DEV_A,
                         json={"ids": [book["id"]], "ticket": asked["ticket"]}
                         ).get_json()["results"][0]
    assert result["status"] == "working" and result["learn_id"]
    assert "wikipedia:en:dinosaur" in find_more.pending_orders()
    after = client.get(f"/add/{sid}", headers=DEV_A).get_json()
    lines = after["progress"][book["id"]]
    assert lines and plain_words.check("\n".join(lines)) == []
    # taking it out before it arrived cancels the order
    rows = add_more.added()
    assert "before it was fetched" in add_more.take_out(rows[0]["n"])
    assert find_more.pending_orders() == {}


def test_the_notes_can_be_read_and_cleared(client, monkeypatch):
    built()
    guard_network(monkeypatch)
    found = client.post("/add/find", headers=DEV_A,
                        json={"words": "zxqv blorf"}).get_json()
    assert found["nothing_found"] and found["choose_line"] == ""
    assert "I can't make new tools yet." in found["nothing_found"]
    notes = client.get("/add/notes", headers=DEV_A).get_json()["notes"]
    assert notes[0]["terms"] == ["zxqv", "blorf"]
    assert client.post("/add/notes/clear", headers=DEV_A).get_json()["cleared"] == 1
    assert not os.path.exists(os.path.join(add_more._state_dir(),
                                           add_more.WISHES_FILE))


def test_searches_are_limited_per_minute(client, monkeypatch):
    built()
    guard_network(monkeypatch)
    codes = [client.post("/add/find", headers=DEV_A,
                         json={"words": "dinosaurs"}).status_code
             for _ in range(add_more_api.FINDS_PER_MINUTE + 1)]
    assert codes[-1] == 429 and codes[:-1] == [200] * add_more_api.FINDS_PER_MINUTE


def test_the_add_page_says_everything_in_plain_words():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "dashboard" / "app" / "dashboard" / "add"
    src = (root / "page.tsx").read_text(encoding="utf-8")
    shown = re.findall(r'label="([^"]+)"', src)
    shown += re.findall(r'placeholder="([^"]+)"', src)
    shown += [t.strip() for t in re.findall(r">\s*([A-Z][^<>{}]{3,})\s*<", src)]
    shown += re.findall(r'"([A-Z][a-z][^"]*\s[^"]*)"', src)
    text = "\n".join(shown)
    assert "Add these" in text and "Yes, add them" in text and "Take it out" in text
    assert plain_words.check(text) == []

