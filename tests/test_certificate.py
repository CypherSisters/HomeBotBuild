"""Tests for the adoption certificate (puffpai.certificate).

Everything renders from an explicit synthetic fixture — no hatch state, no
live config, no network — proving the generator works from data alone.
"""

import pytest

import puffpai.certificate as certificate
import puffpai.hatch as hatch


FIXTURE_CREW = [
    {"id": "concierge", "display_name": "Finn",
     "specialty": "front desk — every request routes through them",
     "workflow": "WF-1"},
    {"id": "steward", "display_name": "Jake",
     "specialty": "the house — lights, doors, and tools",
     "workflow": "WF-2"},
    {"id": "sentinel", "display_name": "Marceline",
     "specialty": "security watch — reviews and vetoes",
     "workflow": "WF-3"},
    {"id": "toolsmith", "display_name": "Choose Goose",
     "specialty": "outside errands — useful, never handed the keys",
     "workflow": "WF-7"},
]


@pytest.fixture
def fixture_data():
    return certificate.certificate_data(
        name="Nimbus",
        hatch_date="2026-08-15",
        wake_word="hey puffpai",
        hardware="NVIDIA Jetson AGX Orin — 64GB",
        crew=[dict(m) for m in FIXTURE_CREW],
    )


class TestCertificateData:
    def test_explicit_fields_pass_through(self, fixture_data):
        assert fixture_data["name"] == "Nimbus"
        assert fixture_data["hatch_date"] == "2026-08-15"
        assert fixture_data["wake_word"] == "hey puffpai"
        assert len(fixture_data["crew"]) == 4

    def test_concierge_roster_earns_the_class_line(self, fixture_data):
        assert fixture_data["classing"] == "Concierge-class companion"

    def test_no_concierge_no_class_line(self):
        data = certificate.certificate_data(
            name="Nimbus", hatch_date="2026-08-15", wake_word="w",
            hardware="h", crew=[m for m in FIXTURE_CREW
                                if m["id"] != "concierge"])
        assert data["classing"] is None

    def test_unhatched_build_refuses(self, monkeypatch):
        monkeypatch.setattr(hatch, "load_state", lambda: None)
        monkeypatch.setattr(hatch, "current_name", lambda: None)
        with pytest.raises(certificate.CertificateUnavailable):
            certificate.certificate_data()


class TestRenderSvg:
    def test_quotes_in_values_still_valid_xml(self):
        # Values land in attributes too (root aria-label) — a double quote
        # must not break the document served as image/svg+xml.
        import xml.etree.ElementTree as ET
        data = certificate.certificate_data(
            name='Puff "Q" O\'Malley',
            hatch_date="2026-08-15",
            wake_word='say "hey puffpai"',
            hardware="h",
            crew=[dict(m) for m in FIXTURE_CREW],
        )
        svg = certificate.render_svg(data)
        ET.fromstring(svg)  # raises on malformed XML
        assert "&quot;Q&quot;" in svg

    def test_uppercased_fields_stay_valid_xml(self):
        # .upper() after escaping turned &apos; into &APOS; — entity names
        # are case-sensitive, so the footer's uppercased hardware line
        # served malformed XML. Uppercase must happen before escaping.
        import xml.etree.ElementTree as ET
        for hardware in ("Sam's Orin", "AT&T Orin box", 'the "big" box'):
            data = certificate.certificate_data(
                name="Nimbus", hatch_date="2026-08-15",
                wake_word="hey puffpai", hardware=hardware,
                crew=[dict(m) for m in FIXTURE_CREW])
            svg = certificate.render_svg(data)
            ET.fromstring(svg)
            for bad in ("&APOS;", "&QUOT;", "&AMP;"):
                assert bad not in svg
        data = certificate.certificate_data(
            name="Nimbus", hatch_date="2026-08-15", wake_word="w",
            hardware="h", crew=[dict(m) for m in FIXTURE_CREW])
        data["classing"] = "Concierge & Steward-class"
        ET.fromstring(certificate.render_svg(data))

    def test_key_fields_appear(self, fixture_data):
        svg = certificate.render_svg(fixture_data)
        assert svg.startswith("<svg")
        assert "Nimbus" in svg
        assert "2026-08-15" in svg
        assert "hey puffpai" in svg
        assert "NVIDIA Jetson AGX Orin — 64GB" in svg
        assert "CERTIFICATE OF ADOPTION" in svg
        assert "CONCIERGE-CLASS COMPANION" in svg
        for member in FIXTURE_CREW:
            assert member["display_name"] in svg
            assert member["specialty"].split(" — ")[0] in svg
            assert member["workflow"] in svg

    def test_night_palette_not_generic(self, fixture_data):
        svg = certificate.render_svg(fixture_data)
        for token in ("#0B0714", "#F7EEF8", "#F06FD0"):
            assert token in svg

    def test_class_line_omitted_without_concierge(self):
        data = certificate.certificate_data(
            name="Nimbus", hatch_date="2026-08-15", wake_word="w",
            hardware="h", crew=[m for m in FIXTURE_CREW
                                if m["id"] != "concierge"])
        assert "CONCIERGE-CLASS" not in certificate.render_svg(data)

    def test_name_is_xml_escaped(self):
        data = certificate.certificate_data(
            name="Nim<bus> & Co", hatch_date="2026-08-15", wake_word="w",
            hardware="h", crew=[])
        svg = certificate.render_svg(data)
        assert "Nim&lt;bus&gt; &amp; Co" in svg
        assert "<bus>" not in svg

    def test_render_is_deterministic(self, fixture_data):
        assert certificate.render_svg(fixture_data) == \
            certificate.render_svg(fixture_data)


class TestSeal:
    def test_geometry_is_a_pure_function_of_name_and_date(self):
        a = certificate.seal_geometry("Nimbus", "2026-08-15")
        b = certificate.seal_geometry("Nimbus", "2026-08-15")
        assert a == b

    def test_name_changes_the_paw(self):
        a = certificate.seal_geometry("Nimbus", "2026-08-15")
        b = certificate.seal_geometry("Stratus", "2026-08-15")
        assert a != b

    def test_date_changes_the_paw(self):
        a = certificate.seal_geometry("Nimbus", "2026-08-15")
        b = certificate.seal_geometry("Nimbus", "2026-08-16")
        assert a != b

    def test_geometry_stays_in_seal_bounds(self):
        geo = certificate.seal_geometry("Nimbus", "2026-08-15")
        assert all(40 <= r <= 80 for r in geo["blob_radii"])
        assert abs(geo["rotation"]) <= 12
        assert len(geo["toes"]) == 4


class TestPng:
    def test_png_bytes_when_pillow_present(self, fixture_data):
        if not certificate.png_available():
            pytest.skip("Pillow not installed — SVG-only build")
        payload = certificate.render_png(fixture_data, scale=1)
        assert payload is not None
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"

    def test_generate_png_raises_cleanly_without_pillow(self, monkeypatch,
                                                        fixture_data):
        monkeypatch.setattr(certificate, "png_available", lambda: False)
        monkeypatch.setattr(certificate, "render_png",
                            lambda data, **k: None)
        with pytest.raises(certificate.CertificateUnavailable):
            certificate.generate(
                fmt="png", name="Nimbus", hatch_date="2026-08-15",
                wake_word="w", hardware="h", crew=[])


class TestGenerate:
    def test_generate_svg_bytes(self):
        payload = certificate.generate(
            fmt="svg", name="Nimbus", hatch_date="2026-08-15",
            wake_word="hey puffpai", hardware="h", crew=[])
        assert payload.startswith(b"<svg")
        assert b"Nimbus" in payload
