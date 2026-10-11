"""Appliance speaker volume — the master lever for everything she hears.

Server-side radio (the mpv container in :mod:`radio_player`), Jellyfin playlist
playback, and Piper's speech all render through PulseAudio's default sink (the
ReSpeaker's analog out, which feeds the powered speakers). Moving THAT sink is
what a person means by "turn the volume up" — it changes whatever happens to be
making sound, without needing to know which backend owns it.

Deliberately the sink and NOT the sink-inputs. Voice barge-in ducking
(``voice/service.py:_duck_playback``) lowers individual sink-inputs while she is
speaking and restores them to 100%%, so a per-stream level set here would be
silently wiped the next time she said "Hey PuffPAI". Sink volume is an
independent axis that ducking never touches.

Spotify (librespot) drives ALSA directly rather than
Pulse, so they are outside this lever; they are also not what "turn the radio
up" refers to. Their own controls stay with their own modules.

Reaching Pulse from inside the service is the awkward part. ``puffpai-api`` sets
``ProtectHome=true``, which bind-mounts ``/run/user`` to systemd's *inaccessible*
dir — so the usual socket at ``/run/user/1000/pulse/native`` is not merely
hidden, it cannot be reached at all, and every volume command answered "I
couldn't reach the speaker mixer". A ``BindPaths=`` drop-in does NOT fix that:
the mask is a read-only inaccessible mount, so systemd cannot create a
mountpoint underneath it to bind into (verified live — the bind silently never
materialised).

So PulseAudio also listens on a SECOND unix socket under the service's own
state dir, which is already in the unit's ``ReadWritePaths``. That path needs no
root, no sandbox change, and no mask/bind ordering luck. The XDG socket is kept
as a fallback so this module still works unchanged from a normal shell.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from puffpai.platform_paths import data_path

_RUNTIME_DIR = os.environ.get("PUFFPAI_PULSE_RUNTIME_DIR", "/run/user/1000")
_SINK = os.environ.get("PUFFPAI_PULSE_SINK", "@DEFAULT_SINK@")
#: Second Pulse socket, published by module-native-protocol-unix into the
#: service's state dir. See the module docstring for why the standard one is
#: unreachable from inside the unit's sandbox. The default follows the state
#: root (``$PUFFPAI_DATA_DIR``, per-user otherwise) rather than naming the
#: appliance's ``/data/puffpai`` — on a generic Linux box that directory does
#: not exist, and on Windows there is no Pulse at all.
_SOCKET = os.environ.get("PUFFPAI_PULSE_SOCKET", data_path("pulse.sock"))
_TIMEOUT = 6

#: How far one bare "turn it up" moves the dial, in percentage points.
STEP = 10

_PCT_RE = re.compile(r"(\d{1,3})%")


def pactl_available() -> bool:
    """True when a ``pactl`` binary exists on this machine.

    PulseAudio is a Linux thing. Windows and a headless CPU-only Linux box
    without Pulse have no mixer this module can move, and that is an honest
    "not available on this platform", not a failure: callers already treat a
    non-zero return as "Pulse is unreachable".
    """
    return shutil.which("pactl") is not None


def _pactl(*args: str) -> subprocess.CompletedProcess:
    """Run pactl, preferring the socket the sandbox can actually reach.

    Tries the state-dir socket first (the only one visible to the service),
    then the XDG one (the only one visible from a plain shell). Falling back
    rather than choosing once means the same code works in both places, and a
    stale socket file cannot wedge it.

    With no ``pactl`` on the box a synthetic non-zero result is returned
    instead of raising ``FileNotFoundError`` per call, so every caller sees the
    same "mixer unreachable" shape on every platform.
    """
    if not pactl_available():
        return subprocess.CompletedProcess(
            args=["pactl", *args], returncode=127, stdout="",
            stderr="pactl not found: PulseAudio volume control is not "
                   "available on this platform",
        )

    base = dict(os.environ)
    base["XDG_RUNTIME_DIR"] = _RUNTIME_DIR
    # A stale PULSE_SERVER would win over everything below and point us at a
    # socket that is not this session's.
    base.pop("PULSE_SERVER", None)

    envs = []
    if _SOCKET and os.path.exists(_SOCKET):
        via_socket = dict(base)
        via_socket["PULSE_SERVER"] = "unix:" + _SOCKET
        envs.append(via_socket)
    envs.append(base)

    result = None
    for env in envs:
        result = subprocess.run(["pactl", *args], capture_output=True,
                                text=True, timeout=_TIMEOUT, env=env)
        if result.returncode == 0:
            return result
    return result


def get_state() -> tuple[int | None, bool]:
    """Return ``(percent, muted)``; ``(None, False)`` if Pulse is unreachable."""
    try:
        r = _pactl("get-sink-volume", _SINK)
        if r.returncode != 0:
            return None, False
        m = _PCT_RE.search(r.stdout or "")
        if not m:
            return None, False
        muted = "yes" in (_pactl("get-sink-mute", _SINK).stdout or "").lower()
        return int(m.group(1)), muted
    except Exception:
        return None, False


def set_percent(pct: int) -> int | None:
    """Set absolute volume 0-100. Returns the level set, or None on failure."""
    pct = max(0, min(100, int(pct)))
    try:
        if _pactl("set-sink-volume", _SINK, f"{pct}%").returncode != 0:
            return None
        # Asking for sound implies wanting to hear it: a raise un-mutes, so she
        # never ends up turning a muted sink up and still hearing nothing.
        if pct > 0:
            _pactl("set-sink-mute", _SINK, "0")
        return pct
    except Exception:
        return None


def nudge(delta: int) -> int | None:
    """Move the volume by ``delta`` points. Returns the new level, or None."""
    current, _ = get_state()
    if current is None:
        return None
    return set_percent(current + delta)


def set_mute(muted: bool) -> bool:
    try:
        return _pactl("set-sink-mute", _SINK, "1" if muted else "0").returncode == 0
    except Exception:
        return False
