"""Trivia as a real game, not a question vending machine.

A single fastpath that calls ``app_trivia_question`` and returns the tool's
raw scaffolding string verbatim leaks the answer key (``[answer=C]``) and has
no notion of a round: a wrong answer produces a brand new question with no
feedback, "what's my score" is answered from short-term chat context (and
contradicts "quiz history" one turn later), "harder questions" can produce a
false claim that no trivia tool exists, and "play trivia about space" drops
the topic even though ``app_trivia_generate`` exists for exactly that case.

This module is the missing middle: a small game-state machine persisted in the
``trivia`` app-data file, so a quiz survives the turn that created it and every
score claim is read back from disk instead of improvised.

Shape written into ``trivia.json`` (additive -- ``results`` keeps the shape
``AppData.trivia_get_stats`` and the browser game already read):

    {
      "results":  [ {score, total, category, difficulty, date} ],
      "sessions": [ {id, started_at, ended_at, topic, difficulty, source,
                     player, level, asked, correct, pending, history} ],
      "players":  { "<name>": {name, level, asked, correct} },
      "last_updated": "..."
    }

Difficulty is a user-facing ``level`` -- ``easy`` or ``hard``, chosen out
loud -- and NOT an age band. No age is asked for, stored, or spoken; an ``age``
left in the file by an older age-banded shape is discarded the first time the
document is read. The under-13
deny-list applies to EVERYONE by default; only an explicit
``grown_up_categories: true`` in the app document lifts it.

The two question sources are injected (``ask_bank`` / ``ask_generate``) so the
caller owns tool resolution and the tests can run the whole loop against a
fixture bank with no model and no network.
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timezone

LETTERS = ("A", "B", "C", "D")

#: Every reply is scrubbed of this before it reaches a user or the transcript.
#: The marker is how the tools hand the answer key to the grading layer; it is
#: never something a player may see.
ANSWER_MARKER_RE = re.compile(r"\s*\[answer=[^\]]*\]", re.IGNORECASE)

#: The question tools' old stage direction to the orchestrating model. It is
#: gone at both sources now, but a question already persisted in a household's
#: `sessions[].pending` still carries it, and so would any future tool that
#: forgets. Scrubbed on the way out, next to the answer key, for the same
#: reason: an instruction addressed to a model is not something a person reads.
STAGE_DIRECTION_RE = re.compile(
    r"\s*Ask the user to pick A,\s*B,\s*C,?\s*or D\.?"
    r"(?:\s*Do NOT reveal the answer yet\s*[—–-]?\s*"
    r"wait for their response\.?)?",
    re.IGNORECASE)

#: The nine categories the bundled bank actually has. Anything else is a topic
#: for the generator rather than a silent fallback to "random".
BANK_CATEGORIES = ("science", "history", "geography", "movies", "music",
                   "sports", "technology", "nature", "literature")

_START_RE = re.compile(
    r"(?i)\b(?:lets|let's|let us)\s+play\s+(?:a\s+|some\s+)?(?:quiz|trivia)"
    r"|\bplay\s+(?:a\s+|some\s+)?(?:quiz|trivia)\b"
    r"|\bstart\s+(?:a\s+|the\s+)?(?:quiz|trivia)\b"
    r"|\bquiz\s+me\b|\btrivia\s+(?:question|time|round)\b"
    r"|\b(?:give|ask)\s+me\s+(?:a\s+|another\s+)?(?:trivia|quiz)\b"
    r"|\bask\s+me\s+a\s+(?:trivia\s+)?question\b"
    r"|\btest\s+me\b"
    r"|\btrivia\s+(?:about|on)\b"
    r"|\bquiz\s+(?:the\s+)?(?:kids|kiddos|children|little\s+ones)\b"
    r"|\btrivia\s+(?:for|with)\s+[a-z]"
    r"|\bnext\s+question\b|\banother\s+question\b")

#: "space trivia", "dinosaur trivia" -- anchored so "trivia night is tomorrow"
#: and "the trivia app" cannot match (decline corpus).
_TOPIC_FIRST_RE = re.compile(
    r"(?i)^(?:can\s+we\s+do\s+|lets\s+do\s+|let's\s+do\s+|do\s+|give\s+me\s+)?"
    r"(?:some\s+)?([a-z][a-z0-9 '\-]{1,28}?)\s+trivia\s*[!.?]*$")

#: The topic tail of "play trivia about space" / "quiz me on the solar system".
_TOPIC_TAIL_RE = re.compile(
    r"(?i)\b(?:about|on|regarding|based\s+on)\s+([a-z0-9][a-z0-9 '\-]{1,40})\s*[!.?]*$")

_HARDER_RE = re.compile(
    r"(?i)\b(?:harder|tougher|more\s+difficult|more\s+challenging|"
    r"less\s+easy|hard\s+mode)\b")
_EASIER_RE = re.compile(
    r"(?i)\b(?:easier|simpler|less\s+hard|easy\s+mode|too\s+hard)\b")
#: Difficulty phrasing only counts as a trivia turn when it names the game or a
#: round is already running -- "that was harder than I thought" is not a quiz.
_DIFFICULTY_SUBJECT_RE = re.compile(
    r"(?i)\b(?:questions?|quiz|quizzes|trivia|round|ones?)\b")

_SCORE_RE = re.compile(
    r"(?i)\b(?:what(?:'s|s| is)\s+(?:my|our|the)\s+(?:current\s+)?"
    r"(?:trivia\s+|quiz\s+|game\s+)?score"
    # ...but "my score in the fantasy league" is somebody else's game.
    r"(?:\s*[?.!]*$|\s+(?:so\s+far|now|right\s+now)\b"
    r"|\s+(?:in|for|on|at)\s+(?:the\s+|this\s+|our\s+)?"
    r"(?:quiz|trivia|round|game)\b)"
    r"|\bhow\s+am\s+i\s+doing\b"
    r"|\bhow\s+are\s+we\s+doing\s+(?:in|at|on)\s+(?:the\s+)?(?:quiz|trivia)\b"
    r"|\bmy\s+(?:quiz|trivia)\s+score\b"
    r"|\bscore\s+so\s+far\b)")

_HISTORY_RE = re.compile(
    r"(?i)\b(?:quiz|trivia)\s+(?:history|stats|statistics|scores|record|results)\b"
    r"|\b(?:history|stats|record)\s+(?:of|for)\s+(?:the\s+|our\s+|my\s+)?(?:quiz|trivia)\b"
    r"|\bhow\s+(?:did|have)\s+(?:we|i)\s+(?:do|done)\s+(?:in|at|on)\s+(?:the\s+)?(?:quiz|trivia)\b")

_STOP_RE = re.compile(
    r"(?i)\b(?:stop|end|quit|finish|cancel)\s+(?:the\s+|this\s+|our\s+|my\s+)?"
    r"(?:quiz|trivia|game|round)\b"
    r"|\b(?:no\s+more|done\s+with)\s+(?:the\s+)?(?:quiz|trivia|questions)\b")

_ANSWER_RE = re.compile(
    r"(?i)^\s*(?:the\s+answer\s+is\s+|is\s+it\s+|i(?:'ll)?\s+say\s+|i\s+pick\s+"
    r"|answer\s+|option\s+|my\s+answer\s+is\s+)?\(?([abcd])\)?[.!?]*\s*$")
_GIVE_UP_RE = re.compile(
    r"(?i)^\s*(?:i\s+)?(?:dont|don't|do not)\s+know\s*[.!?]*$"
    r"|^\s*(?:skip|pass|no\s+idea|give\s+up|tell\s+me)\s*[.!?]*$")

#: Topics this appliance will not write children's quiz questions about. A
#: short, honest decline beats a silently sanitised question.
_UNSAFE_TOPIC_RE = re.compile(
    r"(?i)\b(?:porn|pornography|sex|sexual|nude|nudity|erotic|fetish"
    r"|rape|incest"
    r"|cocaine|heroin|meth|methamphetamine|fentanyl|weed|marijuana|getting\s+high"
    r"|suicide|self[\s-]?harm|kill\s+(?:myself|yourself)"
    r"|torture|gore|mutilation|beheading"
    r"|how\s+to\s+(?:make|build)\s+(?:a\s+)?(?:bomb|gun|explosive|meth)"
    r"|bomb\s+making|school\s+shooting|serial\s+killers?)\b")

#: Applied ON TOP of the list above for EVERY round.
#: Nothing here is unsafe for an adult; all of it is material a parent would
#: not want a nine-year-old's quiz built around, and a family appliance in a
#: shared room cannot know who is standing in front of it. Lifted only by an
#: explicit `grown_up_categories: true` in the app document.
_UNSAFE_FOR_KIDS_RE = re.compile(
    r"(?i)\b(?:murder|homicide|horror|slasher|zombies?"
    r"|guns?|firearms?|weapons?|war\s+crimes?"
    r"|alcohol|beer|wine|whiskey|vodka|cocktails?|drinking"
    r"|gambling|casinos?|betting|poker"
    r"|drugs?|smoking|vaping|cigarettes?"
    r"|dating|romance)\b")

_MAX_TURN_CHARS = 200

#: How long an utterance may be and still be read as a bare level change
#: ("make it harder") while a round is running. Beyond this it is a
#: sentence that merely contains the word.
_MAX_UNNAMED_LEVEL_WORDS = 5

# ── levels ───────────────────────────────────────────────────────────────────
#
# Difficulty is a thing a person SAYS ("quiz me, hard"), not a thing inferred
# from a birthday, so nothing here reads, stores, or speaks an age. Safety
# does not ride on knowing who is playing: the kid deny-list is on for everyone unless the household has
# explicitly turned grown-up categories on in the app document.

#: The two user-facing levels. Anything else is not a level.
LEVELS = ('easy', 'hard')

#: What a player gets when nobody has said otherwise.
DEFAULT_LEVEL = 'easy'

#: The key that lifts the kid deny-list. ABSENT by default -- never written
#: here, only read, so a household has to put it there on purpose.
GROWN_UP_KEY = 'grown_up_categories'

#: The bundled bank carries no difficulty metadata, so the safe default is
#: expressed as the set of categories it may draw from. "movies" is withheld
#: (the bank's film entries reference mature titles).
SAFE_CATEGORIES = ("science", "geography", "nature", "music", "history",
                   "sports", "technology", "literature")

#: What the generator is told about its audience. Never an age, never a number.
LEVEL_AUDIENCE = {
    'easy': "a general family audience",
    'hard': "an adult general-knowledge audience",
}


def normalize_level(level):
    """``'easy'``/``'hard'``, or ``None`` when this isn't a level."""
    if not isinstance(level, str):
        return None
    value = level.strip().lower()
    return value if value in LEVELS else None


def _grown_up(data):
    """True only when the app document explicitly says so."""
    return bool(isinstance(data, dict) and data.get(GROWN_UP_KEY) is True)


def _allowed_categories(data):
    return BANK_CATEGORIES if _grown_up(data) else SAFE_CATEGORIES


def _topic_allowed(topic, data):
    """``(ok, scope)`` for a requested topic under the household's rules."""
    if not topic:
        return True, ""
    if _UNSAFE_TOPIC_RE.search(topic):
        return False, 'always'
    if not _grown_up(data) and _UNSAFE_FOR_KIDS_RE.search(topic):
        return False, 'kids'
    return True, ""


# ── output hygiene ───────────────────────────────────────────────────────────

def strip_answer_marker(text):
    """Remove the tools' internal scaffolding from a reply.

    Two things, both addressed to the MODEL and never to a person: the
    ``[answer=X]`` / ``[answer=X|fact=...]`` key and the "Ask the user to
    pick A, B, C, or D. Do NOT reveal the answer yet" stage direction, either
    of which would otherwise reach the user verbatim on every question.

    Applied to every outbound reply on both the chat and voice paths, so no
    routing surface -- fastpath, agent loop, or legacy loop -- can print
    either. The name is kept because every call site already uses it.
    """
    if not text:
        return text
    raw = str(text)
    if "[answer" not in raw.lower() and "ask the user to pick" not in raw.lower():
        return text
    return STAGE_DIRECTION_RE.sub("", ANSWER_MARKER_RE.sub("", raw)).strip()


# ── parsing the tool output into a question object ───────────────────────────

def parse_question(raw):
    """Turn a bank/generator string into ``{prompt, options, answer, fact}``.

    Both tools emit the same envelope: a ``[Trivia — Label]`` header, the
    question, ``(A) .. (D)`` options and a trailing ``[answer=X]`` (the
    generator adds ``|fact=...``). Returns ``None`` when the shape is not
    recognised, so a malformed model reply degrades to "couldn't get one"
    rather than to a leaked or wrong grade.
    """
    if not raw:
        return None
    text = str(raw)
    marker = re.search(r"\[answer=([ABCD])(?:\|fact=(.*?))?\]", text,
                       re.IGNORECASE)
    if not marker:
        return None
    answer = marker.group(1).upper()
    fact = (marker.group(2) or "").strip()
    body = strip_answer_marker(text)
    options = re.findall(r"\(([ABCD])\)\s*([^()\n]+?)(?=\s{2,}\(|\s*$)", body)
    opts = {letter.upper(): value.strip() for letter, value in options}
    if len(opts) < 2 or answer not in opts:
        return None
    return {"prompt": body, "options": opts, "answer": answer, "fact": fact}


def _render(question, lead=""):
    """The question exactly as the player sees it -- never the key."""
    prompt = strip_answer_marker(question["prompt"])
    return f"{lead}{prompt}" if lead else prompt


# ── persistence ──────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).isoformat()


class _AppDataStore:
    """Default store: the app-data write path every other app already uses."""

    def load(self):
        from puffpai.app_data import AppData
        return AppData().get_data('trivia') or {}

    def save(self, data):
        from puffpai.app_data import AppData
        return AppData().set_data('trivia', data)


def _normalize(data):
    """Bootstrap the game keys on first play -- the file may not exist yet.

    Also the level migration: a document written under the older age-banded
    shape carries ``players[<name>].age`` and ``sessions[].band``. Both are
    DROPPED here, on the first read, before anything can select on them or
    say them out loud; the player keeps a ``level`` instead. Nothing writes
    an age back, so the field leaves the file at the next save. The
    ``grown_up_categories`` switch is deliberately NOT defaulted in -- it
    exists only if the household put it there.
    """
    if not isinstance(data, dict):
        data = {}
    data.setdefault('results', [])
    data.setdefault('sessions', [])
    players = data.setdefault('players', {})
    if isinstance(players, dict):
        for key, profile in list(players.items()):
            if not isinstance(profile, dict):
                continue
            profile.pop('age', None)
            profile['level'] = (normalize_level(profile.get('level'))
                                or DEFAULT_LEVEL)
    for session in (data.get('sessions') or []):
        if not isinstance(session, dict):
            continue
        session.pop('band', None)
        session['level'] = (normalize_level(session.get('level'))
                            or DEFAULT_LEVEL)
    return data


def _active(data):
    for session in reversed(data.get('sessions') or []):
        if not session.get('ended_at'):
            return session
    return None


def _new_session(data, topic=None, difficulty="medium", source="bank",
                 player="you", level=None):
    session = {
        'id': _now().replace(':', '').replace('-', '')[:18],
        'started_at': _now(),
        'ended_at': None,
        'topic': topic,
        'difficulty': difficulty,
        'source': source,
        'player': player,
        'level': normalize_level(level) or DEFAULT_LEVEL,
        'asked': 0,
        'correct': 0,
        'pending': None,
        'history': [],
    }
    data.setdefault('sessions', []).append(session)
    # Keep the file bounded; a household never needs more than the last 50.
    data['sessions'] = data['sessions'][-50:]
    return session


def _close(data, session, player=None):
    session['ended_at'] = _now()
    session['pending'] = None
    name = player or session.get('player') or 'you'
    if session.get('asked'):
        data.setdefault('results', []).append({
            'score': session.get('correct', 0),
            'total': session.get('asked', 0),
            'category': session.get('topic') or 'mixed',
            'difficulty': session.get('difficulty', 'medium'),
            'player': name,
            'date': session['ended_at'],
        })
        data['results'] = data['results'][-200:]
        tally = _player(data, name)
        tally['asked'] = tally.get('asked', 0) + session.get('asked', 0)
        tally['correct'] = tally.get('correct', 0) + session.get('correct', 0)


def _player(data, name):
    """The stored profile for a player: ``{name, level, asked, correct}``.

    No age, ever. ``level`` is what the player last asked for, and
    defaults to ``easy``.
    """
    players = data.setdefault('players', {})
    key = (name or 'you').strip().lower()
    profile = players.setdefault(
        key, {'name': key, 'level': DEFAULT_LEVEL, 'asked': 0, 'correct': 0})
    profile.setdefault('name', key)
    profile.pop('age', None)
    profile['level'] = normalize_level(profile.get('level')) or DEFAULT_LEVEL
    return profile


def _score_line(session):
    asked = session.get('asked', 0)
    correct = session.get('correct', 0)
    if not asked:
        return "No questions answered yet in this round."
    return f"{correct} out of {asked} right so far."


# ── question sourcing ────────────────────────────────────────────────────────

def _default_bank(category="random"):
    from puffpai.api_server import _get_assistant_tools  # deferred: avoids a cycle
    tool = _get_assistant_tools().tool_dict.get('app_trivia_question')
    return tool.invoke(category) if tool is not None else None


def _default_generate(topic, difficulty="medium", audience=""):
    from puffpai.api_server import _get_assistant_tools  # deferred: avoids a cycle
    tool = _get_assistant_tools().tool_dict.get('app_trivia_generate')
    if tool is None:
        return None
    return tool.invoke({'topic': topic, 'difficulty': difficulty,
                        'grade_level': audience})


def _bank_category(session, data=None):
    """A category this household is allowed to draw from.

    The bundled bank carries no safety metadata, so the deny-list is enforced
    by choosing the category here rather than passing "random" and hoping.
    """
    topic = session.get('topic')
    allowed = _allowed_categories(data)
    if topic and topic in allowed:
        return topic
    if set(allowed) == set(BANK_CATEGORIES):
        return 'random'
    return random.choice(list(allowed))


def _next_question(session, ask_bank, ask_generate, data=None):
    """Fetch the next question for a session. Returns ``(question, note)``.

    ``note`` is an honest sentence to prepend when we could not do exactly what
    was asked -- a topic the model could not serve, or a difficulty the bundled
    bank has no tiers for. Never silence.
    """
    topic = session.get('topic')
    difficulty = session.get('difficulty', 'medium')
    level = normalize_level(session.get('level')) or DEFAULT_LEVEL
    audience = LEVEL_AUDIENCE.get(level, LEVEL_AUDIENCE[DEFAULT_LEVEL])
    wants_model = (bool(topic and topic not in BANK_CATEGORIES)
                   or difficulty != 'medium')
    if wants_model:
        subject = topic or 'general knowledge'
        try:
            raw = ask_generate(subject, difficulty, audience)
        except TypeError:
            # Injected two-arg generators (older fixtures) still work.
            raw = _call2(ask_generate, subject, difficulty)
        except Exception:
            raw = None
        question = parse_question(raw)
        if question:
            session['source'] = 'generated'
            return question, ""
        # Model unavailable (or unparseable) -- say so, then fall back.
        session['source'] = 'bank'
        fallback = parse_question(_call(ask_bank, _bank_category(session, data)))
        if not fallback:
            return None, ""
        if topic and topic not in BANK_CATEGORIES:
            # Name the topic and say plainly that this is NOT it, so a bank
            # question never arrives under a request for a space question with
            # nothing said about the swap.
            note = (f"I couldn't make a {topic} question just now, so here's "
                    f"one from the built-in bank instead.\n\n")
        elif difficulty != 'medium':
            note = ("The built-in bank doesn't come in difficulty levels and "
                    "the question writer is unavailable, so this one is a "
                    "standard question.\n\n")
        else:
            note = ("The question writer is unavailable, so this one comes "
                    "from the built-in bank.\n\n")
        return fallback, note
    question = parse_question(_call(ask_bank, _bank_category(session, data)))
    return (question, "") if question else (None, "")


def _call(fn, arg):
    try:
        return fn(arg)
    except Exception:
        return None


def _call2(fn, a, b):
    try:
        return fn(a, b)
    except Exception:
        return None


# ── intent detection ─────────────────────────────────────────────────────────

def _topic_of(text):
    """The requested topic, or None. Normalised to lowercase words."""
    tail = _TOPIC_TAIL_RE.search(text)
    if tail and _START_RE.search(text):
        return tail.group(1).strip().lower()
    first = _TOPIC_FIRST_RE.match(text.strip())
    if first:
        candidate = first.group(1).strip().lower()
        stop = {'the', 'a', 'an', 'our', 'my', 'that', 'this', 'no', 'some',
                'at', 'in', 'for', 'of', 'is', 'was', 'and', 'tonight',
                'tomorrow', 'last', 'next', 'won', 'lost',
                # "easy trivia" names a LEVEL, not a subject.
                'easy', 'hard', 'harder', 'easier'}
        if candidate and candidate not in stop:
            return candidate
    return None


#: "play trivia for Casey", "quiz Casey", "trivia for the kids".
_FOR_PLAYER_RE = re.compile(
    r"(?i)\b(?:for|with)\s+(?:the\s+)?([a-z][a-z'\-]{1,20})\b")
_KIDS_RE = re.compile(r"(?i)\b(?:the\s+)?(?:kids|kiddos|children|little\s+ones)\b")

#: A level said in so many words: "quiz me, hard", "easy trivia", "hard ones".
#: This replaces age banding. Nothing reads an age, so an
#: age offered in the same breath ("she's 9") is simply not looked at.
_LEVEL_WORD_RE = re.compile(r"(?i)\b(easy|hard)\b")

#: Words after "for"/"with" that are never a person.
_NOT_A_NAME = {
    'fun', 'me', 'us', 'you', 'now', 'later', 'today', 'tonight', 'a',
    'an', 'the', 'some', 'once', 'while', 'minute', 'bit', 'everyone',
    'anybody', 'anyone', 'practice', 'school', 'work', 'points', 'real',
}


def _player_and_level(text, data):
    """``(player, level)`` for this turn, from the phrasing plus the store.

    The level is what the player last SAID, remembered per player and
    defaulting to ``easy``. Nothing here reads, stores or infers an age.
    """
    if _KIDS_RE.search(text):
        return 'kids', _player(data, 'kids').get('level') or DEFAULT_LEVEL

    name = None
    found = _FOR_PLAYER_RE.search(text)
    if found and found.group(1).lower() not in _NOT_A_NAME:
        name = found.group(1).lower()
    if name is None:
        return 'you', None

    return name, _player(data, name).get('level') or DEFAULT_LEVEL


def _difficulty_turn(text, session):
    """The level this turn asks for, or None. Only easy or hard.

    Three phrasings have to work: a comparative ("make it harder"), a level
    named beside the game ("quiz me, hard", "easy trivia"), and a mode
    ("hard mode"). The game must be NAMED for a bare comparative to count --
    "that test was harder than I expected" is not a quiz turn -- unless a
    round is already running, in which case "make it harder" plainly is.
    """
    if _HARDER_RE.search(text):
        direction = 'hard'
    elif _EASIER_RE.search(text):
        direction = 'easy'
    else:
        said = _LEVEL_WORD_RE.search(text)
        if not said:
            return None
        direction = said.group(1).lower()
    if _DIFFICULTY_SUBJECT_RE.search(text):
        return direction
    # Unnamed, so it leans on the round being live -- and on being SHORT. "make
    # it harder" is a quiz turn mid-round; "turn off the hard drive light" is
    # a sentence that happens to contain the word, and a running round must not
    # give the game first claim on it.
    if session is not None and len(text.split()) <= _MAX_UNNAMED_LEVEL_WORDS:
        return direction
    return None


def claims(user_message):
    """True when this utterance unambiguously NAMES the game. Pure, no I/O.

    The caller needs this before it may say "Trivia can't run on this build":
    an app's unavailability is an answer to a question about that app, never
    a reply to every sentence a household says. Deliberately the strict half
    of :func:`handle`'s gate -- a bare answer letter and a bare comparative
    are trivia turns only while a round is running, which only the store
    knows, so they are not claimed here.
    """
    text = (user_message or "").strip()
    if not text or len(text) > _MAX_TURN_CHARS:
        return False
    return bool(_START_RE.search(text) or _SCORE_RE.search(text)
                or _HISTORY_RE.search(text) or _STOP_RE.search(text)
                or _TOPIC_FIRST_RE.match(text)
                or _difficulty_turn(text, None))


# ── the state machine ────────────────────────────────────────────────────────

def handle(user_message, ask_bank=None, ask_generate=None, store=None):
    """Serve one trivia turn, or return ``None`` if this isn't one.

    The ONLY entry point. Ordering matters: an answer to a pending question
    ("C") is graded before anything else, so a bare letter is never mistaken
    for a new intent.
    """
    text = (user_message or "").strip()
    if not text or len(text) > _MAX_TURN_CHARS:
        return None
    ask_bank = ask_bank or _default_bank
    ask_generate = ask_generate or _default_generate
    store = store or _AppDataStore()

    looks_trivia = bool(
        _START_RE.search(text) or _SCORE_RE.search(text)
        or _HISTORY_RE.search(text) or _STOP_RE.search(text)
        or _TOPIC_FIRST_RE.match(text)
        or _difficulty_turn(text, None))
    maybe_answer = bool(_ANSWER_RE.match(text) or _GIVE_UP_RE.match(text))
    # A level word that does NOT name the game -- "make it harder", "hard
    # mode" -- is a quiz turn only while a round is running. Cheap to check
    # here; the store decides below.
    maybe_level = bool(_HARDER_RE.search(text) or _EASIER_RE.search(text)
                       or _LEVEL_WORD_RE.search(text))
    if not looks_trivia and not maybe_answer and not maybe_level:
        return None

    data = _normalize(store.load())
    session = _active(data)

    # 1. grading an outstanding question
    if session and session.get('pending') and maybe_answer:
        return _grade(data, session, text, ask_bank, ask_generate, store)

    # A bare letter with no question in flight, or a level word with no round
    # running, is not a trivia turn.
    if not looks_trivia and not (maybe_level and session is not None):
        return None

    # 2. ending the round
    if _STOP_RE.search(text):
        return _stop(data, session, store)

    # 3. score / history reads -- always from the file, never improvised
    if _SCORE_RE.search(text):
        return _report_score(data, session)
    if _HISTORY_RE.search(text):
        return _report_history(data)

    # 4. level change (easy / hard)
    direction = _difficulty_turn(text, session)
    if direction and not _START_RE.search(text):
        return _change_difficulty(data, session, direction, ask_bank,
                                  ask_generate, store)

    # 5. start (optionally on a topic, for a named player, at a level)
    topic = _topic_of(text)
    who, level = _player_and_level(text, data)
    if who == 'you' and level is None and session:
        who, level = session.get('player', 'you'), session.get('level')
    level = normalize_level(direction or level) or DEFAULT_LEVEL
    ok, scope = _topic_allowed(topic, data)
    if not ok:
        if scope == 'kids':
            return ("That's not a topic I'll build this round's questions "
                    "about. Pick another one and I'm in.")
        return ("I'll write a quiz about almost anything, but not that one — "
                "it isn't a topic I'll make questions about. Name another and "
                "I'm in.")
    difficulty = 'hard' if direction == 'hard' else (
        'easy' if direction == 'easy' else 'medium')
    if session and session.get('pending') and not topic and \
            direction is None and not _START_RE.search(text):
        return None
    switched = ""
    if session and (topic or difficulty != 'medium'
                    or who != session.get('player', 'you')):
        # A new topic/player/difficulty is a NEW round. Close the old one so
        # its score lands in the history rather than silently vanishing, and
        # say so — the score restarting must never look like a bug.
        if session.get('asked'):
            switched = (f"Wrapping that round at {session['correct']} out of "
                        f"{session['asked']}. Starting a new one.\n\n")
        _close(data, session)
        session = None
    created = session is None
    if created:
        session = _new_session(data, topic=topic, difficulty=difficulty,
                               player=who, level=level)
        if direction:
            # A level said out loud is remembered for that player.
            _player(data, who)['level'] = level
    else:
        session['topic'] = session.get('topic') or topic

    question, note = _next_question(session, ask_bank, ask_generate, data)
    if question is None:
        # No question source answered. Leave no half-started round behind and
        # write nothing, so the turn falls through to the ordinary path.
        if created:
            data['sessions'].remove(session)
        return None
    session['pending'] = question
    store.save(data)
    return _render(question, lead=switched + note)


def _grade(data, session, text, ask_bank, ask_generate, store):
    pending = session['pending']
    correct = pending['answer']
    gave_up = bool(_GIVE_UP_RE.match(text))
    match = _ANSWER_RE.match(text)
    given = match.group(1).upper() if match else ""
    right = (not gave_up) and given == correct

    session['asked'] = session.get('asked', 0) + 1
    if right:
        session['correct'] = session.get('correct', 0) + 1
    session.setdefault('history', []).append({
        'question': pending['prompt'].split('\n')[1] if '\n' in pending['prompt']
        else pending['prompt'],
        'answer': correct,
        'given': given or '(no answer)',
        'correct': right,
        'at': _now(),
    })
    session['pending'] = None

    answer_text = pending['options'].get(correct, '')
    if right:
        head = f"Correct! {answer_text}."
    elif gave_up:
        head = f"No worries — it was {correct}, {answer_text}."
    else:
        head = f"Not quite — it was {correct}, {answer_text}."
    if pending.get('fact'):
        head += f" {pending['fact']}"
    head += f" {_score_line(session)}"

    question, note = _next_question(session, ask_bank, ask_generate, data)
    if question is None:
        store.save(data)
        return head + " I couldn't pull the next question — say \"quiz me\" to keep going."
    session['pending'] = question
    store.save(data)
    return f"{head}\n\n{note}{_render(question)}"


def _stop(data, session, store):
    if session is None:
        return ("There's no quiz running right now. Say \"quiz me\" whenever "
                "you want to start one.")
    asked = session.get('asked', 0)
    correct = session.get('correct', 0)
    _close(data, session)
    store.save(data)
    if not asked:
        return "Quiz stopped — no questions were answered in that round."
    return (f"Quiz stopped. Final score: {correct} out of {asked} "
            f"correct. Say \"quiz me\" to start another.")


def _report_score(data, session):
    if session and session.get('asked'):
        return (f"In this round you're at {session['correct']} out of "
                f"{session['asked']} correct.")
    if session:
        return ("The quiz just started — you haven't answered a question yet, "
                "so there's no score in this round.")
    results = data.get('results') or []
    if not results:
        return ("There's no quiz running and no finished games on record yet, "
                "so there's no score to report. Say \"quiz me\" to start one.")
    last = results[-1]
    return (f"No quiz is running. Your last finished round was "
            f"{last.get('score', 0)} out of {last.get('total', 0)} correct.")


def _report_history(data):
    results = data.get('results') or []
    session = _active(data)
    if not results and not (session and session.get('asked')):
        return ("No finished trivia rounds are on record yet. Play one and "
                "I'll keep the score here.")
    lines = []
    if session and session.get('asked'):
        lines.append(f"In progress: {session['correct']} of {session['asked']} "
                     f"correct this round.")
    total_q = sum(r.get('total', 0) for r in results)
    total_c = sum(r.get('score', 0) for r in results)
    if results:
        lines.append(f"{len(results)} finished round"
                     f"{'s' if len(results) != 1 else ''} on record, "
                     f"{total_c} of {total_q} correct overall.")
        for entry in results[-3:]:
            lines.append(f"  - {entry.get('category', 'mixed')}: "
                         f"{entry.get('score', 0)}/{entry.get('total', 0)} "
                         f"({entry.get('difficulty', 'medium')})")
    return "\n".join(lines)


def _change_difficulty(data, session, direction, ask_bank, ask_generate, store):
    """Honour "harder"/"easier"/"hard"/"easy" -- and never claim the tool
    doesn't exist. The chosen level sticks to the player, not to an age."""
    if session is None:
        session = _new_session(data, difficulty=direction, level=direction)
        lead = f"Starting a {direction}er round.\n\n" if direction == 'hard' \
            else "Starting an easier round.\n\n"
    else:
        session['difficulty'] = direction
        session['level'] = normalize_level(direction) or DEFAULT_LEVEL
        session['pending'] = None
        lead = ("Turning the difficulty up.\n\n" if direction == 'hard'
                else "Easing off.\n\n")
    _player(data, session.get('player') or 'you')['level'] = (
        normalize_level(direction) or DEFAULT_LEVEL)
    question, note = _next_question(session, ask_bank, ask_generate, data)
    if question is None:
        store.save(data)
        return ("I do have a trivia generator, but I couldn't reach it just "
                "now, so I can't change the difficulty this minute. The "
                "built-in question bank has no difficulty levels of its own.")
    session['pending'] = question
    store.save(data)
    return f"{lead}{note}{_render(question)}"
