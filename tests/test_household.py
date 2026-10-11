"""ONE household: name everywhere, avatars, propagation.

What these pin
--------------
* ``/household`` is served from profiles.json, and the OWNER row's placeholder
  ``display_name: "Owner"`` is healed from ``data/profile.json`` on read AND
  written back, so the next reader sees the real name.
* Create/update/delete validate (length, role enum, case-insensitive
  duplicates), refuse to delete the owner, and every one of them propagates.
* ``sync_household_consumers`` rewrites the Calendar's family list without
  orphaning a row an event still references, ADDS FitFam users without
  touching the ones that exist, and adds Trivia players.
* The avatar pipeline: a real PNG in, a square JPEG out; a text file refused;
  an upload id that climbs out of the uploads directory refused.
* The spoken hooks answer from the same functions, with a read-back.

No household data is used anywhere here — every name is a fixture.
"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api                      # noqa: E402
import puffpai.profile_manager as pm                  # noqa: E402
from puffpai import household                         # noqa: E402
from puffpai.agents import household_reads            # noqa: E402

PIL_Image = pytest.importorskip("PIL.Image")


# ── the stores, all redirected into tmp_path ────────────────────────────────

class FakeStore:
    """An app_data stand-in with the real merge semantics."""

    def __init__(self, docs=None):
        self.docs = docs or {}
        self.writes = []

    def get_data(self, app):
        import copy
        return copy.deepcopy(self.docs.get(app, {}))

    def set_data(self, app, data, merge=False, allow_shrink=False, **kw):
        from puffpai import app_data_merge
        current = self.docs.get(app, {})
        if merge:
            drops = app_data_merge.find_drops(current, data)
            if drops and not allow_shrink:
                raise app_data_merge.AppDataWouldDropData(app, drops)
            self.docs[app] = app_data_merge.merge_document(current, data)
        else:
            self.docs[app] = dict(data)
        self.writes.append((app, sorted(data)))
        return True


def _png(width=300, height=200, color=(200, 30, 60)):
    buf = io.BytesIO()
    PIL_Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def bench(tmp_path, monkeypatch):
    """profiles.json, the legacy profile, avatars and uploads, all in tmp."""
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({
        "profiles": [{
            "id": "usr_owner", "display_name": "Owner",
            "avatar_style": "thumbs", "avatar_seed": "Owner",
            "created": "2026-01-01T00:00:00", "last_active": "2026-01-01T00:00:00",
        }],
        "last_active_profile": "usr_owner",
    }), encoding="utf-8")
    monkeypatch.setitem(vars(pm), "_PROFILES_PATH", str(profiles))

    legacy = tmp_path / "profile.json"
    legacy.write_text(json.dumps({"name": "Fixture Owner",
                                  "email": "fixture@example.invalid"}),
                      encoding="utf-8")
    monkeypatch.setattr(api, "_PROFILE_PATH", str(legacy))

    avatars = tmp_path / "avatars"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr(household, "avatars_dir", lambda: avatars)
    monkeypatch.setattr(household, "thumbs_dir", lambda: avatars / "thumbs")
    monkeypatch.setattr(household, "_uploads_dir", lambda: uploads)
    # The SECOND image root: knowledge uploads keep the original
    # here, and it can be the only root with anything in it.
    library = tmp_path / "library_sources"
    library.mkdir()
    monkeypatch.setattr(household, "_library_sources_dir", lambda: library)

    store = FakeStore({
        "calendar": {"family": [{"id": "dad", "name": "Dad", "role": "parent",
                                 "emoji": "\U0001f468", "color": "#38bdf8"},
                                {"id": "emma", "name": "Emma", "role": "child",
                                 "emoji": "\U0001f467", "color": "#fbbf24"}],
                     "events": [{"id": "e1", "title": "Fixture", "member": "emma"}],
                     "tasks": []},
        "fitfam": {"users": {"alex": {"name": "Alex", "age": 40,
                                      "emoji": "\U0001f9d4",
                                      "prefs": {"level": "mid", "type": "full",
                                                "duration": 45,
                                                "program": "fixture_program"},
                                      "goals": [], "history": [{"date": "x"}],
                                      "streak": 3, "totalWorkouts": 9}}},
        "trivia": {"results": []},
    })
    monkeypatch.setattr(household, "_app_data", lambda app=None: store)

    api.app.config["TESTING"] = True
    api._rate_limit_store.clear()
    with api.app.test_client() as client:
        yield client, {"X-API-Key": api._API_KEY}, store, tmp_path


# ── the roster ──────────────────────────────────────────────────────────────

class TestRoster:

    def test_owner_placeholder_is_healed_from_the_legacy_profile(self, bench):
        client, headers, _store, tmp_path = bench
        body = client.get("/household", headers=headers).get_json()
        assert body["owner_id"] == "usr_owner"
        owner = body["members"][0]
        assert owner["display_name"] == "Fixture Owner"
        assert owner["role"] == "owner"
        assert owner["emoji"] and owner["color"]
        assert owner["avatar_url"] is None
        # ...and written BACK, so the next reader needs no heal.
        stored = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
        assert stored["profiles"][0]["display_name"] == "Fixture Owner"
        assert stored["profiles"][0]["role"] == "owner"

    def test_create_returns_the_member_and_propagates(self, bench):
        client, headers, store, _tmp = bench
        resp = client.post("/household", headers=headers,
                           json={"display_name": "Riley", "role": "child"})
        assert resp.status_code == 201
        member = resp.get_json()["member"]
        assert member["id"] == "riley"
        assert member["role"] == "child"
        names = [m["display_name"] for m in
                 client.get("/household", headers=headers).get_json()["members"]]
        assert names == ["Fixture Owner", "Riley"]
        assert "riley" in store.docs["fitfam"]["users"]

    @pytest.mark.parametrize("payload,status,code", [
        ({"display_name": ""}, 400, "invalid_name"),
        ({"display_name": "x" * 41}, 400, "invalid_name"),
        ({"display_name": "Riley", "role": "goldfish"}, 400, "invalid_role"),
        ({"display_name": "Riley", "role": "owner"}, 400, "invalid_role"),
    ])
    def test_create_validation(self, bench, payload, status, code):
        client, headers, _store, _tmp = bench
        resp = client.post("/household", headers=headers, json=payload)
        assert resp.status_code == status
        assert resp.get_json()["code"] == code

    def test_duplicate_names_are_refused_case_insensitively(self, bench):
        client, headers, _store, _tmp = bench
        client.post("/household", headers=headers, json={"display_name": "Riley"})
        resp = client.post("/household", headers=headers,
                           json={"display_name": "riley"})
        assert resp.status_code == 409
        assert resp.get_json()["code"] == "duplicate"

    def test_update_changes_the_row_and_the_calendar(self, bench):
        client, headers, store, _tmp = bench
        client.post("/household", headers=headers, json={"display_name": "Riley"})
        resp = client.put("/household/riley", headers=headers,
                          json={"display_name": "Riley B", "role": "child",
                                "emoji": "\U0001f431"})
        assert resp.status_code == 200
        assert resp.get_json()["member"]["display_name"] == "Riley B"
        family = {row["id"]: row for row in store.docs["calendar"]["family"]}
        assert family["riley"]["name"] == "Riley B"
        assert family["riley"]["role"] == "child"

    def test_update_takes_a_partial_body_and_a_null_clears(self, bench):
        """The dashboard sends ONE field at a time; null is "clear it"."""
        client, headers, _store, _tmp = bench
        client.post("/household", headers=headers,
                    json={"display_name": "Riley", "role": "child",
                          "emoji": "\U0001f431", "color": "#123456"})
        only_color = client.put("/household/riley", headers=headers,
                                json={"color": "#abcdef"}).get_json()["member"]
        assert only_color["display_name"] == "Riley"
        assert only_color["role"] == "child"
        assert only_color["emoji"] == "\U0001f431"
        assert only_color["color"] == "#abcdef"
        cleared = client.put("/household/riley", headers=headers,
                             json={"emoji": None, "color": None}
                             ).get_json()["member"]
        assert cleared["emoji"] == "\U0001f9d2"          # the child default
        assert cleared["color"] and cleared["color"] != "#abcdef"

    def test_every_error_body_carries_an_error_string(self, bench):
        client, headers, _store, _tmp = bench
        for resp in (client.post("/household", headers=headers,
                                 json={"display_name": ""}),
                     client.put("/household/nobody", headers=headers,
                                json={"display_name": "X"}),
                     client.delete("/household/usr_owner", headers=headers),
                     client.get("/household/usr_owner/avatar", headers=headers)):
            assert resp.get_json()["error"], resp.get_json()

    def test_delete_removes_the_member_but_never_the_owner(self, bench):
        client, headers, _store, _tmp = bench
        client.post("/household", headers=headers, json={"display_name": "Riley"})
        assert client.delete("/household/riley", headers=headers).status_code == 200
        refused = client.delete("/household/usr_owner", headers=headers)
        assert refused.status_code == 409
        assert refused.get_json()["code"] == "owner_undeletable"
        assert client.delete("/household/nobody", headers=headers).status_code == 404

    def test_put_profile_renames_the_owner_in_the_roster(self, bench):
        client, headers, _store, tmp_path = bench
        resp = client.put("/profile", headers=headers, json={"name": "Renamed"})
        assert resp.status_code == 200
        assert "avatar_url" in resp.get_json()["profile"]
        stored = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
        assert stored["profiles"][0]["display_name"] == "Renamed"
        assert household.names() == ["Renamed"]

    def test_get_profile_carries_an_avatar_url(self, bench):
        client, headers, _store, _tmp = bench
        assert client.get("/profile", headers=headers).get_json()["profile"][
            "avatar_url"] is None
        client.post("/profile/avatar", headers=headers,
                    data={"file": (io.BytesIO(_png()), "me.png")},
                    content_type="multipart/form-data")
        assert client.get("/profile", headers=headers).get_json()["profile"][
            "avatar_url"] == "/household/usr_owner/avatar"


# ── preferences and the active profile ────────────────────

class TestPreferences:

    def test_household_get_shapes_preferences_and_active_id(self, bench):
        client, headers, _store, _tmp = bench
        body = client.get("/household", headers=headers).get_json()
        assert body["active_id"] == "usr_owner"          # falls back to owner
        assert body["members"][0]["preferences"] == {}

    def test_profile_get_carries_owner_preferences(self, bench):
        client, headers, _store, _tmp = bench
        assert client.get("/profile", headers=headers).get_json()[
            "profile"]["preferences"] == {}
        client.put("/household/usr_owner", headers=headers,
                   json={"preferences": {"theme": "plum"}})
        assert client.get("/profile", headers=headers).get_json()[
            "profile"]["preferences"] == {"theme": "plum"}

    def test_preferences_merge_and_null_removes_a_key(self, bench):
        client, headers, _store, _tmp = bench
        first = client.put("/household/usr_owner", headers=headers,
                           json={"preferences": {"theme": "night"}})
        assert first.status_code == 200
        assert first.get_json()["member"]["preferences"] == {"theme": "night"}
        again = client.get("/household", headers=headers).get_json()
        assert again["members"][0]["preferences"] == {"theme": "night"}
        cleared = client.put("/household/usr_owner", headers=headers,
                             json={"preferences": {"theme": None}})
        assert cleared.get_json()["member"]["preferences"] == {}

    def test_invalid_theme_value_is_a_400(self, bench):
        client, headers, _store, _tmp = bench
        resp = client.put("/household/usr_owner", headers=headers,
                          json={"preferences": {"theme": "chartreuse"}})
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "invalid_preference"

    def test_non_scalar_preference_value_is_a_400(self, bench):
        client, headers, _store, _tmp = bench
        resp = client.put("/household/usr_owner", headers=headers,
                          json={"preferences": {"anything": {"nested": 1}}})
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "invalid_preference"

    def test_unrecognised_scalar_preference_is_accepted_and_ignored(self, bench):
        client, headers, _store, _tmp = bench
        resp = client.put("/household/usr_owner", headers=headers,
                          json={"preferences": {"font_size": "large"}})
        assert resp.status_code == 200
        assert resp.get_json()["member"]["preferences"] == {}

    def test_set_active_updates_get_household_and_last_active(self, bench):
        client, headers, _store, tmp_path = bench
        client.post("/household", headers=headers, json={"display_name": "Riley"})
        resp = client.post("/household/active", headers=headers,
                           json={"id": "riley"})
        assert resp.status_code == 200
        assert resp.get_json()["active_id"] == "riley"
        body = client.get("/household", headers=headers).get_json()
        assert body["active_id"] == "riley"
        stored = json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8"))
        assert stored["last_active_profile"] == "riley"
        riley = next(p for p in stored["profiles"] if p["id"] == "riley")
        assert riley.get("last_active")

    def test_set_active_unknown_id_is_404(self, bench):
        client, headers, _store, _tmp = bench
        resp = client.post("/household/active", headers=headers,
                           json={"id": "nobody"})
        assert resp.status_code == 404
        assert resp.get_json()["code"] == "not_found"


# ── propagation ─────────────────────────────────────────────────────────────

class TestSync:

    def test_calendar_drops_placeholders_but_keeps_referenced_rows(self, bench):
        client, headers, store, _tmp = bench
        client.post("/household", headers=headers, json={"display_name": "Riley"})
        family = store.docs["calendar"]["family"]
        ids = [row["id"] for row in family]
        # "dad" was a placeholder nothing points at; "emma" is on an event.
        assert "dad" not in ids
        assert "emma" in ids
        assert ids[:2] == ["usr_owner", "riley"]
        assert family[0]["name"] == "Fixture Owner"

    def test_fitfam_gains_an_empty_slice_and_loses_nothing(self, bench):
        client, headers, store, _tmp = bench
        client.post("/household", headers=headers, json={"display_name": "Riley"})
        users = store.docs["fitfam"]["users"]
        assert users["alex"]["history"] == [{"date": "x"}]
        assert users["alex"]["streak"] == 3
        fresh = users["riley"]
        assert fresh["name"] == "Riley"
        assert fresh["history"] == [] and fresh["goals"] == []
        assert fresh["streak"] == 0 and fresh["totalWorkouts"] == 0
        assert fresh["prefs"] == {"level": "mid"}
        assert "program" not in fresh["prefs"]

    def test_pets_are_on_the_calendar_but_not_in_fitness_or_trivia(self, bench):
        client, headers, store, _tmp = bench
        client.post("/household", headers=headers,
                    json={"display_name": "Biscuit", "role": "pet"})
        assert "biscuit" in [r["id"] for r in store.docs["calendar"]["family"]]
        assert "biscuit" not in store.docs["fitfam"]["users"]
        assert "Biscuit" not in store.docs["trivia"].get("players", [])

    def test_trivia_players_gain_the_household(self, bench):
        client, headers, store, _tmp = bench
        client.post("/household", headers=headers, json={"display_name": "Riley"})
        assert store.docs["trivia"]["players"] == ["Fixture Owner", "Riley"]
        # Idempotent: a second sync adds nobody twice.
        household.sync_household_consumers()
        assert store.docs["trivia"]["players"] == ["Fixture Owner", "Riley"]

    def test_trivia_dict_store_keeps_stats_and_adds_a_member(self, bench):
        """The live trivia store keys players by id -> per-player stats;
        the sync must never collapse that into a list."""
        client, headers, store, _tmp = bench
        store.docs["trivia"] = {"players": {
            "usr_owner": {"asked": 7, "correct": 5, "level": "hard"},
        }}
        resp = client.post("/household", headers=headers,
                           json={"display_name": "Riley"})
        assert resp.status_code == 201
        players = store.docs["trivia"]["players"]
        assert players["usr_owner"] == {"asked": 7, "correct": 5, "level": "hard"}
        assert players["riley"] == {"name": "Riley", "asked": 0, "correct": 0,
                                    "level": ""}

    def test_trivia_list_store_is_unaffected_by_the_dict_fix(self, bench):
        client, headers, store, _tmp = bench
        client.post("/household", headers=headers, json={"display_name": "Riley"})
        assert store.docs["trivia"]["players"] == ["Fixture Owner", "Riley"]
        # Idempotent: a second sync adds nobody twice.
        household.sync_household_consumers()
        assert store.docs["trivia"]["players"] == ["Fixture Owner", "Riley"]

    def test_trivia_dict_store_member_already_present_by_name_no_change(self, bench):
        client, headers, store, _tmp = bench
        store.docs["trivia"] = {"players": {
            "usr_owner": {"name": "Fixture Owner", "asked": 0, "correct": 0},
            "someone_else": {"name": "Riley", "asked": 3, "correct": 1},
        }}
        before = dict(store.docs["trivia"]["players"])
        resp = client.post("/household", headers=headers,
                           json={"display_name": "Riley"})
        assert resp.status_code == 201
        assert store.docs["trivia"]["players"] == before
        assert "riley" not in store.docs["trivia"]["players"]

    def test_fitfam_key_lookup_prefers_the_profile_id(self, bench):
        client, headers, store, _tmp = bench
        client.post("/household", headers=headers, json={"display_name": "Riley"})
        assert household.fitfam_keys_for("Riley")[0] == "riley"
        assert api._workout_user_key(store, "Riley") == "riley"


# ── avatars ─────────────────────────────────────────────────────────────────

class TestAvatars:

    def test_device_upload_becomes_a_square_jpeg(self, bench):
        client, headers, _store, tmp_path = bench
        resp = client.post("/household/usr_owner/avatar", headers=headers,
                           data={"file": (io.BytesIO(_png()), "me.png")},
                           content_type="multipart/form-data")
        assert resp.status_code == 200
        assert resp.get_json()["decoded"] is True
        stored = tmp_path / "avatars" / "usr_owner.jpg"
        with PIL_Image.open(stored) as img:
            assert img.size == (household.AVATAR_PX, household.AVATAR_PX)
            assert img.format == "JPEG"
        served = client.get("/household/usr_owner/avatar", headers=headers)
        assert served.status_code == 200
        assert served.headers["Cache-Control"] == "no-cache"
        assert served.data[:3] == b"\xff\xd8\xff"
        assert client.get("/household", headers=headers).get_json()[
            "members"][0]["avatar_url"] == "/household/usr_owner/avatar"
        assert client.delete("/household/usr_owner/avatar",
                             headers=headers).status_code == 200
        assert client.get("/household/usr_owner/avatar",
                          headers=headers).status_code == 404

    def test_a_non_image_is_refused(self, bench):
        client, headers, _store, _tmp = bench
        resp = client.post("/household/usr_owner/avatar", headers=headers,
                           data={"file": (io.BytesIO(b"#!/bin/sh\nrm -rf /"),
                                          "payload.png")},
                           content_type="multipart/form-data")
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "not_an_image"

    def test_from_upload_uses_a_file_the_household_sent_to_puff(self, bench):
        client, headers, _store, tmp_path = bench
        (tmp_path / "uploads" / "1700000000000_dog.png").write_bytes(_png())
        listed = client.get("/ingest/images", headers=headers).get_json()
        assert [row["upload_id"] for row in listed] == ["1700000000000_dog.png"]
        assert listed[0]["filename"] == "dog.png"
        assert listed[0]["mime"] == "image/png"
        assert listed[0]["thumb_url"] == "/ingest/images/1700000000000_dog.png/thumb"
        resp = client.post("/household/usr_owner/avatar/from-upload",
                           headers=headers,
                           json={"upload_id": "1700000000000_dog.png"})
        assert resp.status_code == 200
        assert (tmp_path / "avatars" / "usr_owner.jpg").exists()

    def test_thumbs_are_256px_and_cached(self, bench):
        client, headers, _store, tmp_path = bench
        (tmp_path / "uploads" / "1700000000001_cat.png").write_bytes(_png())
        resp = client.get("/ingest/images/1700000000001_cat.png/thumb",
                          headers=headers)
        assert resp.status_code == 200
        with PIL_Image.open(io.BytesIO(resp.data)) as img:
            assert img.size == (household.THUMB_PX, household.THUMB_PX)
        cached = tmp_path / "avatars" / "thumbs" / "1700000000001_cat.png.jpg"
        assert cached.exists()

    @pytest.mark.parametrize("upload_id", [
        "../profile.json", "..%2Fprofile.json", "/etc/passwd",
        "..\\profile.json", "sub/../../profile.json", "",
    ])
    def test_from_upload_refuses_anything_outside_the_uploads_dir(
            self, bench, upload_id):
        client, headers, _store, tmp_path = bench
        resp = client.post("/household/usr_owner/avatar/from-upload",
                           headers=headers, json={"upload_id": upload_id})
        assert resp.status_code in (400, 404), upload_id
        assert not (tmp_path / "avatars" / "usr_owner.jpg").exists()

    def test_from_upload_refuses_a_non_image_upload(self, bench):
        client, headers, _store, tmp_path = bench
        (tmp_path / "uploads" / "1700000000002_notes.txt").write_text("hi")
        resp = client.post("/household/usr_owner/avatar/from-upload",
                           headers=headers,
                           json={"upload_id": "1700000000002_notes.txt"})
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "not_an_image"

    def test_images_list_both_roots_and_dedupe_by_catalog_id(
            self, bench, monkeypatch):
        """the library sources root is listed too, once per picture."""
        client, headers, _store, tmp_path = bench
        (tmp_path / "uploads" / "photo_b_jpeg.jpeg").write_bytes(_png())
        (tmp_path / "library_sources" / "photo_b_jpeg.jpeg").write_bytes(_png())
        (tmp_path / "library_sources" / "photo_a_jpeg.jpeg").write_bytes(_png())
        from puffpai import ingestion
        monkeypatch.setattr(ingestion, "list_ingested_docs", lambda: [
            {"id": "photo_b_jpeg", "filename": "photo_b.jpeg",
             "indexed_at": "2026-08-22T18:10:00"},
            {"id": "photo_a_jpeg", "filename": "photo_a.jpeg",
             "indexed_at": "2026-09-15T18:35:00"},
        ])
        listed = client.get("/ingest/images", headers=headers).get_json()
        ids = [row["upload_id"] for row in listed]
        assert sorted(ids) == ["photo_a_jpeg.jpeg", "photo_b_jpeg.jpeg"]
        by_id = {row["upload_id"]: row for row in listed}
        assert by_id["photo_a_jpeg.jpeg"]["filename"] == "photo_a.jpeg"
        assert by_id["photo_b_jpeg.jpeg"]["filename"] == "photo_b.jpeg"
        assert by_id["photo_a_jpeg.jpeg"]["thumb_url"] == (
            "/ingest/images/photo_a_jpeg.jpeg/thumb")
        # A library-only picture is usable end to end, not just listed.
        assert client.get("/ingest/images/photo_a_jpeg.jpeg/thumb",
                          headers=headers).status_code == 200
        resp = client.post("/household/usr_owner/avatar/from-upload",
                           headers=headers,
                           json={"upload_id": "photo_a_jpeg.jpeg"})
        assert resp.status_code == 200
        assert (tmp_path / "avatars" / "usr_owner.jpg").exists()

    def test_a_non_image_in_library_sources_is_not_listed_or_usable(
            self, bench):
        client, headers, _store, tmp_path = bench
        (tmp_path / "library_sources" / "Master_Sheet_docx.docx").write_text("x")
        listed = client.get("/ingest/images", headers=headers).get_json()
        assert listed == []
        resp = client.post("/household/usr_owner/avatar/from-upload",
                           headers=headers,
                           json={"upload_id": "Master_Sheet_docx.docx"})
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "not_an_image"

    @pytest.mark.parametrize("upload_id", [
        "../profile.json", "../uploads/../profile.json",
        "sub/../../profile.json", "/etc/passwd", "..\\profile.json",
    ])
    def test_traversal_is_refused_against_the_library_root_too(
            self, bench, upload_id):
        """The same climb, tried when BOTH roots exist and are non-empty."""
        client, headers, _store, tmp_path = bench
        (tmp_path / "library_sources" / "real.png").write_bytes(_png())
        (tmp_path / "uploads" / "1700000000003_real.png").write_bytes(_png())
        resp = client.post("/household/usr_owner/avatar/from-upload",
                           headers=headers, json={"upload_id": upload_id})
        assert resp.status_code in (400, 404), upload_id
        assert not (tmp_path / "avatars" / "usr_owner.jpg").exists()
        thumb = client.get(f"/ingest/images/{upload_id}/thumb", headers=headers)
        assert thumb.status_code in (400, 404, 308), upload_id

    def test_profile_avatar_is_the_owner_alias(self, bench):
        client, headers, _store, tmp_path = bench
        assert client.get("/profile/avatar", headers=headers).status_code == 404
        client.post("/profile/avatar", headers=headers,
                    data={"file": (io.BytesIO(_png()), "me.png")},
                    content_type="multipart/form-data")
        assert (tmp_path / "avatars" / "usr_owner.jpg").exists()
        assert client.get("/profile/avatar", headers=headers).status_code == 200
        assert client.delete("/profile/avatar", headers=headers).status_code == 200
        assert client.get("/profile/avatar", headers=headers).status_code == 404


# ── spoken ──────────────────────────────────────────────────────────────────

class TestSpoken:

    def test_add_writes_the_roster_and_reads_it_back(self, bench):
        _client, _headers, store, _tmp = bench
        said = api._try_household_add("add Riley to my family as a child")
        assert said and "Riley" in said
        assert "Fixture Owner, Riley" in said
        assert "riley" in store.docs["fitfam"]["users"]
        assert household.get_member("riley")["role"] == "child"

    @pytest.mark.parametrize("utterance", [
        "add oat milk to the shopping list",
        "add soccer practice to the family calendar",
        "add two more people to my household",
        "what's on the calendar tomorrow",
    ])
    def test_add_declines_everything_that_is_not_a_person(self, bench, utterance):
        assert api._try_household_add(utterance) is None

    def test_who_is_in_my_family_names_the_roster(self, bench):
        _client, _headers, _store, _tmp = bench
        household.create_member("Riley", role="child")
        said = api._try_household_list("who is in my family")
        assert said and "Fixture Owner (owner)" in said and "Riley (child)" in said
        assert household_reads.match("who lives here") == "household_members"

    def test_the_roster_read_declines_an_add(self, bench):
        assert api._try_household_list("add Riley to my family") is None

    def test_both_hooks_are_attributed(self):
        from puffpai import capability_registry, chat_activity
        for name in ("_try_household_add", "_try_household_list"):
            assert chat_activity.FASTPATH_CAPABILITY[name] == "home.household"
        assert capability_registry.get("home.household") is not None
        assert "_try_household_add" in chat_activity.WRITE_FASTPATHS
