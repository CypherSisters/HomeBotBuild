"""Routing regression tests from adversarial phrasings.

Every input below is an exact adversarial phrasing. Behaviours covered here:

* compound turns must not silently drop ``sources[]``
* (honesty half) the reply must not claim an SSN was "noted" while
  ``vaulted: []`` and nothing was stored
* ``cancel the 6:45 am alarm`` must work after ``set an alarm for 6:45 am``
* ``what radio stations can i listen to`` must not be answered from the
  encyclopedia
* ``widget_egress.jsonl`` must label weather fetches as weather, at 0640
* a control action speaks with one voice, never
  ``Done - light.office_status_led turn on.``

Run: pytest tests/test_routing_hard_phrasings.py -q
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ── a compound turn keeps every clause's citations ─────────────────────────

def test_dedup_sources_keeps_order_and_drops_repeats():
    from puffpai.api_server import _dedup_sources
    a = {"title": "Moon landing", "source_type": "zim_fts"}
    b = {"title": "Moon landing hoax", "source_type": "zim_fts"}
    assert _dedup_sources([a, b, dict(a)]) == [a, b]
    assert _dedup_sources([]) == []
    assert _dedup_sources(None) == []


def test_dedup_sources_survives_an_unhashable_record():
    from puffpai.api_server import _dedup_sources
    weird = {"title": "x", "spans": [[1, 2], [3, 4]]}
    assert _dedup_sources([weird, dict(weird)]) == [weird]


def test_compound_turn_carries_each_clause_sources(monkeypatch):
    """The same sources the standalone turn gets, never 0 in the compound."""
    from puffpai import api_server

    moon = [{"title": "Moon landing", "source_type": "zim_fts"},
            {"title": "Moon landing hoax", "source_type": "zim_fts"}]

    # **kw so the double tracks the real _process_chat_query signature, which
    # gained `conversation_id` in the calendar-nl merge.
    def fake_process(message, history=None, **kw):
        if "moon" in message.lower():
            return {'response': 'Apollo 11 landed in 1969.', 'sources': moon,
                    'suggestions': []}
        return {'response': 'Pantry (1 item): ground beef.', 'sources': [],
                'suggestions': []}

    captured = {}

    def fake_compound(user_message, read_handler, requester="owner"):
        # Drive the handler exactly the way the real assembler does.
        parts = [read_handler(c) for c in
                 ("tell me about the moon landing", "whats in the pantry")]
        captured['parts'] = parts
        return "\n\n".join(parts)

    monkeypatch.setattr(api_server, '_try_compound_request', fake_compound)
    monkeypatch.setattr(api_server, '_augment_persona_with_memory',
                        lambda persona, uid: persona)
    monkeypatch.setattr(api_server, '_load_persona', lambda: "p")
    monkeypatch.setattr(api_server, '_remember_for_user', lambda *a: None)
    monkeypatch.setattr(api_server, '_requester_for', lambda uid: "owner")
    monkeypatch.setattr(api_server, '_detect_route', lambda *a, **k: "rag")

    real = api_server._process_chat_query

    def dispatch(message, history=None, **kw):
        if message == "tell me about the moon landing, then whats in the pantry":
            return real(message, history, **kw)
        return fake_process(message, history, **kw)

    monkeypatch.setattr(api_server, '_process_chat_query', dispatch)

    result = dispatch(
        "tell me about the moon landing, then whats in the pantry")
    assert captured['parts'][0].startswith('Apollo 11')
    # added per-clause attribution to each record, so the
    # comparison is now "the same citations, plus the clause that retrieved
    # them" rather than byte-identical dicts. The defect this row exists for,
    # the citations being DROPPED, is exactly what is asserted.
    assert [{k: v for k, v in s.items()
             if k not in ('clause', 'clause_index')}
            for s in result['sources']] == moon, (
        "compound turn dropped its clause citations")
    assert all(s['clause'] == "tell me about the moon landing"
               for s in result['sources']), (
        "a clause's citations must name the clause that retrieved them")


# ── never claim PII was kept when nothing was kept ─────────────────────────

_SSN_META_NOT_VAULTED = {
    'detected': True,
    'summary': '1 SSN',
    'types': ['ssn'],
    'severity': 'critical',
    'vaulted': [],
    'redacted_message': 'my ssn is [REDACTED-SSN].',
    'handles': 0,
    'scrubbed_not_vaulted': 1,
    'status': 'VAULT_LOCKED',
}

_SSN_META_VAULTED = dict(_SSN_META_NOT_VAULTED,
                         vaulted=['ssn'], handles=1,
                         scrubbed_not_vaulted=0, status='OK')


def test_noted_claim_is_replaced_when_nothing_was_vaulted():
    """The false claim, verbatim: `Got it-your SSN has been noted.`"""
    from puffpai.api_server import _apply_pii_honesty
    out = _apply_pii_honesty("Got it—your SSN has been noted.",
                             _SSN_META_NOT_VAULTED)
    assert "noted" not in out.lower()
    assert "Social Security number" in out
    assert "did not keep it" in out
    assert "vault isn't unlocked" in out


def test_vaulted_turn_is_left_alone():
    """The other branch: when it really was vaulted, say nothing extra."""
    from puffpai.api_server import _apply_pii_honesty
    original = "Got it—your SSN is locked in the vault."
    assert _apply_pii_honesty(original, _SSN_META_VAULTED) == original


def test_no_pii_is_left_alone():
    from puffpai.api_server import _apply_pii_honesty
    assert _apply_pii_honesty("It is 72 degrees.", None) == "It is 72 degrees."
    assert _apply_pii_honesty("It is 72 degrees.",
                              {'detected': False}) == "It is 72 degrees."


def test_a_real_answer_is_prepended_not_destroyed():
    """A turn that also asked something real keeps its answer."""
    from puffpai.api_server import _apply_pii_honesty
    answer = ("Your shopping list has 4 items: milk, bread, eggs and coffee. "
              "I saved the last one you added on Tuesday, and the list is "
              "synced to the FreshTable app on the appliance screen so you "
              "can check it off there as you shop.")
    out = _apply_pii_honesty(answer, _SSN_META_NOT_VAULTED)
    assert answer in out
    assert out.startswith("I spotted a Social Security number")


def test_honesty_notice_names_the_type_that_was_detected():
    from puffpai.api_server import _pii_honesty_notice
    meta = dict(_SSN_META_NOT_VAULTED, types=['credit_card'])
    assert "credit-card number" in _pii_honesty_notice(meta)
    meta = dict(_SSN_META_NOT_VAULTED, types=['ssn', 'phone'])
    notice = _pii_honesty_notice(meta)
    assert "Social Security number" in notice and "phone number" in notice


def test_honesty_notice_without_a_status_still_tells_the_truth():
    """Legacy (flag-OFF) meta carries no `status` key."""
    from puffpai.api_server import _pii_honesty_notice
    meta = {'detected': True, 'types': ['ssn'], 'vaulted': []}
    notice = _pii_honesty_notice(meta)
    assert "did not keep it" in notice
    assert "not saved anywhere" in notice


# ── the reciprocal of "set an alarm for 6:45 am" ───────────────────────────

class _FakeTool:
    def __init__(self, sink):
        self._sink = sink

    def invoke(self, arg):
        return self._sink(arg)


class _FakeTools:
    def __init__(self, app):
        self.app = app
        self.tool_dict = {
            'app_delete_alarm': _FakeTool(
                lambda ident: app.alarm_delete(ident)),
        }


@pytest.fixture()
def alarm_app(tmp_path, monkeypatch):
    from puffpai import app_data as ad
    monkeypatch.setenv('PUFFPAI_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(ad, '_data_dir_cache', None, raising=False)
    app = ad.AppData()
    assert str(tmp_path) in app._files['alarmflow'], app._files['alarmflow']
    yield app
    ad._data_dir_cache = None


def test_cancel_the_645_am_alarm_actually_cancels_it(alarm_app, monkeypatch):
    """Verbatim: set an alarm for 6:45 am -> cancel the 6:45 am alarm."""
    from puffpai import api_server
    alarm_app.alarm_create_alarm("Alarm", 6, 45)
    monkeypatch.setattr(api_server, '_try_alarm_cancel',
                        api_server._try_alarm_cancel)

    class _App:
        pass
    monkeypatch.setattr('puffpai.app_data.AppData', lambda *a, **k: alarm_app)
    tools = _FakeTools(alarm_app)
    out = api_server._try_alarm_cancel("cancel the 6:45 am alarm", tools)
    assert out is not None
    assert "cancel" in out.lower()
    assert "6:45 AM" in out
    assert alarm_app.alarm_active_clocks() == {}


def test_cancel_by_time_is_honest_when_no_such_alarm(alarm_app, monkeypatch):
    from puffpai import api_server
    monkeypatch.setattr('puffpai.app_data.AppData', lambda *a, **k: alarm_app)
    out = api_server._try_alarm_cancel("cancel the 6:45 am alarm",
                                       _FakeTools(alarm_app))
    assert out is not None
    assert "don't have an alarm set for 6:45 AM" in out


def test_cancel_the_alarm_i_just_set(alarm_app, monkeypatch):
    from puffpai import api_server
    alarm_app.alarm_create_alarm("Alarm", 6, 45)
    monkeypatch.setattr('puffpai.app_data.AppData', lambda *a, **k: alarm_app)
    out = api_server._try_alarm_cancel("cancel the alarm i just set",
                                       _FakeTools(alarm_app))
    assert out is not None and "cancel" in out.lower()
    assert alarm_app.alarm_active_clocks() == {}


def test_cancel_all_alarms_confirms_each_one(alarm_app, monkeypatch):
    from puffpai import api_server
    alarm_app.alarm_create_alarm("Alarm", 6, 45)
    alarm_app.alarm_create_alarm("Wake up", 7, 30)
    monkeypatch.setattr('puffpai.app_data.AppData', lambda *a, **k: alarm_app)
    out = api_server._try_alarm_cancel("cancel all alarms",
                                       _FakeTools(alarm_app))
    assert out is not None
    assert "6:45 AM" in out and "7:30 AM" in out
    assert "2 alarms" in out
    assert alarm_app.alarm_active_clocks() == {}


def test_alarm_cancel_stands_down_for_timers_and_bare_phrasings(alarm_app,
                                                                monkeypatch):
    """Anything the existing dismiss/delete path already handles is untouched."""
    from puffpai import api_server
    alarm_app.alarm_create_alarm("Alarm", 6, 45)
    monkeypatch.setattr('puffpai.app_data.AppData', lambda *a, **k: alarm_app)
    tools = _FakeTools(alarm_app)
    for phrase in ("cancel the three minute timer",
                   "delete the alarm",
                   "stop the alarm",
                   "silence my alarm",
                   "what alarms do i have",
                   "cancel the pizza timer"):
        assert api_server._try_alarm_cancel(phrase, tools) is None, phrase
    assert alarm_app.alarm_active_clocks(), "an alarm was cancelled by a stand-down phrase"


def test_alarm_ids_at_ignores_timers_and_stopped_items(alarm_app):
    alarm_app.alarm_create_alarm("Alarm", 6, 45)
    assert len(alarm_app.alarm_ids_at(6, 45)) == 1
    assert alarm_app.alarm_ids_at(7, 0) == []
    assert alarm_app.alarm_ids_at("x", "y") == []


# ── radio discovery reaches a reader, not the encyclopedia ─────────────────

def test_radio_station_discovery_dispatches_to_the_media_reader():
    """Verbatim: `what radio stations can i listen to` -> Route: rag."""
    from puffpai.api_server import _CAPREAD_RULES
    phrases = [
        "what radio stations can i listen to",
        "what radio stations do you have",
        "which radio stations are available",
        "what stations do you have",
        "list the radio stations",
        "what radio can i listen to",
    ]
    for phrase in phrases:
        hit = _first_capread_rule(phrase)
        assert hit == "media_radio_stations", f"{phrase!r} -> {hit!r}"


def _first_capread_rule(text):
    from puffpai.api_server import _CAPREAD_RULES
    for tool_name, match_re, exclude_re, needs_query in _CAPREAD_RULES:
        if not match_re.search(text):
            continue
        if exclude_re is not None and exclude_re.search(text):
            continue
        return tool_name
    return None


def test_radio_rule_does_not_eat_the_other_station_words():
    """The decline corpus's station sentences, plus the near misses."""
    for phrase in ("where is the nearest gas station",
                   "what is a railway station",
                   "who invented the radio",
                   "what is the train station called",
                   "what does a police station do",
                   "how do i say (radio stations) in spanish",
                   "what spotify playlists do i have",
                   "play some jazz radio"):
        assert _first_capread_rule(phrase) != "media_radio_stations", phrase


def test_decline_corpus_still_declines_every_capread_rule():
    """The whole refusal corpus, run against the rules table itself."""
    from tests.test_capread_dispatch import DECLINE_CORPUS
    offenders = [(s, _first_capread_rule(s)) for s in DECLINE_CORPUS
                 if _first_capread_rule(s) == "media_radio_stations"]
    assert not offenders, offenders


def test_media_radio_stations_is_honest_when_the_door_is_shut(monkeypatch):
    from puffpai import media_hub
    from puffpai.security.egress_doors import EgressBlocked

    def shut(door):
        raise EgressBlocked(door, "door closed",
                            "The internet radio switch is off "
                            "(Settings > Egress).")
    monkeypatch.setattr('puffpai.security.egress_doors.require_open', shut)
    out = media_hub.tool_radio_stations("")
    assert "switch is off" in out


def test_media_radio_stations_is_honest_when_the_catalog_is_empty(monkeypatch):
    from puffpai import media_hub
    monkeypatch.setattr('puffpai.security.egress_doors.require_open',
                        lambda door: None)
    monkeypatch.setattr(media_hub, 'search_stations_by_tag',
                        lambda tag, limit=20: [])
    out = media_hub.tool_radio_stations("")
    assert "couldn't reach" in out
    assert "won't" in out


def test_media_radio_stations_names_only_stations_it_got_back(monkeypatch):
    from puffpai import media_hub
    monkeypatch.setattr('puffpai.security.egress_doors.require_open',
                        lambda door: None)
    catalog = {
        # Each station carries a stream address: a station with nothing to
        # play is dropped by the ranking before it can be named.
        "news": [{"name": "BBC World Service", "country": "UK",
                  "codec": "MP3", "bitrate": 128,
                  "url_resolved": "https://stream.example.org/world-service"}],
        "jazz": [{"name": "Adroit Jazz Underground", "country": "US",
                  "url_resolved": "https://stream.example.org/adroit-jazz"}],
        "classical": [],
        "country": [],
    }
    monkeypatch.setattr(media_hub, 'search_stations_by_tag',
                        lambda tag, limit=20: catalog.get(tag, []))
    out = media_hub.tool_radio_stations("")
    assert "BBC World Service" in out
    assert "Adroit Jazz Underground" in out
    # Nothing invented for the genres that returned nothing.
    assert "Classical:" not in out and "Country:" not in out
    # And none of the encyclopedia prose the defect produced.
    for banned in ("repeater", "ham band", "satellite dish", "listening post"):
        assert banned not in out.lower()


def test_media_radio_stations_is_bound_and_declared():
    from puffpai import capability_registry as reg
    cap = next(c for c in reg.CAPABILITIES if c.id == "streaming.radio")
    assert "media_radio_stations" in cap.also_tools


# ── the widget egress log labels what it logged, at 0640 ───────────────────

def test_widget_egress_kind_is_per_widget(tmp_path, monkeypatch):
    from puffpai import dashboard_widgets as dw
    path = str(tmp_path / "widget_egress.jsonl")
    monkeypatch.setattr(dw, '_EGRESS_LOG', path)

    dw._log_egress("https://api.open-meteo.com/v1/forecast", True,
                   "daily hi/lo", kind=dw.KIND_WEATHER)
    dw._log_egress("https://air-quality-api.open-meteo.com/v1/air-quality",
                   True, "us_aqi=40", kind=dw.KIND_AIR_QUALITY)
    dw._log_egress("https://feeds.npr.org/1001/rss.xml", True, "6 items")

    kinds = [json.loads(line)["kind"]
             for line in open(path, encoding="utf-8") if line.strip()]
    assert kinds == [dw.KIND_WEATHER, dw.KIND_AIR_QUALITY, dw.KIND_NEWS]


def test_no_weather_call_site_still_says_news():
    """Grep guard on the module, not just the helper."""
    import inspect
    from puffpai import dashboard_widgets as dw
    source = inspect.getsource(dw)
    for line in source.splitlines():
        if "_log_egress(" not in line or "def _log_egress" in line:
            continue
        if "open-meteo.com/v1/forecast" in line:
            block = source[source.index(line):source.index(line) + 300]
            assert "KIND_WEATHER" in block, line
        if "air-quality-api" in line:
            block = source[source.index(line):source.index(line) + 300]
            assert "KIND_AIR_QUALITY" in block, line


@pytest.mark.skipif(os.name == 'nt',
                    reason="POSIX mode bits are not expressible on Windows")
def test_widget_egress_log_is_0640_even_when_it_already_existed(tmp_path,
                                                               monkeypatch):
    from puffpai.security import egress_ledger
    path = str(tmp_path / "widget_egress.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"ts": "old"}\n')
    os.chmod(path, 0o644)
    egress_ledger._MODE_CHECKED.discard(path)
    assert egress_ledger.append({"kind": "widget_weather_fetch"}, path=path)
    assert stat.S_IMODE(os.stat(path).st_mode) == egress_ledger.FILE_MODE


def test_ensure_mode_is_memoised_per_path(tmp_path, monkeypatch):
    """One stat per path per process, not one per log line."""
    from puffpai.security import egress_ledger
    path = str(tmp_path / "egress.jsonl")
    egress_ledger._MODE_CHECKED.discard(path)
    calls = []
    monkeypatch.setattr(os, 'chmod', lambda p, m: calls.append(p))
    for _ in range(5):
        egress_ledger.append({"kind": "x"}, path=path)
    assert calls.count(path) <= 1
    assert path in egress_ledger._MODE_CHECKED


# ── one voice for a control action ─────────────────────────────────────────

def _control_reply(state="on", name="Office Status LED"):
    return {
        'intent': 'control_device',
        'targets': ['light.office_status_led'],
        'delegate_reply': {'ok': True, 'result': {'action': 'turn_on'}},
    }, name, state


def test_single_control_reply_uses_the_friendly_name_and_the_read_back(
        monkeypatch):
    """Verbatim: `Done - light.office_status_led turn on.`"""
    from puffpai import api_server
    reply, name, state = _control_reply()
    monkeypatch.setattr(api_server, '_observe_entity_after_action',
                        lambda eid, action: (name, state))
    out = api_server._reword_single_control_reply(
        reply, "Done — light.office_status_led turn on.")
    assert out == "Office Status LED is now on."
    assert "light.office_status_led" not in out


def test_control_reply_never_claims_more_than_the_read_back_showed(monkeypatch):
    from puffpai import api_server
    reply, name, _ = _control_reply()
    monkeypatch.setattr(api_server, '_observe_entity_after_action',
                        lambda eid, action: (name, "off"))
    out = api_server._reword_single_control_reply(reply, "Done — whatever.")
    assert "still reads off" in out


def test_control_reply_left_alone_for_multi_target_and_denials(monkeypatch):
    from puffpai import api_server
    monkeypatch.setattr(api_server, '_observe_entity_after_action',
                        lambda eid, action: ("X", "on"))
    original = "Done — something."

    two = {'intent': 'control_device', 'targets': ['a.b', 'c.d'],
           'delegate_reply': {'ok': True}}
    assert api_server._reword_single_control_reply(two, original) == original

    denied = {'intent': 'control_device', 'targets': ['a.b'],
              'delegate_reply': {'ok': True, 'denied': True}}
    assert api_server._reword_single_control_reply(denied, original) == original

    other = {'intent': 'set_climate', 'targets': ['climate.x'],
             'delegate_reply': {'ok': True}}
    assert api_server._reword_single_control_reply(other, original) == original


def test_control_reply_defers_inside_a_compound_turn(monkeypatch):
    """The compound path does its own read-back; don't do it twice."""
    from puffpai import api_server
    reply, name, state = _control_reply()
    monkeypatch.setattr(api_server, '_observe_entity_after_action',
                        lambda eid, action: (name, state))
    api_server._COMPOUND_TL.active = True
    try:
        original = "Done — light.office_status_led turn on."
        assert api_server._reword_single_control_reply(reply,
                                                       original) == original
    finally:
        api_server._COMPOUND_TL.active = False
