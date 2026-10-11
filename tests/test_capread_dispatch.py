"""Registry-driven READ dispatch — the gate, and the things it must REFUSE.

The chat READ fastpath answers a settled
question by invoking a tool directly, before the router, the team and the
tool-calling brain. It answers and stops, so a wrong dispatch has no recovery
path — the person just gets a confident, fluent, wrong answer.

A first attempt drove that off the registry's ``keywords`` and shipped green:
77 suite failures before, 77 after, identical names. It was still broken, and
the reason it passed is the reason this file leads with the decline corpus —
its tests asserted that specific writes were blocked and specific reads passed,
and never that an ORDINARY SENTENCE DECLINES.

A gate is only as good as the corpus of things it must refuse. That corpus is
:data:`DECLINE_CORPUS`, and it is the first test in the file on purpose.

Run: pytest tests/test_capread_dispatch.py -q
"""

from __future__ import annotations

import pytest

from puffpai import capability_registry as reg


# ── the corpus that must NEVER dispatch ─────────────────────────────────────
#: Ordinary English. Every one of these contains a word the registry declares
#: as a keyword for some dispatchable capability -- "secret", "locked",
#: "network", "documents", "alarm", "timer", "grant", "consent", "scan",
#: "privacy", "perimeter", "station", "radio", "detected", "blocked" -- and in
#: none of them does the person want a household read.
#:
#: The first 25 are the sentences the adversarial pass actually caught attempt 1
#: dispatching on. They are kept verbatim as the regression floor.
DECLINE_CORPUS = [
    # --- caught dispatching in attempt 1 (verbatim) ---
    "is the front door locked",
    "why is my account locked",
    "tell me a secret",
    "what is the secret to good bread",
    "what is a private key",
    "where is my pin number for the bank",
    "get my coat",
    "the road is blocked",
    "how does a firewall work",
    "what is a social network",
    "what is privacy in europe",
    "scan this barcode",
    "what is the perimeter of a square",
    "what does consent mean",
    "did grant write that",
    "where is the nearest gas station",
    "who is npr's ceo",
    "how do i notarize documents",
    "my file is corrupted",
    "who was the doorbell inventor",
    "do i have any notifications",
    "what timer should i use for eggs",
    "the alarm clock is broken",
    "how much disk space is left",
    "what is my morning report",
    # --- further ordinary English over the same keyword surface ---
    "what time is sunset",
    "tell me about the french revolution",
    "who invented the radio",
    "what is a railway station",
    "explain how a camera lens works",
    "what does the word vault mean in gymnastics",
    "the pole vault record",
    "how do i block a rude caller on my phone",
    "what is network theory in mathematics",
    "write me a poem about a locked garden",
    "what are the panama papers documents",
    "how long should i set the timer for pasta",
    "is consent required for a photograph",
    "what is a permission slip",
    "who granted the charter",
    "what does detected mean in medicine",
    "how do smoke alarms work",
    "what is a credential in academia",
    "explain public key cryptography",
    "why did the movement fail",
    # --- attempt 2: sentences that CONTAIN a declared dispatch phrase but are
    # --- not that request. Containment matching answered every one of these;
    # --- whole-utterance matching is what refuses them. Kept verbatim.
    "is the vault locked in the heist movie",
    "what is the pole vault status of the olympic record",
    "the vault status page for hashicorp vault",
    "what does vault status output in hashicorp",
    "what is the vault status of the disney vault",
    "the vault status indicator on the safe is red",
    "her vault status is unknown",
    "is the vault locked question for trivia night",
    "what is my sister's network status",
    "can you check bob's network status",
    "the tv network status of nbc",
    "network status codes in http",
    "the network status bar disappeared from my laptop",
    "why does my printer say network status offline",
    "what is the network status light on a router",
    "how do i read the network status on an android phone",
    "explain the network status of the electrical grid",
    "who is on my network drive at work",
    "my network status is none of your business",
    "pretend you don't know the network status",
    "how do i say 'network status' in spanish",
    "what does network status mean",
    # --- verbs of TRANSMISSION and repair: an action, not a question ---
    "print the vault status",
    "upload the vault status to dropbox",
    "send the network status to my landlord",
    "share the network status with the hoa",
    "tweet the network status",
    "forward the network status to support",
    "copy the network status to the clipboard",
    "announce the network status on the speaker",
    "text me the network status",
    "email me the network status",
    "reset the network status",
    "fix the network status",
    "ignore the network status",
    "remind me to check the network status",
    "add network status to my dashboard",
    "buy a new router because of the network status",
    "stop asking me about the network status",
]


def test_decline_corpus_never_dispatches():
    """The whole point of the file. Ordinary English gets no tool."""
    offenders = []
    for sentence in DECLINE_CORPUS:
        cap = reg.find_for_dispatch(sentence)
        if cap is not None:
            offenders.append((sentence, cap.id))
    assert not offenders, "these ordinary sentences dispatched:\n" + "\n".join(
        f"  {s!r} -> {c}" for s, c in offenders)


def test_decline_corpus_is_big_enough_to_be_evidence():
    """A three-sentence corpus proves nothing; the spec sets the floor at 40."""
    assert len(DECLINE_CORPUS) >= 40
    assert len(set(DECLINE_CORPUS)) == len(DECLINE_CORPUS)


# ── the reads this layer exists to serve ────────────────────────────────────
#: (phrasing, capability id). These are the reported live failures plus the
#: phrasings that already worked and must keep working.
MUST_DISPATCH = [
    ("what is the network status", "security.network"),
    ("network status", "security.network"),
    # Lock STATE belongs to vault.status, not vault.secrets: vault_list
    # answers with LABELS, which is both non-responsive to "is it locked"
    # and the sensitive half of the vault.
    ("is the vault locked", "vault.status"),
    ("vault status", "vault.status"),
    # --- normalization. Each of these pins a step that a mutation test showed
    # --- was otherwise unprotected: deleting the politeness peel, or the
    # --- case-fold, left the suite green while breaking real phrasings.
    ("hey puff, network status", "security.network"),      # wake word
    ("please tell me the network status", "security.network"),  # politeness
    ("network status please", "security.network"),         # trailing politeness
    ("can you tell me the network status", "security.network"),
    ("NETWORK STATUS", "security.network"),                # case-fold
    ("Is The Vault Locked?", "vault.status"),              # case + punctuation
    ("what's the network status?", "security.network"),    # contraction + "?"
    ("  network status  ", "security.network"),            # whitespace
]


@pytest.mark.parametrize("phrasing,cap_id", MUST_DISPATCH)
def test_reported_failures_now_dispatch(phrasing, cap_id):
    cap = reg.find_for_dispatch(phrasing)
    assert cap is not None, f"{phrasing!r} still falls through"
    assert cap.id == cap_id


# ── fail-closed by construction ─────────────────────────────────────────────

def test_capability_without_dispatch_phrases_is_not_dispatchable():
    """Default empty means a NEW capability is never dispatchable by accident.

    This is the property that makes the registry safe to extend: adding a
    capability cannot widen what a chat message can invoke until someone
    deliberately writes a dispatch phrase and defends it.
    """
    silent = [c for c in reg.all_capabilities() if not getattr(c, "dispatch_phrases", ())]
    assert silent, "expected most capabilities to declare no dispatch phrase"
    for cap in silent:
        for phrase in cap.phrases or ():
            hit = reg.find_for_dispatch(phrase)
            assert hit is None or hit.id != cap.id, (
                f"{cap.id} declares no dispatch_phrases but matched {phrase!r}")


def test_dispatch_never_consults_keywords():
    """Keywords are a BINDING signal. Using them to dispatch is what broke
    attempt 1 -- calendar.timers lists 'dismiss', 'snooze' and 'silence the
    alarm' among its keywords, because that list exists to bind the alarm
    tools, not to answer with one."""
    for cap in reg.all_capabilities():
        for kw in cap.keywords or ():
            kw = kw.strip()
            # A bare keyword, on its own, must never be enough to dispatch --
            # unless it was ALSO deliberately declared as a dispatch phrase.
            if not kw or kw in (getattr(cap, "dispatch_phrases", ()) or ()):
                continue
            hit = reg.find_for_dispatch(kw)
            assert hit is None or hit.id != cap.id, (
                f"{cap.id} dispatched on bare keyword {kw!r}")


# ── the api_server side: containment, guards, and ordering ──────────────────

def test_registry_layer_cannot_reach_an_untrusted_tool():
    """THE containment invariant, walked across every capability.

    Widening the trigger must not widen what a chat message can invoke: every
    tool the registry layer can dispatch has to be one the explicit rules
    already list as a zero-argument read.
    """
    import puffpai.api_server as api
    trusted = set(api._CAPREAD_ZERO_ARG)
    assert trusted, "the derived allowlist must not be empty"
    for cap in reg.all_capabilities():
        tool = api._capread_registry_tool(cap)
        if tool is not None:
            assert tool in trusted, f"{cap.id} -> untrusted {tool}"


def test_write_tools_are_never_registry_dispatchable():
    """``app_complete_task`` COMPLETES the task it is handed."""
    import puffpai.api_server as api
    assert "app_complete_task" not in api._CAPREAD_ZERO_ARG
    arg_tools = {n for n, _m, _e, needs_q in api._CAPREAD_RULES if needs_q}
    # A read tool may own both a browse rule and a lookup rule (for example
    # app_list_pantry answers "what's in the pantry" AND "do we have rice in
    # the pantry"). Those are declared in api_server, next to the table, so an
    # exemption is a visible change to the guarded file — never a test edit.
    exempt = api._CAPREAD_DUAL_ARITY_READS
    assert "app_complete_task" not in exempt
    assert exempt <= set(api._CAPREAD_ZERO_ARG), (
        "a dual-arity exemption must name a tool the rules already trust as a "
        "zero-arg READ")
    assert arg_tools and not ((arg_tools - exempt) & set(api._CAPREAD_ZERO_ARG))


def test_ambiguous_capabilities_decline_rather_than_guess():
    """pantry.shopping owns two read tools; "what is in the pantry" must not
    be answered with the shopping list."""
    import puffpai.api_server as api
    cap = reg.get("pantry.shopping")
    assert cap is not None
    assert api._capread_registry_tool(cap) is None


#: Action phrasings an earlier attempt let through into the read fastpath.
#: Each one cost a real misfire, so each is pinned by name.
ACTION_TURNS = [
    "silence my alarm", "silence the alarm", "wake me at 7am",
    "unblock my laptop", "unblock the tv", "grant the tablet access",
    "authorize the new laptop", "give the tablet permission", "start spotify",
    "add milk to the pantry", "cancel my alarm", "approve the camera",
    "unlock the vault", "delete that document", "turn on the lights",
    "set a timer for 5 minutes", "log my workout",
    # --- verbs of transmission and repair, added after an adversarial pass
    # --- found them slipping the guard. Whole-utterance equality declines
    # --- these too, but the guard must not quietly rot: a phrase list only
    # --- grows, and this is the defence that does not depend on the next
    # --- phrase being written carefully.
    "send the network status to my landlord", "email me the network status",
    "text me the network status", "share the network status with the hoa",
    "print the vault status", "upload the vault status to dropbox",
    "tweet the network status", "forward the network status to support",
    "copy the network status to the clipboard", "export the network status",
    "announce the network status on the speaker", "publish the network status",
    "reset the network status", "fix the network status",
    "ignore the network status", "stop asking about the network status",
    "remind me to check the network status",
]


@pytest.mark.parametrize("turn", ACTION_TURNS)
def test_action_turns_never_reach_the_read_fastpath(turn):
    import puffpai.api_server as api
    assert api._CAPREAD_WRITE_INTENT_RE.search(turn), f"action leaked: {turn}"


@pytest.mark.parametrize("turn", [
    "what is the network status", "is the vault locked", "vault status",
    "what is in my vault", "who is on my network", "what alarms are set",
])
def test_the_guard_does_not_eat_the_reads_it_guards(turn):
    import puffpai.api_server as api
    assert not api._CAPREAD_WRITE_INTENT_RE.search(turn)


def test_silence_my_alarm_still_reaches_the_dismiss_path():
    """The regression that got a first attempt rolled back.

    Asserted as an OUTCOME, not as "no dispatch happened": the alarm-dismiss
    fastpath must be the thing that claims this turn, because the failure mode
    was the person's alarm continuing to ring while she was told what alarms
    she had.
    """
    import puffpai.api_server as api
    # Nothing upstream may claim it...
    for name, match_re, exclude_re, _nq in api._CAPREAD_RULES:
        if match_re.search("silence my alarm") and not (
                exclude_re and exclude_re.search("silence my alarm")):
            pytest.fail(f"explicit rule {name} claimed 'silence my alarm'")
    assert api._try_registry_read("silence my alarm") is None
    # ...and the dismiss fastpath must recognise it.
    called = {}

    class _FakeTool:
        def invoke(self, arg):
            called["hit"] = True
            return "Alarm silenced."

    class _FakeTools:
        tool_dict = {"app_dismiss_alarm": _FakeTool()}

    out = api._try_alarm_dismiss("silence my alarm", _FakeTools())
    assert called.get("hit"), "alarm-dismiss fastpath did not fire"
    assert out and "silenced" in out.lower()


def test_dispatch_runs_after_the_action_fastpaths():
    """Ordering is the second defence. Assert it structurally so a future edit
    that hoists the registry call back above _try_alarm_dismiss fails here."""
    import inspect
    import puffpai.api_server as api
    src = inspect.getsource(api._try_instant_answer)
    assert "_try_registry_read" in src
    assert src.index("_try_alarm_dismiss") < src.index("_try_registry_read"), (
        "the registry read must run AFTER the alarm-dismiss fastpath")


def test_trailing_politeness_trim_does_not_bite_into_a_word():
    """The trim must not turn a different word into a declared phrase.

    "network statusthanks" trimmed to "network status" and dispatched, because
    the trailing alternative lacked the word boundary its two leading siblings
    have. Not exploitable in English, but a matcher whose whole correctness
    argument is exact equality must not have a normalizer that eats word parts.
    """
    assert reg.find_for_dispatch("network statusthanks") is None
    assert reg.find_for_dispatch("network status displease") is None
    # ...while the real trailing politeness still works.
    assert reg.find_for_dispatch("network status thanks") is not None
    assert reg.find_for_dispatch("network status, please") is not None


def test_normalization_of_nothing_is_nothing():
    """Politeness-only input must not reduce to a phrase, or to a crash."""
    for empty in ("", "   ", "please", "can you", "ok ok ok", "hey puff",
                  "...", "?!", None):
        assert reg.find_for_dispatch(empty) is None
