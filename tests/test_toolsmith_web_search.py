"""Tests for ``toolsmith.web_search`` — the SearXNG pinhole.

The security crux of this vertical is that ``web_search`` may reach a LOCAL
SearXNG (loopback / LAN) that ``web_fetch``'s SSRF gate correctly refuses, and
does so through a NARROW exception that opens no SSRF hole:

* the SearXNG host is owner-configured, never taken from the payload;
* redirects from it are refused, not followed;
* nothing weakens ``reject_internal_address`` for ``web_fetch`` or anything else;
* the result URLs come back as DATA and, if later fetched, still face the full
  unmodified SSRF gate.

No test here opens a socket: the HTTP transport is injected, and the SearXNG URL
and timeout are injected too, so the search path needs neither config nor a real
SearXNG.

Run: pytest tests/test_toolsmith_web_search.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.toolsmith import (
    DEFAULT_SEARCH_RESULTS,
    MAX_SEARCH_RESULTS,
    TOOL_INTENTS,
    EGRESS_INTENTS,
    ToolsmithAgent,
)

SEARXNG = "http://127.0.0.1:8888"
PUBLIC_IP = "93.184.216.34"


# ── fakes ───────────────────────────────────────────────────────────

class FakeResponse:
    """Minimal streamed-``requests`` stand-in (mirrors test_toolsmith)."""

    def __init__(self, *, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers if headers is not None else {
            "Content-Type": "application/json"}
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeHTTP:
    """Queued responses; records every URL and kwargs it was asked for."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.kwargs = []

    def get(self, url, **kw):
        self.calls.append(url)
        self.kwargs.append(kw)
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        return self.responses.pop(0)


class RaisingHTTP:
    """A transport whose ``get`` always raises — SearXNG offline."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        raise self._exc


def resolver_for(mapping=None, default=PUBLIC_IP):
    mapping = mapping or {}

    def resolve(host, port):
        answer = mapping.get(host, default)
        return [answer] if isinstance(answer, str) else list(answer)

    return resolve


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "state" / "workspace"
    root.mkdir(parents=True)
    return str(root)


def search_agent(workspace, http, *, searxng_url=SEARXNG, timeout=5.0):
    return ToolsmithAgent(workspace_root=workspace, http=http,
                          resolver=resolver_for(),
                          searxng_url=searxng_url, searxng_timeout=timeout)


def envelope(intent, payload=None, *, egress=False, actions=4, seconds=30.0):
    return Envelope(
        intent=intent, payload=payload or {},
        budget=Budget(max_actions=actions, max_seconds=seconds,
                      allow_egress=egress),
        origin_agent="concierge",
    )


def searxng_json(*results):
    return json.dumps({"query": "q", "results": list(results)}).encode("utf-8")


# ── the intent is wired ─────────────────────────────────────────────

def test_web_search_is_a_declared_egress_intent():
    assert "web_search" in TOOL_INTENTS
    # It leaves the appliance (reaches SearXNG), so it must ride an egress
    # budget exactly like web_fetch.
    assert "web_search" in EGRESS_INTENTS


# ── Part A: parses a mocked SearXNG reply ───────────────────────────

def test_web_search_parses_results(workspace):
    body = searxng_json(
        {"url": "https://example.com/a", "title": "A title",
         "content": "the A snippet"},
        {"url": "https://example.org/b", "title": "B title",
         "content": "the B snippet"},
    )
    http = FakeHTTP(FakeResponse(body=body))
    agent = search_agent(workspace, http)

    reply = agent.handle(envelope("web_search", {"query": "cats"}, egress=True))

    assert reply["ok"] is True
    assert reply["untrusted"] is True
    assert reply["count"] == 2
    assert reply["results"][0] == {
        "title": "A title", "url": "https://example.com/a",
        "snippet": "the A snippet"}
    # It hit the owner-configured SearXNG /search endpoint, with the query and
    # format passed as params (never concatenated into the path).
    assert http.calls == [SEARXNG + "/search"]
    assert http.kwargs[0]["params"] == {"q": "cats", "format": "json"}
    assert http.kwargs[0]["allow_redirects"] is False


def test_web_search_caps_and_shapes_results(workspace):
    many = [{"url": f"https://h{i}.example/", "title": f"t{i}",
             "content": f"c{i}"} for i in range(50)]
    http = FakeHTTP(FakeResponse(body=searxng_json(*many)))
    agent = search_agent(workspace, http)

    reply = agent.handle(envelope(
        "web_search", {"query": "x", "max_results": 3}, egress=True))
    assert reply["count"] == 3
    assert set(reply["results"][0]) == {"title", "url", "snippet"}

    # Default when unspecified, hard ceiling honoured over an absurd ask.
    http2 = FakeHTTP(FakeResponse(body=searxng_json(*many)))
    a2 = search_agent(workspace, http2)
    r_default = a2.handle(envelope("web_search", {"query": "x"}, egress=True))
    assert r_default["count"] == DEFAULT_SEARCH_RESULTS

    http3 = FakeHTTP(FakeResponse(body=searxng_json(*many)))
    a3 = search_agent(workspace, http3)
    r_max = a3.handle(envelope(
        "web_search", {"query": "x", "max_results": 999}, egress=True))
    assert r_max["count"] == MAX_SEARCH_RESULTS


def test_web_search_strips_control_chars_from_snippets(workspace):
    body = searxng_json(
        {"url": "https://example.com/a", "title": "ok\x1b[31m",
         "content": "line one\x00\x07 and two"})
    http = FakeHTTP(FakeResponse(body=body))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    title = reply["results"][0]["title"]
    snippet = reply["results"][0]["snippet"]
    assert "\x1b" not in title and "\x00" not in snippet and "\x07" not in snippet


def test_web_search_drops_results_with_no_usable_url(workspace):
    body = searxng_json(
        {"title": "no url here", "content": "..."},
        {"url": "https://good.example/", "title": "good", "content": "..."},
    )
    http = FakeHTTP(FakeResponse(body=body))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["count"] == 1
    assert reply["results"][0]["url"] == "https://good.example/"


# ── Part C: graceful degradation ────────────────────────────────────

def test_web_search_offline_is_graceful(workspace):
    http = RaisingHTTP(ConnectionError("connection refused"))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["ok"] is False
    assert reply["offline"] is True
    assert "search service" in reply["error"]
    # It tried exactly once and did not crash the bus.
    assert http.calls == [SEARXNG + "/search"]


def test_web_search_timeout_is_graceful(workspace):
    http = RaisingHTTP(TimeoutError("read timed out"))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["ok"] is False and reply["offline"] is True


def test_web_search_bad_json_is_graceful(workspace):
    http = FakeHTTP(FakeResponse(body=b"<html>not json</html>"))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["ok"] is False
    assert "JSON" in reply["error"]


def test_web_search_http_error_is_graceful(workspace):
    http = FakeHTTP(FakeResponse(status_code=502, body=b""))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["ok"] is False and reply["status"] == 502


# ── the pinhole is narrow ───────────────────────────────────────────

def test_web_search_refuses_without_egress(workspace):
    http = FakeHTTP(FakeResponse(body=searxng_json()))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=False))
    assert reply["ok"] is False
    assert reply["egress_denied"] is True
    # Refused before any socket — the request never left.
    assert http.calls == []


def test_web_search_does_not_follow_a_redirect_from_searxng(workspace):
    # A redirect from the fixed local host is the classic bounce-to-metadata
    # move. web_search refuses it rather than chasing it anywhere.
    http = FakeHTTP(FakeResponse(
        status_code=302, headers={"Location": "http://169.254.169.254/"},
        body=b""))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["ok"] is False
    assert "redirect" in reply["error"]
    # Exactly one call: it did NOT follow the Location to a second request.
    assert http.calls == [SEARXNG + "/search"]


def test_web_search_rejects_bad_query(workspace):
    http = FakeHTTP()
    agent = search_agent(workspace, http)
    for bad in ("", "   ", "x\x00y", "a" * 5000):
        reply = agent.handle(envelope("web_search", {"query": bad}, egress=True))
        assert reply["ok"] is False
        assert http.calls == []


def test_web_search_refuses_a_misconfigured_searxng_url(workspace):
    # An owner who set tools.searxng_url to a file scheme (or garbage) gets a
    # clean refusal, not an attempt.
    http = FakeHTTP()
    agent = search_agent(workspace, http, searxng_url="file:///etc/passwd")
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["ok"] is False
    assert http.calls == []


# ── the SSRF gate is INTACT for web_fetch (pinhole did not widen it) ─

@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1:8888/",      # the very SearXNG host — web_fetch still no
    "http://169.254.169.254/",     # cloud metadata
    "http://localhost/",
    "http://[::1]/",
    "file:///etc/passwd",
])
def test_web_fetch_still_rejects_internal_targets(workspace, bad_url):
    """The pinhole is web_search-only: web_fetch's SSRF refusals are unchanged,
    including — pointedly — the loopback SearXNG host itself."""
    http = FakeHTTP(FakeResponse(body=b"should never be reached"))
    agent = ToolsmithAgent(
        workspace_root=workspace, http=http,
        resolver=resolver_for({"localhost": "127.0.0.1"}),
        searxng_url=SEARXNG, searxng_timeout=5.0)
    reply = agent.handle(envelope("web_fetch", {"url": bad_url}, egress=True))
    assert reply["ok"] is False
    # Refused before any socket opened — the SSRF gate runs pre-connect.
    assert http.calls == []


def test_web_fetch_result_host_must_still_clear_ssrf(workspace):
    """A URL that came out of a search is just data; fetching one an attacker
    pointed at the metadata service is still refused by web_fetch."""
    http = FakeHTTP(FakeResponse(body=b"x"))
    agent = ToolsmithAgent(
        workspace_root=workspace, http=http, resolver=resolver_for(),
        searxng_url=SEARXNG, searxng_timeout=5.0)
    # Simulate the planner having picked a hostile result URL.
    reply = agent.handle(envelope(
        "web_fetch",
        {"url": "http://169.254.169.254/latest/meta-data/",
         "allowed_hosts": ["169.254.169.254"]},
        egress=True))
    assert reply["ok"] is False
    assert http.calls == []
