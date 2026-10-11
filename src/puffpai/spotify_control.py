"""PuffPAI Spotify Web API control plane.

Voice-driven Spotify: "hey PuffPAI, play my Discover Weekly" -> the LLM calls a
tool here -> the Spotify Web API starts that playlist on the appliance's own
signed-in Spotify device.

Two Spotify identities live on this appliance and MUST NOT be conflated:

    puffpai-spotify container    zeroconf casting target for personal phones.
                                 Owned elsewhere; this module never touches it.
    voice librespot ("PuffPAI")  a single shared account signed in as a Connect
                                 speaker named "PuffPAI". A teammate builds that
                                 container. Every Web API call here targets THAT
                                 device by name, never "whatever is active".

This module is the *control plane*: a Spotify DEVELOPER APP (confidential
client, client_id + client_secret) authorized against the SAME shared account
via the Authorization Code flow. It holds the long-lived refresh token and
mints short-lived access tokens on demand.

SECRET POSTURE (deliberate — do not "improve" it into the vault):
    The PIN-gated vault is LOCKED at every headless boot. Putting the refresh
    token there would break unattended voice until a human types a PIN. The
    appliance's other SERVICE credentials are plaintext, file-permission
    protected, read at boot (see /etc/puffpai/api.env). We follow that posture:
    client_id / client_secret / refresh_token live in a 0600 JSON file owned by
    puffpai, alongside the app_data JSONs. The secret is WRITE-ONLY across the
    API: it is never logged and never returned in any response.

puff puff pass
"""

from __future__ import annotations

import base64
import logging
import os
import secrets as _secrets
import tempfile
import threading
import time
import urllib.parse
from typing import Any

logger = logging.getLogger(__name__)

# ── Fixed protocol constants ─────────────────────────────────────────────────

#: The user registers EXACTLY this redirect URI in the Spotify dashboard. 2026
#: rules: a loopback IPv4 literal over http is allowed; ``localhost`` is NOT.
#: The Next dashboard (:3003) owns this route and forwards the code to us.
REDIRECT_URI = "http://127.0.0.1:3003/api/spotify/callback"

#: Least-privilege scope set — exactly what playback control + read needs.
SCOPES = (
    "user-modify-playback-state user-read-playback-state "
    "playlist-read-private playlist-read-collaborative "
    "user-library-read user-read-currently-playing"
)

AUTH_BASE = "https://accounts.spotify.com"
API_BASE = "https://api.spotify.com/v1"

#: The Connect device the voice librespot registers under. Matched
#: case-insensitively; we NEVER fall through to a random active device.
DEVICE_NAME = "PuffPAI"

#: A minted OAuth state is single-use and dies this many seconds after issue.
_STATE_TTL = 15 * 60

#: Refresh a little before the real expiry so a command never races the clock.
_TOKEN_SKEW = 60

#: Every outbound HTTP call is bounded; a wedged mirror must not wedge voice.
_HTTP_TIMEOUT = 12

#: The Bridge switch every call in this module goes out through. It names
#: exactly ``accounts.spotify.com`` and ``api.spotify.com``.
DOOR_ID = "spotify"

# app_data resolution mirrors puffpai.app_data._data_dir exactly, so the OAuth
# file lands in the SAME directory as the app JSONs. Resolved lazily (see
# statedir docstring: importing config at module scope re-creates a cycle).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "app_data")
_OAUTH_FILENAME = "spotify_oauth.json"


# ── Typed failures (mapped to spoken / JSON messages at the edges) ───────────

class SpotifyError(Exception):
    """Base for every anticipated Spotify failure."""


class SpotifyNotConfigured(SpotifyError):
    """No client_id / client_secret has been stored yet."""


class SpotifyNotConnected(SpotifyError):
    """No refresh token, or a refresh no longer works (re-auth needed)."""


class SpotifyDeviceOffline(SpotifyError):
    """The "PuffPAI" Connect device is not in the account's device list."""


class SpotifyNoPremium(SpotifyError):
    """Playback control returned 403 — the account is not Premium."""


class SpotifyNoActiveDevice(SpotifyError):
    """Spotify returned 404 NO_ACTIVE_DEVICE for a control call."""


class SpotifyRefused(SpotifyError):
    """The Bridge refused the call before anything left the house.

    The message is the registry's own sentence, fit to show or speak as is: for
    a closed switch it names the switch and where to turn it on.
    """


def _switch_open() -> bool:
    """Is the ``spotify`` switch open? A read error counts as closed."""
    try:
        from puffpai.security.egress_doors import egress_allowed
        return bool(egress_allowed(DOOR_ID)[0])
    except Exception:
        return False


def _switch_hint() -> str:
    """The registry's sentence for a closed ``spotify`` switch."""
    try:
        from puffpai.security.egress_doors import door
        d = door(DOOR_ID)
        if d is not None and d.settings_hint:
            return d.settings_hint
    except Exception:
        pass
    return ("Spotify is switched off. To use it, open Settings and switch on "
            "'Spotify' in the Bridge section.")


def _require_switch() -> None:
    """Refuse unless the ``spotify`` switch is open. Opens nothing."""
    from puffpai.security.egress_doors import EgressBlocked, require_open
    try:
        require_open(DOOR_ID)
    except EgressBlocked as blocked:
        raise SpotifyRefused(blocked.user_message) from None


def _send(method: str, url: str, **kwargs):
    """One request to Spotify, through the registry's ``spotify`` switch.

    ``egress_request`` decides before any DNS lookup or socket: a closed
    switch, a host the switch does not name, or a name that resolves into the
    house all become :class:`SpotifyRefused`. Transport errors from
    ``requests`` pass through unchanged.
    """
    from puffpai.security.egress_doors import EgressBlocked, egress_request
    try:
        return egress_request(DOOR_ID, method, url, **kwargs)
    except EgressBlocked as blocked:
        raise SpotifyRefused(blocked.user_message) from None


def _oauth_path() -> str:
    """Absolute path to the 0600 OAuth file, in the app_data directory."""
    from puffpai import statedir
    data_dir = statedir.resolve_runtime_dir(_DEFAULT_DATA_DIR, env_subdir="app_data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, _OAUTH_FILENAME)


def _chmod_600(target) -> None:
    """Best-effort 0600. A no-op on platforms without POSIX modes (dev/Win)."""
    try:
        if isinstance(target, int):
            if hasattr(os, "fchmod"):
                os.fchmod(target, 0o600)
        else:
            os.chmod(target, 0o600)
    except OSError:
        pass


class SpotifyStore:
    """The on-disk credential file — the ONLY place secrets live.

    Every write is atomic (temp + ``os.replace``) and clamped to 0600 before it
    is visible under the real name, so a reader never sees a world-readable or
    half-written secret. Nothing in this class logs a secret value.
    """

    def __init__(self, path: str | None = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        if self._path is None:
            self._path = _oauth_path()
        return self._path

    def _load(self) -> dict[str, Any]:
        try:
            import json
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        import json
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".spotify_oauth.", suffix=".tmp")
        try:
            _chmod_600(fd)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
            _chmod_600(self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- accessors (all lock-guarded; callers never see the raw dict) --------

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._load()

    def set_client(self, client_id: str, client_secret: str) -> None:
        with self._lock:
            data = self._load()
            data["client_id"] = client_id
            data["client_secret"] = client_secret
            self._save(data)

    def set_refresh_token(self, refresh_token: str) -> None:
        with self._lock:
            data = self._load()
            data["refresh_token"] = refresh_token
            self._save(data)

    def clear_tokens(self, forget_app: bool = False) -> None:
        with self._lock:
            data = self._load()
            data.pop("refresh_token", None)
            if forget_app:
                data.pop("client_id", None)
                data.pop("client_secret", None)
            if data:
                self._save(data)
            else:
                # Nothing left worth keeping — remove the file entirely so no
                # empty secret-shaped artifact lingers.
                try:
                    os.unlink(self.path)
                except OSError:
                    pass


class SpotifyControl:
    """Stateful controller: store + cached access token + pending OAuth states.

    One instance per process (see :func:`get_control`). ``requests`` is imported
    lazily inside the request path so tests patch the same module object and no
    network import happens at module load.
    """

    def __init__(self, store: SpotifyStore | None = None):
        self._store = store or SpotifyStore()
        self._lock = threading.Lock()
        self._access_token: str | None = None
        self._access_expiry: float = 0.0
        # state -> issued_at monotonic-ish wall time. In memory on purpose: a
        # state that never touches disk cannot leak, and a login->callback
        # always happens inside one process lifetime.
        self._pending_states: dict[str, float] = {}

    # ── credential presence ─────────────────────────────────────────────────

    def _client(self) -> tuple[str, str]:
        data = self._store.get()
        cid = data.get("client_id")
        csec = data.get("client_secret")
        if not cid or not csec:
            raise SpotifyNotConfigured("Spotify app credentials are not set.")
        return cid, csec

    def is_configured(self) -> bool:
        data = self._store.get()
        return bool(data.get("client_id") and data.get("client_secret"))

    def has_refresh_token(self) -> bool:
        return bool(self._store.get().get("refresh_token"))

    # ── OAuth: authorize URL + state ─────────────────────────────────────────

    def _prune_states(self, now: float) -> None:
        for st in [s for s, t in self._pending_states.items() if now - t > _STATE_TTL]:
            self._pending_states.pop(st, None)

    def _new_state(self) -> str:
        now = time.time()
        state = _secrets.token_urlsafe(24)
        with self._lock:
            self._prune_states(now)
            self._pending_states[state] = now
        return state

    def _consume_state(self, state: str | None) -> bool:
        """Single-use validation: a state is valid at most once, then gone."""
        if not state:
            return False
        now = time.time()
        with self._lock:
            self._prune_states(now)
            issued = self._pending_states.pop(state, None)
        return issued is not None and (now - issued) <= _STATE_TTL

    def build_authorize_url(self) -> str:
        cid, _ = self._client()
        state = self._new_state()
        params = {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
            # A dormant appliance re-authorizing should re-consent cleanly.
            "show_dialog": "false",
        }
        return f"{AUTH_BASE}/authorize?" + urllib.parse.urlencode(params)

    def set_config(self, client_id: str, client_secret: str) -> str:
        client_id = (client_id or "").strip()
        client_secret = (client_secret or "").strip()
        if not client_id or not client_secret:
            raise SpotifyError("client_id and client_secret are both required.")
        self._store.set_client(client_id, client_secret)
        # Setting new app credentials invalidates any cached token.
        with self._lock:
            self._access_token = None
            self._access_expiry = 0.0
        return self.build_authorize_url()

    # ── OAuth: token exchange + refresh ──────────────────────────────────────

    def _basic_auth_header(self) -> dict[str, str]:
        cid, csec = self._client()
        raw = f"{cid}:{csec}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def exchange_code(self, code: str, state: str | None) -> dict[str, Any]:
        """Validate state, swap the auth code for tokens, persist refresh token.

        State is REQUIRED and single-use (CSRF / account-confusion defense).
        The manual-paste path must carry the state from the redirected URL.
        The switch is asked before the state is spent, so a refusal leaves the
        same pasted URL usable once the switch is on.
        """
        _require_switch()
        if not self._consume_state(state):
            raise SpotifyError("Invalid or expired authorization state. "
                               "Start the Spotify connection again.")
        if not code:
            raise SpotifyError("Missing authorization code.")
        import requests
        try:
            resp = _send(
                "POST",
                f"{AUTH_BASE}/api/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                },
                headers=self._basic_auth_header(),
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SpotifyError(f"Could not reach Spotify: {exc}") from exc
        if resp.status_code != 200:
            raise SpotifyError(f"Token exchange failed ({resp.status_code}).")
        payload = resp.json()
        refresh = payload.get("refresh_token")
        if not refresh:
            raise SpotifyError("Spotify did not return a refresh token.")
        self._store.set_refresh_token(refresh)
        with self._lock:
            self._access_token = payload.get("access_token")
            self._access_expiry = time.time() + int(payload.get("expires_in", 3600)) - _TOKEN_SKEW
        # Best-effort account label; never fatal.
        try:
            return {"account": self._account_name()}
        except SpotifyError:
            return {"account": None}

    def _refresh(self) -> None:
        data = self._store.get()
        refresh = data.get("refresh_token")
        if not refresh:
            raise SpotifyNotConnected("Spotify is not connected.")
        import requests
        try:
            resp = _send(
                "POST",
                f"{AUTH_BASE}/api/token",
                data={"grant_type": "refresh_token", "refresh_token": refresh},
                headers=self._basic_auth_header(),
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SpotifyError(f"Could not reach Spotify: {exc}") from exc
        if resp.status_code != 200:
            # 400/401 here means the refresh token was revoked or the app
            # secret changed — the user must re-authorize.
            raise SpotifyNotConnected(
                f"Spotify refused the refresh token ({resp.status_code}). "
                "Re-connect Spotify.")
        payload = resp.json()
        access = payload.get("access_token")
        if not access:
            raise SpotifyNotConnected("Spotify returned no access token.")
        with self._lock:
            self._access_token = access
            self._access_expiry = time.time() + int(payload.get("expires_in", 3600)) - _TOKEN_SKEW
        # Spotify MAY rotate the refresh token; persist the new one if so.
        rotated = payload.get("refresh_token")
        if rotated and rotated != refresh:
            self._store.set_refresh_token(rotated)

    def _valid_access_token(self, force: bool = False) -> str:
        with self._lock:
            token = self._access_token
            fresh = token and time.time() < self._access_expiry
        if fresh and not force:
            return token  # type: ignore[return-value]
        self._refresh()
        with self._lock:
            return self._access_token  # type: ignore[return-value]

    # ── Authed Web API request wrapper (auto-refresh once on 401) ────────────

    def _api(self, method: str, path: str, *, params=None, json_body=None,
             allow_empty=True) -> Any:
        import requests
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        token = self._valid_access_token()
        for attempt in (1, 2):
            try:
                resp = _send(
                    method, url,
                    params=params, json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=_HTTP_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise SpotifyError(f"Could not reach Spotify: {exc}") from exc
            if resp.status_code == 401 and attempt == 1:
                # Access token expired mid-flight; refresh once and retry.
                token = self._valid_access_token(force=True)
                continue
            return self._interpret(resp, allow_empty)
        # Unreachable, but keep the type-checker + fail-closed contract honest.
        raise SpotifyError("Spotify request failed after refresh.")

    @staticmethod
    def _interpret(resp, allow_empty: bool) -> Any:
        code = resp.status_code
        if code == 403:
            raise SpotifyNoPremium(
                "Spotify Premium is required to control playback.")
        if code == 404:
            raise SpotifyNoActiveDevice("No active Spotify device.")
        if code == 401:
            raise SpotifyNotConnected("Spotify authorization expired.")
        if code >= 400:
            raise SpotifyError(f"Spotify API error ({code}).")
        if code == 204 or not (resp.content or b""):
            return {} if allow_empty else None
        try:
            return resp.json()
        except ValueError:
            # A malformed 2xx body must not crash the caller.
            return {}

    # ── Read helpers ─────────────────────────────────────────────────────────

    def _account_name(self) -> str | None:
        me = self._api("GET", "/me")
        if not isinstance(me, dict):
            return None
        return me.get("display_name") or me.get("id")

    def _me(self) -> dict[str, Any]:
        me = self._api("GET", "/me")
        return me if isinstance(me, dict) else {}

    def _devices(self) -> list[dict[str, Any]]:
        data = self._api("GET", "/me/player/devices")
        if not isinstance(data, dict):
            return []
        devices = data.get("devices")
        return devices if isinstance(devices, list) else []

    def _find_puffpai_device(self) -> dict[str, Any] | None:
        for dev in self._devices():
            name = dev.get("name") if isinstance(dev, dict) else None
            if isinstance(name, str) and name.strip().lower() == DEVICE_NAME.lower():
                return dev
        return None

    def _require_puffpai_device_id(self) -> str:
        dev = self._find_puffpai_device()
        if not dev or not dev.get("id"):
            raise SpotifyDeviceOffline(
                f"The {DEVICE_NAME} Spotify speaker is not online.")
        return dev["id"]

    def _search(self, query: str, types: str) -> dict[str, Any]:
        data = self._api("GET", "/search", params={
            "q": query, "type": types, "limit": 5})
        return data if isinstance(data, dict) else {}

    def _my_playlists(self, pages: int = 2) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max(1, pages)):
            data = self._api("GET", "/me/playlists",
                             params={"limit": 50, "offset": offset})
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                break
            out.extend(p for p in items if isinstance(p, dict))
            if len(items) < 50:
                break
            offset += 50
        return out

    def _current_playback(self) -> dict[str, Any] | None:
        data = self._api("GET", "/me/player", allow_empty=True)
        if not isinstance(data, dict) or not data:
            return None
        return data

    # ── Playback actions (always target the PuffPAI device explicitly) ───────

    def play(self, context_uri: str | None = None, uris: list[str] | None = None,
             device_id: str | None = None) -> None:
        dev = device_id or self._require_puffpai_device_id()
        body: dict[str, Any] = {}
        if context_uri:
            body["context_uri"] = context_uri
        if uris:
            body["uris"] = uris
        # No body => resume whatever was paused on the device.
        self._api("PUT", "/me/player/play",
                  params={"device_id": dev},
                  json_body=body or None, allow_empty=True)

    def pause(self, device_id: str | None = None) -> None:
        dev = device_id or self._require_puffpai_device_id()
        self._api("PUT", "/me/player/pause",
                  params={"device_id": dev}, allow_empty=True)

    def next(self, device_id: str | None = None) -> None:
        dev = device_id or self._require_puffpai_device_id()
        self._api("POST", "/me/player/next",
                  params={"device_id": dev}, allow_empty=True)

    def previous(self, device_id: str | None = None) -> None:
        dev = device_id or self._require_puffpai_device_id()
        self._api("POST", "/me/player/previous",
                  params={"device_id": dev}, allow_empty=True)

    # ── Status (never raises; returns only booleans / derived facts) ─────────

    def status(self) -> dict[str, Any]:
        """Return the setup/connection posture. NEVER raises, NEVER leaks a
        secret. ``configured`` = id+secret present; ``connected`` = a refresh
        actually succeeds; account/premium/device only when connected.
        ``bridge_open`` says whether the ``spotify`` switch is on; when it is
        off nothing is contacted and ``bridge_message`` says how to turn it on.
        The switch is read without a ledger line, because the dashboard polls
        this every few seconds and each poll is not an attempt to go out."""
        out = {
            "configured": self.is_configured(),
            "connected": False,
            "account": None,
            "device_online": False,
            "premium": None,
            "bridge_open": _switch_open(),
        }
        if not out["bridge_open"]:
            out["bridge_message"] = _switch_hint()
            return out
        if not out["configured"] or not self.has_refresh_token():
            return out
        try:
            self._valid_access_token()  # forces a refresh if needed
            out["connected"] = True
        except SpotifyError:
            return out
        try:
            me = self._me()
            out["account"] = me.get("display_name") or me.get("id")
            product = me.get("product")
            if isinstance(product, str):
                out["premium"] = (product == "premium")
        except SpotifyError:
            pass
        try:
            out["device_online"] = self._find_puffpai_device() is not None
        except SpotifyError:
            pass
        return out

    def disconnect(self, forget_app: bool = False) -> None:
        self._store.clear_tokens(forget_app=forget_app)
        with self._lock:
            self._access_token = None
            self._access_expiry = 0.0
            if forget_app:
                self._pending_states.clear()


# ── Process-wide singleton ────────────────────────────────────────────────────

_control_lock = threading.Lock()
_control: SpotifyControl | None = None


def get_control() -> SpotifyControl:
    global _control
    with _control_lock:
        if _control is None:
            _control = SpotifyControl()
        return _control


# ══════════════════════════════════════════════════════════════════════════════
#  LLM TOOL BODIES  (plain functions; assistant_tools registers thin shims to these)
# ══════════════════════════════════════════════════════════════════════════════

_SETUP_HINT = ("Spotify isn't set up yet. Open the dashboard settings and "
               "connect a Spotify account to control music by voice.")
_DEVICE_HINT = ("PuffPAI isn't signed in as a Spotify speaker yet, so there's "
                "nothing for me to play on. Check that the PuffPAI Spotify "
                "device is online.")
_PREMIUM_HINT = ("Spotify Premium is required to control playback from here.")


def _switch_refusal() -> str | None:
    """The sentence to answer with when the ``spotify`` switch is off, else
    None. A refusal is recorded in the egress ledger like any other."""
    try:
        _require_switch()
    except SpotifyRefused as exc:
        return str(exc)
    return None


def _label_context(kind: str, name: str) -> str:
    return name or kind


def tool_spotify_play(query: str, control: SpotifyControl | None = None) -> str:
    """Play something on the PuffPAI Spotify speaker. Resolves the request
    against the user's own playlists first, then artist / album / track."""
    ctl = control or get_control()
    query = (query or "").strip().strip("'\"")
    if not query:
        return "Tell me what to play — a playlist, artist, album, or song."
    if not ctl.is_configured() or not ctl.has_refresh_token():
        return _SETUP_HINT
    refused = _switch_refusal()
    if refused:
        return refused
    try:
        device_id = ctl._require_puffpai_device_id()
    except SpotifyDeviceOffline:
        return _DEVICE_HINT
    except SpotifyNotConnected:
        return _SETUP_HINT
    except SpotifyRefused as exc:
        return str(exc)
    except SpotifyError as exc:
        return f"I couldn't reach Spotify just now: {exc}"

    # 1) The user's OWN playlists — best-effort exact-ish name match.
    #    NOTE: Spotify's algorithmic mixes ("Discover Weekly", "Daily Mix",
    #    "Release Radar") are frequently NOT returned by /me/playlists or
    #    /search for third-party API apps. This match is best-effort; if the
    #    name isn't found we say so rather than playing something adjacent.
    try:
        want = query.lower()
        best = None
        for pl in ctl._my_playlists(pages=2):
            name = (pl.get("name") or "").strip()
            low = name.lower()
            if low == want:
                best = pl
                break
            if best is None and (want in low or low in want):
                best = pl
        if best and best.get("uri"):
            ctl.play(context_uri=best["uri"], device_id=device_id)
            return f"Playing {best.get('name') or 'your playlist'} on {DEVICE_NAME}."

        # 2) Search — prefer artist, then album, then track.
        results = ctl._search(query, "artist,album,track")
        for kind, plural in (("artist", "artists"),
                             ("album", "albums"),
                             ("track", "tracks")):
            items = (results.get(plural) or {}).get("items") or []
            if not items:
                continue
            item = items[0]
            uri = item.get("uri")
            name = item.get("name") or query
            if not uri:
                continue
            if kind == "track":
                ctl.play(uris=[uri], device_id=device_id)
                artists = ", ".join(a.get("name", "") for a in item.get("artists", []) if isinstance(a, dict))
                who = f" by {artists}" if artists else ""
                return f"Playing {name}{who} on {DEVICE_NAME}."
            ctl.play(context_uri=uri, device_id=device_id)
            return f"Playing {name} on {DEVICE_NAME}."

        return (f"I couldn't find anything on Spotify matching '{query}'. "
                "Note that Spotify's own mixes like Discover Weekly aren't "
                "always searchable from here.")
    except SpotifyDeviceOffline:
        return _DEVICE_HINT
    except SpotifyNoPremium:
        return _PREMIUM_HINT
    except SpotifyNoActiveDevice:
        return _DEVICE_HINT
    except SpotifyNotConnected:
        return _SETUP_HINT
    except SpotifyRefused as exc:
        return str(exc)
    except SpotifyError as exc:
        return f"Spotify hit a snag: {exc}"


def tool_spotify_pause(control: SpotifyControl | None = None) -> str:
    ctl = control or get_control()
    if not ctl.is_configured() or not ctl.has_refresh_token():
        return _SETUP_HINT
    refused = _switch_refusal()
    if refused:
        return refused
    try:
        ctl.pause()
        return "Paused Spotify."
    except SpotifyDeviceOffline:
        return _DEVICE_HINT
    except SpotifyNoActiveDevice:
        return "Nothing is playing on Spotify right now."
    except SpotifyNoPremium:
        return _PREMIUM_HINT
    except SpotifyNotConnected:
        return _SETUP_HINT
    except SpotifyRefused as exc:
        return str(exc)
    except SpotifyError as exc:
        return f"Spotify hit a snag: {exc}"


def tool_spotify_resume(control: SpotifyControl | None = None) -> str:
    ctl = control or get_control()
    if not ctl.is_configured() or not ctl.has_refresh_token():
        return _SETUP_HINT
    refused = _switch_refusal()
    if refused:
        return refused
    try:
        ctl.play()  # no context => resume
        return f"Resuming Spotify on {DEVICE_NAME}."
    except SpotifyDeviceOffline:
        return _DEVICE_HINT
    except SpotifyNoActiveDevice:
        return "There's nothing paused on Spotify to resume."
    except SpotifyNoPremium:
        return _PREMIUM_HINT
    except SpotifyNotConnected:
        return _SETUP_HINT
    except SpotifyRefused as exc:
        return str(exc)
    except SpotifyError as exc:
        return f"Spotify hit a snag: {exc}"


def tool_spotify_next(control: SpotifyControl | None = None) -> str:
    ctl = control or get_control()
    if not ctl.is_configured() or not ctl.has_refresh_token():
        return _SETUP_HINT
    refused = _switch_refusal()
    if refused:
        return refused
    try:
        ctl.next()
        return "Skipping to the next track."
    except SpotifyDeviceOffline:
        return _DEVICE_HINT
    except SpotifyNoActiveDevice:
        return "Nothing is playing on Spotify to skip."
    except SpotifyNoPremium:
        return _PREMIUM_HINT
    except SpotifyNotConnected:
        return _SETUP_HINT
    except SpotifyRefused as exc:
        return str(exc)
    except SpotifyError as exc:
        return f"Spotify hit a snag: {exc}"


def tool_spotify_status(control: SpotifyControl | None = None) -> str:
    """Spoken summary: what's playing, on which device, for which account."""
    ctl = control or get_control()
    st = ctl.status()
    if not st["configured"]:
        return _SETUP_HINT
    if not st.get("bridge_open"):
        return st.get("bridge_message") or _switch_hint()
    if not st["connected"]:
        return ("Spotify is set up but not connected. Re-connect it in the "
                "dashboard settings.")
    account = st.get("account") or "the shared account"
    try:
        pb = ctl._current_playback()
    except SpotifyError:
        pb = None
    device_note = (f"The {DEVICE_NAME} speaker is online."
                   if st.get("device_online")
                   else f"The {DEVICE_NAME} speaker isn't online right now.")
    if pb and isinstance(pb.get("item"), dict):
        item = pb["item"]
        title = item.get("name") or "something"
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []) if isinstance(a, dict))
        who = f" by {artists}" if artists else ""
        state = "Playing" if pb.get("is_playing") else "Paused"
        dev = (pb.get("device") or {}).get("name") or "a device"
        return f"{state} {title}{who} on {dev} ({account}). {device_note}"
    return f"Spotify is connected as {account}, but nothing is playing. {device_note}"
