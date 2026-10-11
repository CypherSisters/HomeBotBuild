"""Trivia is a game, not a vending machine.

What a stateless trivia fastpath gets wrong, and these tests forbid:

    printing the answer key: "...wait for their response.
           [answer=C]" in every reply
    "quiz me" -> answer "A" (wrong) -> a brand new question, no feedback
    eight chat trivia turns, and ``trivia.json`` never created
    "whats my score" -> "50 %"; next turn "what is our quiz history"
           -> "no past quiz history has been logged" (a self-contradiction)
    "give me harder questions" -> "I don't have a trivia-question tool
           available in this session" (false; it has just been used twice)
    "play trivia about space" -> a Berlin Wall history question

Player NAMES are
stored with a user-facing ``level`` of easy or hard. No age is asked for,
stored, spoken, or inferred. The kid deny-list applies to EVERYONE unless the
household has put ``grown_up_categories: true`` in the app document.

The whole loop runs against a fixture bank and a fixture generator: no model,
no network, no real app data. No real household names appear here, and no age
appears anywhere at all.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import trivia_game  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# fixtures — a bank and a generator that behave like the real tools
# ══════════════════════════════════════════════════════════════════════════

#: The bank tool's exact envelope, answer key and all (assistant_tools.py:3490).
BANK_REPLY = (
    "[Trivia - Science]\n"
    "What is the chemical symbol for gold?\n"
    "(A) Au  (B) Ag  (C) Fe  (D) Cu\n\n"
    "Ask the user to pick A, B, C, or D. "
    "Do NOT reveal the answer yet - wait for their response. [answer=A]"
)

GEN_REPLY = (
    "[Trivia - space]\n"
    "Which planet has the Great Red Spot?\n"
    "(A) Mars  (B) Jupiter  (C) Saturn  (D) Neptune\n\n"
    "Ask the user to pick A, B, C, or D. "
    "Do NOT reveal the answer yet - wait for their response. "
    "[answer=B|fact=It is a storm wider than Earth.]"
)


class FakeStore:
    """The app-data write path, in memory, with the same load/save contract."""

    def __init__(self, data=None):
        self.data = data if data is not None else {"results": [],
                                                   "last_updated": ""}
        self.saves = 0

    def load(self):
        return json.loads(json.dumps(self.data))

    def save(self, data):
        self.saves += 1
        self.data = json.loads(json.dumps(data, default=str))
        return True


class FakeBank:
    def __init__(self, reply=BANK_REPLY):
        self.reply = reply
        self.calls = []

    def __call__(self, category="random"):
        self.calls.append(category)
        return self.reply


class FakeGenerator:
    def __init__(self, reply=GEN_REPLY, available=True):
        self.reply = reply
        self.available = available
        self.calls = []

    def __call__(self, topic, difficulty="medium", audience=""):
        self.calls.append((topic, difficulty, audience))
        if not self.available:
            raise RuntimeError("model unavailable")
        return self.reply


@pytest.fixture
def game():
    """one household, one persisted file."""
    store = FakeStore()
    bank = FakeBank()
    gen = FakeGenerator()

    def play(said):
        return trivia_game.handle(said, ask_bank=bank, ask_generate=gen,
                                  store=store)

    return play, store, bank, gen


# ══════════════════════════════════════════════════════════════════════════
# the answer key never reaches the player
# ══════════════════════════════════════════════════════════════════════════

def test_the_question_never_carries_the_answer_key(game):
    play, _, _, _ = game
    reply = play("lets play a quiz")
    assert reply, "the plain phrasing must still start a quiz"
    assert "[answer" not in reply.lower()
    # The bank's key is A; the letter must not be presented as the answer.
    assert "answer=A" not in reply
    assert "chemical symbol for gold" in reply


def test_strip_answer_marker_removes_both_envelopes():
    assert "[answer" not in trivia_game.strip_answer_marker(BANK_REPLY)
    assert "[answer" not in trivia_game.strip_answer_marker(GEN_REPLY)
    # Untouched text passes through unchanged.
    assert trivia_game.strip_answer_marker("just a sentence") == \
        "just a sentence"


def test_no_reply_in_a_whole_round_ever_shows_the_key(game):
    play, _, _, _ = game
    said = ["lets play a quiz", "B", "quiz me", "A", "whats my score",
            "what is our quiz history", "stop the quiz"]
    for turn in said:
        reply = play(turn) or ""
        assert "[answer" not in reply.lower(), turn


# ══════════════════════════════════════════════════════════════════════════
# / — the full play loop: question, wrong, right, score
# ══════════════════════════════════════════════════════════════════════════

def test_the_full_play_loop(game):
    play, store, _, _ = game

    first = play("lets play a quiz")
    assert "chemical symbol for gold" in first

    # A wrong answer is TOLD it is wrong, with the right one .
    wrong = play("B")
    assert "Not quite" in wrong
    assert "it was A, Au" in wrong
    assert "0 out of 1 right so far." in wrong
    #...and the next question comes with it, so play continues.
    assert "chemical symbol for gold" in wrong

    right = play("A")
    assert right.startswith("Correct!")
    assert "1 out of 2 right so far." in right

    # The score is read back from the persisted round, not improvised.
    score = play("whats my score")
    assert score == "In this round you're at 1 out of 2 correct."

    #...and history in the SAME conversation agrees with it.
    history = play("what is our quiz history")
    assert "1 of 2 correct this round" in history
    assert "no" not in history.lower().split("\n")[0]


def test_giving_up_reveals_the_answer_without_scoring_it_right(game):
    play, _, _, _ = game
    play("quiz me")
    reply = play("i dont know")
    assert "it was A, Au" in reply
    assert "0 out of 1 right so far." in reply


def test_score_and_history_agree_and_never_contradict(game):
    """Score then history, back to back: they must agree."""
    play, _, _, _ = game
    play("lets play a quiz")
    play("A")
    score = play("whats my score")
    history = play("what is our quiz history")
    assert "1 out of 1" in score
    assert "1 of 1 correct this round" in history


def test_score_with_nothing_played_says_so_instead_of_inventing(game):
    play, _, _, _ = game
    reply = play("whats my score")
    assert "no score to report" in reply
    assert "%" not in reply


def test_history_with_nothing_played_says_so(game):
    play, _, _, _ = game
    reply = play("what is our quiz history")
    assert "No finished trivia rounds are on record yet." in reply


# ══════════════════════════════════════════════════════════════════════════
# chat play persists
# ══════════════════════════════════════════════════════════════════════════

def test_chat_play_writes_the_session_to_app_data(game):
    play, store, _, _ = game
    assert store.saves == 0
    play("lets play a quiz")
    assert store.saves >= 1
    sessions = store.data["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["ended_at"] is None
    assert sessions[0]["pending"]["answer"] == "A"

    play("A")
    assert store.data["sessions"][0]["asked"] == 1
    assert store.data["sessions"][0]["correct"] == 1


def test_stopping_records_the_result_and_the_player_tally(game):
    play, store, _, _ = game
    play("lets play a quiz")
    play("A")
    final = play("stop the quiz")
    assert "Final score: 1 out of 1" in final
    assert store.data["results"][-1]["score"] == 1
    assert store.data["results"][-1]["total"] == 1
    assert store.data["players"]["you"]["correct"] == 1
    assert store.data["sessions"][-1]["ended_at"]


def test_a_bootstrapped_file_keeps_the_shape_the_browser_game_reads(game):
    """``results`` stays the list ``trivia_get_stats`` and the HTML app read."""
    play, store, _, _ = game
    play("quiz me")
    play("A")
    play("stop the quiz")
    assert isinstance(store.data["results"], list)
    entry = store.data["results"][-1]
    assert set(("score", "total", "category", "difficulty", "date")) <= set(entry)


def test_the_round_survives_a_reload_of_the_file(game):
    """State lives in app data, not in memory — a restart mid-round is fine."""
    play, store, bank, gen = game
    play("lets play a quiz")
    reply = trivia_game.handle("A", ask_bank=bank, ask_generate=gen,
                               store=FakeStore(store.data))
    assert reply.startswith("Correct!")


# ══════════════════════════════════════════════════════════════════════════
# "harder questions" is honoured, never denied
# ══════════════════════════════════════════════════════════════════════════

def test_harder_questions_is_served_not_denied(game):
    play, _, _, gen = game
    play("lets play a quiz")
    reply = play("give me harder questions")
    assert reply, "the tool must not be denied"
    assert "don't have a trivia" not in reply.lower()
    assert "do not have a trivia" not in reply.lower()
    assert gen.calls and gen.calls[-1][1] == "hard"


def test_easier_questions_lowers_the_difficulty(game):
    play, _, _, gen = game
    play("lets play a quiz")
    play("give me easier questions")
    assert gen.calls[-1][1] == "easy"


def test_harder_with_no_model_is_honest_not_a_denial_of_the_tool(game):
    play, store, bank, _ = game
    dead = FakeGenerator(available=False)

    def offline(said):
        return trivia_game.handle(said, ask_bank=bank, ask_generate=dead,
                                  store=store)

    offline("lets play a quiz")
    reply = offline("give me harder questions")
    # It still gives a question, and it says WHY it isn't harder.
    assert "difficulty levels" in reply
    assert "chemical symbol for gold" in reply
    assert "don't have a trivia" not in reply.lower()


def test_difficulty_is_persisted_on_the_session(game):
    play, store, _, _ = game
    play("lets play a quiz")
    play("give me harder questions")
    assert store.data["sessions"][-1]["difficulty"] == "hard"


# ══════════════════════════════════════════════════════════════════════════
# a named topic reaches the generator
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("said", [
    "play trivia about space",
    "quiz me on the solar system",
    "space trivia",
])
def test_a_named_topic_goes_to_the_generator(said, game):
    play, _, bank, gen = game
    reply = play(said)
    assert gen.calls, f"{said!r} never reached app_trivia_generate"
    assert "Great Red Spot" in reply
    assert not bank.calls, "the bank must not answer a named topic"


def test_a_bank_category_still_uses_the_bank(game):
    play, _, bank, gen = game
    play("quiz me on science")
    assert bank.calls == ["science"]
    assert not gen.calls


def test_topic_falls_back_to_the_bank_and_says_so_when_the_model_is_down(game):
    _, store, bank, _ = game
    dead = FakeGenerator(available=False)
    reply = trivia_game.handle("play trivia about space", ask_bank=bank,
                               ask_generate=dead, store=store)
    assert "couldn't make a space question" in reply
    assert "from the built-in bank" in reply
    assert "chemical symbol for gold" in reply
    assert "[answer" not in reply.lower()


def test_the_topic_is_recorded_on_the_session(game):
    play, store, _, _ = game
    play("play trivia about space")
    assert store.data["sessions"][-1]["topic"] == "space"


# ══════════════════════════════════════════════════════════════════════════
# family safety: the deny-list, on for everyone by default
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("said", [
    "play trivia about pornography",
    "quiz me on how to make a bomb",
    "trivia about serial killers",
])
def test_unsafe_topics_are_declined_honestly_for_anyone(said, game):
    play, _, bank, gen = game
    reply = play(said)
    assert "a topic I'll make questions about" in reply
    assert not gen.calls and not bank.calls


def test_a_declined_topic_starts_no_session(game):
    play, store, _, _ = game
    play("play trivia about pornography")
    assert not store.data.get("sessions")


def test_the_kid_deny_list_applies_to_everyone_by_default(game):
    """No age is known, so the safe list is on for every round."""
    play, store, bank, gen = game
    reply = play("play trivia about guns")
    assert "not a topic I'll build this round's questions about" in reply
    assert not gen.calls and not bank.calls


def test_a_household_can_lift_the_deny_list_on_purpose(game):
    play, store, _, gen = game
    store.data["grown_up_categories"] = True
    assert play("play trivia about guns")
    assert gen.calls, "the explicit adult switch did not lift the deny-list"


def test_the_switch_must_be_exactly_true(game):
    play, store, _, gen = game
    store.data["grown_up_categories"] = "yes"
    reply = play("play trivia about guns")
    assert "not a topic I'll build this round's questions about" in reply


def test_the_always_deny_list_survives_the_adult_switch(game):
    play, store, bank, gen = game
    store.data["grown_up_categories"] = True
    reply = play("play trivia about pornography")
    assert "a topic I'll make questions about" in reply
    assert not gen.calls and not bank.calls


def test_the_bank_never_draws_the_movies_category_by_default(game):
    """The bank's film entries name mature titles (the note)."""
    play, store, bank, _ = game
    dead = FakeGenerator(available=False)
    trivia_game.handle("play trivia for robin", ask_bank=bank,
                       ask_generate=dead, store=store)
    for _ in range(20):
        trivia_game.handle("next question", ask_bank=bank, ask_generate=dead,
                           store=store)
    assert "movies" not in bank.calls
    assert "random" not in bank.calls


# ══════════════════════════════════════════════════════════════════════════
# level replaces the age band
# ══════════════════════════════════════════════════════════════════════════

def test_a_new_player_defaults_to_easy(game):
    play, store, _, _ = game
    play("play trivia for robin")
    assert store.data["players"]["robin"]["level"] == "easy"
    assert store.data["sessions"][-1]["level"] == "easy"


@pytest.mark.parametrize("said,level", [
    ("quiz me, hard", "hard"),
    ("easy trivia", "easy"),
    ("make it harder", "hard"),
    ("hard mode", "hard"),
    ("give me harder questions", "hard"),
    ("give me easier questions", "easy"),
])
def test_a_level_is_selected_by_phrase(said, level, game):
    play, store, _, _ = game
    play("lets play a quiz")
    assert play(said) is not None, said
    assert store.data["sessions"][-1]["level"] == level, said


def test_a_chosen_level_sticks_to_the_player(game):
    play, store, _, _ = game
    play("lets play a quiz")
    play("give me harder questions")
    assert store.data["players"]["you"]["level"] == "hard"


def test_easy_is_a_level_not_a_topic(game):
    play, store, _, _ = game
    play("easy trivia")
    assert store.data["sessions"][-1]["topic"] in (None, "")
    assert store.data["sessions"][-1]["level"] == "easy"


def test_the_generator_is_told_an_audience_never_an_age(game):
    play, _, _, gen = game
    play("quiz me, hard")
    assert gen.calls
    audience = gen.calls[-1][2]
    assert audience == "an adult general-knowledge audience"
    assert not any(ch.isdigit() for ch in audience)


def test_quiz_the_kids_is_still_a_player_not_an_age(game):
    play, store, _, _ = game
    play("quiz the kids")
    assert store.data["sessions"][-1]["player"] == "kids"
    assert store.data["sessions"][-1]["level"] == "easy"


# ── no age, anywhere ────────────────────────────────────────────────────────

def test_no_age_is_stored_even_when_one_is_volunteered(game):
    play, store, _, _ = game
    play("play trivia for robin, she's 9")
    document = json.dumps(store.data)
    assert '"age"' not in document
    assert store.data["players"]["robin"] == {
        "name": "robin", "level": "easy", "asked": 0, "correct": 0}
    assert store.data["sessions"][-1]["player"] == "robin"
    assert "band" not in store.data["sessions"][-1]


def test_a_reply_never_states_a_players_age(game):
    play, store, _, _ = game
    play("play trivia for robin, she's 9")
    replies = [play("quiz me"), play("A"), play("whats my score"),
               play("what is our quiz history"), play("stop the quiz")]
    for reply in replies:
        assert reply is not None
        assert "9" not in reply.replace("2nd", "").replace("5th", "") or \
            "out of" in reply, reply
        assert "years old" not in reply.lower()
        assert "age" not in reply.lower()


def test_an_age_left_by_the_old_shape_is_discarded_on_load(game):
    """An age-band document (the older shape) loads as a level, age dropped."""
    play, store, _, _ = game
    store.data["players"] = {
        "robin": {"name": "robin", "age": 9, "asked": 4, "correct": 3}}
    store.data["sessions"] = [{
        "id": "old", "started_at": "2026-09-01T00:00:00", "ended_at": None,
        "topic": None, "difficulty": "medium", "source": "bank",
        "player": "robin", "band": "kid", "asked": 0, "correct": 0,
        "pending": None, "history": []}]
    play("quiz me")
    robin = store.data["players"]["robin"]
    assert "age" not in robin
    assert robin["level"] == "easy"
    assert robin["asked"] == 4 and robin["correct"] == 3, \
        "the migration threw away the score with the age"
    assert '"age"' not in json.dumps(store.data)
    assert '"band"' not in json.dumps(store.data)


def test_a_migrated_age_is_never_spoken(game):
    play, store, _, _ = game
    store.data["players"] = {
        "robin": {"name": "robin", "age": 9, "asked": 0, "correct": 0}}
    for said in ("play trivia for robin", "whats my score",
                 "what is our quiz history", "stop the quiz"):
        reply = play(said) or ""
        assert "age" not in reply.lower()
        assert " 9" not in reply


# ══════════════════════════════════════════════════════════════════════════
# stop
# ══════════════════════════════════════════════════════════════════════════

def test_stopping_with_no_round_does_not_pretend(game):
    play, _, _, _ = game
    reply = play("stop the quiz")
    assert "no quiz running" in reply.lower()


# ══════════════════════════════════════════════════════════════════════════
# the decline corpus — none of this is a trivia turn
# ══════════════════════════════════════════════════════════════════════════

DECLINE = [
    "trivia night is tomorrow",
    "we won trivia night at the pub",
    "the trivia app wont load",
    "add trivia night to the calendar",
    "that test was harder than I expected",
    "the drive home is easier at this hour",
    "whats my score in the fantasy league",
    "turn off the kitchen light",
    "what is the weather tomorrow",
    "add milk to the shopping list",
    "how many days of food do we have",
    "play some music",
    "what time is it",
    "set a timer for ten minutes",
    "tell me about the moon landing",
    "a",
    "yes",
]


@pytest.mark.parametrize("said", DECLINE)
def test_the_decline_corpus_never_starts_a_quiz(said, game):
    play, store, bank, gen = game
    assert play(said) is None, said
    assert not bank.calls and not gen.calls
    assert not store.data.get("sessions")


def test_a_bare_letter_is_only_an_answer_when_a_question_is_pending(game):
    play, _, bank, _ = game
    assert play("C") is None
    play("lets play a quiz")
    assert play("C") is not None


def test_a_topic_phrase_is_not_swallowed_by_the_bank(game):
    """"play trivia about space" must not silently become a random question."""
    play, _, bank, gen = game
    play("play trivia about space")
    assert gen.calls[0][0] == "space"
    assert bank.calls == []


def test_the_decline_corpus_still_declines_after_a_round_is_running(game):
    """A live session must not turn every sentence into a quiz turn."""
    play, _, _, _ = game
    play("lets play a quiz")
    for said in ["turn off the kitchen light", "what time is it",
                 "add milk to the shopping list", "trivia night is tomorrow"]:
        assert play(said) is None, said


# ══════════════════════════════════════════════════════════════════════════
# the api_server wiring: the surface a chat turn actually hits
# ══════════════════════════════════════════════════════════════════════════

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


@pytest.fixture
def api_bench(monkeypatch):
    """The fastpath wired to fixture tools and an in-memory store."""
    import puffpai.api_server as api

    bank = _FakeTool(BANK_REPLY)
    gen = _FakeTool(GEN_REPLY)
    store = FakeStore()
    monkeypatch.setattr(api, "_get_assistant_tools",
                        lambda: _FakeTools(app_trivia_question=bank,
                                           app_trivia_generate=gen))
    monkeypatch.setattr(trivia_game, "_AppDataStore", lambda: store)
    # Trivia declares requires.ollama, and _try_trivia_start answers with
    # the app's unavailability when the box has no model. These tests are about
    # the GAME, so the requirement is pinned met rather than left to whatever
    # this machine's ollama says at the moment the probe runs.
    monkeypatch.setattr(api, "_requirement_availability",
                        lambda force=False, app_id=None: {
                            "ollama": True, "home_assistant": True,
                            "network": True})
    return api, bank, gen, store


def test_lets_play_a_quiz_does_not_leak_the_key_through_the_api(api_bench):
    api, _, _, _ = api_bench
    reply = api._try_trivia_start("lets play a quiz")
    assert reply and "[answer" not in reply.lower()


def test_play_trivia_about_space_reaches_the_generator_through_the_api(api_bench):
    """The named-topic case, at the surface a chat turn POSTs to."""
    api, bank, gen, _ = api_bench
    reply = api._try_trivia_start("play trivia about space")
    assert gen.calls, "app_trivia_generate was never reached"
    assert gen.calls[0]["topic"] == "space"
    assert gen.calls[0]["difficulty"] == "medium"
    assert bank.calls == []
    assert "Great Red Spot" in reply
    assert "[answer" not in reply.lower()


def test_harder_questions_is_not_denied_through_the_api(api_bench):
    api, _, gen, _ = api_bench
    api._try_trivia_start("lets play a quiz")
    reply = api._try_trivia_start("give me harder questions")
    assert reply
    assert "don't have a trivia" not in reply.lower()
    assert gen.calls[-1]["difficulty"] == "hard"


def test_the_reply_guards_strip_a_leaked_key_from_any_path(api_bench):
    """Defence in depth: the agent loop sees the raw tool string too."""
    api, _, _, _ = api_bench
    leaked = ("Here you go. What is the chemical symbol for gold? "
              "(A) Au  (B) Ag [answer=A]")
    assert "[answer" not in api._guard_chat_reply(leaked).lower()
    assert "[answer" not in api._clean_response(leaked).lower()
    assert "[answer" not in api._strip_trivia_answer(leaked).lower()


@pytest.mark.parametrize("said", DECLINE)
def test_the_api_fastpath_declines_the_corpus(said, api_bench):
    api, bank, gen, store = api_bench
    assert api._try_trivia_start(said) is None, said
    assert not bank.calls and not gen.calls
    assert not store.data.get("sessions")


def test_switching_topic_mid_round_banks_the_old_score_out_loud(game):
    """A new topic starts a new round; the score restarting is announced."""
    play, store, _, _ = game
    play("lets play a quiz")
    play("A")
    reply = play("play trivia about space")
    assert "Wrapping that round at 1 out of 1" in reply
    assert store.data["results"][-1]["score"] == 1
    assert store.data["sessions"][-1]["topic"] == "space"
