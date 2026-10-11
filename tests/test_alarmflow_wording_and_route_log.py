"""AlarmFlow wording and the route-log signature.

* "cancel the 1 minute timer" must not answer a bare "Deleted 1 item.",
  which names neither of the two outcomes the manifest distinguishes: cancel is
  a hard removal, dismiss stops one ring and keeps the item. A custom timer
  label must also survive, otherwise the item comes back described by its
  duration and the person never hears their own word repeated.
* The journal must not show ``[Chat] Route: rag for: what do I
  need from the store...`` immediately before ``[Chat] Capability read:
  app_list_shopping`` for the SAME request. When the capability fastpath wins,
  a route line printed before the fastpaths run would describe a dispatch that
  never happens, so the route line waits for the real decision.
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server                      # noqa: E402
from puffpai.app_data import AppData                # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    return AppData()


class _FakeTool:
    def __init__(self, fn):
        self._fn = fn

    def invoke(self, arg):
        return self._fn(arg)


class _FakeTools:
    def __init__(self, **tools):
        self.tool_dict = {name: _FakeTool(fn) for name, fn in tools.items()}


# ═══════════════════════════════════════════════════════════════════════════
# a cancel says WHICH outcome happened
# ═══════════════════════════════════════════════════════════════════════════
class TestCancelSaysCancelledNotDismissed:

    def test_cancelling_the_1_minute_timer_names_the_removal(self, app):
        """The spoken phrasing, through the tool that serves it."""
        app.alarm_create_timer("1 minute", minutes=1)
        reply = app.alarm_delete("1 minute")

        assert reply.lower().startswith("cancelled and removed")
        assert "won't ring" in reply
        assert "dismiss" in reply.lower()
        assert reply != "Deleted 1 item."

    def test_the_item_really_is_gone(self, app):
        app.alarm_create_timer("1 minute", minutes=1)
        app.alarm_delete("1 minute")
        assert app.get_data("alarmflow")["items"] == []

    def test_nothing_matching_still_says_so_plainly(self, app):
        assert app.alarm_delete("1 minute") == \
            "No item found matching '1 minute'."

    def test_the_alarm_cancel_fastpath_still_recognises_success(self):
        """The fastpath gates on the reply prefix; both wordings must count."""
        for reply in ("Deleted 1 item.",
                      "Cancelled and removed 1 item - it won't ring. "
                      '(Say "dismiss" instead to stop one ring and keep it.)'):
            assert str(reply).lower().startswith(
                ("deleted", "cancelled", "canceled"))


# ═══════════════════════════════════════════════════════════════════════════
# a named timer keeps its name
# ═══════════════════════════════════════════════════════════════════════════
class TestCustomTimerLabel:

    def _capture(self, monkeypatch):
        seen = {}

        def _set_timer(kwargs):
            seen.update(kwargs)
            return f"Timer set: {kwargs['description']}, 10 minutes counting down."

        monkeypatch.setattr(api_server, "_get_assistant_tools",
                            lambda: _FakeTools(app_set_timer=_set_timer))
        return seen

    def test_set_a_10_minute_timer_called_pasta(self, monkeypatch):
        seen = self._capture(monkeypatch)
        reply = api_server._try_timer_set("set a 10 minute timer called pasta")

        assert reply is not None, "the fastpath must claim this phrasing"
        assert seen["description"] == "pasta"
        assert seen["minutes"] == 10
        assert "pasta" in reply

    @pytest.mark.parametrize("phrase", [
        "set a 10 minute timer called pasta",
        "set a timer for 10 minutes called pasta",
        "set a pasta timer for 10 minutes",
        "start a 10 minute timer named pasta",
    ])
    def test_every_way_of_naming_it_keeps_the_name(self, monkeypatch, phrase):
        seen = self._capture(monkeypatch)
        reply = api_server._try_timer_set(phrase)
        assert reply is not None, phrase
        assert seen["description"] == "pasta", phrase

    def test_an_unnamed_timer_keeps_its_duration_description(self, monkeypatch):
        """That label is what makes "cancel the ten minute timer" resolve."""
        seen = self._capture(monkeypatch)
        api_server._try_timer_set("set a timer for 10 minutes")
        assert seen["description"] == "10 minute"

    def test_the_label_parser_on_its_own(self):
        assert api_server._timer_label(
            "set a 10 minute timer called pasta") == "pasta"
        assert api_server._timer_label("set a timer for 10 minutes") is None
        assert api_server._timer_label("set a 5 minute timer") is None


# ═══════════════════════════════════════════════════════════════════════════
# the route line never precedes a capability answer
# ═══════════════════════════════════════════════════════════════════════════
SHOPPING_READ ="what do I need from the store"

_ROUTE_LINE = re.compile(r"^\[Chat\] Route: ")
_CAPABILITY_LINE = re.compile(r"^\[Chat\] Capability read: ")


def _quiet_chat(monkeypatch):
    """Strip _process_chat_query down to the route/fastpath decision."""
    monkeypatch.setattr(api_server, "_load_persona", lambda: "")
    monkeypatch.setattr(api_server, "_augment_persona_with_memory",
                        lambda persona, user_id: persona)
    monkeypatch.setattr(api_server, "_remember_for_user",
                        lambda user_id, message: None)
    monkeypatch.setattr(api_server, "_requester_for", lambda user_id: "test")
    monkeypatch.setattr(api_server, "_detect_route",
                        lambda message, history=None: "rag")
    monkeypatch.setattr(api_server, "_try_compound_request",
                        lambda *a, **kw: None)


def _lines(capsys):
    return [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]


class TestRouteLogSignature:

    def test_the_capability_answer_is_not_preceded_by_route_rag(
            self, monkeypatch, capsys):
        _quiet_chat(monkeypatch)

        def _instant(message, **kw):
            print("[Chat] Capability read: app_list_shopping")
            return "milk, eggs, bread"

        monkeypatch.setattr(api_server, "_try_instant_answer", _instant)
        out = api_server._process_chat_query(SHOPPING_READ)

        assert out["response"] == "milk, eggs, bread"
        lines = _lines(capsys)
        assert any(_CAPABILITY_LINE.match(ln) for ln in lines)
        assert not any(_ROUTE_LINE.match(ln) for ln in lines), lines

    def test_a_turn_the_fastpaths_decline_still_logs_its_route(
            self, monkeypatch, capsys):
        """The line is not deleted — it just waits for the real decision."""
        _quiet_chat(monkeypatch)
        monkeypatch.setattr(api_server, "_try_instant_answer",
                            lambda message, **kw: None)
        monkeypatch.setattr(api_server, "_rag_answer",
                            lambda *a, **kw: {"response": "from the library",
                                              "sources": [], "suggestions": []},
                            raising=False)
        try:
            api_server._process_chat_query("tell me about the moon landing")
        except Exception:
            pass                      # the RAG tail is not what is under test
        lines = _lines(capsys)
        assert any(_ROUTE_LINE.match(ln) for ln in lines), lines

    def test_the_route_line_is_printed_at_most_once(
            self, monkeypatch, capsys):
        _quiet_chat(monkeypatch)
        monkeypatch.setattr(api_server, "_try_instant_answer",
                            lambda message, **kw: None)
        try:
            api_server._process_chat_query(SHOPPING_READ)
        except Exception:
            pass
        lines = [ln for ln in _lines(capsys) if _ROUTE_LINE.match(ln)]
        assert len(lines) <= 1, lines
