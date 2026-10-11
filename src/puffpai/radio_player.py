"""Server-side internet-radio playback.

Played only through the kiosk Chromium <audio> element (via the poll-based
KioskMediaBridge), internet radio would make sound only while the dashboard is
up. This plays the stream on the appliance itself, a tiny mpv container
(image ``puffpai-radio``) that outputs to PulseAudio, the SAME sink Puff's
voice uses, so it is always heard and Pulse mixes it with speech. Mirrors the
server-side pattern of spotify_control (it uses ALSA /dev/snd;
radio uses the Pulse socket, which avoids single-card exclusive-access contention).

Control is intentionally trivial: one named container. ``play`` (re)starts it,
``stop`` removes it. The now-playing NAME is tracked by the caller in the
mediahub app_data (so the dashboard widget still shows it); this module only
owns the audio process.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from urllib.parse import urlsplit

_CONTAINER = "puffpai-radio"
_IMAGE = os.environ.get("PUFFPAI_RADIO_IMAGE", "puffpai-radio:latest")
_PULSE_SOCK = os.environ.get(
    "PUFFPAI_PULSE_SOCK", "/run/user/1000/pulse/native")
_PULSE_COOKIE = os.environ.get(
    "PUFFPAI_PULSE_COOKIE", os.path.expanduser("~/.config/pulse/cookie"))
_DOCKER_TIMEOUT = 20


def _docker(args: list[str], timeout: int = _DOCKER_TIMEOUT):
    return subprocess.run(["docker", *args], capture_output=True,
                          text=True, timeout=timeout)


def is_playing() -> bool:
    """True if the radio container is currently running."""
    try:
        r = _docker(["ps", "-q", "-f", f"name=^{_CONTAINER}$"], timeout=8)
        return bool(r.stdout.strip())
    except Exception:
        return False


# Common mpv flags. We override the image ENTRYPOINT and pass these explicitly so
# one image serves both live radio and a Jellyfin file queue. --cache buffers the
# network; the loopback Pulse socket is world-writable so no cookie is needed.
#
# --ytdl=no and --load-scripts=no are SECURITY flags, not preferences: mpv's
# built-in ytdl_hook hands any URL it likes the look of to an external
# youtube-dl/yt-dlp binary, and script auto-loading is a second way to run code
# chosen by something other than us. Neither has any part in playing a radio
# stream. --no-config already refuses the config dir.
_MPV = ["--no-video", "--no-terminal", "--no-config", "--ao=pulse",
        "--cache=yes", "--ytdl=no", "--load-scripts=no"]


def _spawn(mpv_args: list[str], header: str | None = None):
    stop()  # only ever one appliance-audio process
    hdr = [f"--http-header-fields={header}"] if header else []
    return _docker([
        "run", "-d", "--rm", "--name", _CONTAINER,
        "--network", "host", "--user", "1000:1000",
        "-e", f"PULSE_SERVER=unix:{_PULSE_SOCK}",
        "-v", f"{_PULSE_SOCK}:{_PULSE_SOCK}",
        # "--" ends mpv's option parsing: everything after it is a URL to play,
        # never a flag. Without it a catalog row whose "stream" begins with a
        # dash ("--script=...", "--ao=...") is an OPTION to mpv, chosen by a
        # third-party database. Cheap, and it covers both callers.
        "--entrypoint", "mpv", _IMAGE, *_MPV, *hdr, "--", *mpv_args,
    ])


#: Schemes a station row may use. mpv also understands file://, smb://, av://,
#: lavfi://, memory://, edl:// and more — every one of them a way for a catalog
#: row to name something that is not a stream.
_STREAM_SCHEMES = ("http", "https")

#: Names that mean "this house" even before they are resolved.
_INDOOR_SUFFIXES = (".local", ".localhost", ".internal", ".home", ".lan")


def _indoor_ip(ip) -> bool:
    """True for anything that is not a plain public address.

    Delegates to the one shared classifier, which names CGNAT
    (100.64.0.0/10, also Tailscale), unique-local, documentation and
    benchmark ranges explicitly and unwraps IPv4-mapped, 6to4 and Teredo
    forms. Anything it cannot classify counts as indoor: fail closed.
    """
    from puffpai.security import netaddr
    try:
        return netaddr.classify(str(ip)) is not None
    except Exception:
        return True


#: A host label that is a NUMBER, in any of the three spellings inet_aton (and
#: therefore curl, mpv and most C resolvers) accepts: decimal, octal via a
#: leading zero, hex via 0x. Ordinary hostnames never match this — "1.fm" has
#: an "fm", "abc.de" has letters outside the hex alphabet in a non-0x label —
#: so a host whose labels ALL match is a numeric literal pretending not to be.
_NUMERIC_LABEL = re.compile(r"^(0[xX][0-9a-fA-F]+|[0-9]+)$")


def _numeric_host(host: str):
    """``(is_numeric, ip_or_None)`` for an inet_aton-style host.

    ``(False, None)`` is an ordinary hostname. ``(True, ip)`` is a literal we
    could decode — check it like any other address. ``(True, None)`` is
    numeric-looking but NOT decodable (too many parts, a part out of range, an
    empty label): refuse it rather than hand an ambiguous string to a resolver
    that may decode it differently than we did.
    """
    labels = host.split(".")
    if not labels or not all(_NUMERIC_LABEL.match(lb) for lb in labels):
        return False, None
    if len(labels) > 4:
        return True, None
    try:
        parts = [int(lb, 16) if lb[:2].lower() == "0x"
                 else int(lb, 8) if len(lb) > 1 and lb[0] == "0"
                 else int(lb, 10) for lb in labels]
    except ValueError:
        return True, None
    # inet_aton: the LAST part fills every byte the earlier parts left over.
    if any(p > 0xFF for p in parts[:-1]) or parts[-1] >= 256 ** (4 - (len(parts) - 1)):
        return True, None
    value = parts[-1]
    for index, part in enumerate(parts[:-1]):
        value += part << (8 * (3 - index))
    try:
        return True, ipaddress.ip_address(value)
    except ValueError:
        return True, None


def _resolve(host: str):
    """Addresses *host* answers with. Split out so tests can drive it."""
    return [info[4][0] for info in
            socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def stream_url_refusal(url, *, resolve_host: bool = True) -> str | None:
    """Why *url* may not be handed to the player, or None when it may.

    Station stream URLs come from THIRD-PARTY catalogs (radio-browser, TuneIn)
    and from favorites saved out of them, and they are handed to a media player
    running with ``--network host``. So the row a stranger's database returned
    decides what this appliance connects to unless something says no first.
    Three things are refused here, at the one place every radio path passes:

    * a scheme that is not http/https — mpv happily opens ``file:///etc/...``,
      ``smb://``, ``av://`` and friends;
    * control characters, which have no place in a URL and every place in an
      injection;
    * a target inside the house — loopback, RFC1918, link-local, or a name that
      says so. A GET the household never asked for, aimed at this box's own API
      or at a neighbour's printer, is the blind half of an SSRF and the player
      is a perfectly good confused deputy for it. "Inside the house" covers the
      inet_aton spellings of an address (``2130706433``, ``0x7f.1``,
      ``0177.0.0.1``, ``127.1``), which ``ipaddress`` alone does not parse and
      which would otherwise slip through as if they were hostnames;
    * credentials in the URL (``http://user:pw@host/``) — they would land in
      the player's argv and in any log that ever prints the command, and a
      userinfo field is the classic way to make a URL parser and a resolver
      disagree about which host is the host.

    A name that does not resolve is ALLOWED through: it reaches nothing, so
    there is nothing to refuse, and refusing would turn every DNS hiccup into a
    security message. A name that resolves public and is re-resolved private by
    mpv (DNS rebinding) is not covered — mpv does its own lookup and there is
    no way to pin it from here.

    This does NOT apply to :func:`play_urls`, whose URLs are built by us from
    the configured Jellyfin base: that path legitimately plays LAN addresses,
    and its URLs are not attacker-chosen. The distinction is the source, not
    the address.

    ``resolve_host=False`` is the STORAGE-TIME mode: every
    check above still runs except the DNS lookup, so saving or importing forty
    favorites costs zero lookups and cannot be used to make this box resolve a
    list of names someone else chose. Nothing is weakened by it, because the
    full check — this function with its default — still runs at PLAY time in
    :func:`play`, which is the only place a URL reaches mpv.
    """
    if not isinstance(url, str) or not url.strip():
        return "no stream url"
    raw = url.strip()
    if any(ch in raw for ch in "\r\n\t\x00"):
        return "the stream address has control characters in it"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "the stream address could not be read"
    scheme = (parts.scheme or "").lower()
    if scheme not in _STREAM_SCHEMES:
        return f"{scheme or 'that'} is not a stream address"
    if "@" in (parts.netloc or ""):
        return "that stream address carries a username or password"
    try:
        host = (parts.hostname or "").strip()
    except ValueError:
        return "the stream address could not be read"
    if not host:
        return "the stream address has no host"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _indoor_ip(ip):
            return "that stream address points inside this house"
        return None

    low = host.lower().rstrip(".")
    if low == "localhost" or low.endswith(_INDOOR_SUFFIXES):
        return "that stream address points inside this house"

    # A numeric literal in any spelling: decode it ourselves rather than trust
    # the resolver to agree with us about what it means.
    numeric, literal = _numeric_host(low)
    if numeric:
        if literal is None:
            return "the stream address could not be read"
        return ("that stream address points inside this house"
                if _indoor_ip(literal) else None)
    if not resolve_host:
        return None
    try:
        addresses = _resolve(low)
    except Exception:
        return None  # resolves to nothing, so it reaches nothing
    for addr in addresses:
        try:
            if _indoor_ip(ipaddress.ip_address(addr.split("%")[0])):
                return "that stream address points inside this house"
        except ValueError:
            continue
    return None


#: When to re-check that mpv is still alive after ``docker run -d`` returned.
#: A dead stream (404, DNS gone, TLS refused) makes mpv exit within a second or
#: two and ``--rm`` takes the container with it, so these three probes are the
#: difference between "Playing X on the speakers" and actual sound. Bounded at
#: ~3 s because this runs inside a chat/voice turn.
_ALIVE_PROBE_GAPS = (0.6, 0.8, 1.4)


def play(url: str, verify: bool = True) -> tuple[bool, str]:
    """(Re)start server-side playback of a single stream URL (live radio).

    ``docker run -d`` returning 0 means the CONTAINER started, not that the
    stream did, so without a check a dead station becomes "Playing X on the
    speakers" followed by silence. With ``verify`` on, the
    container is re-checked for ~3 s; mpv exits on an unplayable stream and
    ``--rm`` removes it, so a disappeared container is a failed stream and is
    reported as one, letting the caller try the NEXT candidate.

    The exit reason is NOT recoverable: ``--rm`` deletes the container (and its
    logs) the moment mpv exits, and keeping it around to read them would leave
    dead containers behind on every bad station. The caller gets "the stream
    didn't start" and moves on, which is what it would do with the detail.
    """
    refusal = stream_url_refusal(url)
    if refusal:
        return False, refusal
    url = url.strip()
    try:
        r = _spawn([url])
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "").strip()[:300]
        if not verify:
            return True, ""
        import time
        for gap in _ALIVE_PROBE_GAPS:
            time.sleep(gap)
            if not is_playing():
                return False, "the stream didn't start (player exited)"
        return True, ""
    except Exception as exc:
        return False, str(exc)[:300]


def play_urls(urls, header: str | None = None) -> tuple[bool, str]:
    """Play a queue of stream URLs in order (e.g. a Jellyfin playlist). An
    optional HTTP header (e.g. 'X-Emby-Token: <key>') authenticates them, so the
    key stays out of the URLs. Same single container as radio (mutually
    exclusive: starting one stops the other)."""
    urls = [u for u in (urls or []) if u]
    if not urls:
        return False, "no urls"
    try:
        r = _spawn(list(urls), header=header)
        return r.returncode == 0, (r.stderr or r.stdout or "").strip()[:300]
    except Exception as exc:
        return False, str(exc)[:300]


def stop() -> bool:
    """Stop + remove the radio container (no-op if not running)."""
    try:
        _docker(["rm", "-f", _CONTAINER], timeout=12)
        return True
    except Exception:
        return False
