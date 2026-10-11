"""The turn-writes honesty rule, driven through the REAL chat routes.

WHAT THIS CLOSES
----------------
An appliance that sets ``PUFFPAI_SCRUB_AND_VAULT=1`` in
``/etc/puffpai/api.env`` runs the flag-ON chat branch, so both branches must
keep the rule. If the ON branch handed the *scrubbed* text to the
deterministic router (``add insulin to my meds`` ->
``add [REDACTED-HEALTH] to my [REDACTED-HEALTH]``), nothing would reach
``calendar.meds[]`` and every phrasing would carry "I spotted a health detail
and did not keep it -... not saved anywhere." On the reminder phrasing that is
FALSE: a calendar task titled with those very words has just been written.

WHY THESE TESTS LOOK LIKE THIS
------------------------------
Driving a **synthesised** pipeline (``_prepare_user_message`` ->
``_snapshot_counts`` -> ``_verify_and_fix`` -> ``_apply_pii_honesty``) by hand
proves nothing: **no chat route runs that sequence**. ``_fix_add_med`` is
reachable only through ``_verify_and_fix``, ``_detect_route`` sends "add
insulin to my meds" down the **rag** path, and the rag path returns the model's
text directly, so such a pipeline can pass while four of five surfaces write
nothing at all.

So every behavioural test below drives the **real Flask route**, on all five
surfaces, under both flag states, and asserts three things together: what the
reply says, what ``calendar.json`` holds, and what conversation memory kept.
**Nothing here calls ``_verify_and_fix``.**

Fake data only, per the standing rules. No household names anywhere.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from puffpai import app_data as app_data_mod  # noqa: E402


def _clear_every_app_data_dir_cache(monkeypatch):
    """Reset the resolved app-data directory on EVERY copy of the module.

    ``assistant_tools`` does ``from app_data import AppData`` - the TOP-LEVEL module,
    a different object from ``puffpai.app_data`` with its own ``_data_dir_cache``.
    Clearing only the package one left the AssistantTools instance (and therefore
    every ``_try_*`` fastpath write) pointed at the FIRST test's tmp dir, so the
    first parametrised case passed and every later one silently wrote somewhere
    else. Found the hard way; do not delete one of these two lines.
    """
    seen = 0
    for name, mod in list(sys.modules.items()):
        if name in ("app_data", "puffpai.app_data") and mod is not None:
            monkeypatch.setattr(mod, "_data_dir_cache", None, raising=False)
            seen += 1
    return seen

#: The sentence this file exists to keep out of a turn that DID keep something.
NOT_KEPT = "did not keep it"

#: Structurally valid, not a published fake - so the quality gate vaults it and
#: the flag-ON path really does mint a handle. Same constant as
#: tests/test_scrub_and_vault.py.
GOOD_SSN = "123-45-6788"

#: What the stubbed model says. Deliberately a false storage claim: it is what
#: the honesty pass has to correct when nothing was stored, and what the
#: deterministic fastpath has to REPLACE when something was.
MODEL_REPLY = "Medication added."

#: The five chat surfaces a household can reach. All of them, every time: the
#: defect this guards against is one surface behaving unlike the other four.
SURFACES = ["/chat/send", "/conversation", "/chat/stream",
            "/v1/chat/completions", "/api/chat"]


@pytest.fixture(autouse=True)
def _no_turn_leaks_out_of_this_file():
    """Both per-turn records are thread-local and pytest runs on one thread."""
    from puffpai.security import turn_writes, turn_health
    turn_writes.end_turn()
    turn_health.end_turn()
    yield
    turn_writes.end_turn()
    turn_health.end_turn()


class _Wired:
    """A live Flask test client on a throwaway data dir, at one flag state."""

    def __init__(self, api, client, headers, flag, tmp_path):
        self.api = api
        self.client = client
        self.headers = headers
        self.flag = flag
        self.tmp_path = tmp_path

    @property
    def on(self):
        return self.flag == "on"

    # ── observation helpers ────────────────────────────────────────────
    def calendar(self):
        path = self.tmp_path / "state" / "app_data" / "calendar.json"
        if not path.exists():
            return {"meds": [], "tasks": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def meds(self):
        return [m.get("name") for m in self.calendar().get("meds", [])]

    def tasks(self):
        return [t.get("title") for t in self.calendar().get("tasks", [])]

    def persisted(self):
        out = []
        for conv in self.api._chat_history:
            for msg in conv.get("messages", []):
                out.append((msg["role"], msg["content"]))
        return out

    def persisted_user(self):
        return [c for r, c in self.persisted() if r == "user"]

    def persisted_assistant(self):
        return [c for r, c in self.persisted() if r == "assistant"]

    def reset(self):
        """Empty the calendar and the transcript between surfaces."""
        path = self.tmp_path / "state" / "app_data" / "calendar.json"
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["meds"] = []
            doc["tasks"] = []
            doc["events"] = []
            path.write_text(json.dumps(doc), encoding="utf-8")
        self.api._chat_history.clear()

    # ── the driver ─────────────────────────────────────────────────────
    def say(self, surface, message):
        """Post one turn to a real route and return the assistant's reply.

        Every surface's own request/response shape, so a test cannot pass by
        accidentally exercising a different one than it names.
        """
        c, h = self.client, self.headers
        if surface == "/chat/send":
            r = c.post("/chat/send", json={"message": message}, headers=h)
            assert r.status_code == 200, r.get_data(as_text=True)[:200]
            return (r.get_json() or {}).get("response", "")
        if surface == "/conversation":
            r = c.post("/conversation", json={"text": message}, headers=h)
            assert r.status_code == 200, r.get_data(as_text=True)[:200]
            return (r.get_json() or {}).get("response", "")
        if surface == "/chat/stream":
            r = c.post("/chat/stream", json={"message": message}, headers=h)
            return "".join(e.get("delta", "") for e in _sse(r))
        if surface == "/v1/chat/completions":
            # The dashboard Chat page's shape: it saves its own turns back
            # through /chat/save, and says so.
            r = c.post("/v1/chat/completions", headers=h,
                       json={"model": "puffpai", "stream": True,
                             "client_saves": True,
                             "messages": [{"role": "user", "content": message}]})
            return "".join(
                e["choices"][0].get("delta", {}).get("content", "")
                for e in _sse(r) if e.get("choices"))
        if surface == "/api/chat":
            r = c.post("/api/chat", headers=h,
                       json={"messages": [{"role": "user", "content": message}]})
            assert r.status_code == 200, r.get_data(as_text=True)[:200]
            return (r.get_json() or {}).get("message", {}).get("content", "")
        raise AssertionError("unknown surface %r" % surface)


def _sse(response):
    """Parse a text/event-stream body into its JSON events."""
    events = []
    for line in response.get_data(as_text=True).splitlines():
        if line.startswith("data: ") and line[6:].strip() != "[DONE]":
            try:
                events.append(json.loads(line[6:]))
            except ValueError:
                pass
    return events


class _StubTools:
    """Stands in for AssistantTools on the /conversation agent path only.

    Constructed by ``conversation`` at call time. The cached instance the
    ``_try_*`` fastpaths use is warmed BEFORE this patch lands, so the
    deterministic writes still go through the real tools - which is the whole
    point of the test.
    """

    def __init__(self, *a, **k):
        pass

    def run(self, message, conversation_history=None):
        return MODEL_REPLY


@pytest.fixture(params=["off", "on"], ids=["flag_off", "flag_on"])
def wired(request, tmp_path, monkeypatch):
    """A real Flask client on a throwaway data dir, parametrised over the flag.

    Four caches have to go, or a test inherits the previous test's data dir:
    ``app_data._data_dir_cache``, ``api_server._app_data_instance``,
    ``api_server._assistant_tools_instance`` (its tools hold an ``AppData``) and the
    in-memory ``_chat_history``.

    For the ON case the go-live interlock needs BOTH the flag and an enrolled
    PIN-recovery code - minting one is what actually flips
    ``scrub_and_vault_active`` - and the assert proves the parametrised case is
    really exercising the branch it claims to rather than silently degrading to
    the legacy one.
    """
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(app_data_mod, "_data_dir_cache", None)
    _clear_every_app_data_dir_cache(monkeypatch)
    import puffpai.api_server as api_server
    import puffpai.assistant_tools as assistant_tools
    from puffpai.security import scrub_and_vault as sav
    monkeypatch.setattr(api_server, "_app_data_instance", None)
    monkeypatch.setattr(api_server, "_assistant_tools_instance", None)
    sav.invalidate_recovery_code_cache()

    if request.param == "on":
        from puffpai.security.vault import Vault
        monkeypatch.setenv("PUFFPAI_SCRUB_AND_VAULT", "1")
        vault = Vault(vault_dir=str(tmp_path / "vault"))
        vault.initialize("vault-pin-1234")
        session = vault.authenticate("vault-pin-1234")
        vault.generate_recovery_code(session.session_id)
        monkeypatch.setattr(api_server, "_get_vault", lambda: vault)
        assert sav.scrub_and_vault_active(vault=vault) is True
    else:
        monkeypatch.delenv("PUFFPAI_SCRUB_AND_VAULT", raising=False)
        assert sav.scrub_and_vault_active() is False

    # Every model sink stubbed: these tests must never touch a backend, and a
    # deterministic reply is what makes "the fastpath REPLACED the model's
    # false claim" an assertable statement.
    monkeypatch.setattr(api_server, "_query_ollama_direct",
                        lambda *a, **k: MODEL_REPLY)
    monkeypatch.setattr(api_server, "_stream_ollama_tokens",
                        lambda *a, **k: iter([MODEL_REPLY]))
    monkeypatch.setattr(api_server, "_process_chat_query",
                        lambda m, h=None, **k: {
                            'response': api_server._try_instant_answer(m)
                                        or MODEL_REPLY,
                            'sources': [], 'suggestions': []})
    monkeypatch.setattr(api_server, "_process_voice_query",
                        lambda m, h=None, **k:
                        api_server._try_instant_answer(m) or MODEL_REPLY)

    # Warm the cached tools against THIS test's data dir, then stub the class
    # so /conversation's agent path is offline and deterministic.
    api_server._get_assistant_tools()
    monkeypatch.setattr(assistant_tools, "AssistantTools", _StubTools)

    history = list(api_server._chat_history)
    api_server._chat_history.clear()
    client = api_server.app.test_client()
    headers = {"X-API-Key": api_server._API_KEY}
    yield _Wired(api_server, client, headers, request.param, tmp_path)
    api_server._chat_history.clear()
    api_server._chat_history.extend(history)
    sav.invalidate_recovery_code_cache()


# ══════════════════════════════════════════════════════════════════════════
# The medication must land on EVERY surface, not just /conversation
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("surface", SURFACES)
def test_add_insulin_to_my_meds_lands_on_every_surface(wired, surface):
    """`_fix_add_med` is reachable only through `_verify_and_fix`, and
    `_detect_route` sends this phrasing down the rag path, so without the
    `_try_med_add` fastpath four of five surfaces write nothing at all, on BOTH
    flag states, while every reply says "Medication added." regardless."""
    wired.reset()
    reply = wired.say(surface, "add insulin to my meds")

    assert wired.meds() == ["insulin"], (surface, wired.meds())
    assert "insulin" in reply
    # Nothing may claim it kept nothing on a turn that kept the medication.
    assert NOT_KEPT not in reply, (surface, reply)


@pytest.mark.parametrize("surface", SURFACES)
def test_the_medication_is_written_exactly_once(wired, surface):
    """Two code paths can serve a med turn (`_try_instant_answer` and the
    streaming-RAG pre-check). Exactly one may reach any given turn."""
    wired.reset()
    wired.say(surface, "add insulin to my meds")
    assert wired.meds() == ["insulin"], (surface, wired.meds())


@pytest.mark.parametrize("surface", SURFACES)
def test_add_a_medication_called_metformin(wired, surface):
    wired.reset()
    reply = wired.say(surface, "add a medication called Metformin")
    assert wired.meds() == ["Metformin"], (surface, wired.meds())
    assert NOT_KEPT not in reply


@pytest.mark.parametrize("surface", SURFACES)
def test_what_meds_do_i_have_reads_the_list(wired, surface):
    """A read, answered from the file rather than guessed by a model."""
    wired.reset()
    wired.say(surface, "add insulin to my meds")
    reply = wired.say(surface, "what meds do I have")
    assert "insulin" in reply, (surface, reply)
    # A read must not write.
    assert wired.meds() == ["insulin"]


@pytest.mark.parametrize("surface", SURFACES)
def test_i_took_my_medicine_answers_from_the_list(wired, surface):
    """Nothing is stored, so the notice - when the flag makes one - is TRUE,
    and the deterministic answer replaces the model's false claim."""
    wired.reset()
    reply = wired.say(surface, "I took my medicine")

    assert wired.meds() == []
    assert wired.tasks() == []
    # The honest deterministic answer, not "Medication added." It names
    # nothing: echoing the user's words back ("No medication found matching
    # 'medicine'.") can read a real drug name out loud. _try_med_log owns this
    # shape. The property under test: nothing stored, and a deterministic
    # honest reply rather than the model's claim.
    assert "don't have that medication on file" in reply, (surface, reply)
    assert "medicine" not in reply, (surface, reply)
    if wired.on and surface != "/v1/chat/completions":
        # /v1/chat/completions does not apply the honesty pass on a
        # non-fastpath turn.
        assert NOT_KEPT in reply, (surface, reply)


# ══════════════════════════════════════════════════════════════════════════
# The reminder must keep working: it is the load-bearing write
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("surface", SURFACES)
def test_remind_me_to_take_my_blood_pressure_pill_at_8(wired, surface):
    """`_try_add_task` owns this phrasing and runs BEFORE the med fastpath.
    The med rules must never steal a reminder - and the task title must carry
    the real words, with no "not saved anywhere" over the top of it."""
    wired.reset()
    reply = wired.say(surface, "remind me to take my blood pressure pill at 8")

    titles = wired.tasks()
    assert titles and "blood pressure pill" in titles[0], (surface, titles)
    assert "REDACTED" not in titles[0]
    assert wired.meds() == [], (surface, wired.meds())
    assert NOT_KEPT not in reply, (surface, reply)


@pytest.mark.parametrize("surface", SURFACES)
def test_i_have_asthma_stores_nothing(wired, surface):
    """The control: nothing written anywhere, so a notice is honest."""
    wired.reset()
    reply = wired.say(surface, "I have asthma")

    assert wired.meds() == []
    assert wired.tasks() == []
    # A health detection may only ever PREPEND - the answer survives.
    assert MODEL_REPLY in reply, (surface, reply)
    if wired.on and surface != "/v1/chat/completions":
        assert NOT_KEPT in reply, (surface, reply)


# ══════════════════════════════════════════════════════════════════════════
# A disclosure in the same breath costs a repeat
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("surface", SURFACES)
def test_a_disclosure_in_the_same_message_blocks_the_medication(wired, surface):
    """This phrasing DELIBERATELY stores no `vitamin`, and this test is the
    record of the trade.

    Clause-scoped screening that tries to keep both promises is walked past by
    reordering, for example "my date of birth is January 2, 1990, add
    01/02/1990 to my meds", which would store an unencrypted date of birth on
    five surfaces while replying "not saved anywhere". One fail-closed rule
    replaces the heuristics: any non-health PII anywhere in the message means
    no medication is stored.

    The cost is exactly this test. Mixing a disclosure and a medication in one
    breath costs the household ONE repeat, the reply says so, and the turn's PII
    notice stays TRUE because nothing was stored. Weighed against a false
    "not saved anywhere" over a stored DOB, a repeat is cheap.
    """
    wired.reset()
    reply = wired.say(surface, "my SSN is 219-09-9999, add my vitamin to my meds")

    assert wired.meds() == [], (surface, wired.meds())
    assert "personal details" in reply, (surface, reply)
    # The APP DOCUMENT and the assistant turn. NOT the user turn: /api/chat at
    # flag OFF persists what was typed, a separate transcript contract.
    assert "219-09-9999" not in json.dumps(wired.calendar()), surface
    assert "219-09-9999" not in " ".join(wired.persisted_assistant()), surface
    if wired.on:
        assert "Social Security number" in reply, (surface, reply)


@pytest.mark.parametrize("surface", SURFACES)
def test_the_redaction_refusal_is_said_once(wired, surface):
    """If Case 1 of `_verify_and_fix` appended the decline without marking a
    correction, Case 3 would run the same fix and append the identical
    sentence a second time."""
    wired.reset()
    reply = wired.say(surface, "add [REDACTED-HEALTH] to my meds")
    assert reply.count("I did not add that to your medication list") <= 1, reply
    assert wired.meds() == [], (surface, wired.meds())


def test_a_placeholder_never_becomes_a_medication_name(wired):
    """Direct assertion on the rule, independent of any surface."""
    assert wired.api._try_med_add("add [REDACTED-HEALTH] to my meds") is not None
    assert "did not add" in wired.api._try_med_add(
        "add [REDACTED-HEALTH] to my meds")
    assert wired.meds() == []


# ══════════════════════════════════════════════════════════════════════════
# Conversation memory - both halves of the turn, under the flag
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("surface", ["/chat/send", "/conversation",
                                     "/chat/stream", "/api/chat"])
def test_conversation_memory_never_stores_a_health_word_under_the_flag(
        wired, surface):
    """The at-rest half. Routing moved; persistence did not.

    `/v1/chat/completions` is excluded because it persists nothing at all to
    conversation memory - asserted separately below rather than assumed.
    """
    for message, term in (
            ("add insulin to my meds", "insulin"),
            ("I took my medicine", "medicine"),
            ("remind me to take my blood pressure pill at 8", "blood pressure"),
            ("I have asthma", "asthma"),
    ):
        wired.reset()
        wired.say(surface, message)
        blob = json.dumps(wired.persisted())
        if wired.on:
            assert term not in blob, (surface, message, blob[:200])
            assert "REDACTED-HEALTH" in blob, (surface, message)
        else:
            assert term in blob, (surface, message)


def test_v1_chat_completions_persists_nothing_to_conversation_memory(wired):
    """Pinned, not assumed: it is why the surface is excluded above."""
    wired.reset()
    wired.say("/v1/chat/completions", "add insulin to my meds")
    assert wired.persisted() == []


@pytest.mark.parametrize("surface", ["/chat/send", "/conversation",
                                     "/chat/stream", "/api/chat"])
def test_the_assistant_turn_is_scrubbed_but_the_reply_is_not(wired, surface):
    """Honesty is not memory: the household reads the drug name, the stored
    copy does not carry it."""
    wired.reset()
    reply = wired.say(surface, "add insulin to my meds")
    assert "insulin" in reply, (surface, reply)
    stored = " ".join(wired.persisted_assistant())
    if wired.on:
        assert "insulin" not in stored, (surface, stored)
        assert "REDACTED-HEALTH" in stored, (surface, stored)
    else:
        assert "insulin" in stored, (surface, stored)


def test_the_voice_transcript_carries_the_corrected_reply(wired):
    """`_voice_full_tl.text` is stashed inside `_process_voice_query`, BEFORE
    the honesty pass, and is always truthy on a voice turn, so the transcript
    must be updated too or it stores the UNCORRECTED claim while the speaker
    hears the corrected one."""
    if not wired.on:
        pytest.skip("the notice only exists under the flag")
    wired.reset()
    spoken = wired.say("/api/chat", "I have asthma")
    assert NOT_KEPT in spoken
    stored = " ".join(wired.persisted_assistant())
    assert NOT_KEPT in stored, stored


# ══════════════════════════════════════════════════════════════════════════
# Bounds that must survive all of the above
# ══════════════════════════════════════════════════════════════════════════

def test_an_ssn_notice_still_replaces_a_false_storage_claim(wired):
    """ is untouched by the health and medication work."""
    api = wired.api
    meta = {'detected': True, 'types': ['ssn'], 'vaulted': [],
            'summary': '1 SSN(s)', 'severity': 'critical'}
    notice = api._pii_honesty_notice(meta)
    assert notice and NOT_KEPT in notice
    assert api._apply_pii_honesty(
        "Got it - your SSN has been noted.", meta) == notice


def test_an_app_write_can_never_silence_an_ssn_notice(wired):
    """The filter is bounded by construction: `turn_health` can only ever add
    `health.condition`, and `app_data_tags` only four classes."""
    api = wired.api
    from puffpai.security import turn_writes, turn_health
    turn_writes.begin_turn()
    turn_health.begin_turn()
    turn_health.arm(["insulin"])
    api._get_app_data().cal_add_med("insulin", "1 dose", "all")
    assert turn_health.health_was_stored() is True

    meta = {'detected': True, 'types': ['ssn', 'health.condition'],
            'vaulted': [], 'summary': '1 SSN(s)', 'severity': 'critical'}
    notice = api._pii_honesty_notice(meta)
    assert notice and "Social Security number" in notice
    assert "health detail" not in notice


def test_the_flag_on_routing_text_still_never_carries_a_raw_ssn(wired):
    """The screening order differs for health only. The seven detectors still scrub
    and vault BEFORE either text exists, so the LLM sink is not weakened."""
    if not wired.on:
        pytest.skip("the vault contract is the flag-ON path")
    llm_text, persist_text, meta = wired.api._prepare_user_message(
        f"my ssn is {GOOD_SSN}", source="chat")
    assert GOOD_SSN not in llm_text
    assert GOOD_SSN not in persist_text
    assert "{{vault:" in llm_text
    assert meta and meta["handles"] == 1


def test_the_meta_summary_agrees_with_its_own_severity(wired):
    """Appending the mention to an already-formatted summary and raising the
    severity afterwards produces
    `PII detected [medium]: 1 email(s), 1 health mention(s)` beside severity
    `high`. `summary` is handed to the frontend in `response_data['pii']`."""
    if not wired.on:
        pytest.skip("the merged summary only exists on the flag-ON path")
    _llm, _persist, meta = wired.api._prepare_user_message(
        "I have asthma. My email is nobody@example.invalid.", source="chat")
    assert meta["severity"] == "high"
    assert meta["summary"].startswith("PII detected [high]:"), meta["summary"]
    assert "health mention" in meta["summary"]
    # A scrubbed-never-vaulted health span belongs in this count, where the
    # pre-fix include_health=True call already put it.
    assert meta["scrubbed_not_vaulted"] >= 1


# ══════════════════════════════════════════════════════════════════════════
# The medication fastpath's own edges
# ══════════════════════════════════════════════════════════════════════════

def test_a_walk_with_the_dog_is_not_a_medication(wired):
    """`_try_med_take` fires only on a medication NOUN or a stored medication.
    Without that, "I took my dog for a walk" became a medication lookup."""
    assert wired.api._try_med_take("I took my dog for a walk") is None


def test_took_fires_once_the_medication_is_actually_stored(wired):
    wired.reset()
    wired.api._get_app_data().cal_add_med("insulin", "1 dose", "all")
    out = wired.api._try_med_take("I took my insulin")
    assert out and "insulin" in out


def test_the_med_fastpath_never_steals_a_reminder(wired):
    """The ordering rule, asserted directly: _try_add_task runs first."""
    assert wired.api._try_med_add(
        "remind me to take my blood pressure pill at 8") is None


@pytest.mark.parametrize("phrase,expected", [
    ("add insulin to my meds", "insulin"),
    ("add a medication called Metformin", "Metformin"),
    ("set a medication reminder for warfarin", "warfarin"),
    ("put my vitamin on the medication list", "vitamin"),
    ("my SSN is 219-09-9999, add my vitamin to my meds", "vitamin"),
])
def test_the_extracted_name_is_the_drug_not_the_sentence(wired, phrase,
                                                         expected):
    assert wired.api._extract_med_name(phrase) == expected


@pytest.mark.parametrize("phrase", [
    "what meds do I have",
    "list my medications",
    "show me my meds",
    "how many medications do I have",
])
def test_a_read_never_writes(wired, phrase):
    assert wired.api._try_med_add(phrase) is None


@pytest.mark.parametrize("phrase", [
    "remove insulin from my meds",
    "delete my medication",
    "stop my medication reminder",
])
def test_a_delete_never_adds(wired, phrase):
    assert wired.api._try_med_add(phrase) is None


# ══════════════════════════════════════════════════════════════════════════
# turn_health's own contract - no value ever escapes
# ══════════════════════════════════════════════════════════════════════════

def test_the_health_record_exposes_no_value(wired):
    from puffpai.security import turn_health
    turn_health.begin_turn()
    turn_health.arm(["insulin"])
    turn_health.note_write("calendar", {"meds": [{"name": "insulin"}]})
    assert turn_health.health_was_stored() is True
    assert "insulin" not in repr(turn_health.__all__)


def test_a_write_without_the_turns_terms_does_not_mark_it_kept(wired):
    from puffpai.security import turn_health
    turn_health.begin_turn()
    turn_health.arm(["insulin"])
    turn_health.note_write("alarmflow", {"alarms": [{"label": "wake up"}]})
    assert turn_health.health_was_stored() is False


def test_a_background_write_cannot_mark_a_chat_turns_health_kept(wired):
    """The alarm scheduler and the sync poller write on their own threads."""
    import threading
    from puffpai.security import turn_health
    turn_health.begin_turn()
    turn_health.arm(["insulin"])

    def background():
        turn_health.note_write("calendar", {"meds": [{"name": "insulin"}]})

    thread = threading.Thread(target=background)
    thread.start()
    thread.join()
    assert turn_health.health_was_stored() is False


def test_beginning_a_turn_discards_the_previous_turns_health_record(wired):
    from puffpai.security import turn_health
    turn_health.begin_turn()
    turn_health.arm(["insulin"])
    turn_health.note_write("calendar", {"meds": [{"name": "insulin"}]})
    assert turn_health.health_was_stored() is True
    turn_health.begin_turn()
    assert turn_health.health_was_stored() is False


def test_the_term_match_is_word_bounded_and_case_insensitive(wired):
    from puffpai.security import turn_health
    turn_health.begin_turn()
    turn_health.arm(["insulin"])
    turn_health.note_write("calendar", {"tasks": [{"title": "Insulin at 8"}]})
    assert turn_health.health_was_stored() is True

    turn_health.begin_turn()
    turn_health.arm(["meds"])
    turn_health.note_write("calendar", {"tasks": [{"title": "medsafe order"}]})
    assert turn_health.health_was_stored() is False


def test_an_unarmed_turn_keeps_the_notice(wired):
    """Fail-safe direction: nothing proven kept means the honest notice stands."""
    from puffpai.security import turn_health
    turn_health.begin_turn()
    turn_health.note_write("calendar", {"meds": [{"name": "insulin"}]})
    assert turn_health.health_was_stored() is False


def test_split_for_persistence_leaves_routing_text_alone(wired):
    from puffpai.security import turn_health
    turn_health.begin_turn()
    routing = "add insulin to my meds"
    persisted, terms = turn_health.split_for_persistence(routing)
    assert routing == "add insulin to my meds"
    assert "insulin" not in persisted
    assert "[REDACTED-HEALTH]" in persisted
    assert terms


# ══════════════════════════════════════════════════════════════════════════
# The assistant-turn scrubber
# ══════════════════════════════════════════════════════════════════════════

MED_REPLY = ("Medication added.\n\nConfirmed done: medication 'insulin' added "
             "to your calendar's medication list.")


def test_every_persistence_path_uses_the_same_scrubber(wired):
    """A grep-shaped test on purpose: the defect it closes was a site that had
    simply never been wired, and a behavioural test on one route would not
    catch the next unwired one."""
    import inspect
    src = inspect.getsource(wired.api)
    # One save point
    # (_append_thread_turn); the two pairing close-report lines post a fixed
    # assistant-only status line, not a turn, and are the only others.
    appends = [ln for ln in src.splitlines()
               if "'role': 'assistant', 'content'" in ln
               and "'content': text," not in ln]
    assert len(appends) == 1, appends
    assert [ln for ln in appends
            if "_scrub_assistant_for_persistence(" not in ln] == []


def test_the_assistant_scrub_fails_closed(wired, monkeypatch):
    if not wired.on:
        pytest.skip("the scrub only runs under the flag")

    class _Boom:
        def redact_pii(self, *a, **k):
            raise RuntimeError("scan unavailable")

    monkeypatch.setattr(wired.api, "_get_pii_classifier", lambda: _Boom())
    stored = wired.api._scrub_assistant_for_persistence(MED_REPLY)
    assert "insulin" not in stored
    assert stored == "[redacted: PII scan unavailable]"


def test_an_empty_assistant_turn_is_left_alone(wired):
    assert wired.api._scrub_assistant_for_persistence("") == ""
    assert wired.api._scrub_assistant_for_persistence(None) is None


def test_the_streaming_notice_is_computed_off_the_generator(wired):
    """The records it reads are thread-local and a Flask generator is not
    guaranteed to run on the request thread. The bug this prevents is silent:
    an empty record reads as "nothing was stored", which keeps a notice that
    may be false."""
    import inspect
    src = inspect.getsource(wired.api.chat_stream)
    assert (src.index("_stream_notice = _pii_honesty_notice(pii_meta)")
            < src.index("def generate_stream():"))


# ══════════════════════════════════════════════════════════════════════════
# A medication NAME is screened before it is ever stored
# ══════════════════════════════════════════════════════════════════════════
#
# On the flag-OFF path `_scan_and_redact_message` redacts only the copy that is
# PERSISTED; the routing text stays raw. So `add <SSN> to my meds` hands the
# fastpath a real SSN, and the placeholder guard (which knows only
# `[REDACTED-…]` / `{{vault:`) does not fire. Without the name screen the
# number would be written into calendar.json, unencrypted (0600 is a file mode,
# not a cipher), while the honesty notice says "I did not keep it". A false
# "not kept" over a STORED SSN is the worst version of the defect this file
# exists to close.

#: Structurally valid, not a published fake — the detector must actually see it.
SCREEN_SSN = "123-45-6788"
SCREEN_EMAIL = "nobody@example.invalid"


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("phrase", [
    "add %s to my meds" % SCREEN_SSN,
    "add a medication called %s" % SCREEN_SSN,
    "add %s to my meds" % SCREEN_EMAIL,
    "add a medication called %s" % SCREEN_EMAIL,
])
def test_personal_information_is_never_stored_as_a_medication_name(
        wired, surface, phrase):
    """Nothing stored, on every surface and BOTH flag states — and the value
    reaches no app document at all."""
    wired.reset()
    reply = wired.say(surface, phrase)

    assert wired.meds() == [], (surface, phrase, wired.meds())
    document = json.dumps(wired.calendar())
    assert SCREEN_SSN not in document, (surface, phrase)
    assert SCREEN_EMAIL not in document, (surface, phrase)
    # The turn declines out loud rather than silently doing nothing.
    assert ("store that as a medication name" in reply
            or "did not add that to your medication list" in reply), \
        (surface, phrase, reply)


#: The voice route does not scan under flag OFF, so there is no pii_meta and
#: therefore no notice to check there. Adding a scan to that path would change
#: a surface outside this defect.
NOTICE_SURFACES_OFF = ["/chat/send", "/conversation", "/chat/stream"]


@pytest.mark.parametrize("surface", NOTICE_SURFACES_OFF)
def test_the_not_kept_notice_is_true_because_nothing_was_stored(wired,
                                                                surface):
    """The two sentences have to agree. Under OFF the honesty notice fires for
    the SSN and it is TRUE; the refusal explains why, in the same reply."""
    wired.reset()
    reply = wired.say(surface, "add %s to my meds" % SCREEN_SSN)
    assert wired.meds() == []
    if not wired.on:
        assert NOT_KEPT in reply, (surface, reply)
        assert "store that as a medication name" in reply, (surface, reply)
    assert SCREEN_SSN not in json.dumps(wired.persisted()), surface


def test_the_voice_route_still_stores_nothing_even_without_a_notice(wired):
    """`/api/chat` under OFF has no pii_meta at all, so no notice — but the
    screen is not part of the notice machinery, and the SSN must still reach no
    app document. Pinned so the gap above cannot quietly become a leak."""
    wired.reset()
    reply = wired.say("/api/chat", "add %s to my meds" % SCREEN_SSN)
    assert wired.meds() == []
    assert SCREEN_SSN not in json.dumps(wired.calendar())
    # Under ON the value is already redacted before routing, so the placeholder
    # refusal is what fires; under OFF the PII screen is. Either is a decline.
    assert ("store that as a medication name" in reply
            or "did not add that to your medication list" in reply), reply


def test_the_screen_covers_every_write_path_into_the_med_list(wired):
    """Both entry points, asserted directly.

    `_verify_and_fix`'s Case 3 calls `_fix_add_med` AFTER the fastpath has
    already declined, so screening only the fastpath left /conversation writing
    the value anyway. One screen, every write path into calendar.meds[].
    """
    api = wired.api
    assert "store that as a medication name" in (api._try_med_add(
        "add %s to my meds" % SCREEN_SSN) or "")
    out = api._fix_add_med("add %s to my meds" % SCREEN_SSN, api._get_app_data())
    assert out and out.startswith("!")
    assert "store that as a medication name" in out
    assert wired.meds() == []


def test_the_screen_fails_closed_when_the_classifier_cannot_run(wired,
                                                                monkeypatch):
    """A refused add costs one repeat; a stored SSN does not come back out of
    a backup."""
    class _Boom:
        def scan_for_pii(self, *a, **k):
            raise RuntimeError("classifier unavailable")

    monkeypatch.setattr(wired.api, "_get_pii_classifier", lambda: _Boom())
    assert "store that as a medication name" in (
        wired.api._med_name_refusal("insulin") or "")


def test_an_ordinary_medication_still_passes_the_screen(wired):
    """The screen must not refuse the thing it exists to protect.

    ``include_health`` is deliberately OFF inside it: a medication name is
    supposed to be a health term, and screening for that would refuse every
    legitimate add.
    """
    for name in ["insulin", "Metformin", "warfarin", "vitamin D", "Advair"]:
        assert wired.api._med_name_refusal(name) is None, name


# ══════════════════════════════════════════════════════════════════════════
# A fastpath-written drug name is length-bounded
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("surface", SURFACES)
def test_a_600_character_name_is_refused(wired, surface):
    """600 characters of prose is a parser failure, not a prescription."""
    wired.reset()
    reply = wired.say(surface, "add %s to my meds" % ("x" * 600))
    assert wired.meds() == [], (surface, wired.meds())
    assert reply, surface


def test_the_length_bound_is_64_characters(wired):
    api = wired.api
    assert api._med_name_refusal("a" * 64) is None
    assert "too long to be a medication name" in (
        api._med_name_refusal("a" * 65) or "")


# ══════════════════════════════════════════════════════════════════════════
# The medication-list turn
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("surface", SURFACES)
def test_the_med_list_reply_keeps_both_safety_hedges(wired, surface):
    """The fastpath is registered ahead of `_try_household_read` and is
    called directly on the branches that never reach `_try_instant_answer`, so
    it preempts `household_reads.medication_status`, which is not a thin
    wrapper around `cal_list_meds`: it carries two deliberate medication
    hedges. The reply is DELEGATED to that function, so the wording has
    exactly one owner and the hedges survive on both flag states.
    """
    wired.reset()
    empty = wired.say(surface, "what meds do I have")
    assert "Please don't treat that as confirmation" in empty, (surface, empty)
    assert "take one" in empty, (surface, empty)

    wired.say(surface, "add insulin to my meds")
    listed = wired.say(surface, "what meds do I have")
    assert "I can only report what has been logged" in listed, (surface, listed)
    assert "insulin" in listed, (surface, listed)


@pytest.mark.parametrize("surface", ["/chat/send", "/conversation",
                                     "/chat/stream", "/api/chat"])
def test_the_med_list_turn_never_persists_a_stored_drug_name_under_the_flag(
        wired, surface):
    """`redact_turn_terms` knows only what THIS turn disclosed; "what meds do
    I have" discloses no drug, so a reply naming one read out of calendar.json
    would go into conversation memory in the clear: closed for the turn where
    the user says the drug and open for the turn where the appliance reads it
    back. Screening against the STORED names closes it for any reply that
    echoes a medication on file, whichever fastpath, tool or model produced it.
    """
    wired.reset()
    wired.say(surface, "add insulin to my meds")
    wired.api._chat_history.clear()
    listed = wired.say(surface, "what meds do I have")

    assert "insulin" in listed, (surface, listed)   # the household still reads it
    blob = json.dumps(wired.persisted())
    if wired.on:
        assert "insulin" not in blob, (surface, blob[:300])
        assert "REDACTED-HEALTH" in blob, (surface, blob[:300])
    else:
        assert "insulin" in blob, surface


def test_the_stored_name_scrub_is_word_bounded_and_skips_short_names(wired):
    from puffpai.security import turn_health
    out = turn_health.redact_known_medications(
        "Medication added: insulin. medsafe is fine.", ["insulin", "ok"])
    assert "[REDACTED-HEALTH]" in out
    assert "medsafe is fine" in out          # "ok" is below the length floor
    assert turn_health.redact_known_medications("a medsafe order", ["med"]) \
        == "a medsafe order"
    assert turn_health.redact_known_medications("Take Insulin now",
                                                ["insulin"]) \
        == "Take [REDACTED-HEALTH] now"
    assert turn_health.redact_known_medications("nothing", []) == "nothing"
    assert turn_health.redact_known_medications("", ["insulin"]) == ""


# ══════════════════════════════════════════════════════════════════════════
# The CUE-DEPENDENT detectors
# ══════════════════════════════════════════════════════════════════════════
#
# Screening the extracted FRAGMENT covers the structurally-validated detectors
# (ssn, card, phone, email). It does not cover `dob`, `address` and `name`:
# those need a cue the fragment no longer carries, so
#
# "my date of birth is <a date>, add <the same date> to my meds"
#
# would store the date on all five surfaces, both flags, and still say "not
# saved anywhere". Two of the three are worse than a cue problem: the
# classifier cannot see them on ANY input (`_ADDRESS_PATTERN` has no "Terrace"
# suffix; `_LABELED_NAME_PATTERN` needs "name:"). Widening either would change
# what the appliance detects, vaults, redacts and announces on every surface, so
# the classifier is left as it is and the medication write path refuses on the
# SOURCE CLAUSE instead.

#: Each is (phrase, the value that must not reach calendar.json).
CUE_CASES = [
    ("my date of birth is 01/02/1990, add 01/02/1990 to my meds",
     "01/02/1990"),
    ("my address is 742 Evergreen Terrace, "
     "add 742 Evergreen Terrace to my meds", "742 Evergreen Terrace"),
    ("my name is John Smith, add John Smith to my meds", "John Smith"),
    ("i live at 742 Evergreen Terrace, "
     "add 742 Evergreen Terrace to my meds", "742 Evergreen Terrace"),
]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("phrase,value", CUE_CASES)
def test_a_cue_disclosed_value_never_becomes_a_medication(wired, surface,
                                                          phrase, value):
    """Nothing stored, and the value reaches no app document — five surfaces,
    both flag states. The DOB phrasing is the one that can also carry a
    "not saved anywhere" notice; it cannot be false, because nothing is saved.
    """
    wired.reset()
    reply = wired.say(surface, phrase)

    assert wired.meds() == [], (surface, phrase, wired.meds())
    # The APP DOCUMENT is the sink this closes: nothing the household said may
    # become a stored medication record.
    assert value not in json.dumps(wired.calendar()), (surface, phrase)
    #...and PuffPAI never echoes it back into the stored transcript either.
    assert value not in " ".join(wired.persisted_assistant()), (surface, phrase)
    assert reply, surface
    # NOT asserted: the USER turn. Conversation memory keeps what the household
    # typed, governed by the scrub-and-vault rules: the DOB phrasing is
    # redacted there on both flags because the classifier sees it; the address
    # and name phrasings are not, because it cannot (see _MED_IDENTITY_CUE_RE).
    # That is the transcript contract, and widening it is a detector change,
    # not a medication fix.


@pytest.mark.parametrize("phrase,value", CUE_CASES)
def test_the_source_screen_refuses_at_both_write_paths(wired, phrase, value):
    """The fastpath and `_verify_and_fix`'s Case 3, asserted directly — the
    surface-level test above cannot tell which one declined."""
    api = wired.api
    name = api._extract_med_name(phrase)
    assert name, phrase
    assert api._med_name_refusal(name, source=phrase) is not None
    out = api._fix_add_med(phrase, api._get_app_data())
    assert out and out.startswith("!"), out
    assert wired.meds() == []


def test_the_whole_message_is_screened_not_the_clause(wired):
    """The whole-message rule, at unit level.

    There is no clause scoping. A detected value ANYWHERE in the message blocks
    the add, because the alternative, comparing renderings of a value across
    clauses, is a game with no end (`January 2, 1990` vs `01/02/1990`).
    """
    api = wired.api
    for msg in ["my SSN is 219-09-9999, add my vitamin to my meds",
                "my ssn is 123-45-6788, add insulin to my meds",
                "email me at nobody@example.invalid, add insulin to my meds"]:
        name = api._extract_med_name(msg)
        assert name, msg
        assert api._med_name_refusal(name, source=msg) is not None, msg


#: A 46-string real-medication corpus. Doses, hyphens, slashes,
#: hashes and digits — the shapes most likely to trip a PII screen.
REAL_MEDICATIONS = [
    "insulin", "Metformin", "warfarin", "Advair", "Lipitor", "Synthroid",
    "amlodipine", "levothyroxine", "atorvastatin", "omeprazole", "losartan",
    "gabapentin", "sertraline", "montelukast", "escitalopram", "bupropion",
    "B12", "5-HTP", "CoQ10", "Vitamin D3", "omega-3", "Plan B",
    "NovoLog 70/30", "Tylenol #3", "Adderall XR 20mg", "Coumadin 2.5",
    "aspirin 81", "Amoxicillin 500", "melatonin 5mg", "metoprolol tartrate 25",
    "insulin glargine U-100", "Preparation H", "Ventolin HFA", "Claritin-D",
    "Robitussin DM", "Mucinex D", "EpiPen", "Humalog", "Lantus",
    "prednisone 10mg", "ibuprofen 200", "vitamin D", "iron", "calcium",
    "magnesium citrate", "Zyrtec",
]


@pytest.mark.parametrize("name", REAL_MEDICATIONS)
def test_a_real_medication_is_never_refused(wired, name):
    """Zero false refusals — the screen must not refuse the thing it protects.

    Checked BOTH ways the screen is reached: the bare fragment, and the fragment
    with its real source clause. The digit-bearing names are the point: `ssn`,
    `card` and `phone` are structurally validated, so no dose string reaches
    them, and the identity-cue guard keys on "my <identity noun> is", which an
    add-a-medication clause does not contain.
    """
    api = wired.api
    assert api._med_name_refusal(name) is None, name
    message = "add %s to my meds" % name
    extracted = api._extract_med_name(message)
    assert extracted == name, (name, extracted)
    assert api._med_name_refusal(extracted, source=message) is None, name


@pytest.mark.parametrize("name", ["insulin", "Tylenol #3", "NovoLog 70/30"])
def test_a_real_medication_still_lands_through_the_routes(wired, name):
    wired.reset()
    wired.say("/chat/send", "add %s to my meds" % name)
    assert wired.meds() == [name], (name, wired.meds())


# ══════════════════════════════════════════════════════════════════════════
# The stored-name over-redaction side effect: deliberate, pinned
# ══════════════════════════════════════════════════════════════════════════

def test_a_medication_named_with_a_common_word_over_redacts_memory(wired):
    """DELIBERATE, and pinned so it cannot change unnoticed.

    `redact_known_medications` substitutes every occurrence of a stored
    medication name in the PERSISTED assistant turn, so a medication named with
    an ordinary English word — "iron", "calcium", "Plan B" — also redacts that
    word out of unrelated transcript text. That is the safe direction:

      * the reply the household READS is untouched — only the stored copy;
      * the error direction is toward hiding, not leaking; and
      * the alternative needs word-sense disambiguation, which is not something
        to bolt onto a persist path.

    A cheap word-boundary or context guard does NOT fix it: "the iron gate" and
    "my iron tablet" are both word-bounded uses of the same token, so any guard
    short of knowing which sense was meant either keeps the over-redaction or
    starts leaking the drug name. Left as-is, on purpose.
    """
    from puffpai.security import turn_health
    stored = turn_health.redact_known_medications(
        "the iron gate is closed and the iron is hot", ["iron"])
    assert stored == ("the [REDACTED-HEALTH] gate is closed and "
                      "the [REDACTED-HEALTH] is hot")
    #...and the safe half of the trade, on the same call: the drug name really
    # is gone from what gets stored.
    assert "iron" not in stored


# ══════════════════════════════════════════════════════════════════════════
# ONE fail-closed rule over the whole normalised message
# ══════════════════════════════════════════════════════════════════════════
#
# The case clause-scoped heuristics cannot handle:
#
# "my date of birth is January 2, 1990, add 01/02/1990 to my meds"
# flag OFF -> calendar.meds == ["01/02/1990"]
# reply: "I spotted a date of birth and did not keep it — it
# was removed... and not saved anywhere.
# Medication added: 01/02/1990..."
#
# The cue is in another clause, and the detector's span (`January 2, 1990`)
# neither contains nor is contained by the stored rendering (`01/02/1990`):
# overlap compares RENDERINGS of a value, not the value. One rule replaces the
# heuristics.

#: Clause-reordering bypass phrasings, plus the separator and word-order
#: variants, each with the value that must not reach an app document. Newline,
#: semicolon and comma separators are all here because a line-bound detector
#: treats the newline form differently from the comma form.
MUST_REFUSE = [
    ("dob_two_formats",
     "my date of birth is January 2, 1990, add 01/02/1990 to my meds",
     "01/02/1990"),
    ("dob_comma",
     "my date of birth is 01/02/1990, add 01/02/1990 to my meds", "01/02/1990"),
    ("dob_newline",
     "my date of birth is 01/02/1990\nadd 01/02/1990 to my meds", "01/02/1990"),
    ("dob_semicolon",
     "my date of birth is 01/02/1990; add 01/02/1990 to my meds", "01/02/1990"),
    ("dob_parenthesised",
     "add 01/02/1990 to my meds (my dob)", "01/02/1990"),
    ("dob_labelled",
     "my dob: 01/02/1990, add 01/02/1990 to my meds", "01/02/1990"),
    ("dob_born_on",
     "i was born on January 2, 1990, add 01/02/1990 to my meds", "01/02/1990"),
    ("address_value_first",
     "742 Evergreen Terrace is my address, "
     "add 742 Evergreen Terrace to my meds", "742 Evergreen Terrace"),
    ("address_i_live_at",
     "add 742 Evergreen Terrace to my meds, "
     "i live at 742 Evergreen Terrace", "742 Evergreen Terrace"),
    ("name_value_first",
     "John Smith is my name, add John Smith to my meds", "John Smith"),
    ("ssn_plain", "add 123-45-6788 to my meds", "123-45-6788"),
    ("bare_nine_digits", "add 123456788 to my meds", "123456788"),
    ("ssn_elsewhere",
     "my SSN is 219-09-9999, add my vitamin to my meds", "219-09-9999"),
]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("label,phrase,value",
                         MUST_REFUSE,
                         ids=[c[0] for c in MUST_REFUSE])
def test_a_message_carrying_personal_details_stores_no_medication(
        wired, surface, label, phrase, value):
    """Nothing stored, and the value in no app document — five surfaces, both
    flag states. Where a notice fires it is TRUE, because nothing was stored."""
    wired.reset()
    reply = wired.say(surface, phrase)

    assert wired.meds() == [], (label, surface, wired.meds())
    assert value not in json.dumps(wired.calendar()), (label, surface)
    assert reply, (label, surface)


@pytest.mark.parametrize("label,phrase,value",
                         MUST_REFUSE,
                         ids=[c[0] for c in MUST_REFUSE])
def test_both_write_paths_refuse_the_personal_detail_phrasings(wired, label,
                                                          phrase, value):
    """The fastpath and `_verify_and_fix`'s Case 3, asserted directly — the
    surface test above cannot say which one declined."""
    api = wired.api
    name = api._extract_med_name(phrase)
    assert name, (label, phrase)
    assert api._med_name_refusal(name, source=phrase) is not None, label
    out = api._fix_add_med(phrase, api._get_app_data())
    assert out and out.startswith("!"), (label, out)
    assert wired.meds() == [], label


@pytest.mark.parametrize("separator", [",", ";", "\n", " , ", ";\n"])
def test_every_separator_behaves_the_same(wired, separator):
    """`_DOB_PATTERN` needs its birth-trigger and the date on the same LINE,
    so a newline could hide a disclosure the comma form exposes.
    Normalisation collapses every separator before the screen runs, so they
    cannot differ."""
    api = wired.api
    phrase = ("my date of birth is 01/02/1990%s"
              "add 01/02/1990 to my meds" % separator)
    name = api._extract_med_name(phrase)
    assert name, phrase
    assert api._med_name_refusal(name, source=phrase) is not None, separator


def test_the_dob_blocker_is_closed_end_to_end(wired):
    """The two-format DOB sentence on /chat/send, with the assertion this
    file exists to make: the reply and the file agree."""
    wired.reset()
    reply = wired.say("/chat/send",
                      "my date of birth is January 2, 1990, "
                      "add 01/02/1990 to my meds")
    assert wired.meds() == []
    assert "01/02/1990" not in json.dumps(wired.calendar())
    assert "Medication added" not in reply, reply
    if not wired.on:
        # The notice fires for the DOB and is TRUE.
        assert NOT_KEPT in reply, reply


def test_a_medication_name_must_contain_a_letter(wired):
    """A bare nine-digit run is not an SSN to the detector, so nothing else
    stops `123456788` becoming a drug name."""
    api = wired.api
    assert api._med_name_refusal("123456788") is not None
    assert api._med_name_refusal("01/02/1990") is not None
    assert api._med_name_refusal("70/30") is not None
    #...and the letter rule must not catch a real medication that carries
    # digits, because the letters are right there.
    for name in ["B12", "5-HTP", "NovoLog 70/30", "Coumadin 2.5", "aspirin 81"]:
        assert api._med_name_refusal(name) is None, name


@pytest.mark.parametrize("phrase", [
    "742 Evergreen Terrace is my address, add 742 Evergreen Terrace to my meds",
    "John Smith is my name, add John Smith to my meds",
    "add 01/02/1990 to my meds (my dob)",
    "my dob: 01/02/1990, add it to my meds",
    "i live at 742 Evergreen Terrace, add 742 Evergreen Terrace to my meds",
    "i was born on January 2, 1990, add 01/02/1990 to my meds",
])
def test_the_identity_cue_matches_in_any_word_order(wired, phrase):
    """A cue that requires "my <noun> is" is bypassable by word order.
    Value-first, labelled, parenthesised, "i live at" and "born on" are all
    covered, anywhere in the message."""
    assert wired.api._MED_IDENTITY_CUE_RE.search(
        wired.api._med_normalise_for_screen(phrase)) is not None, phrase


def test_the_normaliser_keeps_digit_runs_and_folds_dates(wired):
    api = wired.api
    out = api._med_normalise_for_screen("my dob is 01.02.1990\nadd x to my meds")
    assert "01/02/1990" in out          # dotted form folded onto the slash form
    assert "\n" not in out              # separators collapsed
    assert "123456788" in api._med_normalise_for_screen("add 123456788 please")


def test_the_source_screen_fails_closed(wired, monkeypatch):
    """A classifier that cannot run refuses the add rather than storing an
    unscreened value."""
    class _Boom:
        def scan_for_pii(self, *a, **k):
            raise RuntimeError("classifier unavailable")

    monkeypatch.setattr(wired.api, "_get_pii_classifier", lambda: _Boom())
    assert wired.api._med_name_refusal(
        "insulin", source="add insulin to my meds") is not None


#: The four real add-a-medication clauses, so the corpus is screened the way a
#: household actually reaches it rather than as bare fragments.
REAL_MED_CLAUSES = [
    "add %s to my meds",
    "add a medication called %s",
    "set a medication reminder for %s",
    "put my %s on the medication list",
    "add %s to my medication list",
]


@pytest.mark.parametrize("name", REAL_MEDICATIONS)
def test_a_real_medication_survives_every_real_clause(wired, name):
    """46 names × 4 clauses = 184 screens, zero false refusals. The fail-closed
    rule is only acceptable if it never refuses the thing it protects."""
    api = wired.api
    for template in REAL_MED_CLAUSES:
        message = template % name
        extracted = api._extract_med_name(message)
        assert extracted == name, (name, message, extracted)
        assert api._med_name_refusal(extracted, source=message) is None, message


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("name", ["insulin", "NovoLog 70/30", "Tylenol #3"])
def test_a_real_medication_still_lands_on_every_surface(wired, surface, name):
    wired.reset()
    wired.say(surface, "add %s to my meds" % name)
    assert wired.meds() == [name], (surface, name, wired.meds())


# ══════════════════════════════════════════════════════════════════════════
# A cue must carry a VALUE
# ══════════════════════════════════════════════════════════════════════════
#
# An identity cue whose terminator set includes ", ) $" makes the mere WORDS
# "my birthday" / "my phone" / "my name" / "my address" / "my email" / "my
# credit card" count as a disclosure. Ordinary medication sentences would then
# store nothing and be told:
#
# "That message mentions personal details, so I didn't add a medication..."
#
# They mention none. A false refusal is not the safe direction in the half of
# this defect that costs a person a dose, so a bare possessive noun phrase (at
# a comma, a full stop, the end of the message, or before another preposition)
# is not a cue. A cue must carry a value.

#: Fourteen ordinary sentences, each with the drug that must reach
#: `calendar.meds`. Twelve are below; the two "remind me" ones follow.
ORDINARY_SENTENCES = [
    ("add melatonin to my meds for my birthday", "melatonin"),
    ("add vitamin D to my meds, it is for my birthday", "vitamin D"),
    ("add prednisone to my meds before my birthday", "prednisone"),
    ("add Lipitor to my meds and text my phone", "Lipitor"),
    ("add Metformin to my meds and email it to my email", "Metformin"),
    ("add aspirin to my meds, it is under my name", "aspirin"),
    ("add warfarin to my meds, the bottle has my name", "warfarin"),
    ("add insulin to my meds, delivered to my address", "insulin"),
    ("add Lantus to my meds, ship to my home address", "Lantus"),
    ("add Humalog to my meds, put it on my credit card", "Humalog"),
    ("add Advair to my meds - it is on my card", "Advair"),
    ("add Synthroid to my meds", "Synthroid"),
]

#: The other two of the fourteen. These say "remind me", which
#: `_MED_ADD_EXCLUDE_RE` hands to `_try_add_task` on purpose so the medication
#: fastpath can never steal a reminder, so on four of the five surfaces they
#: never write a medication. What matters here is the SCREEN, so they are
#: asserted where the screen lives: it must not refuse them, and no surface may
#: say they mention personal details.
ORDINARY_REMINDER_SENTENCES = [
    ("add insulin to my meds, and remind me to charge my phone", "insulin"),
    ("add Zyrtec to my meds, remind me on my phone", "Zyrtec"),
]


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("phrase,drug", ORDINARY_SENTENCES,
                         ids=[c[1].replace(" ", "_") for c in ORDINARY_SENTENCES])
def test_an_ordinary_sentence_mentioning_a_noun_still_stores(wired, surface,
                                                             phrase, drug):
    """End to end: five surfaces x both flag states.

    The medication reaches the calendar, and nothing tells the household their
    message mentioned personal details when it mentioned none.
    """
    wired.reset()
    reply = wired.say(surface, phrase)

    assert wired.meds() == [drug], (surface, phrase, wired.meds())
    assert wired.api._MED_PERSONAL_DETAILS_REFUSAL not in reply, (surface,
                                                                  reply)
    assert wired.api._MED_PII_REFUSAL not in reply, (surface, reply)
    #...and the honesty invariant this whole file exists for: nothing may
    # claim it kept nothing on a turn that kept the medication.
    assert NOT_KEPT not in reply, (surface, reply)


@pytest.mark.parametrize("phrase,drug", ORDINARY_SENTENCES + ORDINARY_REMINDER_SENTENCES,
                         ids=[c[0][:28].replace(" ", "_")
                              for c in ORDINARY_SENTENCES + ORDINARY_REMINDER_SENTENCES])
def test_the_screen_refuses_none_of_the_fourteen(wired, phrase, drug):
    """The screen itself, on all fourteen - the surface test above cannot see
    the two reminder phrasings, which never reach the medication fastpath."""
    api = wired.api
    extracted = api._extract_med_name(phrase)
    assert extracted == drug, (phrase, extracted)
    assert api._med_name_refusal(extracted, source=phrase) is None, phrase


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("phrase,drug", ORDINARY_REMINDER_SENTENCES,
                         ids=[c[1] for c in ORDINARY_REMINDER_SENTENCES])
def test_a_reminder_phrasing_is_never_called_personal_details(wired, surface,
                                                              phrase, drug):
    """The two "remind me" sentences of the fourteen.

    `_MED_ADD_EXCLUDE_RE` gives these to `_try_add_task`, so four of the five
    surfaces write a task rather than a medication. What must not happen is
    the personal-details refusal.
    """
    wired.reset()
    reply = wired.say(surface, phrase)

    assert wired.api._MED_PERSONAL_DETAILS_REFUSAL not in reply, (surface,
                                                                  reply)
    assert wired.api._MED_PII_REFUSAL not in reply, (surface, reply)
    # On the one surface that does reach `_fix_add_med`, the drug lands.
    if surface == "/conversation":
        assert wired.meds() == [drug], (surface, wired.meds())


@pytest.mark.parametrize("phrase", [
    "add melatonin to my meds for my birthday",
    "add Zyrtec to my meds, remind me on my phone",
    "add warfarin to my meds, the bottle has my name",
    "add Lantus to my meds, ship to my home address",
    "add Humalog to my meds, put it on my credit card",
    "add insulin to my meds, it is my birthday",
    "add insulin to my meds. my phone.",
    "add insulin to my meds and text my phone",
])
def test_a_bare_possessive_noun_phrase_is_not_an_identity_cue(wired, phrase):
    """The rule at its root. A possessive noun phrase with no value beside
    it - at a comma, a full stop, the end of the message, or before another
    preposition - is not a disclosure."""
    assert wired.api._MED_IDENTITY_CUE_RE.search(
        wired.api._med_normalise_for_screen(phrase)) is None, phrase


@pytest.mark.parametrize("phrase", [
    "my address is 742 Evergreen Terrace, add it to my meds",
    "my dob: 01/02/1990, add it to my meds",
    "my name is John Smith, add it to my meds",
    "742 Evergreen Terrace is my address, add it to my meds",
    "John Smith is my name, add it to my meds",
    "add 01/02/1990 to my meds (my dob)",
    "i live at 742 Evergreen Terrace, add it to my meds",
    "i was born on January 2, 1990, add it to my meds",
    # Flag ON: the routing text is already scrubbed, so the value the cue
    # carries IS a placeholder. Without these the ON half of the same-breath
    # rule would reopen while the OFF half stays closed.
    "my SSN is [REDACTED-SSN], add my vitamin to my meds",
    "my dob is {{vault:handle}}, add my vitamin to my meds",
])
def test_a_cue_that_carries_a_value_still_matches(wired, phrase):
    """The other half of the value rule: narrowing the cue must not reopen
    the word-order and clause-reordering bypasses. Every word order still
    matches."""
    assert wired.api._MED_IDENTITY_CUE_RE.search(
        wired.api._med_normalise_for_screen(phrase)) is not None, phrase
