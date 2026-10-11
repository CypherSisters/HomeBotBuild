"""On a scrubbed turn the honest notice IS the reply.

the cued bare-digit SSN was detected,
redacted, and the honest first sentence was produced — and then the REDACTED
prompt was handed to the model anyway, which appended "I'm sorry, but I can't
process that." to it. The model was asked to answer a sentence with a hole in
it; there was never an answer for it to give.
"""

import inspect

import pytest

from puffpai import api_server


def _meta(types, status="VAULT_LOCKED", vaulted=None):
    return {"detected": True, "types": list(types), "vaulted": list(vaulted or []),
            "status": status}


# ── the notice is produced, and it is the WHOLE reply ────────────────────

@pytest.mark.parametrize("types", [["ssn"], ["credit_card"],
                                   ["ssn", "credit_card"]])
def test_a_pure_disclosure_is_answered_by_the_notice(types):
    meta = _meta(types)
    reply = api_server._pii_deterministic_reply(meta)
    assert reply == api_server._pii_honesty_notice(meta)
    assert "I'm sorry" not in reply
    assert "can't process" not in reply


def test_the_dashed_and_bare_digit_forms_take_the_same_path():
    """Both produce the same meta, so both must short-circuit identically."""
    dashed = _meta(["ssn"])
    bare = _meta(["ssn"])
    assert (api_server._pii_deterministic_reply(dashed)
            == api_server._pii_deterministic_reply(bare)
            == api_server._pii_honesty_notice(dashed))


def test_the_notice_names_the_social_security_number():
    reply = api_server._pii_deterministic_reply(_meta(["ssn"]))
    assert "Social Security number" in reply
    assert "did not keep it" in reply


# ── what must NOT be short-circuited ─────────────────────────────────────

@pytest.mark.parametrize("types", [
    ["phone"],                 # "remind me to call 555-0134"
    ["email"],                 # "text alex@example.com the list"
    ["address"],
    ["name"],
    ["ssn", "phone"],          # a command that also carries an SSN
    ["health.condition"],
])
def test_a_class_that_appears_inside_commands_is_not_short_circuited(types):
    assert api_server._pii_deterministic_reply(_meta(types)) is None


def test_no_detection_means_no_short_circuit():
    assert api_server._pii_deterministic_reply({"detected": False}) is None
    assert api_server._pii_deterministic_reply(None) is None


def test_a_vaulted_disclosure_is_not_short_circuited():
    """Vaulted PII has no "did not keep it" notice, so there is nothing to say."""
    meta = _meta(["ssn"], vaulted=["ssn"])
    assert api_server._pii_honesty_notice(meta) is None
    assert api_server._pii_deterministic_reply(meta) is None


def test_meta_without_types_keeps_the_old_generic_path():
    """A caller that does not populate `types` must not be short-circuited."""
    meta = {"detected": True, "vaulted": [], "status": "VAULT_LOCKED"}
    assert api_server._pii_honesty_notice(meta) is not None
    assert api_server._pii_deterministic_reply(meta) is None


# ── the honesty pass stays idempotent ────────────────────────────────────

def test_applying_the_honesty_pass_to_the_notice_does_not_double_it():
    meta = _meta(["ssn"])
    notice = api_server._pii_deterministic_reply(meta)
    assert api_server._apply_pii_honesty(notice, meta) == notice


# ── the clause splitter itself ─────────────────────────
#
# The pattern read `r"[,;.!?]+|\band\b|\bthen\b"` in intent and held literal
# BACKSPACE bytes (0x08) where the word boundaries belonged, so the only thing
# that ever split a turn was punctuation. "my ssn is <ssn> and add milk to the
# list" was therefore read as ONE disclosure clause: the privacy notice became
# the whole reply and the instruction was thrown away without a word.


def test_the_clause_splitter_holds_no_control_characters():
    pattern = api_server._PII_CLAUSE_SPLIT_RE.pattern
    offenders = [(i, ch) for i, ch in enumerate(pattern)
                 if ord(ch) < 32 or ord(ch) == 127]
    assert offenders == [], (
        "a control character in the clause splitter is a typo, not a rule; "
        "found at %r" % (offenders,))
    assert r"\band\b" in pattern and r"\bthen\b" in pattern


@pytest.mark.parametrize("text", [
    "my ssn is 123-45-6789 and add milk to the list",
    "my ssn is 123-45-6789 then add milk to the list",
    "my ssn is 123-45-6789, add milk to the list",
])
def test_a_disclosure_carrying_an_instruction_is_still_an_instruction(text):
    clauses = api_server._PII_CLAUSE_SPLIT_RE.split(text)
    assert len(clauses) >= 2
    assert any(c.strip().startswith("add milk") for c in clauses)
    assert api_server._pii_turn_is_a_command(text) is True
    #...so the notice is NOT the whole answer: the command keeps its own.
    assert api_server._pii_deterministic_reply(_meta(["ssn"]), text) is None


@pytest.mark.parametrize("text", [
    "my ssn is 123-45-6789",
    "my social security number is 123456789 and my wife's is different",
])
def test_a_pure_disclosure_is_still_answered_by_the_notice_alone(text):
    meta = _meta(["ssn"])
    assert api_server._pii_turn_is_a_command(text) is False
    assert (api_server._pii_deterministic_reply(meta, text)
            == api_server._pii_honesty_notice(meta))


def test_the_scrub_path_itself_is_unchanged_by_the_split_fix():
    """The splitter decides WHO answers, never what is redacted."""
    meta = _meta(["ssn"])
    notice = api_server._pii_honesty_notice(meta)
    assert notice and "123-45-6789" not in notice
    assert api_server._apply_pii_honesty(
        "Added milk to the shopping list.", meta).endswith(
            "Added milk to the shopping list.")


# ── wiring: no model call on either chat surface ─────────────────────────

def test_chat_send_skips_the_pipeline_on_a_pure_disclosure():
    # The body of /chat/send lives in _chat_send_turn (shared with the Send to
    # Puff voice memo route).
    src = inspect.getsource(api_server._chat_send_turn)
    assert "_pii_deterministic_reply" in src
    head = src.split("_pii_deterministic_reply", 1)[1]
    assert "_process_chat_query" in head
    # The pipeline call must be in the ELSE arm, i.e. after the notice branch.
    notice_at = src.index("_pii_deterministic_reply")
    pipeline_at = src.index("_process_chat_query")
    assert notice_at < pipeline_at, (
        "the deterministic notice must be decided BEFORE the pipeline is "
        "called, or the redacted prompt still reaches the model")


def test_the_streaming_surface_short_circuits_too():
    src = inspect.getsource(api_server.chat_stream)
    assert "_pii_deterministic_reply" in src
    assert src.index("_pii_deterministic_reply") < src.index(
        "_try_med_fastpath"), (
        "a pure disclosure must short-circuit before any write fastpath runs")
