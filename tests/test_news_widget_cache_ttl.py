"""News widget cache: 30 min of headlines, but never past a closed door.

``_NEWS_TTL`` is 1800 s. A cache hit served before looking at the ``news_rss``
door would keep showing headlines for up to 30 min after the owner switched the
door off, instead of the Bridge refusal. So a cache hit is served only while the
door is open.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from puffpai import dashboard_widgets as dw  # noqa: E402
from puffpai.security import egress_doors  # noqa: E402

URL = "https://feeds.npr.org/1001/rss.xml"
ITEMS = [{"title": "Cached headline", "link": "", "published": ""}]


@pytest.fixture
def cached_feed(monkeypatch):
    """A fresh cache entry, no ledger writes, and no real fetch possible."""
    monkeypatch.setattr(dw, "_news_cache", {URL: (time.time(), list(ITEMS))})
    monkeypatch.setattr(dw, "_log_egress", lambda *a, **k: None)
    fetched = []

    def _get(door_id, url, **kwargs):
        fetched.append(door_id)
        allowed, reason = egress_doors.egress_allowed(door_id)
        if not allowed:
            raise egress_doors.EgressBlocked(
                door_id, reason, "News needs the RSS-feeds door, which is off.")
        raise AssertionError("a cache hit went to the network")

    monkeypatch.setattr(egress_doors, "egress_get", _get)
    yield fetched
    egress_doors.reset_cache()


def _door(monkeypatch, value):
    monkeypatch.setenv("PUFFPAI_EGRESS_NEWS_RSS", value)
    egress_doors.reset_cache()


def test_open_door_serves_the_cache_without_a_fetch(cached_feed, monkeypatch):
    _door(monkeypatch, "true")
    assert dw.fetch_news(URL) == (True, ITEMS)
    assert cached_feed == []


def test_closed_door_skips_the_cache_and_shows_the_refusal(cached_feed,
                                                           monkeypatch):
    _door(monkeypatch, "false")
    ok, result = dw.fetch_news(URL)
    assert ok is False
    assert result == "News needs the RSS-feeds door, which is off."
    assert cached_feed == ["news_rss"]


def test_an_unreadable_switch_counts_as_closed(cached_feed, monkeypatch):
    def _boom(door_id):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(egress_doors, "egress_allowed", _boom)
    monkeypatch.setattr(
        egress_doors, "egress_get",
        lambda door_id, url, **k: (_ for _ in ()).throw(
            egress_doors.EgressBlocked(door_id, "unreadable", "door shut")))
    assert dw.fetch_news(URL) == (False, "door shut")


def test_the_ttl_is_thirty_minutes():
    assert dw._NEWS_TTL == 1800
