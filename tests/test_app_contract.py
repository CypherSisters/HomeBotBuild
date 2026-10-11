"""The app contract on HomeBot: manifests, fallback, bootstrap, vocabulary.

HomeBot's five apps are described by ``apps/<id>/app.json`` manifests, the
same format BuildBot reads, with the core (the valid-app list and default
shapes in :mod:`puffpai.app_data`, the words in the router, the "app" surface
in the capability registry) as the fallback. The builds must read the SAME
manifest, because "an app from one build runs on another" is the whole point
of sharing one manifest.

What these tests pin:

* the five shipped manifests load, in order, with no faults recorded;
* the FALLBACK still runs — an app with no manifest, and a build with no
  ``apps/`` directory at all, behave exactly as the core defaults describe;
* ``_FORBIDDEN_KEYS`` still refuses a manifest that names code, at the top
  level and inside an action and a door;
* the five privacy fields are present on every shipped app and are
  actually validated (wildcard host, missing purpose, refused credential kind);
* the trivia bootstrap: an app with no data file gets one on first use, and a
  corrupt file is NEVER overwritten;
* the router's app vocabulary still reaches the same readers, using the
  phrasings from the capability matrix;
* packs load, and a pack naming an app this build does not
  have is refused with the missing id named;
* no shipped app page reaches a model endpoint or any absolute URL from the
  browser.

No network, no model, no writes inside the repo.

Run: py -3 -m pytest tests/test_app_contract.py -q
"""

import json
import os
import re
from pathlib import Path

import pytest

from puffpai import app_registry
from puffpai.app_data import AppData

REPO_ROOT = Path(__file__).resolve().parents[1]
APPS_DIR = REPO_ROOT / "apps"
PACKS_DIR = REPO_ROOT / "packs"

SHIPPED = ["alarmflow", "calendar", "freshtable", "fitfam", "trivia"]


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Every test starts from a cold scan, and leaves one behind."""
    app_registry.rescan()
    yield
    try:
        app_registry.rescan()
    except Exception:
        # A test that broke the scan on purpose has not yet had its
        # monkeypatch undone; the next test's setup rescans anyway.
        pass


def _manifest(app_id):
    with open(APPS_DIR / app_id / "app.json", encoding="utf-8") as fh:
        return json.load(fh)


# ── manifest load ───────────────────────────────────────────────────────────

def test_five_shipped_manifests_load_in_order():
    manifests = app_registry.load_manifests(str(APPS_DIR))
    assert [m["id"] for m in manifests] == SHIPPED
    assert app_registry.load_problems() == []


def test_every_shipped_manifest_validates():
    for app_id in SHIPPED:
        assert app_registry.validate_manifest(_manifest(app_id)) is None, app_id


def test_manifest_schema_is_the_buildbot_one():
    """`puffpai_app/v1` manifests stay valid — the two builds read one file."""
    for app_id in SHIPPED:
        assert _manifest(app_id)["schema"] == "puffpai_app/v1"


def test_a_bad_manifest_costs_one_app_not_the_registry(tmp_path):
    for app_id in SHIPPED:
        folder = tmp_path / app_id
        folder.mkdir()
        (folder / "app.json").write_text(
            json.dumps(_manifest(app_id)), encoding="utf-8")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "app.json").write_text("{not json", encoding="utf-8")

    manifests = app_registry.load_manifests(str(tmp_path))
    assert [m["id"] for m in manifests] == SHIPPED
    problems = app_registry.load_problems()
    assert len(problems) == 1 and "broken" in problems[0]["detail"]


def test_missing_apps_directory_is_quiet(tmp_path):
    assert app_registry.load_manifests(str(tmp_path / "nope")) == []
    assert app_registry.load_problems() == []


# ── forbidden keys ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["handler", "module", "exec", "script",
                                 "command", "hook", "plugin", "run"])
def test_forbidden_key_refused_at_top_level(key):
    data = _manifest("trivia")
    data[key] = "anything"
    problem = app_registry.validate_manifest(data)
    assert problem is not None and key in problem


def test_forbidden_key_refused_inside_an_action():
    data = _manifest("trivia")
    data["actions"][0]["run"] = "rm -rf /"
    problem = app_registry.validate_manifest(data)
    assert problem is not None and "run" in problem


def test_forbidden_key_refused_inside_a_door():
    data = _manifest("trivia")
    data["doors"][0]["exec"] = "curl"
    problem = app_registry.validate_manifest(data)
    assert problem is not None and "exec" in problem


def test_an_id_cannot_walk_out_of_the_apps_directory():
    for bad in ["../etc", "..", "a/b", "A", "_x", ".hidden", "x" * 40, 7, None]:
        assert not app_registry.valid_id(bad)
    with pytest.raises(ValueError):
        app_registry.app_dir("../etc", str(APPS_DIR))


def test_an_app_cannot_invent_a_surface():
    data = _manifest("trivia")
    data["surfaces"] = ["house.cameras.secret"]
    problem = app_registry.validate_manifest(data)
    assert problem is not None and "cannot invent one" in problem


# ── the five privacy fields ─────────────────────────────────────────────────

def test_every_shipped_app_declares_the_five_privacy_fields():
    for app_id in SHIPPED:
        data = _manifest(app_id)
        door = data["doors"][0]
        assert door["hosts"] == [], f"{app_id} reaches off the box"
        assert door["purpose"] and len(door["purpose"]) <= 140
        assert door["data_sent_plain"] == "nothing"
        assert data["credential"]["kind"] == "none"
        assert data["children"]["may_be_used_by_minor"] is True


def test_a_wildcard_host_is_a_validator_error():
    data = _manifest("trivia")
    data["doors"][0]["hosts"] = ["*.example.com"]
    problem = app_registry.validate_manifest(data)
    assert problem is not None and "wildcard" in problem


def test_a_host_may_not_carry_a_scheme_or_port():
    data = _manifest("trivia")
    for host in ["https://example.com", "example.com:443", "example.com/path"]:
        data["doors"][0]["hosts"] = [host]
        assert app_registry.validate_manifest(data) is not None, host


def test_a_door_must_say_why_and_what_leaves():
    for field in ("purpose", "data_sent_plain"):
        data = _manifest("trivia")
        del data["doors"][0][field]
        problem = app_registry.validate_manifest(data)
        assert problem is not None, field


@pytest.mark.parametrize("kind", ["password", "cookie"])
def test_password_and_cookie_credentials_are_refused(kind):
    data = _manifest("trivia")
    data["credential"]["kind"] = kind
    problem = app_registry.validate_manifest(data)
    assert problem is not None and kind in problem


def test_a_manifest_may_not_carry_a_credential_value():
    data = _manifest("trivia")
    data["credential"]["value"] = "not-a-real-key"
    problem = app_registry.validate_manifest(data)
    assert problem is not None and "never a secret's value" in problem


def test_may_be_used_by_minor_must_be_a_boolean():
    data = _manifest("trivia")
    data["children"]["may_be_used_by_minor"] = "yes"
    assert app_registry.validate_manifest(data) is not None


# ── requires: the ollama dependency is declared ─────────────────────────────

def test_the_two_model_backed_apps_declare_ollama():
    """FreshTable and Trivia are incomplete without the model."""
    assert _manifest("freshtable")["requires"]["ollama"] is True
    assert _manifest("trivia")["requires"]["ollama"] is True
    for app_id in ["alarmflow", "calendar", "fitfam"]:
        assert _manifest(app_id)["requires"]["ollama"] is False


# ── app_data: manifest wins, fallback fills ─────────────────────────────────

def test_manifest_defaults_match_what_the_core_shipped(tmp_path, monkeypatch):
    """Placing the manifests changes nothing an owner can observe."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib
    from puffpai import app_data as mod
    importlib.reload(mod)
    store = mod.AppData()
    for app_id in ["alarmflow", "calendar", "freshtable", "trivia"]:
        declared = app_registry.default_data(_manifest(app_id))
        assert store._defaults[app_id]() == declared, app_id


def test_fitfam_falls_back_to_the_core_default(tmp_path, monkeypatch):
    """FitFam's manifest declares no data block, so the shipped shape stands.

    The shipped shape is a SCHEMA, not a household: seeding members by name,
    age or sport would ship one family inside everyone else's appliance. The
    core default is an empty roster on purpose (a running box supplies its own
    fitfam.json, so this default is never what an owner actually sees) -- this
    proves the fallback path resolves at all AND that it carries no name/age
    data, not that it is pre-populated.
    """
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib
    from puffpai import app_data as mod
    importlib.reload(mod)
    store = mod.AppData()
    assert "data" not in _manifest("fitfam")
    default = store._defaults["fitfam"]()
    assert "users" in default, "fallback default was lost"
    assert default["users"] == {}, "the core default must ship an EMPTY roster, not a household"
    default_text = repr(default)
    for leaked_field in ("name", "age", "sport"):
        assert leaked_field not in default_text, (
            f"core FitFam default must carry no {leaked_field!r} field")


def test_apps_without_manifests_still_work(tmp_path, monkeypatch):
    """mediahub is a core surface, not an installable app."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib
    from puffpai import app_data as mod
    importlib.reload(mod)
    store = mod.AppData()
    for app_id in ["mediahub"]:
        assert app_id in store.VALID_APPS
        assert store.get_data(app_id) is not None


def test_valid_apps_and_files_agree(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib
    from puffpai import app_data as mod
    importlib.reload(mod)
    store = mod.AppData()
    assert set(store.VALID_APPS) == set(store._files)
    assert set(SHIPPED).issubset(set(store.VALID_APPS))


def test_a_registry_that_cannot_be_read_leaves_the_build_running(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib
    from puffpai import app_data as mod
    importlib.reload(mod)
    monkeypatch.setattr(app_registry, "load_manifests",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    store = mod.AppData()
    assert set(mod.AppData.VALID_APPS).issubset(set(store.VALID_APPS))


# ── bootstrap ───────────────────────────────────────────────────────────────

def test_first_use_creates_the_data_file(tmp_path, monkeypatch):
    """An app with no data file (Trivia ships without ``trivia.json``) gets one."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib
    from puffpai import app_data as mod
    importlib.reload(mod)
    store = mod.AppData()
    path = Path(store._files["trivia"])
    assert not path.exists()

    data = store.get_data("trivia")
    assert data == {"results": [], "sessions": [], "players": {},
                    "last_updated": ""}
    assert path.exists(), "first use did not create the data file"
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_bootstrap_is_generic_across_every_app(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib
    from puffpai import app_data as mod
    importlib.reload(mod)
    store = mod.AppData()
    for app_id in store.VALID_APPS:
        store.get_data(app_id)
        assert Path(store._files[app_id]).exists(), app_id


def test_a_corrupt_data_file_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib
    from puffpai import app_data as mod
    importlib.reload(mod)
    store = mod.AppData()
    path = Path(store._files["trivia"])
    path.write_text("{ this is the owner's broken file", encoding="utf-8")

    from puffpai.app_data import AppDataCorrupt
    with pytest.raises(AppDataCorrupt):
        store.get_data("trivia")
    assert path.read_text(encoding="utf-8").startswith("{ this is")


def test_a_read_only_data_dir_still_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib
    from puffpai import app_data as mod
    importlib.reload(mod)
    store = mod.AppData()

    def _refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(mod, "open", _refuse, raising=False)
    import builtins
    monkeypatch.setattr(builtins, "open", _refuse)
    assert store.get_data("trivia") == {"results": [], "sessions": [],
                                       "players": {}, "last_updated": ""}


# ── vocabulary reaches the same readers ─────────────────────────────────────

MATRIX_PHRASINGS = [
    ("set an alarm for 7am", "alarmflow"),
    ("what's for dinner tonight", "freshtable"),
    ("whats in the pantry", "freshtable"),
    ("lets play a quiz", "trivia"),
    ("log todays workout", "fitfam"),
]


@pytest.mark.parametrize("phrase,app_id", MATRIX_PHRASINGS)
def test_the_matrix_phrasings_still_reach_their_app(phrase, app_id):
    """Every phrasing keeps at least one keyword of the app that owns it.

    This is an anti-drift check: the assembled
    vocabulary is the apps the build has, so a removed app must take its words
    with it and a kept app must keep them.
    """
    manifests = app_registry.load_manifests(str(APPS_DIR))
    mine = [m for m in manifests if m["id"] == app_id][0]
    words = [w for w in mine["vocabulary"]["keywords"]]
    lower = phrase.lower()
    assert any(w in lower for w in words), (phrase, words)


def test_the_assembled_vocabulary_is_the_installed_apps(tmp_path):
    """Remove Trivia's folder and the trivia words go with it."""
    for app_id in SHIPPED:
        folder = tmp_path / app_id
        folder.mkdir()
        (folder / "app.json").write_text(
            json.dumps(_manifest(app_id)), encoding="utf-8")

    full = app_registry.assemble_vocabulary(
        app_registry.load_manifests(str(tmp_path)))
    assert "quiz" in full["keywords"] and "trivia" in full["keywords"]

    import shutil
    shutil.rmtree(tmp_path / "trivia")
    trimmed = app_registry.assemble_vocabulary(
        app_registry.load_manifests(str(tmp_path)))
    assert "quiz" not in trimmed["keywords"]
    assert "trivia" not in trimmed["keywords"]
    assert "pantry" in trimmed["keywords"], "the other apps kept their words"


def test_vocabulary_bounds_hold():
    for app_id in SHIPPED:
        data = _manifest(app_id)
        words = data["vocabulary"]["keywords"]
        assert len(words) <= app_registry.MAX_KEYWORDS
        for word in words:
            assert 2 <= len(word) <= app_registry.MAX_KEYWORD_LEN
            assert word == word.lower()


# ── the capability registry resolves app ids through the registry ───────────

def test_every_mapped_capability_exists():
    from puffpai import capability_registry as cr
    for cap_id in app_registry.CAPABILITY_APPS:
        assert cap_id in cr._BY_ID, cap_id


def test_every_mapped_tool_exists_in_the_capability_registry():
    from puffpai import capability_registry as cr
    known = set()
    for cap in cr._BY_ID.values():
        known |= set(cap.all_tools)
    assert not (set(app_registry.CORE_TOOL_APPS) - known)


def test_a_removed_app_loses_its_app_surface(monkeypatch):
    from puffpai import capability_registry as cr
    cap = cr._BY_ID["learning.trivia"]
    monkeypatch.setattr(cr, "_BY_ID", cr._BY_ID)

    # Installed: the declared surfaces stand, whatever they are.
    assert cr.live_surfaces(cap) == cap.surfaces

    pantry = cr._BY_ID["pantry.shopping"]
    assert "app" in pantry.surfaces
    monkeypatch.setattr(app_registry, "installed_ids",
                        lambda *a, **k: ["alarmflow", "calendar"])
    assert "app" not in cr.live_surfaces(pantry)
    assert "voice" in cr.live_surfaces(pantry), "only the app surface goes"


def test_a_registry_failure_leaves_surfaces_alone(monkeypatch):
    from puffpai import capability_registry as cr
    pantry = cr._BY_ID["pantry.shopping"]
    monkeypatch.setattr(app_registry, "installed_ids",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert cr.live_surfaces(pantry) == pantry.surfaces


# ── packs ──────────────────────────────────────────────

def test_the_homebot_pack_loads():
    manifests = app_registry.load_manifests(str(APPS_DIR))
    packs = app_registry.load_packs(str(PACKS_DIR), manifests)
    assert [p["id"] for p in packs] == ["homebot"]
    pack = packs[0]
    assert pack["display_name"] == "HomeBot app pack"
    assert [a["id"] for a in pack["apps"]] == SHIPPED
    assert app_registry.load_problems() == []


def test_the_pack_privacy_summary_is_derived_from_its_apps():
    manifests = app_registry.load_manifests(str(APPS_DIR))
    pack = app_registry.load_packs(str(PACKS_DIR), manifests)[0]
    assert pack["privacy"]["hosts"] == []
    assert pack["privacy"]["credentials"] == []
    assert pack["privacy"]["child_safe_declared"] is True


def test_nothing_calls_a_self_attestation_child_safe():
    """It is an AND-fold over five boxes the authors ticked themselves."""
    manifests = app_registry.load_manifests(str(APPS_DIR))
    pack = app_registry.load_packs(str(PACKS_DIR), manifests)[0]
    assert "child_safe" not in pack["privacy"]
    source = (REPO_ROOT / "src" / "puffpai" / "app_registry.py").read_text(
        encoding="utf-8")
    assert '"child_safe":' not in source, "a payload key called child_safe is a claim"


def test_a_pack_naming_a_missing_app_is_refused():
    data = {
        "schema": "puffpai_pack/v1",
        "id": "examplebot",
        "display_name": "ExampleBot",
        "apps": [{"id": "telescope", "version": "1.0.0"}],
    }
    problem = app_registry.validate_pack(data, set(SHIPPED))
    assert problem is not None and "telescope" in problem


def test_a_pack_may_not_name_code():
    data = {
        "schema": "puffpai_pack/v1",
        "id": "examplebot",
        "display_name": "ExampleBot",
        "run": "setup.sh",
        "apps": [{"id": "trivia", "version": "1.0.0"}],
    }
    problem = app_registry.validate_pack(data, set(SHIPPED))
    assert problem is not None and "run" in problem


def test_a_pack_with_no_apps_is_refused():
    data = {"schema": "puffpai_pack/v1", "id": "empty",
            "display_name": "Empty", "apps": []}
    assert app_registry.validate_pack(data, set(SHIPPED)) is not None


def test_a_broken_pack_is_reported_and_skipped(tmp_path):
    manifests = app_registry.load_manifests(str(APPS_DIR))
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "pack.json").write_text(
        json.dumps({"schema": "puffpai_pack/v1", "id": "bad",
                    "display_name": "Bad",
                    "apps": [{"id": "nosuchapp"}]}), encoding="utf-8")
    assert app_registry.load_packs(str(tmp_path), manifests) == []
    problems = app_registry.load_problems()
    assert any("nosuchapp" in p["detail"] for p in problems)


# ── no shipped app page reaches a model endpoint from the browser ───────────

#: The dashboard ships inside this repo at ``dashboard/``. Its
#: ``public/*-app.html`` files are the pages the browser actually runs, so the
#: rule is asserted there too.
DASHBOARD_PUBLIC = REPO_ROOT / "dashboard" / "public"


def _app_pages():
    """Every ``*-app.html`` reachable from here, in either repo."""
    pages = []
    roots = [REPO_ROOT]
    if DASHBOARD_PUBLIC.is_dir():
        roots.append(DASHBOARD_PUBLIC)
    for base in roots:
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs
                       if d not in {".git", "node_modules", ".next",
                                    "__pycache__"}]
            for name in files:
                if name.endswith("-app.html"):
                    pages.append(Path(root) / name)
        vendor_css = Path(base) / "vendor" / "fonts"
        if vendor_css.is_dir():
            pages.extend(sorted(vendor_css.glob("*.css")))
    return pages


_ABSOLUTE_URL = re.compile(r"https?://", re.IGNORECASE)


def test_no_app_page_reaches_a_model_endpoint_or_an_absolute_url():
    """The page must not open a socket the box's doors never see.

    A browser call is invisible to the door registry, the ledger and the PIN
    gate, so "runs locally on your machine" is only true if the page has no
    absolute URL at all — model port included.

    HomeBot's own repo carries no ``*-app.html`` — its pages live in the
    dashboard repo beside it, which is scanned when checked out. On a machine
    without that checkout this still asserts the rule for anything carried
    here, so a page landing in this tree later cannot land with a URL in it.
    """
    offenders = []
    for page in _app_pages():
        text = page.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if "11434" in line or _ABSOLUTE_URL.search(line):
                offenders.append(f"{page}:{number}: {line.strip()[:120]}")
    assert not offenders, "app pages must reach nothing:\n" + "\n".join(offenders)


# ── the HTTP surface the app pages use instead of dialling the model ────────

class TestAppRoutes:
    """``/apps/generate`` and ``/apps/ocr`` replace the browser's model call.

    A page that held a free-text endpoint and POSTed to it itself would be
    invisible to every door, ledger and PIN on the box. These routes stand in
    for that call, so the two things that matter are: they exist, and they are
    NOT public.
    """

    @pytest.fixture(scope="class")
    def api(self):
        import sys
        from unittest.mock import MagicMock
        from tests.modpatch import patched_modules

        src = str(REPO_ROOT / "src" / "puffpai")
        if src not in sys.path:
            sys.path.insert(0, src)
        mock_fw = MagicMock()
        mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
        with patched_modules({"firewall_manager":
                              MagicMock(FirewallManager=lambda: mock_fw)}):
            import importlib
            if "api_server" in sys.modules:
                mod = importlib.reload(sys.modules["api_server"])
            else:
                mod = importlib.import_module("api_server")
            mod.app.config["TESTING"] = True
            yield mod

    def test_the_app_model_routes_exist(self, api):
        rules = {r.rule for r in api.app.url_map.iter_rules()}
        assert "/apps/generate" in rules
        assert "/apps/ocr" in rules
        assert "/apps/packs" in rules
        assert "/apps/installed" in rules
        assert "/apps/rescan" in rules

    def test_none_of_them_is_public(self, api):
        for endpoint in ("apps_generate", "apps_ocr", "apps_packs",
                         "apps_installed", "apps_rescan"):
            assert endpoint not in api._PUBLIC_ENDPOINTS, endpoint

    def test_generate_refuses_an_empty_and_an_oversized_prompt(self, api):
        api._rate_limit_store.clear()
        with api.app.test_client() as c:
            head = {"X-API-Key": api._API_KEY}
            assert c.post("/apps/generate", json={}, headers=head).status_code == 400
            big = "x" * (api.MAX_APP_PROMPT_CHARS + 1)
            assert c.post("/apps/generate", json={"prompt": big},
                          headers=head).status_code == 413

    def test_ocr_refuses_a_body_that_is_not_base64(self, api):
        api._rate_limit_store.clear()
        with api.app.test_client() as c:
            head = {"X-API-Key": api._API_KEY}
            assert c.post("/apps/ocr", json={}, headers=head).status_code == 400
            assert c.post("/apps/ocr", json={"image_b64": "not base64!!"},
                          headers=head).status_code == 400

    def test_the_valid_app_check_reads_the_live_store(self, api):
        assert api._valid_app("freshtable")
        assert not api._valid_app("no_such_app")


# ── requirements are evaluated, not decorative  ──────────────

def test_declared_requirements_reads_only_the_true_ones():
    assert app_registry.declared_requirements(_manifest("freshtable")) == ["ollama"]
    assert app_registry.declared_requirements(_manifest("trivia")) == ["ollama"]
    for app_id in ["alarmflow", "calendar", "fitfam"]:
        assert app_registry.declared_requirements(_manifest(app_id)) == []


def test_an_app_cannot_invent_a_requirement():
    data = _manifest("trivia")
    data["requires"]["telescope"] = True
    assert app_registry.declared_requirements(data) == ["ollama"]


@pytest.mark.parametrize("key,app_id", [
    ("ollama", "freshtable"),
    ("ollama", "trivia"),
])
def test_an_unmet_requirement_degrades_with_a_reason(key, app_id):
    data = _manifest(app_id)
    reason = app_registry.degraded_reason(data, {key: False})
    assert reason
    assert data["name"] in reason
    assert app_registry.REQUIREMENT_REASONS[key] in reason


def test_a_met_requirement_is_not_degraded():
    assert app_registry.degraded_reason(
        _manifest("trivia"), {"ollama": True}) == ""


def test_a_probe_that_could_not_answer_says_so_rather_than_nothing():
    """None is not "not unmet": a box that could not check must not report
    healthy apps. Both False and None are degraded; only the sentence
    differs. An empty/absent map is still "nobody probed"."""
    reason = app_registry.degraded_reason(_manifest("trivia"), {"ollama": None})
    assert reason == ("Trivia needs a local model, and this build could not "
                      "verify that one is there.")
    assert app_registry.degraded_reason(_manifest("trivia"), {}) == ""
    assert app_registry.degraded_reason(_manifest("trivia"), None) == ""


def test_a_known_absence_reads_differently_from_a_broken_probe():
    absent = app_registry.degraded_reason(_manifest("trivia"), {"ollama": False})
    unsure = app_registry.degraded_reason(_manifest("trivia"), {"ollama": None})
    assert absent == "Trivia needs a local model, and none is available on this build."
    assert absent != unsure


def test_an_app_with_no_requirement_is_never_degraded_by_a_broken_probe():
    assert app_registry.degraded_reason(
        _manifest("calendar"),
        {"ollama": None, "home_assistant": None, "network": None}) == ""

def test_a_box_with_no_model_backend_reports_degraded(monkeypatch):
    """The production shape of an unknown probe: get_llm_backend RAISES when there
    is no model, which is exactly the state the badge exists for."""
    import sys

    from puffpai import api_server
    import puffpai.config as config_mod

    def _no_backend():
        raise RuntimeError("no LLM backend configured on this build")

    # PIN the module first. _requirement_availability does
    # `from puffpai.config import get_llm_backend` at call time, which reads
    # sys.modules; an earlier test file that swapped a stub in there would make
    # the patch below land on an object nobody consults, and the probe would
    # answer True for reasons that have nothing to do with this assertion.
    monkeypatch.setitem(sys.modules, "puffpai.config", config_mod)
    monkeypatch.setattr(config_mod, "get_llm_backend", _no_backend)
    from puffpai.config import get_llm_backend as _resolved
    assert _resolved is _no_backend, "the patch did not reach the import path"
    available = api_server._requirement_availability(force=True)
    assert available["ollama"] is False, "a raised probe must answer False"

    app_registry.rescan()
    records = {r["id"]: r for r in app_registry.ui_records(
        app_registry.current_manifests(), available)}
    for app_id in ("freshtable", "trivia"):
        assert records[app_id]["degraded"] is True, app_id
        assert "local model" in records[app_id]["degraded_reason"], app_id
    assert records["calendar"]["degraded"] is False
    api_server._requirement_availability(force=True)   # drop the pinned cache


@pytest.mark.parametrize("key", ["ollama", "home_assistant", "network"])
def test_every_requirement_key_has_an_owner_readable_reason(key):
    reason = app_registry.REQUIREMENT_REASONS[key]
    assert reason and reason[0].islower() and not reason.endswith(".")
    data = _manifest("trivia")
    data["requires"] = {key: True}
    assert app_registry.degraded_reason(data, {key: False}).endswith(".")


def test_a_degraded_app_is_still_installed():
    """An unmet requirement never blocks an install."""
    manifests = app_registry.load_manifests(str(APPS_DIR))
    records = app_registry.ui_records(manifests, {"ollama": False})
    assert [r["id"] for r in records] == SHIPPED
    degraded = {r["id"]: r["degraded_reason"] for r in records if r["degraded"]}
    assert set(degraded) == {"freshtable", "trivia"}


def test_the_pack_names_its_degraded_apps():
    manifests = app_registry.load_manifests(str(APPS_DIR))
    pack = app_registry.load_packs(str(PACKS_DIR), manifests,
                                   {"ollama": False})[0]
    assert {d["id"] for d in pack["degraded_apps"]} == {"freshtable", "trivia"}
    assert all(d["reason"] for d in pack["degraded_apps"])

    healthy = app_registry.load_packs(str(PACKS_DIR), manifests,
                                      {"ollama": True})[0]
    assert healthy["degraded_apps"] == []


# ── the hosts line is honest about what the box does  ────────
#
# These two are a tripwire pair: one asserts the label every surface shows
# (``HOSTS_ENFORCEMENT == "enforced"``), the other asserts the code that makes
# it true (every app's ``app:`` door is in the enforced table, see
# ``test_security_manifest_declared_hosts.py``), so the label and the code
# cannot move apart. If registration is ever removed, the second fails and the
# ``declared_only`` notice has to come back.


def test_every_surface_reports_what_the_box_really_does_with_manifest_hosts():
    assert app_registry.HOSTS_ENFORCEMENT == "enforced"
    assert app_registry.HOSTS_DECLARED_ONLY == "declared_only"
    assert "does not yet enforce" in app_registry.HOSTS_DECLARED_ONLY_NOTICE
    manifests = app_registry.load_manifests(str(APPS_DIR))
    app_registry.register_egress_doors(manifests)
    for record in app_registry.ui_records(manifests):
        assert record["hosts_enforcement"] == "enforced"
        assert record["hosts_notice"] == app_registry.HOSTS_ENFORCEMENT_NOTICE
    pack = app_registry.load_packs(str(PACKS_DIR), manifests)[0]
    assert pack["privacy"]["hosts_enforcement"] == "enforced"


def test_manifest_hosts_really_do_reach_the_door_table():
    """The claim above must stay true-by-code, not true-by-comment.

    Every shipped app's declared door is in the table this build enforces,
    namespaced so it cannot shadow a built-in one, and shut. If somebody
    unwires registration, this fails and the ``declared_only`` notice must go
    back on every surface in the same commit — which is the point, still.
    """
    from puffpai.security import egress_doors
    manifests = app_registry.load_manifests(str(APPS_DIR))
    app_registry.register_egress_doors(manifests)
    ids = set(egress_doors.door_ids())
    assert ids, "the door table could not be read; this guard is not running"
    for app_id in SHIPPED:
        door_id = f"app:{app_id}:0"
        assert door_id in ids
        assert egress_doors.egress_allowed(door_id)[0] is False
        assert egress_doors.door(door_id).default is False


class TestHonestDecline:
    """An app that cannot work says so before it starts, not halfway through.

    Without this, ``plan this week's meals`` reaches ``auto_meal_plan`` on a
    build with no model and comes back "came back empty", which an owner reads
    as "I have no meals" rather than "this build has no model".
    """

    @pytest.fixture(scope="class")
    def api(self):
        import sys
        from unittest.mock import MagicMock
        from tests.modpatch import patched_modules

        src = str(REPO_ROOT / "src" / "puffpai")
        if src not in sys.path:
            sys.path.insert(0, src)
        mock_fw = MagicMock()
        mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
        with patched_modules({"firewall_manager":
                              MagicMock(FirewallManager=lambda: mock_fw)}):
            import importlib
            if "api_server" in sys.modules:
                mod = importlib.reload(sys.modules["api_server"])
            else:
                mod = importlib.import_module("api_server")
            mod.app.config["TESTING"] = True
            yield mod

    def _pin(self, api, monkeypatch, value):
        monkeypatch.setattr(api, "_requirement_availability",
                            lambda force=False, app_id=None: {
                                "ollama": value,
                                "home_assistant": value,
                                "network": value})

    def test_the_meal_fastpath_declines_by_naming_what_is_missing(
            self, api, monkeypatch):
        app_registry.rescan()
        self._pin(api, monkeypatch, False)
        answer = api._try_meal_plan("plan this week's meals")
        assert answer and "local model" in answer
        assert "FreshTable" in answer

    def test_the_quiz_fastpath_declines_by_naming_what_is_missing(
            self, api, monkeypatch):
        app_registry.rescan()
        self._pin(api, monkeypatch, False)
        answer = api._try_trivia_start("lets play a quiz")
        assert answer and "local model" in answer

    def test_an_unknown_probe_says_it_could_not_check(self, api, monkeypatch):
        """A probe that could not answer is still an answer."""
        app_registry.rescan()
        self._pin(api, monkeypatch, None)
        assert "could not verify" in api._app_unavailable_reason("trivia")
        assert "could not verify" in api._app_unavailable_reason("freshtable")

    def test_an_unavailable_app_answers_only_questions_about_itself(
            self, api, monkeypatch):
        """A build with no model must not answer every sentence with
        Trivia's (or FreshTable's) unavailability."""
        app_registry.rescan()
        self._pin(api, monkeypatch, False)
        for said in ("turn off the kitchen light", "what time is it",
                     "trivia night is tomorrow", "add milk to the list"):
            assert api._try_trivia_start(said) is None, said
            assert api._try_meal_plan(said) is None, said

    def test_a_met_requirement_does_not_decline(self, api, monkeypatch):
        app_registry.rescan()
        self._pin(api, monkeypatch, True)
        assert api._app_unavailable_reason("freshtable") == ""

    def test_the_probe_answers_all_three_keys_and_never_raises(self, api):
        available = api._requirement_availability(force=True)
        assert set(available) == {"ollama", "home_assistant", "network"}
        for value in available.values():
            assert value is True or value is False or value is None
