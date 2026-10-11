"""A capability READ must be registry-driven for any phrasing.

When a capability phrasing misses the detector it goes to RAG, and the model
invents a capability list:

    "list your skills" -> Route: rag
      -> "programming languages (Python, JavaScript, C#), web frameworks
          (React, ASP.NET, Django)... cloud platforms (AWS, Azure)... Docker,
          Kubernetes... IDEs (VS Code...)"

    "give me a rundown of everything you can help with" -> Route: rag
      -> "troubleshooting household appliances, fixing simple plumbing or
          electrical issues... gardening tips... first-aid guidance"

    "what functions do you support" -> Route: rag
      -> real bits blended with emergency medicine, mechanics, electrical
         wiring

while "what are you capable of" / "what can I ask you to do around here" /
"walk me through what youre able to do for this house" reach the registry.

The registry read itself is fine; a phrasing-bound DETECTOR turns every miss
into an invention. This file pins a wide phrasing set onto the fastpath, and
pins the decline corpus that must stay on RAG.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import capability_registry as capreg  # noqa: E402

#: The three phrasings that produce a fabrication when they miss the fastpath.
FABRICATED_PHRASINGS = [
    "list your skills",
    "give me a rundown of everything you can help with",
    "what functions do you support",
]

#: Three phrasings the detector has always claimed; they must keep working.
ORIGINAL_PHRASINGS = [
    "what are you capable of",
    "what can I ask you to do around here",
    "walk me through what youre able to do for this house",
]

#: The six core capability phrasings.
CORE_PHRASINGS = [
    "what can you do",
    "what can you access",
    "what are your capabilities",
    "what all can you do",
    "what do you have access to",
    "list your capabilities",
]

#: Fresh phrasings a first-time user would try.
FRESH_PHRASINGS = [
    "what are your skills",
    "what skills do you have",
    "show me your abilities",
    "what are your abilities",
    "what features do you have",
    "list your features",
    "what commands can you support",
    "tell me your functions",
    "what do you know how to do",
    "what are you good at",
    "what else can you do",
    "what can't you do",
    "show me what you can do",
    "how can you help",
    "what can you help me with",
    "what kind of things can you do",
    "what do you do around here",
    "help",
]

#: The invented answer to "list your skills". Nothing on this path
#: may produce any of it.
RESUME_LEAK = ("python", "react", "docker", "kubernetes", "aws", "azure",
               "django", "vs code")

#: The other two fabrications.
HANDYMAN_LEAK = ("plumbing", "gardening", "first-aid", "electrical wiring",
                 "emergency medicine")


def _capability_answer(phrase):
    return api._try_capabilities(phrase)


@pytest.mark.parametrize("phrase", FABRICATED_PHRASINGS + ORIGINAL_PHRASINGS
                         + CORE_PHRASINGS + FRESH_PHRASINGS)
def test_phrasing_reaches_the_registry_reader(phrase):
    reply = _capability_answer(phrase)
    assert reply, f"{phrase!r} fell through to RAG"


@pytest.mark.parametrize("phrase", FABRICATED_PHRASINGS)
def test_the_fabricated_phrasings_now_answer_from_the_registry(phrase):
    reply = _capability_answer(phrase).lower()
    for leak in RESUME_LEAK + HANDYMAN_LEAK:
        assert leak not in reply, f"{phrase!r} leaked {leak!r}"
    # A registry answer names real areas of this house.
    assert "i'm one system" in reply or "i can help with" in reply


def test_instant_answer_chain_claims_list_your_skills():
    """End of the wiring, not just the regex: the chain must return it."""
    assert api._try_instant_answer("list your skills")


# ── the decline corpus: somebody else's skills stay on RAG ──────────────────

DECLINE = [
    "list Python skills for a resume",
    "list python skills for a résumé",
    "what skills does a nurse need",
    "what skills do I need to be a plumber",
    "what skills should I put on my CV",
    "what skills does a teacher need for a job interview",
    "what do you know about the Great Wall of China",
    "how far is the moon from earth",
]


@pytest.mark.parametrize("phrase", DECLINE)
def test_declines_stay_on_the_normal_route(phrase):
    assert _capability_answer(phrase) is None, f"{phrase!r} was hijacked"


def test_help_me_is_not_a_feature_list_request():
    """A person saying "help me" may be in trouble. Never answer with a menu."""
    assert _capability_answer("help me") is None


# ── the shut-door marker with a stale legacy key in the config ─────────────
#
# A config can carry BOTH the new switch and a stale legacy one:
# egress_doors.web_search=false AND a top-level web_search_consent=true.
# The precedence chain in egress_doors._resolve_flag puts the egress_doors
# section ahead of legacy_config_key, so the door is correctly shut — this
# pins that, because the day the order flips the overview starts selling a
# capability the owner switched off.

def test_overview_marks_web_tools_off_when_both_doors_are_shut(monkeypatch):
    import puffpai.security.egress_doors as ed
    monkeypatch.setattr(ed, "egress_allowed",
                        lambda door_id, *a, **k:
                        (door_id not in ("web_search", "web_fetch"), ""))
    overview = capreg.describe(None, for_voice=True)
    assert "web tools" in overview
    assert capreg.EGRESS_OFF_NOTE.strip() in overview


def test_overview_drops_the_marker_when_a_web_door_is_open(monkeypatch):
    import puffpai.security.egress_doors as ed
    # The chat opt-in is the second half of the same AND.
    monkeypatch.setattr(capreg, "_chat_egress_on", lambda: True)
    monkeypatch.setattr(ed, "egress_allowed", lambda door_id, *a, **k: (True, ""))
    overview = capreg.describe(None, for_voice=True)
    assert "web tools" in overview
    assert "web tools" + capreg.EGRESS_OFF_NOTE not in overview


def test_stale_legacy_consent_key_cannot_reopen_a_shut_web_door(monkeypatch):
    """egress_doors.web_search=false beats web_search_consent=true."""
    import puffpai.security.egress_doors as ed
    monkeypatch.delenv("PUFFPAI_EGRESS_WEB_SEARCH", raising=False)
    monkeypatch.setattr(ed, "_raw_overlay", lambda: {})
    monkeypatch.setattr(ed, "_raw_config", lambda: {
        "web_search_consent": True,
        ed.CONFIG_SECTION: {"web_search": False},
    })
    allowed, _reason = ed.egress_allowed("web_search")
    assert allowed is False
