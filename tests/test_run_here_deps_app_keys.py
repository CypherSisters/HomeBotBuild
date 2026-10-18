"""The seams between running an app here, its dependencies and its keys.

* ``run_here.install_missing`` makes the "Install what's
  missing" card (one yes, Drawbridge first, no key in the installer, one
  security-log line), only for programs on BuildBot's own list.
* ``run_here.cli_dirs`` reaches ``run_command``'s path (last) and
  ``deps.has_program``.
* ``run_here.app_keys``: a key an app names in its README or its
  example settings file can be kept for that app (with a "Get a key" link)
  and reaches only that app's own process when it starts.
* The routine skill check reads what a skill needs with deps.

Nothing is installed and nothing goes online: the installer is replaced.
"""
from __future__ import annotations

import json
import os
import sys


import pytest

sys.path.insert(0, os.path.dirname(__file__))
from keys_support import PIN, fast_vault, reset_keys  # noqa: E402
from test_integrate_run_here import (  # noqa: E402,F401 - the fixture is used by name
    PY_SERVER, _get, _prepare_and_yes, py_tree, world)

from puffpai import brain_commands, deps, drawbridge, routines, skills, things  # noqa: E402
from puffpai.integrate import run_here  # noqa: E402
from puffpai.security import keys  # noqa: E402

GEMINI = "AIzaSyD-test-0123456789abcdefghijklmnop"
README = """# Tiny tutor

Shows chords on its own page. Set `GEMINI_API_KEY` first
(get one at https://aistudio.google.com/app/apikey ).
Never set $PUFFPAI_API_KEY or `NODE_OPTIONS` for it.
"""
KEY_SERVER = PY_SERVER.replace(
    'body = b"<h1>Tiny tutor, its own page</h1>"',
    'body = (b"has-key" if os.environ.get("GEMINI_API_KEY") == "' + GEMINI + '" else '
    'b"no-key")')


def _keyed_tree(root: str) -> str:
    py_tree(root)
    from integrate_support import write
    write(root, "README.md", README)
    write(root, ".env.example", "LOG_LEVEL=info\n")      # the key is named only in the README
    write(root, "server.py", KEY_SERVER)
    return root


@pytest.fixture
def vault(monkeypatch, tmp_path):
    reset_keys()
    v = fast_vault(monkeypatch, tmp_path / "vault")
    keys.set_vault_provider(lambda: v)
    yield v
    keys.set_vault_provider(None)


@pytest.fixture
def installer(monkeypatch):
    """The one installer, replaced: what it would run, and with which env."""
    monkeypatch.setitem(drawbridge._CHECKERS, deps.KIND, {  # noqa: SLF001
        "rows_fn": deps.package_rows, "block": deps.BLOCK_CODES, "hold": frozenset(),
        "version": deps.CHECKER_VERSION})
    monkeypatch.setattr(deps, "_is_windows", lambda: True)
    from puffpai.security import host_programs
    real = host_programs.path
    fake_winget = os.path.join(os.sep, "fake", "WindowsApps", "winget.exe")
    monkeypatch.setattr(host_programs, "path",
                        lambda name: fake_winget if name == "winget" else real(name))
    monkeypatch.setattr(deps, "has_program", lambda name: False)
    ran: list = []

    def fake(argv, extra_env=None):
        ran.append({"argv": list(argv), "env": {**host_programs.run_kwargs()["env"],
                                                 **(extra_env or {})}})
        return {"code": 0, "output": "ok"}
    monkeypatch.setattr(deps, "_run_installer", fake)
    deps._PLANS.clear()  # noqa: SLF001
    yield {"ran": ran, "winget": fake_winget}
    deps._PLANS.clear()  # noqa: SLF001


# ── seam 1: install_missing is the dependency installer ───────────────────────────

def test_docker_installs_with_the_one_yes_and_is_the_owners_where_the_bot_cannot(
        tmp_path, monkeypatch, installer):
    # Docker is on the bot's own install list (deps installs Docker and app
    # runtimes): on Windows it is one winget step under the owner's one yes.
    monkeypatch.setenv("PUFFPAI_STATE_DIR", str(tmp_path / "state"))
    hook = run_here.install_missing({"integration": "x"}, ["docker"])
    assert hook["ok"] is True and "needs Docker" in hook["line"]
    assert hook["card"]["steps"] == ["Docker Desktop, with Windows' own installer (winget)."]
    assert hook["card"]["manual"] == []
    assert installer["ran"] == []                       # nothing before the owner's yes
    texts = [hook["line"], *hook["card"]["steps"]]
    # Where no installer can take it (here Linux without admin), no plan is
    # made: the owner installs it themself, with Docker's own page named.
    monkeypatch.setattr(deps, "_is_windows", lambda: False)
    monkeypatch.setattr(deps.sys, "platform", "linux")
    monkeypatch.setattr(deps, "_is_root", lambda: False)
    deps._PLANS.clear()  # noqa: SLF001
    hook = run_here.install_missing({"integration": "x"}, ["docker"])
    assert hook["ok"] is False and "needs Docker" in hook["line"]
    assert hook["card"]["plan"] == "" and hook["card"]["manual"]
    assert "Install it yourself" in hook["line"]
    assert deps.SYSTEM_PROGRAM_LINKS["docker"] in hook["card"]["manual"][0]
    assert installer["ran"] == []
    for text in [*texts, hook["line"], *hook["card"]["manual"]]:
        assert "—" not in text and "door" not in text.lower()


def test_the_find_preview_carries_the_same_card(world, monkeypatch, installer):
    from test_integrate_run_here import node_tree
    from puffpai.finding import install
    h = world(node_tree, mode="web_build", name="piano-coach",
              item="apps_code:github:someone/piano-coach")
    monkeypatch.setattr(run_here, "_host_program", lambda name: None)
    monkeypatch.setattr(run_here, "_npm_cli", lambda: None)
    from puffpai.integrate import apply
    out = apply.prepare_code_project(h["item"], __import__("integrate_support").Ctx())
    assert out["card"]["host"]["missing"] == ["node"]
    got = apply.finish_prepared(out["integration_id"], code_sha=out["card"]["code_sha"],
                                host_sha=out["card"]["host_sha"])
    assert got["status"] == "held" and "Install what's missing" in got["line"]
    # The held app's own page shows the card too.
    needs = things.host_needs(out["integration_id"])
    assert needs["programs_missing"] == ["Node.js"] and needs["install"]["plan"]

    fake_f = {"id": "f1", "name": "piano-coach"}
    monkeypatch.setattr(apply, "prepared_card", lambda iid: out["card"])
    got = install._prepare_app({**fake_f, "prepared": {"iid": out["integration_id"]}})  # noqa: SLF001
    assert got["needs"] == ["node"] and got["missing"]["plan"]


# ── seam 2: cli_dirs reach run_command, last ────────────────────────────────

def test_a_host_apps_cli_folder_is_last_on_a_commands_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_STATE_DIR", str(tmp_path / "state"))
    iid = "tiny-cli-abcdefgh"
    run_here._save_state(iid, {"plan": {"how": "python_cli"}})  # noqa: SLF001
    bin_dir = os.path.dirname(run_here._venv_bin(iid, "python"))  # noqa: SLF001
    os.makedirs(bin_dir)
    tool = os.path.join(bin_dir, "chordify" + (".exe" if os.name == "nt" else ""))
    with open(tool, "wb") as fh:
        fh.write(b"")
    if os.name != "nt":
        os.chmod(tool, 0o755)
    assert run_here.cli_dirs() == [bin_dir]
    assert deps.command_paths()["host_path"] == [bin_dir]
    env = brain_commands.command_env()
    path = next(v for k, v in env.items() if k.upper() == "PATH").split(os.pathsep)
    assert path[-1] == bin_dir and path[0] == brain_commands.shim_dir()
    assert deps.has_program("chordify")


# ── seam 3: an app's own keys ───────────────────────────────────────────────

def test_keys_named_in_a_readme_are_read_with_a_get_a_key_link(tmp_path):
    src = str(tmp_path / "p")
    _keyed_tree(src)
    plan = run_here.detect(src, port=45002)
    assert plan["keys"] == ["GEMINI_API_KEY"]        # never PUFFPAI_* or NODE_OPTIONS
    assert deps.get_url_for("GEMINI_API_KEY", README) == \
        "https://aistudio.google.com/app/apikey"
    text = "\n".join(run_here.card_lines(plan, "tiny-tutor-abcdefgh"))
    assert "GEMINI_API_KEY" in text and "only this app gets them" in text
    assert "—" not in text and "door" not in text.lower()


def test_a_docker_project_never_gets_a_key(world, vault, monkeypatch, tmp_path):
    iid = "compose-app-abcdefgh"
    run_here._save_state(iid, {"plan": {"how": "compose", "keys": ["GEMINI_API_KEY"]}})  # noqa: SLF001
    monkeypatch.setattr(things, "get", lambda tid: {"id": tid, "kind": "app", "ref": iid,
                                                    "name": "Boxy"})
    from puffpai.integrate import records
    monkeypatch.setattr(records, "get", lambda rid: {"id": rid, "mode": "own_server"})
    assert things.put_key("app-x", "GEMINI_API_KEY", GEMINI)["ok"] is False
    assert run_here.key_rows(iid) == []
    assert run_here.app_keys(iid, {"how": "compose", "keys": ["GEMINI_API_KEY"]}) == {}


# ── the routine check reads needs with deps ──────────────────────────

def _skill(folder, text):
    path = os.path.join(skills.skills_dir(create=True), folder)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(text)


def test_a_routine_skill_is_checked_with_the_requirements_reader(
        tmp_path, monkeypatch, vault):
    monkeypatch.setenv("PUFFPAI_STATE_DIR", str(tmp_path / "state"))
    _skill("piano-journal", "---\nname: piano-journal\ndescription: A piano practice "
           "journal.\n---\ncurl -H \"Authorization: Bearer $BLUECOLUMN_API_KEY\" ...\n")
    _skill("piano-sound", "---\nname: piano-sound\ndescription: Piano lesson sounds."
           "\n---\n```bash\npip install librosa\n```\n")
    monkeypatch.setattr(deps, "has_package", lambda kind, name: False)
    skill, why = routines.skill_fit("piano-journal", "daily piano lesson")
    assert skill is None and "BLUECOLUMN_API_KEY" in why     # named only in its text
    keys.put_skill_key("piano-journal", "BLUECOLUMN_API_KEY", GEMINI,
                       declared=deps.key_names("piano-journal"))
    assert routines.skill_fit("piano-journal", "daily piano lesson") == ("piano-journal", "")
    skill, why = routines.skill_fit("piano-sound", "daily piano lesson")
    assert skill is None and "librosa" in why
    monkeypatch.setattr(deps, "has_package", lambda kind, name: True)
    assert routines.skill_fit("piano-sound", "daily piano lesson") == ("piano-sound", "")

