"""Cast Live TV from the box to the TVs in the house ("Cast to...").

Home Assistant is the TV registry. A TV is linked ONCE in Home Assistant (its Apple TV or Roku
integration, one code typed in HA's own setup screen) and from then on it is an
allowed device: nobody types a code to cast, change channel or come back.

* The "Cast to..." list is exactly the HA ``media_player`` entities whose
  entity-registry platform is ``apple_tv`` or ``roku``. Nothing else (Music
  Assistant's AirPlay *audio* players share names with the Apple TVs and are
  deliberately excluded).
* Casting calls ``media_player.play_media`` through the box's own HA bridge
  (everyday token, vault service tier, never leaves the backend). The URL it
  hands HA is a CAST PROXY on this backend:
  ``http://<box>:8000/cast/<cast id>/index.m3u8``. The cast id is
  ``secrets.token_urlsafe(32)``, bound to ONE channel and the TV's IP, and it
  dies on Stop, after 90 s unfetched, 2 minutes idle, or 4 hours. The proxy
  fetches only from the box's own Jellyfin, only under ``/videos/<that
  channel>/``, and rewrites every playlist line and ``URI="..."`` attribute into
  opaque ``/cast/<id>/r/<key>.<ext>`` paths. No Jellyfin token, port or
  upstream path is ever served; a served playlist that still matches
  ``api_key|token|8096`` is refused (fail closed).
* The TV's IP comes from HA's device registry (the device's MAC), resolved on
  this box through the AirPlay mDNS ``deviceid``, then the ARP table, both by
  MAC. Names are never matched. No resolvable house-LAN IP, no cast.
* Jellyfin is reached as the ``tv-cast`` user: Live TV plus every library
  (a trusted device on the house network gets the whole media library). Not an admin. The boundary is outside this
  module: the /tvcast routes need the API key (see tv_cast_routes for what
  the dashboard adds), inbound is LAN-only, and the token never leaves the
  backend. It lives in a
  0600 file next to ``media_server_config.json`` and goes upstream in a
  header, never in a URL.
* Books are listed for reading on the device in hand (the dashboard opens
  Jellyfin's own reader); they are never cast.
* Library items (movies, episodes, songs) use the same /cast/ proxy as Live
  TV: Jellyfin HLS, remuxed (codec copy) when the Apple TV can take the codecs
  (h264/hevc video, aac/ac3/eac3 audio), converted to h264 only when not, and
  at most MAX_VIDEO_TRANSCODES of those at a time (CPU encodes). Songs are AAC
  HLS to an Apple TV, sent as "video" so the TV's own player fetches them
  (HA's "music" path would fetch from HA's address, which the id refuses).
* Why HA can say 500: HA refuses even Apple's own public sample
  stream within 0.3 s, and the TV never fetched anything. pyatv fails that way
  when it holds no working AirPlay pairing for the TV, so the fix is in HA
  (re-add the Apple TV with every code). ``ha_failure_message`` says so.
* Counters for diagnosis (``diag``; GET /tvcast/diag, key-gated) are fixed
  event names only: never an id, URL, token or address.
* "More free channels" (the ad-stitched set-aside list, mostly Pluto TV) are
  RELAYED through the same /cast/ proxy: the box fetches the channel from the
  internet and the TV only ever sees an opaque /cast/<id> link. For Pluto the
  box starts a fresh anonymous session per cast; its token (which names the
  house's IP and rough location) stays in this process's memory and is never
  logged, stored, or sent to Home Assistant or the TV. Every relayed fetch goes
  through the ``tv_free_channels`` Bridge switch (closed until opened in
  Settings), checked before each socket and recorded in the egress ledger by
  host, https/443 only, public addresses only (netaddr pinned session), each
  redirect hop re-validated. Relayed playlists keep only standard HLS tags.
* A live channel fetches a segment every few seconds, so the relay's ledger
  lines are coalesced per cast: each (host, outcome) is written the first time
  it happens, then at most once per ``RELAY_LEDGER_QUIET_S`` with a count of
  the fetches in between, and any count still open is written when the cast
  ends or its key is forgotten (see ``_relay_record``).

Logging: device name, channel name, start/stop and the first 6 characters of
the cast id. Never a URL, never a token, never an exception message from
``requests`` or the HA bridge (those can embed URLs or request bodies). /cast/
access lines are dropped in logging_config.

puff puff pass
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import itertools
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit

logger = logging.getLogger("puffpai.tv_cast")

# urllib3 (under requests) logs every request line at DEBUG, e.g.
# 'http://192.168.1.10:8096 "GET /videos/<id>/master.m3u8?...&LiveStreamId=..."'.
# With PUFFPAI_LOG_LEVEL=DEBUG that would put upstream paths and ids in the
# journal, and upstream URLs are never logged. A filter on each urllib3
# logger that emits URLs drops everything below INFO. It only ever removes
# records, never loosens a level someone set; it applies process-wide, which
# is the restrictive direction.
URLLIB3_URL_LOGGERS = ("urllib3.connectionpool", "urllib3.util.retry",
                       "urllib3.poolmanager", "urllib3.connection",
                       "urllib3.response", "requests")


class _BelowInfoDrop(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.INFO


def quiet_urllib3_debug() -> None:
    """Idempotently attach the below-INFO drop filter to urllib3's URL loggers."""
    for name in URLLIB3_URL_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, _BelowInfoDrop) for f in lg.filters):
            lg.addFilter(_BelowInfoDrop())


quiet_urllib3_debug()


# ── limits ────────────────────────────────────────────────────────────────
CAST_MAX_AGE_S = 4 * 3600          # 4 hours, whichever first
CAST_FIRST_FETCH_S = 90            # never fetched by the TV -> revoke
CAST_IDLE_S = 120                  # TV stopped fetching -> revoke
CAST_START_WAIT_S = 25             # how long start() waits for the TV's first fetch
MAX_ACTIVE_CASTS = 4               # CPU guard on the box
DISCOVERY_TTL_S = 60               # plan: device list cached for 60 s
CHANNELS_TTL_S = 60
RESOURCE_MAP_MAX = 8192           # a 4-hour movie at 3 s segments fits
PLAYLIST_MAX_BYTES = 2 * 1024 * 1024
SEGMENT_MAX_BYTES = 64 * 1024 * 1024
REAPER_INTERVAL_S = 10
HA_PLAY_TIMEOUT_S = 8              # HA's apple_tv play_url can block for the whole cast
HA_PLATFORMS = {"apple_tv": "appletv", "roku": "roku"}

CAST_IDLE_LIBRARY_S = 30 * 60      # a paused movie stops fetching; keep its id alive
MAX_VIDEO_TRANSCODES = 1           # software h264 encodes on the CPU, one at a time
LIBRARY_PAGE = 60
LIBRARY_MAX_BITRATE = 120_000_000  # high, so bitrate alone never forces a transcode
#: Cast sheet tab -> (Jellyfin item types, the library to look in or None for all).
#: A tab names a library only so Shows and Anime don't repeat each other.
LIBRARY_KINDS = {"movies": ("Movie", None), "shows": ("Series", "Shows"),
                 "anime": ("Series,Movie", "Anime"), "music": ("MusicAlbum", None),
                 "books": ("Book", None)}
QUERY_MAX = 80
#: Jellyfin item types the sheet can show -> (our kind, playable, audio)
ITEM_TYPES = {"Movie": ("movie", True, False), "Episode": ("episode", True, False),
              "Series": ("show", False, False), "MusicAlbum": ("album", False, True),
              "Audio": ("song", True, True), "Book": ("book", False, False)}

#: Acceptance test applied to every playlist before it is served.
FORBIDDEN_RE = re.compile(r"api_?key|token|8096", re.I)
CID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16}$")
CHANNEL_RE = re.compile(r"^[0-9a-f]{32}$")
PUBLIC_CHANNEL_RE = re.compile(r"^x[0-9a-f]{20}$")
ENTITY_RE = re.compile(r"^media_player\.[a-z0-9_]{1,64}$")
MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_URI_ATTR_RE = re.compile(r'URI="([^"]*)"')
_AUTH_PARAMS = frozenset({"api_key", "apikey"})
EXT_BY_SUFFIX = {".m3u8": "m3u8", ".ts": "ts", ".mp4": "mp4", ".m4s": "m4s",
                 ".aac": "aac", ".vtt": "vtt"}
CONTENT_TYPES = {"m3u8": "application/vnd.apple.mpegurl", "ts": "video/mp2t",
                 "mp4": "video/mp4", "m4s": "video/iso.segment",
                 "aac": "audio/aac", "vtt": "text/vtt",
                 "key": "application/octet-stream"}

DOOR_ID = "tv_free_channels"
PLUTO_BOOT = "https://boot.pluto.tv/v4/start"
RELAY_MAX_HOPS = 3
#: Ledger coalescing for the relay. A live channel fetches a playlist and a
#: segment every few seconds, so one evening would write thousands of identical
#: lines and push older Bridge history out of the rotated ledger. Within one
#: cast, each (host, outcome) is written the first time it happens, then again
#: only once this many seconds have passed since its last line, and that line
#: carries the count of fetches in between.
RELAY_LEDGER_QUIET_S = 600.0
#: Most (cast, host, outcome) keys remembered at once. The oldest is forgotten
#: first, and its open count is written as a last line before it goes.
RELAY_LEDGER_KEYS_MAX = 512
PUBLIC_M3U_DEFAULT = "/data/puffpai/jellyfin/config/livetv/iptv-unreliable.m3u"
PUBLIC_GROUP = "More free channels"
_SECRETISH_PARAM_RE = re.compile(
    r"token|key|auth|sig|secret|pass|jwt|session|credential|policy|hmac|"
    r"expire|login|user|account|email", re.I)
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".arpa",
                   ".localdomain", ".home.arpa")
LINK_HINT = ("{names} {verb} on the network but not linked yet. Link each TV "
             "once in Home Assistant: Settings, Devices and services, Add "
             "integration, Apple TV (or Roku), pick the TV, and type the code "
             "the TV shows. After that it shows up here.")


class CastError(Exception):
    """A refusal with a plain-words message safe to show in the dashboard."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status


# ── cast diagnostics (counters only) ──────────────────────────────────────
# Privacy-safe by construction: event names are fixed strings chosen here
# (what was asked for, the answer code, which KIND of address asked, which
# KIND of client). No cast id, no URL, no token, no address is ever a key.
# Read through the key-gated GET /tvcast/diag.
_DIAG_LOCK = threading.Lock()
_DIAG: dict = {}


def diag(event: str) -> None:
    with _DIAG_LOCK:
        _DIAG[event] = _DIAG.get(event, 0) + 1


def diag_snapshot(reset: bool = False) -> dict:
    with _DIAG_LOCK:
        snap = dict(sorted(_DIAG.items()))
        if reset:
            _DIAG.clear()
    return snap


def _ha_host_ip() -> str:
    try:
        from puffpai.config import get_config
        host = urlsplit(get_config().ha.url).hostname or ""
        return socket.gethostbyname(host) if host else ""
    except Exception:
        return ""


def address_class(remote_ip: str, cast_ip: str = "") -> str:
    """Which KIND of address asked: never the address itself."""
    if cast_ip and remote_ip == cast_ip:
        return "tv"
    if ":" in (remote_ip or ""):
        return "ipv6"
    if remote_ip and remote_ip == _ha_host_ip():
        return "home_assistant"
    try:
        a = ipaddress.ip_address(remote_ip)
    except ValueError:
        return "unparsable"
    if a.is_loopback:
        return "loopback"
    return "house_lan" if is_house_device(remote_ip) else "other"


def client_class(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "applecoremedia" in ua or "appletv" in ua or "tvos" in ua:
        return "apple_player"
    if "aiohttp" in ua or "python" in ua:
        return "python_http"
    if "roku" in ua:
        return "roku"
    return "other" if ua else "none"


# ── small helpers ─────────────────────────────────────────────────────────

def new_token(nbytes: int) -> str:
    """token_urlsafe that can never trip the served-content acceptance test."""
    while True:
        tok = secrets.token_urlsafe(nbytes)
        if not FORBIDDEN_RE.search(tok):
            return tok


def _cid_key(cid: str) -> bytes:
    """Index key for a cast id: its SHA-256, so lookups never compare raw ids."""
    return hashlib.sha256(cid.encode("utf-8")).digest()


def dashed_guid(hex32: str) -> str:
    h = hex32.lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def strip_auth_params(query: str) -> str:
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
             if k.lower() not in _AUTH_PARAMS]
    return urlencode(pairs)


def _posix() -> bool:
    return os.name == "posix"


def read_private_json(path: str) -> dict:
    """Read a secret JSON file, refusing it unless it is 0600-or-tighter and ours."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if _posix() and ((st.st_mode & 0o077) or st.st_uid != os.getuid()):
            raise PermissionError("secret file is not private to this user")
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1
            return json.load(fh)
    finally:
        if fd >= 0:
            os.close(fd)


def write_private_json(path: str, data: dict) -> None:
    """Atomic 0600 write into a 0700 directory."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if _posix():
        os.chmod(directory, 0o700)
    tmp = f"{path}.tmp-{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def local_ip_for(dest_ip: str) -> str:
    """The box address that routes to *dest_ip* (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest_ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def is_house_device(ip: str) -> bool:
    """Targets must be on the house LAN.

    ``PUFFPAI_TV_CAST_LAN_CIDR`` pins the network; otherwise the device must be
    a private IPv4 address in the same /24 as the box address that reaches it.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4 or not addr.is_private or addr.is_loopback \
            or addr.is_link_local or addr.is_multicast:
        return False
    pinned = os.environ.get("PUFFPAI_TV_CAST_LAN_CIDR", "").strip()
    try:
        if pinned:
            return addr in ipaddress.ip_network(pinned, strict=False)
        box = ipaddress.ip_address(local_ip_for(ip))
        return addr in ipaddress.ip_network(f"{box}/24", strict=False) and addr != box
    except (ValueError, OSError):
        return False


def validate_local_base(url: str) -> str:
    """The Jellyfin base must be THIS box (loopback or an address bound here).

    The review says 127.0.0.1:8096; on this box Jellyfin is published on the
    LAN address only, so loopback would not connect. What matters is that the
    proxy can only ever reach the box's own Jellyfin, which this enforces: a
    non-local host refuses, and the base never comes from a caller.
    """
    parts = urlsplit(url or "")
    if parts.scheme != "http" or not parts.hostname:
        raise CastError("jellyfin_config", "The media server address is not set up for casting.", 503)
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(parts.hostname))
    except (OSError, ValueError):
        raise CastError("jellyfin_config", "The media server address is not set up for casting.", 503)
    if not ip.is_loopback:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((str(ip), 0))   # succeeds only for an address on this box
        except OSError:
            raise CastError("jellyfin_config", "Casting only works with the media server on this box.", 503)
        finally:
            probe.close()
    port = parts.port or 80
    return f"http://{parts.hostname}:{port}"


# ── paths ─────────────────────────────────────────────────────────────────

def jellyfin_secret_path() -> str:
    env = os.environ.get("PUFFPAI_TV_CAST_JELLYFIN_FILE", "").strip()
    if env:
        return env
    from puffpai.config import get_config
    return os.path.join(get_config().CONFIG_DIR, "tv_cast_jellyfin.json")


def state_dir() -> str:
    env = os.environ.get("PUFFPAI_TV_CAST_STATE_DIR", "").strip()
    if env:
        return env
    from puffpai.statedir import resolve_runtime_dir
    default = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data", "tv_cast")
    return resolve_runtime_dir(default, env_subdir="tv_cast")

def cast_port() -> int:
    try:
        return int(os.environ.get("PUFFPAI_TV_CAST_PORT", "8000"))
    except ValueError:
        return 8000


# ── Jellyfin, as the tv-cast user ─────────────────────────────────────────

class JellyfinCastClient:
    """Jellyfin as the tv-cast user (not an admin). Token only in headers."""

    def __init__(self, base: str, creds: dict, http=None):
        self.base = base
        self.user_id = str(creds["user_id"])
        self._token = str(creds["access_token"])
        self._device_id = str(creds.get("device_id") or "puffpai-tv-cast")
        if http is None:
            import requests
            http = requests
        self._http = http

    @classmethod
    def from_config(cls) -> "JellyfinCastClient":
        from puffpai.config import get_config
        ms = get_config().media_server
        if not getattr(ms, "enabled", False):
            raise CastError("jellyfin_off", "The media server is turned off.", 503)
        base = validate_local_base(ms.jellyfin_url)
        try:
            creds = read_private_json(jellyfin_secret_path())
        except (OSError, ValueError):
            raise CastError("not_set_up", "TV casting is not set up on this box yet.", 503)
        if not creds.get("access_token") or not creds.get("user_id"):
            raise CastError("not_set_up", "TV casting is not set up on this box yet.", 503)
        return cls(base, creds)

    def _headers(self) -> dict:
        auth = (f'MediaBrowser Client="PuffPAI Cast", Device="PuffPAI", '
                f'DeviceId="{self._device_id}", Version="1.0", Token="{self._token}"')
        return {"Authorization": auth, "X-Emby-Authorization": auth}

    def request(self, method: str, path_query: str, *, json_body=None,
                stream=False, timeout=(3, 15)):
        if not path_query.startswith("/"):
            raise CastError("bad_upstream", "That stream is not available.", 502)
        try:
            return self._http.request(method, self.base + path_query,
                                      headers=self._headers(), json=json_body,
                                      stream=stream, timeout=timeout,
                                      allow_redirects=False)
        except Exception as exc:  # requests' messages embed the URL: class only
            logger.warning("tv-cast: media server call failed (%s)", type(exc).__name__)
            raise CastError("jellyfin_down", "The media server did not answer.", 502)

    def _json(self, method, path_query, **kw):
        r = self.request(method, path_query, **kw)
        if r.status_code >= 300:
            r.close()
            raise CastError("jellyfin_refused", "The media server said no.", 502)
        try:
            return r.json() if r.content else None
        finally:
            r.close()

    def channels(self) -> list:
        data = self._json("GET", f"/LiveTv/Channels?UserId={self.user_id}"
                                 "&EnableImages=false&EnableUserData=false&Limit=1000")
        out = []
        for item in (data or {}).get("Items", []):
            cid = str(item.get("Id", "")).lower()
            if CHANNEL_RE.match(cid):
                out.append({"id": cid, "name": str(item.get("Name", ""))[:120],
                            "number": str(item.get("Number") or "")[:12]})
        return out

    def open_stream(self, channel_id: str) -> dict:
        profile = {
            "MaxStreamingBitrate": 40_000_000,
            "DirectPlayProfiles": [],
            "TranscodingProfiles": [{
                "Container": "ts", "Type": "Video", "VideoCodec": "h264",
                "AudioCodec": "aac", "Protocol": "hls", "Context": "Streaming",
                "BreakOnNonKeyFrames": True}],
        }
        info = self._json(
            "POST",
            f"/Items/{channel_id}/PlaybackInfo?UserId={self.user_id}"
            "&AutoOpenLiveStream=true&IsPlayback=true&MaxStreamingBitrate=40000000",
            json_body={"DeviceProfile": profile})
        for ms in (info or {}).get("MediaSources", []):
            turl = ms.get("TranscodingUrl") or ""
            parts = urlsplit(turl)
            want = f"/videos/{dashed_guid(channel_id)}/master.m3u8"
            if parts.path.lower() != want:
                continue
            q = strip_auth_params(parts.query)
            return {"master": parts.path + (f"?{q}" if q else ""),
                    "live_stream_id": ms.get("LiveStreamId") or "",
                    "play_session_id": (info or {}).get("PlaySessionId") or ""}
        raise CastError("no_stream", "That channel has no stream right now.", 502)

    # ── the library: every library the tv-cast user sees (all of them) ──

    def _library_id(self, name: str) -> str | None:
        """The id of the library called *name* (Shows, Anime), or None."""
        data = self._json("GET", f"/Users/{self.user_id}/Views") or {}
        for v in data.get("Items") or []:
            vid = str(v.get("Id", "")).lower()
            if str(v.get("Name") or "").strip().lower() == name.lower() and CHANNEL_RE.match(vid):
                return vid
        return None

    def library(self, kind: str, query: str = "", start: int = 0, letter: str = "") -> dict:
        spec = LIBRARY_KINDS.get(kind)
        if spec is None:
            raise CastError("bad_kind", "Pick one of the tabs.")
        types, lib_name = spec
        if query and kind == "music":
            types = "MusicAlbum,Audio"
        params = {"IncludeItemTypes": types, "Recursive": "true",
                  "SortBy": "SortName", "SortOrder": "Ascending",
                  "StartIndex": str(max(0, int(start))), "Limit": str(LIBRARY_PAGE),
                  "Fields": "ProductionYear,AlbumArtist,MediaStreams",
                  "EnableImages": "false", "EnableUserData": "false"}
        if query:
            params["SearchTerm"] = query
        elif letter:
            if not re.fullmatch(r"[A-Z]", letter):
                raise CastError("bad_letter", "Pick a letter from A to Z.")
            params["NameStartsWith"] = letter
        if lib_name:
            lib_id = self._library_id(lib_name)
            if lib_id is None:  # that library isn't there: an empty tab, not everything
                return {"items": [], "total": 0, "start": int(params["StartIndex"]),
                        "page": LIBRARY_PAGE}
            params["ParentId"] = lib_id
        data = self._json("GET", f"/Users/{self.user_id}/Items?{urlencode(params)}") or {}
        items = [v for v in (library_view(i) for i in data.get("Items") or []) if v]
        return {"items": items, "total": int(data.get("TotalRecordCount") or 0),
                "start": int(params["StartIndex"]), "page": LIBRARY_PAGE}

    def item(self, item_id: str) -> dict:
        if not CHANNEL_RE.match(item_id or ""):
            raise CastError("bad_item", "That isn't in your library.")
        r = self.request("GET", f"/Users/{self.user_id}/Items/{item_id}")
        try:
            if r.status_code != 200:
                raise CastError("bad_item", "That isn't in your library.")
            return r.json() or {}
        except ValueError:
            raise CastError("bad_item", "That isn't in your library.")
        finally:
            r.close()

    def children(self, item_id: str) -> dict:
        """A show's episodes or an album's songs."""
        parent = self.item(item_id)
        ptype = parent.get("Type")
        if ptype == "Series":
            data = self._json("GET", f"/Shows/{item_id}/Episodes?" + urlencode({
                "UserId": self.user_id, "Fields": "MediaStreams",
                "EnableImages": "false", "EnableUserData": "false"})) or {}
        elif ptype == "MusicAlbum":
            data = self._json("GET", f"/Users/{self.user_id}/Items?" + urlencode({
                "ParentId": item_id, "IncludeItemTypes": "Audio", "Recursive": "true",
                "SortBy": "ParentIndexNumber,IndexNumber,SortName",
                "Fields": "AlbumArtist", "EnableImages": "false",
                "EnableUserData": "false", "Limit": "500"})) or {}
        else:
            raise CastError("bad_item", "That has nothing inside it to pick.")
        items = [v for v in (library_view(i) for i in data.get("Items") or []) if v]
        return {"parent": library_view(parent), "items": items}

    def open_item_stream(self, item_id: str, audio: bool) -> dict:
        """HLS for one library item, remuxed when the TV can take the codecs."""
        if audio:
            # AudioCodec "aac" alone: with "aac,mp3" Jellyfin answers with a
            # progressive /stream instead of HLS.
            prof = {"Container": "ts", "Type": "Audio", "AudioCodec": "aac",
                    "Protocol": "hls", "Context": "Streaming", "MaxAudioChannels": "2",
                    "BreakOnNonKeyFrames": True}
            want = f"/audio/{dashed_guid(item_id)}/master.m3u8"
        else:
            # fMP4 HLS: Apple wants HEVC in fMP4, never TS. h264/hevc are copied
            # (a remux); anything else (old DivX, MPEG-2) is encoded to h264.
            prof = {"Container": "mp4", "Type": "Video", "VideoCodec": "h264,hevc",
                    "AudioCodec": "aac,ac3,eac3", "Protocol": "hls", "Context": "Streaming",
                    "MaxAudioChannels": "6", "BreakOnNonKeyFrames": True}
            want = f"/videos/{dashed_guid(item_id)}/master.m3u8"
        info = self._json(
            "POST",
            f"/Items/{item_id}/PlaybackInfo?UserId={self.user_id}&IsPlayback=true"
            f"&AutoOpenLiveStream=true&MaxStreamingBitrate={LIBRARY_MAX_BITRATE}"
            "&SubtitleStreamIndex=-1",
            json_body={"DeviceProfile": {"MaxStreamingBitrate": LIBRARY_MAX_BITRATE,
                                         "DirectPlayProfiles": [],
                                         "TranscodingProfiles": [prof]}})
        for ms in (info or {}).get("MediaSources", []):
            parts = urlsplit(ms.get("TranscodingUrl") or "")
            if parts.path.lower() != want:
                continue
            q = strip_auth_params(parts.query)
            vcodec = next((str(s.get("Codec") or "").lower() for s in ms.get("MediaStreams") or []
                           if s.get("Type") == "Video"), "")
            return {"master": parts.path + (f"?{q}" if q else ""),
                    "live_stream_id": ms.get("LiveStreamId") or "",
                    "play_session_id": (info or {}).get("PlaySessionId") or "",
                    "video_transcode": (not audio) and vcodec not in ("h264", "hevc")}
        raise CastError("no_stream", "That can't be played right now.", 502)

    def close_stream(self, info: dict) -> None:
        """Best effort: end the transcode job and the live stream."""
        ps = info.get("play_session_id") or ""
        ls = info.get("live_stream_id") or ""
        for method, pq in (
                ("DELETE", f"/Videos/ActiveEncodings?DeviceId={self._device_id}&PlaySessionId={ps}"),
                ("POST", f"/LiveStreams/Close?LiveStreamId={ls}")):
            if (ps and "PlaySessionId" in pq) or (ls and "LiveStreamId" in pq):
                try:
                    self.request(method, pq, timeout=(2, 5)).close()
                except CastError:
                    pass

def library_view(item: dict) -> dict | None:
    """What the dashboard may know about a library item: names, never paths."""
    if not isinstance(item, dict):
        return None
    iid = str(item.get("Id", "")).lower()
    kind = ITEM_TYPES.get(str(item.get("Type") or ""))
    if kind is None or not CHANNEL_RE.match(iid):
        return None
    sub = ""
    if kind[0] == "episode":
        s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
        sub = " ".join(x for x in (f"S{s}" if s is not None else "",
                                   f"E{e}" if e is not None else "") if x)
    elif kind[0] in ("album", "song"):
        sub = str(item.get("AlbumArtist") or ", ".join(item.get("Artists") or []) or "")
    elif item.get("ProductionYear"):
        sub = str(item.get("ProductionYear"))
    return {"id": iid, "name": str(item.get("Name") or "")[:120], "kind": kind[0],
            "sub": sub[:80], "playable": kind[1], "apple_tv_only": kind[2]}


def clean_query(q) -> str:
    """A search term: printable, trimmed, at most QUERY_MAX characters."""
    s = "".join(ch for ch in str(q or "") if ch.isprintable())
    return s.strip()[:QUERY_MAX]


# ── playlist rewriting ────────────────────────────────────────────────────

def allowed_upstream(abs_url: str, base: str, channel_id: str) -> str | None:
    """path?query (auth params stripped) if *abs_url* is this channel on our Jellyfin.

    The path is checked as Jellyfin reads it, percent-decoded, so an encoded
    dot segment or separator (``%2e%2e``, ``%2f``, ``%5c``) cannot step out of
    the channel's own folder. A path that still holds a ``%`` after one decode
    (double encoding) or holds a control character is refused.
    """
    p, b = urlsplit(abs_url), urlsplit(base)
    try:
        same = (p.scheme, (p.hostname or "").lower(), p.port or 80) == \
               (b.scheme, (b.hostname or "").lower(), b.port or 80)
    except ValueError:
        return None
    if not same or p.username or p.password or "%" in p.netloc:
        return None
    path = p.path
    decoded = unquote(path, errors="replace")
    if "%" in decoded or "\ufffd" in decoded or \
            any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return None
    if "//" in decoded or "\\" in decoded or \
            any(seg in (".", "..") for seg in decoded.split("/")):
        return None
    lower = decoded.lower()
    # This one channel or library item only; /audio/ is a song's HLS.
    if not any(lower.startswith(f"/{root}/{ident}/")
               for root in ("videos", "audio")
               for ident in (dashed_guid(channel_id), channel_id)):
        return None
    q = strip_auth_params(p.query)
    return path + (f"?{q}" if q else "")


def ext_for(path_query: str) -> str | None:
    path = path_query.split("?", 1)[0].lower()
    for suffix, ext in EXT_BY_SUFFIX.items():
        if path.endswith(suffix):
            return ext
    return None


def _uri_hint(tag_line: str) -> str:
    """What a URI is, from the tag that names it (for extension-less URLs)."""
    tag = tag_line.split(":", 1)[0]
    if tag in ("#EXT-X-STREAM-INF", "#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF"):
        return "m3u8"
    if tag == "#EXT-X-MAP":
        return "mp4"
    if tag == "#EXT-X-KEY":
        return "key"
    return "ts"


#: Relay casts (internet channels) keep only these HLS tags. Ad-stitched
#: services add vendor tags (ad beacons, session data) that may carry ids or
#: tokens; a tag that is not here is dropped rather than echoed to the TV.
RELAY_TAGS = frozenset({
    "#EXTM3U", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION", "#EXT-X-MEDIA-SEQUENCE",
    "#EXT-X-DISCONTINUITY-SEQUENCE", "#EXT-X-DISCONTINUITY", "#EXT-X-PLAYLIST-TYPE",
    "#EXT-X-INDEPENDENT-SEGMENTS", "#EXTINF", "#EXT-X-STREAM-INF", "#EXT-X-MEDIA",
    "#EXT-X-MAP", "#EXT-X-KEY", "#EXT-X-PROGRAM-DATE-TIME", "#EXT-X-ENDLIST",
    "#EXT-X-BYTERANGE",
})


def rewrite_playlist(text: str, upstream_abs: str, register,
                     allowed_tags: frozenset | None = None) -> str:
    """Rewrite every URI line and URI="..." attribute through *register*.

    ``register(abs_url, hint) -> served path | None``; *hint* is what the URI
    is ("m3u8", "ts", "mp4", "key") judged from the tag that names it. A URI
    that is refused drops its whole line (and its tag line), so nothing
    upstream is ever echoed. With *allowed_tags*, any other tag line is dropped.
    """
    if not text.lstrip("﻿ \r\n\t").startswith("#EXTM3U"):
        raise CastError("bad_playlist", "That stream is not available.", 502)
    out = []
    last_tag = ""
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("#"):
            if allowed_tags is not None and line.split(":", 1)[0] not in allowed_tags:
                continue
            refused = False
            hint = _uri_hint(line)

            def sub(m):
                nonlocal refused
                served = register(urljoin(upstream_abs, m.group(1).strip()), hint)
                if served is None:
                    refused = True
                    return 'URI=""'
                return f'URI="{served}"'
            new = _URI_ATTR_RE.sub(sub, line)
            if not refused:
                out.append(new)
                if line.startswith(("#EXT-X-STREAM-INF", "#EXTINF")):
                    last_tag = line
            continue
        served = register(urljoin(upstream_abs, line.strip()),
                          "m3u8" if last_tag.startswith("#EXT-X-STREAM-INF") else "ts")
        last_tag = ""
        if served is None:
            # Drop the URI and the per-URI tag lines that described it.
            while out and out[-1].startswith(("#EXTINF", "#EXT-X-STREAM-INF",
                                              "#EXT-X-BYTERANGE")):
                out.pop()
            continue
        out.append(served)
    body = "\n".join(out) + "\n"
    if FORBIDDEN_RE.search(body):
        # Fail closed: the acceptance test runs on every serve, not just in CI.
        raise CastError("leak_guard", "That stream is not available.", 502)
    return body



# ── casts ─────────────────────────────────────────────────────────────────

@dataclass(eq=False)
class Cast:
    kind: str                       # "proxy" (Jellyfin) | "relay" (free internet channel); both via /cast/
    device_id: str                  # the HA entity id
    device_name: str
    device_ip: str
    channel_id: str
    channel_name: str
    tv_kind: str = "appletv"        # "appletv" | "roku"
    cid: str | None = None
    created: float = field(default_factory=time.time)
    expires: float = 0.0
    last_fetch: float | None = None
    master: str | None = None
    stream_info: dict | None = None
    resources: "OrderedDict[str, tuple]" = field(default_factory=OrderedDict)
    keys_by_target: dict = field(default_factory=dict)
    media: str = "channel"          # "channel" | "movie" | "episode" | "song"
    video_transcode: bool = False   # True when Jellyfin must encode h264 (CPU)
    ha_result: str | None = None    # None (pending) | "ok" | "sent" | "failed"
    #: relay only: the channel's absolute master URL. For Pluto it carries the
    #: anonymous session token, so it lives in memory only, is never logged,
    #: and is cleared on revoke.
    upstream: str | None = None
    state: str = "starting"
    #: relay only: an in-memory number for this cast, used to coalesce its
    #: ledger lines (see ``_relay_record``). Not the cast id, not secret.
    relay_scope: int | None = None

    @property
    def short(self) -> str:
        return (self.cid or "")[:6] or "-"

    def public_view(self) -> dict:
        return {"device": self.device_id, "device_name": self.device_name,
                "channel": self.channel_id, "channel_name": self.channel_name,
                "kind": self.kind, "media": self.media, "state": self.state,
                "started_at": int(self.created),
                "ends_by": int(self.expires)}


# ── the TV registry: Home Assistant ───────────────────────────────────────

def _avahi_unescape(s: str) -> str:
    s = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), s)
    return s.replace("\\.", ".").replace("\\\\", "\\")


def avahi_airplay_records(timeout: float = 6) -> list:
    """``[{name, ip, deviceid, model}]`` from the box's mDNS cache (no pyatv)."""
    exe = shutil.which("avahi-browse")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "-rpt", "_airplay._tcp"], capture_output=True,
                             text=True, timeout=timeout).stdout
    except Exception:
        return []
    recs = {}
    for line in out.splitlines():
        f = line.split(";")
        if len(f) < 10 or f[0] != "=" or f[2] != "IPv4":
            continue
        txt = f[9]
        dev = re.search(r"deviceid=([0-9A-Fa-f:]{17})", txt)
        model = re.search(r'model=([^"]+)', txt)
        recs[f[7]] = {"name": _avahi_unescape(f[3]), "ip": f[7],
                      "deviceid": dev.group(1).lower() if dev else "",
                      "model": model.group(1) if model else ""}
    return list(recs.values())


def neighbour_macs() -> dict:
    """``{mac: ip}`` from the kernel ARP table (unprivileged ``ip neigh``)."""
    exe = shutil.which("ip") or "/usr/sbin/ip"
    try:
        out = subprocess.run([exe, "-4", "neigh", "show"], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return {}
    found = {}
    for line in out.splitlines():
        m = re.match(r"^(\d+\.\d+\.\d+\.\d+) .*lladdr ([0-9a-fA-F:]{17})", line)
        if m:
            found[m.group(2).lower()] = m.group(1)
    return found


def _ha_registries() -> tuple[list, list]:
    """HA's device + entity registries, as already pulled by the bridge."""
    from puffpai import home_topology
    snap = home_topology.registry_snapshot(allow_pull=True)
    age = snap.get("age_seconds")
    if age is not None and age > 900:
        home_topology._maybe_pull()            # own cooldown; never raises
        snap = home_topology.registry_snapshot(allow_pull=False)
    return list(snap.get("devices") or []), list(snap.get("entities") or [])


class TvRegistry:
    """The allowed TVs = HA media_player entities of the apple_tv / roku platforms."""

    def __init__(self, *, ha=None, registries=_ha_registries,
                 avahi=avahi_airplay_records, neigh=neighbour_macs,
                 lan_check=None):
        self._ha = ha
        self._registries = registries
        self._avahi = avahi
        self._neigh = neigh
        self._lan_check = lan_check or is_house_device
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] = (0.0, {})

    def _bridge(self):
        if self._ha is None:
            from puffpai.ha_bridge import get_bridge
            self._ha = get_bridge()
        return self._ha

    def invalidate(self) -> None:
        with self._lock:
            self._cache = (0.0, self._cache[1])

    def snapshot(self, refresh: bool = False) -> dict:
        with self._lock:
            ts, cached = self._cache
            if cached and not refresh and time.time() - ts < DISCOVERY_TTL_S:
                return cached
        try:
            devices, entities = self._registries()
        except Exception as exc:
            logger.warning("tv-cast: HA registries unavailable (%s)", type(exc).__name__)
            raise CastError("ha_down", "Home Assistant did not answer. Try again in a moment.", 503)
        try:
            states = {s.get("entity_id"): s for s in self._bridge().get_all_states()}
        except Exception as exc:
            logger.warning("tv-cast: HA states unavailable (%s)", type(exc).__name__)
            raise CastError("ha_down", "Home Assistant did not answer. Try again in a moment.", 503)
        by_dev = {d.get("id"): d for d in devices if isinstance(d, dict)}
        avahi = self._avahi()
        neigh = self._neigh()
        avahi_by_mac = {r["deviceid"]: r["ip"] for r in avahi if r.get("deviceid")}
        tvs = []
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            eid = str(ent.get("entity_id") or "")
            kind = HA_PLATFORMS.get(str(ent.get("platform") or ""))
            if kind is None or not ENTITY_RE.match(eid) or ent.get("disabled_by"):
                continue
            dev = by_dev.get(ent.get("device_id")) or {}
            st = states.get(eid) or {}
            name = str(dev.get("name_by_user") or (st.get("attributes") or {}).get("friendly_name")
                       or dev.get("name") or eid)[:80]
            macs = [str(v).lower() for t, v in (dev.get("connections") or [])
                    if t == "mac" and MAC_RE.match(str(v).lower())]
            ip, how = "", ""
            for mac in macs:
                if mac in avahi_by_mac:
                    ip, how = avahi_by_mac[mac], "mdns-mac"
                    break
                if mac in neigh:
                    ip, how = neigh[mac], "arp-mac"
                    break
            # Fail closed: the address comes ONLY from HA's MAC for this device,
            # matched on this box. No name matching, no guessing.
            if ip and not self._lan_check(ip):
                ip, how = "", ""
            state = str(st.get("state") or "unknown")
            reachable = state not in ("unavailable", "unknown")
            try:
                feats = int((st.get("attributes") or {}).get("supported_features") or 0)
            except (TypeError, ValueError):
                feats = 0
            # diag: HA's state word and whether it offers play_media (bit 512).
            diag(f"tv_seen:{kind}:{state}:{'play_media' if feats & 512 else 'no_play_media'}"
                 f":{how or 'no_address'}")
            tvs.append({"entity_id": eid, "name": name, "kind": kind,
                        "model": str(dev.get("model") or "")[:60], "state": state,
                        "ip": ip, "ip_source": how,
                        "available": bool(reachable and ip),
                        "why": ("" if reachable and ip else
                                "Home Assistant can't reach this TV right now." if not reachable
                                else "PuffPAI can't match this TV to an address on the "
                                     "network, so it won't cast to it. Turn the TV on and "
                                     "look again.")})
        tvs.sort(key=lambda d: (not d["available"], d["kind"] != "appletv", d["name"].casefold()))
        linked_ips = {t["ip"] for t in tvs if t["ip"]}
        unlinked = sorted({r["name"] for r in avahi
                           if r.get("model", "").startswith("AppleTV")
                           and r["ip"] not in linked_ips and self._lan_check(r["ip"])},
                          key=str.casefold)
        snap = {"tvs": tvs, "unlinked": unlinked}
        with self._lock:
            self._cache = (time.time(), snap)
        return snap

    def get(self, entity_id: str) -> dict | None:
        for t in self.snapshot()["tvs"]:
            if t["entity_id"] == entity_id:
                return t
        return None

    def play(self, entity_id: str, kind: str, url: str, title: str) -> None:
        """media_player.play_media through the box's HA bridge (gate applies)."""
        data = {"media_content_id": url,
                # apple_tv: "url"/"app" would LAUNCH AN APP; video -> AirPlay play_url.
                "media_content_type": "video" if kind == "appletv" else "url"}
        if kind == "roku":
            data["extra"] = {"format": "hls", "name": title[:60]}
        self._bridge().call_service("media_player", "play_media",
                                    entity_id=entity_id, **data)

    def stop(self, entity_id: str) -> None:
        self._bridge().call_service("media_player", "media_stop", entity_id=entity_id)

    def state(self, entity_id: str) -> str:
        return str((self._bridge().get_entity_state(entity_id) or {}).get("state") or "")

    def public_list(self, mgr: "CastManager", refresh: bool = False) -> dict:
        snap = self.snapshot(refresh)
        active = {c["device"]: c for c in mgr.status()}
        tvs = [{"id": t["entity_id"], "name": t["name"], "kind": t["kind"],
                "model": t["model"], "available": t["available"], "why": t["why"],
                "casting": active.get(t["entity_id"])} for t in snap["tvs"]]
        hint = ""
        if snap["unlinked"]:
            names = snap["unlinked"]
            joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            hint = LINK_HINT.format(names=joined, verb="is" if len(names) == 1 else "are")
        elif not tvs:
            hint = LINK_HINT.format(names="Your TVs", verb="are")
        return {"devices": tvs, "unlinked": snap["unlinked"], "hint": hint}


HA_FAST_FAIL_S = 2.5


def ha_failure_message(cast: "Cast") -> str:
    """Plain words for an HA play_media refusal (status number only, never a body)."""
    code = getattr(cast, "ha_status", "")
    if code == "500" and getattr(cast, "ha_fast", False) and cast.tv_kind == "appletv":
        # HA refuses even Apple's own sample stream within a
        # second, before the TV is asked for anything. pyatv does exactly that
        # when it holds no working AirPlay pairing for the TV.
        return (f"Home Assistant could not open AirPlay on {cast.device_name} "
                "(Home Assistant error 500, straight away). Its AirPlay link to this TV "
                "is missing. In Home Assistant: Settings, Devices and services, Apple TV, "
                f"delete {cast.device_name}, then add it again and type every code the TV "
                "shows.")
    return ("Home Assistant could not send it to the TV"
            + (f" (Home Assistant error {code})." if code else "."))


class CastManager:
    def __init__(self, *, jellyfin_factory=None, registry=None, clock=time.time,
                 relay_http=None):
        self._relay_http_factory = relay_http
        self._lock = threading.RLock()
        self._by_cid: dict[bytes, Cast] = {}   # sha256(cast id) -> Cast
        self._by_device: dict[str, Cast] = {}
        self._jf_factory = jellyfin_factory or JellyfinCastClient.from_config
        self._registry = registry
        self._clock = clock
        self._reaper: threading.Thread | None = None
        self._channels_cache: tuple[float, list] = (0.0, [])

    def _relay_http(self):
        """A requests session that can only reach validated PUBLIC addresses."""
        if self._relay_http_factory is not None:
            return self._relay_http_factory()
        from puffpai.security import netaddr
        return netaddr.pinned_session(posture=netaddr.PUBLIC)

    @property
    def registry(self) -> TvRegistry:
        if self._registry is None:
            self._registry = TvRegistry()
        return self._registry

    # ── channels ──
    def channels(self) -> dict:
        now = self._clock()
        ts, cached = self._channels_cache
        if not cached or now - ts > CHANNELS_TTL_S:
            cached = self._jf_factory().channels()
            self._channels_cache = (now, cached)
        groups = [{"name": "Live TV", "channels": cached}]
        more = {"enabled": public_streams_enabled(), "count": 0}
        if more["enabled"]:
            pub = [{"id": c["id"], "name": c["name"], "number": c["number"],
                    "genre": c["group"]} for c in load_public_channels()]
            more["count"] = len(pub)
            if pub:
                groups.append({"name": PUBLIC_GROUP, "channels": pub,
                               "apple_tv_only": True})
        return {"groups": groups, "more_free_channels": more}

    # ── library (Movies, Shows, Anime, Music, Books) ──
    def library(self, kind: str, query: str = "", start: int = 0, letter: str = "") -> dict:
        try:
            start = max(0, min(int(start or 0), 100_000))
        except (TypeError, ValueError):
            start = 0
        return self._jf_factory().library(str(kind or ""), clean_query(query), start,
                                          str(letter or "").upper()[:1])

    def library_children(self, item_id: str) -> dict:
        return self._jf_factory().children(str(item_id or "").lower())

    def _channel(self, channel_id: str) -> tuple[str, str | None]:
        """(name, public url or None). Only ids we listed are accepted."""
        if CHANNEL_RE.match(channel_id):
            for c in self.channels()["groups"][0]["channels"]:
                if c["id"] == channel_id:
                    return c["name"], None
        elif PUBLIC_CHANNEL_RE.match(channel_id) and public_streams_enabled():
            for c in load_public_channels():
                if c["id"] == channel_id:
                    return c["name"], c["url"]
        raise CastError("bad_channel", "That channel is not on the list.")

    # ── start / stop ──
    def _reserve(self, cast: Cast) -> None:
        """Atomically claim the device slot (cap check + insert under one lock)."""
        with self._lock:
            if cast.device_id in self._by_device:
                raise CastError("busy", "That TV is already starting a cast. Try again in a moment.", 409)
            if len(self._by_device) >= MAX_ACTIVE_CASTS:
                raise CastError("too_many", "Too many TVs are casting. Stop one first.", 409)
            self._by_device[cast.device_id] = cast

    def start(self, device_id: str, channel_id: str = "", item_id: str = "") -> dict:
        if not ENTITY_RE.match(device_id or ""):
            raise CastError("bad_device", "Pick a TV from the list.")
        tv = self.registry.get(device_id)
        if tv is None:
            raise CastError("bad_device", "That TV is not linked. Refresh and try again.")
        if not tv["available"]:
            raise CastError("tv_unavailable", tv["why"] or "That TV can't be reached right now.", 409)
        media, public_url = "channel", None
        if item_id:
            # A library item: it must be in Jellyfin and playable (a book
            # is read on the device in hand, never cast).
            item_id = str(item_id).lower()
            view = library_view(self._jf_factory().item(item_id))
            if not view or not view["playable"]:
                raise CastError("bad_item", "Pick a movie, an episode or a song.")
            if view["apple_tv_only"] and tv["kind"] != "appletv":
                raise CastError("apple_tv_only", "Music only casts to an Apple TV.")
            name, media, channel_id = view["name"], view["kind"], item_id
        else:
            channel_id = str(channel_id or "").lower()
            name, public_url = self._channel(channel_id)
        if public_url and tv["kind"] != "appletv":
            raise CastError("apple_tv_only", "Those channels only cast to an Apple TV.")
        self.stop(device_id, reason="replaced", tell_tv=False)
        cast = Cast("relay" if public_url else "proxy", device_id, tv["name"], tv["ip"],
                    channel_id, name, tv_kind=tv["kind"], media=media)
        cast.expires = cast.created + CAST_MAX_AGE_S
        self._reserve(cast)
        try:
            if public_url:
                self._attach_relay(cast, public_url)
            else:
                self._attach_proxy(cast)
            # HA and the TV only ever get this opaque link, never an upstream URL.
            url = f"http://{local_ip_for(tv['ip'])}:{cast_port()}/cast/{cast.cid}/index.m3u8"
            threading.Thread(target=self._ha_play, args=(cast, url), daemon=True,
                             name="tv-cast-ha-play").start()
            url = ""
            deadline = self._clock() + CAST_START_WAIT_S
            while self._clock() < deadline:
                if cast.ha_result == "failed":
                    raise CastError("ha_refused", ha_failure_message(cast), 502)
                if cast.last_fetch is not None:
                    diag("start:tv_fetched")
                    break                       # the TV itself is fetching: playing
                time.sleep(0.25)
            else:
                raise CastError("tv_silent", "The TV didn't start playing. Is it on?", 504)
            cast.state = "playing"
        except BaseException:
            self.revoke(cast, "start failed", tell_tv=cast.ha_result != "failed")
            raise
        self._ensure_reaper()
        logger.info("TV cast started: %s on %s (cast %s, %s, %s%s)", name, tv["name"],
                    cast.short, cast.kind, media, ", converting" if cast.video_transcode else "")
        return cast.public_view()

    def _ha_play(self, cast: Cast, url: str) -> None:
        t0 = time.monotonic()
        try:
            self.registry.play(cast.device_id, cast.tv_kind, url, cast.channel_name)
            cast.ha_result = "ok"
            diag("ha_play:ok")
        except Exception as exc:
            # HA's apple_tv play_url holds the call open for the whole cast, so
            # a timeout means "handed over", not "failed". Message never logged:
            # an HA error body can echo the request.
            if "timed out" in str(exc).lower():
                cast.ha_result = "sent"
                diag("ha_play:timed_out")
            else:
                # Only the 3-digit HTTP status is kept from the message; the
                # body after it is never logged or shown.
                m = re.search(r"HTTP (\d{3})", str(exc))
                cast.ha_status = m.group(1) if m else ""
                cast.ha_fast = time.monotonic() - t0 < HA_FAST_FAIL_S
                cast.ha_result = "failed"
                diag(f"ha_play:failed:{cast.ha_status or type(exc).__name__}")
                logger.warning("tv-cast: HA play_media failed for %s (%s%s)",
                               cast.device_name, type(exc).__name__,
                               f", HTTP {cast.ha_status}" if cast.ha_status else "")

    def _attach_proxy(self, cast: Cast) -> None:
        """Open the Jellyfin stream and give *cast* a live cast id."""
        jf = self._jf_factory()
        if cast.media == "channel":
            info = jf.open_stream(cast.channel_id)
        else:
            info = jf.open_item_stream(cast.channel_id, audio=cast.media == "song")
            if info.get("video_transcode"):
                with self._lock:
                    busy = sum(1 for c in self._by_device.values()
                               if c is not cast and c.video_transcode)
                    if busy < MAX_VIDEO_TRANSCODES:
                        cast.video_transcode = True
                if not cast.video_transcode:
                    jf.close_stream(info)
                    raise CastError("transcode_busy",
                                    "That movie is an older format the box has to convert, and it "
                                    "is already converting one for another TV. Stop that one first, "
                                    "or pick something else.", 409)
        cast.cid = new_token(32)
        cast.master = info["master"]
        cast.stream_info = info
        cast._jf = jf  # type: ignore[attr-defined]
        if not self._register_cast(cast):
            jf.close_stream(info)
            raise CastError("busy", "That TV is already starting a cast. Try again in a moment.", 409)

    def _register_cast(self, cast: Cast) -> bool:
        """Make the cast id live, unless the cast lost its device slot meanwhile."""
        with self._lock:
            slot = self._by_device.get(cast.device_id)
            orphan = (slot is not None and slot is not cast) or cast.state == "stopped"
            if not orphan:
                self._by_cid[_cid_key(cast.cid)] = cast
        if not orphan:
            self._ensure_reaper()
        return not orphan

    def _attach_relay(self, cast: Cast, public_url: str) -> None:
        """A free internet channel, relayed through /cast/ (switch: tv_free_channels).

        For Pluto the box starts a fresh anonymous session here and keeps the
        token in memory; the TV and Home Assistant never see it.
        """
        upstream = public_url
        pid = _pluto_id(public_url)
        if pid:
            upstream = self._pluto_master(pid)
        cast.upstream = upstream
        cast.cid = new_token(32)
        cast.relay_scope = new_relay_scope()
        cast._http = self._relay_http()  # type: ignore[attr-defined]
        if not self._register_cast(cast):
            cast.upstream = None
            raise CastError("busy", "That TV is already starting a cast. Try again in a moment.", 409)

    def _pluto_master(self, channel: str) -> str:
        """Pluto's own web-client boot call, the same one tvplay.sh makes.

        The same call as docker/tvplayer/tvplay.sh (pluto_master), which is shell
        inside the player container and cannot be imported. No account; the
        token is Pluto's anonymous session for this house.
        """
        params = urlencode({
            "appName": "web", "appVersion": "8.0.0", "deviceVersion": "122.0.0",
            "deviceModel": "web", "deviceMake": "chrome", "deviceType": "web",
            "clientID": str(__import__("uuid").uuid4()), "clientModelNumber": "1.0.0",
            "channelID": channel, "serverSideAds": "false"})
        r, _final = _relay_get(self._relay_http(), PLUTO_BOOT + "?" + params, timeout=(5, 15))
        try:
            if r.status_code != 200:
                raise CastError("pluto_down", "Pluto TV did not start a session. Try again.", 502)
            boot = r.json()
        except ValueError:
            raise CastError("pluto_down", "Pluto TV did not start a session. Try again.", 502)
        finally:
            r.close()
        stitcher = str(((boot or {}).get("servers") or {}).get("stitcher") or "")
        sparams = str((boot or {}).get("stitcherParams") or "")
        token = str((boot or {}).get("sessionToken") or "")
        boot = None
        if not re.fullmatch(r"https://[a-z0-9.-]+\.pluto\.tv", stitcher) or not token:
            raise CastError("pluto_down", "Pluto TV did not start a session. Try again.", 502)
        q = urlencode({"jwt": token, "masterJWTPassthrough": "true"})
        return f"{stitcher}/v2/stitch/hls/channel/{channel}/master.m3u8?{sparams}&{q}"

    def open_proxy_cast(self, device_id, device_name, device_ip, channel_id,
                        channel_name, media: str = "channel") -> Cast:
        """A proxy cast with no TV attached (tests and the on-box proof)."""
        cast = Cast("proxy", device_id, device_name, device_ip, channel_id, channel_name,
                    media=media)
        cast.expires = cast.created + CAST_MAX_AGE_S
        self._attach_proxy(cast)
        return cast

    def stop(self, device_id: str | None = None, reason: str = "stopped",
             tell_tv: bool = True) -> int:
        with self._lock:
            targets = [c for d, c in self._by_device.items()
                       if device_id is None or d == device_id]
            if device_id is None:
                targets += [c for c in self._by_cid.values() if c not in targets]
        for cast in targets:
            self.revoke(cast, reason, tell_tv=tell_tv)
        return len(targets)

    def revoke(self, cast: Cast, reason: str, tell_tv: bool = True) -> None:
        """Kill the cast id FIRST (synchronously), then clean up in the background."""
        with self._lock:
            was = False
            if cast.cid and self._by_cid.get(_cid_key(cast.cid)) is cast:
                del self._by_cid[_cid_key(cast.cid)]
                was = True
            if self._by_device.get(cast.device_id) is cast:
                del self._by_device[cast.device_id]
                was = True
            if not was:
                return
            cast.state = "stopped"
            cast.resources.clear()
            cast.keys_by_target.clear()
            cast.upstream = None

        def cleanup():
            http = getattr(cast, "_http", None)
            if http is not None:
                try:
                    http.close()
                except Exception:
                    pass
            if cast.relay_scope is not None:
                flush_relay_ledger(cast.relay_scope)
            jf = getattr(cast, "_jf", None)
            if jf is not None and cast.stream_info:
                jf.close_stream(cast.stream_info)
            if tell_tv and ENTITY_RE.match(cast.device_id or ""):
                try:
                    self.registry.stop(cast.device_id)
                except Exception as exc:
                    logger.info("tv-cast: HA media_stop for %s: %s", cast.device_name,
                                type(exc).__name__)
        threading.Thread(target=cleanup, daemon=True, name="tv-cast-cleanup").start()
        logger.info("TV cast stopped: %s on %s (cast %s, %s)", cast.channel_name,
                    cast.device_name, cast.short, reason)

    def status(self) -> list:
        with self._lock:
            return [c.public_view() for c in self._by_device.values()]

    # ── proxy ──
    def lookup(self, cid: str, remote_ip: str) -> Cast | None:
        if not CID_RE.match(cid or ""):
            diag(f"miss:bad_id:{address_class(remote_ip)}")
            return None
        # Constant-time: the dict is keyed by SHA-256 of the id, so the lookup's
        # timing depends only on a hash the caller cannot steer bytewise, and
        # the id itself is then confirmed with compare_digest.
        with self._lock:
            cast = self._by_cid.get(_cid_key(cid))
            if cast is None or not hmac.compare_digest(cast.cid or "", cid):
                miss = "miss:unknown_id"
                cast = None
            else:
                miss = ""
                now = self._clock()
                if now > cast.expires:
                    expired = True
                else:
                    expired = False
                    if remote_ip != cast.device_ip:
                        miss = "miss:wrong_address"
                    else:
                        cast.last_fetch = now
        if miss:
            diag(f"{miss}:{address_class(remote_ip)}")
            return None
        if expired:
            diag("miss:expired")
            self.revoke(cast, "expired")
            return None
        return cast

    def _register(self, cast: Cast, abs_url: str, hint: str | None = None) -> str | None:
        if cast.kind == "relay":
            if not relay_url_ok(abs_url):
                return None
            target = abs_url
            ext = ext_for(urlsplit(abs_url).path) or hint
            if ext not in CONTENT_TYPES:
                return None
        else:
            jf = getattr(cast, "_jf")
            target = allowed_upstream(abs_url, jf.base, cast.channel_id)
            if target is None:
                return None
            ext = ext_for(target)
            if ext is None:
                return None
        with self._lock:
            if self._by_cid.get(_cid_key(cast.cid or "")) is not cast:
                return None
            # Same upstream resource -> same key, so a re-fetched playlist
            # (live windows, a TV re-reading a long movie's list) does not churn.
            key = cast.keys_by_target.get((target, ext))
            if key is None or key not in cast.resources:
                key = new_token(12)
                cast.resources[key] = (target, ext)
                cast.keys_by_target[(target, ext)] = key
            while len(cast.resources) > RESOURCE_MAP_MAX:
                old, entry = cast.resources.popitem(last=False)
                cast.keys_by_target.pop(entry, None)
        return f"/cast/{cast.cid}/r/{key}.{ext}"

    def fetch(self, cast: Cast, key: str | None, ext: str | None):
        """Return (status, content_type, body_or_iter)."""
        if key is None:
            target, ext = (cast.upstream if cast.kind == "relay" else cast.master), "m3u8"
        else:
            if not KEY_RE.match(key or ""):
                return 404, None, None
            with self._lock:
                entry = cast.resources.get(key)
            if entry is None or entry[1] != ext:
                return 404, None, None
            target = entry[0]
        if not target:
            return 404, None, None
        if cast.kind == "relay":
            r, base_url = _relay_get(getattr(cast, "_http"), target,
                                     scope=cast.relay_scope)
            tags = RELAY_TAGS
        else:
            jf = getattr(cast, "_jf")
            r = jf.request("GET", target, stream=True, timeout=(3, 30))
            base_url, tags = jf.base + target, None
        if ext == "m3u8":
            try:
                if r.status_code != 200:
                    return (404 if r.status_code == 404 else 502), None, None
                raw = b""
                for chunk in r.iter_content(65536):
                    raw += chunk
                    if len(raw) > PLAYLIST_MAX_BYTES:
                        return 502, None, None
            finally:
                r.close()
            body = rewrite_playlist(raw.decode("utf-8", "replace"), base_url,
                                    lambda u, h=None: self._register(cast, u, h),
                                    allowed_tags=tags)
            return 200, CONTENT_TYPES["m3u8"], body.encode("utf-8")
        if r.status_code != 200:
            r.close()
            return (404 if r.status_code == 404 else 502), None, None

        def body():
            sent = 0
            try:
                for chunk in r.iter_content(65536):
                    sent += len(chunk)
                    if sent > SEGMENT_MAX_BYTES:
                        break
                    yield chunk
            except Exception as exc:
                # Ends the response short. Class name only: a requests error
                # message names the upstream host and port.
                logger.debug("tv-cast segment cut short (%s)", type(exc).__name__)
            finally:
                r.close()
        return 200, CONTENT_TYPES.get(ext, "application/octet-stream"), body()


    # ── reaper ──
    def _ensure_reaper(self) -> None:
        with self._lock:
            if self._reaper is None or not self._reaper.is_alive():
                self._reaper = threading.Thread(target=self._reap_loop, daemon=True,
                                                name="tv-cast-reaper")
                self._reaper.start()

    def reap_once(self) -> None:
        now = self._clock()
        with self._lock:
            casts = list({id(c): c for c in list(self._by_cid.values())
                          + list(self._by_device.values())}.values())
        for cast in casts:
            if now > cast.expires:
                self.revoke(cast, "expired")
            elif cast.last_fetch is None and now - cast.created > CAST_FIRST_FETCH_S:
                self.revoke(cast, "never fetched")
            elif cast.last_fetch is not None and now - cast.last_fetch > (
                    CAST_IDLE_S if cast.media == "channel" else CAST_IDLE_LIBRARY_S):
                self.revoke(cast, "idle", tell_tv=False)

    def _reap_loop(self) -> None:
        while True:
            time.sleep(REAPER_INTERVAL_S)
            try:
                self.reap_once()
            except Exception as exc:
                logger.warning("tv-cast reaper: %s", type(exc).__name__)
            with self._lock:
                if not self._by_cid and not self._by_device:
                    self._reaper = None
                    return


# ── "More free channels" ──────────────────────────────────────────────────

def public_streams_enabled() -> bool:
    """The owner's switch is the ``tv_free_channels`` egress door (default shut)."""
    try:
        from puffpai.security import egress_doors
        return bool(egress_doors.egress_allowed(DOOR_ID)[0])
    except Exception:
        return False


def _pluto_id(url: str) -> str | None:
    """Reuses tv_player's Pluto address parser (old relay or Pluto's stitcher)."""
    try:
        from puffpai.tv_player import pluto_id
    except Exception:
        return None
    return pluto_id(url)


def relay_url_ok(url: str) -> bool:
    """A URL the relay may fetch: https, port 443, a DNS name, no userinfo.

    The address behind the name is checked separately (netaddr PUBLIC posture,
    pinned at connect time). Query strings are allowed here because the TV never
    sees them: CDNs sign segment URLs, and Pluto carries its session in one.
    """
    if not isinstance(url, str) or len(url) > 4096 or any(c.isspace() for c in url):
        return False
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host or "." not in host or p.username or p.password:
        return False
    if port not in (None, 443) or "@" in p.netloc or "%" in p.netloc:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not (host == "localhost" or host.endswith(_LOCAL_SUFFIXES))


# ── the relay's egress-ledger lines ───────────────────────────────────────

_relay_ledger_lock = threading.Lock()
#: (relay scope, host, kind) -> _RelayKey. In memory only, bounded by
#: RELAY_LEDGER_KEYS_MAX, oldest forgotten first.
_relay_ledger_seen: "OrderedDict[tuple, _RelayKey]" = OrderedDict()
_relay_scopes = itertools.count(1)
#: The coalescing clock. Monotonic so a wall-clock change cannot hide a line;
#: a module attribute so tests can move it without sleeping.
_relay_clock = time.monotonic


@dataclass
class _RelayKey:
    ok: bool
    detail: str
    #: Clock time of the key's last written line; None means the next hop is due.
    last: float | None = None
    #: Hops for this key that are not in the ledger yet.
    skipped: int = 0
    #: A line for this key is being written right now.
    writing: bool = False


def new_relay_scope() -> int:
    """A fresh number for one cast's relay (in memory only, never reused)."""
    return next(_relay_scopes)


def _relay_write(ok: bool, host: str, detail: str, skipped: int, kind: str,
                 **fields) -> None:
    """Write one line. Raises whatever the ledger raises."""
    from puffpai.security import egress_doors
    if skipped:
        detail = f"{detail}, plus {skipped} more since the last line"
    egress_doors.record_external(DOOR_ID, ok, detail=detail, host=host,
                                 kind=kind, **fields)


def _relay_flush(items) -> None:
    """Write the open count of each (key, entry) as a last line. Never raises.

    The line stands for exactly the hops counted, none of its own, so it says
    "N more" rather than the "plus N more" of a line written for a fetch.
    """
    from puffpai.security import egress_doors
    for (_scope, host, kind), entry in items:
        if entry.skipped <= 0:
            continue
        try:
            egress_doors.record_external(
                DOOR_ID, entry.ok, host=host, kind=kind,
                detail=f"{entry.detail}: {entry.skipped} more since the last line")
        except Exception as exc:  # a ledger fault must not break a cast
            logger.warning("tv-cast: %d relay hops not written to the ledger (%s)",
                           entry.skipped, type(exc).__name__)


def _relay_evict_locked() -> list:
    """Drop the oldest idle keys over the bound; return them for flushing."""
    out = []
    if len(_relay_ledger_seen) <= RELAY_LEDGER_KEYS_MAX:
        return out
    for key in list(_relay_ledger_seen):
        if len(_relay_ledger_seen) <= RELAY_LEDGER_KEYS_MAX:
            break
        entry = _relay_ledger_seen[key]
        if entry.writing:
            # Its writer still needs the entry to settle the outcome.
            continue
        del _relay_ledger_seen[key]
        out.append((key, entry))
    return out


def flush_relay_ledger(scope) -> None:
    """Write any open count for one cast and forget its keys. Never raises."""
    with _relay_ledger_lock:
        items = [(k, e) for k, e in _relay_ledger_seen.items()
                 if k[0] == scope and not e.writing]
        for key, _entry in items:
            del _relay_ledger_seen[key]
    _relay_flush(items)


def _relay_record(ok: bool, host: str, detail: str, *, kind: str = "request",
                  scope=None, **fields) -> None:
    """One egress-ledger line for a relay hop, coalesced per cast. Never raises.

    Host only, never the path or query: a relayed path can carry a session id
    and the query carries Pluto's session token.

    With no *scope* (the Pluto session call, made once per cast) every hop is
    written. Within a scope, a (host, kind) key is written the first time it is
    seen, then again only once RELAY_LEDGER_QUIET_S has passed since its last
    line, and that line counts the hops in between. The decision is taken
    under one lock, so racing hops on a new key produce one line, not several.
    A key counts as written only after the ledger accepts the line: if the
    write raises, the hop joins the open count and the next hop is due at once,
    so a failing write never silences the window. Keys forgotten to keep the
    map bounded, and every key of a cast that ends, have their open count
    written as a last line rather than dropped.
    """
    if scope is None:
        try:
            _relay_write(ok, host, detail, 0, kind, **fields)
        except Exception as exc:  # a ledger fault must not break a cast
            logger.debug("tv-cast: egress ledger write failed (%s)", type(exc).__name__)
        return
    key = (scope, host, kind)
    now = _relay_clock()
    with _relay_ledger_lock:
        entry = _relay_ledger_seen.get(key)
        if entry is None:
            entry = _relay_ledger_seen[key] = _RelayKey(ok, detail)
        _relay_ledger_seen.move_to_end(key)
        if entry.writing or (entry.last is not None
                             and now - entry.last < RELAY_LEDGER_QUIET_S):
            entry.skipped += 1
            return
        carried, entry.skipped = entry.skipped, 0
        entry.writing = True
        entry.ok, entry.detail = ok, detail
        evicted = _relay_evict_locked()
    try:
        _relay_write(ok, host, detail, carried, kind, **fields)
        written = True
    except Exception as exc:  # a ledger fault must not break a cast
        logger.debug("tv-cast: egress ledger write failed (%s)", type(exc).__name__)
        written = False
    with _relay_ledger_lock:
        entry.writing = False
        if written:
            entry.last = now
        else:
            entry.last = None
            entry.skipped += carried + 1
        evicted += _relay_evict_locked()
    _relay_flush(evicted)


def _content_length(resp) -> int | None:
    try:
        raw = resp.headers.get("Content-Length")
        return int(raw) if raw is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _relay_get(session, url: str, *, timeout=(5, 30), scope=None):
    """GET one relayed URL through the tv_free_channels switch. Returns (response, final_url).

    The switch is asked before EVERY socket, so closing it stops a running
    cast, and every hop goes to the egress ledger by host: allowed, refused or
    failed, coalesced per *scope* (one cast) by :func:`_relay_record`; with no
    scope every hop is written. Redirects are followed here (at most
    RELAY_MAX_HOPS), each hop re-checked. Nothing about the URL is ever logged:
    it can carry Pluto's session token.
    """
    from puffpai.security import egress_doors, egress_ledger, netaddr
    for _hop in range(RELAY_MAX_HOPS + 1):
        try:
            egress_doors.require_open("tv_free_channels")
        except egress_doors.EgressBlocked as blocked:
            raise CastError("door_closed", blocked.user_message, 409)
        host = egress_ledger.safe_parts(url)[0]
        if not relay_url_ok(url):
            _relay_record(False, host, "relay address refused", kind="url_check",
                          scope=scope)
            raise CastError("bad_upstream", "That channel is not available.", 502)
        try:
            try:
                netaddr.validated_addresses(urlsplit(url).hostname, 443, posture=netaddr.PUBLIC)
            except netaddr.ResolutionError:
                # One retry: a single missed DNS answer (Pluto's boot host can
                # miss one and resolve again right after)
                # should not fail a cast.
                time.sleep(1.0)
                netaddr.validated_addresses(urlsplit(url).hostname, 443, posture=netaddr.PUBLIC)
            r = session.get(url, stream=True, timeout=timeout, allow_redirects=False)
        except netaddr.AddressRefused as refused:
            _relay_record(False, host, "relay host is not a public address",
                          kind="address_check", scope=scope,
                          address_class=refused.address_class)
            logger.warning("tv-cast: free channel fetch failed (%s)", type(refused).__name__)
            raise CastError("upstream_down", "The channel did not answer.", 502)
        except Exception as exc:
            _relay_record(True, host, f"GET {host} failed: {type(exc).__name__}",
                          kind="request_failed", scope=scope)
            logger.warning("tv-cast: free channel fetch failed (%s)", type(exc).__name__)
            raise CastError("upstream_down", "The channel did not answer.", 502)
        _relay_record(True, host, f"GET {host}", scope=scope,
                      status=getattr(r, "status_code", None),
                      bytes_out=0, bytes_in=_content_length(r))
        loc = r.headers.get("Location") if r.status_code in (301, 302, 303, 307, 308) else None
        if not loc:
            return r, url
        r.close()
        url = urljoin(url, loc)
    raise CastError("upstream_down", "The channel did not answer.", 502)


def public_url_ok(url: str) -> bool:
    """A set-aside channel URL we would hand to an Apple TV as-is.

    https only, a real DNS name (no IP literal, no house-local name), no
    userinfo or custom port, and no query parameter whose NAME looks like a
    credential. Pluto entries go through a redirector that adds Pluto's own
    session parameters on the TV's side; those are minted for the TV, not
    taken from this box.
    """
    if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() for c in url):
        return False
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return False
    host = (p.hostname or "").lower()
    if p.scheme != "https" or not host or "." not in host or p.username or p.password:
        return False
    if port not in (None, 443) or "@" in p.netloc:
        return False
    try:
        ipaddress.ip_address(host)
        return False                      # IP literals refused outright
    except ValueError:
        pass
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return False
    for k, _ in parse_qsl(p.query, keep_blank_values=True):
        if _SECRETISH_PARAM_RE.search(k):
            return False
    if FORBIDDEN_RE.search(p.path):
        return False
    return True


_public_cache: tuple[float, float, list] = (0.0, 0.0, [])


def load_public_channels(path: str | None = None) -> list:
    """Parse the set-aside M3U. Channels whose URL fails public_url_ok are dropped."""
    global _public_cache
    path = path or os.environ.get("PUFFPAI_TV_CAST_PUBLIC_M3U", PUBLIC_M3U_DEFAULT)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    ts, cached_mtime, cached = _public_cache
    if cached and cached_mtime == mtime and time.time() - ts < 300:
        return cached
    out, info = [], None
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("#EXTINF"):
                    info = line
                    continue
                if not line or line.startswith("#"):
                    continue
                if info is None:
                    continue
                name = info.rsplit(",", 1)[-1].strip()[:120]
                grp = re.search(r'group-title="([^"]*)"', info)
                chno = re.search(r'tvg-chno="([^"]*)"', info)
                info = None
                if not name or not public_url_ok(line):
                    continue
                out.append({"id": "x" + hashlib.sha256(line.encode()).hexdigest()[:20],
                            "name": name, "group": (grp.group(1) if grp else "")[:40],
                            "number": (chno.group(1) if chno else "")[:12], "url": line})
    except OSError:
        return []
    _public_cache = (time.time(), mtime, out)
    return out



# ── singleton ─────────────────────────────────────────────────────────────

_manager: CastManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> CastManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = CastManager()
        return _manager


__all__ = ["CastError", "CastManager", "TvRegistry", "JellyfinCastClient",
           "get_manager", "rewrite_playlist", "allowed_upstream", "public_url_ok",
           "load_public_channels", "new_token", "FORBIDDEN_RE"]

if sys.version_info < (3, 10):  # pragma: no cover
    raise ImportError("tv_cast needs Python 3.10+")
