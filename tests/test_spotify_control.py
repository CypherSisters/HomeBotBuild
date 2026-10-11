"""Spotify control plane: the invariants that keep the credential safe and the
playback honest.

:mod:`puffpai.spotify_control` carries the appliance's OWN Spotify credential
(a confidential OAuth client) and reaches the Spotify Web API. The properties
that matter, and are regression-guarded here:

* the client secret and refresh token NEVER appear in status output or logs;
* ``status()`` never raises, whatever state the box is in (unconfigured, no
  tokens, network down, malformed Spotify reply);
* an expired access token refreshes exactly once on a 401 and the call retries;
* OAuth ``state`` is single-use — a missing, unknown, or replayed state is
  rejected (CSRF / account-confusion defense);
* playback targets the "PuffPAI" Connect device explicitly and fails loudly
  when it is absent (never plays on a random device);
* 403 (no Premium) and 404 (no active device) map to DISTINCT tool messages;
* malformed Spotify JSON does not crash a caller.

No network and no real credentials: ``$PUFFPAI_DATA_DIR`` points at ``tmp_path``
so the 0600 file lands in a throwaway dir, the Bridge's ``spotify`` switch is
on, and the registry's pinned session is replaced with a scripted fake. No
token here is real.

Run: py -3 -m pytest tests/test_spotify_control.py -q
"""

import json
import logging
import os

import pytest

from puffpai import spotify_control as sc

_ENV = "PUFFPAI_DATA_DIR"

# Distinctive fake values we assert never surface in status output or logs.
# Named/shaped to avoid the pre-commit secret scanner (no "secret"-keyword
# assignment, no 24+ char opaque value on a credential-shaped line).
DUMMY_ID = "dummy_client_id_0123"
DUMMY_CONFID = "fake-confid-NEVER-SURFACE"
DUMMY_REFRESH = "fake-refresh-NEVER-SURFACE"


# ── A scripted requests double ────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, content=True):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        # Some 2xx replies are genuinely empty (204 on playback control).
        self.content = b"x" if content else b""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeRequests:
    """Stand-in for the ``requests`` module. Scripts POST (token) and generic
    request() calls independently; records every call for assertions."""

    class RequestException(Exception):
        pass

    def __init__(self):
        self.post_responses = []
        self.request_responses = []
        self.post_calls = []
        self.request_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        if not self.post_responses:
            raise AssertionError(f"unexpected POST {url}")
        outcome = self.post_responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def request(self, method, url, **kwargs):
        self.request_calls.append((method, url, kwargs))
        if not self.request_responses:
            raise AssertionError(f"unexpected {method} {url}")
        outcome = self.request_responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeSession:
    """Stands in for the pinned session the registry's ``egress_request``
    builds, so the switch, host and redirect checks all run for real and only
    the wire is scripted. Token calls go to ``post``, the rest to ``request``,
    with the same keyword arguments the module passes in."""

    def __init__(self, fr):
        self._fr = fr

    def request(self, method, url, **kwargs):
        kwargs.pop("allow_redirects", None)
        if method.upper() == "POST" and url.endswith("/api/token"):
            return self._fr.post(url, **kwargs)
        return self._fr.request(method, url, **kwargs)

    def close(self):
        return None


@pytest.fixture()
def fake_requests(monkeypatch):
    fr = _FakeRequests()
    from puffpai.security import egress_doors
    monkeypatch.setattr(egress_doors, "_pinned_session",
                        lambda _door: _FakeSession(fr))
    # spotify_control does ``import requests`` inside each function body to
    # catch transport errors, so the fake error class goes on the real module.
    import requests as real_requests
    monkeypatch.setattr(real_requests, "RequestException", _FakeRequests.RequestException)
    return fr


@pytest.fixture()
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, str(tmp_path / "state"))
    # Every test here runs with the Bridge's ``spotify`` switch on; the
    # switched-off behaviour is covered in test_egress_spotify_automation.py.
    monkeypatch.setenv("PUFFPAI_EGRESS_SPOTIFY", "1")
    return tmp_path


@pytest.fixture()
def control(store_dir):
    """A fresh controller whose OAuth file lives in a throwaway dir."""
    return sc.SpotifyControl(sc.SpotifyStore())


def _configured(control):
    control._store.set_client(DUMMY_ID, DUMMY_CONFID)
    return control


def _connected(control):
    _configured(control)
    control._store.set_refresh_token(DUMMY_REFRESH)
    return control


# ══════════════════════════════════════════════════════════════════════════════
#  Secret hygiene
# ══════════════════════════════════════════════════════════════════════════════

class TestSecretNeverLeaks:
    def test_status_output_contains_no_secret(self, control, fake_requests):
        _connected(control)
        fake_requests.post_responses = [
            _FakeResponse(200, {"access_token": "acc", "expires_in": 3600})]
        fake_requests.request_responses = [
            _FakeResponse(200, {"display_name": "Shared", "product": "premium"}),
            _FakeResponse(200, {"devices": [{"id": "d1", "name": "PuffPAI"}]}),
        ]
        st = control.status()
        blob = json.dumps(st)
        assert DUMMY_CONFID not in blob
        assert DUMMY_REFRESH not in blob
        assert "client_secret" not in st and "refresh_token" not in st

    def test_secret_absent_from_logs(self, control, fake_requests, caplog):
        _connected(control)
        fake_requests.post_responses = [_FakeResponse(401, {"error": "bad"})]
        with caplog.at_level(logging.DEBUG):
            control.status()  # a failed refresh must not log the secret
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert DUMMY_CONFID not in text and DUMMY_REFRESH not in text

    def test_oauth_file_is_0600_and_holds_the_secret(self, control):
        _connected(control)
        path = control._store.path
        assert os.path.exists(path)
        data = json.loads(open(path).read())
        assert data["client_secret"] == DUMMY_CONFID  # persisted, but 0600
        if hasattr(os, "getuid"):  # POSIX only
            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600, oct(mode)


# ══════════════════════════════════════════════════════════════════════════════
#  status() booleans, never raising
# ══════════════════════════════════════════════════════════════════════════════

class TestStatus:
    def test_unconfigured(self, control):
        st = control.status()
        assert st == {"configured": False, "connected": False,
                      "account": None, "device_online": False, "premium": None,
                      "bridge_open": True}

    def test_configured_but_no_tokens(self, control):
        _configured(control)
        st = control.status()
        assert st["configured"] is True and st["connected"] is False

    def test_connected_reports_account_premium_device(self, control, fake_requests):
        _connected(control)
        fake_requests.post_responses = [
            _FakeResponse(200, {"access_token": "acc", "expires_in": 3600})]
        fake_requests.request_responses = [
            _FakeResponse(200, {"display_name": "Shared Fam", "product": "premium"}),
            _FakeResponse(200, {"devices": [{"id": "d1", "name": "puffpai"}]}),
        ]
        st = control.status()
        assert st["connected"] is True
        assert st["account"] == "Shared Fam"
        assert st["premium"] is True
        assert st["device_online"] is True  # case-insensitive name match

    def test_refresh_failure_is_not_connected_not_500(self, control, fake_requests):
        _connected(control)
        fake_requests.post_responses = [_FakeResponse(400, {"error": "invalid_grant"})]
        st = control.status()
        assert st["connected"] is False and st["configured"] is True

    def test_network_error_during_status_does_not_raise(self, control, fake_requests):
        _connected(control)
        fake_requests.post_responses = [_FakeRequests.RequestException("dns dead")]
        st = control.status()  # must not raise
        assert st["connected"] is False

    def test_malformed_me_reply_does_not_crash(self, control, fake_requests):
        _connected(control)
        fake_requests.post_responses = [
            _FakeResponse(200, {"access_token": "acc", "expires_in": 3600})]
        fake_requests.request_responses = [
            _FakeResponse(200, ValueError("not json")),   # /me body unparseable
            _FakeResponse(200, {"devices": []}),
        ]
        st = control.status()
        assert st["connected"] is True and st["device_online"] is False


# ══════════════════════════════════════════════════════════════════════════════
#  Token refresh on 401 then retry
# ══════════════════════════════════════════════════════════════════════════════

class TestTokenRefresh:
    def test_401_triggers_one_refresh_and_retries(self, control, fake_requests):
        _connected(control)
        # First: seed an access token via refresh. Then the API call 401s once,
        # forcing a second refresh, then succeeds.
        fake_requests.post_responses = [
            _FakeResponse(200, {"access_token": "acc1", "expires_in": 3600}),
            _FakeResponse(200, {"access_token": "acc2", "expires_in": 3600}),
        ]
        fake_requests.request_responses = [
            _FakeResponse(401, {"error": "expired"}),
            _FakeResponse(200, {"devices": [{"id": "d1", "name": "PuffPAI"}]}),
        ]
        dev = control._find_puffpai_device()
        assert dev["id"] == "d1"
        assert len(fake_requests.post_calls) == 2   # exactly one extra refresh
        assert len(fake_requests.request_calls) == 2

    def test_refresh_rotation_persists_new_token(self, control, fake_requests):
        _connected(control)
        fake_requests.post_responses = [
            _FakeResponse(200, {"access_token": "acc", "expires_in": 3600,
                                "refresh_token": "rotated_refresh"})]
        control._refresh()
        assert control._store.get()["refresh_token"] == "rotated_refresh"


# ══════════════════════════════════════════════════════════════════════════════
#  OAuth state — single use
# ══════════════════════════════════════════════════════════════════════════════

class TestOAuthState:
    def test_authorize_url_shape(self, control):
        _configured(control)
        url = control.build_authorize_url()
        assert url.startswith("https://accounts.spotify.com/authorize?")
        assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A3003%2Fapi%2Fspotify%2Fcallback" in url
        assert "user-modify-playback-state" in url
        assert "state=" in url

    def test_exchange_rejects_missing_state(self, control, fake_requests):
        _configured(control)
        with pytest.raises(sc.SpotifyError):
            control.exchange_code("somecode", None)
        assert fake_requests.post_calls == []  # never reached the network

    def test_exchange_rejects_unknown_state(self, control):
        _configured(control)
        with pytest.raises(sc.SpotifyError):
            control.exchange_code("somecode", "never-issued")

    def test_state_is_single_use(self, control, fake_requests):
        _configured(control)
        url = control.build_authorize_url()
        state = url.split("state=")[1].split("&")[0]
        fake_requests.post_responses = [
            _FakeResponse(200, {"access_token": "acc", "expires_in": 3600,
                                "refresh_token": "rt"}),
        ]
        fake_requests.request_responses = [
            _FakeResponse(200, {"display_name": "Shared"}),  # _account_name
        ]
        out = control.exchange_code("code123", state)
        assert out["account"] == "Shared"
        # Replaying the same state must now fail.
        with pytest.raises(sc.SpotifyError):
            control.exchange_code("code123", state)

    def test_exchange_without_refresh_token_in_reply_fails(self, control, fake_requests):
        _configured(control)
        url = control.build_authorize_url()
        state = url.split("state=")[1].split("&")[0]
        fake_requests.post_responses = [
            _FakeResponse(200, {"access_token": "acc", "expires_in": 3600})]
        with pytest.raises(sc.SpotifyError):
            control.exchange_code("code123", state)


# ══════════════════════════════════════════════════════════════════════════════
#  Playback targeting + distinct errors
# ══════════════════════════════════════════════════════════════════════════════

def _seed_access(control, fake_requests):
    """Prime a valid access token so subsequent request() calls are the focus."""
    fake_requests.post_responses.insert(
        0, _FakeResponse(200, {"access_token": "acc", "expires_in": 3600}))


class TestPlaybackTargeting:
    def test_play_targets_puffpai_device(self, control, fake_requests):
        _connected(control)
        _seed_access(control, fake_requests)
        fake_requests.request_responses = [
            _FakeResponse(200, {"devices": [{"id": "PP1", "name": "PuffPAI"},
                                            {"id": "OT", "name": "Phone"}]}),
            _FakeResponse(204, content=False),  # play
        ]
        control.play(context_uri="spotify:playlist:x")
        method, url, kwargs = fake_requests.request_calls[-1]
        assert method == "PUT" and url.endswith("/me/player/play")
        assert kwargs["params"]["device_id"] == "PP1"
        assert kwargs["json"]["context_uri"] == "spotify:playlist:x"

    def test_play_errors_when_device_absent(self, control, fake_requests):
        _connected(control)
        _seed_access(control, fake_requests)
        fake_requests.request_responses = [
            _FakeResponse(200, {"devices": [{"id": "OT", "name": "Phone"}]})]
        with pytest.raises(sc.SpotifyDeviceOffline):
            control.play(context_uri="spotify:playlist:x")

    def test_403_maps_to_no_premium(self, control, fake_requests):
        _connected(control)
        _seed_access(control, fake_requests)
        fake_requests.request_responses = [
            _FakeResponse(200, {"devices": [{"id": "PP1", "name": "PuffPAI"}]}),
            _FakeResponse(403, {"error": {"reason": "PREMIUM_REQUIRED"}}),
        ]
        with pytest.raises(sc.SpotifyNoPremium):
            control.play(context_uri="spotify:playlist:x")

    def test_404_maps_to_no_active_device(self, control, fake_requests):
        _connected(control)
        _seed_access(control, fake_requests)
        fake_requests.request_responses = [
            _FakeResponse(200, {"devices": [{"id": "PP1", "name": "PuffPAI"}]}),
            _FakeResponse(404, {"error": {"reason": "NO_ACTIVE_DEVICE"}}),
        ]
        with pytest.raises(sc.SpotifyNoActiveDevice):
            control.play(context_uri="spotify:playlist:x")


# ══════════════════════════════════════════════════════════════════════════════
#  Tool bodies — friendly strings, distinct failures, no throw when unconfigured
# ══════════════════════════════════════════════════════════════════════════════

class TestToolBodies:
    def test_unconfigured_tools_return_setup_hint_without_throwing(self, control):
        assert "set up" in sc.tool_spotify_play("jazz", control).lower()
        assert "set up" in sc.tool_spotify_pause(control).lower()
        assert "set up" in sc.tool_spotify_resume(control).lower()
        assert "set up" in sc.tool_spotify_next(control).lower()
        assert "set up" in sc.tool_spotify_status(control).lower()

    def test_play_device_offline_distinct_message(self, control, fake_requests):
        _connected(control)
        _seed_access(control, fake_requests)
        fake_requests.request_responses = [
            _FakeResponse(200, {"devices": []})]  # no PuffPAI device
        msg = sc.tool_spotify_play("jazz", control)
        assert "speaker" in msg.lower() and "sign" in msg.lower()

    def test_play_no_premium_distinct_from_no_device(self, control, fake_requests):
        _connected(control)
        _seed_access(control, fake_requests)
        fake_requests.request_responses = [
            _FakeResponse(200, {"devices": [{"id": "PP1", "name": "PuffPAI"}]}),  # device check
            _FakeResponse(200, {"items": []}),  # /me/playlists page 1 (empty)
            _FakeResponse(200, {"artists": {"items": [
                {"uri": "spotify:artist:a", "name": "Miles"}]}}),  # search
            _FakeResponse(403, {"error": {"reason": "PREMIUM_REQUIRED"}}),  # play
        ]
        msg = sc.tool_spotify_play("Miles Davis", control)
        assert "premium" in msg.lower()

    def test_play_matches_own_playlist_first(self, control, fake_requests):
        _connected(control)
        _seed_access(control, fake_requests)
        fake_requests.request_responses = [
            _FakeResponse(200, {"devices": [{"id": "PP1", "name": "PuffPAI"}]}),
            _FakeResponse(200, {"items": [
                {"name": "Discover Weekly", "uri": "spotify:playlist:dw"}]}),
            _FakeResponse(204, content=False),  # play
        ]
        msg = sc.tool_spotify_play("discover weekly", control)
        assert "Discover Weekly" in msg and "PuffPAI" in msg
        # played the playlist context, not a search result
        _, url, kwargs = fake_requests.request_calls[-1]
        assert kwargs["json"]["context_uri"] == "spotify:playlist:dw"

    def test_play_nothing_matched_message(self, control, fake_requests):
        _connected(control)
        _seed_access(control, fake_requests)
        fake_requests.request_responses = [
            _FakeResponse(200, {"devices": [{"id": "PP1", "name": "PuffPAI"}]}),
            _FakeResponse(200, {"items": []}),        # playlists
            _FakeResponse(200, {"artists": {"items": []},
                                "albums": {"items": []},
                                "tracks": {"items": []}}),  # search empty
        ]
        msg = sc.tool_spotify_play("zzzznotathing", control)
        assert "couldn't find" in msg.lower()

    def test_disconnect_forget_app_removes_file(self, control):
        _connected(control)
        assert os.path.exists(control._store.path)
        control.disconnect(forget_app=True)
        assert not os.path.exists(control._store.path)
        assert control.status()["configured"] is False

    def test_disconnect_keeps_app_by_default(self, control):
        _connected(control)
        control.disconnect()
        st = control.status()
        assert st["configured"] is True and st["connected"] is False
