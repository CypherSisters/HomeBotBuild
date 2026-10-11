"""Kiosk TV, hardware decoded ("Watch smooth").

The kiosk's snap Chromium runs with GL off, so Live TV in the Jellyfin frame
is decoded and scaled to 2560x1440 on the CPU (about two cores, choppy). This plays a channel in a native full-screen window on the
kiosk display instead, decoded by a Jetson's video engine (NVDEC) and drawn by
the GPU, in one container (``puffpai-tvplayer``, built from
docker/tvplayer/Dockerfile). Same control pattern as :mod:`radio_player`: one
named container, ``play`` (re)starts it, ``stop`` removes it.

Two kinds of source:

* a Jellyfin Live TV channel (home channels and the free list Jellyfin
  carries), by its 32-hex channel id. The container asks Jellyfin for its own
  TS remux on 127.0.0.1:8096. The Jellyfin key is NOT passed in: the box's
  0600 media_server_config.json is mounted read-only and read inside the
  container, so the key never appears in argv, ``docker inspect`` or logs;
* a "more free channels (ads)" entry from the set-aside list
  (``iptv-unreliable.m3u``: Pluto and others whose ad breaks reset
  timestamps, which Jellyfin cannot play through). Picked by a short id this
  module derives from the list; the caller never supplies a URL. The player's
  ffmpeg evens out the timestamp resets. Pluto entries play by channel id:
  the player starts its own anonymous Pluto session (no account) inside the
  container, because the session token names the house's IP and rough
  location and must not sit in a file, an argument or a log.

  These channels come from the internet, so they play only while the
  ``tv_free_channels`` Bridge switch is open. :func:`play_free` asks the
  switch before the container starts, refuses while it is closed, and
  records each start in the egress ledger. It checks only the channel's own
  address (https, not inside the house). The container then fetches the
  playlist's redirects and segments itself, with no address or https check
  and no ledger line, so a playlist could point it at a device on the
  house network; the cast relay checks every hop, this player does not.
  Closing the switch stops new plays; Stop ends one that is already running.

The container runs with only nine device nodes, no capabilities, no
privilege gain and a read-only root (see ``_devices`` and ``_base_args``).

Ending: a tap on the picture, ``stop``, the stream ending or stalling all end
the container, and its window with it, so the dashboard is what shows next.
The window is only mapped once the first frame is decoded, so a slow start
leaves the dashboard up, never a black screen.

The NVIDIA pieces (decoder plugin, Tegra libraries, devices) are bind-mounted
from the host, read-only; no nvidia container runtime is needed.

This player needs a Jetson (Tegra decoder libraries and the host1x render
node). On any other machine :func:`supported` is False, ``/status`` says so,
``/play`` refuses, and the dashboard hides the button; TV still plays in the
Jellyfin frame.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

CONTAINER = "puffpai-tvplayer"
IMAGE = os.environ.get("PUFFPAI_TVPLAYER_IMAGE", "puffpai-tvplayer:1")
#: The install's media server config, read inside the container. Empty
#: means media_server_config.json in this install's config dir.
_MEDIA_CONFIG = os.environ.get("PUFFPAI_TVPLAYER_MEDIA_CONFIG", "")
_FREE_ADS_M3U = os.environ.get(
    "PUFFPAI_TVPLAYER_FREE_M3U",
    "/data/puffpai/jellyfin/config/livetv/iptv-unreliable.m3u")
#: Plugin-registry cache. Empty means tvplayer/cache under the data dir.
_CACHE_DIR = os.environ.get("PUFFPAI_TVPLAYER_CACHE", "")
_PULSE_SOCK = os.environ.get("PUFFPAI_PULSE_SOCK", "/run/user/1000/pulse/native")
_XAUTH = os.environ.get("PUFFPAI_KIOSK_XAUTHORITY", "/run/user/1000/gdm/Xauthority")
_DISPLAY = os.environ.get("PUFFPAI_KIOSK_DISPLAY", ":0")
_HOST1X_RENDER = "/dev/dri/by-path/platform-13e00000.host1x-render"
_LIB = "/usr/lib/aarch64-linux-gnu"
# Groups that own the GPU/decoder device nodes on this box (video, render).
_GROUPS = ("44", "104")
_DOCKER_TIMEOUT = 20

_JF_ID = re.compile(r"^[0-9a-f]{32}$")
_FREE_ID = re.compile(r"^[0-9a-f]{12}$")

#: The Bridge switch for the free internet channels (shared with tv_cast).
DOOR_ID = "tv_free_channels"
#: Shown when the switch cannot be read at all; the switch's own refusal
#: sentence is used whenever it can be.
FREE_CHANNELS_OFF = ("Free TV channels are switched off. To watch or cast "
                     "them, open Settings and switch on 'Free TV channels' in "
                     "the Bridge section.")

_lock = threading.Lock()
_now = {"name": None, "kind": None, "started": None}


def supported() -> bool:
    """True on a Jetson: the Tegra decoder library and host1x render node exist."""
    return (sys.platform.startswith("linux")
            and os.path.exists(f"{_LIB}/nvidia/libnvv4l2.so")
            and os.path.exists(_HOST1X_RENDER))


def _media_config() -> str:
    if _MEDIA_CONFIG:
        return _MEDIA_CONFIG
    from puffpai.config import get_config
    return os.path.join(get_config().CONFIG_DIR, "media_server_config.json")


def _cache_dir() -> str:
    if _CACHE_DIR:
        return _CACHE_DIR
    from puffpai.statedir import resolve_runtime_dir
    return resolve_runtime_dir(
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "data", "tvplayer", "cache"),
        env_subdir=os.path.join("tvplayer", "cache"))


def _docker(args, timeout=_DOCKER_TIMEOUT):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout)


def _devices() -> list[str]:
    """The only device nodes the player gets (least privilege).

    Found by removing nodes from NVIDIA's full container list and
    re-proving live Pluto through ad switches: the memory allocator, the
    decoder's V4L2 entry, the host1x sync fence and DRI render node (the
    decoder and the VIC scaler sit behind host1x), and the GPU's control,
    address-space, channel, TSG and power nodes (EGL drawing). Without the
    fence or the GPU channel nodes the picture froze at the first ad switch,
    when the decoder and sink rebuild. Left out: DLA, PVA, NVENC, the
    nvhost-* aliases, GPU debug/profiler/scheduler nodes, nvidia0/ctl/modeset,
    the display card nodes, the framebuffers and nvsciipc. The render node
    is found by its platform path because DRI numbering can change at boot.
    """
    render = "/dev/dri/renderD128"
    try:
        render = os.path.realpath(_HOST1X_RENDER) if os.path.exists(
            _HOST1X_RENDER) else render
    except OSError:
        pass
    out = []
    gpu = "/dev/nvgpu/igpu0/"
    for path in ("/dev/nvmap", "/dev/v4l2-nvdec", "/dev/host1x-fence", render,
                 *(gpu + n for n in ("ctrl", "as", "channel", "tsg", "power"))):
        out += ["--device", path]
    return out


def _base_args(cache_mode: str) -> list[str]:
    ro = lambda src, dst=None: ["-v", f"{src}:{dst or src}:ro"]  # noqa: E731
    return [
        "--network", "host", "--user", "1000:1000",
        # Least privilege: no capabilities, no privilege gain, a read-only
        # root; the only writable place is a small private tmpfs.
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:size=16m,mode=1777",
        *[x for g in _GROUPS for x in ("--group-add", g)],
        *_devices(),
        *ro(f"{_LIB}/nvidia"), *ro(f"{_LIB}/tegra-egl"),
        *ro(f"{_LIB}/libv4l/plugins/nv"),
        *ro(f"{_LIB}/gstreamer-1.0/libgstnvvideo4linux2.so",
            "/opt/nvgst/libgstnvvideo4linux2.so"),
        *ro(f"{_LIB}/gstreamer-1.0/libgstnvvideosinks.so",
            "/opt/nvgst/libgstnvvideosinks.so"),
        *ro(f"{_LIB}/gstreamer-1.0/libgstnvvidconv.so",
            "/opt/nvgst/libgstnvvidconv.so"),
        *ro("/usr/share/glvnd/egl_vendor.d/10_nvidia.json"),
        # GStreamer's plugin registry: built once (5 to 6 s), then mounted
        # read-only so every start skips the scan.
        "-v", f"{_cache_dir()}:/cache:{cache_mode}",
        "-e", "GST_REGISTRY=/cache/gst-registry.bin",
    ]


def _ensure_registry() -> None:
    """Build the plugin registry once, in a throwaway container."""
    if os.path.exists(os.path.join(_cache_dir(), "gst-registry.bin")):
        return
    os.makedirs(_cache_dir(), exist_ok=True)
    try:
        _docker(["run", "--rm", *_base_args("rw"), "--entrypoint",
                 "gst-inspect-1.0", IMAGE, "nvv4l2decoder"], timeout=60)
    except Exception as exc:
        logger.warning("tv player: registry build failed: %s", type(exc).__name__)


def _run_args(mode: str, arg: str) -> list[str]:
    ro = lambda src, dst=None: ["-v", f"{src}:{dst or src}:ro"]  # noqa: E731
    args = [
        "run", "-d", "--rm", "--name", CONTAINER, *_base_args("ro"),
        *ro("/tmp/.X11-unix"), *ro(_XAUTH, "/run/x11auth"),
        "-e", f"DISPLAY={_DISPLAY}", "-e", "XAUTHORITY=/run/x11auth",
        "-v", f"{_PULSE_SOCK}:/run/pulse/native",
        "-e", "PULSE_SERVER=unix:/run/pulse/native",
    ]
    if mode == "jellyfin":
        args += ro(_media_config(), "/run/puffpai/media.json")
    # The URL / id is the last argument and only ever data to the entry
    # script (tvplay), which checks it again.
    return [*args, IMAGE, mode, arg]


def is_playing() -> bool:
    try:
        r = _docker(["ps", "-q", "-f", f"name=^{CONTAINER}$"], timeout=8)
        return bool(r.stdout.strip())
    except Exception:
        return False


def _phase() -> str:
    """'off', 'starting' (container up, no picture yet) or 'playing'."""
    if not is_playing():
        return "off"
    try:
        r = _docker(["logs", "--tail", "40", CONTAINER], timeout=8)
        text = (r.stdout or "") + (r.stderr or "")
    except Exception:
        return "starting"
    return "playing" if "first frame" in text else "starting"


def status() -> dict:
    phase = _phase()
    with _lock:
        if phase == "off":
            _now.update(name=None, kind=None, started=None)
        return {"phase": phase, "playing": phase != "off",
                "supported": supported(), **_now}


def stop() -> bool:
    """End playback; the window closes and the dashboard shows. Always safe."""
    try:
        # SIGTERM first so a Jellyfin channel closes its live stream cleanly,
        # then remove whatever is left.
        _docker(["stop", "-t", "5", CONTAINER], timeout=15)
    except Exception:
        pass
    try:
        _docker(["rm", "-f", CONTAINER], timeout=12)
    except Exception:
        pass
    with _lock:
        _now.update(name=None, kind=None, started=None)
    ok = not is_playing()
    logger.info("tv player: stop (%s)", "done" if ok else "still running")
    return ok


def _start(mode: str, arg: str, name: str) -> tuple[bool, str]:
    stop()
    try:
        # Only one appliance sound at a time: TV replaces the radio.
        from puffpai import radio_player
        radio_player.stop()
    except Exception:
        pass
    _ensure_registry()
    try:
        r = _docker(_run_args(mode, arg))
    except Exception as exc:
        return False, f"the TV player could not start ({type(exc).__name__})"
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
        logger.warning("tv player: docker run failed: %s", detail[0][:200])
        return False, "the TV player could not start"
    # A bad source makes the player exit within a couple of seconds; anything
    # still running after that is starting up (the picture follows in about
    # 5 to 20 s while the video engine wakes).
    time.sleep(2.5)
    if not is_playing():
        return False, "that channel did not start"
    with _lock:
        _now.update(name=name, kind=mode, started=int(time.time()))
    logger.info("tv player: playing %s (%s)", name, mode)
    return True, ""


# ── Channel lists ────────────────────────────────────────────────────────────

def jellyfin_channels() -> list[dict]:
    """Live TV channels Jellyfin carries: [{id, number, name}]."""
    from puffpai.media_bridge import get_media_bridge
    bridge = get_media_bridge()
    data = bridge._jf_request(  # noqa: SLF001 (same package)
        "/LiveTv/Channels",
        params={"UserId": bridge._get_user_id(), "Limit": 500,  # noqa: SLF001
                "EnableImages": "false", "SortBy": "SortName"})
    out = []
    for ch in (data or {}).get("Items", []) if isinstance(data, dict) else []:
        cid = str(ch.get("Id", "")).replace("-", "").lower()
        if _JF_ID.match(cid):
            out.append({"id": cid, "number": ch.get("ChannelNumber"),
                        "name": ch.get("Name", "")})

    def _num(c):
        try:
            return float(c["number"])
        except (TypeError, ValueError):
            return 1e9
    out.sort(key=_num)
    return out


# A Pluto TV channel id, in any address form the list has used: the old
# relay (jmp2.uk/plu-<id>.m3u8) or Pluto's own stitcher (.../channel/<id>/...).
_PLUTO = re.compile(
    r"^https://(?:jmp2\.uk/plu-(?P<a>[0-9a-f]{24})\.m3u8"
    r"|[a-z0-9.-]+\.pluto\.tv/[^?#]*/channel/(?P<b>[0-9a-f]{24})/)")


def pluto_id(url: str) -> str | None:
    """The Pluto channel id behind *url*, or None if it is not a Pluto address."""
    m = _PLUTO.match(url or "")
    return (m.group("a") or m.group("b")) if m else None


_ATTR = re.compile(r'[\w-]+="[^"]*"')
_GROUP = re.compile(r'group-title="([^"]*)"')


def free_ad_channels() -> list[dict]:
    """The set-aside ad-stitched channels: [{id, name, group, url}].

    ``url`` stays server-side; routes strip it before answering.
    """
    try:
        with open(_FREE_ADS_M3U, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh]
    except OSError:
        return []
    out, info = [], None
    for ln in lines:
        if ln.startswith("#EXTINF"):
            # Attributes are key="value" (a logo URL may hold commas), so drop
            # them first; the name is what follows the first comma after that.
            group = _GROUP.search(ln)
            bare = _ATTR.sub("", ln)
            info = (group.group(1) if group else "",
                    bare.split(",", 1)[1].strip() if "," in bare else "")
        elif ln and not ln.startswith("#") and info:
            group, name = info
            out.append({
                "id": hashlib.sha1(ln.encode()).hexdigest()[:12],
                "name": name or "Free channel", "group": group or "Other",
                "url": ln,
            })
            info = None
    return out


def play_jellyfin(channel_id: str) -> tuple[bool, str]:
    cid = str(channel_id or "").replace("-", "").lower()
    if not _JF_ID.match(cid):
        return False, "that is not a channel"
    name = "TV channel"
    try:
        for ch in jellyfin_channels():
            if ch["id"] == cid:
                name = ch["name"]
                break
        else:
            return False, "that channel is not in the guide"
    except Exception as exc:
        logger.warning("tv player: channel list failed: %s", type(exc).__name__)
        return False, "the media server did not answer"
    return _start("jellyfin", cid, name)


def free_channels_refusal() -> str | None:
    """None while the free channels' Bridge switch is open, else why not.

    Fails closed: a switch that cannot be read is a closed switch. Asking
    records a refusal in the egress ledger, like every other switch.
    """
    try:
        from puffpai.security import egress_doors
    except Exception:
        return FREE_CHANNELS_OFF
    try:
        egress_doors.require_open("tv_free_channels")
    except egress_doors.EgressBlocked as blocked:
        return blocked.user_message or FREE_CHANNELS_OFF
    except Exception:
        return FREE_CHANNELS_OFF
    return None


def _record_start(mode: str, url: str, ok: bool) -> None:
    """One ledger line per free-channel start, naming the first host it reaches.

    The host only: a stream's path or query can carry a session id.
    """
    try:
        from puffpai.security import egress_doors, egress_ledger
        if mode == "pluto":
            # The container's first call is Pluto's anonymous-session boot.
            host = "boot.pluto.tv"
        else:
            host = egress_ledger.safe_parts(url)[0]
        egress_doors.record_external(
            DOOR_ID, True, host=host, kind="player_start",
            detail=f"tv player {mode} {'started' if ok else 'did not start'}")
    except Exception as exc:  # a ledger fault must not break playback
        logger.debug("tv player: ledger write failed: %s", type(exc).__name__)


def play_free(free_id: str) -> tuple[bool, str]:
    """Play one set-aside internet channel. Refuses while its switch is closed."""
    refusal = free_channels_refusal()
    if refusal:
        return False, refusal
    fid = str(free_id or "").lower()
    if not _FREE_ID.match(fid):
        return False, "that is not a channel"
    for ch in free_ad_channels():
        if ch["id"] == fid:
            pluto = pluto_id(ch["url"])
            if pluto:
                # Pluto needs a fresh anonymous session per play; the player
                # starts it itself, so only the channel id leaves this module.
                ok, why = _start("pluto", pluto, ch["name"])
                _record_start("pluto", ch["url"], ok)
                return ok, why
            from puffpai.radio_player import stream_url_refusal
            refusal = stream_url_refusal(ch["url"])
            if not refusal and not ch["url"].startswith("https://"):
                # tvplay.sh refuses it too; refusing here says why.
                refusal = "that channel is not an encrypted (https) stream"
            if refusal:
                logger.warning("tv player: refused %s: %s", ch["name"], refusal)
                return False, refusal
            ok, why = _start("url", ch["url"], ch["name"])
            _record_start("url", ch["url"], ok)
            return ok, why
    return False, "that channel is not in the list"


# ── HTTP ─────────────────────────────────────────────────────────────────────
# Behind the app-wide API-key gate like every other route (see
# bridge_routes.py); nothing here is public.

tv_bp = Blueprint("tv_native", __name__, url_prefix="/api/tv/native")


@tv_bp.route("/status", methods=["GET"])
def tv_status():
    return jsonify({"success": True, **status()})


@tv_bp.route("/channels", methods=["GET"])
def tv_channels():
    home, err = [], None
    try:
        home = jellyfin_channels()
    except Exception as exc:
        logger.warning("tv player: channel list failed: %s", type(exc).__name__)
        err = "the media server did not answer"
    free = [{k: c[k] for k in ("id", "name", "group")} for c in free_ad_channels()]
    return jsonify({"success": err is None, "error": err,
                    "channels": home, "free_ads": free})


@tv_bp.route("/play", methods=["POST"])
def tv_play():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"success": False, "error": "send a JSON object"}), 400
    if not (body.get("channel_id") or body.get("free_id")):
        return jsonify({"success": False,
                        "error": "channel_id or free_id required"}), 400
    if not body.get("channel_id"):
        # Asked here as well as in play_free, and before the hardware check,
        # so a closed switch answers 403 with the switch named on any machine.
        refusal = free_channels_refusal()
        if refusal:
            return jsonify({"success": False, "reason": "bridge_closed",
                            "switches": [DOOR_ID], "error": refusal}), 403
    if not supported():
        return jsonify({"success": False, "supported": False,
                        "error": "native TV playback needs a Jetson; "
                                 "play TV in the media page instead"}), 503
    if body.get("channel_id"):
        ok, why = play_jellyfin(body["channel_id"])
    else:
        ok, why = play_free(body["free_id"])
    if not ok:
        return jsonify({"success": False, "error": why, **status()}), 502
    return jsonify({"success": True, **status()})


@tv_bp.route("/stop", methods=["POST"])
def tv_stop():
    ok = stop()
    return jsonify({"success": ok, **status()}), (200 if ok else 500)


def register(app) -> None:
    app.register_blueprint(tv_bp)


__all__ = ["CONTAINER", "DOOR_ID", "IMAGE", "free_ad_channels",
           "free_channels_refusal", "is_playing", "jellyfin_channels",
           "play_free", "play_jellyfin", "register", "status", "stop",
           "tv_bp"]
