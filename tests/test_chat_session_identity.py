"""What keys the vault-unlocked chat session, and who may choose it.

THE BUG THESE TESTS LOCK SHUT
-----------------------------
``_get_chat_client_id()`` used to return ``X-Client-Id`` or, failing that, the
socket address. That value keys ``_VAULT_CHAT_STATE``: a 300-second window in
which protected questions ("what are my allergies") are answered out of the
DECRYPTED vault with no further PIN. Two consequences, both bad:

  * a caller that can set a header chose the bucket, so anyone reaching the
    backend directly with the API key could name another member's session;
  * through the dashboard proxy every request arrives as 127.0.0.1, so ALL
    household devices shared ONE bucket — one member unlocked and everybody's
    Talk page answered from the vault for five minutes.

WHAT IS ASSERTED
  1. ``X-Client-Id`` is ignored entirely — it can neither select nor create a
     session, from loopback or from the LAN.
  2. ``X-Puff-Device-Id`` is honoured ONLY from a trusted proxy peer, and only
     when it is shaped like a real derived id.
  3. Two devices get two buckets; one device is stable across requests.
  4. Direct LAN callers (CLI, Home Assistant, voice) still work, keyed on the
     socket peer, which no header can change. A trusted-proxy request that
     asserts no identity gets a single-use ``anon:`` key instead of the shared
     loopback bucket — that half of the rule lives in
     ``test_chat_anonymous_session.py``.
  5. The state dict really is keyed on the result, so an ignored header cannot
     reach another member's cached vault session.
  6. CORS no longer advertises the dead header.

Flask test client / test request contexts only: nothing binds a port, no vault
is opened, no model is called.
"""

import os
import tempfile

_TMP_DATA_DIR = tempfile.mkdtemp(prefix="puffpai_chatid_test_")
os.environ.setdefault("PUFFPAI_DATA_DIR", _TMP_DATA_DIR)

import pytest  # noqa: E402

from puffpai import api_server  # noqa: E402

#: Values shaped like the dashboard's derived id (32 hex chars).
DEVICE_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
DEVICE_B = "0f9e8d7c6b5a4938271605f4e3d2c1b0"


@pytest.fixture
def app_ctx():
    api_server.app.config.update(TESTING=True)
    return api_server.app


def _client_id(app, *, headers=None, peer="127.0.0.1"):
    """_get_chat_client_id() under a synthetic request."""
    with app.test_request_context(
        "/v1/chat/completions",
        method="POST",
        headers=headers or {},
        environ_base={"REMOTE_ADDR": peer},
    ):
        return api_server._get_chat_client_id()


# ── 1. X-Client-Id is dead ──────────────────────────────────────────────────

def test_x_client_id_is_ignored_from_loopback(app_ctx):
    """From loopback with no asserted identity the key is now a single-use
    ``anon:`` value (see test_chat_anonymous_session.py); what matters here is
    that the client's value contributes NOTHING to it."""
    got = _client_id(app_ctx, headers={"X-Client-Id": "192.168.1.42"})
    assert "192.168.1.42" not in got
    assert got.startswith("anon:")


def test_x_client_id_is_ignored_from_the_lan(app_ctx):
    got = _client_id(app_ctx, headers={"X-Client-Id": "192.168.1.42"},
                     peer="192.168.1.99")
    assert got == "peer:192.168.1.99"


def test_x_client_id_cannot_select_another_members_session(app_ctx):
    """The spoof attempt lands in its OWN bucket, not the victim's."""
    api_server._VAULT_CHAT_STATE.clear()
    try:
        victim = _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A})
        api_server._VAULT_CHAT_STATE[victim] = {"vault_session": "victim-session"}

        # An attacker with the API key, talking to the backend directly, sends
        # both the old header and the victim's derived id, hoping to land on the
        # cached unlock.
        attacker = _client_id(
            app_ctx,
            headers={"X-Client-Id": victim, "X-Puff-Device-Id": DEVICE_A},
            peer="192.168.1.50",
        )
        assert attacker != victim
        assert api_server._VAULT_CHAT_STATE.get(attacker) is None
        assert api_server._VAULT_CHAT_STATE[victim]["vault_session"] == "victim-session"
    finally:
        api_server._VAULT_CHAT_STATE.clear()


# ── 2. the proxy-set header, honoured only from a trusted peer ──────────────

@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_device_header_honoured_from_loopback(app_ctx, peer):
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A},
                      peer=peer) == f"device:{DEVICE_A}"


@pytest.mark.parametrize("peer", ["192.168.1.50", "203.0.113.7", "10.0.0.9"])
def test_device_header_IGNORED_from_a_non_proxy_peer(app_ctx, peer):
    """A LAN or public client with the API key cannot borrow a device id it read
    out of /api/pair/devices, because it is not a trusted proxy peer."""
    got = _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A}, peer=peer)
    assert got == f"peer:{peer}"
    assert DEVICE_A not in got


@pytest.mark.parametrize("bad", [
    "",                       # empty
    "   ",                    # whitespace only
    "short",                  # under the length floor
    "a" * 65,                 # over the ceiling
    "has spaces in it aaaa",  # not the charset
    "path/traversal/../x",    # separators
    "semi;colon;injection",
    # A whole pairing cookie pasted in: dots are not in the charset, so it is
    # rejected rather than used as a key.
    "v2.aaaaaaaaaaaa.1758000000.1789536000." + "f" * 64,
])
def test_malformed_device_header_is_discarded(app_ctx, bad):
    """Garbage in the identity header is exactly as unidentified as no header at
    all: from a trusted proxy peer it yields a single-use anon: key, never the
    shared loopback bucket."""
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": bad}).startswith("anon:")


def test_trusted_peer_list_is_extensible_but_closed_by_default(app_ctx, monkeypatch):
    """A split-out dashboard host can be named; nothing else is trusted."""
    monkeypatch.setenv("PUFFPAI_TRUSTED_PROXY_IPS", "10.0.0.5")
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A},
                      peer="10.0.0.5") == f"device:{DEVICE_A}"
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A},
                      peer="10.0.0.6") == "peer:10.0.0.6"
    # ...and the same host's IPv4-mapped spelling still matches the config.
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A},
                      peer="::ffff:10.0.0.5") == f"device:{DEVICE_A}"
    monkeypatch.delenv("PUFFPAI_TRUSTED_PROXY_IPS")
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A},
                      peer="10.0.0.5") == "peer:10.0.0.5"


@pytest.mark.parametrize("value", ["*", "", "   ", "nonsense", "localhost"])
def test_no_configuration_value_trusts_everyone(app_ctx, monkeypatch, value):
    """There is no wildcard, and a name is not an address: a garbage value
    trusts nobody instead of trusting anybody."""
    monkeypatch.setenv("PUFFPAI_TRUSTED_PROXY_IPS", value)
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A},
                      peer="192.168.1.50") == "peer:192.168.1.50"


def test_localhost_as_a_peer_name_is_not_loopback(app_ctx):
    """Peers arrive as addresses. The word "localhost" must not assert locality."""
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A},
                      peer="localhost") == "peer:localhost"


# ── 3. separation and stability ─────────────────────────────────────────────

def test_two_devices_get_two_buckets(app_ctx):
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A}) != \
        _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_B})


def test_the_same_device_is_stable_across_requests(app_ctx):
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A}) == \
        _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A})


def test_device_and_peer_keys_are_namespaced(app_ctx):
    """A device id that looks like an address cannot land in an address bucket."""
    assert _client_id(app_ctx, headers={"X-Puff-Device-Id": DEVICE_A}).startswith("device:")
    assert _client_id(app_ctx, peer="192.168.1.20").startswith("peer:")
    assert _client_id(app_ctx).startswith("anon:")


def test_an_unidentified_proxy_request_does_not_join_a_device_bucket(app_ctx):
    """No cookie -> the proxy sends no device header (and an explicit anonymous
    marker) -> a key of its own, which is nobody's unlocked session and is not
    shared with the next unidentified caller either."""
    first = _client_id(app_ctx)
    assert first.startswith("anon:")
    assert first != f"device:{DEVICE_A}"
    assert first != "peer:127.0.0.1"
    assert first != _client_id(app_ctx)


# ── 4. direct callers keep working ──────────────────────────────────────────

def test_direct_client_with_no_headers_still_gets_a_key(app_ctx):
    """Direct callers reach the backend from a LAN address, which is not a
    trusted proxy peer, so they keep their stable per-address bucket. A caller on
    LOOPBACK that asserts no id is indistinguishable from the proxy's
    unidentified traffic and is treated as anonymous — the remedy is the one the
    voice service uses: assert your own X-Puff-Device-Id."""
    assert _client_id(app_ctx, peer="192.168.1.20") == "peer:192.168.1.20"
    assert _client_id(app_ctx, peer="10.0.0.9") == "peer:10.0.0.9"


def test_the_voice_satellite_gets_its_own_bucket(app_ctx):
    """src/puffpai/voice/service.py asserts a fixed id from loopback so the
    shared speaker does not share the loopback bucket with proxy traffic."""
    from puffpai.voice import service as voice_service
    got = _client_id(app_ctx,
                     headers={"X-Puff-Device-Id": voice_service.BRAIN_SESSION_ID})
    assert got == f"device:{voice_service.BRAIN_SESSION_ID}"
    assert got != "peer:127.0.0.1"
    assert api_server._DEVICE_SESSION_ID_RE.match(voice_service.BRAIN_SESSION_ID)


def test_missing_remote_addr_degrades_to_unknown(app_ctx):
    """No socket address at all (unix socket, odd WSGI server) -> one shared
    bucket, which grants nobody another identity."""
    with app_ctx.test_request_context("/v1/chat/completions", method="POST",
                                      environ_base={"REMOTE_ADDR": None}):
        assert api_server._get_chat_client_id() == "unknown"


def test_no_request_context_at_all():
    """Called off a request (a background task): never raises."""
    assert api_server._get_chat_client_id() == "unknown"


# ── 5. the header is not advertised to browsers ─────────────────────────────

def test_cors_no_longer_invites_x_client_id():
    api_server.app.config.update(TESTING=True)
    client = api_server.app.test_client()
    resp = client.options(
        "/v1/chat/completions",
        headers={"Origin": "http://localhost:3003",
                 "Access-Control-Request-Method": "POST"},
    )
    allowed = resp.headers.get("Access-Control-Allow-Headers", "")
    assert "X-Client" not in allowed
    assert "X-Puff-Device-Id" not in allowed
    # The client headers that legitimately pass are still advertised.
    assert "X-Vault-Token" in allowed
    assert "Authorization" in allowed


# ── 6. the rule is documented (the docstring is the contract) ───────────────

def test_the_rule_is_documented_for_the_next_reader():
    doc = api_server._get_chat_client_id.__doc__ or ""
    assert "X-Puff-Device-Id" in doc
    assert "X-Client-Id" in doc          # names what was removed and why
    assert "PUFFPAI_TRUSTED_PROXY_IPS" in doc
    for word in ("CLI", "Home Assistant", "voice"):
        assert word in doc, f"direct-caller rule must be documented: {word}"
