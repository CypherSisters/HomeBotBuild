"""Uploads carry a NAME — the household's, or the one Puff wrote.

The owner's ask: "let me name files when I send them, or let Puff
label them and offer, looks like a recipe, save it?". Three halves:

* **Title on upload.** ``POST /knowledge/upload`` honours a ``title`` (or the
  long-standing ``library``) form field. When neither arrives, the classifier
  pass writes the label — ``recipe: Chocolate chip cookies`` — and only then,
  because a name a person typed is never overwritten. The collection id, which
  keys ChromaDB and the retained source file, never changes.
* **Rename.** ``PUT /knowledge/library/<collection_id> {"title": …}`` and its
  ``PUT /ingest/<upload_id>/title`` alias, plus the voice hook that says the
  new name back.
* **The spoken offer.** One announcement per actionable suggestion, answered by
  the next yes or no. NOTHING is written without the yes, and a calendar import
  still goes through the human preview.
"""

import io
import json
import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server as mod                              # noqa: E402
from puffpai import household                                      # noqa: E402
from puffpai import pending_clarifier                              # noqa: E402
from puffpai import suggestions as sug                             # noqa: E402
from puffpai import upload_index                                   # noqa: E402


def _llm(reply):
    return lambda messages: reply


_RECIPE_JSON = '{"kind": "recipe", "confidence": 0.9, "summary": "cookies"}'


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A throwaway suggestion store."""
    monkeypatch.setattr(sug, "_STORE_PATH", str(tmp_path / "suggestions.json"))
    return tmp_path / "suggestions.json"


@pytest.fixture()
def manifest(tmp_path, monkeypatch):
    """A throwaway indexed_libraries.json, with two uploads in it."""
    monkeypatch.setattr(mod, "_SRC_DIR", str(tmp_path))
    entries = [
        {"library": "IMG_4821.jpg", "title": "IMG_4821.jpg",
         "collection": "IMG_4821_jpg", "zone": "knowledge", "chunks": 1,
         "uploaded_at": "2026-09-14T10:00:00+00:00"},
        {"library": "budget.pdf", "title": "budget.pdf", "collection": "budget_pdf",
         "zone": "knowledge", "chunks": 1,
         "uploaded_at": "2026-09-15T10:00:00+00:00"},
    ]
    path = tmp_path / "indexed_libraries.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


@pytest.fixture()
def client():
    mod.app.config["TESTING"] = True
    mod._rate_limit_store.clear()
    with mod.app.test_client() as c:
        yield c, {"X-API-Key": mod._API_KEY}


@pytest.fixture(autouse=True)
def clean_clarifier():
    pending_clarifier.reset()
    yield
    pending_clarifier.reset()


# ===========================================================================
# Puff's own label
# ===========================================================================

class TestAutoTitle:
    def test_kind_and_heading(self):
        assert sug.auto_title(
            "recipe", "Chocolate chip cookies\n2 cups flour",
            "IMG_4821.jpg") == "recipe: Chocolate chip cookies"

    def test_schedule_heading(self):
        assert sug.auto_title(
            "schedule", "Fall soccer\nTuesdays at 5",
            "scan.pdf") == "schedule: Fall soccer"

    def test_general_keeps_the_filename(self):
        assert sug.auto_title("general", "Some words here",
                              "notes.txt") == "notes.txt"

    def test_no_readable_heading_keeps_the_filename(self):
        assert sug.auto_title("recipe", "\n\n---\n12\n", "x.jpg") == "x.jpg"

    def test_heading_is_capped_and_not_cut_mid_word(self):
        title = sug.auto_title("recipe", "A" * 10 + " " + "B" * 60, "f.jpg")
        body = title.split(": ", 1)[1]
        assert len(body) <= sug.TITLE_MAX
        assert body == "A" * 10

    def test_leading_blank_and_symbol_lines_are_skipped(self):
        assert sug.auto_title(
            "recipe", "\n***\n  \nChocolate chip cookies\n",
            "f.jpg") == "recipe: Chocolate chip cookies"


class TestSuggestionRecordCarriesTheTitle:
    def test_explicit_title_wins_over_the_label(self, store):
        record = sug.build_suggestion(
            "IMG_4821.jpg", "Chocolate chip cookies\nflour",
            collection="IMG_4821_jpg", llm_chat=_llm(_RECIPE_JSON),
            title="Grandma's cookies", upload_id="IMG_4821_jpg")
        assert record["title"] == "Grandma's cookies"
        assert record["upload_id"] == "IMG_4821_jpg"
        assert record["offered_at"] is None

    def test_label_is_written_when_nobody_named_it(self, store, monkeypatch):
        monkeypatch.setattr("puffpai.recipe_ingest.ingest_text",
                            lambda f, t, llm_chat=None: {
                                "method": "llm", "recipes": [], "notes": []})
        record = sug.build_suggestion(
            "IMG_4821.jpg", "Chocolate chip cookies\nflour",
            collection="IMG_4821_jpg", llm_chat=_llm(_RECIPE_JSON))
        assert record["title"] == "recipe: Chocolate chip cookies"

    def test_display_title_drops_the_kind_prefix_for_speech(self):
        assert sug.display_title(
            {"kind": "recipe",
             "title": "recipe: Chocolate chip cookies"}
        ) == "Chocolate chip cookies"


# ===========================================================================
# Title on upload
# ===========================================================================

class TestUploadTitle:
    @pytest.fixture()
    def upload_env(self, client, store, manifest, tmp_path, monkeypatch):
        c, headers = client
        monkeypatch.setattr(mod, "_LIBRARY_SOURCES_DIR",
                            str(tmp_path / "sources"))
        monkeypatch.setattr(mod, "_get_query_embedder",
                            lambda: (lambda chunks: [[0.1]] * len(chunks)))
        collection = MagicMock()
        collection.count.return_value = 99
        monkeypatch.setattr(mod, "_get_dual_zone_rag", lambda: MagicMock())
        monkeypatch.setattr("puffpai.ingestion.get_or_create_upload_collection",
                            lambda *a, **kw: collection)
        captured = {}
        monkeypatch.setattr(sug, "queue_suggestion_async",
                            lambda **kw: captured.update(kw))
        return c, headers, captured, tmp_path

    def _post(self, c, headers, **fields):
        data = {"file": (io.BytesIO(b"Chocolate chip cookies\n2 cups flour"),
                         "IMG_4821.txt")}
        data.update(fields)
        return c.post("/knowledge/upload", headers=headers,
                      content_type="multipart/form-data", data=data)

    def _entry(self, tmp_path, collection):
        entries = json.loads(
            (tmp_path / "indexed_libraries.json").read_text(encoding="utf-8"))
        return next(e for e in entries if e["collection"] == collection)

    def test_title_field_is_honoured(self, upload_env):
        c, headers, captured, tmp_path = upload_env
        resp = self._post(c, headers, title="Grandma's cookies")
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "Grandma's cookies"
        collection = resp.get_json()["collection"]
        entry = self._entry(tmp_path, collection)
        assert entry["title"] == "Grandma's cookies"
        # The household named it, so the classifier must not relabel it.
        assert captured["title"] == "Grandma's cookies"

    def test_no_field_titles_by_filename_and_leaves_the_label_to_puff(
            self, upload_env):
        c, headers, captured, tmp_path = upload_env
        resp = self._post(c, headers)
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "IMG_4821.txt"
        assert self._entry(tmp_path, "IMG_4821_txt")["title"] == "IMG_4821.txt"
        assert captured["title"] == ""
        assert callable(captured["on_title"])

    def test_the_label_lands_in_the_manifest(self, upload_env):
        c, headers, captured, tmp_path = upload_env
        self._post(c, headers)
        captured["on_title"]("recipe: Chocolate chip cookies")
        assert (self._entry(tmp_path, "IMG_4821_txt")["title"]
                == "recipe: Chocolate chip cookies")

    def test_library_field_still_names_the_collection(self, upload_env):
        c, headers, _captured, tmp_path = upload_env
        resp = self._post(c, headers, library="Fall class schedule")
        assert resp.get_json()["collection"] == "Fall_class_schedule"
        entry = self._entry(tmp_path, "Fall_class_schedule")
        assert entry["title"] == "Fall class schedule"


# ===========================================================================
# The catalog shows the title
# ===========================================================================

class TestCatalogTitles:
    def test_library_docs_carry_the_title(self, manifest):
        (Path(manifest).write_text(json.dumps([
            {"library": "IMG_4821.jpg", "title": "recipe: Cookies",
             "collection": "IMG_4821_jpg", "uploaded_at": "2026-09-15T10:00:00"},
        ]), encoding="utf-8"))
        docs = upload_index.uploaded_library_docs()
        assert docs[0]["filename"] == "IMG_4821.jpg"
        assert docs[0]["title"] == "recipe: Cookies"

    def test_every_listed_doc_has_a_title(self, manifest, monkeypatch):
        monkeypatch.setattr("puffpai.ingestion.list_ingested_docs",
                            lambda: [{"id": "a1", "filename": "notes.txt"}])
        docs = upload_index.all_uploaded_docs(index_docs={})
        assert all(d["title"] for d in docs)
        assert next(d for d in docs if d["id"] == "a1")["title"] == "notes.txt"

    def test_ingest_list_route_reports_titles(self, client, manifest,
                                              monkeypatch):
        c, headers = client
        monkeypatch.setattr("puffpai.ingestion.list_ingested_docs", lambda: [])
        monkeypatch.setattr(upload_index, "upload_index_snapshot",
                            lambda: (0, {}))
        docs = c.get("/ingest/list", headers=headers).get_json()["documents"]
        assert {d["title"] for d in docs} == {"IMG_4821.jpg", "budget.pdf"}

    def test_image_rows_show_the_title(self, tmp_path, monkeypatch):
        root = tmp_path / "sources"
        root.mkdir()
        (root / "IMG_4821_jpg.jpg").write_bytes(b"x")
        monkeypatch.setattr(household, "_image_roots", lambda: [root])
        monkeypatch.setattr(household, "_upload_catalog", lambda: {
            "IMG_4821_jpg": {"filename": "IMG_4821.jpg",
                             "title": "recipe: Cookies"}})
        rows = household.upload_images()
        assert rows[0]["filename"] == "IMG_4821.jpg"
        assert rows[0]["title"] == "recipe: Cookies"


# ===========================================================================
# Rename
# ===========================================================================

class TestRenameRoute:
    def test_renames_by_collection_id(self, client, manifest):
        c, headers = client
        resp = c.put("/knowledge/library/budget_pdf",
                     json={"title": "Fall soccer"}, headers=headers)
        assert resp.status_code == 200
        doc = resp.get_json()["document"]
        assert doc["title"] == "Fall soccer"
        # The collection id and the filename are untouched.
        assert doc["collection"] == "budget_pdf" and doc["filename"] == "budget.pdf"
        entries = json.loads(Path(manifest).read_text(encoding="utf-8"))
        assert [e for e in entries if e["collection"] == "budget_pdf"][0][
            "title"] == "Fall soccer"

    def test_ingest_alias_renames_the_same_document(self, client, manifest):
        c, headers = client
        resp = c.put("/ingest/IMG_4821_jpg.jpg/title",
                     json={"title": "Cookies"}, headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["document"]["collection"] == "IMG_4821_jpg"

    @pytest.mark.parametrize("title", ["", "   ", "x" * 81])
    def test_bad_titles_are_refused(self, client, manifest, title):
        c, headers = client
        resp = c.put("/knowledge/library/budget_pdf", json={"title": title},
                     headers=headers)
        assert resp.status_code == 400

    def test_unknown_document_is_404(self, client, manifest):
        c, headers = client
        resp = c.put("/knowledge/library/nope", json={"title": "x"},
                     headers=headers)
        assert resp.status_code == 404

    def test_requires_the_api_key(self, client, manifest):
        c, _headers = client
        assert c.put("/knowledge/library/budget_pdf",
                     json={"title": "x"}).status_code == 401

    def test_delete_still_routes(self, client, manifest, monkeypatch):
        """The PUT rule must not swallow the pre-existing DELETE."""
        c, headers = client
        monkeypatch.setattr(mod, "_get_dual_zone_rag", lambda: MagicMock())
        assert c.delete("/knowledge/library/budget.pdf",
                        headers=headers).status_code == 200


class TestRenameByVoice:
    def test_names_the_newest_upload(self, manifest):
        reply = mod._try_rename_upload("call that picture Fall soccer")
        assert "'Fall soccer' now" in reply
        entries = json.loads(Path(manifest).read_text(encoding="utf-8"))
        newest = [e for e in entries if e["collection"] == "budget_pdf"][0]
        assert newest["title"] == "Fall soccer"

    def test_read_back_names_the_original_file(self, manifest):
        reply = mod._try_rename_upload("call that photo 'Cookies'")
        assert "'Cookies'" in reply and "budget.pdf" in reply

    def test_quoted_name_without_a_noun(self, manifest):
        assert "'Cookies'" in mod._try_rename_upload("call that 'Cookies'")

    def test_rename_x_to_y_targets_the_named_document(self, manifest):
        reply = mod._try_rename_upload("rename IMG_4821.jpg to Cookies")
        assert "Cookies" in reply
        entries = json.loads(Path(manifest).read_text(encoding="utf-8"))
        older = [e for e in entries if e["collection"] == "IMG_4821_jpg"][0]
        assert older["title"] == "Cookies"
        assert [e for e in entries
                if e["collection"] == "budget_pdf"][0]["title"] == "budget.pdf"

    def test_unknown_target_renames_nothing(self, manifest):
        reply = mod._try_rename_upload("rename the tax return to Taxes")
        assert "don't have anything called" in reply
        entries = json.loads(Path(manifest).read_text(encoding="utf-8"))
        assert {e["title"] for e in entries} == {"IMG_4821.jpg", "budget.pdf"}

    def test_nothing_to_name_yet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "_SRC_DIR", str(tmp_path))
        assert "haven't sent me anything" in mod._try_rename_upload(
            "call that picture Cookies")

    @pytest.mark.parametrize("said", [
        "name that tune",
        "call mom",
        "what did you call that",
        "call the vet about the dog",
        "rename",
    ])
    def test_declines_everything_that_is_not_a_rename(self, manifest, said):
        assert mod._try_rename_upload(said) is None


# ===========================================================================
# The latest upload reads back by name
# ===========================================================================

class TestLatestUploadReadsBackTheTitle:
    @pytest.fixture()
    def one_upload(self, store, monkeypatch):
        monkeypatch.setattr(
            "puffpai.document_lookup._load_index",
            lambda: [{"id": "IMG_4821_jpg", "filename": "IMG_4821.jpg",
                      "title": "schedule: Fall soccer",
                      "indexed_at": "2026-09-15T10:00:00"}])
        monkeypatch.setattr("puffpai.document_lookup.text_of",
                            lambda doc: "Fall soccer. Tuesdays at five.")
        sug.add({"id": "s1", "status": "pending", "kind": "schedule",
                 "collection": "IMG_4821_jpg", "upload_id": "IMG_4821_jpg",
                 "doc_name": "IMG_4821.jpg",
                 "title": "schedule: Fall soccer",
                 "created_at": "2026-09-15T10:00:00+00:00",
                 "expires_at": "2099-01-01T00:00:00+00:00",
                 "action": {"type": "none", "params": {}}})

    def test_title_and_kind_are_spoken(self, one_upload):
        reply = mod._try_latest_upload_read(
            "what's in the document I just sent you")
        assert "'schedule: Fall soccer'" in reply
        assert "a schedule" in reply
        assert "IMG_4821.jpg" in reply

    def test_what_did_you_find_answers_with_the_gist(self, one_upload):
        reply = mod._try_latest_upload_read(
            "what did you find in the last thing I sent you")
        assert reply is not None
        assert "a schedule" in reply and "Here's the gist" in reply
        assert "Fall soccer" in reply

    def test_unclassified_upload_claims_no_kind(self, store, monkeypatch):
        monkeypatch.setattr(
            "puffpai.document_lookup._load_index",
            lambda: [{"id": "n1", "filename": "notes.txt", "title": "notes.txt",
                      "indexed_at": "2026-09-15T10:00:00"}])
        monkeypatch.setattr("puffpai.document_lookup.text_of",
                            lambda doc: "Some words.")
        reply = mod._try_latest_upload_read("what's in the file I just sent")
        assert "notes.txt" in reply and " — a " not in reply


# ===========================================================================
# The spoken offer
# ===========================================================================

@pytest.fixture()
def recipe_offer(store, monkeypatch):
    """One pending recipe suggestion, already spoken."""
    monkeypatch.setattr("puffpai.recipe_ingest.ingest_text",
                        lambda f, t, llm_chat=None: {
                            "method": "llm",
                            "recipes": [{"name": "Chocolate chip cookies",
                                         "ingredients": ["flour", "sugar"],
                                         "instructions": "Bake."}],
                            "notes": []})
    said = []
    record = sug.queue_suggestion(
        "IMG_4821.jpg", "Chocolate chip cookies\nflour",
        collection="IMG_4821_jpg", upload_id="IMG_4821_jpg",
        llm_chat=_llm(_RECIPE_JSON), speak=True,
        announce_fn=said.append)
    return record, said


class TestSpokenOffer:
    def test_offer_is_spoken_once(self, recipe_offer):
        record, said = recipe_offer
        assert said == ["That looks like a recipe — Chocolate chip cookies. "
                        "Want me to save it to your recipes?"]
        # A second pass over the same record says nothing more.
        assert sug.speak_offer(sug.get(record["id"]),
                               announce_fn=said.append) is False
        assert len(said) == 1

    def test_a_general_document_is_never_spoken(self, store):
        said = []
        sug.queue_suggestion("notes.txt", "Some words", llm_chat=_llm("nope"),
                             speak=True, announce_fn=said.append)
        assert said == []

    def test_speaking_is_off_unless_the_upload_path_asks(self, store,
                                                         monkeypatch):
        monkeypatch.setattr("puffpai.recipe_ingest.ingest_text",
                            lambda f, t, llm_chat=None: {
                                "method": "llm",
                                "recipes": [{"name": "Cookies",
                                             "ingredients": ["flour"],
                                             "instructions": "Bake."}],
                                "notes": []})
        said = []
        record = sug.queue_suggestion("x.jpg", "Cookies\nflour",
                                      llm_chat=_llm(_RECIPE_JSON),
                                      announce_fn=said.append)
        assert said == [] and record["offered_at"] is None

    def test_pending_record_shape_is_what_the_dashboard_codes_against(
            self, recipe_offer, client):
        c, headers = client
        pending = c.get("/knowledge/suggestions/pending",
                        headers=headers).get_json()["suggestions"]
        assert len(pending) == 1
        row = pending[0]
        for field in ("id", "kind", "title", "upload_id", "action",
                      "message", "status", "offered_at"):
            assert field in row
        assert row["kind"] == "recipe"
        assert row["action"]["type"] == "recipe_import"


class TestOfferReply:
    def test_yes_saves_the_recipe(self, recipe_offer, monkeypatch):
        record, _said = recipe_offer
        app = MagicMock()
        monkeypatch.setattr(mod, "_get_app_data", lambda: app)
        reply = mod._try_upload_offer_reply("yes, save it", "conv-1")
        assert "Chocolate chip cookies" in reply
        app.fresh_create_recipe.assert_called_once_with(
            "Chocolate chip cookies", ["flour", "sugar"], "Bake.")
        assert sug.get(record["id"])["status"] == "accepted"

    def test_no_dismisses_and_writes_nothing(self, recipe_offer, monkeypatch):
        record, _said = recipe_offer
        app = MagicMock()
        monkeypatch.setattr(mod, "_get_app_data", lambda: app)
        reply = mod._try_upload_offer_reply("no thanks", "conv-1")
        assert "still in your library" in reply
        app.fresh_create_recipe.assert_not_called()
        assert sug.get(record["id"])["status"] == "dismissed"

    def test_an_unrelated_turn_writes_nothing(self, recipe_offer, monkeypatch):
        record, _said = recipe_offer
        app = MagicMock()
        monkeypatch.setattr(mod, "_get_app_data", lambda: app)
        assert mod._try_upload_offer_reply(
            "what's the weather tomorrow", "conv-1") is None
        app.fresh_create_recipe.assert_not_called()
        assert sug.get(record["id"])["status"] == "pending"

    def test_a_yes_with_no_offer_outstanding_does_nothing(self, store,
                                                          monkeypatch):
        app = MagicMock()
        monkeypatch.setattr(mod, "_get_app_data", lambda: app)
        assert mod._try_upload_offer_reply("yes", "conv-1") is None
        app.fresh_create_recipe.assert_not_called()

    def test_the_offer_is_single_use(self, recipe_offer, monkeypatch):
        _record, _said = recipe_offer
        monkeypatch.setattr(mod, "_get_app_data", lambda: MagicMock())
        assert mod._try_upload_offer_reply("yes", "conv-1") is not None
        assert mod._try_upload_offer_reply("yes", "conv-1") is None

    def test_a_stale_offer_is_not_answerable(self, recipe_offer, monkeypatch):
        record, _said = recipe_offer
        monkeypatch.setattr(sug, "OFFER_TTL_SECONDS", -1.0)
        monkeypatch.setattr(mod, "_get_app_data", lambda: MagicMock())
        assert mod._try_upload_offer_reply("yes", "conv-1") is None
        assert sug.get(record["id"])["status"] == "pending"

    def test_an_open_question_still_wins(self, recipe_offer, monkeypatch):
        """A clarifier the household was ASKED outranks the unprompted offer."""
        monkeypatch.setattr(mod, "_get_app_data", lambda: MagicMock())
        pending_clarifier.register(
            "med_schedule", lambda chosen, said: "Noted the dose.",
            conversation_id="conv-1")
        assert mod._try_pending_clarifier("yes", "conv-1") == "Noted the dose."


class TestCalendarOfferKeepsThePreview:
    @pytest.fixture()
    def schedule_offer(self, store, monkeypatch):
        monkeypatch.setattr("puffpai.calendar_ingest.ingest_text",
                            lambda f, t, llm_chat=None: {
                                "method": "llm", "notes": [],
                                "events": [{"title": "Soccer", "date": "2026-10-01"},
                                           {"title": "Soccer", "date": "2026-10-08"}]})
        said = []
        record = sug.queue_suggestion(
            "budget.pdf", "Fall soccer\nTuesdays",
            collection="budget_pdf", upload_id="budget_pdf",
            llm_chat=_llm('{"kind":"schedule","confidence":0.9}'),
            speak=True, announce_fn=said.append)
        return record, said

    def test_offer_names_the_event_count(self, schedule_offer):
        _record, said = schedule_offer
        assert said == ["That looks like a schedule — Fall soccer, with "
                        "2 events. Want me to put them on your calendar?"]

    def test_yes_opens_the_preview_and_writes_nothing(self, schedule_offer,
                                                      monkeypatch):
        record, _said = schedule_offer
        shown = []
        monkeypatch.setattr("puffpai.dashboard_display.show_query",
                            lambda query, source="voice": shown.append(query))
        app = MagicMock()
        monkeypatch.setattr(mod, "_get_app_data", lambda: app)
        reply = mod._try_upload_offer_reply("yes please", "conv-1")
        assert "on the screen to confirm" in reply and "2 events" in reply
        assert shown == ["show me my calendar"]
        # Nothing was written, and the card the preview needs is still pending.
        app.fresh_set_meal.assert_not_called()
        assert sug.get(record["id"])["status"] == "pending"
        # ...but the spoken offer is spent, so a later "yes" does not repeat it.
        assert mod._try_upload_offer_reply("yes", "conv-1") is None


# ===========================================================================
# Attribution
# ===========================================================================

def test_the_new_hooks_are_attributed():
    from puffpai import chat_activity
    for name in ("_try_rename_upload", "_try_upload_offer_reply"):
        assert chat_activity.FASTPATH_CAPABILITY[name] == "knowledge.documents"
