"""Regression tests for mech-search-timeout.

The bug: ``tools.searxng_timeout`` defaulted to 5.0s while a real SearXNG relay
was observed returning a full ~19KB result set at ~10s, so real searches were
being cut off before they could answer. On timeout, ``_web_search`` also
collapsed into the same "offline" reply as a genuinely unreachable service, so
the chat surface never told the household a search had actually been
attempted and had timed out.

This file covers:
* the raised config default (20.0s) and the ``PUFFPAI_SEARXNG_TIMEOUT`` env
  override (config.py);
* ``_web_search`` distinguishing ``reason: timeout`` from ``reason: unreachable``
  / ``reason: refused``, all under ``status: "search_failed"``, and never
  fabricating a search-results reply on timeout (toolsmith.py);
* the normal (non-timeout) path is unchanged;
* the ledger line for a failed attempt carries no URL/query.

Run: pytest tests/test_mech_search_timeout.py -q
"""

from __future__ import annotations

import json
import os

import pytest
import requests

from puffpai.agents.envelope import Budget, Envelope
from puffpai.agents.toolsmith import ToolsmithAgent
from puffpai import config as config_mod

SEARXNG = "http://127.0.0.1:8888"
PUBLIC_IP = "93.184.216.34"


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
    """Queued responses; records every URL it was asked for."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        return self.responses.pop(0)


class RaisingHTTP:
    """A transport whose ``get`` always raises the given exception."""

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


# ── config: raised default + env override ───────────────────────────

def test_searxng_timeout_default_is_20_seconds():
    from puffpai.config import ToolsConfig
    assert ToolsConfig().searxng_timeout == 20.0


def test_searxng_timeout_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_SEARXNG_TIMEOUT", "2.5")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "puffpai_config.json").write_text("{}", encoding="utf-8")
    cfg = config_mod._build_config(str(tmp_path))
    assert cfg.tools.searxng_timeout == 2.5


def test_searxng_timeout_env_override_ignores_garbage(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PUFFPAI_SEARXNG_TIMEOUT", "not-a-number")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "puffpai_config.json").write_text("{}", encoding="utf-8")
    cfg = config_mod._build_config(str(tmp_path))
    # Falls back to the file/default value rather than crashing config load.
    assert cfg.tools.searxng_timeout == 20.0


# ── _web_search: honest timeout vs. unreachable vs. refused ─────────

def test_web_search_timeout_is_status_search_failed(workspace):
    http = RaisingHTTP(requests.exceptions.Timeout("read timed out"))
    agent = search_agent(workspace, http, timeout=20.0)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["ok"] is False
    assert reply["status"] == "search_failed"
    assert reply["reason"] == "timeout"
    assert "timed out" in reply["error"]
    assert "20" in reply["error"]
    # A timeout is not the same fact as "the appliance is offline" — it tried.
    assert reply["offline"] is False
    # No fabricated results on a timeout.
    assert "results" not in reply


def test_web_search_unreachable_is_status_search_failed_not_timeout(workspace):
    http = RaisingHTTP(ConnectionError("connection refused"))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["ok"] is False
    assert reply["status"] == "search_failed"
    assert reply["reason"] == "unreachable"
    assert reply["offline"] is True


def test_web_search_normal_path_unchanged(workspace):
    body = searxng_json({"title": "T", "url": "https://example.com/a",
                          "content": "snippet"})
    http = FakeHTTP(FakeResponse(body=body))
    agent = search_agent(workspace, http)
    reply = agent.handle(envelope("web_search", {"query": "q"}, egress=True))
    assert reply["ok"] is True
    assert reply["count"] == 1
    assert "status" not in reply
    assert "reason" not in reply


def test_web_search_timeout_ledger_line_has_no_url_or_query(workspace, monkeypatch):
    recorded = []

    def fake_record_external(door_id, ok, **kw):
        recorded.append((door_id, ok, kw))

    from puffpai.security import egress_doors
    monkeypatch.setattr(egress_doors, "record_external", fake_record_external)

    http = RaisingHTTP(requests.exceptions.Timeout("read timed out"))
    agent = search_agent(workspace, http, timeout=20.0)
    agent.handle(envelope("web_search", {"query": "super secret query"},
                          egress=True))

    assert recorded, "expected a ledger line for the failed attempt"
    door_id, ok, kw = recorded[-1]
    assert door_id == "web_search"
    assert ok is False
    assert kw.get("reason_code") == "timeout"
    blob = json.dumps(kw)
    assert "super secret query" not in blob
    assert "http://" not in blob and "https://" not in blob
