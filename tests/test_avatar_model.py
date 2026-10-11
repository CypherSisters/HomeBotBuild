"""Bring your own avatar: the .glb upload, storage and serve-back.

What these pin
--------------
* A real, minimal glTF 2.0 binary (one triangle, hand-built below — no fixture
  binary in the repo, same reason no avatar model ships) uploads, is stored
  under the DATA DIR (not in the dashboard checkout), and comes back byte-for-
  byte from the GET route with the glTF mime type.
* Uploading POINTS the avatar at the model: ai_settings.avatar_3d_url becomes
  avatar_model.MODEL_URL. Deleting clears it again.
* The file is on disk, so it survives a restart — proven here by reading it
  through a FRESH resolution of the path, with the route's in-process state
  irrelevant.
* Anything that is not a .glb is refused with a plain message: a PNG, a text
  file, a truncated GLB, a glTF 1.0 GLB, and a .gltf filename.
* Absolute ``avatar_3d_url`` values are house-local only (the public-URL case
  lives in test_api_server.TestAvatar3dUrl).

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

import io
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api                      # noqa: E402
from puffpai import avatar_model                      # noqa: E402


# ── the fixture: a valid one-triangle GLB, built here ───────────────────────

def minimal_glb() -> bytes:
    """A spec-valid glTF 2.0 binary with one triangle. Under 2 KB.

    Built by hand rather than shipped as a binary: no model binary lives in
    this repository.
    """
    # 3 vertices, float32 xyz = 36 bytes, padded to a 4-byte boundary.
    verts = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    bin_chunk = verts + b"\x00" * ((4 - len(verts) % 4) % 4)
    gltf = {
        "asset": {"version": "2.0", "generator": "puffpai test fixture"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "Triangle"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "accessors": [{
            "bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
            "min": [0, 0, 0], "max": [1, 1, 0],
        }],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36}],
        "buffers": [{"byteLength": len(bin_chunk)}],
    }
    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)   # pad with spaces
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_chunk)
    out = struct.pack("<4sII", b"glTF", 2, total)
    out += struct.pack("<I4s", len(json_bytes), b"JSON") + json_bytes
    out += struct.pack("<I4s", len(bin_chunk), b"BIN\x00") + bin_chunk
    return out


def _png() -> bytes:
    """A tiny real PNG — the "wrong file, right confidence" case."""
    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def bench(tmp_path, monkeypatch):
    """The model dir and ai_settings.json, both redirected into tmp_path."""
    models = tmp_path / "avatar_models"
    monkeypatch.setattr(avatar_model, "models_dir", lambda: models)

    settings = tmp_path / "ai_settings.json"
    monkeypatch.setattr(api, "_AI_SETTINGS_PATH", str(settings))

    api.app.config["TESTING"] = True
    api._rate_limit_store.clear()
    with api.app.test_client() as client:
        yield client, {"X-API-Key": api._API_KEY}, models, settings


def _upload(client, headers, data, filename="avatar.glb"):
    return client.post("/profile/avatar-model", headers=headers,
                       data={"file": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data")


def _stored_url(client, headers):
    res = client.get("/profile/ai", headers=headers)
    return res.get_json()["settings"].get("avatar_3d_url")


# ── the container check, on its own ──────────────────────────────────────────

class TestSniff:
    def test_the_fixture_is_a_glb(self):
        glb = minimal_glb()
        assert len(glb) <= 2048
        assert avatar_model.sniff_glb(glb)

    @pytest.mark.parametrize("bad", [
        b"", b"glTF", b"not a model at all, just prose about one",
        b"PK\x03\x04" + b"\x00" * 64,
    ])
    def test_rubbish_is_not_a_glb(self, bad):
        assert not avatar_model.sniff_glb(bad)

    def test_truncated_glb_is_refused(self):
        glb = minimal_glb()
        assert not avatar_model.sniff_glb(glb[:-8])

    def test_gltf_version_1_is_refused(self):
        glb = bytearray(minimal_glb())
        glb[4:8] = struct.pack("<I", 1)
        assert not avatar_model.sniff_glb(bytes(glb))

    def test_second_chunk_first_is_refused(self):
        """A file whose first chunk is BIN, not JSON, is not a loadable GLB."""
        glb = bytearray(minimal_glb())
        glb[16:20] = b"BIN\x00"
        assert not avatar_model.sniff_glb(bytes(glb))


# ── the route, end to end ───────────────────────────────────────────────────

class TestUpload:
    def test_upload_then_get_round_trips_the_bytes(self, bench):
        client, headers, models, _ = bench
        glb = minimal_glb()
        res = _upload(client, headers, glb)
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["success"] is True
        assert body["bytes"] == len(glb)
        assert body["model_url"] == avatar_model.MODEL_URL

        # Stored under the DATA dir, not in the dashboard checkout.
        assert (models / avatar_model.MODEL_FILENAME).read_bytes() == glb

        got = client.get("/profile/avatar-model", headers=headers)
        assert got.status_code == 200
        assert got.mimetype == "model/gltf-binary"
        assert got.get_data() == glb

    def test_upload_points_the_avatar_at_the_model(self, bench):
        client, headers, _, _ = bench
        _upload(client, headers, minimal_glb())
        assert _stored_url(client, headers) == avatar_model.MODEL_URL

    def test_the_pointed_url_is_one_the_settings_validator_accepts(self):
        """Belt and braces: the URL we store must survive PUT /profile/ai."""
        assert api._valid_avatar_url(avatar_model.MODEL_URL)

    def test_replacing_the_model_overwrites_rather_than_accumulates(self, bench):
        client, headers, models, _ = bench
        _upload(client, headers, minimal_glb())
        bigger = minimal_glb() + b""
        _upload(client, headers, bigger)
        files = sorted(p.name for p in models.iterdir())
        assert files == [avatar_model.MODEL_FILENAME]

    def test_it_survives_a_restart(self, bench):
        """The bytes are on disk: a fresh path resolution still finds them."""
        client, headers, models, _ = bench
        glb = minimal_glb()
        _upload(client, headers, glb)
        # Nothing in-process is consulted here — this is what a new interpreter
        # would do on boot.
        assert avatar_model.read_model() == glb
        assert avatar_model.has_model()

    def test_delete_removes_the_file_and_unpoints_the_avatar(self, bench):
        client, headers, models, _ = bench
        _upload(client, headers, minimal_glb())
        res = client.delete("/profile/avatar-model", headers=headers)
        assert res.status_code == 200
        assert res.get_json()["removed"] is True
        assert not (models / avatar_model.MODEL_FILENAME).exists()
        assert _stored_url(client, headers) == ""
        assert client.get("/profile/avatar-model",
                          headers=headers).status_code == 404

    def test_get_with_no_model_is_a_plain_404(self, bench):
        client, headers, _, _ = bench
        res = client.get("/profile/avatar-model", headers=headers)
        assert res.status_code == 404
        assert res.get_json()["code"] == "no_model"
        assert "No avatar model" in res.get_json()["message"]

    def test_delete_with_no_model_is_not_an_error(self, bench):
        client, headers, _, _ = bench
        res = client.delete("/profile/avatar-model", headers=headers)
        assert res.status_code == 200
        assert res.get_json()["removed"] is False


class TestRefusals:
    def test_a_png_named_glb_is_refused_with_a_plain_message(self, bench):
        client, headers, models, _ = bench
        res = _upload(client, headers, _png(), "face.glb")
        assert res.status_code == 400
        body = res.get_json()
        assert body["code"] == "not_a_glb"
        assert "glTF Binary" in body["message"]
        assert not (models / avatar_model.MODEL_FILENAME).exists()

    def test_a_text_file_is_refused(self, bench):
        client, headers, _, _ = bench
        res = _upload(client, headers, b"this is my avatar, honest", "a.glb")
        assert res.status_code == 400
        assert res.get_json()["code"] == "not_a_glb"

    def test_a_gltf_filename_is_refused_before_it_is_read(self, bench):
        client, headers, _, _ = bench
        res = _upload(client, headers, minimal_glb(), "avatar.gltf")
        assert res.status_code == 400
        assert res.get_json()["code"] == "not_a_glb"
        assert ".glb" in res.get_json()["message"]

    def test_no_file_field_is_a_400(self, bench):
        client, headers, _, _ = bench
        res = client.post("/profile/avatar-model", headers=headers, data={},
                          content_type="multipart/form-data")
        assert res.status_code == 400
        assert res.get_json()["code"] == "no_file"

    def test_a_refused_upload_does_not_change_the_pointer(self, bench):
        client, headers, _, _ = bench
        _upload(client, headers, minimal_glb())
        _upload(client, headers, b"nope", "x.glb")
        assert _stored_url(client, headers) == avatar_model.MODEL_URL

    def test_oversize_is_refused_by_size_before_sniffing(self, bench, monkeypatch):
        client, headers, models, _ = bench
        monkeypatch.setattr(avatar_model, "MODEL_MAX_BYTES", 1024)
        res = _upload(client, headers, b"\x00" * 2048)
        assert res.status_code == 413
        assert res.get_json()["code"] == "too_large"
        assert not (models / avatar_model.MODEL_FILENAME).exists()


class TestHouseLocalUrls:
    """The paste-a-URL path is kept, restricted to this house."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8080/model.glb",
        "http://192.168.1.50:8080/models/puff.glb",
        "http://10.1.2.3/a.glb",
        "/api/pai/profile/avatar-model",
    ])
    def test_house_local_is_accepted(self, url):
        assert api._valid_avatar_url(url)

    @pytest.mark.parametrize("url", [
        "https://models.example.com/a.glb",
        "http://8.8.8.8/a.glb",
        "https://readyplayer.me/avatar.glb",
    ])
    def test_off_house_is_refused(self, url):
        assert not api._valid_avatar_url(url)
