"""Voice answers that must come from the box, not from a model's guess.

One section per failure class, each pinned to the voice turn that shows it:

* **The briefing**: "give me my daily briefing" must not hang for 45s and
  answer after 53s, or open "Good morning" at 00:50.
* **The house, counted**: "Are the doors locked?" must not answer "I couldn't
  find a device matching that" when there is no ``lock.*`` entity in the house
  at all; "how many devices can you control" must not answer with 25s of essay
  about household wiring and circuit breakers.
* **The document I just sent**: "tell me what's in the document I just sent
  you" must not answer with the byte-identical filename recital that "what
  documents do you have" returns.
* **Web tools**: "what can you do" marks "web tools" with the shut-door marker.
* **Code replies**: a code reply ends with one sentence saying the code is in
  the chat, never two.
* **Zigbee pairing**: "did the zigbee pairing window open" must not leak
  ``script.puffpaizigbeepermit...``, and "what did you find when pairing" must
  not return an iFixit guide to 3-D glasses.

Everything here runs with no model and no network.
"""

from datetime import datetime

import pytest

from puffpai import api_server
from puffpai.agents import daily_brief


# ── the briefing composes locally, in four sentences ────────────────────────

def test_the_night_greeting_is_not_good_morning():
    said = daily_brief.compose(now=datetime(2026, 9, 15, 0, 50),
                               city="the default city")
    assert said.startswith("Hello, the default city.")
    assert "Good morning" not in said


@pytest.mark.parametrize("hour,opening", [
    (5, "Good morning"), (11, "Good morning"),
    (12, "Good afternoon"), (16, "Good afternoon"),
    (17, "Good evening"), (21, "Good evening"),
    (22, "Hello"), (2, "Hello"), (4, "Hello"),
])
def test_every_hour_gets_the_greeting_the_spec_names(hour, opening):
    said = daily_brief.compose(now=datetime(2026, 9, 15, hour, 30))
    assert said.startswith(opening)


def test_the_briefing_reads_weather_air_calendar_and_tasks():
    said = daily_brief.compose(
        now=datetime(2026, 9, 15, 7, 0), city="the default city",
        temp=82, high=100, aqi=30, aqi_label="Good",
        occurrences=[{"title": "Dentist", "start": "2026-09-15T09:00:00"},
                     {"title": "Book club", "start": "2026-09-15T19:30:00"}],
        open_tasks=3)
    assert "Good morning, the default city." in said
    assert "It's 82 out" in said and "high of 100" in said
    assert "air quality is 30" in said
    assert "Dentist at 9 AM" in said and "Book club at 7:30 PM" in said
    assert "3 tasks still open." in said
    # Four sentences, not a paragraph: this is read out loud.
    assert said.count(".") <= 5


def test_an_empty_day_is_said_plainly_not_padded():
    said = daily_brief.compose(now=datetime(2026, 9, 15, 13, 0))
    assert "Nothing is on the calendar today." in said
    assert "No tasks are open." in said


def test_the_briefing_hook_calls_no_model(monkeypatch):
    calls = []

    class _Tool:
        def invoke(self, _arg):
            calls.append("weather")
            return "Currently 82F. High 100F. 10% chance of rain."

    class _Tools:
        tool_dict = {"auto_weather_forecast": _Tool()}

    class _App:
        def cal_occurrences(self, _a, _b):
            return [{"title": "Dentist", "start": "2026-09-15T09:00:00"}]

        def _load(self, _name):
            return {"tasks": [{"status": "pending"}, {"status": "done"}]}

    monkeypatch.setattr(api_server, "_get_assistant_tools", lambda: _Tools())
    monkeypatch.setattr(api_server, "_get_app_data", lambda: _App())
    monkeypatch.setattr(api_server, "_home_city", lambda: "the default city")
    import puffpai.dashboard_widgets as dw
    monkeypatch.setattr(dw, "fetch_environment",
                        lambda *a, **k: {"us_aqi": 30, "aqi_label": "Good"})
    said = api_server._try_daily_briefing("give me my daily briefing")
    assert said and "It's 82 out" in said and "high of 100" in said
    assert "air quality is 30" in said
    assert "Dentist at 9 AM" in said
    assert "1 task still open." in said
    assert calls == ["weather"]


@pytest.mark.parametrize("said", [
    "give me my daily briefing", "what's my briefing", "brief me",
    "my morning briefing please",
])
def test_the_briefing_phrasings_are_claimed(said):
    assert api_server._DAILY_BRIEFING_RE.search(said.lower())


@pytest.mark.parametrize("said", [
    "brief me on the water purification article",
    "give me a brief summary of the news",
])
def test_a_brief_me_about_something_is_not_the_briefing(said):
    assert not api_server._DAILY_BRIEFING_RE.search(said.lower())


# ── the house, counted ──────────────────────────────────────────────────────

def _states(*rows):
    out = []
    for entity_id, state, name in rows:
        out.append({"entity_id": entity_id, "state": state,
                    "attributes": {"friendly_name": name}})
    return out


class _Bridge:
    def __init__(self, rows):
        self.rows = rows

    def get_all_states(self):
        return self.rows


HOUSE = _states(
    ("light.kitchen", "on", "Kitchen"),
    ("light.porch", "off", "Porch"),
    ("switch.fan", "off", "Fan Switch"),
    ("climate.living_room", "heat", "Living Room"),
    ("media_player.tv", "idle", "TV"),
    ("script.puffpai_zigbee_permit", "off", "Zigbee permit"),
    ("sensor.humidity", "44", "Humidity"),
)


def test_no_locks_is_said_plainly_not_as_a_lookup_failure(monkeypatch):
    monkeypatch.setattr(api_server, "_get_ha_bridge", lambda: _Bridge(HOUSE))
    said = api_server._try_doors_locked("Are the doors locked?")
    assert said == "You don't have any door locks set up in Home Assistant."
    assert "couldn't find a device" not in said


def test_real_locks_are_reported_by_name(monkeypatch):
    rows = HOUSE + _states(("lock.front", "locked", "Front Door"),
                           ("lock.back", "unlocked", "Back Door"))
    monkeypatch.setattr(api_server, "_get_ha_bridge", lambda: _Bridge(rows))
    said = api_server._try_doors_locked("are the doors locked")
    assert "Back Door" in said and "not locked" in said
    assert "lock.back" not in said


def test_a_lock_command_is_left_alone(monkeypatch):
    monkeypatch.setattr(api_server, "_get_ha_bridge", lambda: _Bridge(HOUSE))
    assert api_server._try_doors_locked("lock the front door") is None


@pytest.mark.parametrize("said", [
    "how many devices can you control",
    "what devices can you control",
    "what can you control",
    "how many devices do you have",
])
def test_the_device_count_phrasings_are_claimed(said, monkeypatch):
    monkeypatch.setattr(api_server, "_get_ha_bridge", lambda: _Bridge(HOUSE))
    answer = api_server._try_device_count(said)
    assert answer is not None
    assert answer.startswith("I can control 6 devices:")
    assert "2 lights" in answer and "1 switch" in answer
    assert "1 thermostat" in answer and "1 media player" in answer
    assert "1 script" in answer
    # The sensor is not something the household can operate.
    assert "sensor" not in answer
    # And no raw entity id is ever spoken.
    assert "light." not in answer


def test_an_unreachable_bridge_counts_nothing(monkeypatch):
    class _Dead:
        def get_all_states(self):
            raise RuntimeError("no HA")

    monkeypatch.setattr(api_server, "_get_ha_bridge", lambda: _Dead())
    said = api_server._try_device_count("how many devices can you control")
    assert "can't reach Home Assistant" in said


def test_the_count_never_answers_a_circuit_breaker_question(monkeypatch):
    monkeypatch.setattr(api_server, "_get_ha_bridge", lambda: _Bridge(HOUSE))
    assert api_server._try_device_count(
        "how many devices can a 15 amp circuit breaker handle") is None


# ── the document I just sent ────────────────────────────────────────────────

INDEX = [
    {"id": "a", "filename": "old_notes.txt", "indexed_at": "2026-09-01T10:00",
     "word_count": 40},
    {"id": "b", "filename": "Lease Agreement.pdf",
     "indexed_at": "2026-09-14T22:10", "word_count": 900},
]


def _patch_index(monkeypatch, index=INDEX, text=""):
    import puffpai.document_lookup as dl
    monkeypatch.setattr(dl, "_load_index", lambda: list(index))
    monkeypatch.setattr(dl, "text_of", lambda entry, **k: text)


@pytest.mark.parametrize("said", [
    "tell me what's in the document I just sent you",
    "what's in the file I just uploaded",
    "what was in the last document I sent",
    "summarize the most recent document",
    # A typical speech-to-text garbling of the same request.
    "tell me what's in the document. i just let you",
])
def test_the_latest_upload_is_resolved_not_listed(said, monkeypatch):
    _patch_index(monkeypatch, text=(
        "This lease runs from October 1 2026 to September 30 2027. "
        "Rent is due on the first of each month. "
        "The tenant is responsible for utilities. "
        "A fourth sentence that should not be read out."))
    answer = api_server._try_latest_upload_read(said)
    assert answer is not None
    assert answer.startswith(
        "The last thing you sent me was Lease Agreement.pdf, uploaded "
        "2026-09-14.")
    assert "This lease runs from October 1 2026" in answer
    assert "A fourth sentence" not in answer
    # The forbidden pattern: a recital of the household's filenames.
    assert "old_notes.txt" not in answer
    assert "ingested documents" not in answer


def test_an_upload_with_no_text_says_so(monkeypatch):
    _patch_index(monkeypatch, text="")
    answer = api_server._try_latest_upload_read(
        "what's in the document I just sent you")
    assert "no readable text was extracted" in answer


def test_with_no_uploads_at_all_it_says_so(monkeypatch):
    _patch_index(monkeypatch, index=[], text="")
    answer = api_server._try_latest_upload_read(
        "what's in the document I just sent you")
    assert answer == ("You haven't sent me any documents yet, so there's "
                      "nothing to read back.")


def test_the_list_question_is_not_claimed(monkeypatch):
    _patch_index(monkeypatch, text="x")
    assert api_server._try_latest_upload_read(
        "what documents do you have") is None


def test_the_gist_is_capped(monkeypatch):
    long_text = "word " * 400
    assert len(api_server._upload_gist(long_text)) <= \
        api_server._LATEST_UPLOAD_CHARS + 3


# ── web tools carry their shut-door marker ──────────────────────────────────

def test_web_tools_are_marked_off_when_the_chat_opt_in_is_off(monkeypatch):
    from puffpai import capability_registry as capreg
    import puffpai.security.egress_policy as ep
    monkeypatch.setattr(ep, "enabled", lambda policy_id: False)
    assert capreg._chat_egress_on() is False
    said = capreg.describe_for("what can you do")
    assert "web tools" + capreg.EGRESS_OFF_NOTE in said


def test_web_tools_are_marked_off_when_both_doors_are_shut(monkeypatch):
    from puffpai import capability_registry as capreg
    import puffpai.security.egress_policy as ep
    monkeypatch.setattr(ep, "enabled", lambda policy_id: True)
    monkeypatch.setattr(ep, "chat_egress_allowed",
                        lambda door_id=None: (False, "door shut"))
    assert capreg._chat_egress_on() is False


def test_web_tools_are_plain_when_the_gate_would_let_the_turn_out(monkeypatch):
    from puffpai import capability_registry as capreg
    import puffpai.security.egress_policy as ep
    monkeypatch.setattr(ep, "enabled", lambda policy_id: True)
    monkeypatch.setattr(ep, "chat_egress_allowed",
                        lambda door_id=None: (True, "open"))
    assert capreg._chat_egress_on() is True


# ── one closing sentence about the code, never two ──────────────────────────

def test_the_code_pointer_sentence_is_said_once():
    reply = ("This script renames photo files by their capture date.\n\n"
             "```python\nprint(1)\n```\n\n"
             "The full code is in this chat on your dashboard — I won't read "
             "it out loud.")
    said = api_server._clean_response(reply)
    assert api_server._VOICE_CODE_FENCE_SENTENCE not in said
    assert said.count("code is in") == 1
    assert "print(1)" not in said


def test_a_bare_fence_still_gets_its_one_sentence():
    said = api_server._clean_response("Here you go.\n\n```py\nprint(1)\n```")
    assert said.endswith(api_server._VOICE_CODE_FENCE_SENTENCE)
    assert said.count("code is in") == 1


# ── a pairing question reaches the pairing report ───────────────────────────

REPORT = ("The pairing window is closed. The last one opened at 9:12 PM for "
          "60 seconds and nothing joined.")


@pytest.mark.parametrize("said", [
    "did the zigbee pairing window open",
    "what did you find when pairing",
    "did anything join when you were pairing",
    "what happened with the zigbee pairing",
    "how did the pairing go",
    "pairing status",
])
def test_every_pairing_question_reaches_the_report(said, monkeypatch):
    import puffpai.zigbee_pairing as zp
    monkeypatch.setattr(zp, "pairing_status",
                        lambda: {"ok": True, "message": REPORT})
    answer = api_server._try_zigbee_pair_status(said)
    assert answer == REPORT
    assert "script." not in answer


def test_a_request_to_open_the_window_is_left_to_the_consent_path(monkeypatch):
    import puffpai.zigbee_pairing as zp
    monkeypatch.setattr(zp, "pairing_status",
                        lambda: {"ok": True, "message": REPORT})
    assert api_server._try_zigbee_pair_status(
        "open the pairing window") is None
    assert api_server._try_zigbee_pair_status(
        "put the hub in pairing mode") is None


@pytest.mark.parametrize("said", [
    "find me a pair of hiking boots",
    "what did you find in my documents",
    "how do I pair 3d glasses with my tv",
])
def test_the_pairing_rule_does_not_over_capture(said, monkeypatch):
    import puffpai.zigbee_pairing as zp
    monkeypatch.setattr(zp, "pairing_status",
                        lambda: {"ok": True, "message": REPORT})
    assert api_server._try_zigbee_pair_status(said) is None


def test_the_concierge_status_rule_hears_the_window_question():
    from puffpai.agents import concierge
    assert concierge.resolve_intent(
        "did the zigbee pairing window open").intent == "zigbee_pair_status"
    assert concierge.resolve_intent(
        "what did you find when pairing").intent == "zigbee_pair_status"
    # The REQUEST forms are untouched.
    assert concierge.resolve_intent(
        "open the pairing window").intent == "zigbee_pair"
