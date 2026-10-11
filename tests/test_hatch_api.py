"""Tests for the hatch/certificate HTTP surface (puffpai.hatch_api).

Registered onto a bare Flask app — the 12k-line api_server module is not
imported. In production the routes live on that app and inherit its API-key
gate + rate limiter (they are not in _PUBLIC_ENDPOINTS); the gate itself is
covered by test_api_server.py.
"""

import pytest
from flask import Flask

import puffpai.hatch as hatch
import puffpai.hatch_api as hatch_api
import puffpai.certificate as certificate


GOOD_RESULT = {
    "replay": False,
    "name": "Nimbus",
    "hatched_at": "2026-08-15T12:00:00+00:00",
    "hatch_date": "2026-08-15",
    "replays": 0,
    "script": [{"id": "intro", "text": "Hi. I'm Nimbus. I live here now."}],
    "ask_line": "I'm awake. What's my name?",
    "preflight": {"ok": True, "blocking": [], "checks": []},
}


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    renames = []
    hatch_api.register(app, on_name_change=renames.append)
    with app.test_client() as c:
        yield c, renames


class TestStatus:
    def test_unhatched(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setattr(hatch, "load_state", lambda: None)
        resp = c.get("/hatch/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["hatched"] is False
        assert data["name"] is None
        assert data["ask_line"]
        assert "svg" in data["certificate_formats"]

    def test_hatched(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setattr(hatch, "load_state", lambda: {
            "name": "Nimbus", "hatched_at": "2026-08-15T12:00:00+00:00",
            "hatch_date": "2026-08-15", "replays": 3})
        data = c.get("/hatch/status").get_json()
        assert data["hatched"] is True
        assert data["name"] == "Nimbus"
        assert data["replays"] == 3


class TestPreflightRoute:
    def test_returns_probe_results(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setattr(hatch, "run_preflight",
                            lambda **kw: {"ok": True, "blocking": [],
                                          "checks": [], "kw": kw})
        data = c.get("/hatch/preflight").get_json()
        assert data["ok"] is True
        assert data["kw"]["require_voice"] is True

    def test_require_voice_can_be_disabled(self, client, monkeypatch):
        c, _ = client
        seen = {}
        monkeypatch.setattr(
            hatch, "run_preflight",
            lambda **kw: seen.update(kw) or {"ok": True, "blocking": [],
                                             "checks": []})
        c.get("/hatch/preflight?require_voice=0")
        assert seen["require_voice"] is False


class TestHatchRoute:
    def test_refusal_is_503_free_plain_and_structured(self, client,
                                                      monkeypatch):
        c, renames = client
        blocking = [{"id": "voice_tts", "label": "local voice (Piper)",
                     "status": "fail", "detail": "model missing",
                     "blocking": True}]

        def refuse(*a, **kw):
            raise hatch.HatchRefused(blocking)
        monkeypatch.setattr(hatch, "perform_hatch", refuse)
        monkeypatch.setattr(hatch, "is_hatched", lambda: False)

        resp = c.post("/hatch", json={"name": "Nimbus"})
        assert resp.status_code == 409
        data = resp.get_json()
        assert data["refused"] is True
        assert data["blocking"] == blocking
        assert "model missing" in data["message"]
        assert renames == []

    def test_first_hatch_success_notifies_name_change(self, client,
                                                      monkeypatch):
        c, renames = client
        monkeypatch.setattr(hatch, "is_hatched", lambda: False)
        monkeypatch.setattr(hatch, "perform_hatch",
                            lambda *a, **kw: dict(GOOD_RESULT))
        resp = c.post("/hatch", json={"name": "Nimbus"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["name"] == "Nimbus"
        assert data["script"][0]["text"].startswith("Hi. I'm Nimbus")
        assert renames == ["Nimbus"]

    def test_replay_does_not_notify(self, client, monkeypatch):
        c, renames = client
        monkeypatch.setattr(hatch, "is_hatched", lambda: True)
        monkeypatch.setattr(
            hatch, "perform_hatch",
            lambda *a, **kw: dict(GOOD_RESULT, replay=True, replays=1))
        resp = c.post("/hatch", json={})
        assert resp.status_code == 200
        assert resp.get_json()["replay"] is True
        assert renames == []

    def test_rename_notifies(self, client, monkeypatch):
        c, renames = client
        monkeypatch.setattr(hatch, "is_hatched", lambda: True)
        monkeypatch.setattr(
            hatch, "perform_hatch",
            lambda *a, **kw: dict(GOOD_RESULT, replay=True, name="Stratus"))
        resp = c.post("/hatch", json={"name": "Stratus", "rename": True})
        assert resp.status_code == 200
        assert renames == ["Stratus"]

    def test_name_required_is_400(self, client, monkeypatch):
        c, _ = client

        def needs_name(*a, **kw):
            raise hatch.HatchNameRequired("first hatch needs a name")
        monkeypatch.setattr(hatch, "perform_hatch", needs_name)
        monkeypatch.setattr(hatch, "is_hatched", lambda: False)
        resp = c.post("/hatch", json={})
        assert resp.status_code == 400
        assert "name" in resp.get_json()["message"]


class TestCertificateRoute:
    def test_svg(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setattr(certificate, "generate",
                            lambda fmt="svg", **kw: b"<svg>Nimbus</svg>")
        resp = c.get("/hatch/certificate")
        assert resp.status_code == 200
        assert resp.mimetype == "image/svg+xml"
        assert b"Nimbus" in resp.data

    def test_download_disposition(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setattr(certificate, "generate",
                            lambda fmt="svg", **kw: b"<svg/>")
        monkeypatch.setattr(hatch, "load_state", lambda: {"name": "Nimbus"})
        resp = c.get("/hatch/certificate?download=1")
        disposition = resp.headers["Content-Disposition"]
        assert "attachment" in disposition
        assert "Nimbus-adoption-certificate.svg" in disposition

    def test_unhatched_is_409(self, client, monkeypatch):
        c, _ = client

        def unavailable(**kw):
            raise certificate.CertificateUnavailable("not hatched yet")
        monkeypatch.setattr(certificate, "generate", unavailable)
        resp = c.get("/hatch/certificate")
        assert resp.status_code == 409
        assert "not hatched" in resp.get_json()["error"]

    def test_png_without_pillow_is_501(self, client, monkeypatch):
        c, _ = client

        def unavailable(**kw):
            raise certificate.CertificateUnavailable("PNG export needs Pillow")
        monkeypatch.setattr(certificate, "generate", unavailable)
        monkeypatch.setattr(certificate, "png_available", lambda: False)
        resp = c.get("/hatch/certificate?format=png")
        assert resp.status_code == 501
        assert resp.get_json()["formats"] == ["svg"]

    def test_unknown_format_is_400(self, client):
        c, _ = client
        assert c.get("/hatch/certificate?format=pdf").status_code == 400
