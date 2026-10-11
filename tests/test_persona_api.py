"""The persona routes: build one in the browser, talk to it, delete it.

``puffpai persona new`` is the terminal path. These four routes are the same
thing for somebody who never opens a terminal, and they go through the
SAME module (:mod:`puffpai.persona_scaffold`), so the two paths cannot drift into
validating different things or writing different files. That is what most of
this file asserts.

The rest is the other half of the same honesty: a build with no language model
must say what a request needed and how to fix it, in the
assistant's own sentence, rather than storing "[Error] Cannot connect to Ollama"
as though the bot had said it.

Gate classification, pinned at the bottom: all four routes require the app API
key from every peer, loopback included. Writing — or deleting — a folder in the
owner's apps directory is not a keyless operation.

No Ollama and no network: nothing here calls a model.

Run:
    py -3 -m pytest tests/test_persona_api.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server                      # noqa: E402
from puffpai import app_data as app_data_mod        # noqa: E402
from puffpai import app_registry                    # noqa: E402
from puffpai import persona_scaffold                # noqa: E402

AUTH = {"X-API-Key": api_server._API_KEY}

GARDEN_GOAL = ("a garden and greenhouse assistant for a small family farm: "
               "remind me when to water and keep a planting list")


@pytest.fixture(autouse=True)
def _fresh_registry():
    """The manifest cache is process-wide; a scaffolded app must not outlive its
    own test, or it becomes a stray sixth app in whatever file runs next."""
    app_registry.rescan()
    api_server._reset_app_data()
    api_server._rate_limit_store.clear()
    yield
    app_registry.rescan()
    api_server._reset_app_data()


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A client over a build with NO persona yet — the builder page's start.

    ``$PUFFPAI_DATA_DIR`` is what :func:`app_registry.user_apps_dir` resolves
    through, so pointing it at tmp_path is the whole isolation. The store dir is
    redirected too (``app_data._data_dir`` caches, so the accessor is replaced),
    because a scaffolded app's document must never land in the checkout.
    """
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(app_data_mod, "_data_dir",
                        lambda: str(tmp_path / "app_data"))
    app_registry.rescan()
    api_server._reset_app_data()
    api_server.app.config["TESTING"] = True
    # Deliberately NOT pinning _get_app_data to one instance: the routes signal a
    # new app by dropping the cache (:func:`api_server._reset_app_data`), which is
    # this tree's invalidation, and a test holding a pinned instance would never
    # see the persona the route just installed.
    return api_server.app.test_client()


# ---------------------------------------------------------------------------
# 1. preview: the words, before anything is written
# ---------------------------------------------------------------------------

def test_preview_shows_the_words_without_writing_anything(bare):
    resp = bare.post("/persona/preview",
                     json={"name": "GardenBot", "goal": GARDEN_GOAL,
                           "lists": ["plantings", "harvest log"]}, headers=AUTH)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()

    assert body["id"] == "gardenbot"
    assert body["lists"] == ["plantings", "harvest_log"]
    assert body["list_labels"] == ["plantings", "harvest log"]
    assert "garden" in body["keywords"] and "plantings" in body["keywords"]
    assert body["route"] == "/dashboard/apps/gardenbot"
    assert body["taken"] is False and body["pack_taken"] is False
    assert not os.path.exists(body["app_path"])
    assert not os.path.exists(body["pack_path"])

    # The preview is the manifest that would be written, word for word.
    written = bare.post("/persona/scaffold",
                        json={"name": "GardenBot", "goal": GARDEN_GOAL,
                              "lists": ["plantings", "harvest log"]},
                        headers=AUTH).get_json()
    assert written["written"] is True
    assert written["persona"]["keywords"] == body["keywords"]
    assert written["persona"]["app_path"] == body["app_path"]
    assert "gardenbot" in written["apps"]


def test_preview_says_when_a_PACK_already_claims_the_id(bare):
    """This build ships a pack called "homebot" and no app called "homebot".

    An id-only ``taken`` would have greyed nothing out and let the owner press
    Create, and then :func:`persona_scaffold.scaffold` would have refused at the
    last step. ``pack_taken`` is why the page can say so first, and
    ``suggested_id`` is the free id it offers instead.
    """
    body = bare.post("/persona/preview", json={"id": "homebot", "name": "HomeBot"},
                     headers=AUTH).get_json()
    assert body["taken"] is False       # no APP holds it
    assert body["pack_taken"] is True   # the shipped pack does
    assert body["suggested_id"] != "homebot"

    refused = bare.post("/persona/scaffold",
                        json={"id": "homebot", "name": "HomeBot"}, headers=AUTH)
    assert refused.status_code == 400
    assert "pack called 'homebot'" in refused.get_json()["error"]


# ---------------------------------------------------------------------------
# 2. scaffold: the same refusals the CLI gives, and nothing left behind
# ---------------------------------------------------------------------------

def test_the_route_refuses_what_the_cli_refuses(bare):
    for payload, fragment in (
        ({"name": "Nope", "id": "Garden Bot"}, "cannot be an app id"),
        ({"name": "Nope", "id": "../evil"}, "cannot contain"),
        ({"name": "x" * 41, "id": "okbot"}, "as long as a name"),
        ({"name": "Nope", "id": "okbot",
          "lists": [f"list{n}" for n in range(9)]}, "9 lists"),
        ({"name": "Nope", "id": "okbot", "lists": ["a" * 33]}, "characters"),
        ({"name": "Nope", "id": "okbot", "lists": ["../etc"]}, "cannot contain"),
        ({"name": "Mine", "id": "calendar"}, "already installed"),
    ):
        resp = bare.post("/persona/scaffold", json=payload, headers=AUTH)
        assert resp.status_code == 400, (payload, resp.get_data(as_text=True))
        body = resp.get_json()
        assert fragment in body["error"], (payload, body["error"])
        assert body["written"] is False
    assert not os.path.isdir(os.path.join(app_registry.user_apps_dir(), "okbot"))
    # And the refusal is the same one the terminal gives, from the same function.
    with pytest.raises(persona_scaffold.ScaffoldError):
        persona_scaffold.validate_request("okbot", "Nope", "", ["../etc"])


def test_preview_refuses_the_same_requests_and_writes_nothing(bare):
    resp = bare.post("/persona/preview", json={"id": "../evil", "name": "Nope"},
                     headers=AUTH)
    assert resp.status_code == 400
    assert resp.get_json()["written"] is False


def test_scaffolding_from_the_route_installs_it_for_chat_at_once(bare):
    """The loop the walk follows: build it in the browser, then talk to it."""
    bare.post("/persona/scaffold",
              json={"name": "GardenBot", "goal": GARDEN_GOAL,
                    "lists": ["plantings"]}, headers=AUTH)

    said = bare.post("/chat/send",
                     json={"message": "add tomatoes to the plantings list"},
                     headers=AUTH).get_json()["response"]
    assert "Added tomatoes to the plantings list" in said
    stored = bare.get("/apps/gardenbot/data", headers=AUTH).get_json()
    assert [row["item"] for row in stored["plantings"]] == ["tomatoes"]


# ---------------------------------------------------------------------------
# 3. list: the owner's own keys, and the one sentence that proves it works
# ---------------------------------------------------------------------------

def test_persona_list_carries_the_lists_and_the_try_line(bare):
    bare.post("/persona/scaffold",
              json={"name": "GardenBot", "lists": ["plantings", "harvest log"]},
              headers=AUTH)
    body = bare.get("/persona/list", headers=AUTH).get_json()

    assert [p["id"] for p in body["personas"]] == ["gardenbot"]
    app = next(a for a in body["apps"] if a["id"] == "gardenbot")
    assert app["lists"] == ["plantings", "harvest_log"]
    assert app["try"] == "add tomatoes to the plantings list"
    assert app["route"] == "/dashboard/apps/gardenbot"
    assert body["user_apps_dir"] == app_registry.user_apps_dir()
    # A shipped app is not a persona and is not listed as one.
    for shipped in ("calendar", "freshtable", "alarmflow", "fitfam", "trivia"):
        assert shipped not in [a["id"] for a in body["apps"]]


def test_the_try_line_is_a_sentence_the_build_actually_answers(bare):
    """The hint is only honest if saying it lands in the store."""
    bare.post("/persona/scaffold", json={"name": "GardenBot",
                                         "lists": ["plantings"]}, headers=AUTH)
    app = next(a for a in bare.get("/persona/list", headers=AUTH).get_json()["apps"]
               if a["id"] == "gardenbot")
    said = bare.post("/chat/send", json={"message": app["try"]},
                     headers=AUTH).get_json()["response"]
    assert "Added tomatoes" in said


# ---------------------------------------------------------------------------
# 4. delete: typed confirmation, and the owner's root only
# ---------------------------------------------------------------------------

def test_delete_needs_the_id_typed_back_and_then_it_is_gone(bare):
    bare.post("/persona/scaffold",
              json={"name": "GardenBot", "lists": ["plantings"]}, headers=AUTH)
    bare.post("/chat/send", json={"message": "add tomatoes to the plantings list"},
              headers=AUTH)
    store = api_server._get_app_data()._files["gardenbot"]
    assert os.path.isfile(store)

    # No confirmation, or the wrong one: nothing happens.
    for body in ({}, {"confirm": "gardenbo"}):
        resp = bare.delete("/persona/gardenbot", json=body, headers=AUTH)
        assert resp.status_code == 400
        assert resp.get_json()["removed"] is False
    assert "gardenbot" in app_registry.installed_ids(
        app_registry.current_manifests())

    resp = bare.delete("/persona/gardenbot", json={"confirm": "gardenbot"},
                       headers=AUTH)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["removed"] is True and body["installed"] is False
    assert not os.path.isdir(
        os.path.join(app_registry.user_apps_dir(), "gardenbot"))
    assert not os.path.isdir(
        os.path.join(app_registry.user_packs_dir(), "gardenbot"))
    assert not os.path.isfile(store)
    assert body["store_removed"] is True


def test_a_shipped_app_cannot_be_deleted_this_way(bare):
    resp = bare.delete("/persona/calendar", json={"confirm": "calendar"},
                       headers=AUTH)
    assert resp.status_code == 403
    assert "not a persona you made" in resp.get_json()["error"]
    assert "calendar" in app_registry.installed_ids(
        app_registry.current_manifests())
    assert persona_scaffold.is_user_persona("calendar") is False


def test_a_path_shaped_id_cannot_reach_anything(bare):
    for name in ("..", "../calendar", "not_here"):
        resp = bare.delete(f"/persona/{name}", json={"confirm": name},
                           headers=AUTH)
        assert resp.status_code in (403, 404), (name, resp.status_code)
    assert os.path.isdir(app_registry.app_dir("calendar"))


# ---------------------------------------------------------------------------
# 5. the honest brainless reply
# ---------------------------------------------------------------------------

def test_a_request_that_really_needs_the_model_says_so_and_how_to_fix_it():
    """A list verb works with no model; a question does not, and says why.

    The fastpath must not be allowed to make a brainless build LOOK complete.
    Anything outside the four verbs needs the language model, and what the owner
    gets is a sentence naming that and the remedy.
    """
    assert api_server._try_app_fastpath("why are my tomatoes not ripening") is None

    for status in (None, 404, 500, 'timeout'):
        message = api_server._brain_failure(status)["message"]
        assert message.startswith(api_server._NEEDS_MODEL_SENTENCE), message
        assert "[Error]" not in message
    assert "puffpai setup --ollama-url" in api_server._brain_failure(None)["message"]
    assert "ollama pull" in api_server._brain_failure(404)["message"]
    assert api_server._brain_failure(None)["code"] == "unreachable"
    assert api_server._brain_failure(404)["code"] == "model_not_installed"


def test_the_chat_paths_no_longer_store_an_error_string_as_the_reply():
    """Both chat legs — streaming and not — must give the honest sentence.

    The streaming one is what the chat page uses first, and it used to yield
    "[Error] Cannot connect to Ollama." straight into the conversation.
    """
    import requests as _requests

    import puffpai.api_server as mod

    def _refuse(*a, **k):
        raise _requests.exceptions.ConnectionError("no ollama here")

    original = mod._ollama_chat
    mod._ollama_chat = _refuse
    try:
        streamed = "".join(mod._stream_ollama_tokens(
            "why are my tomatoes not ripening", [], ""))
        direct = mod._query_ollama_direct(
            "why are my tomatoes not ripening", [], "")
    finally:
        mod._ollama_chat = original

    for reply in (streamed, direct if isinstance(direct, str)
                  else direct.get("response", "")):
        assert reply.startswith(mod._NEEDS_MODEL_SENTENCE), reply
        assert "[Error]" not in reply
        assert "puffpai setup --ollama-url" in reply


# ---------------------------------------------------------------------------
# 6. gate classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda c: c.get("/persona/list"),
    lambda c: c.post("/persona/preview", json={"name": "x"}),
    lambda c: c.post("/persona/scaffold", json={"name": "x"}),
    lambda c: c.delete("/persona/x", json={"confirm": "x"}),
])
def test_every_persona_route_needs_the_api_key(call):
    assert call(api_server.app.test_client()).status_code == 401


def test_no_persona_route_is_public_or_local_exempt():
    """Neither list: the key is required from loopback too, because the caller
    that needs these is the dashboard's own proxy and it carries the key."""
    from puffpai.security import endpoint_gate

    for endpoint in ("persona_preview", "persona_scaffold_route",
                     "persona_list", "persona_delete"):
        assert endpoint not in api_server._PUBLIC_ENDPOINTS, endpoint
        assert endpoint not in endpoint_gate.LOCAL_ONLY_ENDPOINTS, endpoint
        assert endpoint_gate.local_caller_allowed(endpoint, "127.0.0.1") is False
