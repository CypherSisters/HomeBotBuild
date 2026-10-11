"""Security properties of the media egress gate.

THE CORE PROPERTY (G, privacy)
    Deciding whether "put X on" is a radio request by ASKING THE CATALOG
    (``search_stations(X)``) would egress to the radio-browser.org mirrors.
    With the ``radio_browser`` door open, the object of every non-blocklisted
    "put X on" would leave the house before the box decided not to act on it,
    even when it then correctly plays nothing:

        "put grandma on" · "put my therapist on" · "put dr smith on"
        "put my medication reminder on" · "put the divorce papers on"

    The decision is made locally by ``puffpai.radio_cues.has_radio_cue``
    and is fail-closed: no catalog call unless the utterance says "radio".

Also pinned here: the A/B/C phrasing gaps, and the residual cases
(Pulse-unreadable honesty and a player key written at a possibly-poisoned
prompt).

Nothing in this file touches the network: every egress-capable function is
replaced with a recorder that FAILS the test if it is ever called.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import now_playing as np  # noqa: E402
from puffpai import radio_cues  # noqa: E402


# ═════════════════════════════════════════════════════════════════════════════
# G · no household noun egresses
# ═════════════════════════════════════════════════════════════════════════════

#: Person, health and document nouns — the shapes a household actually says
#: out loud. Fake names only: no real household member appears.
HOUSEHOLD_NOUNS = (
    # people
    "put grandma on",
    "put my therapist on",
    "put dr smith on",
    "put my mother on",
    "put uncle bob on",
    "put test fakeperson on",
    "put my counselor on",
    "put the babysitter on",
    # health
    "put my medication reminder on",
    "put my prescription on",
    "put the blood test results on",
    "put my therapy notes on",
    # documents
    "put the divorce papers on",
    "put the mortgage paperwork on",
    "put the will on",
    "put the tax return on",
    # verb-first form — the same rule must cover "put on X" (defect B negative)
    "put on a sweater",
    "put on my therapist",
    "put on grandma",
    "put on the divorce papers",
)

#: A household noun sitting NEXT TO a real radio cue. Each of these genuinely
#: says "radio" or "station", so a cue gate alone passes them, and the
#: remainder ('therapist's show', 'grandma's', ...) would go to the mirrors.
#: Fake names only.
CUE_ADJACENT_NOUNS = (
    "put on my therapist's radio show",
    "put on grandma's radio show",
    "put on grandma's station",
    "play a station for grandma",
)

#: The "tune the guitar" class: "tune" with no radio meaning at all.
TUNE_NOT_RADIO = (
    "tune the guitar",
    "tune my guitar",
    "tune in the guitar",
    "tune in my guitar",
    "tune the piano",
    "tune in the piano",
)


@pytest.fixture
def egress_recorder(monkeypatch):
    """Record every call to anything that can reach the network.

    Patches the catalog helpers AND the door itself, so a future caller that
    bypasses ``search_stations`` and dials ``egress_get`` directly is caught by
    the same test rather than silently reopening this hole.
    """
    calls = []

    def _record(name):
        def _fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return []
        return _fn

    monkeypatch.setattr("puffpai.media_hub.search_stations",
                        _record("search_stations"))
    monkeypatch.setattr("puffpai.media_hub.search_stations_by_tag",
                        _record("search_stations_by_tag"))
    monkeypatch.setattr("puffpai.security.egress_doors.egress_get",
                        _record("egress_get"))
    monkeypatch.setattr("puffpai.security.egress_doors.egress_request",
                        _record("egress_request"))
    # Two rungs BELOW the door, so a caller that builds its own
    # requests session — or merely RESOLVES a hostname — is caught here too.
    # A DNS lookup is itself an egress of whatever is in the query string.
    # Loopback is NOT egress: the appliance talks to its own ollama and radio
    # containers over localhost on nearly every turn, and recording those would
    # make this fixture assert on the wrong thing.
    import socket
    import requests

    _LOOPBACK = ("localhost", "127.0.0.1", "::1", "0.0.0.0")

    def _record_offbox(name, host_of):
        def _fn(*args, **kwargs):
            host = (host_of(args, kwargs) or "").lower()
            if not any(h in host for h in _LOOPBACK):
                calls.append((name, args, kwargs))
            return []
        return _fn

    monkeypatch.setattr(
        requests.Session, "request",
        _record_offbox("http", lambda a, k: str(k.get("url") or
                                                (a[2] if len(a) > 2 else ""))))
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _record_offbox("dns", lambda a, k: str(a[0] if a else "")))
    return calls


class TestNoHouseholdNounEgresses:
    """No household text reaches an external host without an explicit radio cue."""

    @pytest.mark.parametrize("said", HOUSEHOLD_NOUNS)
    def test_fastpath_makes_zero_catalog_calls(self, said, egress_recorder):
        api._try_tunein_radio(said)
        assert egress_recorder == [], (
            f"{said!r} sent household text to an external host: "
            f"{egress_recorder!r}")

    @pytest.mark.parametrize("said", HOUSEHOLD_NOUNS)
    def test_fastpath_declines_the_turn(self, said, egress_recorder):
        assert api._try_tunein_radio(said) is None

    @pytest.mark.parametrize("said", TUNE_NOT_RADIO)
    def test_tune_the_guitar_class_makes_zero_catalog_calls(
            self, said, egress_recorder):
        assert api._try_tunein_radio(said) is None
        assert egress_recorder == [], (
            f"{said!r} is not a radio request and must not egress: "
            f"{egress_recorder!r}")

    @pytest.mark.parametrize("said", HOUSEHOLD_NOUNS + TUNE_NOT_RADIO)
    def test_whole_dispatcher_makes_zero_catalog_calls(self, said,
                                                       egress_recorder,
                                                       monkeypatch):
        """Through the real dispatcher, not just the one function: no LATER
        fastpath may pick the utterance up and egress it instead."""
        monkeypatch.setattr(np, "observed_playback", lambda *a, **k: [])
        try:
            api._try_instant_answer(said)
        except Exception:
            pass  # an unrelated fastpath erroring is not this test's business
        assert egress_recorder == [], (
            f"{said!r} egressed via the dispatcher: {egress_recorder!r}")

    def test_the_gate_fails_closed_when_it_cannot_run(self, egress_recorder,
                                                      monkeypatch):
        """If the cue module itself is broken, decline — never fall open."""
        def _explode(*_a, **_k):
            raise RuntimeError("cue module unavailable")

        monkeypatch.setattr(radio_cues, "has_radio_cue", _explode)
        assert api._try_tunein_radio("put grandma on") is None
        assert egress_recorder == []

    def test_a_cue_carrying_phrase_is_still_allowed_through(self,
                                                            egress_recorder):
        """The gate must not be so tight that a real station request fails."""
        api._try_tunein_radio("put NPR on")
        assert any(c[0] == "search_stations" for c in egress_recorder), (
            "an explicit call sign must still reach the catalog")

    def test_the_query_that_egresses_carries_no_household_text(
            self, egress_recorder):
        api._try_tunein_radio("put NPR on")
        sent = " ".join(str(a) for c in egress_recorder for a in c[1]).lower()
        for leak in ("grandma", "therapist", "divorce", "medication"):
            assert leak not in sent

    # ── a household noun sitting NEXT TO a real cue ─────────────────────────
    #
    # These all DO say "radio"/"station", but if the cue opened the door for
    # the whole utterance, the REMAINDER (which is where the household noun
    # lives) would go to the mirrors anyway.

    @pytest.mark.parametrize("said", CUE_ADJACENT_NOUNS)
    def test_a_cue_next_to_a_household_noun_still_egresses_nothing(
            self, said, egress_recorder):
        reply = api._try_tunein_radio(said) or api._try_media_play(said)
        assert egress_recorder == [], (
            f"{said!r} sent the remainder to an external host: "
            f"{egress_recorder!r}")
        # It must also be ANSWERED — declining to None would drop it into RAG,
        # which is where the fabricated station answers came from.
        assert reply == radio_cues.STATION_QUERY_HELP, reply

    @pytest.mark.parametrize("said", CUE_ADJACENT_NOUNS)
    def test_the_dispatcher_egresses_nothing_either(self, said,
                                                    egress_recorder,
                                                    monkeypatch):
        monkeypatch.setattr(np, "observed_playback", lambda *a, **k: [])
        try:
            api._try_instant_answer(said)
        except Exception:
            pass
        assert egress_recorder == [], (
            f"{said!r} egressed via the dispatcher: {egress_recorder!r}")

    def test_a_music_request_for_a_person_is_tag_only(self, egress_recorder):
        """'play some music for Casey' — a name, no radio noun at all. The
        genre axis may answer it; the NAME axis must never see the sentence."""
        api._try_media_play("play some music for Casey")
        names = [c for c in egress_recorder if c[0] == "search_stations"]
        assert names == [], f"the free text reached the name search: {names!r}"
        sent = " ".join(str(a) for c in egress_recorder
                        for a in c[1] + tuple(c[2].values())).lower()
        assert "casey" not in sent, f"the household name egressed: {sent!r}"

    @pytest.mark.parametrize("remainder,expected", [
        # What the fastpaths actually hand the gate, once the cue words are
        # stripped. "reject" and "empty" both decline; only "name" and "tag"
        # may search, and "tag" may only search the closed tag vocabulary.
        ("therapist's show", "reject"),
        ("grandma's", "reject"),
        ("grandma", "reject"),
        ("play a station for grandma", "empty"),   # recipient phrase dropped
        ("npr", "name"),
        ("jazz", "tag"),
        ("90.3 fm", "name"),
    ])
    def test_the_remainder_gate_classifies_locally(self, remainder, expected,
                                                   egress_recorder):
        """The gate itself: same verdicts, no network, called directly."""
        kind, _ = radio_cues.plan_station_query(
            remainder, tag_words=api._STATION_TAG_WORDS)
        assert kind == expected, (remainder, kind)
        assert egress_recorder == []

    def test_a_possessive_is_never_forwarded(self):
        """Whatever the gate DOES hand on carries no possessive or person."""
        for said in ("grandma's jazz station", "my therapist's npr show"):
            kind, value = radio_cues.plan_station_query(
                said, tag_words=api._STATION_TAG_WORDS)
            text = " ".join(value) if isinstance(value, tuple) else str(value)
            for leak in ("grandma", "therapist", "'s", "show"):
                assert leak not in text, (said, value)


class TestTheLedgerNeverRecordsTheQuery:
    """Requirement 2: an egress that DOES happen is logged content-free."""

    def test_station_search_puts_the_query_in_params_not_the_url(self,
                                                                 monkeypatch):
        """The ledger records the URL path with the query discarded, so the
        search term must travel as `params`, never inside the URL string."""
        seen = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return []

        def _fake_get(door, url, **kwargs):
            seen["door"] = door
            seen["url"] = url
            seen["params"] = kwargs.get("params")
            return _Resp()

        monkeypatch.setattr("puffpai.security.egress_doors.egress_get",
                            _fake_get)
        from puffpai import media_hub
        media_hub.search_stations("kexp seattle")
        assert seen["door"] == "radio_browser"
        assert "?" not in seen["url"], "a query in the URL would reach the ledger"
        assert "kexp" not in seen["url"].lower()
        assert seen["params"]["name"] == "kexp seattle"

    def test_safe_parts_discards_the_query(self):
        from puffpai.security import egress_doors as ed
        host, path = ed._safe_parts(
            "https://de1.api.radio-browser.info/json/stations/search"
            "?name=grandma&limit=1")
        assert "grandma" not in (host + path)
        assert "?" not in path


# ═════════════════════════════════════════════════════════════════════════════
# radio_cues — the local, offline allowlist
# ═════════════════════════════════════════════════════════════════════════════

class TestRadioCuesAreDecidedLocally:

    @pytest.mark.parametrize("said", [
        "put the jazz station on", "put on npr", "put kexp on",
        "put on 88.5 fm", "put the radio on", "put on bbc radio 4",
        "tune in to kcrw", "play jazz on tunein", "put on world radio",
        "put wnyc on", "put on 1010 am",
    ])
    def test_explicit_radio_cues_are_recognised(self, said):
        assert radio_cues.has_radio_cue(said) is True

    @pytest.mark.parametrize("said", [
        "put grandma on", "put on a sweater", "tune the guitar",
        "put my therapist on", "put the divorce papers on",
        "put my medication reminder on", "tune in the guitar",
        "put the kettle on", "what am i doing today",
        # bare "am" is the VERB, not the dial — must not be a cue
        "put on whatever i am into",
    ])
    def test_everything_else_is_not_a_cue(self, said):
        assert radio_cues.has_radio_cue(said) is False

    def test_the_cue_check_never_touches_the_network(self, egress_recorder):
        for said in HOUSEHOLD_NOUNS:
            radio_cues.has_radio_cue(said)
        assert egress_recorder == []

    def test_the_last_played_station_counts_as_a_cue(self, monkeypatch):
        """The household's own listening history is a LOCAL signal."""
        monkeypatch.setattr(radio_cues, "_remembered_station",
                            lambda _ad=None: "Adroit Jazz Underground")
        assert radio_cues.has_radio_cue("put adroit jazz underground on")
        assert radio_cues.has_radio_cue("put grandma on") is False

    def test_a_too_short_remembered_name_is_ignored(self, monkeypatch):
        monkeypatch.setattr(radio_cues, "_remembered_station", lambda _ad=None: "mix")
        assert radio_cues.has_radio_cue("put the mix on") is False

    def test_an_unreadable_mediahub_doc_does_not_open_the_gate(self,
                                                               monkeypatch):
        def _explode(*_a, **_k):
            raise RuntimeError("no app data")

        monkeypatch.setattr(radio_cues, "_remembered_station", _explode)
        assert radio_cues.has_radio_cue("put grandma on") is False


class TestVibeFallbackDoesNotLeakFreeText:
    """Defence in depth: `_try_media_play`'s genre fallback also ran a
    station-NAME search on free text before falling back to the tags."""

    def test_a_music_request_searches_tags_not_the_free_text(self,
                                                             egress_recorder):
        api._try_media_play("play some music for grandma")
        names = [c for c in egress_recorder if c[0] == "search_stations"]
        assert names == [], (
            "the free-text name search must be gated behind a radio cue")

    def test_the_genre_tag_search_still_runs(self, egress_recorder):
        api._try_media_play("play some jazz")
        assert any(c[0] == "search_stations_by_tag" for c in egress_recorder), (
            "a vibe request must still resolve by genre tag")

    def test_an_explicit_radio_request_may_still_name_search(self,
                                                             egress_recorder):
        api._try_media_play("play npr radio")
        assert any(c[0] == "search_stations" for c in egress_recorder)


# ═════════════════════════════════════════════════════════════════════════════
# A · now-playing phrasing gaps
# ═════════════════════════════════════════════════════════════════════════════

NOW_PLAYING_PHRASINGS = (
    "what is this song",
    "what's this song",
    "now playing",
    "what am I listening to",
    "what song is this",
    "who sings this",
    "what track is this",
    "who is playing",
)

#: Ambiguous on their own — claimed only while something is actually playing.
NOW_PLAYING_AMBIGUOUS = ("what's this", "whats this", "who's this")


@pytest.fixture
def playing_radio(monkeypatch):
    monkeypatch.setattr(np, "observed_playback",
                        lambda *a, **k: [{"source": "World Radio",
                                          "label": "Adroit Jazz Underground"}])


@pytest.fixture
def silent(monkeypatch):
    monkeypatch.setattr(np, "observed_playback", lambda *a, **k: [])


class TestNowPlayingPhrasings:

    @pytest.mark.parametrize("said", NOW_PLAYING_PHRASINGS)
    def test_phrasing_is_claimed(self, said, playing_radio):
        assert api._try_now_playing(said) == (
            "Adroit Jazz Underground on World Radio.")

    @pytest.mark.parametrize("said", NOW_PLAYING_PHRASINGS)
    def test_phrasing_is_claimed_by_the_dispatcher(self, said, playing_radio):
        assert api._try_instant_answer(said) == (
            "Adroit Jazz Underground on World Radio.")

    @pytest.mark.parametrize("said", NOW_PLAYING_AMBIGUOUS)
    def test_ambiguous_form_answers_while_music_plays(self, said,
                                                      playing_radio):
        assert api._try_now_playing(said) == (
            "Adroit Jazz Underground on World Radio.")

    @pytest.mark.parametrize("said", NOW_PLAYING_AMBIGUOUS)
    def test_ambiguous_form_declines_in_silence(self, said, silent):
        """"what's this" over a photo must not be answered with "nothing is
        playing" — it is only a music question when there is music."""
        assert api._try_now_playing(said) is None

    @pytest.mark.parametrize("said", [
        "what's playing at the theater",
        "what's playing at the theatre",
        "what's playing on netflix",
        "what's playing on spotify",
        "what's for dinner",
        "what's the weather",
    ])
    def test_negatives_still_decline(self, said, playing_radio):
        assert api._try_now_playing(said) is None

    def test_only_one_probe_per_turn(self, monkeypatch):
        """A turn must not probe the players twice."""
        calls = []
        monkeypatch.setattr(np, "observed_playback",
                            lambda *a, **k: calls.append(1) or
                            [{"source": "World Radio", "label": "KEXP"}])
        api._try_now_playing("what's playing")
        assert len(calls) == 1


# ═════════════════════════════════════════════════════════════════════════════
# B · "put on NPR" — verb first, no trailing "on", no radio noun
# ═════════════════════════════════════════════════════════════════════════════

class TestPutOnACallSign:

    @pytest.mark.parametrize("said", [
        "put on NPR", "put on npr", "put NPR on", "put on kexp",
        "put on bbc radio", "put on the jazz station",
    ])
    def test_a_call_sign_reaches_the_player(self, said, monkeypatch):
        played = []
        monkeypatch.setattr(api, "_radio_play_for_query",
                            lambda q, low="": played.append(q) or
                            f"Playing {q} on the speakers.")
        reply = api._try_tunein_radio(said)
        assert reply and "speakers" in reply, f"{said!r} must reach the player"
        assert played

    @pytest.mark.parametrize("said", [
        "put on a sweater", "put on my coat", "put on the kettle",
        "put on grandma", "put on the dishwasher",
    ])
    def test_a_non_station_object_never_reaches_the_player(self, said,
                                                            monkeypatch):
        monkeypatch.setattr(api, "_radio_play_for_query",
                            lambda *a, **k: pytest.fail(
                                f"{said!r} must never start audio"))
        assert api._try_tunein_radio(said) is None

    @pytest.mark.parametrize("said", [
        "put on spotify", "put on my jazz playlist",
    ])
    def test_another_service_keeps_its_own_route(self, said):
        assert api._try_tunein_radio(said) is None


# ═════════════════════════════════════════════════════════════════════════════
# C · whole-house broadcast phrasings
# ═════════════════════════════════════════════════════════════════════════════

#: Common ways to say "every light off", in several shapes.
BROADCAST_PHRASINGS = (
    "shut off all the lights upstairs and downstairs",
    "every light off",
    "kill all the lights",
    "lights off everywhere",
    "all lights off",
    "turn off all the lights on every floor",
    "turn off all the lights in every room",
    "turn off all the lights across the house",
    "cut all the lights",
    "lights out everywhere",
)


@pytest.fixture(autouse=True)
def quiet_media(monkeypatch):
    monkeypatch.setattr(api, "_media_sources_still_playing", lambda: [])


@pytest.fixture
def no_ha(monkeypatch):
    def _forbidden(*_a, **_k):
        raise AssertionError("the untargeted gate must never call HA")

    import puffpai.ha_bridge as hb
    for cls_name in dir(hb):
        cls = getattr(hb, cls_name)
        if isinstance(cls, type) and hasattr(cls, "call_service"):
            monkeypatch.setattr(cls, "call_service", _forbidden)
    for fn in ("get_ha_bridge", "call_service"):
        if hasattr(hb, fn):
            monkeypatch.setattr(hb, fn, _forbidden)


class TestBroadcastLightsPhrasings:

    @pytest.mark.parametrize("said", BROADCAST_PHRASINGS)
    def test_phrasing_gets_the_whole_house_refusal(self, said, no_ha):
        reply = api._try_untargeted_house_command(said)
        assert reply, f"{said!r} must be claimed by the untargeted gate"
        low = reply.lower()
        # The misroute's tell: a media reply to a lights command.
        assert "music or radio" not in low
        assert "whole house from one command" in low
        assert "every light at once" in low

    @pytest.mark.parametrize("said", BROADCAST_PHRASINGS)
    def test_media_stop_is_never_invoked(self, said, no_ha, monkeypatch):
        monkeypatch.setattr(api, "_try_media_stop",
                            lambda *_a, **_k: pytest.fail(
                                f"_try_media_stop must not see {said!r}"))
        reply = api._try_instant_answer(said)
        assert reply and "music or radio" not in reply.lower()

    @pytest.mark.parametrize("said", [
        "turn off the music", "turn off the kitchen light",
        "turn off the living room lights", "turn off the lights in the bedroom",
        "turn off the lamp", "stop the music",
        # a single floor is a TARGET, not the whole house
        "turn off the lights upstairs",
    ])
    def test_targeted_and_media_phrasings_are_untouched(self, said):
        assert api._try_untargeted_house_command(said) is None

    def test_turn_off_the_music_still_stops_media(self, monkeypatch):
        monkeypatch.setattr("puffpai.media_hub.tool_radio_stop",
                            lambda app_data=None: "ok")
        assert api._try_media_stop("turn off the music")


# ═════════════════════════════════════════════════════════════════════════════
# Residuals
# ═════════════════════════════════════════════════════════════════════════════

class TestUnreadablePulseIsNotSilence:

    def test_observed_playback_reports_unknown(self, monkeypatch):
        monkeypatch.setattr(np, "_radio_source", lambda _ad: None)
        monkeypatch.setattr(np, "_jellyfin_sources", lambda: [])
        monkeypatch.setattr(np, "_pulse_sink_input_count", lambda: None)
        entries = np.observed_playback()
        assert entries and entries[0]["source"] == np.UNKNOWN_SOURCE

    def test_the_sentence_is_an_honest_unknown(self, monkeypatch):
        monkeypatch.setattr(np, "_radio_source", lambda _ad: None)
        monkeypatch.setattr(np, "_jellyfin_sources", lambda: [])
        monkeypatch.setattr(np, "_pulse_sink_input_count", lambda: None)
        reply = np.describe_playback()
        assert "can't tell" in reply.lower()
        assert "nothing is playing" not in reply.lower()

    def test_a_readable_and_empty_pulse_is_still_silence(self, monkeypatch):
        monkeypatch.setattr(np, "_radio_source", lambda _ad: None)
        monkeypatch.setattr(np, "_jellyfin_sources", lambda: [])
        monkeypatch.setattr(np, "_pulse_sink_input_count", lambda: 0)
        assert np.observed_playback() == []
        assert np.describe_playback() == "Nothing is playing right now."


# ═════════════════════════════════════════════════════════════════════════════
# a bare dial is a radio request: "play 90.3 fm"
# ═════════════════════════════════════════════════════════════════════════════
#
# `play <dial>` carries neither "radio" nor "station" nor a genre word, so
# without its own claim it falls through to RAG, which answers with prose
# about the frequency instead of tuning it. A number is
# also the one remainder that can never be household text, so it is safe to
# forward exactly as said.

DIALS = ("play 90.3 fm", "play 90.3", "play 1010 am", "tune to 90.3",
         "tune in to 90.3 fm", "put on 88.5 fm")


class TestABareDialIsClaimed:

    @pytest.fixture
    def played(self, monkeypatch):
        seen = []
        monkeypatch.setattr(api, "_radio_play_for_query",
                            lambda q, low="": seen.append(q) or
                            f"Playing {q} on the speakers.")
        return seen

    @pytest.mark.parametrize("said", DIALS)
    def test_the_dial_reaches_the_radio_player(self, said, played):
        reply = api._try_tunein_radio(said) or api._try_media_play(said)
        assert reply and "speakers" in reply, f"{said!r} was not claimed"
        assert played, f"{said!r} never reached _radio_play_for_query"
        assert any(ch.isdigit() for ch in played[-1]), played

    def test_the_decimal_point_survives_the_query_cleaner(self, played):
        """"90.3 fm" must not arrive as "90 3 fm", which matches nothing."""
        api._try_media_play("play 90.3 fm")
        assert "90.3" in played[-1], played

    @pytest.mark.parametrize("said", DIALS)
    def test_the_dial_never_falls_through_to_rag(self, said, played,
                                                 monkeypatch):
        """Through the real dispatcher, with RAG wired to explode."""
        def _no_rag(*_a, **_k):
            raise AssertionError(f"{said!r} reached RAG")

        monkeypatch.setattr(api, "_get_rag_context", _no_rag)
        monkeypatch.setattr(np, "observed_playback", lambda *a, **k: [])
        assert api._try_instant_answer(said), f"{said!r} was not claimed"

    def test_a_bare_year_is_still_a_song_not_a_frequency(self, monkeypatch):
        """"play 1999" is Prince, not 199.9 FM: no band word, no decimal."""
        monkeypatch.setattr(api, "_radio_play_for_query",
                            lambda *a, **k: pytest.fail("tuned a song title"))
        assert api._try_media_play("play 1999") is None

    def test_the_dial_gate_is_decided_locally(self, egress_recorder):
        kind, value = radio_cues.plan_station_query(
            "90.3 fm", tag_words=api._STATION_TAG_WORDS)
        assert (kind, value) == ("name", "90.3 fm")
        assert egress_recorder == []
