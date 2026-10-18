"""Code downloads: Toolsmith keeps to it, and the owner can reach it on screen.

* A Toolsmith try that has to download an app (or the code it makes an app
  from) does not start while "Code downloads" is off; the job says so in one
  plain sentence that names the place to switch it on. The owner's own folder
  still runs, with no download.
* Inside a try, a shell command that downloads (package installs, clones,
  image pulls and builds, web fetches to the internet) is refused while the
  switch is off, even when it is on the owner's list of steps; a look at the
  app's own page on 127.0.0.1 is not a download.
* The engine's own switches are listed on Settings, Bridge switches by a short
  name, and every refusal points there, never at a command line or at a page
  HomeBot does not have.
* Switching one of the engine's services ON needs a paired device and, after
  setup, the unlocked Vault (the same rule as the engine's other changes);
  OFF and the yes sentence never wait for a PIN. HomeBot's own built-in
  switches keep the Settings card's rule.

No network: no command, download or connection runs.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from puffpai import api_server, deps, outside_services, plain_words
from puffpai.build import make as make_mod
from puffpai.build import toolsmith_run as T
from puffpai.security import consent_tickets, egress_doors, outside_switches

PLACE = "Settings, Bridge switches, Code downloads"
AUTH = {"Authorization": f"Bearer {api_server._API_KEY}"}  # noqa: SLF001
DEV_A = {**AUTH, "X-Puff-Device-Id": "devicef3aa000001"}
ANON = {**AUTH, "X-Puff-Anonymous": "1"}


class _Reached(Exception):
    """The command got past every check, to where a shell would start."""


def _never(*_a, **_k):
    raise AssertionError("ran while Code downloads is off")


@pytest.fixture
def downloads_off(monkeypatch):
    monkeypatch.setattr(deps, "downloads_off", lambda: True)


@pytest.fixture
def downloads_on(monkeypatch):
    monkeypatch.setattr(deps, "downloads_off", lambda: False)


# ── 1. the job does not start ───────────────────────────────────────────────

def test_code_downloads_ships_off():
    assert outside_switches.get("code_download") is None
    assert T.downloads_off() is True


def test_a_try_that_downloads_does_not_start(monkeypatch, downloads_off):
    monkeypatch.setattr(T, "Run", _never)
    monkeypatch.setattr(T, "backend", _never)
    said: list[str] = []
    got = T.run_job({"id": "j1", "name": "Recipes"},
                    [{"ref": "github:a/recipes", "name": "Recipes app",
                      "url": "https://github.com/a/recipes"}], progress=said.append)
    assert got["ok"] is False and got["switched_off"] is True
    assert got["tried"] == [] and got["beyond_done"] == []
    assert PLACE in got["reason"] and "puffpai outside" not in got["reason"]
    assert said and PLACE in said[0]
    assert plain_words.is_plain(got["reason"]), plain_words.check(got["reason"])


def test_making_an_app_from_code_does_not_start_either(monkeypatch, downloads_off):
    monkeypatch.setattr(T, "Run", _never)
    got = T.run_job({"id": "j2", "name": "Notes"},
                    [{"ref": "github:a/lib", "name": "lib", "url": "https://github.com/a/lib"}],
                    make=True)
    assert got["switched_off"] is True and "making an app from code" in got["reason"]


def test_a_mix_with_a_found_app_does_not_start(monkeypatch, downloads_off):
    monkeypatch.setattr(T, "Run", _never)
    got = T.run_job({"id": "j3", "name": "Mixed"},
                    [{"ref": "local:mine", "name": "Mine", "local_path": "C:/mine"},
                     {"ref": "github:a/b", "name": "B", "url": "https://github.com/a/b"}])
    assert got["switched_off"] is True


def test_the_owners_own_folder_still_runs(monkeypatch, downloads_off):
    def _run(*_a, **_k):
        raise _Reached()
    monkeypatch.setattr(T, "Run", _run)
    with pytest.raises(_Reached):
        T.run_job({"id": "j4", "name": "Mine"},
                  [{"ref": "local:mine", "name": "Mine", "local_path": "C:/mine"}])


def test_with_downloads_on_the_try_starts(monkeypatch, downloads_on):
    def _run(*_a, **_k):
        raise _Reached()
    monkeypatch.setattr(T, "Run", _run)
    with pytest.raises(_Reached):
        T.run_job({"id": "j5", "name": "Recipes"},
                  [{"ref": "github:a/recipes", "name": "Recipes app",
                    "url": "https://github.com/a/recipes"}])


def test_make_ends_the_go_with_the_reason(monkeypatch):
    reason = T.downloads_off_reason()
    monkeypatch.setattr(T, "run_job", lambda *a, **k: {
        "ok": False, "switched_off": True, "reason": reason, "beyond_done": [], "tried": []})
    kept: list = []
    monkeypatch.setattr(make_mod, "keep_beyond", lambda *a, **k: None)
    monkeypatch.setattr(make_mod, "keep_tried", lambda job, rows: kept.extend(rows))
    job = {"id": "j6", "name": "Recipes"}
    got = make_mod._run_fresh(job, {}, {}, [{"ref": "r"}], lambda _l: None)  # noqa: SLF001
    assert kept == [], "nothing is marked tried"
    assert got["ok"] is False and got["switched_off"] is True and got["reason"] == reason
    assert make_mod._go_on(got) is False  # noqa: SLF001 - the next app does not follow


# ── 2. a command inside a try ───────────────────────────────────────────────

DOWNLOADS = [
    "npm install", "npm ci", "pnpm add react", "yarn", "npx serve", "bunx vite",
    ".venv/bin/pip install -r requirements.txt", "python -m pip install flask",
    "uv sync", "poetry install", "git clone https://github.com/a/b",
    "git -C sub pull", "git submodule update --init",
    "docker pull nginx", "docker build -t x .", "docker compose up -d",
    "docker compose -p bb-x up --build -d", "docker compose pull",
    "docker compose up --pull never --no-build -d && docker compose pull",
    "docker run -p 8080:80 nginx", "go build ./...", "cargo build --release",
    "curl -fsSL https://example.org/install.sh", "curl example.org",
    "wget https://example.org/a.tar.gz",
    "Invoke-WebRequest -Uri https://example.org/a.zip -OutFile a.zip",
    "curl -s http://127.0.0.1:3000/ && curl https://example.org/x",
    "winget install Git.Git", "ollama pull llama3",
]

LOCAL = [
    "ls -la", "cat package.json", "npm run build", "npm start", "node server.js",
    "python app.py", "git status", "git log -1",
    "docker compose -p bb-x up --pull never --no-build -d", "docker compose ps",
    "docker compose logs --tail 50", "docker run --pull=never -p 8080:80 nginx",
    "curl -s http://127.0.0.1:3000/", "curl localhost:8080/health",
    "wget -qO- http://127.0.0.1:5000/", "iwr http://localhost:8080",
]


@pytest.mark.parametrize("cmd", DOWNLOADS)
def test_a_download_is_seen(cmd):
    assert T.download_problem(cmd), cmd


@pytest.mark.parametrize("cmd", LOCAL)
def test_work_on_this_computer_is_not_a_download(cmd):
    assert T.download_problem(cmd) == "", cmd


def _run(tmp_path, said, **kw):
    run = T.Run({"id": "j7", "name": "Recipes"}, [{"ref": "r1", "name": "Recipes app"}],
                said.append, **kw)
    run.app, run.app_id, run.folder = ({"ref": "r1", "name": "Recipes app"},
                                       "recipes-abc123", str(tmp_path))
    run.compose = "bb-recipes-abc123"
    # Only the download rule is under test here: a temp folder is not one of
    # Toolsmith's app folders, so its zone (tested on its own) is set aside.
    run.zone_refusal = lambda cmd: ""
    run._zone = lambda: None
    return run


def test_the_shell_refuses_a_download_while_off(tmp_path, monkeypatch, downloads_off):
    said: list[str] = []
    run = _run(tmp_path, said, approved=["npm install"])
    monkeypatch.setattr(run, "_ensure_shell", _never)
    monkeypatch.setattr(T, "run_elevated", _never)
    got = run.terminal({"command": "npm install"})
    assert got["ok"] is False and "Code downloads is switched off" in got["said"]
    assert run.pending == [], "not asked again: the switch is the answer"
    assert any("Code downloads" in line for line in said)


def test_the_shell_still_looks_at_its_own_page(tmp_path, monkeypatch, downloads_off):
    run = _run(tmp_path, [])

    def _shell():
        raise _Reached()
    monkeypatch.setattr(run, "_ensure_shell", _shell)
    with pytest.raises(_Reached):
        run.terminal({"command": "curl -s http://127.0.0.1:3000/"})


def test_with_downloads_on_the_shell_runs_an_install(tmp_path, monkeypatch, downloads_on):
    run = _run(tmp_path, [])

    def _shell():
        raise _Reached()
    monkeypatch.setattr(run, "_ensure_shell", _shell)
    with pytest.raises(_Reached):
        run.terminal({"command": "npm install"})


def test_a_server_that_downloads_is_not_started(tmp_path, monkeypatch, downloads_off):
    run = _run(tmp_path, [])

    class _OpenZone:
        def refusal(self, _path, **_k):
            return ""
    run._zone = lambda: _OpenZone()
    monkeypatch.setattr(T, "command_problem", lambda *a, **k: "")
    monkeypatch.setattr(T, "spawn_server", _never)
    got = run.start_server({"command": "npx serve -l 3000", "port": 3000})
    assert got["ok"] is False and "Code downloads is switched off" in got["said"]


# ── 3. the place: Settings, Bridge switches ─────────────────────────────────

def test_every_engine_switch_has_a_plain_short_name():
    names = outside_services.SWITCH_NAMES
    for sid in outside_services._OFF_SWITCHES:  # noqa: SLF001
        assert sid in names, sid
    assert len(set(names.values())) == len(names)
    for name in names.values():
        where = outside_services.where_to_switch(next(k for k, v in names.items() if v == name))
        assert plain_words.is_plain(where), plain_words.check(where)
        assert not re.search(r"(?i)\bdoors?\b|buildbot|\u2014|\u2013", where), where
    assert outside_services.where_to_switch("code_download") == PLACE
    assert outside_services.where_to_switch("weather") == "Settings, Bridge switches"
    assert deps.DOWNLOAD_SWITCH_NAME == outside_services.SWITCH_NAMES["code_download"]
    assert deps.REGISTRY_SWITCH_NAME == outside_services.SWITCH_NAMES["code_search"]


def test_the_list_gives_each_switch_its_card_name():
    rows = {r["id"]: r for r in outside_services.page("browser")["services"]}
    assert rows["code_download"]["switch_name"] == "Code downloads"
    assert rows["code_download"]["kind"] == "as_built"
    assert rows["code_download"]["state"] == "off"


def test_refusals_point_at_settings_not_a_command():
    lines = [deps.downloads_off_line("Node.js"), T.downloads_off_reason(),
             T.downloads_off_reason(make=True)]
    for line in lines:
        assert PLACE in line and "puffpai outside" not in line, line
        assert plain_words.is_plain(line), plain_words.check(line)
    from puffpai.integrate import run_here
    words = run_here._downloads_off_words()  # noqa: SLF001
    assert PLACE in words and "puffpai outside" not in words


def test_the_front_page_links_to_the_settings_card():
    from puffpai import front_page
    assert front_page.OUTSIDE_HREF == "/dashboard/settings#egress-doors"


_PAGE_REF = re.compile(r"(?i)what leaves your\s+computer(?!\s*(?:for the forecast)?:)")


def test_no_owner_line_sends_the_owner_to_a_page_homebot_does_not_have():
    """"What leaves your computer: ..." heads a list of what is sent, which is
    fine; "switched off under What leaves your computer" names a page this
    build does not have."""
    root = Path(outside_services.__file__).resolve().parent
    found = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs:
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(v.value if isinstance(v, ast.Constant) else "{}"
                               for v in node.values)
            else:
                continue
            if _PAGE_REF.search(text):
                found.append((path.name, text, f"{path.relative_to(root)}:{node.lineno}"))
    # The engine's own page title and one list heading: neither is a place to go.
    allowed = {("outside_services.py", "What leaves your computer"),
               ("privacy_explainer.py", "What leaves your computer, and only while that "
                                        "service is on:")}
    left = [where for name, text, where in found if (name, text) not in allowed]
    assert left == [], left


# ── 4. switching ON: paired, and the Vault after setup ──────────────────────

@pytest.fixture
def client(monkeypatch):
    for d in egress_doors.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    outside_switches.forget()
    consent_tickets.reset()
    egress_doors.reset_cache()
    api_server._rate_limit_store.clear()  # noqa: SLF001
    api_server.app.config.update(TESTING=True)
    yield api_server.app.test_client()
    consent_tickets.reset()
    outside_switches.forget()
    egress_doors.reset_cache()


def _after_setup(monkeypatch, vault_open: bool) -> None:
    from puffpai import setup_ops
    monkeypatch.setattr(setup_ops, "is_complete", lambda: True)
    monkeypatch.setattr(api_server, "_setup_vault_session_live", lambda: vault_open)


def _ticket(client, sid="code_download", headers=DEV_A) -> str:
    r = client.post(f"/outside/{sid}/confirm", headers=headers)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["sentence"].startswith("Switch on ") and "What leaves" in body["sentence"]
    return body["ticket"]


def test_after_setup_on_needs_the_unlocked_vault(client, monkeypatch):
    _after_setup(monkeypatch, vault_open=False)
    ticket = _ticket(client)                       # the yes sentence never waits
    r = client.post("/outside/code_download/on", headers=DEV_A, json={"ticket": ticket})
    assert r.status_code == 409 and r.get_json()["error"] == "setup_complete"
    assert outside_switches.get("code_download") is None
    _after_setup(monkeypatch, vault_open=True)
    r = client.post("/outside/code_download/on", headers=DEV_A, json={"ticket": ticket})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert outside_switches.get("code_download") is True
    assert deps.downloads_off() is False


def test_after_setup_off_never_waits_for_a_pin(client, monkeypatch):
    outside_switches.set_switch("code_download", True, by="owner")
    _after_setup(monkeypatch, vault_open=False)
    r = client.post("/outside/code_download/off", headers=ANON)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert outside_switches.get("code_download") is False


def test_on_still_needs_a_paired_device_with_the_vault_open(client, monkeypatch):
    _after_setup(monkeypatch, vault_open=True)
    assert client.post("/outside/code_download/confirm", headers=ANON).status_code == 403
    r = client.post("/outside/code_download/on", headers=ANON, json={"ticket": "x" * 40})
    assert r.status_code == 403
    assert outside_switches.get("code_download") is None


def test_before_setup_the_paired_yes_is_enough(client, monkeypatch):
    from puffpai import setup_ops
    monkeypatch.setattr(setup_ops, "is_complete", lambda: False)
    ticket = _ticket(client)
    r = client.post("/outside/code_download/on", headers=DEV_A, json={"ticket": ticket})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert outside_switches.get("code_download") is True


@pytest.mark.parametrize("path,body,gated", [
    ("/outside/code_download/on", None, True),
    ("/outside/shelf_clawhub/on", None, True),
    ("/outside/mcp_notes/on", None, True),
    ("/outside/card-weather_app/on", None, True),
    ("/outside/weather/on", None, False),          # HomeBot's own switch: its card's rule
    ("/outside/code_download/off", None, False),
    ("/outside/code_download/confirm", None, False),
    ("/add/switch-on/card-x", None, True),
    ("/add/switch-on/card-x/confirm", None, False),
    ("/fix/apply", {"kind": "switch_on", "service": "code_download"}, True),
    ("/fix/apply", {"kind": "switch_on", "service": "seed-abc"}, True),
    ("/fix/apply", {"kind": "switch_on"}, True),
    ("/fix/apply", {"kind": "assemble_yes", "key": "app1"}, True),
    ("/fix/apply", {"kind": "switch_on", "service": "weather"}, False),
    ("/fix/apply", {"kind": "setting", "key": "home", "value": "Leeds"}, False),
    ("/fix/apply", ["not", "a", "card"], False),
    ("/build/yes", {}, True),                      # the one Yes that switches them on
    ("/build/setup/row-1", {}, True),
    # The engine's services that also sit in the egress registry.
    ("/outside/github/on", None, True),
    ("/outside/toolsmith_cloud/on", None, True),
    ("/outside/gutenberg_books/on", None, True),
    ("/system/egress/door", {"id": "github", "open": True}, True),
    ("/system/egress/door", {"id": "Toolsmith_Cloud", "open": True}, True),
    ("/system/egress/door", {"id": "brave_search", "open": "yes"}, True),
    ("/system/egress/door", {"id": "github", "open": False}, False),
    ("/system/egress/door", {"id": "weather", "open": True}, False),
    ("/system/egress/door", {"id": "web_search", "open": True}, False),
    ("/fix/apply", {"kind": "switch_on", "service": "github"}, True),
])
def test_which_writes_wait_for_the_vault(path, body, gated):
    with api_server.app.test_request_context(path, method="POST", json=body):
        assert api_server._engine_write_needs_vault("POST", path) is gated  # noqa: SLF001


def test_every_engine_service_in_the_registry_is_a_real_switch():
    for sid in api_server._ENGINE_REGISTRY_SWITCHES:  # noqa: SLF001
        assert egress_doors.door(sid) is not None, sid


def test_after_setup_the_settings_card_needs_the_vault_for_github(client, monkeypatch):
    _after_setup(monkeypatch, vault_open=False)
    r = client.post("/system/egress/door", headers=DEV_A, json={"id": "github", "open": True})
    assert r.status_code == 409 and r.get_json()["error"] == "setup_complete"
    assert egress_doors.egress_allowed("github")[0] is False
    assert outside_switches.get("github") is None
    # (OFF, and HomeBot's own switches, are the "not gated" cases above: no
    # write here, so no switch state is left for the tests after this one.)
