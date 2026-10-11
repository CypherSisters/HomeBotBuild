"""Capability honesty: "what can you do" must not advertise shut-door capabilities.

`what can you do` returns the registry-backed domain list, and its tail says

    "I've also got a calculator, health checks, trivia, and web tools."

That claim is false while BOTH web doors (`web_search`, `web_fetch`) are shut
and `chat_allow_egress` is false. The owner closes those doors on purpose;
being sold the capability back is the same class of dishonesty as claiming an
action that never happened.

The capability read consults the egress-door registry and marks an area whose
doors are all shut. Nothing here opens a door or reads one's contents.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.capability_registry as capreg  # noqa: E402
from puffpai.security import egress_doors as ed  # noqa: E402

NOTE = capreg.EGRESS_OFF_NOTE


@pytest.fixture
def doors(monkeypatch):
    """Drive every door from the environment; env wins over config."""

    def _set(**states):
        for d in ed.DOORS:
            monkeypatch.setenv(d.env_var, "1" if states.get(d.id) else "0")
        ed.reset_cache()

    _set()
    yield _set
    ed.reset_cache()


def test_all_doors_shut_marks_web_tools_off(doors):
    doors()
    said = capreg.describe_for("what can you do")
    assert f"web tools{NOTE}" in said
    # The bare, unqualified claim must be gone.
    assert "and web tools." not in said


def test_all_doors_shut_marks_weather_and_news_off(doors):
    doors()
    said = capreg.describe_for("what can you do")
    assert f"weather and alerts{NOTE}" in said
    assert f"news{NOTE}" in said


def test_weather_open_is_not_marked_off(doors):
    doors(weather=True)
    said = capreg.describe_for("what can you do")
    assert "weather and alerts;" in said
    assert f"weather and alerts{NOTE}" not in said
    # …and the doors that are still shut stay marked.
    assert f"news{NOTE}" in said
    assert f"web tools{NOTE}" in said


def test_air_quality_alone_also_keeps_weather_honest(doors):
    """An area is reachable when ANY of its doors is open."""
    doors(air_quality=True)
    said = capreg.describe_for("what can you do")
    assert f"weather and alerts{NOTE}" not in said


def test_web_doors_open_restore_the_plain_claim(doors, monkeypatch):
    """BOTH halves are required. An open door is reach a PLANNED RUN has; the
    overview is read to somebody in a conversation, and a conversation also
    needs `chat_allow_egress`."""
    monkeypatch.setattr(capreg, "_chat_egress_on", lambda: True)
    doors(web_search=True)
    said = capreg.describe_for("what can you do")
    assert "and web tools." in said
    assert f"web tools{NOTE}" not in said


def test_an_open_door_is_still_off_while_chat_may_not_go_online(doors,
                                                                monkeypatch):
    monkeypatch.setattr(capreg, "_chat_egress_on", lambda: False)
    doors(web_search=True)
    said = capreg.describe_for("what can you do")
    assert f"web tools{NOTE}" in said, (
        "the overview offered web tools while chat egress was off")


def test_local_areas_are_never_marked_off(doors):
    """Nothing local depends on an egress door — don't scare the owner."""
    doors()
    said = capreg.describe_for("what can you do")
    for local in ("home devices", "cameras", "your vault", "network security",
                  "pantry and shopping", "knowledge and documents"):
        assert f"{local}{NOTE}" not in said
        assert local in said


def test_the_area_detail_read_is_marked_too(doors):
    doors()
    assert f"For weather{NOTE}" in capreg.describe("weather")
    doors(weather=True)
    assert f"For weather{NOTE}" not in capreg.describe("weather")


# ── the radio row names TuneIn only when its door is open ──
#
# The registry described World Radio's source as "radio-browser / TuneIn"
# unconditionally. TuneIn now has its OWN default-closed door,
# so on a stock box that sentence advertised a source the appliance cannot
# reach. Same class of dishonesty as the "web tools" defect above — the door
# table is simply read one level down, per capability.

def _radio_line(said: str) -> str:
    return next(ln for ln in said.splitlines() if "World Radio" in ln)


def test_both_radio_doors_open_reads_plainly(doors):
    doors(radio_browser=True, tunein=True)
    line = _radio_line(capreg.describe("streaming"))
    assert "radio-browser / TuneIn" in line
    assert NOTE not in line


def test_tunein_shut_marks_only_tunein(doors):
    doors(radio_browser=True)
    line = _radio_line(capreg.describe("streaming"))
    assert f"TuneIn{NOTE}" in line
    assert f"radio-browser{NOTE}" not in line
    # radio-browser is open, so station search is not claimed to be off.
    assert f"World Radio / TuneIn{NOTE}" not in line


def test_both_radio_doors_shut_marks_the_capability_off(doors):
    doors()
    line = _radio_line(capreg.describe("streaming"))
    assert f"World Radio / TuneIn{NOTE}" in line       # searching is off
    assert f"radio-browser{NOTE}" in line
    assert f"TuneIn{NOTE}" in line
    # …and the owner is told what still works without any door.
    assert "Saved stations still play." in line


def test_the_streaming_overview_is_not_marked_off_for_a_radio_door(doors):
    """Spotify does not go through these doors, so marking the whole
    streaming area 'off' would trade one wrong answer for another."""
    doors()
    said = capreg.describe_for("what can you do")
    assert f"Spotify and World Radio{NOTE}" not in said


def test_an_unreadable_registry_does_not_invent_an_off_marker(monkeypatch):
    """Fails toward saying nothing, never toward a wrong 'off'."""
    monkeypatch.setattr(
        ed, "egress_allowed",
        lambda door_id: (_ for _ in ()).throw(RuntimeError("no config")))
    assert capreg._any_door_open(("weather",)) is True
    assert capreg._area_note("weather") == ""
