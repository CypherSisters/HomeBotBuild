"""
PuffPAI Media Server Bridge
=============================
Single source of truth for all media server communication.

Every component (API server, assistant tools, MCP server, dashboard) should
go through this module instead of making direct Jellyfin/Calibre calls.

Usage:
    from puffpai.media_bridge import get_media_bridge

    bridge = get_media_bridge()
    bridge.search_music("miles davis")
    bridge.play_music(item_id, session_id)
    bridge.now_playing()
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from puffpai.config import get_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class MediaItem:
    """Normalised representation of a media item (track, movie, or book)."""
    item_id: str
    title: str
    media_type: str  # "music", "movie", "book"
    artist: str = ""
    album: str = ""
    genre: str = ""
    year: int = 0
    duration_seconds: int = 0
    path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display(self) -> str:
        if self.media_type == "music":
            parts = [self.title]
            if self.artist:
                parts.append(f"by {self.artist}")
            if self.album:
                parts.append(f"on {self.album}")
            return " ".join(parts)
        elif self.media_type == "movie":
            yr = f" ({self.year})" if self.year else ""
            return f"{self.title}{yr}"
        else:
            parts = [self.title]
            if self.artist:
                parts.append(f"by {self.artist}")
            return " ".join(parts)


@dataclass
class PlaybackSession:
    """Active playback info from Jellyfin."""
    session_id: str
    client_name: str
    device_name: str
    user_name: str
    now_playing: MediaItem | None = None
    is_paused: bool = False
    position_seconds: int = 0
    volume_level: int | None = None


class MediaBridgeError(Exception):
    """Raised when a media server operation fails."""


# ---------------------------------------------------------------------------
# Input validation for anything that becomes part of a URL
# ---------------------------------------------------------------------------
# Session ids, item ids, playlist ids and playlist-entry ids all end up as
# *path segments* of an API-key-authenticated Jellyfin request, and several of
# them arrive straight from a request body (POST /api/media/play,
# /api/media/control) or an agent envelope (Steward "media_play"). requests
# resolves dot segments before sending, so an unvalidated id is an
# authenticated request primitive against arbitrary Jellyfin endpoints
# ("../../System/Configuration", "../Users") plus query-string injection via
# "x?api_key=...". Allow-list rather than escape-and-hope.
#
# Jellyfin ids are GUIDs: 32 lowercase hex characters, or the dashed 8-4-4-4-12
# form on some server/plugin versions. The pattern below covers both (and any
# alphanumeric id a future Jellyfin version returns) while excluding every
# character that could leave the segment: "/", ".", "?", "#", "%", ":", "@" and
# whitespace.
#
# ``\Z`` rather than ``$``: ``$`` also matches before a trailing newline, which
# would let "abc123\n" through.
MEDIA_ID_RE = re.compile(r"\A[A-Za-z0-9-]{1,64}\Z")

_MAX_ECHOED_INPUT = 16


def _echo(value: Any) -> str:
    """Short, sanitised rendering of rejected input for the error message.

    Never returns more than :data:`_MAX_ECHOED_INPUT` characters and strips
    anything non-printable, so a rejected value cannot smuggle control
    characters into logs or into an API error body.
    """
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    cleaned = "".join(ch for ch in value[:_MAX_ECHOED_INPUT] if ch.isprintable())
    suffix = "..." if len(value) > _MAX_ECHOED_INPUT else ""
    return f"{cleaned!r}{suffix}"


def is_valid_media_id(value: Any) -> bool:
    """True when *value* is a safe Jellyfin id usable as a URL path segment."""
    return isinstance(value, str) and MEDIA_ID_RE.match(value) is not None


def validate_media_id(value: Any, *, kind: str = "id") -> str:
    """Return *value* unchanged if it is a safe Jellyfin id, else raise.

    Raises :class:`MediaBridgeError` before any socket is touched.
    """
    if not is_valid_media_id(value):
        raise MediaBridgeError(f"invalid {kind}: {_echo(value)}")
    return value


def quote_segment(value: str) -> str:
    """Percent-encode one path segment (belt and braces behind the validators).

    Ids that pass :func:`validate_media_id` come through untouched; anything a
    future validator gap lets through cannot escape its segment.
    """
    return quote(value, safe="")


def _assert_no_traversal(path: str) -> None:
    """Last line of defence: reject dot segments in an assembled API path."""
    bare = path.split("?", 1)[0].split("#", 1)[0]
    if any(seg in ("..", ".") for seg in bare.split("/")):
        raise MediaBridgeError(f"invalid Jellyfin path: {_echo(path)}")


# ---------------------------------------------------------------------------
# State cache (thread-safe, TTL-based)
# ---------------------------------------------------------------------------

class _StateCache:
    """Simple thread-safe TTL cache for Jellyfin state."""

    def __init__(self, ttl: float = 5.0):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, val = entry
            if time.time() - ts > self._ttl:
                del self._data[key]
                return None
            return val

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key:
                self._data.pop(key, None)
            else:
                self._data.clear()


# ---------------------------------------------------------------------------
# Main bridge class
# ---------------------------------------------------------------------------

class MediaBridge:
    """Centralised bridge to Jellyfin + Calibre-Web media servers."""

    def __init__(self):
        cfg = get_config()
        self._jellyfin_url = cfg.media_server.jellyfin_url.rstrip("/")
        self._jellyfin_api_key = cfg.media_server.jellyfin_api_key
        self._calibre_db = cfg.media_server.calibre_db_path
        self._calibre_web_url = cfg.media_server.calibre_web_url.rstrip("/")
        self._media_mount = cfg.media_server.media_mount
        self._timeout = cfg.media_server.timeout
        self._enabled = cfg.media_server.enabled
        self._user_id = ""  # lazily resolved admin user for playlist/favorite ops
        self._cache = _StateCache(ttl=5.0)
        self._session = requests.Session()
        self._session.headers.update({
            "X-Emby-Authorization": (
                f'MediaBrowser Client="PuffPAI", Device="PuffPAI-Server", '
                f'DeviceId="puffpai-bridge", Version="1.0.0", '
                f'Token="{self._jellyfin_api_key}"'
            ),
        })

    # --- Low-level Jellyfin HTTP ---

    def _jf_request(
        self, path: str, method: str = "GET",
        params: dict | None = None, data: dict | None = None,
    ) -> dict | list:
        """Make an authenticated Jellyfin REST API call."""
        if not self._enabled:
            raise MediaBridgeError("Media server integration is disabled. Set enabled=true in media_server_config.json")
        # Independent of the per-argument validators above: whatever assembled
        # this path, a dot segment never leaves this function.
        _assert_no_traversal(path)
        url = f"{self._jellyfin_url}{path}"
        try:
            resp = self._session.request(
                method, url, params=params, json=data, timeout=self._timeout,
            )
            resp.raise_for_status()
            if resp.content:
                return resp.json()
            return {}
        except requests.ConnectionError:
            raise MediaBridgeError(
                f"Cannot reach Jellyfin at {self._jellyfin_url}. "
                "Is the media server (docker container puffpai-jellyfin) running?"
            )
        except requests.HTTPError as exc:
            raise MediaBridgeError(f"Jellyfin HTTP error: {exc}")
        except Exception as exc:
            raise MediaBridgeError(f"Jellyfin request failed: {exc}")

    # --- Connection check ---

    def is_connected(self) -> bool:
        """Check if Jellyfin is reachable."""
        if not self._enabled:
            return False
        try:
            self._jf_request("/System/Info/Public")
            return True
        except MediaBridgeError:
            return False

    def get_status(self) -> dict[str, Any]:
        """Return connection status and basic server info."""
        result = {
            "enabled": self._enabled,
            "jellyfin_url": self._jellyfin_url,
            "calibre_web_url": self._calibre_web_url,
            "connected": False,
            "server_name": "",
            "version": "",
        }
        if not self._enabled:
            return result
        try:
            info = self._jf_request("/System/Info/Public")
            result["connected"] = True
            result["server_name"] = info.get("ServerName", "")
            result["version"] = info.get("Version", "")
        except MediaBridgeError:
            pass
        return result

    # --- Music search ---

    def search_music(
        self, query: str = "", *, artist: str = "", album: str = "",
        genre: str = "", limit: int = 20,
    ) -> list[MediaItem]:
        """Search the Jellyfin music library."""
        cached = self._cache.get(f"music:{query}:{artist}:{album}:{genre}")
        if cached is not None:
            return cached

        params: dict[str, Any] = {
            "IncludeItemTypes": "Audio",
            "Recursive": "true",
            "Limit": limit,
            "Fields": "Path,Genres,Studios,Overview",
        }
        if query:
            params["SearchTerm"] = query
        if artist:
            params["ArtistIds"] = self._resolve_artist_id(artist)
        if album:
            params["SearchTerm"] = params.get("SearchTerm", "") + f" {album}"
        if genre:
            params["Genres"] = genre

        try:
            data = self._jf_request("/Items", params=params)
        except MediaBridgeError:
            return []

        items = [self._jf_audio_to_item(i) for i in data.get("Items", [])]
        self._cache.put(f"music:{query}:{artist}:{album}:{genre}", items)
        return items

    def _resolve_artist_id(self, artist_name: str) -> str:
        """Look up a Jellyfin artist ID by name."""
        try:
            data = self._jf_request("/Artists", params={
                "SearchTerm": artist_name, "Limit": 1,
            })
            items = data.get("Items", [])
            if items:
                return items[0]["Id"]
        except MediaBridgeError:
            pass
        return ""

    def _jf_audio_to_item(self, raw: dict) -> MediaItem:
        """Convert a Jellyfin Audio item to MediaItem."""
        artists = raw.get("Artists", [])
        return MediaItem(
            item_id=raw.get("Id", ""),
            title=raw.get("Name", "Unknown"),
            media_type="music",
            artist=artists[0] if artists else raw.get("AlbumArtist", ""),
            album=raw.get("Album", ""),
            genre=", ".join(raw.get("Genres", [])),
            year=raw.get("ProductionYear", 0) or 0,
            duration_seconds=int(raw.get("RunTimeTicks", 0) / 10_000_000),
            path=raw.get("Path", ""),
        )

    # --- Movie search ---

    def search_movies(
        self, query: str = "", *, genre: str = "", year: int = 0,
        limit: int = 20,
    ) -> list[MediaItem]:
        """Search the Jellyfin movie library."""
        params: dict[str, Any] = {
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "Limit": limit,
            "Fields": "Path,Genres,Overview",
        }
        if query:
            params["SearchTerm"] = query
        if genre:
            params["Genres"] = genre
        if year:
            params["Years"] = str(year)

        try:
            data = self._jf_request("/Items", params=params)
        except MediaBridgeError:
            return []

        return [self._jf_movie_to_item(i) for i in data.get("Items", [])]

    def _jf_movie_to_item(self, raw: dict) -> MediaItem:
        """Convert a Jellyfin Movie item to MediaItem."""
        return MediaItem(
            item_id=raw.get("Id", ""),
            title=raw.get("Name", "Unknown"),
            media_type="movie",
            genre=", ".join(raw.get("Genres", [])),
            year=raw.get("ProductionYear", 0) or 0,
            duration_seconds=int(raw.get("RunTimeTicks", 0) / 10_000_000),
            path=raw.get("Path", ""),
            extra={"overview": raw.get("Overview", "")},
        )

    # --- Book search (Calibre SQLite) ---

    def books_available(self) -> bool:
        """True when the real Calibre database file can be opened.

        Callers need to tell "the catalogue says no" apart from "there is no
        catalogue to ask" — answering a book question from the model instead of
        from Calibre is a fabrication.
        """
        return os.path.isfile(self._calibre_db)

    def search_books(
        self, query: str = "", *, author: str = "", subject: str = "",
        limit: int = 20,
    ) -> list[MediaItem]:
        """Search the Calibre book database directly via SQLite."""
        if not os.path.isfile(self._calibre_db):
            logger.warning("Calibre DB not found at %s", self._calibre_db)
            return []

        try:
            conn = sqlite3.connect(f"file:{self._calibre_db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Build query with JOIN on authors
            sql = """
                SELECT b.id, b.title, b.path,
                       GROUP_CONCAT(a.name, ', ') AS authors
                FROM books b
                LEFT JOIN books_authors_link bal ON b.id = bal.book
                LEFT JOIN authors a ON bal.author = a.id
                WHERE 1=1
            """
            params_list: list[str] = []

            if query:
                sql += " AND b.title LIKE ?"
                params_list.append(f"%{query}%")
            if author:
                sql += " AND a.name LIKE ?"
                params_list.append(f"%{author}%")

            sql += f" GROUP BY b.id ORDER BY b.timestamp DESC LIMIT {limit}"

            cur.execute(sql, params_list)
            rows = cur.fetchall()
            conn.close()

            return [
                MediaItem(
                    item_id=str(row["id"]),
                    title=row["title"],
                    media_type="book",
                    artist=row["authors"] or "",
                    path=row["path"] or "",
                )
                for row in rows
            ]
        except Exception as exc:
            logger.error("Calibre search error: %s", exc)
            return []

    # --- Playback control ---

    def play_music(self, item_id: str, session_id: str = "") -> str:
        """Start playback of a music item on a Jellyfin session.

        If session_id is empty, plays on the first active session.
        Returns a status message.

        Raises :class:`MediaBridgeError` before any HTTP call when *session_id*
        or *item_id* is not a safe Jellyfin id — both are reachable from a
        request body, and session_id becomes a URL path segment.
        """
        validate_media_id(item_id, kind="item_id")
        if not session_id:
            session_id = self._pick_session()
        validate_media_id(session_id, kind="session_id")

        # Jellyfin takes these as query params, not a JSON body — a body-only
        # request is answered with 400.
        self._jf_request(
            f"/Sessions/{quote_segment(session_id)}/Playing",
            method="POST",
            params={"ItemIds": item_id, "PlayCommand": "PlayNow"},
        )
        self._cache.invalidate()
        return f"Playing on session {session_id}"

    def _pick_session(self) -> str:
        """Most recently active *controllable* Jellyfin session, or a clear
        error if none exist. API-client sessions (SupportsRemoteControl=false,
        e.g. this backend's own connection) can never play anything, so they
        must not be picked even when they sort first."""
        sessions = self._get_sessions()
        controllable = [s for s in sessions if s.get("SupportsRemoteControl")]
        if not controllable:
            raise MediaBridgeError(
                "No active Jellyfin player to play on. Open the Media page on "
                "the dashboard (or any Jellyfin app) first, then ask again."
            )
        controllable.sort(key=lambda s: s.get("LastActivityDate") or "", reverse=True)
        # Upstream-supplied, but it still becomes a path segment: validate here
        # so a compromised/odd Jellyfin cannot steer our own request path.
        return validate_media_id(controllable[0].get("Id"), kind="session_id")

    def play_playlist(self, name_or_id: str, session_id: str = "", shuffle: bool = False) -> str:
        """Queue an entire playlist (PlayNow) on a Jellyfin session."""
        pl = self.resolve_playlist(name_or_id)
        tracks = self.playlist_tracks(pl.item_id, limit=700)
        if not tracks:
            raise MediaBridgeError(f"Playlist '{pl.title}' is empty.")
        if not session_id:
            session_id = self._pick_session()
        validate_media_id(session_id, kind="session_id")
        # Shuffle client-side (before the cap, so the 200-track queue samples
        # the whole playlist instead of always the first 200).
        if shuffle:
            tracks = tracks[:]
            random.shuffle(tracks)
        # Query params, not JSON body (see play_music). Track IDs are
        # comma-joined; cap the count so the URL stays under the server's
        # request-line limit (~8 KB ≈ 230 ids).
        queue = tracks[:200]
        self._jf_request(
            f"/Sessions/{quote_segment(session_id)}/Playing",
            method="POST",
            params={"ItemIds": ",".join(t.item_id for t in queue),
                    "PlayCommand": "PlayNow"},
        )
        self._cache.invalidate()
        mode = ", shuffled" if shuffle else ""
        return f"Playing playlist '{pl.title}' ({len(queue)} of {len(tracks)} tracks{mode})"

    # Spoken/LLM synonyms for the canonical playback actions.
    _ACTION_ALIASES = {
        "play": "resume", "unpause": "resume", "start": "resume",
        "continue": "resume", "skip": "next", "back": "previous",
        "prev": "previous",
    }

    def playback_control(self, session_id: str, action: str) -> str:
        """Control playback on a session. Actions: pause, resume, stop, next,
        previous (plus aliases like play/unpause/skip). Empty session_id picks
        the active controllable session automatically."""
        command_map = {
            "pause": "/Sessions/{sid}/Playing/Pause",
            "resume": "/Sessions/{sid}/Playing/Unpause",
            "stop": "/Sessions/{sid}/Playing/Stop",
            "next": "/Sessions/{sid}/Playing/NextTrack",
            "previous": "/Sessions/{sid}/Playing/PreviousTrack",
        }
        action = action.lower().strip()
        action = self._ACTION_ALIASES.get(action, action)
        path_template = command_map.get(action)
        if not path_template:
            raise MediaBridgeError(
                f"Unknown action {_echo(action)}. Use: pause, resume, stop, next, previous"
            )
        if not session_id:
            session_id = self._pick_session()
        validate_media_id(session_id, kind="session_id")

        self._jf_request(path_template.format(sid=quote_segment(session_id)),
                         method="POST")
        self._cache.invalidate()
        return f"Executed {action} on session {session_id}"

    def set_volume(self, session_id: str, volume: int) -> str:
        """Set volume (0-100) on a Jellyfin session."""
        validate_media_id(session_id, kind="session_id")
        volume = max(0, min(100, volume))
        self._jf_request(
            f"/Sessions/{quote_segment(session_id)}/Command",
            method="POST",
            data={"Name": "SetVolume", "Arguments": {"Volume": str(volume)}},
        )
        return f"Volume set to {volume} on session {session_id}"

    # --- Now playing ---

    def now_playing(self) -> list[PlaybackSession]:
        """Return all active playback sessions."""
        sessions = self._get_sessions()
        result = []
        for s in sessions:
            np = s.get("NowPlayingItem")
            now = None
            if np:
                media_type = "music" if np.get("Type") == "Audio" else "movie"
                now = MediaItem(
                    item_id=np.get("Id", ""),
                    title=np.get("Name", ""),
                    media_type=media_type,
                    artist=", ".join(np.get("Artists", [])) if np.get("Artists") else "",
                    album=np.get("Album", ""),
                    duration_seconds=int((np.get("RunTimeTicks", 0) or 0) / 10_000_000),
                    extra={"album_id": np.get("AlbumId", "")},
                )

            play_state = s.get("PlayState", {})
            pos_ticks = play_state.get("PositionTicks", 0) or 0

            result.append(PlaybackSession(
                session_id=s.get("Id", ""),
                client_name=s.get("Client", ""),
                device_name=s.get("DeviceName", ""),
                user_name=s.get("UserName", ""),
                now_playing=now,
                is_paused=play_state.get("IsPaused", False),
                position_seconds=int(pos_ticks / 10_000_000),
                volume_level=play_state.get("VolumeLevel"),
            ))
        return result

    def _get_sessions(self) -> list[dict]:
        """Get active Jellyfin sessions (with NowPlayingItem or controllable)."""
        cached = self._cache.get("sessions")
        if cached is not None:
            return cached
        try:
            sessions = self._jf_request("/Sessions", params={
                "ControllableByUserId": "",
            })
            if isinstance(sessions, list):
                self._cache.put("sessions", sessions)
                return sessions
        except MediaBridgeError:
            pass
        return []

    # --- User resolution ---

    def _get_user_id(self) -> str:
        """Resolve the Jellyfin user ID used to scope playlist/favorite ops.

        API keys are not tied to a user, but the playlist and favorites
        endpoints require one. Prefers the first administrator account.
        """
        if self._user_id:
            return self._user_id
        data = self._jf_request("/Users")
        if isinstance(data, list) and data:
            admins = [u for u in data if u.get("Policy", {}).get("IsAdministrator")]
            self._user_id = (admins[0] if admins else data[0]).get("Id", "")
        if not self._user_id:
            raise MediaBridgeError("No Jellyfin users found for playlist operations.")
        return self._user_id

    # --- Playlists ---

    def list_playlists(self) -> list[MediaItem]:
        """Return all playlists with track counts."""
        cached = self._cache.get("playlists")
        if cached is not None:
            return cached
        data = self._jf_request("/Items", params={
            "IncludeItemTypes": "Playlist",
            "Recursive": "true",
            "UserId": self._get_user_id(),
            "Fields": "ChildCount",
        })
        items = [
            MediaItem(
                item_id=raw.get("Id", ""),
                title=raw.get("Name", "Unknown"),
                media_type="playlist",
                extra={"track_count": raw.get("ChildCount", 0) or 0},
            )
            for raw in data.get("Items", [])
        ]
        self._cache.put("playlists", items)
        return items

    @staticmethod
    def _fuzzy_key(text: str) -> str:
        """Normalize a name for forgiving comparison: lowercase, drop
        possessive 's and punctuation, collapse whitespace, and singularize
        tokens — so the spoken \"alex's thumbs up\" matches both
        \"Alex Thumbs Up\" and \"Alexs Thumbs Up\"."""
        text = re.sub(r"'s\b", "", text.lower())
        text = re.sub(r"[^a-z0-9]+", " ", text)
        tokens = [
            t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t
            for t in text.split()
        ]
        return " ".join(tokens)

    @staticmethod
    def _prefer_nonempty(cands: list[MediaItem]) -> list[MediaItem]:
        """Among matching playlists, drop empty ones if any have tracks
        (empty duplicates left over from failed imports shadow real ones)."""
        nonempty = [p for p in cands if (p.extra.get("track_count") or 0) > 0]
        return nonempty or cands

    def resolve_playlist(self, name_or_id: str) -> MediaItem:
        """Find a playlist by ID, ID prefix, or (partial/fuzzy) name."""
        needle = name_or_id.strip()
        if not needle:
            raise MediaBridgeError("Empty playlist name.")
        playlists = self.list_playlists()
        for p in playlists:
            if p.item_id == needle or (len(needle) >= 8 and p.item_id.startswith(needle)):
                return p
        low = needle.lower()
        exact = self._prefer_nonempty([p for p in playlists if p.title.lower() == low])
        if exact:
            return exact[0]
        fuzzy_needle = self._fuzzy_key(needle)
        if fuzzy_needle:
            fuzzy_exact = self._prefer_nonempty(
                [p for p in playlists if self._fuzzy_key(p.title) == fuzzy_needle]
            )
            if len(fuzzy_exact) == 1:
                return fuzzy_exact[0]
            if fuzzy_exact:
                names = ", ".join(p.title for p in fuzzy_exact[:8])
                raise MediaBridgeError(f"Multiple playlists match '{needle}': {names}")
        partial = [p for p in playlists if low in p.title.lower()]
        if not partial and fuzzy_needle:
            # Fuzzy substring, then all-words-present (voice transcripts often
            # reorder or drop punctuation: "thumbs up alex" etc.)
            partial = [p for p in playlists if fuzzy_needle in self._fuzzy_key(p.title)]
            if not partial:
                tokens = set(fuzzy_needle.split())
                partial = [p for p in playlists if tokens <= set(self._fuzzy_key(p.title).split())]
        partial = self._prefer_nonempty(partial)
        if len(partial) == 1:
            return partial[0]
        if partial:
            names = ", ".join(p.title for p in partial[:8])
            raise MediaBridgeError(f"Multiple playlists match '{needle}': {names}")
        names = ", ".join(p.title for p in playlists[:15])
        raise MediaBridgeError(f"No playlist matching '{needle}'. Available: {names}")

    def playlist_tracks(self, name_or_id: str, limit: int = 200) -> list[MediaItem]:
        """Return the tracks of a playlist (entry IDs kept in extra['entry_id'])."""
        pl = self.resolve_playlist(name_or_id)
        # pl.item_id comes from Jellyfin's own /Items listing, not the caller —
        # validated anyway so the path segment is provably safe at the sink.
        validate_media_id(pl.item_id, kind="playlist id")
        data = self._jf_request(f"/Playlists/{quote_segment(pl.item_id)}/Items", params={
            "UserId": self._get_user_id(),
            "Limit": limit,
        })
        items = []
        for raw in data.get("Items", []):
            item = self._jf_audio_to_item(raw)
            item.extra["entry_id"] = raw.get("PlaylistItemId", "")
            items.append(item)
        return items

    def create_playlist(self, name: str, item_ids: list[str] | None = None) -> str:
        """Create a new audio playlist; returns its ID."""
        data = self._jf_request("/Playlists", method="POST", data={
            "Name": name,
            "UserId": self._get_user_id(),
            "Ids": item_ids or [],
            "MediaType": "Audio",
        })
        self._cache.invalidate("playlists")
        return data.get("Id", "") if isinstance(data, dict) else ""

    def playlist_add(self, name_or_id: str, item_ids: list[str]) -> str:
        """Add tracks to a playlist."""
        pl = self.resolve_playlist(name_or_id)
        validate_media_id(pl.item_id, kind="playlist id")
        for iid in item_ids:
            validate_media_id(iid, kind="item_id")
        self._jf_request(
            f"/Playlists/{quote_segment(pl.item_id)}/Items",
            method="POST",
            params={"Ids": ",".join(item_ids), "UserId": self._get_user_id()},
        )
        self._cache.invalidate("playlists")
        return f"Added {len(item_ids)} track(s) to '{pl.title}'"

    def playlist_remove(self, name_or_id: str, entry_ids: list[str]) -> str:
        """Remove entries (PlaylistItemIds) from a playlist."""
        pl = self.resolve_playlist(name_or_id)
        validate_media_id(pl.item_id, kind="playlist id")
        for eid in entry_ids:
            validate_media_id(eid, kind="entry_id")
        self._jf_request(
            f"/Playlists/{quote_segment(pl.item_id)}/Items",
            method="DELETE",
            params={"EntryIds": ",".join(entry_ids)},
        )
        self._cache.invalidate("playlists")
        return f"Removed {len(entry_ids)} track(s) from '{pl.title}'"

    # --- Artists / taste playlists ---

    def list_artists(self, limit: int = 500, query: str = "") -> list[dict]:
        """The music library's artists: ``[{id, name, album_count}]``.

        ``/Artists`` is scoped by UserId like every other library read here.
        Ids that would not survive :func:`validate_media_id` are dropped rather
        than carried to a later URL sink. Never raises — an unreachable or
        disabled Jellyfin is an empty library, not an exception.
        """
        params: dict[str, Any] = {
            "Recursive": "true",
            "Limit": limit,
            "Fields": "ChildCount",
        }
        if query:
            params["SearchTerm"] = query
        try:
            params["UserId"] = self._get_user_id()
            data = self._jf_request("/Artists", params=params)
        except MediaBridgeError:
            return []
        artists = []
        rows = data.get("Items") or [] if isinstance(data, dict) else []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("Name") or "").strip()
            item_id = str(raw.get("Id") or "")
            if not name or not is_valid_media_id(item_id):
                continue
            artists.append({
                "id": item_id,
                "name": name,
                "album_count": raw.get("AlbumCount")
                or raw.get("ChildCount") or 0,
            })
        return artists

    def instant_mix(self, item_id: str, limit: int = 10) -> list[MediaItem]:
        """Jellyfin's own "more like this" tracks for an item (artist id works).

        This is the half of a taste playlist the household did not name: music
        the library already knows sits next to what they asked for. A server
        without the feature (or a failing call) returns nothing — never fatal.
        """
        try:
            validate_media_id(item_id, kind="item_id")
            data = self._jf_request(
                f"/Items/{quote_segment(item_id)}/InstantMix",
                params={
                    "UserId": self._get_user_id(),
                    "Limit": limit,
                    "Fields": "Path,Genres",
                },
            )
        except MediaBridgeError:
            return []
        # Total, including on a junk response shape: a Jellyfin that answers a
        # JSON list (or rows that are not objects) must be an empty mix, not an
        # AttributeError thrown out of build_taste_playlist.
        rows = data.get("Items") or [] if isinstance(data, dict) else []
        items = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            try:
                items.append(self._jf_audio_to_item(raw))
            except Exception as exc:            # pragma: no cover - defensive
                logger.debug("instant mix: unreadable row: %s", exc)
        return items

    def _free_playlist_name(self, name: str) -> str:
        """*name*, or ``name 2`` / ``name 3`` … when it is already taken.

        Appending to somebody's existing playlist because the new one happens
        to share its title is a destructive surprise; a suffix is not.
        """
        try:
            taken = {p.title.strip().lower() for p in self.list_playlists()}
        except MediaBridgeError:
            return name
        candidate, n = name, 1
        while candidate.strip().lower() in taken and n < 50:
            n += 1
            candidate = f"{name} {n}"
        return candidate

    def build_taste_playlist(
        self, name: str, artists: list[str], *, per_artist: int = 8,
        similar_per_artist: int = 4, max_tracks: int = 120,
        shuffle: bool = True,
    ) -> dict:
        """Build one playlist out of a list of artist names. Never raises.

        Each name is resolved against the real library — case-insensitive
        first, then difflib at :data:`_ARTIST_MATCH_RATIO`, which is high on
        purpose: a playlist quietly built from the wrong artist is worse than
        an honest "not in your library". Matched artists contribute their own
        tracks plus InstantMix neighbours that are not already represented,
        and the buckets are interleaved so one artist cannot fill the top.

        Returns ``{ok, playlist_id, name, track_count, matched, missing,
        similar_added, error}``. With ZERO matches nothing is created.
        """
        wanted = [str(a).strip() for a in (artists or []) if str(a).strip()]
        result = {
            "ok": False, "playlist_id": "", "name": name, "track_count": 0,
            "matched": [], "missing": [], "similar_added": 0, "error": "",
            "partial": False,
        }
        if not wanted:
            result["error"] = "no artist names were given"
            return result
        try:
            library = self.list_artists()
        except Exception as exc:                # pragma: no cover - defensive
            logger.debug("taste playlist: artist listing failed: %s", exc)
            library = []
        by_key = {_artist_key(a["name"]): a for a in library
                  if isinstance(a, dict) and a.get("name")}
        keys = list(by_key)

        buckets: list[list[str]] = []
        seen_ids: set[str] = set()
        claimed: set[str] = set()       # artist names already represented
        for raw_name in wanted:
            key = _artist_key(raw_name)
            hit = by_key.get(key)
            if hit is None and keys:
                close = difflib.get_close_matches(
                    key, keys, n=1, cutoff=_ARTIST_MATCH_RATIO)
                hit = by_key.get(close[0]) if close else None
            if hit is None:
                result["missing"].append(raw_name)
                continue
            result["matched"].append(hit["name"])
            claimed.add(_artist_key(hit["name"]))
            bucket = []
            try:
                tracks = self.search_music(artist=hit["name"],
                                           limit=max(1, per_artist))
            except Exception as exc:            # pragma: no cover - defensive
                logger.debug("taste playlist: search failed for %s: %s",
                             hit["name"], exc)
                tracks = []
            for track in tracks[:per_artist]:
                if track.item_id and track.item_id not in seen_ids:
                    seen_ids.add(track.item_id)
                    bucket.append(track.item_id)
            buckets.append(bucket)

        if not result["matched"]:
            result["error"] = "none of those artists are in the library"
            return result

        # The "matches my taste" half, added only after every named artist has
        # been served, and never crediting an artist already on the list.
        for hit_name in list(result["matched"]):
            if similar_per_artist <= 0:
                break
            artist = by_key.get(_artist_key(hit_name))
            if not artist:
                continue
            similar = []
            try:
                neighbours = self.instant_mix(
                    artist["id"], limit=max(1, similar_per_artist * 4))
            except Exception as exc:            # "never raises" has to be true
                logger.debug("taste playlist: instant mix failed for %s: %s",
                             hit_name, exc)
                neighbours = []
            for track in neighbours:
                if len(similar) >= similar_per_artist:
                    break
                if not track.item_id or track.item_id in seen_ids:
                    continue
                if _artist_key(track.artist) in claimed:
                    continue
                seen_ids.add(track.item_id)
                similar.append(track.item_id)
            result["similar_added"] += len(similar)
            buckets.append(similar)

        # Interleave FIRST: it decides which tracks survive the cap, so a
        # 40-track artist cannot crowd out the last name on the list. The
        # shuffle then only reorders what was kept.
        ordered = _interleave(buckets)[:max(1, max_tracks)]
        if shuffle and len(ordered) > 2:
            random.shuffle(ordered)
        if not ordered:
            result["error"] = "those artists have no tracks in the library"
            return result

        final_name = self._free_playlist_name(name)
        result["name"] = final_name
        try:
            playlist_id = self.create_playlist(final_name, ordered[:100])
            if not playlist_id:
                result["error"] = "Jellyfin did not return a playlist id"
                return result
        except Exception as exc:
            result["error"] = str(exc)
            return result
        # From here a playlist EXISTS in Jellyfin. Reporting ok=False for a
        # failed later chunk left an orphan behind and invited a retry that
        # made "Road Trip 2", so a half-filled
        # playlist is reported as exactly that.
        added = len(ordered[:100])
        for start in range(100, len(ordered), 100):
            chunk = ordered[start:start + 100]
            try:
                self.playlist_add(playlist_id, chunk)
            except Exception as exc:
                result["partial"] = True
                result["error"] = str(exc)
                logger.warning("taste playlist %r: %d of %d tracks saved: %s",
                               final_name, added, len(ordered), exc)
                break
            added += len(chunk)
        result["ok"] = True
        result["playlist_id"] = playlist_id
        result["track_count"] = added
        return result

    # --- Favorites / preferences ---

    def set_favorite(self, item_id: str, favorite: bool = True) -> str:
        """Mark or unmark an item as a favorite."""
        validate_media_id(item_id, kind="item_id")
        self._jf_request(
            f"/UserFavoriteItems/{quote_segment(item_id)}",
            method="POST" if favorite else "DELETE",
            params={"userId": self._get_user_id()},
        )
        return "Marked as favorite" if favorite else "Removed from favorites"

    def list_favorites(self, limit: int = 50) -> list[MediaItem]:
        """Return favorite music tracks."""
        data = self._jf_request("/Items", params={
            "IncludeItemTypes": "Audio",
            "Filters": "IsFavorite",
            "Recursive": "true",
            "UserId": self._get_user_id(),
            "Limit": limit,
        })
        return [self._jf_audio_to_item(i) for i in data.get("Items", [])]

    # --- Library management ---

    def library_stats(self) -> dict[str, Any]:
        """Get counts of music, movies, and books."""
        stats: dict[str, Any] = {
            "music_tracks": 0, "music_albums": 0, "music_artists": 0,
            "movies": 0, "books": 0,
        }

        try:
            # Music tracks
            data = self._jf_request("/Items/Counts")
            stats["music_tracks"] = data.get("SongCount", 0)
            stats["music_albums"] = data.get("AlbumCount", 0)
            stats["music_artists"] = data.get("ArtistCount", 0)
            stats["movies"] = data.get("MovieCount", 0)
        except MediaBridgeError:
            pass

        # Books from Calibre
        if os.path.isfile(self._calibre_db):
            try:
                conn = sqlite3.connect(f"file:{self._calibre_db}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM books")
                stats["books"] = cur.fetchone()[0]
                conn.close()
            except Exception:
                pass

        return stats

    def trigger_scan(self, library: str = "") -> str:
        """Trigger a Jellyfin library rescan."""
        self._jf_request("/Library/Refresh", method="POST")
        self._cache.invalidate()
        return "Library scan triggered"


# ---------------------------------------------------------------------------
# Taste playlists: reading a list of artists a person actually wrote down
# ---------------------------------------------------------------------------
#
# WHY. A person can send a screenshot of their streaming stations and ask
# for a Jellyfin playlist "like that". The text that comes
# back from OCR is not a list of artists: it is menu chrome, row numbers and
# single-letter junk around the names. Parsing it is deterministic work, so it
# is done here rather than asked of a 3B model.

#: Below this difflib ratio an artist is NOT considered matched. High on
#: purpose — see build_taste_playlist.
_ARTIST_MATCH_RATIO = 0.82

#: Streaming-UI words that sit in such a list and name no artist.
_NOT_ARTISTS = frozenset({
    "quickmix", "quick mix", "shuffle", "thumbprint", "my stations",
    "stations", "station", "radio", "collection", "my collection",
    "now playing", "sort", "sort by", "search", "browse", "recent",
    "recently played", "back", "home", "settings", "podcasts", "playlists",
    "for you", "made for you", "add music", "create station", "more",
})

#: Leading list chrome: "0)", "1.", "-", "*", "•". The punctuation after the
#: number is required, so the band "3 Doors Down" keeps its name.
_LIST_CHROME_RE = re.compile(r"^\W*\d+\s*[\).:\-]\s*|^[\-\*•·]+\s*")

#: A trailing "... Radio" / "... Station(s)" is the row's label, not the name.
_ROW_SUFFIX_RE = re.compile(r"\s+(?:radio|stations?)\s*$", re.IGNORECASE)


def _artist_key(name: str) -> str:
    """Comparison key for artist names: lowercase, letters and digits only."""
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def _interleave(buckets: list[list[str]]) -> list[str]:
    """Round-robin the buckets so no single artist clumps at the top."""
    out: list[str] = []
    for index in range(max((len(b) for b in buckets), default=0)):
        for bucket in buckets:
            if index < len(bucket):
                out.append(bucket[index])
    return out


def parse_artist_names(text: str, limit: int = 40) -> list[str]:
    """Candidate artist names out of a messy pasted/OCR'd list. Pure.

    Strips list numbering and bullets, drops the OCR's stray single characters
    ("0) q Coldplay Radio"), removes the row label, and refuses anything that
    is UI furniture or is mostly not letters. Order is preserved because the
    order a person wrote their favourites in is information.
    """
    names: list[str] = []
    seen: set[str] = set()
    for raw_line in re.split(r"[\n\r,;•·|]+", str(text or "")):
        line = _LIST_CHROME_RE.sub("", raw_line.strip())
        # OCR leaves a single character where the row icon was. "a" and "I"
        # are real words in real band names, so only other singletons go.
        tokens = line.split()
        while (tokens and len(tokens[0]) == 1 and tokens[0].isalpha()
               and tokens[0].lower() not in "ai"):
            tokens.pop(0)          # a lone digit may be the name ("3 Doors Down")
        line = " ".join(tokens).strip(" \t-–—_'\"“”’")
        # The UI word is checked with and without the row label, so both
        # "My Stations" and "Thumbprint Radio" are recognised as chrome.
        if line.lower() in _NOT_ARTISTS:
            continue
        line = _ROW_SUFFIX_RE.sub("", line).strip(" \t-–—_'\"“”’")
        if len(line) < 2 or line.lower() in _NOT_ARTISTS:
            continue
        letters = sum(c.isalpha() for c in line)
        if letters < 2 or letters < len(line.replace(" ", "")) * 0.6:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(line)
        if len(names) >= limit:
            break
    return names


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: MediaBridge | None = None
_lock = threading.Lock()


def get_media_bridge(*, _force_reload: bool = False) -> MediaBridge:
    """Return the global MediaBridge singleton."""
    global _instance
    if _instance is None or _force_reload:
        with _lock:
            if _instance is None or _force_reload:
                _instance = MediaBridge()
    return _instance
