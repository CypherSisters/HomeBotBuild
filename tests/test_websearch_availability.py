"""Web search must report its LIVE state, not a stale status.

``capability_registry`` must not declare ``utility.websearch`` with a
static ``status=NEEDS_AUTH``: appending "(needs a
one-time setup first)" to every self-description on a box where
``chat_allow_egress`` is on, both web doors are open, and the chat fastpath
just answered a live search in 0.75 s would let the owner reasonably
conclude PuffPAI could not search the web.

An appliance that under-claims its own reach is not being modest. The owner
cannot audit egress they have been told is not happening, and they will go
and "fix" a setting that was never broken.

Four states are pinned here because the box really has four, including
``DEGRADED``: SearXNG can be up while its only configured engine returns its
anti-scraping CAPTCHA to every request. "Web search is on"
and "web search is off" are both wrong answers to that.

The engine name is never hard-coded, so the sentences below
read it live from SearXNG's ``/config`` via :func:`st.engine_name`, which
these tests monkeypatch the same way they monkeypatch the reachability
probe.

No test in this file opens a socket. The reachability probe is monkeypatched
everywhere; :func:`test_the_probe_refuses_to_leave_the_box` asserts the reason
that matters — asking "can you search the web?" must not itself be egress.

Run: pytest tests/test_websearch_availability.py -q
"""

from __future__ import annotations

import pytest

from puffpai import capability_probes, capability_registry as reg
from puffpai import websearch_status as st


@pytest.fixture(autouse=True)
def _clean_caches():
    st.reset()
    capability_probes.reset_cache()
    yield
    st.reset()
    capability_probes.reset_cache()


def _switches(monkeypatch, allowed, reason=""):
    monkeypatch.setattr(st, "_switches", lambda: (allowed, reason))


def _reachable(monkeypatch, ok):
    monkeypatch.setattr(st, "probe_searxng", lambda: ok)


def _engine(monkeypatch, name):
    monkeypatch.setattr(st, "engine_name", lambda: name)


# ── the four states ─────────────────────────────────────────────────────────

def test_live_when_the_switches_are_on_and_searxng_answers(monkeypatch):
    _switches(monkeypatch, True)
    _reachable(monkeypatch, True)
    _engine(monkeypatch, "Brave Search")
    assert st.availability() == (st.LIVE, "")
    assert st.registry_note() == ""
    assert st.probe_capability() == (True, "")
    assert st.capability_answer() == st.answer_live()


def test_off_when_the_owner_switched_it_off(monkeypatch):
    _switches(monkeypatch, False, "chat egress opt-in is off (default)")
    _reachable(monkeypatch, True)
    _engine(monkeypatch, "Brave Search")
    state, _reason = st.availability()
    assert state == st.OFF
    assert st.registry_note() == f" ({st.NOTE_OFF})"
    available, reason = st.probe_capability()
    assert available is False
    assert "Settings > Egress" in reason
    assert st.capability_answer() == st.answer_off()


def test_off_outranks_a_down_container(monkeypatch):
    """A shut door is not "unreachable" — nothing was even tried."""
    _switches(monkeypatch, False, "door closed")
    _reachable(monkeypatch, False)
    assert st.availability()[0] == st.OFF


def test_unreachable_when_searxng_does_not_answer(monkeypatch):
    _switches(monkeypatch, True)
    _reachable(monkeypatch, False)
    assert st.availability() == (st.UNREACHABLE, st.NOTE_UNREACHABLE)
    assert st.registry_note() == f" ({st.NOTE_UNREACHABLE})"
    assert st.probe_capability()[0] is False
    assert st.capability_answer() == st.ANSWER_UNREACHABLE


def test_degraded_when_the_engine_returns_nothing(monkeypatch):
    """A real box state: up, allowed, and giving zero results."""
    _switches(monkeypatch, True)
    _reachable(monkeypatch, True)
    _engine(monkeypatch, "Brave Search")
    st.note_search_outcome(0)
    assert st.availability() == (st.DEGRADED, st.note_degraded())
    assert "Brave Search" in st.registry_note()
    # Still AVAILABLE: it is switched on and wired up. Calling the whole
    # capability offline because an upstream is bot-blocking us today would
    # trade one wrong answer for another.
    available, reason = st.probe_capability()
    assert available is True
    assert "Brave Search" in reason
    assert st.capability_answer() == st.answer_degraded()


def test_a_later_success_clears_degraded(monkeypatch):
    _switches(monkeypatch, True)
    _reachable(monkeypatch, True)
    st.note_search_outcome(0)
    assert st.availability()[0] == st.DEGRADED
    st.note_search_outcome(5)
    assert st.availability()[0] == st.LIVE


def test_a_stale_empty_search_stops_speaking_for_the_present(monkeypatch):
    _switches(monkeypatch, True)
    _reachable(monkeypatch, True)
    st.note_search_outcome(0)
    assert st.availability()[0] == st.DEGRADED
    monkeypatch.setattr(st, "OUTCOME_TTL_SECONDS", -1.0)
    assert st.availability()[0] == st.LIVE


def test_no_observation_at_all_is_not_degraded(monkeypatch):
    """Absence of evidence is not evidence of breakage."""
    _switches(monkeypatch, True)
    _reachable(monkeypatch, True)
    assert st._recent_outcome() is None
    assert st.availability()[0] == st.LIVE


# ── fail direction ──────────────────────────────────────────────────────────

def test_an_unreadable_policy_fails_closed(monkeypatch):
    """A registry that will not import is not consent."""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if name == "puffpai.security.egress_policy" or name.endswith(
                "egress_policy"):
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    allowed, reason = st._switches()
    assert allowed is False
    assert reason


def test_a_failing_probe_does_not_raise_into_the_registry(monkeypatch):
    _switches(monkeypatch, True)
    monkeypatch.setattr(st, "probe_searxng",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    available, reason = capability_probes.probe("utility.websearch")
    assert available is False
    assert "RuntimeError" in reason


# ── the probe must not become egress ────────────────────────────────────────

def test_the_probe_refuses_to_leave_the_box(monkeypatch):
    """A non-loopback SearXNG address is NOT probed.

    ``describe()`` runs on dashboard polls. If ``tools.searxng_url`` pointed at
    a public host, probing it from here would turn "what can you do" into an
    outbound request on a timer.
    """
    calls = []
    monkeypatch.setattr(st, "_searxng_base", lambda: "https://search.example.com")
    monkeypatch.setattr(st, "_is_loopback_base", lambda base: False)
    monkeypatch.setattr(st, "_healthz",
                        lambda base: calls.append(base) or True)
    assert st.probe_searxng() is True
    assert calls == [], "a non-loopback address must not be probed"


def test_the_loopback_address_is_probed(monkeypatch):
    calls = []
    monkeypatch.setattr(st, "_searxng_base", lambda: "http://127.0.0.1:8888")
    monkeypatch.setattr(st, "_is_loopback_base", lambda base: True)
    monkeypatch.setattr(st, "_healthz",
                        lambda base: calls.append(base) or False)
    assert st.probe_searxng() is False
    assert calls == ["http://127.0.0.1:8888"]


def test_the_probe_answer_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(st, "_searxng_base", lambda: "http://127.0.0.1:8888")
    monkeypatch.setattr(st, "_is_loopback_base", lambda base: True)
    monkeypatch.setattr(st, "_healthz",
                        lambda base: calls.append(base) or True)
    for _ in range(25):
        st.probe_searxng()
    assert len(calls) == 1, "one health check per TTL, not one per registry row"


def test_the_probe_is_proxy_free():
    """An http_proxy in the unit's environment must not route the probe off-box."""
    import inspect
    source = inspect.getsource(st._healthz)
    assert "ProxyHandler({})" in source


# ── the registry row itself ─────────────────────────────────────────────────

def test_the_row_is_no_longer_hard_coded_needs_auth():
    cap = reg.get("utility.websearch")
    assert cap is not None
    assert cap.status == reg.LIVE, (
        "the static needs-auth is exactly what told the owner web search "
        "needed setting up while it was answering live searches")


def test_the_row_is_probed_not_declared():
    assert "utility.websearch" in capability_probes._PROBES


def test_no_setup_note_when_it_is_live(monkeypatch):
    _switches(monkeypatch, True)
    _reachable(monkeypatch, True)
    capability_probes.reset_cache()
    cap = reg.get("utility.websearch")
    assert reg.runtime_status(cap) == reg.LIVE
    assert reg._STATUS_NOTE[reg.runtime_status(cap)] == ""
    text = reg.describe("utility")
    assert "needs a one-time setup first" not in text


def test_the_off_state_says_so_in_the_spoken_description(monkeypatch):
    _switches(monkeypatch, False, "chat egress opt-in is off")
    _reachable(monkeypatch, True)
    capability_probes.reset_cache()
    text = reg.describe("utility")
    assert "Web search" in text or "web search" in text.lower()
    assert "Settings > Egress" in text


def test_the_row_still_declares_the_web_search_tool():
    """The widening must not have disturbed the tool binding."""
    cap = reg.get("utility.websearch")
    assert cap.authority_tool == "web_search"
    assert "web_search" in reg.tools_for_query("search the web for train times")


def test_the_note_names_the_ledger_without_hard_coding_an_engine():
    cap = reg.get("utility.websearch")
    assert "configured in SearXNG" in cap.note
    assert "ledger" in cap.note.lower()
    assert "DuckDuckGo" not in cap.note


def test_the_engine_name_matches_the_answer_the_owner_reads(monkeypatch):
    _engine(monkeypatch, "Brave Search")
    answer = st.answer_live()
    assert "Brave Search" in answer
    assert "egress log" in answer
    assert "SearXNG" in answer


# ── the live engine name ────────────────────────────────────────────────────

def test_engine_name_reads_brave_from_config(monkeypatch):
    monkeypatch.setattr(st, "_searxng_base", lambda: "http://127.0.0.1:8888")
    monkeypatch.setattr(st, "_is_loopback_base", lambda base: True)
    monkeypatch.setattr(
        st, "_fetch_enabled_engine_names", lambda base: ["Brave Search"])
    assert st.engine_name() == "Brave Search"


def test_engine_name_falls_back_when_config_is_unreachable(monkeypatch):
    monkeypatch.setattr(st, "_searxng_base", lambda: "http://127.0.0.1:8888")
    monkeypatch.setattr(st, "_is_loopback_base", lambda base: True)
    monkeypatch.setattr(st, "_fetch_enabled_engine_names", lambda base: None)
    assert st.engine_name() == st.FALLBACK_ENGINE_NAME


def test_engine_name_keeps_the_last_good_answer_over_a_stale_failure(
        monkeypatch):
    import time as time_mod
    monkeypatch.setattr(st, "_searxng_base", lambda: "http://127.0.0.1:8888")
    monkeypatch.setattr(st, "_is_loopback_base", lambda base: True)
    monkeypatch.setattr(
        st, "_fetch_enabled_engine_names", lambda base: ["Brave Search"])
    assert st.engine_name() == "Brave Search"
    # Force the cached answer to look expired without waiting out the TTL.
    monkeypatch.setattr(
        st, "_engine_name_cache", (time_mod.monotonic() - 1.0, "Brave Search"))
    monkeypatch.setattr(st, "_fetch_enabled_engine_names", lambda base: None)
    assert st.engine_name() == "Brave Search"
