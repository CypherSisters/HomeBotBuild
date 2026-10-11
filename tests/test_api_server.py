"""Tests for api_server.py: Flask endpoint tests using test_client."""

import json
import os
import sys
import threading
import time
import pytest
from unittest.mock import patch, MagicMock

from tests.modpatch import patched_modules

# ---------------------------------------------------------------------------
# The api_server module does heavy module-level work (imports FirewallManager,
# loads config, generates API keys). We patch carefully before importing.
# ---------------------------------------------------------------------------

# Ensure src/puffpai is on the path
_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


@pytest.fixture(scope="module")
def api_app():
    """Import api_server lazily and return the Flask app + its API key."""
    # Mock FirewallManager so we don't need Windows netsh
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    mock_fw.block_device.return_value = {"success": True}
    mock_fw.unblock_device.return_value = {"success": True}
    mock_fw.lockdown_mode.return_value = {"success": True}

    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        import importlib
        # Need to re-import to get a fresh module with our mock
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")

        # Get the actual API key so we can authenticate test requests
        api_key = mod._API_KEY
        app = mod.app
        app.config['TESTING'] = True
        yield app, api_key, mod


@pytest.fixture()
def client(api_app):
    """Provide a fresh test client per test."""
    app, api_key, mod = api_app
    # Reset rate limiter between tests
    mod._rate_limit_store.clear()
    with app.test_client() as c:
        yield c, api_key, mod


# ===========================================================================
# Health & model listing (public endpoints, no auth)
# ===========================================================================

class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        c, _, _ = client
        resp = c.get('/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['status'] == 'ok'
        assert data['service'] == 'PuffPAI API'


class TestSystemStatusVoice:
    """/system/status must PROBE voice engines, not hardcode 'available'."""

    def _fake_engine(self, available):
        eng = MagicMock()
        eng.is_available.return_value = available
        return eng

    def _get_voice(self, client, tts_ok, stt_ok):
        c, api_key, mod = client
        with patch.object(mod, '_get_tts', return_value=self._fake_engine(tts_ok)), \
             patch.object(mod, '_get_stt', return_value=self._fake_engine(stt_ok)):
            resp = c.get('/system/status',
                         headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 200
        return resp.get_json()['voice']

    def test_voice_available_when_both_engines_up(self, client):
        voice = self._get_voice(client, True, True)
        assert voice['status'] == 'available'
        assert voice['tts_available'] is True
        assert voice['stt_available'] is True

    def test_voice_degraded_when_one_engine_up(self, client):
        voice = self._get_voice(client, True, False)
        assert voice['status'] == 'degraded'

    def test_voice_unavailable_when_both_down(self, client):
        # The regression guard: engines down must NOT report 'available'.
        voice = self._get_voice(client, False, False)
        assert voice['status'] == 'unavailable'

    def test_voice_unavailable_when_probe_raises(self, client):
        c, api_key, mod = client
        with patch.object(mod, '_get_tts', side_effect=RuntimeError('boom')):
            resp = c.get('/system/status',
                         headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 200
        assert resp.get_json()['voice']['status'] == 'unavailable'


class TestSystemStatusOnlineSearch:
    """/system/status must surface the THREE online-search preconditions
    (loop / egress opt-in / SearXNG) so a tester sees which one is unmet."""

    def _probe(self, client, loop_on, egress_on, searxng_status):
        """Drive the readiness block with all HTTP mocked (no real network).

        ``searxng_status`` is an int status code, or an Exception instance to
        simulate an unreachable host.
        """
        c, api_key, mod = client

        def fake_get(url, *a, **kw):
            if isinstance(searxng_status, Exception):
                raise searxng_status
            r = MagicMock()
            r.status_code = searxng_status
            return r

        # The opt-in lives in egress_policy.enabled, not preferences.flag:
        # chat_allow_egress has an owner-overlay tier so
        # it can be flipped from Settings instead of by editing a config the
        # hardened unit keeps read-only. /system/status reads the SAME resolver
        # the budget-minting path uses, so patch that one — patching
        # preferences.flag here would drive nothing and quietly assert the
        # default.
        with patch('puffpai.agents.planning_loop.planning_loop_enabled',
                   return_value=loop_on), \
             patch('puffpai.security.egress_policy.enabled',
                   return_value=egress_on), \
             patch('requests.get', side_effect=fake_get):
            resp = c.get('/system/status',
                         headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 200
        return resp.get_json()['online_search']

    def test_ready_when_all_three_preconditions_met(self, client):
        s = self._probe(client, loop_on=True, egress_on=True, searxng_status=200)
        assert s['status'] == 'ready'
        assert s['planning_loop_enabled'] is True
        assert s['chat_allow_egress'] is True
        assert s['searxng']['configured'] is True
        assert s['searxng']['reachable'] is True

    def test_not_ready_when_loop_off(self, client):
        s = self._probe(client, loop_on=False, egress_on=True, searxng_status=200)
        assert s['status'] == 'not_ready'
        assert s['planning_loop_enabled'] is False

    def test_not_ready_when_egress_opt_in_off(self, client):
        s = self._probe(client, loop_on=True, egress_on=False, searxng_status=200)
        assert s['status'] == 'not_ready'
        assert s['chat_allow_egress'] is False

    def test_not_ready_when_searxng_unreachable(self, client):
        s = self._probe(client, loop_on=True, egress_on=True,
                        searxng_status=ConnectionError('refused'))
        assert s['status'] == 'not_ready'
        assert s['searxng']['configured'] is True
        assert s['searxng']['reachable'] is False

    def test_searxng_5xx_is_not_reachable(self, client):
        s = self._probe(client, loop_on=True, egress_on=True, searxng_status=503)
        assert s['status'] == 'not_ready'
        assert s['searxng']['reachable'] is False


class TestModelListing:
    def test_openai_models_format(self, client):
        c, _, _ = client
        resp = c.get('/v1/models')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['object'] == 'list'
        assert len(data['data']) == 1
        assert data['data'][0]['id'] == 'puffpai'
        assert data['data'][0]['owned_by'] == 'puffpai-local'

    def test_ollama_tags_format(self, client):
        c, _, _ = client
        resp = c.get('/api/tags')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'models' in data
        assert data['models'][0]['name'] == 'puffpai'
        assert data['models'][0]['details']['family'] == 'puffpai'


# ===========================================================================
# Authentication
# ===========================================================================

class TestAuthentication:
    def test_protected_endpoint_401_without_key(self, client):
        c, _, _ = client
        resp = c.get('/firewall/status')
        assert resp.status_code == 401
        data = resp.get_json()
        assert 'Unauthorized' in data['error']

    def test_bearer_auth_succeeds(self, client):
        c, api_key, _ = client
        resp = c.get('/firewall/status',
                      headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 200

    def test_x_api_key_header_succeeds(self, client):
        c, api_key, _ = client
        resp = c.get('/firewall/status',
                      headers={'X-API-Key': api_key})
        assert resp.status_code == 200

    def test_wrong_api_key_401(self, client):
        c, _, _ = client
        resp = c.get('/firewall/status',
                      headers={'Authorization': 'Bearer wrong_key_123'})
        assert resp.status_code == 401

    def test_public_endpoints_no_auth(self, client):
        """Health and model endpoints should not require auth."""
        c, _, _ = client
        for url in ['/health', '/v1/models', '/api/tags']:
            resp = c.get(url)
            assert resp.status_code == 200, f"{url} returned {resp.status_code}"


# ===========================================================================
# CORS
# ===========================================================================

class TestCORS:
    def test_localhost_origin_allowed(self, client):
        c, _, _ = client
        resp = c.get('/health', headers={'Origin': 'http://localhost:8000'})
        assert resp.headers.get('Access-Control-Allow-Origin') == 'http://localhost:8000'

    def test_127_origin_allowed(self, client):
        c, _, _ = client
        resp = c.get('/health', headers={'Origin': 'http://127.0.0.1:3000'})
        assert resp.headers.get('Access-Control-Allow-Origin') == 'http://127.0.0.1:3000'

    def test_external_origin_gets_default(self, client):
        c, _, _ = client
        resp = c.get('/health', headers={'Origin': 'https://evil.com'})
        # Should fall back to default localhost origin, not evil.com
        allowed = resp.headers.get('Access-Control-Allow-Origin', '')
        assert 'evil.com' not in allowed

    def test_cors_methods_header(self, client):
        c, _, _ = client
        resp = c.get('/health')
        methods = resp.headers.get('Access-Control-Allow-Methods', '')
        assert 'GET' in methods
        assert 'POST' in methods


# ===========================================================================
# Rate Limiting
# ===========================================================================

class TestRateLimiting:
    def test_under_limit_succeeds(self, client):
        c, _, _ = client
        for _ in range(5):
            resp = c.get('/health')
            assert resp.status_code == 200

    def test_over_limit_returns_429(self, client, monkeypatch):
        c, _, mod = client
        # Production limit (600/min) is too high to hit in a test; shrink it
        # so the 429 path is actually exercised.
        monkeypatch.setattr(mod, '_RATE_LIMIT', 10)
        mod._rate_limit_store.clear()
        for _ in range(10):
            resp = c.get('/health')
            assert resp.status_code == 200
        # Request 11 exceeds the limit
        resp = c.get('/health')
        assert resp.status_code == 429
        data = resp.get_json()
        assert 'Rate limit' in data['error']

    def test_x_forwarded_for_ignored_by_default(self, client, monkeypatch):
        """Without a trusted proxy, XFF must not let one client split buckets."""
        c, _, mod = client
        monkeypatch.setattr(mod, '_RATE_LIMIT', 10)
        mod._rate_limit_store.clear()
        env = {'REMOTE_ADDR': '192.168.50.99'}
        for i in range(10):
            resp = c.get('/health',
                         headers={'X-Forwarded-For': f'203.0.113.{i}'},
                         environ_base=env)
            assert resp.status_code == 200
        resp = c.get('/health',
                     headers={'X-Forwarded-For': '203.0.113.250'},
                     environ_base=env)
        assert resp.status_code == 429
        # Only the socket peer ever became a bucket key.
        assert {ip for ip, _scope in mod._rate_limit_store} == {'192.168.50.99'}

    def test_module_app_not_proxy_wrapped_by_default(self, client):
        c, _, mod = client
        assert mod._configured_proxy_hops() == 0
        assert type(mod.app.wsgi_app).__name__ != 'ProxyFix'

    def test_distinct_client_ips_get_distinct_buckets(self, client, monkeypatch):
        c, _, mod = client
        monkeypatch.setattr(mod, '_RATE_LIMIT', 5)
        mod._rate_limit_store.clear()
        for _ in range(5):
            assert c.get('/health',
                         environ_base={'REMOTE_ADDR': '192.168.50.10'}).status_code == 200
        assert c.get('/health',
                     environ_base={'REMOTE_ADDR': '192.168.50.10'}).status_code == 429
        # A different client IP is unaffected by the exhausted bucket.
        assert c.get('/health',
                     environ_base={'REMOTE_ADDR': '192.168.50.11'}).status_code == 200

    def test_public_flood_does_not_exhaust_auth_budget(self, client, monkeypatch):
        """A /health flood must not lock the same IP out of authed endpoints."""
        c, api_key, mod = client
        monkeypatch.setattr(mod, '_RATE_LIMIT', 10)
        mod._rate_limit_store.clear()
        env = {'REMOTE_ADDR': '192.168.50.140'}
        for _ in range(10):
            assert c.get('/health', environ_base=env).status_code == 200
        assert c.get('/health', environ_base=env).status_code == 429
        resp = c.get('/firewall/status',
                     headers={'Authorization': f'Bearer {api_key}'},
                     environ_base=env)
        assert resp.status_code == 200
        assert ('192.168.50.140', 'public') in mod._rate_limit_store
        assert ('192.168.50.140', 'auth') in mod._rate_limit_store

    def test_failed_auth_flood_is_throttled(self, client, monkeypatch):
        """API-key brute force must hit the limiter, not slip past it via 401."""
        c, _, mod = client
        monkeypatch.setattr(mod, '_RATE_LIMIT', 5)
        mod._rate_limit_store.clear()
        env = {'REMOTE_ADDR': '192.168.50.66'}
        for _ in range(5):
            resp = c.get('/firewall/status',
                         headers={'Authorization': 'Bearer wrong_key_123'},
                         environ_base=env)
            assert resp.status_code == 401
        resp = c.get('/firewall/status',
                     headers={'Authorization': 'Bearer wrong_key_123'},
                     environ_base=env)
        assert resp.status_code == 429

    def test_prune_drops_expired_entries(self, client, monkeypatch):
        """Buckets whose window has expired must not survive the sweep."""
        c, _, mod = client
        stale = time.time() - 3600
        mod._rate_limit_store[('172.16.0.1', 'public')] = (5, stale)
        mod._rate_limit_store[('192.168.50.2', 'auth')] = (99, stale)
        # Force the sweep to be due on this request.
        monkeypatch.setattr(mod, '_rate_limit_last_prune', 0.0)
        resp = c.get('/health', environ_base={'REMOTE_ADDR': '192.168.50.3'})
        assert resp.status_code == 200
        assert ('172.16.0.1', 'public') not in mod._rate_limit_store
        assert ('192.168.50.2', 'auth') not in mod._rate_limit_store
        assert ('192.168.50.3', 'public') in mod._rate_limit_store

    def test_prune_keeps_live_entries(self, client, monkeypatch):
        """The sweep must not reset an in-window bucket's count."""
        c, _, mod = client
        now = time.time()
        mod._rate_limit_store[('192.168.50.10', 'public')] = (7, now - 1)
        mod._rate_limit_store[('192.168.50.11', 'public')] = (3, now - 3600)
        monkeypatch.setattr(mod, '_rate_limit_last_prune', 0.0)
        resp = c.get('/health', environ_base={'REMOTE_ADDR': '192.168.50.12'})
        assert resp.status_code == 200
        assert ('192.168.50.11', 'public') not in mod._rate_limit_store
        assert mod._rate_limit_store[('192.168.50.10', 'public')] == (7, now - 1)

    def test_store_cap_evicts_oldest_windows_first(self, client, monkeypatch):
        """Over the cap, oldest window_start goes first and the caller stays."""
        c, _, mod = client
        monkeypatch.setattr(mod, '_RATE_STORE_MAX', 5)
        low_water = int(5 * 0.9)  # 4 — eviction trims below the cap, not to it
        # Sweep already ran this window, so only the cap path can shrink the
        # store; the seeded entries are all in-window regardless.
        monkeypatch.setattr(mod, '_rate_limit_last_prune', time.time())
        now = time.time()
        for i in range(8):
            mod._rate_limit_store[(f'172.16.0.{i}', 'public')] = (1, now - 8 + i)
        resp = c.get('/health', environ_base={'REMOTE_ADDR': '192.168.50.100'})
        assert resp.status_code == 200
        assert len(mod._rate_limit_store) == low_water
        assert ('192.168.50.100', 'public') in mod._rate_limit_store
        # 9 entries trimmed to the low-water mark: the 5 oldest windows go.
        for i in range(5):
            assert (f'172.16.0.{i}', 'public') not in mod._rate_limit_store
        for i in range(5, 8):
            assert (f'172.16.0.{i}', 'public') in mod._rate_limit_store
        # Hysteresis: the next request refills toward the cap without paying
        # another eviction pass — nothing further may vanish.
        survivors = set(mod._rate_limit_store)
        resp = c.get('/health', environ_base={'REMOTE_ADDR': '192.168.50.101'})
        assert resp.status_code == 200
        assert survivors < set(mod._rate_limit_store)
        assert len(mod._rate_limit_store) == low_water + 1

    def test_concurrent_requests_do_not_race_the_store(self, client, monkeypatch):
        """threaded=True: sweep + count must not blow up under parallel load."""
        _c, _, mod = client
        monkeypatch.setattr(mod, '_RATE_LIMIT', 10_000)
        monkeypatch.setattr(mod, '_RATE_STORE_MAX', 10_000)
        monkeypatch.setattr(mod, '_rate_limit_last_prune', 0.0)
        env = {'REMOTE_ADDR': '172.16.0.1'}
        statuses = []
        errors = []
        collect = threading.Lock()

        def worker():
            try:
                # One client per thread: Flask test clients are not thread-safe.
                with mod.app.test_client() as tc:
                    for _ in range(7):
                        code = tc.get('/health', environ_base=env).status_code
                        with collect:
                            statuses.append(code)
            except Exception as exc:  # noqa: BLE001 - surfaced via assert below
                with collect:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads)
        assert errors == []
        assert len(statuses) == 56
        assert set(statuses) == {200}


class TestProxyFixOptIn:
    """_apply_proxy_fix is opt-in; the module app is left unwrapped (see above)."""

    def _probe_app(self, mod, hops):
        from flask import Flask, request as flask_request
        probe = Flask(f'proxyfix_probe_{hops}')
        probe.config['TESTING'] = True

        @probe.route('/whoami')
        def whoami():
            return {'remote_addr': flask_request.remote_addr}

        wrapped = mod._apply_proxy_fix(probe, hops)
        return probe, wrapped

    def test_zero_hops_does_not_wrap(self, client):
        _, _, mod = client
        probe, wrapped = self._probe_app(mod, 0)
        assert wrapped is False
        assert type(probe.wsgi_app).__name__ != 'ProxyFix'
        resp = probe.test_client().get(
            '/whoami', headers={'X-Forwarded-For': '203.0.113.7'},
            environ_base={'REMOTE_ADDR': '172.16.0.1'})
        assert resp.get_json()['remote_addr'] == '172.16.0.1'

    def test_one_hop_trusts_last_forwarded_value(self, client):
        _, _, mod = client
        probe, wrapped = self._probe_app(mod, 1)
        assert wrapped is True
        assert type(probe.wsgi_app).__name__ == 'ProxyFix'
        # One trusted hop => the last XFF entry is the address the proxy saw.
        resp = probe.test_client().get(
            '/whoami',
            headers={'X-Forwarded-For': '198.51.100.9, 203.0.113.7'},
            environ_base={'REMOTE_ADDR': '172.16.0.1'})
        assert resp.get_json()['remote_addr'] == '203.0.113.7'


# ===========================================================================
# Conversation endpoint
# ===========================================================================

class TestConversation:
    def test_empty_text_returns_400(self, client):
        c, api_key, _ = client
        resp = c.post('/conversation',
                       json={'text': ''},
                       headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False

    def test_missing_text_returns_400(self, client):
        c, api_key, _ = client
        resp = c.post('/conversation',
                       json={},
                       headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 400


# ===========================================================================
# App data endpoints
# ===========================================================================

class TestAppEndpoints:
    def test_invalid_app_returns_404(self, client):
        c, api_key, _ = client
        resp = c.get('/apps/nonexistent/data',
                     headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 404

    def test_forbidden_action_returns_403(self, client):
        c, api_key, _ = client
        resp = c.post('/apps/alarmflow/action',
                       headers={'Authorization': f'Bearer {api_key}'},
                       json={'action': 'dangerous_method', 'params': {}})
        assert resp.status_code == 403
        data = resp.get_json()
        assert 'not allowed' in data['error']

    def test_missing_action_returns_400(self, client):
        c, api_key, _ = client
        resp = c.post('/apps/alarmflow/action',
                       headers={'Authorization': f'Bearer {api_key}'},
                       json={'params': {}})
        assert resp.status_code == 400

    def test_invalid_app_updated_404(self, client):
        c, api_key, _ = client
        resp = c.get('/apps/invalid_app/updated',
                     headers={'Authorization': f'Bearer {api_key}'})
        assert resp.status_code == 404


# ===========================================================================
# Voice format helpers (pure functions, no network needed)
# ===========================================================================

class TestCleanResponse:
    def test_strips_bold_markdown(self, client):
        _, _, mod = client
        result = mod._clean_response("**Hello** world")
        assert "**" not in result
        assert "Hello" in result

    def test_strips_headers(self, client):
        _, _, mod = client
        result = mod._clean_response("## Title\nSome text")
        assert "##" not in result
        assert "Title" in result

    def test_strips_code_blocks(self, client):
        _, _, mod = client
        result = mod._clean_response("```python\nprint('hi')\n```")
        assert "```" not in result

    def test_strips_bullet_markers(self, client):
        _, _, mod = client
        result = mod._clean_response("- item one\n- item two")
        assert "- " not in result
        assert "item one" in result


class TestCleanForVoice:
    def test_truncates_long_text(self, client):
        _, _, mod = client
        long_text = "word " * 1000
        result = mod.clean_for_voice(long_text, max_length=100)
        assert len(result) <= 200  # max_length + suffix
        assert "summary" in result.lower()

    def test_strips_markdown_links(self, client):
        _, _, mod = client
        result = mod.clean_for_voice("[click here](http://example.com)")
        assert "http" not in result


class TestConvertOpenAIMessages:
    def test_extracts_user_message(self, client):
        _, _, mod = client
        messages = [
            {"role": "user", "content": "Hello"}
        ]
        user_msg, history = mod._convert_openai_messages_to_history(messages)
        assert user_msg == "Hello"
        assert history == []

    def test_filters_system_messages(self, client):
        _, _, mod = client
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hi"}
        ]
        user_msg, _ = mod._convert_openai_messages_to_history(messages)
        assert user_msg == "Hi"

    def test_builds_history_from_pairs(self, client):
        _, _, mod = client
        messages = [
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": "Thanks!"}
        ]
        user_msg, history = mod._convert_openai_messages_to_history(messages)
        assert user_msg == "Thanks!"
        assert len(history) == 1
        assert history[0]['user'] == "What is 2+2?"
        assert history[0]['assistant'] == "4"

    def test_empty_messages_returns_none(self, client):
        _, _, mod = client
        user_msg, history = mod._convert_openai_messages_to_history([])
        assert user_msg is None
        assert history == []


# ===========================================================================
# ZIM full-text -> RAG context
# ===========================================================================

def _fts_hit(title, relevance, mode, path=None):
    return {
        'library_id': 'water_pack',
        'path': path or f'C/{title.replace(" ", "_")}',
        'title': title,
        'snippet': f'<b>{title}</b>',
        'relevance': relevance,
        'match_mode': mode,
    }


class TestTrimOrTail:
    """The OR fallback's tail is stopword noise and must not reach the LLM.

    FTSSearcher retries a query with terms OR-ed when no document contains
    them all. bm25's IDF weighting makes the top hit reliable, but the rest
    are typically documents that share only the question's stopwords.
    """

    def test_or_tail_below_margin_is_dropped(self, client):
        _, _, mod = client
        # The live repro: "According to the Kaito KA700 manual in my library,
        # what radio is the manual for and what bands does it cover?"
        hits = [
            _fts_hit("Kaito KA700 owner's manual", 1.0, 'or'),
            _fts_hit("What to plant", 0.68, 'or'),
            _fts_hit("Using your library", 0.59, 'or'),
        ]
        kept = mod._trim_or_tail(hits)
        assert [h['title'] for h in kept] == ["Kaito KA700 owner's manual"]

    def test_or_hits_within_margin_are_kept(self, client):
        _, _, mod = client
        hits = [
            _fts_hit("Primary", 1.0, 'or'),
            _fts_hit("Close second", 0.80, 'or'),
            _fts_hit("Just inside", 0.75, 'or'),
            _fts_hit("Just outside", 0.74, 'or'),
        ]
        kept = mod._trim_or_tail(hits)
        assert [h['title'] for h in kept] == ["Primary", "Close second",
                                              "Just inside"]

    def test_and_hits_are_never_trimmed(self, client):
        _, _, mod = client
        hits = [
            _fts_hit("Best", 1.0, 'and'),
            _fts_hit("Weaker", 0.68, 'and'),
            _fts_hit("Weakest", 0.51, 'and'),
        ]
        assert mod._trim_or_tail(hits) == hits

    def test_cutoff_uses_best_score_not_first_position(self, client):
        """Results are interleaved round-robin, not sorted by relevance."""
        _, _, mod = client
        hits = [
            _fts_hit("Shard A best", 0.60, 'or'),
            _fts_hit("Shard B best", 1.00, 'or'),
            _fts_hit("Shard A tail", 0.55, 'or'),
        ]
        kept = mod._trim_or_tail(hits)
        assert [h['title'] for h in kept] == ["Shard B best"]

    def test_empty_and_single_hit_sets(self, client):
        _, _, mod = client
        assert mod._trim_or_tail([]) == []
        single = [_fts_hit("Only", 1.0, 'or')]
        assert mod._trim_or_tail(single) == single

    def test_missing_match_mode_is_treated_as_and(self, client):
        """Defensive: a hit shaped by older code must not be silently dropped."""
        _, _, mod = client
        hits = [{'library_id': 'l', 'path': 'C/x', 'title': 'X',
                 'snippet': '', 'relevance': 1.0},
                {'library_id': 'l', 'path': 'C/y', 'title': 'Y',
                 'snippet': '', 'relevance': 0.4}]
        assert mod._trim_or_tail(hits) == hits


class TestZimFtsRagContribution:
    """End-to-end through _get_zim_fts_results, with the ZIM reader stubbed."""

    @staticmethod
    def _install(mod, monkeypatch, hits):
        class _StubSearcher:
            def search(self, question, max_results=5):
                return hits

        class _StubArticle:
            plain_text = "Article body text about radios and bands." * 20

        class _StubReader:
            def get_article_by_url(self, url):
                return _StubArticle()

        monkeypatch.setattr(mod, '_fts_searcher', lambda: _StubSearcher())
        monkeypatch.setattr(mod, '_discover_zim_libraries',
                            lambda force=False: {'water_pack': '/zim/water.zim'})
        monkeypatch.setattr(mod, '_get_zim_reader', lambda path: _StubReader())

    def test_or_result_set_contributes_only_the_top_hit(self, client, monkeypatch):
        _, _, mod = client
        self._install(mod, monkeypatch, [
            _fts_hit("Kaito KA700 owner's manual", 1.0, 'or'),
            _fts_hit("What to plant", 0.68, 'or'),
            _fts_hit("Using your library", 0.59, 'or'),
        ])
        rows = mod._get_zim_fts_results("According to the Kaito KA700 manual...")
        assert [r['title'] for r in rows] == ["Kaito KA700 owner's manual"]
        assert all(r['source_type'] == 'zim_fts' for r in rows)
        # Remapped into the RAG band, comfortably above the 0.25 floor.
        assert rows[0]['relevance'] == pytest.approx(0.85)

    def test_and_result_set_keeps_every_hit(self, client, monkeypatch):
        _, _, mod = client
        self._install(mod, monkeypatch, [
            _fts_hit("Kaito KA700 owner's manual", 1.0, 'and'),
            _fts_hit("Shortwave listening guide", 0.68, 'and'),
            _fts_hit("Antenna basics", 0.51, 'and'),
        ])
        rows = mod._get_zim_fts_results("Kaito KA700 radio bands")
        assert len(rows) == 3
        # Every row clears the RAG relevance floor of 0.25.
        assert all(r['relevance'] >= 0.25 for r in rows)


# ===========================================================================
# ZIM library display names (embedded M/* metadata)
# ===========================================================================

class _MetaArticle:
    def __init__(self, text):
        self.text = text


class _MetaReader:
    """Fake ZIMReader exposing only the M namespace."""

    def __init__(self, meta, raises=False):
        self.meta = meta
        self.raises = raises

    def get_article_by_url(self, url):
        if self.raises:
            raise RuntimeError("simulated ZIM read failure")
        value = self.meta.get(url)
        return _MetaArticle(value) if value is not None else None


def _no_rag():
    raise RuntimeError("chromadb unavailable in this test")


@pytest.fixture()
def zim_meta_env(client, monkeypatch, tmp_path):
    """Install fake ZIM packs whose embedded metadata the registry reads."""
    _, _, mod = client
    mod._zim_metadata_cache.clear()
    opens = []

    def make(packs, raises=False):
        """packs: {filename: {'M/Title': ..., 'M/Description': ...}}"""
        from puffpai.knowledge.fts_index import library_id_for
        libs = {}
        readers = {}
        for filename, meta in packs.items():
            path = str(tmp_path / filename)
            with open(path, 'wb') as fh:
                fh.write(b'\0' * 64)
            libs[library_id_for(path)] = path
            readers[path] = _MetaReader(meta, raises=raises)

        def _get_reader(path):
            opens.append(path)
            return readers.get(path)

        monkeypatch.setattr(mod, '_discover_zim_libraries',
                            lambda force=False: dict(libs))
        monkeypatch.setattr(mod, '_get_zim_reader', _get_reader)
        monkeypatch.setattr(mod, '_load_library_manifest', lambda: [])
        monkeypatch.setattr(mod, '_fts_ready_libraries', lambda: set())
        monkeypatch.setattr(mod, '_get_dual_zone_rag', _no_rag)
        return libs

    return mod, make, opens


def _zim_rows(client_tuple):
    c, api_key, _ = client_tuple
    resp = c.get('/knowledge/libraries', headers={'X-API-Key': api_key})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['success'] is True
    return {row['library_id']: row
            for row in body['libraries'] if row['source'] == 'zim'}


class TestZimLibraryNames:

    def test_title_comes_from_embedded_metadata(self, client, zim_meta_env):
        mod, make, _ = zim_meta_env
        make({
            'biologycourses_en_all_2025-10.zim': {
                'M/Title': 'Biology Courses',
                'M/Description': 'LibreTexts biology course collection',
                'M/Language': 'eng'},
        })
        row = _zim_rows(client)['biologycourses_en_all_2025_10']
        assert row['name'] == 'Biology Courses'
        assert row['description'] == 'LibreTexts biology course collection'
        assert row['language'] == 'eng'
        # Display layer only: the routing keys are untouched.
        assert row['library_id'] == 'biologycourses_en_all_2025_10'
        assert row['collection'] == 'biologycourses_en_all_2025_10'

    def test_falls_back_to_filename_when_metadata_missing(self, client,
                                                          zim_meta_env):
        mod, make, _ = zim_meta_env
        make({'zimgit_post_disaster_en_2024-05.zim': {}})
        row = _zim_rows(client)['zimgit_post_disaster_en_2024_05']
        assert row['name'] == 'zimgit post disaster en 2024 05'
        assert row['description'] == ''
        assert row['language'] == ''

    def test_blank_title_falls_back_to_filename(self, client, zim_meta_env):
        mod, make, _ = zim_meta_env
        make({'some_pack_2024.zim': {'M/Title': '   '}})
        assert _zim_rows(client)['some_pack_2024']['name'] == 'some pack 2024'

    def test_metadata_read_failure_does_not_500(self, client, zim_meta_env):
        mod, make, _ = zim_meta_env
        make({'broken_pack_2024.zim': {'M/Title': 'Never Read'}}, raises=True)
        assert _zim_rows(client)['broken_pack_2024']['name'] == \
            'broken pack 2024'

    def test_truncated_pack_is_skipped_not_fatal(self, client, zim_meta_env,
                                                 monkeypatch):
        mod, make, _ = zim_meta_env
        make({'truncated_pack_2024.zim': {'M/Title': 'Unreachable'}})
        monkeypatch.setattr(mod, '_get_zim_reader', lambda path: None)
        mod._zim_metadata_cache.clear()
        assert _zim_rows(client)['truncated_pack_2024']['name'] == \
            'truncated pack 2024'

    def test_metadata_is_cached_across_requests(self, client, zim_meta_env):
        mod, make, opens = zim_meta_env
        make({'wikipedia_en_all_maxi_2025-08.zim': {'M/Title': 'Wikipedia'}})
        _zim_rows(client)
        after_first = len(opens)
        assert after_first >= 1
        _zim_rows(client)
        # A second listing must not reopen the archive.
        assert len(opens) == after_first

    def test_cache_invalidates_when_the_file_changes(self, client,
                                                     zim_meta_env):
        mod, make, opens = zim_meta_env
        libs = make({'pack_2024.zim': {'M/Title': 'First'}})
        assert _zim_rows(client)['pack_2024']['name'] == 'First'
        before = len(opens)
        with open(libs['pack_2024'], 'wb') as fh:
            fh.write(b'\0' * 4096)      # size changes -> cache key changes
        _zim_rows(client)
        assert len(opens) > before


class TestZimLibraryNameDisambiguation:

    def test_colliding_titles_get_distinct_qualifiers(self, client,
                                                      zim_meta_env):
        mod, make, _ = zim_meta_env
        make({
            'wikipedia_en_all_maxi_2025-08.zim': {'M/Title': 'Wikipedia'},
            'wikipedia_en_all_mini_2025-06.zim': {'M/Title': 'Wikipedia'},
            'wikipedia_en_all_nopic_2025-08.zim': {'M/Title': 'Wikipedia'},
            'wikipedia_en_physics_maxi_2025-08.zim': {'M/Title': 'Wikipedia'},
        })
        rows = _zim_rows(client)
        names = {r['name'] for r in rows.values()}
        assert len(names) == 4, names
        assert all(n.startswith('Wikipedia (') for n in names), names
        # Qualifiers are built from the tokens that actually differ.
        assert rows['wikipedia_en_all_maxi_2025_08']['name'] == \
            'Wikipedia (all maxi 2025-08)'
        assert rows['wikipedia_en_all_mini_2025_06']['name'] == \
            'Wikipedia (all mini 2025-06)'
        assert rows['wikipedia_en_physics_maxi_2025_08']['name'] == \
            'Wikipedia (physics maxi 2025-08)'

    def test_unique_titles_are_never_touched(self, client, zim_meta_env):
        mod, make, _ = zim_meta_env
        make({
            'wikipedia_en_all_maxi_2025-08.zim': {'M/Title': 'Wikipedia'},
            'knots_en_all_2024-01.zim': {'M/Title': 'A Library of Knots'},
            'amateurradio_en_all_2025-03.zim': {'M/Title': 'Amateur Radio Q&A'},
        })
        rows = _zim_rows(client)
        assert rows['wikipedia_en_all_maxi_2025_08']['name'] == 'Wikipedia'
        assert rows['knots_en_all_2024_01']['name'] == 'A Library of Knots'
        assert rows['amateurradio_en_all_2025_03']['name'] == \
            'Amateur Radio Q&A'

    def test_disambiguation_keeps_routing_keys_stable(self, client,
                                                      zim_meta_env):
        mod, make, _ = zim_meta_env
        make({
            'wikipedia_en_all_maxi_2025-08.zim': {'M/Title': 'Wikipedia'},
            'wikipedia_en_all_mini_2025-06.zim': {'M/Title': 'Wikipedia'},
        })
        for library_id, row in _zim_rows(client).items():
            assert row['library_id'] == library_id
            assert row['collection'] == library_id

    def test_ted_packs_keep_their_video_prefix(self, client, zim_meta_env):
        """The dashboard categorizes video by a 'ted ' name prefix."""
        mod, make, _ = zim_meta_env
        make({
            'ted_en_military_2024-06.zim': {'M/Title': 'TED military'},
            'ted_en_design_2024-06.zim': {'M/Title': 'TED design'},
        })
        for row in _zim_rows(client).values():
            assert row['name'].lower().startswith('ted ')

    def test_colliding_ted_titles_still_read_as_video(self, client,
                                                      zim_meta_env):
        """Two packs both titled "TED" collide; the qualifier keeps the
        dashboard's `startsWith("ted ")` check working."""
        mod, make, _ = zim_meta_env
        make({
            'ted_en_military_2024-06.zim': {'M/Title': 'TED'},
            'ted_en_design_2024-06.zim': {'M/Title': 'TED'},
        })
        for row in _zim_rows(client).values():
            assert row['name'].lower().startswith('ted ')

    def test_filename_fallback_names_also_disambiguate(self, client,
                                                       zim_meta_env):
        mod, make, _ = zim_meta_env
        make({'wikipedia_en_all_maxi_2025-08.zim': {},
              'wikipedia_en_all_mini_2025-06.zim': {}})
        assert len({r['name'] for r in _zim_rows(client).values()}) == 2


# ===========================================================================
# Knowledge upload size cap (/knowledge/upload)
# ===========================================================================

class TestKnowledgeUploadSizeCap:
    def test_oversized_upload_returns_413(self, client):
        c, api_key, _ = client
        # 51 MB of body. The guard runs on Content-Length, before the
        # multipart parser touches the payload.
        body = b'\0' * (51 * 1024 * 1024)
        resp = c.post('/knowledge/upload', data=body,
                      content_type='multipart/form-data; boundary=puffpai',
                      headers={'X-API-Key': api_key})
        assert resp.status_code == 413
        data = resp.get_json()
        assert data['success'] is False
        assert data['max_mb'] == 50


# ===========================================================================
# AI settings — avatar body + custom 3D model URL (/profile/ai)
# ===========================================================================

@pytest.fixture()
def ai_settings(client, monkeypatch, tmp_path):
    """PUT/GET helpers for /profile/ai backed by a throwaway settings file."""
    c, api_key, mod = client
    monkeypatch.setattr(mod, '_AI_SETTINGS_PATH',
                        str(tmp_path / 'ai_settings.json'))
    headers = {'Authorization': f'Bearer {api_key}'}

    def put(payload):
        return c.put('/profile/ai', json=payload, headers=headers)

    def get():
        resp = c.get('/profile/ai', headers=headers)
        assert resp.status_code == 200
        return resp.get_json()['settings']

    return put, get


class TestAvatarCharacter:
    def test_glb_is_accepted_and_round_trips(self, ai_settings):
        put, get = ai_settings
        assert put({'avatar_character': 'glb'}).status_code == 200
        assert get()['avatar_character'] == 'glb'

    def test_puff_and_human_still_accepted(self, ai_settings):
        put, get = ai_settings
        for character in ('human', 'puff'):
            assert put({'avatar_character': character}).status_code == 200
            assert get()['avatar_character'] == character

    def test_unknown_character_is_ignored(self, ai_settings):
        put, get = ai_settings
        put({'avatar_character': 'glb'})
        assert put({'avatar_character': 'dragon'}).status_code == 200
        assert get()['avatar_character'] == 'glb'


class TestAvatar3dUrl:
    def test_public_https_url_is_refused(self, ai_settings):
        """Absolute URLs are house-local only.

        A public URL saved here would have every dashboard in the house fetch a
        model off the internet with no door and no consent. The household's own
        model is uploaded instead (POST /profile/avatar-model), which stores a
        relative path. See _valid_avatar_url + security/lan_http.validate_url.
        """
        put, get = ai_settings
        put({'avatar_3d_url': '/Avatars/puff.glb'})
        url = 'https://models.example.com/puff/model.glb?v=2'
        assert put({'avatar_3d_url': url}).status_code == 200
        assert get()['avatar_3d_url'] == '/Avatars/puff.glb'

    def test_http_url_round_trips(self, ai_settings):
        put, get = ai_settings
        url = 'http://192.168.1.50:8080/models/puff.glb'
        assert put({'avatar_3d_url': url}).status_code == 200
        assert get()['avatar_3d_url'] == url

    def test_relative_path_is_accepted(self, ai_settings):
        put, get = ai_settings
        assert put({'avatar_3d_url': '/Avatars/puff.glb'}).status_code == 200
        assert get()['avatar_3d_url'] == '/Avatars/puff.glb'

    def test_empty_string_clears_a_previous_value(self, ai_settings):
        put, get = ai_settings
        put({'avatar_3d_url': 'http://10.0.0.7:8080/a.glb'})
        assert get()['avatar_3d_url'] == 'http://10.0.0.7:8080/a.glb'
        assert put({'avatar_3d_url': ''}).status_code == 200
        assert get()['avatar_3d_url'] == ''

    @pytest.mark.parametrize('bad', [
        'javascript:alert(1)',
        'JavaScript:alert(1)',
        'data:model/gltf+json;base64,eyJ9',
        'file:///C:/Windows/win.ini',
        'blob:https://example.com/1234',
        'vbscript:msgbox(1)',
        '//evil.example.com/model.glb',      # protocol-relative == off-origin
        'models/puff.glb',                   # relative without leading slash
        'https://',                          # no host
        ' https://example.com/a.glb',        # leading whitespace
        'java\tscript:alert(1)',             # control char smuggling
        'https://example.com/a.glb\nX',      # newline smuggling
        '/models/../../etc',                 # path traversal
        '/models/..%2f..%2fetc',             # traversal, encoded
        12345,                               # not a string
        None,
    ])
    def test_unsafe_values_leave_the_stored_url_unchanged(self, ai_settings, bad):
        put, get = ai_settings
        put({'avatar_3d_url': '/Avatars/puff.glb'})
        assert put({'avatar_3d_url': bad}).status_code == 200
        assert get()['avatar_3d_url'] == '/Avatars/puff.glb'

    def test_overlong_url_is_rejected(self, ai_settings):
        put, get = ai_settings
        put({'avatar_3d_url': '/Avatars/puff.glb'})
        long_url = 'https://example.com/' + ('a' * 3000) + '.glb'
        assert put({'avatar_3d_url': long_url}).status_code == 200
        assert get()['avatar_3d_url'] == '/Avatars/puff.glb'

    def test_url_at_the_length_limit_is_accepted(self, ai_settings):
        put, get = ai_settings
        prefix = '/Avatars/'
        url = prefix + 'a' * (2048 - len(prefix))
        assert len(url) == 2048
        assert put({'avatar_3d_url': url}).status_code == 200
        assert get()['avatar_3d_url'] == url

    def test_glb_character_and_url_persist_together(self, ai_settings):
        put, get = ai_settings
        assert put({'avatar_character': 'glb',
                    'avatar_3d_url': '/Avatars/custom.glb'}).status_code == 200
        settings = get()
        assert settings['avatar_character'] == 'glb'
        assert settings['avatar_3d_url'] == '/Avatars/custom.glb'


# ===========================================================================
# Agent event inflow (orchestration Phase 1)
# ===========================================================================

class TestAgentEventEndpoints:
    """POST /agents/event + GET /agents/events — auth, validation, caps."""

    @pytest.fixture
    def events(self, client, tmp_path, monkeypatch):
        """Isolate the event log and stub the bus so nothing reaches an agent."""
        c, api_key, _mod = client
        from puffpai import agent_events

        class StubBus:
            def send(self, envelope, to):
                return {'ok': True, 'decision': {'severity': 'info',
                                                 'summary': 'stub',
                                                 'suggested_action': 'logged'}}

            def get(self, name):
                return None

        monkeypatch.setattr(agent_events, 'EVENT_LOG_PATH',
                            str(tmp_path / 'agent_events.jsonl'))
        monkeypatch.setattr(agent_events, '_line_count', None)
        monkeypatch.setattr(agent_events, '_bus', StubBus())
        return c, {'X-API-Key': api_key}

    def test_event_requires_auth(self, client):
        c, _, _ = client
        resp = c.post('/agents/event',
                      json={'source': 'nodered', 'event_type': 'weather_alert'})
        assert resp.status_code == 401

    def test_events_list_requires_auth(self, client):
        c, _, _ = client
        assert c.get('/agents/events').status_code == 401

    def test_valid_event_returns_the_decision(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'nodered', 'event_type': 'weather_alert',
                            'data': {'count': 1, 'headlines': ['Flood Warning']}})
        assert resp.status_code == 200
        assert resp.get_json()['decision']['severity'] == 'info'

    def test_data_is_optional(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'ha', 'event_type': 'camera_person'})
        assert resp.status_code == 200

    @pytest.mark.parametrize('body', [
        {},
        {'source': 'ha'},
        {'event_type': 'camera_person'},
        {'source': '', 'event_type': 'camera_person'},
        {'source': 'ha', 'event_type': '   '},
        {'source': 42, 'event_type': 'camera_person'},
        {'source': 'ha', 'event_type': ['camera_person']},
    ])
    def test_invalid_bodies_return_400(self, events, body):
        c, headers = events
        assert c.post('/agents/event', headers=headers,
                      json=body).status_code == 400

    def test_overlong_source_returns_400(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'x' * 65, 'event_type': 'camera_person'})
        assert resp.status_code == 400

    def test_overlong_event_type_returns_400(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'ha', 'event_type': 'y' * 65})
        assert resp.status_code == 400

    def test_field_at_the_length_limit_is_accepted(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'z' * 64, 'event_type': 'z' * 64})
        assert resp.status_code == 200

    def test_non_object_data_returns_400(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'ha', 'event_type': 'camera_person',
                            'data': ['not', 'an', 'object']})
        assert resp.status_code == 400

    def test_oversized_data_returns_400(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'ha', 'event_type': 'camera_person',
                            'data': {'blob': 'x' * 9000}})
        assert resp.status_code == 400
        assert 'bytes' in resp.get_json()['error']

    def test_events_list_is_newest_first(self, events):
        c, headers = events
        for i in range(3):
            c.post('/agents/event', headers=headers,
                   json={'source': 'ha', 'event_type': f'probe_{i}'})
        resp = c.get('/agents/events?limit=2', headers=headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['count'] == 2
        assert [e['event_type'] for e in data['events']] == ['probe_2', 'probe_1']

    def test_events_limit_is_capped_at_200(self, events):
        c, headers = events
        resp = c.get('/agents/events?limit=99999', headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()['count'] <= 200

    def test_bad_limit_falls_back_to_the_default(self, events):
        c, headers = events
        c.post('/agents/event', headers=headers,
               json={'source': 'ha', 'event_type': 'probe'})
        resp = c.get('/agents/events?limit=abc', headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()['count'] == 1

    # -- non-object bodies: 400, never a 500 from .get() on a list ----------

    @pytest.mark.parametrize('raw', ['[1,2,3]', '"str"', '123', 'true',
                                     'false', 'null'])
    def test_non_object_json_body_returns_400(self, events, raw):
        c, headers = events
        resp = c.post('/agents/event', headers=headers, data=raw,
                      content_type='application/json')
        assert resp.status_code == 400
        assert 'error' in resp.get_json()

    def test_unparseable_body_returns_400(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers, data='{not json',
                      content_type='application/json')
        assert resp.status_code == 400

    # -- entity_id is an HA path segment downstream: allow-list it ----------

    @pytest.mark.parametrize('entity_id', [
        '../../auth/providers',
        'sensor.a/../../error_log',
        'x?filter=1',
        'sensor.a?filter=1',
        'SENSOR.Foo',
        'sensor.Foo',
        'sensor',
        '',
        'sensor.foo\n',
        42,
        None,
        ['sensor.a'],
        {'id': 'sensor.a'},
    ])
    def test_malformed_entity_id_returns_400(self, events, entity_id):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'ha', 'event_type': 'camera_person',
                            'data': {'entity_id': entity_id}})
        assert resp.status_code == 400
        assert 'entity_id' in resp.get_json()['error']

    def test_valid_entity_id_is_accepted(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'ha', 'event_type': 'camera_person',
                            'data': {
                                'entity_id':
                                    'binary_sensor.driveway_person'}})
        assert resp.status_code == 200

    def test_data_without_an_entity_id_is_still_accepted(self, events):
        c, headers = events
        resp = c.post('/agents/event', headers=headers,
                      json={'source': 'nodered', 'event_type': 'weather_alert',
                            'data': {'count': 1}})
        assert resp.status_code == 200


# ===========================================================================
# Non-object JSON bodies
# ===========================================================================

class TestNonObjectJsonBodies:
    """`get_json(silent=True) or {}` + `.get()`/`.setdefault()` raises
    AttributeError -- an HTTP 500 -- for a JSON body that parses to a list,
    string, number or boolean. Every route that used that pattern must answer
    400 instead, in its own error shape."""

    # (path, method) for every route swept. Each is reached with auth; none of
    # them should get far enough to touch a bridge, agent or webhook.
    SWEPT_ROUTES = [
        ('/dashboard/cameras', 'post'),
        ('/dashboard/widgets', 'post'),
        ('/dashboard/widgets/w-does-not-exist', 'patch'),
        ('/sentinel/consent/requests/sensor.nope', 'post'),
        ('/agents/task', 'post'),
        ('/automation/meal-plan', 'post'),
        ('/automation/grocery-list', 'post'),
        ('/api/media/play', 'post'),
        ('/api/media/control', 'post'),
    ]

    @pytest.mark.parametrize('path,method', SWEPT_ROUTES)
    @pytest.mark.parametrize('raw', ['[1,2,3]', '"str"', '123', 'true',
                                     'false'])
    def test_non_object_body_returns_400_not_500(self, client, path, method,
                                                 raw):
        c, api_key, _ = client
        resp = getattr(c, method)(path, headers={'X-API-Key': api_key},
                                  data=raw,
                                  content_type='application/json')
        assert resp.status_code == 400, (
            f'{method.upper()} {path} with body {raw} -> {resp.status_code}')
        body = resp.get_json()
        # One of the three error envelopes this file uses.
        assert 'error' in body or 'message' in body

    # Subset of SWEPT_ROUTES that resolves a `null` body without any outbound
    # request. The automation and media routes would try to reach Node-RED/Jellyfin
    # once past the guard, so they are exercised only with the non-object
    # bodies above (which never get that far).
    OFFLINE_ROUTES = [
        ('/dashboard/cameras', 'post'),
        ('/dashboard/widgets', 'post'),
        ('/dashboard/widgets/w-does-not-exist', 'patch'),
        ('/sentinel/consent/requests/sensor.nope', 'post'),
        ('/agents/task', 'post'),
    ]

    @pytest.mark.parametrize('path,method', OFFLINE_ROUTES)
    def test_null_body_is_treated_as_no_body(self, client, path, method):
        # `null` parses to None, which stays the historical "no body" case;
        # the route then fails on its own required-field validation (or
        # succeeds), but must never 500.
        c, api_key, _ = client
        resp = getattr(c, method)(path, headers={'X-API-Key': api_key},
                                  data='null',
                                  content_type='application/json')
        assert resp.status_code < 500


# ===========================================================================
# Media endpoints — Jellyfin ids become authenticated URL path segments
# ===========================================================================

class TestMediaIdValidation:
    """POST /api/media/play and /api/media/control must reject a malformed
    session_id / item_id at the edge, before the bridge builds a Jellyfin
    URL out of it."""

    BAD_IDS = [
        '../../System/Configuration',
        '../Users',
        'abc/../../x',
        'x?api_key=1',
        'abc def',
        'abc\n',
        42,
        None,
        ['abc'],
        {'id': 'abc'},
    ]

    @pytest.mark.parametrize('bad', BAD_IDS)
    def test_play_rejects_malformed_item_id(self, client, bad):
        c, api_key, _ = client
        resp = c.post('/api/media/play', headers={'X-API-Key': api_key},
                      json={'item_id': bad})
        assert resp.status_code == 400
        assert 'item_id' in resp.get_json()['error']

    @pytest.mark.parametrize('bad', [b for b in BAD_IDS if b])
    def test_play_rejects_malformed_session_id(self, client, bad):
        c, api_key, _ = client
        resp = c.post('/api/media/play', headers={'X-API-Key': api_key},
                      json={'item_id': '0123456789abcdef0123456789abcdef',
                            'session_id': bad})
        assert resp.status_code == 400
        assert 'session_id' in resp.get_json()['error']

    @pytest.mark.parametrize('bad', [b for b in BAD_IDS if b])
    def test_control_rejects_malformed_session_id(self, client, bad):
        c, api_key, _ = client
        resp = c.post('/api/media/control', headers={'X-API-Key': api_key},
                      json={'session_id': bad, 'action': 'pause'})
        assert resp.status_code == 400
        assert 'session_id' in resp.get_json()['error']

    def test_control_rejects_a_traversing_volume_request(self, client):
        c, api_key, _ = client
        resp = c.post('/api/media/control', headers={'X-API-Key': api_key},
                      json={'session_id': '../../System/Configuration',
                            'action': 'volume', 'volume': 50})
        assert resp.status_code == 400

    def test_error_does_not_echo_the_whole_payload(self, client):
        c, api_key, _ = client
        resp = c.post('/api/media/play', headers={'X-API-Key': api_key},
                      json={'item_id': '../' * 40 + 'System/Configuration'})
        assert resp.status_code == 400
        assert 'System/Configuration' not in resp.get_json()['error']


# ===========================================================================
# Scheduled tasks — POST/GET /agents/tasks, DELETE /agents/tasks/<id>
# (orchestration Phase 2: the Majordomo queue the dispatcher consumes)
# ===========================================================================

class TestScheduledTaskEndpoints:
    """Auth, the closed field set, and the validation matrix."""

    @pytest.fixture
    def tasks(self, client, tmp_path, monkeypatch):
        """Point the routes at a Majordomo with a temp queue and no bus."""
        c, api_key, mod = client
        from puffpai.agents.majordomo import MajordomoAgent

        agent = MajordomoAgent(queue_path=str(tmp_path / 'majordomo_tasks.jsonl'))

        class StubBus:
            def get(self, name):
                return agent if name == 'majordomo' else None

        monkeypatch.setattr(mod, '_get_agent_team', lambda: StubBus())
        return c, {'X-API-Key': api_key}, agent

    # -- auth ---------------------------------------------------------------

    def test_create_requires_auth(self, client):
        c, _, _ = client
        resp = c.post('/agents/tasks',
                      json={'intent': 'daily_briefing', 'daily_at': '07:00'})
        assert resp.status_code == 401

    def test_list_requires_auth(self, client):
        c, _, _ = client
        assert c.get('/agents/tasks').status_code == 401

    def test_delete_requires_auth(self, client):
        c, _, _ = client
        assert c.delete('/agents/tasks/abcdef012345').status_code == 401

    # -- happy path ---------------------------------------------------------

    def test_create_returns_201_with_the_record(self, tasks):
        c, headers, _agent = tasks
        resp = c.post('/agents/tasks', headers=headers,
                      json={'intent': 'daily_briefing', 'daily_at': '07:00'})
        assert resp.status_code == 201
        task = resp.get_json()['task']
        assert task['intent'] == 'daily_briefing'
        assert task['status'] == 'scheduled'
        assert task['schedule'] == {'daily_at': '07:00'}
        assert task['next_due']

    def test_create_accepts_a_one_shot_run_at(self, tasks):
        c, headers, _agent = tasks
        resp = c.post('/agents/tasks', headers=headers,
                      json={'intent': 'run_workflow',
                            'run_at': '2030-01-01T07:00:00Z',
                            'payload': {'webhook_name': 'daily_briefing'},
                            'budget': {'max_actions': 4, 'max_seconds': 120,
                                       'allow_egress': True}})
        assert resp.status_code == 201
        task = resp.get_json()['task']
        assert task['budget'] == {'max_actions': 4, 'max_seconds': 120.0,
                                  'allow_egress': True}
        assert task['payload'] == {'webhook_name': 'daily_briefing'}

    def test_list_shows_status_and_next_due(self, tasks):
        c, headers, _agent = tasks
        c.post('/agents/tasks', headers=headers,
               json={'intent': 'a', 'daily_at': '07:00'})
        c.post('/agents/tasks', headers=headers,
               json={'intent': 'b', 'daily_at': '08:00'})
        data = c.get('/agents/tasks', headers=headers).get_json()
        assert data['count'] == 2
        assert all(t['next_due'] and t['status'] == 'scheduled'
                   for t in data['tasks'])

    def test_delete_removes_the_task(self, tasks):
        c, headers, _agent = tasks
        created = c.post('/agents/tasks', headers=headers,
                         json={'intent': 'a', 'daily_at': '07:00'}).get_json()
        task_id = created['task']['id']
        assert c.delete(f'/agents/tasks/{task_id}',
                        headers=headers).status_code == 200
        assert c.get('/agents/tasks', headers=headers).get_json()['count'] == 0

    def test_delete_of_an_unknown_id_is_404(self, tasks):
        c, headers, _agent = tasks
        assert c.delete('/agents/tasks/abcdef012345',
                        headers=headers).status_code == 404

    @pytest.mark.parametrize('task_id', [
        'not-hex', 'ABCDEF012345', 'abc', 'abcdef0123456', '%2e%2e'])
    def test_delete_rejects_a_malformed_id(self, tasks, task_id):
        c, headers, _agent = tasks
        assert c.delete(f'/agents/tasks/{task_id}',
                        headers=headers).status_code == 400

    # -- validation matrix --------------------------------------------------

    @pytest.mark.parametrize('body,fragment', [
        ({}, 'intent'),
        ({'daily_at': '07:00'}, 'intent'),
        ({'intent': '', 'daily_at': '07:00'}, 'intent'),
        ({'intent': 42, 'daily_at': '07:00'}, 'intent'),
        ({'intent': 'x' * 65, 'daily_at': '07:00'}, 'characters'),
        ({'intent': 'x'}, 'exactly one'),
        ({'intent': 'x', 'run_at': '2030-01-01T07:00:00Z',
          'daily_at': '07:00'}, 'exactly one'),
        ({'intent': 'x', 'daily_at': '7:00'}, 'HH:MM'),
        ({'intent': 'x', 'daily_at': '24:00'}, 'HH:MM'),
        ({'intent': 'x', 'run_at': 'tomorrow'}, 'run_at'),
        ({'intent': 'x', 'daily_at': 700}, 'string'),
        ({'intent': 'x', 'daily_at': '07:00', 'payload': ['a']}, 'payload'),
        ({'intent': 'x', 'daily_at': '07:00', 'payload': {'b': 'z' * 9000}},
         'bytes'),
        ({'intent': 'x', 'daily_at': '07:00', 'target': 'steward'}, 'unknown'),
        ({'intent': 'x', 'daily_at': '07:00', 'state_changing': False},
         'unknown'),
        ({'intent': 'x', 'daily_at': '07:00', 'budget': 4}, 'budget'),
        ({'intent': 'x', 'daily_at': '07:00', 'budget': {'nope': 1}}, 'unknown'),
        ({'intent': 'x', 'daily_at': '07:00', 'budget': {'max_actions': 0}},
         'max_actions'),
        ({'intent': 'x', 'daily_at': '07:00', 'budget': {'max_actions': 21}},
         'max_actions'),
        ({'intent': 'x', 'daily_at': '07:00', 'budget': {'max_actions': True}},
         'max_actions'),
        ({'intent': 'x', 'daily_at': '07:00', 'budget': {'max_seconds': 601}},
         'max_seconds'),
        ({'intent': 'x', 'daily_at': '07:00', 'budget': {'allow_egress': 'yes'}},
         'allow_egress'),
    ])
    def test_invalid_bodies_return_400(self, tasks, body, fragment):
        c, headers, _agent = tasks
        resp = c.post('/agents/tasks', headers=headers, json=body)
        assert resp.status_code == 400, resp.get_json()
        assert fragment in resp.get_json()['error']

    @pytest.mark.parametrize('raw', ['[1,2,3]', '"str"', '123', 'true', 'null'])
    def test_non_object_json_body_returns_400(self, tasks, raw):
        c, headers, _agent = tasks
        resp = c.post('/agents/tasks', headers=headers, data=raw,
                      content_type='application/json')
        assert resp.status_code == 400
        assert 'error' in resp.get_json()

    def test_nothing_is_stored_when_validation_fails(self, tasks):
        c, headers, agent = tasks
        c.post('/agents/tasks', headers=headers, json={'intent': 'x'})
        assert agent.list_tasks() == []

    # -- team unavailable ---------------------------------------------------

    def test_503_when_the_agent_team_is_down(self, client, monkeypatch):
        c, api_key, mod = client
        monkeypatch.setattr(mod, '_get_agent_team', lambda: None)
        resp = c.post('/agents/tasks', headers={'X-API-Key': api_key},
                      json={'intent': 'x', 'daily_at': '07:00'})
        assert resp.status_code == 503

    # -- stored briefing ----------------------------------------------------

    def test_briefing_requires_auth(self, client):
        c, _, _ = client
        assert c.get('/agents/briefing').status_code == 401

    def test_briefing_returns_what_was_stored(self, client, tmp_path,
                                              monkeypatch):
        c, api_key, _mod = client
        from puffpai.agents import majordomo as mj
        path = tmp_path / 'daily_briefing.json'
        monkeypatch.setattr(mj, 'BRIEFING_PATH', str(path))
        assert c.get('/agents/briefing',
                     headers={'X-API-Key': api_key}).get_json() == {}

        mj.MajordomoAgent().store_briefing('2026-08-04', 'Good morning.')
        data = c.get('/agents/briefing',
                     headers={'X-API-Key': api_key}).get_json()
        assert data['text'] == 'Good morning.'
        assert data['date'] == '2026-08-04'


# ===========================================================================
# Knowledge title search — query-param parsing
# ===========================================================================

class TestKnowledgeTitleSearchParams:
    def test_non_numeric_limit_is_not_a_500(self, client):
        """?limit=abc used to raise before the handler's try block and 500."""
        c, api_key, _ = client
        resp = c.get('/knowledge/search/titles?q=water&limit=abc&per_lib=xyz',
                     headers={'X-API-Key': api_key})
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    def test_valid_limit_survives_bad_per_lib(self, client, monkeypatch):
        """Each param parses in its own try: one bad value must not reset
        the other to its default."""
        c, api_key, mod = client
        seen = {}

        class _Fake:
            def search_titles(self, q, max_results=None, per_library=None):
                seen.update(max_results=max_results, per_library=per_library)
                return []

        monkeypatch.setattr(mod, '_fts_searcher', lambda: _Fake())
        resp = c.get('/knowledge/search/titles?q=water&limit=10&per_lib=xyz',
                     headers={'X-API-Key': api_key})
        assert resp.status_code == 200
        assert seen == {'max_results': 10, 'per_library': 5}
