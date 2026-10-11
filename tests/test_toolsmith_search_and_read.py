"""Tests for ``toolsmith.search_and_read`` — search + read fused in one step.

This intent exists so a PLANNED run can act on an online search: it searches the
owner's SearXNG (the unchanged pinhole) and then reads the top result through the
UNMODIFIED SSRF gate. The security crux is that the read half runs
``_fetch_url(..., allowed_hosts=None)`` — which skips ONLY the caller-scope check
(there is no pre-sealed scope for a URL discovered a moment earlier) while EVERY
unconditional SSRF gate still runs. So a discovered URL pointing at the metadata
service or a loopback host is still refused, exactly as a separate ``web_fetch``
would refuse it.

No test here opens a socket: the HTTP transport and the SearXNG URL are injected.

Run: pytest tests/test_toolsmith_search_and_read.py -q
"""

from __future__ import annotations

import json

import pytest

from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.toolsmith import (
    EGRESS_INTENTS,
    STATE_CHANGING_INTENTS,
    TOOL_INTENTS,
    ToolsmithAgent,
)

SEARXNG = "http://127.0.0.1:8888"
PUBLIC_IP = "93.184.216.34"


# ── fakes (mirror test_toolsmith_web_search) ────────────────────────

class FakeResponse:
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


def agent_for(workspace, http, *, resolver=None):
    return ToolsmithAgent(workspace_root=workspace, http=http,
                          resolver=resolver or resolver_for(),
                          searxng_url=SEARXNG, searxng_timeout=5.0)


def envelope(intent, payload=None, *, egress=False, actions=6, seconds=30.0):
    return Envelope(
        intent=intent, payload=payload or {},
        budget=Budget(max_actions=actions, max_seconds=seconds,
                      allow_egress=egress),
        origin_agent="concierge")


def searxng_json(*results):
    return json.dumps({"query": "q", "results": list(results)}).encode("utf-8")


# ── it is wired like a search, not a writer ─────────────────────────

def test_search_and_read_is_egress_gated_and_not_state_changing():
    assert "search_and_read" in TOOL_INTENTS
    assert "search_and_read" in EGRESS_INTENTS
    # It mutates nothing durable — it must not demand a consent gate.
    assert "search_and_read" not in STATE_CHANGING_INTENTS


# ── happy path: search, then read the top result ────────────────────

def test_search_and_read_reads_the_top_result(workspace):
    search = FakeResponse(body=searxng_json(
        {"url": "https://example.com/a", "title": "Top hit",
         "content": "snippet a"},
        {"url": "https://example.org/b", "title": "Second", "content": "b"}))
    page = FakeResponse(headers={"Content-Type": "text/html"},
                        body=b"<html><body><p>the page body</p></body></html>")
    http = FakeHTTP(search, page)
    agent = agent_for(workspace, http)

    reply = agent.handle(envelope(
        "search_and_read", {"query": "cats"}, egress=True))

    assert reply["ok"] is True
    assert reply["untrusted"] is True
    assert "the page body" in reply["text"]
    # The chosen result's url + title ride back for the caller/report.
    assert reply["url"] == "https://example.com/a"
    assert reply["title"] == "Top hit"
    assert reply["query"] == "cats"
    # Exactly two hops: the SearXNG search, then the fetch of the top result.
    assert http.calls == [SEARXNG + "/search", "https://example.com/a"]
    # The read half fetched with NO host scope (discovered URL), stream on.
    assert http.kwargs[1].get("allow_redirects") is False
    assert http.kwargs[1].get("stream") is True


# ── the SSRF gate still runs on the DISCOVERED url ──────────────────

def test_search_and_read_refuses_an_internal_result_url(workspace):
    """A result URL an attacker ranked at the top, pointing at the metadata
    service, is still refused by the unmodified SSRF gate — even though the read
    half passes allowed_hosts=None."""
    search = FakeResponse(body=searxng_json(
        {"url": "http://169.254.169.254/latest/meta-data/", "title": "evil",
         "content": "x"}))
    # Only the search response is queued: the fetch must be refused BEFORE any
    # socket, so no second response is consumed.
    http = FakeHTTP(search)
    agent = agent_for(workspace, http)

    reply = agent.handle(envelope(
        "search_and_read", {"query": "metadata"}, egress=True))

    assert reply["ok"] is False
    # The search ran; the read was refused pre-connect, so only one call.
    assert http.calls == [SEARXNG + "/search"]


def test_search_and_read_refuses_a_loopback_result_url(workspace):
    search = FakeResponse(body=searxng_json(
        {"url": "http://127.0.0.1:8888/admin", "title": "loopback",
         "content": "x"}))
    http = FakeHTTP(search)
    agent = agent_for(workspace, http, resolver=resolver_for(
        {"localhost": "127.0.0.1"}))
    reply = agent.handle(envelope(
        "search_and_read", {"query": "x"}, egress=True))
    assert reply["ok"] is False
    assert http.calls == [SEARXNG + "/search"]


def test_search_and_read_refuses_a_name_that_resolves_into_the_lan(workspace):
    """A public-LOOKING host whose DNS answer is private is refused per-hop by
    reject_internal_address — the resolver, not the literal, is the gate here."""
    search = FakeResponse(body=searxng_json(
        {"url": "https://intranet.example/", "title": "rebind", "content": "x"}))
    http = FakeHTTP(search)
    agent = agent_for(workspace, http, resolver=resolver_for(
        {"intranet.example": "10.0.0.5"}))
    reply = agent.handle(envelope(
        "search_and_read", {"query": "x"}, egress=True))
    assert reply["ok"] is False
    assert http.calls == [SEARXNG + "/search"]


# ── egress gate ─────────────────────────────────────────────────────

def test_search_and_read_refuses_without_egress(workspace):
    http = FakeHTTP(FakeResponse(body=searxng_json()))
    agent = agent_for(workspace, http)
    reply = agent.handle(envelope(
        "search_and_read", {"query": "q"}, egress=False))
    assert reply["ok"] is False
    assert reply["egress_denied"] is True
    # Refused before any socket — neither search nor fetch left the appliance.
    assert http.calls == []


# ── degradation ─────────────────────────────────────────────────────

def test_search_and_read_surfaces_an_offline_search(workspace):
    http = RaisingHTTP(ConnectionError("connection refused"))
    agent = agent_for(workspace, http)
    reply = agent.handle(envelope(
        "search_and_read", {"query": "q"}, egress=True))
    assert reply["ok"] is False
    assert reply["offline"] is True
    # It tried the search once and never reached a fetch.
    assert http.calls == [SEARXNG + "/search"]


def test_search_and_read_reports_no_results(workspace):
    http = FakeHTTP(FakeResponse(body=searxng_json()))  # zero results
    agent = agent_for(workspace, http)
    reply = agent.handle(envelope(
        "search_and_read", {"query": "q"}, egress=True))
    assert reply["ok"] is False
    assert reply["results"] == []
    assert "no search results" in reply["error"]
    # The search ran; there was nothing to read, so no fetch.
    assert http.calls == [SEARXNG + "/search"]
