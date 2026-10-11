"""A persona scaffolded from a goal nothing ships for, and used with no model.

The walk these tests follow: goal "a garden and greenhouse assistant for a
small family farm". Three properties, all of them here as tests.

1. **The interview is honest.** It does not propose Calendar just because the
   word FAMILY appears: household filler is stopwords, the calendar carries no
   such tag, and a goal nothing matches says exactly that and offers the
   scaffold instead of "these are the household defaults".
2. **There is a scaffold.** ``puffpai persona new`` writes one app and one
   pack, into the OWNER's directories rather than the checkout, validated by the
   real validator before anything is written.
3. **Chat reaches it without a model.** "add tomatoes to the plantings list"
   must not fall through to the registry-driven reads or the model and 503 on a
   machine with no Ollama. The four list verbs are deterministic,
   manifest-driven, and verified by reading the document back off disk.

No Ollama and no network: the model is never called on these paths, and one
fixture asserts that by making the call fail the test if it happens.

Run:
    py -3 -m pytest tests/test_persona_scaffold.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server                      # noqa: E402
from puffpai import app_data as app_data_mod        # noqa: E402
from puffpai import app_fastpath                    # noqa: E402
from puffpai import app_registry                    # noqa: E402
from puffpai import build_interview                 # noqa: E402
from puffpai import cli                             # noqa: E402
from puffpai import persona_scaffold                # noqa: E402
from puffpai.app_data import AppData                # noqa: E402

AUTH = {"X-API-Key": api_server._API_KEY}

_REPO_ROOT = Path(__file__).resolve().parents[1]

GARDEN_GOAL = ("a garden and greenhouse assistant for a small family farm: "
               "remind me when to water, keep a planting list, and tell me "
               "what the frost forecast looks like")


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Every test in this file starts and ends with a re-read app registry.

    The manifest cache is process-wide, and these tests install an app into a
    temporary directory. Without this, a scaffolded GardenBot stays in the cache
    after its folder has gone - which is both a false pass here and a stray
    sixth app in whatever test file runs next.
    """
    app_registry.rescan()
    api_server._reset_app_data()
    yield
    app_registry.rescan()
    api_server._reset_app_data()


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """An owner's own apps/packs directories, and an app_data dir to match.

    ``$PUFFPAI_DATA_DIR`` is what :func:`app_registry.user_apps_dir` resolves
    through, so pointing it at tmp_path is the whole isolation: a scaffolded app
    cannot leak into the repo, and cannot leak into the next test either. The
    store is redirected as well (``app_data._data_dir`` caches its answer, so the
    accessor itself is replaced), because an app's document must land under the
    data directory and never in the checkout.
    """
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_mod, "_data_dir", lambda: str(tmp_path / "app_data"))
    app_registry.rescan()
    api_server._reset_app_data()
    return tmp_path


# ---------------------------------------------------------------------------
# 1. the interview tells the truth
# ---------------------------------------------------------------------------

def test_household_filler_cannot_carry_a_match_on_its_own():
    """FAMILY is not a tag, so a farm goal is not offered Calendar."""
    for word in ("family", "families", "small", "everyday", "kids", "household"):
        assert word in build_interview._STOPWORDS, word
    calendar = next(item for item in build_interview.load_catalog()["items"]
                    if item["id"] == "app:calendar")
    assert "family" not in calendar["tags"]

    # A goal that is ONLY household filler has told us nothing, and gets the
    # better question rather than a menu.
    proposal = build_interview.propose("a bot for my small family")
    assert proposal["flat"] == [], proposal["flat"]
    assert "could mean almost anything" in proposal["message"].lower()

    # A concrete goal whose only overlap with the catalog WAS the word family
    # now matches nothing, and is told so.
    proposal = build_interview.propose("a family bookkeeping ledger for invoices")
    assert proposal["flat"] == [], proposal["flat"]
    assert "nothing we ship matches" in proposal["message"].lower()


def test_a_family_calendar_goal_still_finds_the_calendar():
    """Dropping the tag must not cost the app the goals it really answers."""
    ids = {e["id"] for e in
           build_interview.propose("a family calendar and reminders")["flat"]}
    assert "app:calendar" in ids


def test_a_garden_goal_matches_real_things_with_cited_reasons():
    """Reminders and the offline library - each citing the word that matched.

    Only things this build HAS. BuildBot's version of this test also expects a
    weather integration; this tree has a weather card but no catalog row for it,
    so nothing here claims one.
    """
    proposal = build_interview.propose(GARDEN_GOAL)
    ids = {entry["id"] for entry in proposal["flat"]}
    assert {"app:alarmflow", "pack:wikipedia"} <= ids
    assert "app:calendar" not in ids

    surfaces = build_interview.derive_surfaces(proposal)
    assert {"comms.timers", "knowledge.library"} <= set(surfaces["surfaces"])
    tags = {t for item in build_interview.load_catalog()["items"]
            for t in item["tags"]}
    for surface, reason in surfaces["reasons"].items():
        assert "because you asked about" in reason, (surface, reason)
        # Every cited term is first-party catalog vocabulary, never the typing.
        assert reason.split("'")[1] in tags, (surface, reason)


def test_nothing_matched_says_so_and_offers_the_scaffold():
    proposal = build_interview.propose("a bot about zzxq and qwph")
    assert proposal["flat"] == []
    assert proposal["scaffold_offer"] is True
    message = proposal["message"]
    assert message.startswith("Nothing we ship matches that yet.")
    assert "scaffold a blank persona" in message
    assert build_interview.SCAFFOLD_STEP in message


def test_the_scaffold_step_number_is_the_step_that_offers_it():
    """The sentence names a step; a renamed or moved step must break here."""
    from puffpai import setup_cli

    index = setup_cli.SETUP_STEPS.index("What this build is for") + 1
    assert build_interview.SCAFFOLD_STEP == (
        f"step {index} of {len(setup_cli.SETUP_STEPS)}")


# ---------------------------------------------------------------------------
# 2. the scaffold
# ---------------------------------------------------------------------------

def test_persona_new_writes_a_valid_app_and_pack_into_the_owners_dirs(owner):
    result = persona_scaffold.scaffold(
        "gardenbot", name="GardenBot", goal=GARDEN_GOAL,
        lists="plantings,harvest log")

    assert result["app_path"].startswith(str(owner))
    assert result["pack_path"].startswith(str(owner))
    assert app_registry.user_apps_dir() in result["app_path"]
    assert result["lists"] == ["plantings", "harvest_log"]
    assert result["installed"] is True and result["pack_installed"] is True

    with open(result["app_path"], encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert app_registry.validate_manifest(manifest) is None
    # Its OWN surface, not a borrowed one: whether this bot shows up in the
    # nav must not depend on whether an unrelated surface is on the allowlist.
    assert manifest["surfaces"] == ["persona.gardenbot"]
    assert manifest["ui"]["route"] == "/dashboard/apps/gardenbot"
    assert manifest["data"]["default"] == {"plantings": [], "harvest_log": []}
    # Vocabulary from the lists and the goal's real words; no filler.
    keywords = manifest["vocabulary"]["keywords"]
    assert "plantings" in keywords and "harvest log" in keywords
    assert "garden" in keywords and "greenhouse" in keywords
    for filler in ("small", "family", "me", "the"):
        assert filler not in keywords

    with open(result["pack_path"], encoding="utf-8") as fh:
        pack = json.load(fh)
    assert app_registry.validate_pack(pack, {"gardenbot"}) is None


def test_the_scaffolded_app_is_installed_without_touching_the_checkout(owner):
    persona_scaffold.scaffold("gardenbot", name="GardenBot", lists="plantings")
    ids = app_registry.installed_ids(app_registry.rescan())
    assert "gardenbot" in ids
    assert "gardenbot" not in os.listdir(_REPO_ROOT / "apps")
    assert "gardenbot" not in os.listdir(_REPO_ROOT / "packs")
    assert [p["id"] for p in persona_scaffold.personas()] == ["gardenbot"]
    # The five shipped apps are still installed alongside it.
    assert {"alarmflow", "calendar", "freshtable", "fitfam", "trivia"} <= set(ids)


def test_an_id_that_is_not_an_id_writes_nothing(owner):
    for bad in ("Garden Bot", "../evil", "9lives", "_hidden", "x"):
        with pytest.raises(persona_scaffold.ScaffoldError):
            persona_scaffold.scaffold(bad, name="Nope")
    assert not os.path.isdir(app_registry.user_apps_dir())


def test_scaffolding_over_an_existing_persona_is_refused(owner):
    persona_scaffold.scaffold("gardenbot", name="GardenBot", lists="plantings")
    with pytest.raises(persona_scaffold.ScaffoldError) as exc:
        persona_scaffold.scaffold("gardenbot", name="Another")
    assert "already" in str(exc.value)


def test_a_shipped_app_id_cannot_be_shadowed(owner):
    with pytest.raises(persona_scaffold.ScaffoldError) as exc:
        persona_scaffold.scaffold("calendar", name="Mine")
    assert "already installed" in str(exc.value)


def test_the_shipped_homebot_pack_id_cannot_be_shadowed(owner):
    """This build ships a pack called homebot, and one id per build is one id.

    Two pack.json files claiming it would drop BOTH out of the scan - the owner
    would lose HomeBot's own pack to a step that said it was helping - so the id
    the wizard OFFERS steps aside, and scaffolding it by hand is refused.
    """
    assert persona_scaffold.suggest_id("HomeBot") == "homebot_lists"
    with pytest.raises(persona_scaffold.ScaffoldError) as exc:
        persona_scaffold.scaffold("homebot", name="HomeBot")
    assert "pack called 'homebot' is already installed" in str(exc.value)
    assert [p["id"] for p in app_registry.load_packs()] == ["homebot"]


def test_the_cli_verbs_print_what_they_made(owner, capsys):
    args = cli.build_parser().parse_args(
        ["persona", "new", "gardenbot", "--name", "GardenBot",
         "--goal", GARDEN_GOAL, "--lists", "plantings,harvest log"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "Made GardenBot (gardenbot)." in out
    assert "plantings, harvest log" in out
    assert "/dashboard/apps/gardenbot" in out

    args = cli.build_parser().parse_args(["persona", "list"])
    assert args.func(args) == 0
    assert "gardenbot" in capsys.readouterr().out

    args = cli.build_parser().parse_args(["apps", "list"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "gardenbot" in out and "calendar" in out
    assert app_registry.user_apps_dir() in out

    args = cli.build_parser().parse_args(["apps", "rescan"])
    assert args.func(args) == 0
    assert "Rescanned." in capsys.readouterr().out


def test_persona_list_with_nothing_made_says_where_they_would_go(owner, capsys):
    args = cli.build_parser().parse_args(["persona", "list"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "No personas of your own yet." in out
    assert app_registry.user_packs_dir() in out


# ---------------------------------------------------------------------------
# 3. the four verbs, with no model anywhere
# ---------------------------------------------------------------------------

@pytest.fixture
def client(owner, monkeypatch):
    """A Flask client over a build carrying GardenBot, and NO model.

    ``_query_ollama_direct`` is replaced with a test failure: every assertion
    below is about a path that must not consult a model at all, so a call is a
    defect rather than a slow test.
    """
    persona_scaffold.scaffold("gardenbot", name="GardenBot", goal=GARDEN_GOAL,
                              lists="plantings,harvest log")
    ad = AppData()
    assert "gardenbot" in ad.VALID_APPS
    monkeypatch.setattr(api_server, "_get_app_data", lambda: ad)

    def _no_model(*a, **k):
        raise AssertionError("the model was consulted for a declarative app")

    monkeypatch.setattr(api_server, "_query_ollama_direct", _no_model)
    api_server.app.config["TESTING"] = True
    api_server._rate_limit_store.clear()
    return api_server.app.test_client()


def _say(client, message: str) -> str:
    resp = client.post("/chat/send", json={"message": message}, headers=AUTH)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["response"]


def _speak(client, message: str) -> str:
    """The same sentence down the VOICE entry (``/api/chat``)."""
    resp = client.post("/api/chat", headers=AUTH,
                       json={"messages": [{"role": "user", "content": message}]})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()["message"]["content"]


def test_the_greenhouse_example_end_to_end_without_ollama(client):
    """The whole scenario, through /chat/send, with no brain."""
    assert "Added tomatoes to the plantings list" in _say(
        client, "add tomatoes to the plantings list")
    assert "tomatoes" in _say(client, "what's on the plantings list")

    # ...and it is really on disk, not merely in a sentence.
    stored = client.get("/apps/gardenbot/data", headers=AUTH).get_json()
    assert [row["item"] for row in stored["plantings"]] == ["tomatoes"]

    assert "One thing" in _say(
        client, "how many things are on the plantings list")
    assert "Took tomatoes off" in _say(
        client, "remove tomatoes from the plantings list")
    assert _say(client, "what is on the plantings list").endswith("is empty.")


def test_the_voice_entry_answers_the_same_way(client):
    """Spoken or typed, the same store and the same deterministic sentence."""
    assert "Added basil to the plantings list" in _speak(
        client, "add basil to the plantings list")
    assert "basil" in _speak(client, "what is on the plantings list")
    stored = client.get("/apps/gardenbot/data", headers=AUTH).get_json()
    assert [row["item"] for row in stored["plantings"]] == ["basil"]


def test_each_list_is_its_own_list(client):
    _say(client, "add tomatoes to the plantings list")
    _say(client, "add four crates of squash to the harvest log")
    assert "squash" in _say(client, "show me the harvest log")
    assert "squash" not in _say(client, "what is on the plantings list")


def test_an_ambiguous_sentence_asks_instead_of_guessing(client):
    answer = _say(client, "add compost to the list")
    assert answer == "Which list - plantings or harvest log?"
    stored = client.get("/apps/gardenbot/data", headers=AUTH).get_json()
    assert stored["plantings"] == [] and stored["harvest_log"] == []


def test_a_write_that_did_not_land_is_not_reported_as_saved(client, monkeypatch):
    """Evidence or silence: no read-back, no claim of success."""
    ad = api_server._get_app_data()
    monkeypatch.setattr(ad, "set_data", lambda *a, **k: False)
    answer = _say(client, "add tomatoes to the plantings list")
    assert "could not save" in answer.lower()
    assert "added" not in answer.lower()


def test_the_hand_written_apps_are_left_alone(owner):
    """FreshTable's shopping list is FreshTable's sentence, not this module's."""
    persona_scaffold.scaffold("gardenbot", name="GardenBot",
                              lists="plantings,harvest log")
    ad = AppData()
    assert app_fastpath.handle("add milk to the shopping list", ad) is None
    assert app_fastpath.handle("set a timer for ten minutes", ad) is None
    assert app_fastpath.handle("what is on my calendar tomorrow", ad) is None
    assert app_fastpath.handle("log my workout", ad) is None
    assert app_fastpath.handle("tell me a joke", ad) is None
    assert app_fastpath.handle("", ad) is None
    # Every app that ships WITH CODE OF ITS OWN is named in the exclusion list,
    # by id: the folders in the checkout's apps/ are exactly the hand-written set.
    shipped = {f.name for f in (_REPO_ROOT / "apps").iterdir()
               if f.is_dir() and not f.name.startswith("_")}
    assert shipped <= app_fastpath.HANDWRITTEN


def test_the_shopping_list_still_reaches_freshtable_through_chat(client):
    """With a declarative app installed, FreshTable keeps its own sentence."""
    answer = _say(client, "add oat milk to the shopping list")
    assert "shopping list" in answer.lower()
    stored = client.get("/apps/freshtable/data", headers=AUTH).get_json()
    assert any("oat milk" in json.dumps(row).lower()
               for row in stored["shoppingList"])
    gardenbot = client.get("/apps/gardenbot/data", headers=AUTH).get_json()
    assert gardenbot["plantings"] == [] and gardenbot["harvest_log"] == []


def test_a_build_with_no_declarative_app_never_answers_here():
    """Nothing scaffolded, nothing claimed - the shipped apps are excluded."""
    ad = AppData()
    assert app_fastpath.handle("add tomatoes to the plantings list", ad) is None


# ---------------------------------------------------------------------------
# 4. what the box says it carries
# ---------------------------------------------------------------------------

def test_apps_installed_lists_the_scaffolded_app_and_pack(client):
    payload = client.get("/apps/installed", headers=AUTH).get_json()
    assert "gardenbot" in [record["id"] for record in payload["apps"]]
    assert payload["problems"] == []
    packs = client.get("/apps/packs", headers=AUTH).get_json()
    assert "gardenbot" in [record["id"] for record in packs["packs"]]


def test_apps_installed_needs_the_api_key():
    assert api_server.app.test_client().get("/apps/installed").status_code == 401
