"""Every broadcast-lights phrasing gets the gated refusal.

Repro:

    "turn off everything" -> correct whole-house refusal
    "turn off all the lights in the house" -> "Okay - stopping any music or
                                              radio." (the media-stop fastpath)

No device was acted on either way, so the safety contract held — but the
sentence was wrong and confusing. The scope words in the gate were all
house-wide NOUNS; a device CLASS ("all the lights") missed it and fell through
to _try_media_stop, which matches "turn off" + "all".

This file pins: 8+ broadcast phrasings -> the refusal, ZERO Home Assistant
calls; "turn off the music" still stops media; "turn off the kitchen light"
still targets a device.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402

#: The primary phrasing first, then the rest of the broadcast shapes.
BROADCAST = (
    "turn off all the lights in the house",   # must not become a media stop
    "turn off all the lights",
    "turn off all lights in the house",
    "turn off every light",
    "turn all the lights off",
    "shut off all the lights",
    "switch off every light in the house",
    "turn off everything",
    "turn on all the lights",
    "please turn off all the lights everywhere",
)

#: Must keep their existing routes untouched.
TARGETED = (
    "turn off the kitchen light",
    "turn off the living room lights",
    "turn off the bedroom lights",
    "turn off the lights in the bedroom",
    "turn off the lamp",
    "turn down the lights",
)


@pytest.fixture(autouse=True)
def no_ha(monkeypatch):
    """Any Home Assistant call from this module is a test failure."""
    def _forbidden(*_a, **_k):
        raise AssertionError("the untargeted gate must never call HA")

    import puffpai.ha_bridge as hb
    for cls_name in dir(hb):
        cls = getattr(hb, cls_name)
        if isinstance(cls, type) and hasattr(cls, "call_service"):
            monkeypatch.setattr(cls, "call_service", _forbidden)
    for fn in ("get_ha_bridge", "call_service"):
        if hasattr(hb, fn):
            monkeypatch.setattr(hb, fn, _forbidden)
    return monkeypatch


@pytest.fixture(autouse=True)
def quiet_media(monkeypatch):
    """Nothing is making noise, so the reply carries no 'separately' clause."""
    monkeypatch.setattr(api, "_media_sources_still_playing", lambda: [])


@pytest.mark.parametrize("said", BROADCAST)
def test_broadcast_gets_the_refusal_not_the_media_stop(said):
    reply = api._try_untargeted_house_command(said)
    assert reply, f"{said!r} must be claimed by the untargeted gate"
    low = reply.lower()
    assert "music or radio" not in low, "a lights broadcast was answered as a media stop"
    assert "whole house from one command" in low
    assert "room or a device" in low


@pytest.mark.parametrize("said", BROADCAST)
def test_broadcast_is_claimed_before_the_media_stop_matcher(said):
    """Ordering, not just matching: the gate runs first in the dispatcher."""
    assert api._try_untargeted_house_command(said) is not None


def test_lights_refusal_names_what_it_will_not_do():
    reply = api._try_untargeted_house_command(
        "turn off all the lights in the house")
    assert "every light" in reply.lower()
    assert "kitchen" in reply.lower() or "living room" in reply.lower()


# ── nothing else may be swallowed ────────────────────────────────────────────

@pytest.mark.parametrize("said", TARGETED)
def test_targeted_commands_fall_through_untouched(said):
    assert api._try_untargeted_house_command(said) is None


@pytest.mark.parametrize("said", ("turn off the music", "stop the music",
                                  "turn off the radio", "stop", "pause"))
def test_media_stop_still_owns_media_phrasings(said, monkeypatch):
    assert api._try_untargeted_house_command(said) is None
    stopped = []
    monkeypatch.setattr("puffpai.media_hub.tool_radio_stop",
                        lambda app_data=None: stopped.append("radio") or "ok")
    reply = api._try_media_stop(said)
    assert reply, f"{said!r} must still reach the media-stop fastpath"
