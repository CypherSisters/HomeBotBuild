"""The public-endpoint allowlist must not answer a bare LAN client with
household data.

Without the gate, a client on the same LAN with no API key, no cookie and no
pairing gets:

    GET /ingest/list -> 200 {"documents":[]}
    GET /ingest/stats -> 200 {"chunk_count":1,...}
    GET /ingest/search?q=test -> 200 + filename + snippet
    DELETE /ingest/document/<id> -> 200 (reached the handler)
    GET /knowledge/library/<a real.docx>/source -> 200, 12730 bytes
    GET /knowledge/library/<a real.png>/source -> 200, 482733 bytes
    POST /v1/chat/completions -> 200, drove the LLM

while /vault/contents, /system/status and /knowledge/libraries correctly 401.

These tests pin the three properties the gate must hold:
  1. every endpoint left unconditionally public is on an explicit
     allow-with-reason list, so a future exemption cannot be added silently;
  2. each gated route 401s for a LAN peer with no credential and
     200s (or at least is not 401) with the key;
  3. the same-host paths the dashboard proxy, kiosk and voice service use are
     still answered without a key.
"""

import importlib

import pytest

from puffpai.security import endpoint_gate


# The endpoints that are same-host only: never public, never keyless from LAN.
GATED_ENDPOINTS = [
    "ingest_upload", "ingest_list", "ingest_search", "ingest_delete",
    "ingest_reindex", "ingest_stats",
    "knowledge_serve_source", "knowledge_serve_asset",
    "openai_chat_completions", "ollama_chat",
    "openai_list_models", "ollama_list_models",
    "voice_capabilities",
]

LAN_PEER = "192.168.50.70"          # another machine on the same LAN
HA_PEER = "192.168.50.140"          # Home Assistant, a different LAN host


# ---------------------------------------------------------------------------
# 1. Every remaining public endpoint is justified in writing.
# ---------------------------------------------------------------------------

class TestPublicListIsJustified:

    def test_every_public_endpoint_has_a_written_reason(self):
        from puffpai import api_server as mod
        unjustified = set(mod._PUBLIC_ENDPOINTS) - set(
            endpoint_gate.PUBLIC_ENDPOINT_JUSTIFICATIONS)
        assert unjustified == set(), (
            "endpoints answered for ANY caller with no recorded reason: "
            f"{sorted(unjustified)}. Add the justification to "
            "endpoint_gate.PUBLIC_ENDPOINT_JUSTIFICATIONS or take it off the "
            "public list."
        )

    def test_no_stale_justifications(self):
        from puffpai import api_server as mod
        stale = set(endpoint_gate.PUBLIC_ENDPOINT_JUSTIFICATIONS) - set(
            mod._PUBLIC_ENDPOINTS)
        assert stale == set(), (
            f"justified but not public: {sorted(stale)}")

    @pytest.mark.parametrize("endpoint", sorted(GATED_ENDPOINTS))
    def test_gated_endpoint_is_not_unconditionally_public(
            self, endpoint):
        from puffpai import api_server as mod
        assert endpoint not in mod._PUBLIC_ENDPOINTS
        assert endpoint in endpoint_gate.LOCAL_ONLY_ENDPOINTS

    def test_reasons_are_not_placeholders(self):
        for ep, why in endpoint_gate.PUBLIC_ENDPOINT_JUSTIFICATIONS.items():
            assert len(why) > 60, f"{ep}: justification is too thin to review"


# ---------------------------------------------------------------------------
# 2. The gate function itself.
# ---------------------------------------------------------------------------

class TestLocalCallerAllowed:

    @pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
    @pytest.mark.parametrize("endpoint", sorted(GATED_ENDPOINTS))
    def test_same_host_is_allowed(self, endpoint, peer):
        assert endpoint_gate.local_caller_allowed(endpoint, peer) is True

    @pytest.mark.parametrize("endpoint", sorted(GATED_ENDPOINTS))
    def test_lan_peer_is_refused(self, endpoint):
        assert endpoint_gate.local_caller_allowed(endpoint, LAN_PEER) is False
        assert endpoint_gate.local_caller_allowed(endpoint, HA_PEER) is False

    def test_unknown_endpoint_is_never_allowed_even_from_loopback(self):
        assert endpoint_gate.local_caller_allowed(
            "vault_contents", "127.0.0.1") is False
        assert endpoint_gate.local_caller_allowed(None, "127.0.0.1") is False

    def test_missing_peer_is_refused(self):
        assert endpoint_gate.local_caller_allowed("ingest_list", None) is False
        assert endpoint_gate.local_caller_allowed("ingest_list", "") is False

    def test_near_miss_loopback_addresses_are_refused(self):
        # 127.0.0.2 and 0.0.0.0 are not the same-host peer the proxy uses.
        for peer in ("127.0.0.2", "127.1.1.1", "0.0.0.0", "10.0.0.1"):
            assert endpoint_gate.local_caller_allowed("ingest_list", peer) is False

    def test_operator_allowlist_admits_named_peer_only(self, monkeypatch):
        monkeypatch.setenv(endpoint_gate.TRUSTED_PEERS_ENV, HA_PEER)
        assert endpoint_gate.local_caller_allowed(
            "openai_chat_completions", HA_PEER) is True
        assert endpoint_gate.local_caller_allowed(
            "openai_chat_completions", LAN_PEER) is False
        # Still scoped to the local-only list, never to everything.
        assert endpoint_gate.local_caller_allowed(
            "vault_contents", HA_PEER) is False

    def test_operator_allowlist_is_empty_by_default(self, monkeypatch):
        monkeypatch.delenv(endpoint_gate.TRUSTED_PEERS_ENV, raising=False)
        assert endpoint_gate.trusted_peers() == frozenset()

    def test_allowlist_ignores_junk_entries(self, monkeypatch):
        monkeypatch.setenv(endpoint_gate.TRUSTED_PEERS_ENV,
                           " , not-an-ip, 192.168.50.140 ,")
        assert endpoint_gate.trusted_peers() == frozenset({HA_PEER})

    def test_ipv4_mapped_form_matches_the_plain_entry(self, monkeypatch):
        monkeypatch.setenv(endpoint_gate.TRUSTED_PEERS_ENV, HA_PEER)
        assert endpoint_gate.local_caller_allowed(
            "ollama_chat", f"::ffff:{HA_PEER}") is True

    def test_peer_arm_is_refused_entirely_when_a_proxy_is_trusted(self):
        # With ProxyFix active remote_addr comes from X-Forwarded-For, which a
        # LAN client controls; fail closed rather than honour a forged 127.0.0.1.
        assert endpoint_gate.local_caller_allowed(
            "ingest_list", "127.0.0.1", proxy_fix_active=True) is False


# ---------------------------------------------------------------------------
# 3. End to end through the Flask app.
# ---------------------------------------------------------------------------

@pytest.fixture
def gated_client(monkeypatch):
    """A test client plus the API key, with the module imported fresh."""
    monkeypatch.setenv("PUFFPAI_API_KEY", "0" * 64)
    monkeypatch.delenv(endpoint_gate.TRUSTED_PEERS_ENV, raising=False)
    from puffpai import api_server as mod
    importlib.reload(mod)
    mod.app.config["TESTING"] = True
    with mod.app.test_client() as c:
        yield c, mod._API_KEY, mod


# (method, path) pairs that map to the gated endpoints. Kept to
# read-only / harmless shapes so the suite never mutates the corpus.
EXPOSED_ROUTES = [
    ("GET", "/ingest/list"),
    ("GET", "/ingest/stats"),
    ("GET", "/ingest/search?q=test"),
    ("DELETE", "/ingest/document/test-nonexistent-document"),
    ("GET", "/knowledge/library/Master%20Training%20Sheet.docx/source"),
    ("GET", "/v1/models"),
    ("GET", "/api/tags"),
    ("GET", "/voice/capabilities"),
]


class TestRoutesEndToEnd:

    @pytest.mark.parametrize("method,path", EXPOSED_ROUTES)
    def test_lan_peer_with_no_credential_gets_401(self, gated_client,
                                                  method, path):
        c, _key, _mod = gated_client
        resp = c.open(path, method=method,
                      environ_base={"REMOTE_ADDR": LAN_PEER})
        assert resp.status_code == 401, (
            f"{method} {path} answered a bare LAN client with "
            f"{resp.status_code}: household data exposed to the LAN")

    @pytest.mark.parametrize("method,path", EXPOSED_ROUTES)
    def test_lan_peer_with_the_key_is_not_401(self, gated_client,
                                              method, path):
        c, key, _mod = gated_client
        resp = c.open(path, method=method,
                      headers={"Authorization": f"Bearer {key}"},
                      environ_base={"REMOTE_ADDR": LAN_PEER})
        assert resp.status_code != 401

    @pytest.mark.parametrize("method,path", EXPOSED_ROUTES)
    def test_loopback_peer_with_no_credential_is_not_401(self, gated_client,
                                                         method, path):
        # This is the dashboard proxy / kiosk / voice-service path.
        c, _key, _mod = gated_client
        resp = c.open(path, method=method,
                      environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert resp.status_code != 401

    def test_chat_completions_refuses_an_unauthenticated_lan_client(
            self, gated_client):
        c, _key, _mod = gated_client
        resp = c.post("/v1/chat/completions",
                      json={"messages": [{"role": "user",
                                          "content": "Reply with: PROBE"}]},
                      environ_base={"REMOTE_ADDR": LAN_PEER})
        assert resp.status_code == 401

    def test_ingest_upload_refuses_an_unauthenticated_lan_client(
            self, gated_client):
        # The sharp end: an unauthenticated prompt-injection channel into the
        # household RAG corpus.
        c, _key, _mod = gated_client
        resp = c.post("/ingest/upload",
                      data={}, content_type="multipart/form-data",
                      environ_base={"REMOTE_ADDR": LAN_PEER})
        assert resp.status_code == 401

    def test_wrong_key_from_lan_is_still_401(self, gated_client):
        c, _key, _mod = gated_client
        resp = c.get("/ingest/list",
                     headers={"Authorization": "Bearer " + "f" * 64},
                     environ_base={"REMOTE_ADDR": LAN_PEER})
        assert resp.status_code == 401

    def test_x_api_key_header_is_accepted_too(self, gated_client):
        c, key, _mod = gated_client
        resp = c.get("/ingest/list", headers={"X-API-Key": key},
                     environ_base={"REMOTE_ADDR": LAN_PEER})
        assert resp.status_code != 401

    def test_forwarded_for_cannot_forge_loopback(self, gated_client):
        # trusted_proxy_hops defaults to 0, so the header is never parsed.
        c, _key, _mod = gated_client
        resp = c.get("/ingest/list",
                     headers={"X-Forwarded-For": "127.0.0.1"},
                     environ_base={"REMOTE_ADDR": LAN_PEER})
        assert resp.status_code == 401

    def test_health_stays_reachable_for_node_red(self, gated_client):
        c, _key, _mod = gated_client
        resp = c.get("/health", environ_base={"REMOTE_ADDR": "172.17.0.3"})
        assert resp.status_code == 200

    def test_still_gated_controls_remain_401(self, gated_client):
        # Control cases that stay gated with or without the allowlist.
        c, _key, _mod = gated_client
        for path in ("/vault/contents", "/system/status",
                     "/knowledge/libraries"):
            resp = c.get(path, environ_base={"REMOTE_ADDR": LAN_PEER})
            assert resp.status_code == 401, path
