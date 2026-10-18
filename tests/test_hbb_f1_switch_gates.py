"""Downloads, image pulls, the older switch route, the Home Assistant name
and the recorded reach, each held to the Bridge's rules.

* A program for the whole computer (winget, apt-get, Docker's installer) is
  installed only while the download switch is on; with it off nothing runs
  and the owner is told which switch and how to turn it on.
* A Docker app starts from the images already on this computer while that
  switch is off: Docker itself is told not to pull or build.
* POST /system/egress/door switches an outside service ON only for a paired
  device (the same rule as /outside/<id>/on); OFF works for every caller with
  the key; both switch stores say the same thing afterwards.
* A ``.local`` Home Assistant name that also answers with an address outside
  the house is accepted when one answer is inside it, and the token goes to
  that inside answer only. Every other refusal stays.
* The browser setup records the same run-time reach as the console preset,
  with every outside switch off.

No network: every lookup, installer, Docker call and connection is stubbed.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

import pytest

from puffpai import api_server, deps
from puffpai.integrate import run_here
from puffpai.security import egress_doors, netaddr, outside_switches

# The switch by its name on HomeBot's Settings card, and the place to switch it
# on (lane F3: refusals point at Settings, never at a command line).
SWITCH_WORDS = "Code downloads is switched off"
HOW_WORDS = "go to Settings, Bridge switches, Code downloads"
TOKEN = "llat-f1-test-token-never-sent-outside"


def _auth() -> dict:
    return {"Authorization": f"Bearer {api_server._API_KEY}"}  # noqa: SLF001


def _never(*_a, **_k):
    raise AssertionError("ran while the download switch is off")


# ── A. programs for the whole computer ──────────────────────────────────────

@pytest.fixture
def no_failures(monkeypatch):
    monkeypatch.setattr(deps, "_FAILED", {})


def test_the_download_switch_ships_off():
    assert outside_switches.get(deps.DOWNLOAD_SERVICE) is None
    assert deps.downloads_off() is True


def test_a_program_is_not_installed_while_downloads_are_off(monkeypatch, no_failures):
    monkeypatch.setattr(deps, "_system_step",
                        lambda name: {"kind": "winget", "name": "OpenJS.NodeJS.LTS"})
    monkeypatch.setattr(deps, "installer_argv", _never)
    monkeypatch.setattr(deps, "_run_installer", _never)
    said = []
    got = deps.install_program("node", said.append)
    assert got["ok"] is False and got["code"] == "switched_off"
    assert SWITCH_WORDS in got["line"] and HOW_WORDS in got["line"]
    assert said == [got["line"]]
    assert "node" not in deps._FAILED, "a refusal is not remembered as a failure"  # noqa: SLF001


def test_docker_is_not_installed_while_downloads_are_off(monkeypatch, no_failures):
    monkeypatch.setattr(deps, "_docker_path", lambda: None)
    monkeypatch.setattr(deps, "_install_docker", _never)
    monkeypatch.setattr(deps, "_run_installer", _never)
    got = deps.install_program("docker")
    assert got["ok"] is False and SWITCH_WORDS in got["line"]


def test_docker_already_here_is_only_started_with_downloads_off(monkeypatch, no_failures):
    monkeypatch.setattr(deps, "_docker_path", lambda: "C:/docker/docker.exe")
    monkeypatch.setattr(deps, "_install_docker", _never)
    monkeypatch.setattr(deps, "ensure_docker_running",
                        lambda progress: {"ok": True, "line": "Docker is running."})
    assert deps.install_program("docker")["ok"] is True


def test_with_downloads_on_the_program_is_installed(monkeypatch, no_failures):
    ran = []
    monkeypatch.setattr(deps, "downloads_off", lambda: False)
    monkeypatch.setattr(deps, "_system_step",
                        lambda name: {"kind": "winget", "name": "OpenJS.NodeJS.LTS"})
    monkeypatch.setattr(deps, "installer_argv", lambda step, v, target=None: ["winget"])
    monkeypatch.setattr(deps, "_run_installer",
                        lambda argv, extra=None: ran.append(argv) or {"code": 0, "output": ""})
    assert deps.install_program("node")["ok"] is True and ran == [["winget"]]


def test_an_install_card_step_is_refused_while_downloads_are_off(monkeypatch):
    monkeypatch.setattr(deps, "_run_installer", _never)
    monkeypatch.setattr(deps, "check_step", _never)
    pid = "f1-plan-winget"
    with deps._PLANS_LOCK:  # noqa: SLF001
        deps._PLANS[pid] = {  # noqa: SLF001
            "id": pid, "made_at": time.time(), "skill": "", "thing": "f1-thing",
            "steps": [{"kind": "winget", "name": "OpenJS.NodeJS.LTS",
                       "label": "Node.js", "version": ""}]}
    got = deps.run_plan(pid, said_yes=True)
    assert got["ok"] is False and got["failed"] == ["OpenJS.NodeJS.LTS"]
    assert any(SWITCH_WORDS in line and HOW_WORDS in line for line in got["lines"])


def test_toolsmith_runs_no_installer_while_downloads_are_off(tmp_path, monkeypatch):
    from puffpai.build import toolsmith_run as T
    said = []
    run = T.Run({"id": "j1", "name": "News"}, [{"ref": "r1", "name": "Reader"}],
                said.append, programs=["Ruby"])
    run.app, run.app_id, run.folder = ({"ref": "r1", "name": "Reader"},
                                       "reader-abc123", str(tmp_path))
    monkeypatch.setattr(T, "_tool_here", lambda prog: False)
    monkeypatch.setattr(T, "_os_key", lambda: "nt")
    monkeypatch.setattr(T, "_program", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(T.subprocess, "run", _never)
    monkeypatch.setattr(T, "winget_search", _never)
    monkeypatch.setattr(T, "run_elevated", _never)
    monkeypatch.setattr(deps, "install_program", _never)
    got = run.install_program({"program": "Ruby", "package_id": "RubyInstallerTeam.Ruby.3.3"})
    assert got["ok"] is False
    assert SWITCH_WORDS in got["said"] and HOW_WORDS in got["said"]
    assert run.pending == []


# ── B. Docker images ────────────────────────────────────────────────────────

_UP = ["C:/docker/docker.exe", "compose", "-f", "buildbot-compose.json", "-p",
       "bb-app-fxaaaaaa", "up", "--build", "-d"]


@pytest.fixture
def compose_run(monkeypatch):
    ran = []
    state = {"code": 0}

    class _Done:
        def __init__(self, code):
            self.returncode = code

    def _run(argv, **_k):
        ran.append(list(argv))
        return _Done(state["code"])

    monkeypatch.setattr(run_here, "_run", _run)
    return ran, state


def test_with_downloads_off_compose_uses_only_local_images(compose_run):
    ran, _state = compose_run
    got = run_here._compose_start("app-fxaaaaaa", {"compose_ports": []},  # noqa: SLF001
                                  list(_UP), {}, by="owner")
    assert got["ok"] is True
    argv = ran[-1]
    assert "--build" not in argv
    at = argv.index("up")
    assert argv[at + 1:at + 4] == ["--no-build", "--pull", "never"]


def test_with_downloads_off_a_failed_start_names_the_switch(compose_run):
    ran, state = compose_run
    state["code"] = 1
    got = run_here._compose_start("app-fxaaaaab", {}, list(_UP), {},  # noqa: SLF001
                                  by="owner")
    assert got["ok"] is False and got["code"] == "bridge"
    assert SWITCH_WORDS in got["line"] and HOW_WORDS in got["line"]
    assert "--pull" in ran[-1]


def test_with_downloads_on_compose_builds_and_pulls_as_before(compose_run, monkeypatch):
    ran, _state = compose_run
    monkeypatch.setattr(run_here, "_bridge_open", lambda: True)
    assert run_here._compose_start("app-fxaaaaac", {}, list(_UP), {},  # noqa: SLF001
                                   by="owner")["ok"] is True
    assert ran[-1] == _UP


def test_a_compose_command_it_cannot_make_local_is_refused(compose_run):
    ran, _state = compose_run
    odd = ["docker", "compose", "-f", "x.json", "pull"]
    got = run_here._compose_start("app-fxaaaaad", {}, odd, {}, by="owner")  # noqa: SLF001
    assert got["ok"] is False and SWITCH_WORDS in got["line"] and ran == []
    assert run_here._local_images_argv(["docker", "compose", "up", "--pull",  # noqa: SLF001
                                        "always"]) is None


# ── D. POST /system/egress/door ─────────────────────────────────────────────

ENGINE_SWITCHES = ("github", "toolsmith_cloud", "web_search", "model_pull")


@pytest.fixture
def doors(tmp_path, monkeypatch):
    cfg = tmp_path / "puffpai_config.json"
    cfg.write_text(json.dumps({"egress_doors": {}}), encoding="utf-8")
    overlay = tmp_path / "state" / "egress_doors.json"
    monkeypatch.setattr(egress_doors, "_config_path", lambda: str(cfg))
    monkeypatch.setattr(egress_doors, "_ledger_path",
                        lambda: str(tmp_path / "egress_last_used.json"))
    egress_doors.set_overlay_override(str(overlay))
    egress_doors.reset_cache()
    outside_switches.forget()
    api_server._rate_limit_store.clear()  # noqa: SLF001
    api_server.app.config.update(TESTING=True)
    try:
        yield overlay
    finally:
        egress_doors.set_overlay_override(None)
        egress_doors.reset_cache()


def _overlay(path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("doors") or {}


def _agree(sid: str) -> None:
    """Both stores, and both readers, say the same thing about *sid*."""
    egress_doors.reset_cache()
    outside_switches.forget()
    allowed, _why = egress_doors.egress_allowed(sid)
    assert outside_switches.is_off(sid) is (not allowed), sid


@pytest.mark.parametrize("sid", ENGINE_SWITCHES)
@pytest.mark.parametrize("how", ["anonymous", "no_identity", "lan_direct"])
def test_an_unpaired_caller_cannot_switch_a_service_on(doors, sid, how):
    headers = dict(_auth())
    environ = {"REMOTE_ADDR": "127.0.0.1"}
    if how == "anonymous":
        headers["X-Puff-Anonymous"] = "1"          # what the proxy sends unpaired
    elif how == "lan_direct":
        headers["X-Puff-Device-Id"] = "b" * 32     # not believed off loopback
        environ = {"REMOTE_ADDR": "192.168.1.50"}
    resp = api_server.app.test_client().post(
        "/system/egress/door", headers=headers, json={"id": sid, "open": True},
        environ_base=environ)
    assert resp.status_code == 403, resp.get_data(as_text=True)
    assert "paired" in resp.get_json()["error"]
    assert not doors.exists(), "nothing written"
    assert outside_switches.get(sid) is None
    assert egress_doors.egress_allowed(sid)[0] is False
    assert outside_switches.is_off(sid) is True


@pytest.mark.parametrize("sid", ENGINE_SWITCHES)
def test_a_paired_screen_can_switch_it_on_and_both_stores_agree(doors, sid):
    headers = {**_auth(), "X-Puff-Device-Id": "c" * 32}
    resp = api_server.app.test_client().post(
        "/system/egress/door", headers=headers, json={"id": sid, "open": True})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    flag = egress_doors.door(sid).config_flag
    assert _overlay(doors)[flag] is True
    assert outside_switches.get(sid) is True
    _agree(sid)


@pytest.mark.parametrize("sid", ENGINE_SWITCHES)
def test_switching_off_works_for_any_caller_with_the_key(doors, sid):
    client = api_server.app.test_client()
    on = client.post("/system/egress/door", json={"id": sid, "open": True},
                     headers={**_auth(), "X-Puff-Device-Id": "c" * 32})
    assert on.status_code == 200
    off = client.post("/system/egress/door", json={"id": sid, "open": False},
                      headers={**_auth(), "X-Puff-Anonymous": "1"})
    assert off.status_code == 200, off.get_data(as_text=True)
    assert _overlay(doors)[egress_doors.door(sid).config_flag] is False
    assert outside_switches.get(sid) is False
    assert egress_doors.egress_allowed(sid)[0] is False
    _agree(sid)


def test_switching_off_on_the_outside_list_shuts_the_settings_switch_too(doors):
    from puffpai import outside_services
    client = api_server.app.test_client()
    assert client.post("/system/egress/door", json={"id": "github", "open": True},
                       headers={**_auth(), "X-Puff-Device-Id": "c" * 32}
                       ).status_code == 200
    outside_services.switch_off("github", by="owner")
    assert _overlay(doors)[egress_doors.door("github").config_flag] is False
    assert outside_switches.get("github") is False
    _agree("github")


def test_an_owner_switch_set_elsewhere_is_copied_to_an_overlay_answer(doors):
    from puffpai.security import egress_admin
    client = api_server.app.test_client()
    client.post("/system/egress/door", json={"id": "web_search", "open": False},
                headers=_auth())
    outside_switches.set_switch("web_search", True, by="owner")
    egress_admin.sync_overlay("web_search", True)
    assert _overlay(doors)[egress_doors.door("web_search").config_flag] is True
    _agree("web_search")


# ── F. the Home Assistant address ───────────────────────────────────────────

GLOBAL_V6 = "2a02:8070:4a1:9e00::5"


@pytest.fixture
def ha(monkeypatch):
    from puffpai import build_manifest, setup_ops
    from puffpai.security import lan_posture
    monkeypatch.setattr(build_manifest, "read_manifest",
                        lambda *a, **k: {"posture": {"run_egress": "enumerated"}})
    answers: dict = {}

    def _resolve(host, port, *_a, **_k):
        got = answers.get(str(host).rstrip(".").lower())
        if not got:
            raise netaddr.ResolutionError(f"cannot resolve {host!r}")
        return list(got)

    monkeypatch.setattr(netaddr, "default_resolver", _resolve)
    monkeypatch.setattr(lan_posture, "_resolver_override", _resolve)
    dialled: list = []

    def _connect(address, *_a, **_k):
        dialled.append(tuple(address))
        raise ConnectionRefusedError("nothing listening in a unit test")

    import urllib3.util.connection as u3
    monkeypatch.setattr(u3, "create_connection", _connect)
    sent: list = []

    def _unpinned(url, **_k):
        sent.append(url)
        raise AssertionError("the unpinned client was used")

    monkeypatch.setattr(setup_ops.requests, "get", _unpinned)
    api_server.app.config.update(TESTING=True)
    return answers, dialled, sent


def _ha_test(url):
    return api_server.app.test_client().post(
        "/setup/ha/test", json={"url": url, "token": TOKEN}, headers=_auth())


def test_a_local_name_with_a_global_answer_reaches_only_the_inside_one(ha):
    answers, dialled, sent = ha
    answers["homeassistant.local"] = [GLOBAL_V6, "fe80::1%2", "192.168.1.20"]
    resp = _ha_test("http://homeassistant.local:8123")
    body = resp.get_json()
    assert body["error"] != "ha_not_local", body
    assert "Nothing answered" in body["message"]
    assert dialled and {a[0] for a in dialled} == {"192.168.1.20"}
    assert all(a[1] == 8123 for a in dialled)
    assert sent == []
    assert TOKEN not in resp.get_data(as_text=True)


def test_a_local_name_with_no_inside_answer_is_refused_plainly(ha):
    answers, dialled, sent = ha
    answers["homeassistant.local"] = ["fe80::1%2", GLOBAL_V6]
    resp = _ha_test("http://homeassistant.local:8123")
    body = resp.get_json()
    assert resp.status_code == 400 and body["error"] == "ha_not_local"
    assert "IP address" in body["message"]
    assert dialled == [] and sent == []
    assert TOKEN not in resp.get_data(as_text=True)


@pytest.mark.parametrize("url,answer", [
    ("http://8.8.8.8:8123", None),
    ("http://example.com:8123", ["93.184.216.34"]),
    ("http://8.8.8.8.nip.io:8123", ["8.8.8.8"]),
    ("http://[64:ff9b::808:808]:8123", None),
    ("http://[64:ff9b:1::808:808]:8123", None),
    ("http://0xa9fea9fe:8123", None),
    ("http://ha.example.org:8123", ["192.168.1.20", "93.184.216.34"]),
    ("http://homeassistant.local:8123", ["93.184.216.34"]),
    ("http://homeassistant.local:8123", ["64:ff9b::c0a8:114"]),
    ("http://homeassistant.local:8123", ["169.254.169.254"]),
])
def test_every_other_refusal_stays(ha, url, answer):
    answers, dialled, sent = ha
    if answer:
        answers[urlsplit(url).hostname] = answer
    resp = _ha_test(url)
    assert resp.status_code == 400, resp.get_json()
    assert resp.get_json()["error"] in ("ha_not_local", "ha_unresolvable", "bad_ha_url")
    assert dialled == [] and sent == []
    assert TOKEN not in resp.get_data(as_text=True)


def test_saving_a_local_name_with_a_global_answer_is_accepted(ha):
    from puffpai import setup_ops
    answers, dialled, _sent = ha
    answers["homeassistant.local"] = ["192.168.1.20", GLOBAL_V6]
    resp = api_server.app.test_client().post(
        "/setup/ha", json={"present": True, "url": "http://homeassistant.local:8123",
                           "token": TOKEN}, headers=_auth())
    assert resp.status_code == 200, resp.get_json()
    assert setup_ops.ha_configured() is True
    assert dialled == []
    assert TOKEN not in resp.get_data(as_text=True)


def test_the_local_name_rule_is_home_assistants_only():
    from puffpai.security import lan_posture

    def both(host, port):
        return ["192.168.1.20", GLOBAL_V6]

    assert lan_posture.check_url("home_assistant", "http://homeassistant.local:8123",
                                 "island", resolve=True, resolver=both) is None
    assert lan_posture.check_url("ollama", "http://brain.local:11434", "island",
                                 resolve=True, resolver=both) is not None
    kind, _ = lan_posture.classify_host_resolving("nas.local", resolver=both)
    assert kind == lan_posture.PUBLIC, "without the HA rule the worst answer wins"
    with pytest.raises(netaddr.AddressRefused):
        netaddr.house_addresses("ha.example.org", 8123, resolver=both)
    assert netaddr.house_addresses("homeassistant.local", 8123,
                                   resolver=both) == ("192.168.1.20",)


# ── E. the recorded reach ───────────────────────────────────────────────────

def test_browser_setup_records_the_console_presets_reach_with_every_switch_off():
    from puffpai import homebot_preset
    api_server.app.config.update(TESTING=True)
    resp = api_server.app.test_client().post(
        "/setup/posture", headers=_auth(),
        json={"build_network": "", "run_egress": "", "enabled_switches": ["github"]})
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["run_egress"] == homebot_preset.RUN_EGRESS == egress_doors.OPEN_POSTURE
    assert body["run_egress_source"] == "defaulted"
    assert body["enabled_switches"] == []
    assert not any(body["egress_switches"].values())
    block = body["manifest_block"]
    assert block["run_egress"] == homebot_preset.RUN_EGRESS
    assert not any((block.get("egress_switches") or {}).values())


def test_a_stated_reach_is_still_recorded_as_stated():
    api_server.app.config.update(TESTING=True)
    body = api_server.app.test_client().post(
        "/setup/posture", headers=_auth(), json={"run_egress": "sealed"}).get_json()
    assert body["run_egress"] == "sealed" and body["run_egress_source"] == "stated"
