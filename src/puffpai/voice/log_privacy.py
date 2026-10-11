"""What the VOICE unit is allowed to write down about what it heard and said.

The voice service is a separate process with its own durable journal (user
journald + the rotating ``voice.log``), and it logged every confirmed
utterance and every reply in plaintext. That is where a spoken PIN and a
freshly decrypted vault secret landed, in full, on disk -- the one surface the
backend's own redaction (``api_server._log_safe``) never covered, because the
backend cannot reach into another process's log line.

It also cannot WAIT for the backend to tell it: the utterance is logged before
the brain is even called. So the two sides are decided differently.

UTTERANCES (``ADDRESSED``, ``COMMAND``, every bounded ``heard=`` excerpt) stay
READABLE. That is not a concession, it is the point of those lines: a wake or
a stop that went wrong is diagnosed from one grep of ``heard=``, and "unlock
the front door" or "what's my wifi password" carries no secret -- the ANSWER
does. An utterance is withheld only when it is PIN-SHAPED, or when a vault
challenge is armed and the next thing said into the room may BE the PIN.

REPLIES (``PUFF:``, the leak-guard warning, the TTS-empty diagnostic) follow
the backend's ``private`` flag, and are additionally filtered on their own
content -- shape AND secret vocabulary -- because a reply is where a decrypted
secret actually appears, and a backend that says "ordinary" may simply be an
older one.

Why this file exists instead of importing api_server: the voice unit is a user
unit with no Flask, no model, no vault. Importing api_server to ask one pure
question would pull the whole application (and its import-time side effects)
into the mic loop on a 50 W appliance. The digit-shape rule is therefore
duplicated from ``api_server._log_text_has_pin_shape`` /
``_live_turn_digit_run``, and ``tests/test_voice_journal_privacy.py`` pins the
copies together: anything the backend withholds, this withholds too, with one
named exception (a clock time -- see ``_TIME_TOKEN_RE``).

Every entry point fails CLOSED: an exception deciding privacy withholds.
"""

import re

#: What a withheld line says. Constants only -- nothing derived from the text,
#: no length, no hash: a 4-digit PIN is short enough that a hash of it IS the
#: PIN, and its length is a real hint.
WITHHELD_DIGITS = "[withheld: contains digits]"
WITHHELD_SECRET = "[withheld: private turn]"
WITHHELD_REPLY = "[private turn]"

# -- the digit-shape rule, mirrored from api_server (see module docstring) ----
#: Spoken digits. "four two eight one" is a PIN said out loud.
_SPOKEN_DIGITS = frozenset((
    'zero', 'oh', 'o', 'nought', 'one', 'two', 'three', 'four', 'five', 'six',
    'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve'))
#: Words that carry no meaning of their own in "my pin is 4 2 8 1".
_PIN_FILLER = frozenset((
    'my', 'the', 'it', 'is', 'its', "it's", 'pin', 'code', 'passcode',
    'number', 'please', 'double', 'triple', 'dash', 'point', 'star', 'hash',
    'pound', 'plus'))
#: Spoken numbers worth two digits, and the "double four" multipliers.
_TEEN_WORDS = frozenset((
    'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
    'seventeen', 'eighteen', 'nineteen'))
_TENS_WORDS = frozenset((
    'twenty', 'thirty', 'forty', 'fourty', 'fifty', 'sixty', 'seventy',
    'eighty', 'ninety'))
_DIGIT_MULTIPLIERS = {'double': 2, 'triple': 3}
#: Words, not tokens: splitting on everything that is not a letter or a digit
#: makes "password | 4821" and "password|4821" the same sentence, which they
#: are (the unlock tool's own input format uses the bar).
_WORD_RE = re.compile(r"[a-z0-9]+")

#: What the recognizer writes when a PIN is dictated at speed: "for ate to
#: won" is 4 8 2 1. Split by how much each word proves. "ate", "won", "nein",
#: "tree" are odd enough inside a row of numbers to be the real thing; "for",
#: "to", "too" and "free" are among the commonest words in the language and
#: prove nothing on their own.
_HOMOPHONE_SURE = frozenset(('ate', 'won', 'nein', 'tree', 'fore', 'sicks'))
_HOMOPHONE_SOFT = frozenset(('for', 'to', 'too', 'free'))
#: Spoken digits that are also ordinary interjections: they count toward a run
#: but never make one credible by themselves.
_SOFT_DIGIT_WORDS = frozenset(('oh', 'o'))

#: A clock time is not a PIN, and it is most of what a household says with
#: digits in it ("set it for 7:30", "the alarm is at 10:45"). The one place
#: this side is deliberately LAXER than the backend rule, because the backend
#: is protecting a save-back and a wall panel while this is protecting a debug
#: journal that has to stay useful for alarms and timers.
#:
#: A real clock, not "anything with a colon in it": 24-hour hours and 0-59
#: minutes, so "9:99" is not a time and "56:78" is not two of them.
_TIME_TOKEN_RE = re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")
#: Words that make a number beside them a secret rather than a clock.
#: "code" is ordinary English on its own -- "the area code", "zip code" --
#: so it is consulted ONLY here, as a reason to refuse the exemption, never
#: as a reason to withhold by itself.
_TIME_BLOCK_WORDS = frozenset(('code', 'codes'))
#: Number words that must not appear beside an exempted clock: a PIN hidden
#: next to a time ("4 8 2 1 at 7:30") must not ride out on the exemption.
#: The soft homophones ("for", "to") are deliberately absent -- they are in
#: half the sentences that mention a time ("set an alarm for 6:15").
_TIME_BLOCK_NUMBERS = (_SPOKEN_DIGITS | _TEEN_WORDS | _TENS_WORDS
                       | _HOMOPHONE_SURE)

#: Anything that NAMES a secret, or the act of getting at one. Used for
#: REPLIES and for the old-backend fallback ONLY -- never to withhold an
#: utterance, which is why "unlock the front door" still reaches the journal.
_SECRET_WORDS = frozenset((
    'vault', 'unlock', 'unlocks', 'unlocking', 'unlocked',
    'pin', 'pins', 'pincode', 'pincodes', 'passcode', 'passcodes',
    'passphrase', 'passphrases', 'password', 'passwords',
    'login', 'logins', 'secret', 'secrets', 'combination', 'combinations',
    'credential', 'credentials'))
#: The same vocabulary as the recognizer actually writes it: "pass word",
#: "pass-code", "log in". Tested against the text with every character that is
#: not a letter or a digit REMOVED, so spacing and hyphenation variants
#: collapse onto one form. Substring matching, so it is blunt: "catalog in the
#: drawer" squashes to ...catalogin... and a REPLY saying that is withheld. A
#: lost journal line is the cheap side of that trade. "pin" is deliberately
#: absent -- it would eat "spin", "shopping", "opinion" -- and is matched as a
#: whole word instead.
_SQUASHED_SECRET_WORDS = (
    'password', 'passcode', 'passphrase', 'pincode', 'login', 'signin',
    'vault', 'unlock', 'credential', 'combination', 'secret')
_SQUASH_RE = re.compile(r"[^a-z0-9]+")
#: "P.I.N.", "p i n", "p-i-n" -- dictated one letter at a time, which is how
#: it is said out loud. Word-bounded, so "spin" and "pinch" are safe.
_DOTTED_PIN_RE = re.compile(r"\bp[\s.\-]*i[\s.\-]*n\b")

#: A stable fragment of every vault PIN challenge the backend speaks ("To
#: access it, please reply with your PIN.", "The vault stays locked until you
#: enter your PIN", "What's your PIN and ..."). Only consulted when the
#: response carries no ``vault_challenge_pending`` field -- i.e. against an
#: older backend. Deliberately loose: arming the guard for a turn that did not
#: really ask for a PIN costs a few readable log lines.
_CHALLENGE_PHRASE = "your pin"


def _clock_exempt(raw):
    """May this text's clock time be hidden from the shape rules?

    Only when the time is the ONLY number in the sentence and nothing in
    the sentence claims it is a secret. "It's 7:30" is a clock; "pin
    12:34", "code 10:45", "my code is 12:34 and 56:78" and "12:34 12:35"
    are a PIN wearing a colon, and the exemption is refused for all of
    them -- after which the ordinary digit rules see the digits and
    withhold. Refusing costs one readable log line; granting it wrongly
    writes a PIN to a durable journal.
    """
    times = _TIME_TOKEN_RE.findall(raw)
    if len(times) != 1:
        return False                      # none to exempt, or too many
    rest = _TIME_TOKEN_RE.sub(" ", raw)
    if any(ch.isdigit() for ch in rest):
        return False                      # another number is in here
    low = rest.lower()
    words = set(_WORD_RE.findall(low))
    if words & _TIME_BLOCK_NUMBERS or words & _TIME_BLOCK_WORDS:
        return False
    return not has_secret_vocabulary(raw)


def digit_run(text, require_spoken=False):
    """Is something PIN-shaped embedded INSIDE this text?

    Mirrors ``api_server._live_turn_digit_run``: a numeric token of three or
    more digits, or three digits' worth of spoken number in a row with only
    filler between them (tens and teens count double, "double four" /
    "triple nine" honoured). ``require_spoken`` demands a DICTATED run of
    three or more tokens, so a lone year does not count.
    """
    tokens = _WORD_RE.findall((text or '').lower())
    run = 0
    parts = 0
    mult = 1
    for tok in tokens:
        if tok in _DIGIT_MULTIPLIERS:
            mult = _DIGIT_MULTIPLIERS[tok]
            continue
        if tok.isdigit():
            if len(tok) >= 3 and not require_spoken:
                return True
            run += len(tok) * mult
            parts += 1
        elif tok in _TENS_WORDS or tok in _TEEN_WORDS:
            run += 2 * mult
            parts += 1
        elif tok in _SPOKEN_DIGITS:
            run += mult
            parts += 1
        elif tok in _PIN_FILLER:
            mult = 1
            continue                      # "my pin is four eight two one"
        else:
            run = 0
            parts = 0
            mult = 1
            continue
        mult = 1
        if run >= 3 and (parts >= 3 or not require_spoken):
            return True
    return False


def _homophone_digit_run(text):
    """A dictated PIN the recognizer spelled as ordinary words.

    Three or more CONSECUTIVE number-ish tokens -- no filler bridging, because
    "who won the game for us" is a sentence and "for ate to won" is a PIN --
    worth three digits between them, and at least one member that is not a
    word people say all day ("ate", "won", a real digit).
    """
    run = parts = 0
    solid = False
    for tok in _WORD_RE.findall((text or '').lower()):
        if tok.isdigit():
            worth, sure = len(tok), True
        elif tok in _TENS_WORDS or tok in _TEEN_WORDS:
            worth, sure = 2, True
        elif tok in _SPOKEN_DIGITS:
            worth, sure = 1, tok not in _SOFT_DIGIT_WORDS
        elif tok in _HOMOPHONE_SURE:
            worth, sure = 1, True
        elif tok in _HOMOPHONE_SOFT:
            worth, sure = 1, False
        else:
            run = parts = 0
            solid = False
            continue
        run += worth
        parts += 1
        solid = solid or sure
        if parts >= 3 and run >= 3 and solid:
            return True
    return False


def _digits_inside_a_token(text):
    """Three digits in ONE token, however many letters are shot through it.

    "4a8b2c1" is a PIN with noise in it, and the character rule misses it:
    every letter resets both counters. Blunt on purpose -- "401k" and
    "mp3s2024" are withheld too, and that costs nothing.
    """
    return any(sum(ch.isdigit() for ch in tok) >= 3
               for tok in _WORD_RE.findall((text or '').lower()))


def has_pin_shape(text):
    """Could a PIN be hiding anywhere in this text?

    The backend's three tests (``api_server._log_text_has_pin_shape``), on the
    FULL text before any truncation:

      * a compact run of three or more digits, ANY Unicode digits
        (``str.isdigit`` covers Arabic-Indic and fullwidth forms that an ASCII
        digit class lets through);
      * separated single digits totalling three since the last LETTER, so
        "4 8 2 1" counts and "a timer for 10 minutes" does not;
      * a dictated run worth three digits ("forty eight twenty one").

    Plus two the recognizer makes necessary and a typed surface never
    produces: three digit characters inside one token ("4a8b2c1"), and a
    homophone run ("for ate to won").

    Minus exactly one thing: a lone, unqualified clock time is hidden from
    all of the above, so "It's 10:45" stays readable (see
    ``_clock_exempt`` for how narrow that is). It is the only way this side
    is laxer than the backend's, and it is named in the parity test.
    """
    raw = text if isinstance(text, str) else str(text or "")
    if _clock_exempt(raw):
        raw = _TIME_TOKEN_RE.sub(" ", raw)
    run = 0        # digits in the current unbroken group
    tally = 0      # digits seen since the last letter
    for ch in raw:
        if ch.isdigit():
            run += 1
            tally += 1
            if run >= 3 or tally >= 3:
                return True
        elif ch.isalpha():
            run = 0
            tally = 0
        else:
            run = 0            # a separator ends the group, not the tally
    if _digits_inside_a_token(raw):
        return True
    return bool(digit_run(raw) or _homophone_digit_run(raw))


def has_secret_vocabulary(text):
    """Does this text name a secret, or the act of getting at one?

    Three passes, because one transcript of one sentence can spell the same
    word three ways: whole words ("password"), the squashed form ("pass
    word", "pass-code", "log in"), and the letter-by-letter PIN ("P.I.N.").

    REPLIES and the old-backend fallback only. An utterance is never withheld
    for vocabulary: see the module docstring.
    """
    raw = (text if isinstance(text, str) else str(text or "")).lower()
    if set(_WORD_RE.findall(raw)) & _SECRET_WORDS:
        return True
    if _DOTTED_PIN_RE.search(raw):
        return True
    squashed = _SQUASH_RE.sub("", raw)
    return any(word in squashed for word in _SQUASHED_SECRET_WORDS)


def utterance_withholds(text):
    """Must this UTTERANCE be kept out of the journal? Shape only. Fails CLOSED."""
    try:
        if not (text or "").strip():
            return False           # nothing to withhold, and "" is not a leak
        return has_pin_shape(text)
    except Exception:
        return True


def utterance_withholds_any(*texts):
    """One verdict for several strings that share a log line. Fails CLOSED."""
    try:
        return any(utterance_withholds(t) for t in texts)
    except Exception:
        return True


def withholds(text):
    """Must this REPLY (or old-backend turn) be kept out? Fails CLOSED."""
    try:
        if not (text or "").strip():
            return False
        return has_pin_shape(text) or has_secret_vocabulary(text)
    except Exception:
        return True


def withholds_any(*texts):
    """One reply-side verdict for several strings. Fails CLOSED."""
    try:
        return any(withholds(t) for t in texts)
    except Exception:
        return True


def _placeholder(text):
    """Which constant a withheld string is replaced by."""
    try:
        return WITHHELD_DIGITS if has_pin_shape(text) else WITHHELD_SECRET
    except Exception:
        return WITHHELD_SECRET


def safe_heard(text, limit=None, hide=None):
    """A log-safe rendering of something the mic heard.

    Whitespace-collapsed (one event stays one line), truncated to ``limit``
    when given, and replaced wholesale by a constant when the text is
    PIN-shaped. ``hide`` forces the verdict, which is how the caller folds in
    the two things this function cannot see: an armed vault challenge, and a
    sibling string on the same log line (the command carved out of the
    transcript). Fails CLOSED.
    """
    try:
        flat = " ".join((text or "").split())
        if hide if hide is not None else utterance_withholds(text):
            return _placeholder(text)
        return flat[:limit] if limit else flat
    except Exception:
        return WITHHELD_SECRET


def safe_reply(text, limit=None, private=None):
    """A log-safe rendering of something she said back.

    ``private`` is the backend's verdict for the whole turn (``/api/chat``'s
    ``private`` field, carried on the service's ``LAST_REPLY_PRIVATE``): a
    private turn logs a constant and no slice of the reply, ever. A turn the
    backend calls ordinary is still filtered on its own content -- shape and
    vocabulary -- because a reply is where a secret actually appears.
    """
    try:
        if private:
            return WITHHELD_REPLY
        flat = " ".join((text or "").split())
        if withholds(text):
            return _placeholder(text)
        return flat[:limit] if limit else flat
    except Exception:
        return WITHHELD_REPLY


def reply_is_private(data, said, reply):
    """Was this turn private? The backend's word first, the text second.

    ``data`` is the parsed ``/api/chat`` body. A backend that sends a BOOLEAN
    ``private`` is authoritative -- it knows things the text cannot show (a
    vault tool ran, a challenge was pending, the wall turn was sealed). A
    backend that does NOT send the field is an OLD backend, and an old backend
    is not a reason to write a secret down: the verdict then comes from the
    text on both sides, vocabulary included. Anything else in the field is a
    backend we cannot read, which is a private turn. Fails CLOSED.
    """
    try:
        if isinstance(data, dict) and "private" in data:
            value = data["private"]
            # Only a real boolean is an answer. ``0``, ``[]``, ``""`` and
            # ``null`` are a backend we do not understand, and a backend we do
            # not understand does not get to say "log it".
            return value if isinstance(value, bool) else True
        return withholds_any(said, reply)
    except Exception:
        return True


def challenge_armed(data, reply):
    """Is the household about to be asked to SAY a PIN?

    The one case where an ordinary-looking utterance must be withheld: the
    next thing said into the room may be the PIN itself, and the utterance is
    logged long before any answer could say so. ``/api/chat`` reports it
    directly; an older backend is read from the challenge sentence it just
    spoke. Fails CLOSED.
    """
    try:
        if isinstance(data, dict) and "vault_challenge_pending" in data:
            value = data["vault_challenge_pending"]
            return value if isinstance(value, bool) else True
        return _CHALLENGE_PHRASE in (reply or "").lower()
    except Exception:
        return True
