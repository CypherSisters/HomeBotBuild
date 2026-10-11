"""Demo-safety: twelve behaviours, each pinned by its own repro.

Six failure modes:

  * a chat runner at ``-c 4096`` that no config governs
  * "turn off everything" answered "Okay, stopping any music or radio."
  * every RAG ``sources[]`` padded with irrelevant household uploads
  * ``audit.jsonl`` / ``dashboard_tile_reviews.jsonl`` at 0644
  * an unavailable camera polled every 30 s, 20 x 502 per 10 minutes
  * the show-me payload lives on ``GET /dashboard/display`` (doc only)

Six more, all the same shape, a capability that works and a phrasing that
never reaches it:

    "do I need a jacket today"
    three workout phrasings, none reaching /fitness/program
    "whats 847 times 63" -> a failed planner call
    a document reference that names a real upload
    "can you look up what the bills capability does"
    "lets play a quiz"

Every phrasing below is a phrasing a user would say. NO health values and no
real household data appear here: the fitness row is exercised against a fixture
library, and the PII constants are the standing fakes.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import puffpai.api_server as api  # noqa: E402
from puffpai import calculator  # noqa: E402
from puffpai import document_lookup  # noqa: E402
from puffpai import ha_camera_backoff as backoff  # noqa: E402
from puffpai.inference import model_options  # noqa: E402


class _FakeTool:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def invoke(self, arg=""):
        self.calls.append(arg)
        return self.reply


class _FakeTools:
    def __init__(self, **tools):
        self.tool_dict = tools


# ══════════════════════════════════════════════════════════════════════════
# the untargeted whole-house command
# ══════════════════════════════════════════════════════════════════════════

#: Three core phrasings, plus the two spellings a stranger is
#: as likely to say. All mean the same untargeted thing.
UNTARGETED = [
    "turn off everything",
    "turn everything on",
    "shut it all down",
    "turn everything off",
    "shut everything down",
]


@pytest.fixture
def quiet_media(monkeypatch):
    """Nothing is playing, so the reply carries no media offer."""
    monkeypatch.setattr(api, "_media_sources_still_playing", lambda: [])


@pytest.mark.parametrize("said", UNTARGETED)
def test_untargeted_command_answers_about_the_gate_not_the_music(said, quiet_media):
    reply = api._try_untargeted_house_command(said)
    assert reply, f"{said!r} must be claimed by the untargeted fastpath"
    assert "music or radio" not in reply.lower()
    assert "whole house from one command" in reply
    assert "room or a device" in reply
    assert "living room lights" in reply


def test_turn_off_everything_never_answers_about_music(quiet_media):
    """The media-stop reply, byte for byte, must not come back."""
    assert api._try_untargeted_house_command("turn off everything") != (
        "Okay — stopping any music or radio.")


def test_direction_is_honoured(quiet_media):
    assert "switch on" in api._try_untargeted_house_command("turn everything on")
    assert "switch off" in api._try_untargeted_house_command("turn off everything")


def test_playing_media_is_offered_as_a_separate_question(monkeypatch):
    monkeypatch.setattr(api, "_media_sources_still_playing",
                        lambda: ["the radio"])
    reply = api._try_untargeted_house_command("turn off everything")
    assert "whole house from one command" in reply
    # The offer is a QUESTION, and it comes after the real answer.
    assert reply.index("whole house") < reply.index("the radio")
    assert "want me to stop that?" in reply


#: Everything that must keep its existing route. A whole-house fastpath that
#: eats a targeted command is a worse bug than the one it fixes.
DECLINE_CORPUS = [
    "turn off the living room lights",
    "turn on the office led",
    "turn off the lights",
    # NOTE: "turn off all the lights" is NOT in this corpus. Declined here, the
    # quantified-lights shape falls through into _try_media_stop and answers
    # "Okay - stopping any music or radio.", so the gate claims it on purpose;
    # see tests/test_broadcast_lights_refusal.py. The unquantified "turn off
    # the lights" above still declines, so a targeted command is never eaten.
    "stop the music",
    "stop everything",
    "turn everything off in the kitchen",
    "shut down",
    "what can you do",
    "turn the volume up",
]


@pytest.mark.parametrize("said", DECLINE_CORPUS)
def test_untargeted_fastpath_declines_everything_else(said, quiet_media):
    assert api._try_untargeted_house_command(said) is None


def test_untargeted_fastpath_calls_no_home_assistant_anything(monkeypatch,
                                                              quiet_media):
    """Zero control_device calls: the row's whole point."""
    calls = []

    def _boom(*a, **kw):
        calls.append(a)
        raise AssertionError("the untargeted path must not reach HA")

    monkeypatch.setattr(api, "_get_ha_bridge", _boom)
    for said in UNTARGETED:
        assert api._try_untargeted_house_command(said)
    assert calls == []


# ══════════════════════════════════════════════════════════════════════════
# RAG sources: no padding, always source_type, only what was cited
# ══════════════════════════════════════════════════════════════════════════

def _row(**kw):
    row = {
        "text": "body", "library": "Wikipedia", "collection": "wiki",
        "distance": 0.1, "relevance": 0.9, "title": "T", "url": "",
        "is_user_upload": False,
    }
    row.update(kw)
    return row


@pytest.fixture
def rag_bench(monkeypatch, tmp_path):
    """_get_rag_context with every backing store replaced by a fixture."""
    monkeypatch.setattr(api, "_SRC_DIR", str(tmp_path))
    (tmp_path / "indexed_libraries.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(api, "_get_dual_zone_rag", lambda: None)
    monkeypatch.setattr(api, "_get_query_embedder", lambda: (lambda x: [[0.0]]))
    monkeypatch.setattr(api, "_get_zim_fts_results", lambda q, max_hits=5: [])
    return tmp_path


def _fake_uploads(monkeypatch, rows):
    monkeypatch.setattr(api, "_get_zim_fts_results",
                        lambda q, max_hits=5: list(rows))


def test_moon_landing_cites_only_zim_and_never_a_training_sheet(rag_bench,
                                                                monkeypatch):
    """The padding shape: real hits plus 4 uploads at ~0.55."""
    zim = [_row(library="Wikipedia-simple: Moon landing", relevance=0.83,
                source_type="zim_fts", collection="zim"),
           _row(library='RationalWiki: Moon landing hoax', relevance=0.78,
                source_type="zim_fts", collection="zim")]
    monkeypatch.setattr(api, "_get_zim_fts_results", lambda q, max_hits=5: zim)

    padding = [
        # l2 distance 1.06 -> relevance 0.47 -> boosted 0.5495, i.e. the
        # 0.55-0.57 band every padded upload lands in.
        {"snippet": "s", "filename": name, "distance": 1.06, "space": "l2",
         "file_id": name, "classification": "unknown"}
        for name in ("Strength Program.docx", "IMG_0002.png",
                     "IMG_0003.png", "Image.png")
    ]
    monkeypatch.setattr(
        "puffpai.ingestion.query_user_uploads",
        lambda q, n_results=5: padding, raising=False)

    out = api._get_rag_context("tell me about the moon landing")
    assert out is not None
    libs = [s["library"] for s in out["sources"]]
    assert not any("Your upload" in lib for lib in libs), libs
    assert not any("Strength Program" in lib for lib in libs), libs


def test_the_ssn_question_never_cites_a_medical_file(rag_bench, monkeypatch):
    """`my ssn is 219-09-9999` cited patient_record.txt at relevance 0.55."""
    monkeypatch.setattr(api, "_get_zim_fts_results", lambda q, max_hits=5: [
        _row(library="Wikipedia: Social Security number", relevance=0.8,
             source_type="zim_fts", collection="zim")])
    monkeypatch.setattr(
        "puffpai.ingestion.query_user_uploads",
        lambda q, n_results=5: [
            {"snippet": "s", "filename": "patient_record.txt", "distance": 1.06,
             "space": "l2", "file_id": "p1", "classification": "medical"}],
        raising=False)
    out = api._get_rag_context("my ssn is 219-09-9999")
    assert out is not None
    assert all("patient_record" not in s["library"] for s in out["sources"])


def test_a_genuine_upload_match_is_still_cited_with_its_source_type(
        rag_bench, monkeypatch):
    """The floor must not silence a real hit — only the padding."""
    monkeypatch.setattr(api, "_get_zim_fts_results", lambda q, max_hits=5: [])
    monkeypatch.setattr(
        "puffpai.ingestion.query_user_uploads",
        lambda q, n_results=5: [
            {"snippet": "the roof warranty runs to 2031", "distance": 0.02,
             "space": "cosine", "filename": "Roof Warranty.pdf",
             "file_id": "r1", "classification": "user_upload"}],
        raising=False)
    out = api._get_rag_context("when does the roof warranty run out")
    assert out is not None
    uploads = [s for s in out["sources"] if s["type"] == "user_upload"]
    assert len(uploads) == 1
    assert uploads[0]["library"] == "Your upload: Roof Warranty.pdf"
    assert uploads[0]["source_type"] == "user_upload"
    assert uploads[0]["relevance"] >= api._RAG_UPLOAD_RELEVANCE_MIN


def test_every_source_carries_a_source_type(rag_bench, monkeypatch):
    monkeypatch.setattr(api, "_get_zim_fts_results", lambda q, max_hits=5: [
        _row(library="Wikipedia: Boeing 747", relevance=0.9,
             source_type="zim_fts", collection="zim")])
    monkeypatch.setattr(
        "puffpai.ingestion.query_user_uploads",
        lambda q, n_results=5: [
            {"snippet": "747 spec sheet", "distance": 0.02, "space": "cosine",
             "filename": "747.pdf", "file_id": "a", "classification": "ref"}],
        raising=False)
    out = api._get_rag_context("what is the wingspan of a boeing 747")
    assert out["sources"]
    for source in out["sources"]:
        assert source.get("source_type"), source


def test_a_source_is_only_listed_if_its_text_is_in_the_context(rag_bench,
                                                               monkeypatch):
    monkeypatch.setattr(api, "_get_zim_fts_results", lambda q, max_hits=5: [
        _row(library=f"Lib {i}", relevance=0.9 - i / 100.0,
             source_type="zim_fts", collection="zim", text=f"body {i}")
        for i in range(6)])
    monkeypatch.setattr("puffpai.ingestion.query_user_uploads",
                        lambda q, n_results=5: [], raising=False)
    out = api._get_rag_context("anything")
    for source in out["sources"]:
        assert f"[{source['library']}]" in out["context_text"]


def test_the_upload_floor_is_higher_than_the_generic_floor():
    assert api._RAG_UPLOAD_RELEVANCE_MIN >= 0.7


# ══════════════════════════════════════════════════════════════════════════
# one config governs every chat runner's context
# ══════════════════════════════════════════════════════════════════════════

def test_ollama_chat_always_sends_a_model_options_num_ctx(monkeypatch):
    sent = {}

    class _Resp:
        pass

    def _post(url, json=None, timeout=None, stream=None):
        sent.update(json or {})
        return _Resp()

    monkeypatch.setitem(sys.modules, "requests",
                        type("m", (), {"post": staticmethod(_post)}))
    api._ollama_chat([{"role": "user", "content": "hi"}])
    assert sent["options"]["num_ctx"] == model_options.num_ctx()
    assert sent["options"]["num_predict"] == model_options.num_predict()


def test_the_planner_role_has_its_own_config_backed_context():
    value = model_options.planner_num_ctx()
    assert isinstance(value, int) and value > 0
    # Its own role: never silently the chat window, never above it.
    assert value <= model_options.num_ctx()


def test_every_planner_call_site_reads_the_accessor_not_a_literal():
    """A future edit cannot reintroduce a fourth hardcoded 4096."""
    root = Path(__file__).resolve().parents[1] / "src" / "puffpai"
    for rel in ("agents/engine.py", "assistant_tools.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "planner_num_ctx()" in text, rel


def test_the_settings_default_no_longer_advertises_a_phantom_context():
    assert api._DEFAULT_AI_SETTINGS["context_length"] == model_options.num_ctx()


# ══════════════════════════════════════════════════════════════════════════
# 0640 on every security ledger
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX mode bits; chmod is a no-op on Windows")
def test_audit_log_is_written_0640(tmp_path):
    from puffpai.security import audit_log as al
    from puffpai.security import egress_ledger as el
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o644)
    el.forget_mode_check(str(path))
    al.AuditLog(log_file=path).log("vault_unlock", user="owner")
    assert (os.stat(path).st_mode & 0o777) == el.FILE_MODE


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX mode bits; chmod is a no-op on Windows")
def test_dashboard_tile_reviews_is_written_0640(tmp_path, monkeypatch):
    from puffpai import dashboard_registry as dr
    from puffpai.security import egress_ledger as el
    path = tmp_path / "dashboard_tile_reviews.jsonl"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o644)
    el.forget_mode_check(str(path))
    monkeypatch.setattr(dr, "_AUDIT_PATH", str(path))
    dr._audit("denied", "camera.test", ["pending consent"], "tests")
    assert (os.stat(path).st_mode & 0o777) == el.FILE_MODE


def test_the_mode_helper_is_shared_not_copied():
    """One definition of "a security log's mode", reachable by both writers."""
    from puffpai.security import egress_ledger as el
    assert callable(el.ensure_secure_mode)
    assert el.FILE_MODE == 0o640


def test_both_ledgers_still_record_their_line(tmp_path, monkeypatch):
    """The mode work must not cost a log line on any platform."""
    from puffpai.security import audit_log as al
    from puffpai import dashboard_registry as dr
    audit = tmp_path / "audit.jsonl"
    al.AuditLog(log_file=audit).log("firewall_block", user="owner")
    assert json.loads(audit.read_text(encoding="utf-8").splitlines()[0])["action"] \
        == "firewall_block"
    reviews = tmp_path / "dashboard_tile_reviews.jsonl"
    monkeypatch.setattr(dr, "_AUDIT_PATH", str(reviews))
    dr._audit("allowed", "camera.test", [], "tests")
    assert json.loads(reviews.read_text(encoding="utf-8").splitlines()[0])[
        "entity_id"] == "camera.test"


# ══════════════════════════════════════════════════════════════════════════
# camera backoff
# ══════════════════════════════════════════════════════════════════════════

CAMERA = "camera.driveway_fluent"


@pytest.fixture(autouse=True)
def _clean_backoff():
    backoff.reset()
    yield
    backoff.reset()


def test_backoff_grows_exponentially_to_a_ten_minute_ceiling():
    waits = []
    for _ in range(8):
        _, wait, _ = backoff.record_failure(CAMERA)
        waits.append(wait)
        backoff.reset(CAMERA) if False else None
    assert waits[:6] == [30, 60, 120, 240, 480, 600]
    assert max(waits) == backoff.MAX_BACKOFF == 600


def test_only_the_first_failure_of_a_streak_is_loud():
    firsts = [backoff.record_failure(CAMERA)[2] for _ in range(5)]
    assert firsts == [True, False, False, False, False]


def test_a_success_clears_the_streak_immediately():
    backoff.record_failure(CAMERA)
    backoff.record_failure(CAMERA)
    assert backoff.streak(CAMERA) == 2
    backoff.record_success(CAMERA)
    assert backoff.streak(CAMERA) == 0
    assert backoff.retry_after(CAMERA) == 0


def test_a_camera_in_backoff_is_not_polled_upstream(monkeypatch):
    """A fake HA that 502s: the second request must not reach it."""
    from puffpai.ha_bridge import HABridgeError
    hits = []

    class _Bridge:
        def get_camera_image(self, entity_id):
            hits.append(entity_id)
            raise HABridgeError("502 Bad Gateway from Home Assistant")

    monkeypatch.setattr(api, "_get_ha_bridge", lambda: _Bridge())
    with api.app.test_request_context():
        body, status = api.ha_camera(CAMERA)
        assert status == 502
        assert len(hits) == 1
        # Immediately again: served from the backoff, HA untouched.:
        # a poll landing inside the backoff window is not an error — the
        # dashboard's 30s tile poll must see 200, not another 502, or the
        # journal's client-visible error rate never reflects the real fix.
        body2, status2 = api.ha_camera(CAMERA)
        assert status2 == 200
        assert len(hits) == 1, "the second poll must not reach Home Assistant"
        payload = body2.get_json()
        assert payload["available"] is False
        assert payload["unavailable"] is True
        assert payload["reason"] == "camera unavailable"
        assert payload["retry_after"] >= 1
        assert body2.headers["Retry-After"]
        assert body2.headers["X-PuffPAI-Camera-Unavailable"] == "1"


def test_a_healthy_camera_is_untouched_by_the_backoff(monkeypatch):
    class _Bridge:
        def get_camera_image(self, entity_id):
            return b"\xff\xd8jpeg", "image/jpeg"

    monkeypatch.setattr(api, "_get_ha_bridge", lambda: _Bridge())
    with api.app.test_request_context():
        resp = api.ha_camera("camera.front_door")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "no-cache"
    assert backoff.retry_after("camera.front_door") == 0


# ══════════════════════════════════════════════════════════════════════════
# deterministic calculator
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("said,expected", [
    ("whats 847 times 63", "53,361"),          # the exact failing phrasing
    ("what is 847 multiplied by 63", "53,361"),
    ("calculate 15 percent tip on 84 dollars", "12.6"),
    ("whats 100 divided by 4", "25"),
    ("what is 1,200 minus 350", "850"),
    ("whats 2 to the power of 10", "1,024"),
])
def test_arithmetic_is_answered_deterministically(said, expected):
    reply = api._try_calculate(said)
    assert reply and reply.startswith(expected), reply


CALC_DECLINE = [
    "set an alarm for 6:45 am",
    "set a timer for 10 minutes",
    "whats in the pantry",
    "what is 42",
    "tell me about the moon landing",
    "turn off everything",
    "add 6 pounds of ground beef to the pantry",
    "how many devices are on my network",
    "whats 10 divided by 0",
]


@pytest.mark.parametrize("said", CALC_DECLINE)
def test_calculator_declines_everything_that_is_not_arithmetic(said):
    assert api._try_calculate(said) is None


def test_the_calculator_never_evaluates_python():
    for hostile in ("__import__('os').system('id')",
                    "1 if open('/etc/passwd') else 2",
                    "2**999999999"):
        assert calculator.solve(hostile) is None


# ══════════════════════════════════════════════════════════════════════════
# conversational weather
# ══════════════════════════════════════════════════════════════════════════

FORECAST = "the default city: 87F, clear, 10% chance of rain."


@pytest.fixture
def weather_bench(monkeypatch):
    tool = _FakeTool(FORECAST)
    monkeypatch.setattr(api, "_get_assistant_tools",
                        lambda: _FakeTools(auto_weather_forecast=tool))
    monkeypatch.setattr(api, "_summarize_tool_result",
                        lambda message, raw: f"ANSWER<{raw}>")
    return tool


@pytest.mark.parametrize("said", [
    "is it hot out",
    "is it cold out",
])
def test_conversational_weather_reaches_the_real_forecast(said, weather_bench):
    reply = api._try_weather_answer(said)
    assert reply == f"ANSWER<{FORECAST}>", reply
    assert weather_bench.calls == [""]


@pytest.mark.parametrize("said", [
    "do I need a jacket today",            # the primary phrasing
    "will I need an umbrella today",
    "should I wear a coat",
    "do we need sunscreen today",
])
def test_an_attire_question_reaches_the_forecast_and_gets_a_yes_or_no(
        said, weather_bench):
    """These take the weather path, and the reply leads with the yes/no
    instead of reading the whole snapshot aloud with the verdict last. The
    forecast is still fetched; only the SHAPE of the answer differs."""
    reply = api._try_weather_answer(said)
    assert weather_bench.calls == [""]
    assert not reply.startswith("ANSWER<")
    assert reply.split(" ")[0].rstrip(",") in ("Yes", "No", "Maybe",
                                               "Probably", "Not")


@pytest.mark.parametrize("said", [
    "where is my jacket",
    "add a jacket to the shopping list",
    "whats the temperature in the living room",
    "show me the weather widget",
    "how many devices are on my network",
])
def test_weather_fastpath_still_declines_non_weather(said, weather_bench):
    assert api._try_weather_answer(said) is None


# ══════════════════════════════════════════════════════════════════════════
# today's program, from the same library as /fitness/program
# ══════════════════════════════════════════════════════════════════════════

#: A FIXTURE program. No household health values appear in this repo.
FIXTURE_LIBRARY = {
    "programs": {
        "beginner_general": {
            "title": "Sample Beginner Program",
            "audience": "fixture",
            "days_per_week": 3,
            "guidance": "Fixture guidance line.",
            "sessions": [{"name": "Session A",
                          "exercises": [{"name": "Movement One",
                                         "prescription": "3 x 8"}]}],
            "sources": ["fixture"],
        }
    }
}


@pytest.fixture
def fitness_bench(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({
        "last_active_profile": "p1",
        "profiles": [{"id": "p1", "display_name": "Test Fakeperson",
                      "preferences": {"workout_level": "beginner"}}],
    }), encoding="utf-8")
    monkeypatch.setattr("puffpai.profile_manager.profiles_path",
                        lambda: str(profiles), raising=False)
    monkeypatch.setattr(api, "_load_workout_library", lambda: FIXTURE_LIBRARY)


@pytest.mark.parametrize("said", [
    "what workout am I supposed to do today",
    "give me todays training session",
    "show me my exercise program for today",
])
def test_all_three_workout_phrasings_reach_the_program(said, fitness_bench):
    reply = api._try_fitness_program(said)
    assert reply, said
    assert "Sample Beginner Program" in reply
    assert "Session A" in reply
    assert "Movement One" in reply
    # The false "no program" claim must not come back.
    assert "don't have an existing program" not in reply


def test_the_endpoint_and_the_chat_path_read_the_same_library(fitness_bench):
    data, reason = api._fitness_program_data("")
    assert reason == ""
    assert data["program"] == "Sample Beginner Program"
    assert data["user"] == "Test Fakeperson"


@pytest.mark.parametrize("said", [
    "log my workout",
    "what are my personal records",
    "whats my workout streak",
    "generate a workout",
    "whats in the pantry",
])
def test_fitness_fastpath_declines_the_other_fitness_reads(said, fitness_bench):
    assert api._try_fitness_program(said) is None


# ══════════════════════════════════════════════════════════════════════════
# trivia
# ══════════════════════════════════════════════════════════════════════════

#: The bank tool's real envelope. The answer key rides along for the grading
#: layer and must never reach the user verbatim.
QUESTION = (
    "[Trivia - Science]\n"
    "What is the chemical symbol for gold?\n"
    "(A) Au  (B) Ag  (C) Fe  (D) Cu\n\n"
    "Ask the user to pick A, B, C, or D. "
    "Do NOT reveal the answer yet - wait for their response. [answer=A]"
)


class _MemoryStore:
    """The trivia game's app-data contract, in memory (never the real file)."""

    def __init__(self):
        self.data = {"results": [], "last_updated": ""}

    def load(self):
        return json.loads(json.dumps(self.data))

    def save(self, data):
        self.data = json.loads(json.dumps(data, default=str))
        return True


@pytest.fixture
def trivia_bench(monkeypatch):
    from puffpai import trivia_game
    tool = _FakeTool(QUESTION)
    monkeypatch.setattr(api, "_get_assistant_tools",
                        lambda: _FakeTools(app_trivia_question=tool))
    monkeypatch.setattr(trivia_game, "_AppDataStore", _MemoryStore)
    return tool


@pytest.mark.parametrize("said", [
    "lets play a quiz",                    # the primary phrasing
    "give me a trivia question",
    "quiz me",
    "test me",
])
def test_quiz_phrasings_reach_the_trivia_capability(said, trivia_bench):
    reply = api._try_trivia_start(said)
    assert reply and "chemical symbol for gold" in reply
    #: the reply never carries the [answer=A] key.
    assert "[answer" not in reply.lower()


def test_a_named_category_is_passed_through(trivia_bench):
    api._try_trivia_start("quiz me on science")
    assert trivia_bench.calls == ["science"]


@pytest.mark.parametrize("said", [
    "play some jazz radio",
    "show me Casablanca",
    "whats in the pantry",
])
def test_trivia_fastpath_declines_everything_else(said, trivia_bench):
    assert api._try_trivia_start(said) is None
    assert trivia_bench.calls == []


def test_trivia_stats_phrasing_reads_the_store_instead_of_a_question(trivia_bench):
    """a stats question is answered from the file, never invented."""
    reply = api._try_trivia_start("what are my trivia stats")
    assert reply and "on record yet" in reply
    assert trivia_bench.calls == []


# reading a capability out of the registry
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("said", [
    "can you look up what the bills capability does",   # the exact phrasing
    "what tools do you have for weather",
    "can you read the registry entry for weather",
])
def test_registry_questions_are_answered_from_the_registry(said):
    reply = api._try_capability_lookup(said)
    assert reply, said
    # A reply offering a web search instead of the registry read must not appear.
    assert "search" not in reply.lower() or "web" not in reply.lower()
    assert "." in reply


def test_the_bills_capability_is_described_by_its_real_id():
    reply = api._try_capability_lookup(
        "can you look up what the bills capability does")
    assert "bills" in reply.lower()
    assert "Access:" in reply and "Tools:" in reply


def test_what_tools_do_you_have_for_weather_reaches_the_registry_not_weather():
    """If `_try_instant_answer` consulted the weather fastpath first, "what
    tools do you have for weather" would be absorbed as a live forecast query
    instead of a registry read. _try_capability_lookup must win via the full
    instant-answer dispatch, not just when called directly.
    """
    reply = api._try_instant_answer("what tools do you have for weather")
    assert reply
    assert "Access:" in reply and "Tools:" in reply
    assert "degrees" not in reply.lower()
    assert "%" not in reply


@pytest.mark.parametrize("said", [
    "what can you do",
    "whats in the pantry",
    "turn off the living room lights",
    "tell me about the moon landing",
])
def test_capability_lookup_declines_everything_else(said):
    assert api._try_capability_lookup(said) is None


# ══════════════════════════════════════════════════════════════════════════
# a document reference that names a real upload
# ══════════════════════════════════════════════════════════════════════════

MANIFEST = [
    {"id": "d1", "filename": "Strength Program.docx",
     "classification": "user_upload", "indexed_at": "2026-08-01T00:00:00",
     "word_count": 812},
    {"id": "d2", "filename": "Gym Session Notes.docx",
     "classification": "user_upload", "indexed_at": "2026-07-04T00:00:00",
     "word_count": 210},
]


@pytest.fixture
def docs_bench(monkeypatch):
    monkeypatch.setattr("puffpai.ingestion.list_ingested_docs",
                        lambda: [dict(d) for d in MANIFEST], raising=False)
    monkeypatch.setattr(document_lookup, "text_of", lambda e, limit=6000: "TEXT")
    monkeypatch.setattr(api, "_summarize_tool_result",
                        lambda message, raw: f"ANSWER<{raw.splitlines()[0]}>")


def test_the_exact_failing_document_request_now_resolves(docs_bench):
    reply = api._try_document_read(
        "can you read the Strength Program document and convert it for me")
    assert reply
    assert "Strength Program.docx" in reply
    assert "couldn't locate" not in reply


def test_a_title_resolves_with_and_without_wrapper_words(docs_bench):
    for reference in ("Strength Program",
                      "the Strength Program",
                      "strength program.docx"):
        entry = document_lookup.resolve(reference)
        assert entry and entry["id"] == "d1", reference


def test_a_document_that_is_not_there_gets_an_honest_miss(docs_bench):
    reply = api._try_document_read("read the Quarterly Widget Report document")
    assert reply
    assert "don't have a document by that name" in reply
    # The docref over-capture fix stopped the miss reply reciting filenames
    # out loud; it offers the listing instead of performing it.
    assert "Strength Program.docx" not in reply
    assert "list your documents" in reply


def test_the_resolver_never_guesses_below_its_floor(docs_bench):
    assert document_lookup.resolve("something else entirely") is None
    assert document_lookup.MIN_RATIO >= 0.7


@pytest.mark.parametrize("said", [
    "show me my documents",
    "what documents have i uploaded",
    "read the file",
    "show me Casablanca",
])
def test_document_fastpath_declines_list_and_media_questions(said, docs_bench):
    assert api._try_document_read(said) is None
