"""What is ACTUALLY playing on the appliance, right now.

WHY THIS MODULE EXISTS
    With World Radio playing (a real ``mpv --ao=pulse <icecast url>`` process
    AND a live PulseAudio sink-input), "what's playing right now" must not be
    answered "There's nothing playing at the moment." The same holds for a
    TuneIn stream.

    The trap is structural. Asking JELLYFIN for its session list and nothing
    else makes radio and TuneIn invisible by construction. The dashboard tile's
    ``media_hub.aggregate_sources`` does know about radio, but it gates on the
    KIOSK's poll heartbeat (``radio_last_seen``) — a dashboard-liveness signal,
    not a playback signal — so a stale or closed kiosk also reports silence
    while the speakers are audibly playing.

THE RULE THIS MODULE FOLLOWS
    Derive from the PLAYER, never from a declaration. Every source below is
    read from positive, observable evidence:

      radio / TuneIn  the ``puffpai-radio`` mpv container is running
                      (``radio_player.is_playing()`` — a docker ps, the same
                      evidence ``_media_sources_still_playing`` already trusts)
      Jellyfin        a Jellyfin session reports a loaded item

    The station/track NAME still comes from the app-data record, because the
    player itself has no metadata channel — but a name is only ever attached to
    a source that was independently observed to be playing. A name without a
    live player is never reported.

    Last line of defence: if no named source can be confirmed but PulseAudio
    still has a sink-input, we say something is playing and that we cannot name
    it. Saying "nothing is playing" over audible sound is the one answer this
    module must never give.

puff puff pass
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Display name for the appliance radio player. World Radio and TuneIn are the
#: same radio-browser-backed player, so the STATION name is what distinguishes
#: them to the listener; the source label stays stable.
RADIO_SOURCE = "World Radio"
JELLYFIN_SOURCE = "Jellyfin"
SPEAKERS_SOURCE = "the speakers"
#: Not a source — the marker for "every probe came back unreadable". Kept
#: distinct from an empty result because "I could not look" and "I looked and
#: there was nothing" are different answers, and only one of them is silence.
UNKNOWN_SOURCE = "unknown"
UNKNOWN_REPLY = ("I can't tell right now — the audio system didn't answer.")

#: A radio status that means "a station was tuned", as opposed to the mpv
#: container being reused for a Jellyfin playlist queue.
_RADIO_LIVE_STATUS = ("playing", "connecting", "paused")


def _radio_station_name(app_data: Any) -> str | None:
    """The station name last tuned, from mediahub app data. Never raises.

    Deliberately does NOT apply the ``radio_last_seen`` staleness gate that
    ``media_hub.aggregate_sources`` applies: that gate asks "is the kiosk
    polling?", and the kiosk's health has nothing to do with whether the
    appliance's own mpv process is making sound.
    """
    try:
        if app_data is None:
            from puffpai.app_data import AppData
            app_data = AppData()
        doc = app_data.get_data("mediahub") or {}
    except Exception as exc:
        logger.debug("mediahub read for now-playing failed: %s", exc)
        return None
    if not isinstance(doc, dict):
        return None
    radio = doc.get("radio")
    if not isinstance(radio, dict):
        return None
    if str(radio.get("status") or "").strip().lower() not in _RADIO_LIVE_STATUS:
        return None
    station = radio.get("station")
    if isinstance(station, dict):
        name = str(station.get("name") or "").strip()
        return name or None
    if isinstance(station, str) and station.strip():
        return station.strip()
    return None


def _radio_source(app_data: Any) -> dict | None:
    """The appliance radio player, if its process is actually running."""
    try:
        from puffpai import radio_player
        if not radio_player.is_playing():
            return None
    except Exception as exc:
        logger.debug("radio observation skipped: %s", exc)
        return None
    name = _radio_station_name(app_data)
    if name:
        return {"source": RADIO_SOURCE, "label": name}
    # The container is running but nothing named it — most likely the Jellyfin
    # playlist queue, which shares this player. Report the truth we have.
    return {"source": SPEAKERS_SOURCE, "label": None}


def _jellyfin_sources() -> list:
    """Every Jellyfin session with something loaded."""
    out = []
    try:
        from puffpai.media_bridge import get_media_bridge
        sessions = get_media_bridge().now_playing()
    except Exception as exc:
        logger.debug("jellyfin observation skipped: %s", exc)
        return out
    for s in sessions or []:
        item = getattr(s, "now_playing", None)
        if not item:
            continue
        label = getattr(item, "display", None) or ""
        device = getattr(s, "device_name", "") or ""
        if getattr(s, "is_paused", False):
            label = f"{label} (paused)".strip()
        if device:
            label = f"{label} on {device}".strip()
        out.append({"source": JELLYFIN_SOURCE, "label": label or None})
    return out


def _pulse_sink_input_count() -> int | None:
    """How many PulseAudio streams are open. ``None`` when Pulse is unreadable.

    This is the backstop that makes a false "nothing is playing" impossible:
    every appliance audio path (mpv, librespot, the dashboard tab)
    lands on this sink.
    """
    try:
        from puffpai.audio_volume import _pactl
        result = _pactl("list", "sink-inputs", "short")
        if result is None or result.returncode != 0:
            return None
        return len([ln for ln in (result.stdout or "").splitlines() if ln.strip()])
    except Exception as exc:
        logger.debug("pulse sink-input read skipped: %s", exc)
        return None


def observed_playback(app_data: Any = None) -> list:
    """Every source OBSERVABLY making sound, as ``{"source", "label"}`` dicts.

    Never raises: this answers a spoken question and must degrade to "I can't
    tell" rather than to a traceback.
    """
    found = []
    # The CHEAP, definite probes: radio is a `docker ps`, Jellyfin is one
    # HTTP call.
    try:
        entry = _radio_source(app_data)
    except Exception as exc:                              # pragma: no cover
        logger.debug("now-playing radio probe failed: %s", exc)
        entry = None
    if entry:
        found.append(entry)
    try:
        found.extend(_jellyfin_sources())
    except Exception as exc:                              # pragma: no cover
        logger.debug("now-playing jellyfin probe failed: %s", exc)
    if found:
        return found
    # Nothing named itself. Before claiming silence, check the mixer.
    try:
        count = _pulse_sink_input_count()
    except Exception as exc:                              # pragma: no cover
        logger.debug("now-playing pulse probe failed: %s", exc)
        count = None
    if count:
        return [{"source": SPEAKERS_SOURCE, "label": None}]
    if count is None:
        # Pulse did not answer (pactl missing, non-zero exit, socket hidden by
        # ProtectHome). Every probe we have is now unreadable, so we know
        # NOTHING — and "nothing is playing" is a claim, not an absence of one.
        return [{"source": UNKNOWN_SOURCE, "label": None, "unknown": True}]
    return []


def describe_playback(app_data: Any = None, entries: list | None = None) -> str:
    """One spoken sentence naming what is playing, and where.

    ``entries`` lets a caller that has ALREADY run :func:`observed_playback`
    pass the result straight in, so a single turn never probes twice.
    """
    if entries is None:
        entries = observed_playback(app_data)
    if not entries:
        return "Nothing is playing right now."
    if any(e.get("unknown") or e.get("source") == UNKNOWN_SOURCE
           for e in entries):
        return UNKNOWN_REPLY
    lines = []
    for e in entries:
        source = e.get("source") or SPEAKERS_SOURCE
        label = e.get("label")
        if label:
            lines.append(f"{label} on {source}.")
        elif source == SPEAKERS_SOURCE:
            lines.append("Something is playing on the speakers, but I can't "
                         "tell you what — I don't have a title for it.")
        else:
            lines.append(f"{source} is playing, but I don't have a title for it.")
    return " ".join(lines)
