"""The home page, the Apps page and their words.

What this pins:

* the home page shows its apps and the Mood board even with no live cards,
  and the Apps page lists the Mood board, so "Choose a pictures folder
  there" points somewhere;
* an empty persona app ("WeddingBot", "Scaffolded from your goal") is never
  a tile in the home page's Apps widget;
* the floating Puff layer takes clicks only on herself;
* no em dashes or jargon in BuildBot's own words;
* the Developer Vault card never says "opened with your PIN" while locked;
* day-mode stars stand out from a light page.

The browser behaviour itself is proven headless; these
are the unit halves. No network, no model.

Run:
    pytest tests/test_dashboard_home_and_apps_lists.py
"""
from __future__ import annotations

import os
import re

import pytest

from puffpai import app_data as app_data_mod
from puffpai import app_registry, front_page, persona_scaffold
from puffpai.looks import colour as C
from puffpai.looks import package
from puffpai.wizard import first_look

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "dashboard", "app")


def _read(*parts: str) -> str:
    with open(os.path.join(DASH, *parts), encoding="utf-8") as fh:
        return fh.read()


# -- a persona with nothing in it -----------------------------------

@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(app_data_mod, "_DATA_DIR", str(tmp_path / "app_data"))
    app_registry.rescan()
    yield tmp_path
    app_registry.rescan()


def _owner_app(app_id: str) -> dict:
    return next(a for a in persona_scaffold.owner_apps() if a["id"] == app_id)


def test_a_persona_with_no_list_and_no_row_is_empty(owner):
    persona_scaffold.scaffold("weddingbot", name="WeddingBot",
                              goal="I want to build a wedding planner bot.")
    assert _owner_app("weddingbot")["lists"] == []
    assert _owner_app("weddingbot")["empty"] is True


def test_a_persona_with_a_list_is_not_empty_even_before_its_first_row(owner):
    persona_scaffold.scaffold("gardenbot", name="GardenBot", lists="plantings")
    assert _owner_app("gardenbot")["empty"] is False


def test_a_persona_with_a_saved_row_under_an_old_key_is_not_empty(owner):
    persona_scaffold.scaffold("weddingbot", name="WeddingBot")
    ad = app_data_mod.AppData()
    if "weddingbot" not in ad.VALID_APPS:
        pytest.skip("this build keeps no store for a list-less persona")
    ad.set_data("weddingbot", {"guests": [{"name": "Leo Park"}]})
    assert _owner_app("weddingbot")["empty"] is False


def test_an_unreadable_store_shows_the_app(owner, monkeypatch):
    persona_scaffold.scaffold("weddingbot", name="WeddingBot")

    class Broken:
        def get_data(self, _app):
            raise app_data_mod.AppDataUnreadable("torn")

    monkeypatch.setattr(app_data_mod, "AppData", Broken)
    assert _owner_app("weddingbot")["empty"] is False


# -- the home page and the Apps page ----------------------------------

# -- the floating Puff takes clicks only on herself ---------------------

# -- the owner's words -------------------------------------------------------

#: Every page the wedding run read an em dash on, and the words it used.
EM_DASH_PAGES = (
    ("dashboard", "apps", "_components", "AppsIndex.tsx"),
    ("dashboard", "chat", "page.tsx"),
    ("dashboard", "page.tsx"),
    ("_components", "Widgets", "JetsonTile.tsx"),
    ("dashboard", "_components", "FloatingPuff.tsx"),
)


@pytest.mark.parametrize("parts", EM_DASH_PAGES, ids=lambda p: "/".join(p))
def test_no_em_dash_in_the_words_on_these_pages(parts):
    from test_plain_words_in_ui_strings import tsx_strings
    hits = [t for _, t in tsx_strings(os.path.join(DASH, *parts)) if "—" in t]
    assert hits == []


def test_the_hardware_screen_says_ollama_is_running_without_its_address():
    shared = {"memory_gb": 16, "installed_models": ["qwen2.5:7b-instruct"],
              "ollama_url": "http://127.0.0.1:11435"}
    rows = {r["label"]: r["text"] for r in first_look.found_rows(shared)}
    assert rows["Ollama"] == "is running, with 1 model already: qwen2.5:7b-instruct"
    assert "127.0.0.1" not in rows["Ollama"]


def test_the_sign_up_keys_line_follows_web_search(monkeypatch):
    assert "Wikipedia" not in front_page.keys_line("WeddingBot", False)
    assert front_page.keys_line("WeddingBot", False).startswith(
        "Web search is off for WeddingBot.")
    assert "searches Wikipedia with no sign-up" in front_page.keys_line("WeddingBot", True)

    from puffpai import outside_services
    monkeypatch.setattr(outside_services, "rows",
                        lambda: [{"id": "web_search", "state": "off"}])
    assert front_page._web_search_on() is False
    monkeypatch.setattr(outside_services, "rows",
                        lambda: [{"id": "web_search", "state": "on"}])
    assert front_page._web_search_on() is True

    def boom():
        raise RuntimeError("unreadable")
    monkeypatch.setattr(outside_services, "rows", boom)
    assert front_page._web_search_on() is False


# -- the Developer Vault card ------------------------------------------

def test_the_vault_card_says_locked_or_open_now():
    words = _read("dashboard", "developer", "overviewWords.ts")
    assert '"Made, locked"' in words and '"Made, open now"' in words
    assert "opened with your PIN" not in words
    page = _read("dashboard", "developer", "page.tsx")
    assert "status: vaultStatus(vault)," in page
    assert "opened with your PIN" not in page


# -- day stars ------------------------------------------------------------

def test_day_stars_stand_out_from_a_light_page():
    m = {"bg": "#f7f4fb", "text": "#2e2140", "accent": "#b0328f", "bg2": "#efe9f7"}
    star = package.day_star(m)
    assert star == C.rgba_css("#b0328f", 0.9)
    # A pale accent that would vanish falls back to the text colour, nearly solid.
    pale = dict(m, accent="#f3e8f5")
    assert package.day_star(pale) == C.rgba_css("#2e2140", 0.8)
    day = package.background_vars("stars", "day", m)
    assert day["--puff-star"] == star
    assert day["--puff-star"] != C.rgba_css(m["text"], 0.35)
    # Night is unchanged: white stars on a dark sky.
    night = package.background_vars("stars", "night",
                                     dict(m, bg="#0b1026", bg2="#141a3a"))
    assert night["--puff-star"] == C.rgba_css("#ffffff", 0.9)

