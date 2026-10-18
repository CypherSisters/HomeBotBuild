"""Toolsmith's cloud model (security/toolsmith_cloud.py, build/toolsmith_cloud_loop.py).

Toolsmith is the ONLY agent that may use a cloud
model (Anthropic); everything else runs locally. A stubbed Anthropic answers
at the egress seam (``egress_doors._send``); nothing goes online.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time
from typing import Any

import pytest

from keys_support import PIN, fast_vault, grep_tree, reset_keys, set_posture
from puffpai import api_server, brain_tools
from puffpai.build import toolsmith_cloud_loop as L
from puffpai.build import toolsmith_run as T
from puffpai.security import egress_doors, egress_ledger, keys
from puffpai.security import toolsmith_cloud as tc

AUTH = {"Authorization": f"Bearer {api_server._API_KEY}"}
DEV = {**AUTH, "X-Puff-Device-Id": "devicetsmith0001"}
KEY = "sk-ant-api03-" + "Q" * 40 + "_test"
SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "puffpai"


class Resp:
    def __init__(self, status: int, body: Any, headers: dict | None = None):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.raw = None
        self.request = type("Req", (), {"headers": {}, "body": None})()

    def close(self):
        pass


class FakeAnthropic:
    """api.anthropic.com: /v1/models for the key check, /v1/messages scripted."""

    def __init__(self):
        self.calls: list[dict] = []
        self.replies: list[tuple] = []
        self.offline = False

    def __call__(self, method, url, *, pin=None, **kw):
        if self.offline:
            raise ConnectionError("network is down")
        headers = dict(kw.get("headers") or {})
        data = kw.get("data")
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": json.loads(data) if data else None})
        if headers.get("x-api-key") != KEY:
            return Resp(401, {"type": "error", "error": {"type": "authentication_error"}})
        if url.endswith("/v1/models"):
            return Resp(200, {"data": [{"id": "claude-opus-5-5"}]})
        status, body, hdrs = self.replies.pop(0) if self.replies else (
            200, msg([{"type": "text", "text": "done"}], "end_turn"), {})
        resp = Resp(status, body, hdrs)
        # As requests does: the reply carries the body that was sent, which is
        # where the outside-traffic log reads its byte count from.
        resp.request.body = data
        return resp

    @property
    def posts(self) -> list[dict]:
        return [c for c in self.calls if c["method"] == "POST"]


def msg(content: list, stop: str, usage: dict | None = None, **extra) -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "content": content,
            "stop_reason": stop, "usage": usage or {"input_tokens": 100, "output_tokens": 20,
                                                     "cache_read_input_tokens": 0,
                                                     "cache_creation_input_tokens": 0},
            **extra}


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    for d in egress_doors.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    reset_keys()
    set_posture("enumerated")
    v = fast_vault(monkeypatch, tmp_path / "vault")
    monkeypatch.setattr(api_server, "_vault_instance", v)
    keys.set_vault_provider(lambda: v)
    for name in (tc.SETTING_FILE, tc.USAGE_FILE, tc.KEEP_FILE):
        try:
            os.unlink(tc._path(name))  # noqa: SLF001
        except OSError:
            pass
    tc._no_fallback.clear()  # noqa: SLF001
    tc.drop_lease("idle")
    monkeypatch.setattr(tc, "_active_runs", 0)
    monkeypatch.setattr(tc, "_queue_busy", None)
    monkeypatch.setattr(tc, "_wizard_busy", None)
    monkeypatch.setattr(tc, "_resumers", [])
    monkeypatch.setattr(tc, "_listening", set())
    fake = FakeAnthropic()
    monkeypatch.setattr(egress_doors, "_send", fake)
    monkeypatch.setattr(tc, "_sleep", lambda s: fake.calls.append({"method": "SLEEP",
                                                                    "s": s}))
    api_server.app.config.update(TESTING=True)
    fake.client = api_server.app.test_client()
    fake.vault, fake.tmp = v, tmp_path
    yield fake
    tc.drop_lease("idle")
    for name in (tc.SETTING_FILE, tc.USAGE_FILE, tc.KEEP_FILE):
        try:
            os.unlink(tc._path(name))  # noqa: SLF001
        except OSError:
            pass
    reset_keys()


def _turn_on(cloud, model: str = "claude-opus-5-5") -> dict:
    r = cloud.client.post("/toolsmith/model", headers=DEV,
                          json={"where": "cloud", "model": model, "key": KEY})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _scope(job_id: str = "job1"):
    run = T.Run({"id": job_id, "name": "News"}, [{"ref": "r1", "name": "NewsApp"}],
                lambda _l: None)
    run.use_cloud = True
    return run._cloud_scope()  # noqa: SLF001


def _tools(record: list, fail: set = frozenset()) -> list:
    def make(name):
        def fn(args, ctx=None):
            record.append((name, args))
            if name in fail:
                return {"ok": False, "said": f"{name} failed"}
            return {"ok": True, "said": f"{name} ran", "output": args.get("command", "")}
        return brain_tools.Tool(name, f"the {name} tool", {
            "type": "object", "properties": {"command": {"type": "string"}},
            "additionalProperties": False}, fn, source="toolsmith")
    return [make("terminal"), make("download"), make("give_up")]


def _ctx(tools: list) -> dict:
    T._register_provider()  # noqa: SLF001
    return {"conversation_id": "toolsmith-test", "caller": "owner",
            T.TOOLS_KEY: tools, "cancelled": threading.Event()}


# -- the scope guard ------------------------------------------------------------

def test_a_caller_that_is_not_toolsmith_never_gets_the_cloud_path(cloud):
    _turn_on(cloud)
    with pytest.raises(tc.ScopeRefused):
        tc.toolsmith_scope("job1")                        # this test module, not toolsmith_run
    for fake in (None, "toolsmith", object(), object.__new__(tc.Scope)):
        with pytest.raises(tc.ScopeRefused):
            tc.send(fake, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    with pytest.raises(tc.ScopeRefused):
        tc.Scope(object(), "job1", "x")
    assert cloud.posts == []                              # nothing was sent
    # keyed_post is for the security package only
    with pytest.raises(PermissionError):
        keys.keyed_post(keys.handle("anthropic"), url="https://api.anthropic.com/v1/messages",
                        caller="chat", data=b"{}")


def test_only_toolsmith_run_mints_the_scope_and_runs_the_cloud_loop():
    """Fails if any other module names the scope or the cloud loop: chat,
    voice, interpret, ranking, decide, Drawbridge, Archivist, routines and
    every other agent always use the local model."""
    import ast
    minting, looping, keyed, where = [], [], [], set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if "toolsmith_scope" in text and rel != "security/toolsmith_cloud.py":
            minting.append(rel)
            for fn in ast.walk(ast.parse(text)):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Call) and "toolsmith_scope" in ast.unparse(
                                node.func):
                            where.add((rel, fn.name))
        if re.search(r"toolsmith_cloud_loop", text) and rel != "build/toolsmith_cloud_loop.py":
            looping.append(rel)
        if "keyed_post(" in text and rel not in ("security/keys.py",
                                                  "security/toolsmith_cloud.py"):
            keyed.append(rel)
    # Toolsmith's install loop, and its own finding work in build/apps.py
    # (search_words and rank) only. Never add another.
    assert sorted(minting) == ["build/apps.py", "build/toolsmith_run.py"]
    assert {fn for rel, fn in where if rel == "build/apps.py"} == {"search_words", "rank"}
    assert looping == ["build/toolsmith_run.py"]
    assert keyed == []
    assert tc.SCOPE_CALLER == "puffpai.build.toolsmith_run"
    assert tc.FIND_CALLERS == {"puffpai.build.apps": frozenset({"search_words", "rank"})}


def _minted_from(module: str, func: str):
    """toolsmith_scope called from a function *func* in a module named *module*."""
    code = f"def {func}():\n    return tc.toolsmith_scope('j', 'J')\n"
    space = {"__name__": module, "tc": tc}
    exec(compile(code, f"<{module}>", "exec"), space)  # noqa: S102 - the test's own text
    return space[func]()


def test_the_find_scope_is_only_for_apps_rank_and_search_words(cloud):
    assert _minted_from("puffpai.build.apps", "rank").kind == "find"
    assert _minted_from("puffpai.build.apps", "search_words").kind == "find"
    assert _minted_from("puffpai.build.toolsmith_run", "anything").kind == "install"
    for module, func in (("puffpai.build.apps", "for_job"), ("puffpai.build.apps", "look"),
                         ("puffpai.build.knowledge", "rank"), ("puffpai.chat", "rank"),
                         ("puffpai.build.interpret", "search_words"),
                         ("puffpai.build.decide", "rank")):
        with pytest.raises(tc.ScopeRefused):
            _minted_from(module, func)
    _turn_on(cloud)
    find = _minted_from("puffpai.build.apps", "rank")
    with pytest.raises(tc.ScopeRefused):          # a finding ask runs no tools
        tc.send(find, system="s", tools=[{"name": "run", "input_schema": {}}],
                messages=[{"role": "user", "content": "x"}])
    with pytest.raises(tc.ScopeRefused):          # the install scope is not for finding
        tc.ask_json(_scope(), system="s", user="x", purpose="p")
    assert cloud.posts == []


# -- a locked vault while saving the key --------------------------------------------

def test_a_locked_vault_gives_423_then_the_owners_pin_then_the_retry_saves(cloud):
    cloud.vault.lock()
    ask = {"where": "cloud", "model": "claude-opus-5-5", "key": KEY}
    r = cloud.client.post("/toolsmith/model", headers=DEV, json=ask)
    body = r.get_json()
    assert r.status_code == 423 and body["code"] == "vault_locked"
    assert body["message"] == "Your vault is locked. Type your vault PIN to save the key."
    assert KEY not in r.get_data(as_text=True) and not tc.in_use()
    u = cloud.client.post("/vault/unlock", headers=AUTH, json={"pin": PIN})
    assert u.status_code == 200 and u.get_json()["success"] is True
    again = cloud.client.post("/toolsmith/model", headers=DEV, json=ask)
    assert again.status_code == 200 and again.get_json()["where"] == "cloud"
    assert keys._record_for(cloud.vault, "anthropic")["value"] == KEY  # noqa: SLF001
    assert tc.in_use()


def test_before_the_plan_is_approved_the_save_says_so_plainly(cloud, monkeypatch):
    from puffpai import outside_services

    def refuse(*_a, **_k):
        raise keys.KeyRefused("This bot has not been set up yet, so nothing can be "
                              "switched on.", "cannot")
    monkeypatch.setattr(outside_services, "_has_posture_record", lambda: False)
    monkeypatch.setattr(keys, "add_key", refuse)
    r = cloud.client.post("/toolsmith/model", headers=DEV,
                          json={"where": "cloud", "model": "claude-opus-5-5", "key": KEY})
    assert r.status_code == 409 and r.get_json()["code"] == "before_setup"
    assert r.get_json()["message"] == tc.BEFORE_SETUP_LINE and not tc.in_use()


# -- Toolsmith's own finding work on the cloud model --------------------------------

def _repo(name, stars, desc, topics=()):
    return {"full_name": f"o/{name}", "name": name, "stargazers_count": stars,
            "language": "TypeScript", "description": desc, "topics": list(topics),
            "html_url": f"https://github.com/o/{name}", "default_branch": "main"}


def _no_local_brain(*_a, **_k):
    raise AssertionError("the local brain was asked while Toolsmith's cloud answered")


def test_cloud_ranking_keeps_the_app_keeps_the_library_as_code_drops_the_demo(
        cloud, monkeypatch):
    from puffpai.build import apps
    _turn_on(cloud)
    demo = _repo("opentelemetry-demo", 9000, "Astronomy Shop: an OpenTelemetry "
                 "observability demo", ["observability", "demo", "astronomy"])
    real = _repo("stellarium-web", 800, "Planetarium in your browser: see the night sky",
                 ["astronomy", "planetarium"])
    lib = _repo("astropy-lite", 400, "Astronomy calculations: rise and set times, "
                "planet positions", ["astronomy", "library"])
    cloud.replies = [(200, msg([{"type": "text", "text": json.dumps(
        {"apps": [1], "code": [2], "dropped": [0]})}], "end_turn"), {})]
    job = {"id": "sky", "name": "Night sky", "does": "show tonight's stars and planets"}
    # Each README: the app runs with compose; the library has no way to run as an app.
    plans = {"stellarium-web": ("readme", {"steps": ["docker compose up -d"],
                                           "how": "docker", "found": "compose"}),
             "astropy-lite": ("readme", {"steps": ["docker build ."], "how": "docker",
                                         "found": "Dockerfile"})}
    monkeypatch.setattr(apps, "run_plan", lambda it: plans[it["name"]])
    monkeypatch.setattr(apps, "_cand", lambda it, plan, n: {
        "ref": it["full_name"], "full_name": it["full_name"], "name": it["name"]})
    monkeypatch.setattr(apps, "_beyond", lambda *a, **k: [])
    monkeypatch.setattr(apps, "hers_code", lambda j: [])
    monkeypatch.setattr(apps, "catalog_items", lambda j: [])
    got = apps.for_job(job, "Help me learn astronomy.", brain=_no_local_brain,
                       words={"goal": [], "jobs": {"sky": [["astronomy"]]}},
                       found={("astronomy",): [demo, real, lib]})
    assert [c["name"] for c in got] == ["stellarium-web"]          # the app, as an app
    assert [c["name"] for c in job["code"]] == ["astropy-lite"]    # the library, as code
    assert job["code"][0]["ref"] == "code:o/astropy-lite" and apps.CLOUD_CODE not in job["code"][0]
    assert all("opentelemetry" not in c["name"] for c in got + job["code"])   # dropped
    post = cloud.posts[-1]
    b = post["body"]
    assert post["url"] == "https://api.anthropic.com/v1/messages"
    assert "tools" not in b and "thinking" not in b and "tool_choice" not in b
    assert b["max_tokens"] == tc.FIND_MAX_TOKENS
    assert '"Astronomy Shop" observability demo is not an astronomy app' in b["system"]
    sent = json.loads(b["messages"][0]["content"])
    assert sent["job"]["name"] == "Night sky" and sent["goal"].startswith("Help me learn")
    assert sent["projects"][1]["topics"] == ["astronomy", "planetarium"]
    assert sent["projects"][0]["stars"] == 9000
    assert set(sent["projects"][0]) == {"n", "name", "full_name", "description", "stars",
                                        "topics", "language"}
    row = tc.read_usage()["jobs"]["sky"]
    assert row["calls"] == 1 and row["last"]["purpose"] == "Toolsmith ranking for Night sky"
    assert KEY not in json.dumps(tc.read_usage())


def test_cloud_search_words_and_a_cloud_failure_falls_back_to_this_computer(cloud):
    from puffpai.build import apps
    _turn_on(cloud)
    cloud.replies = [(200, msg([{"type": "text", "text": "Here: " + json.dumps(
        {"goal": ["night sky"], "jobs": [{"id": "sky", "phrases": ["planetarium"]}]})}],
        "end_turn"), {})]
    jobs = [{"id": "sky", "name": "Night sky", "does": "show the stars"}]
    words = apps.search_words(jobs, "Help me learn astronomy.", _no_local_brain)
    assert words == {"goal": ["night", "sky"], "jobs": {"sky": [["planetarium"]]}}
    cloud.offline = True
    asked = []

    def local(messages, fmt):
        asked.append(messages[0]["content"][:20])
        return json.dumps({"order": [0]})
    got = apps.rank({"id": "sky", "name": "Night sky"},
                    [_repo("a", 5, "x"), _repo("b", 9, "y")], local)
    assert asked and [i["name"] for i in got] == ["b", "a"]   # local order kept both


def test_with_cloud_off_the_ranking_never_leaves_this_computer(cloud):
    from puffpai.build import apps
    got = apps.rank({"id": "sky", "name": "Night sky"}, [_repo("a", 5, "x")],
                    lambda m, f: json.dumps({"order": [0]}))
    assert [i["name"] for i in got] == ["a"] and cloud.calls == []


def test_a_run_handed_its_own_brain_never_goes_to_the_cloud(cloud):
    _turn_on(cloud)
    run = T.Run({"id": "j", "name": "x"}, [{"ref": "r1", "name": "A"}], lambda _l: None)
    assert run._cloud_scope() is None                     # noqa: SLF001 - use_cloud unset
    run.use_cloud = True
    assert isinstance(run._cloud_scope(), tc.Scope)       # noqa: SLF001


# -- the setting and the key -----------------------------------------------------

def test_local_is_the_default_and_the_key_is_kept_only_in_the_vault(cloud, caplog):
    caplog.set_level("DEBUG")
    assert tc.read_setting() == {"where": "local"} and not tc.in_use()
    st = cloud.client.get("/toolsmith/model", headers=DEV).get_json()
    assert st["where"] == "local" and st["consent_text"] == tc.CONSENT_TEXT
    assert "—" not in tc.CONSENT_TEXT
    got = _turn_on(cloud)
    assert got["where"] == "cloud" and got["model"] == "claude-opus-5-5"
    assert KEY not in json.dumps(got)
    assert keys._record_for(cloud.vault, "anthropic")["value"] == KEY  # noqa: SLF001
    from puffpai.config import get_state_dir
    assert grep_tree(get_state_dir(), KEY) == []
    assert grep_tree(str(cloud.tmp / "vault"), KEY) == []           # encrypted at rest
    assert KEY not in caplog.text
    # The key check went to api.anthropic.com with the key in a header only.
    check = cloud.calls[0]
    assert check["url"] == "https://api.anthropic.com/v1/models"
    assert check["headers"]["anthropic-version"] == "2023-06-01"
    # A key in the address is refused; off is one click.
    r = cloud.client.post(f"/toolsmith/model?key={KEY}", headers=DEV, json={"where": "local"})
    assert r.status_code == 400 and KEY not in r.get_data(as_text=True)
    assert cloud.client.post("/toolsmith/model", headers=DEV,
                             json={"where": "local"}).get_json()["where"] == "local"
    assert not tc.in_use() and not egress_doors.egress_allowed("toolsmith_cloud")[0]


def test_the_routes_need_a_paired_device(cloud):
    assert cloud.client.get("/toolsmith/model", headers=AUTH).status_code == 403
    r = cloud.client.post("/toolsmith/model", headers=AUTH,
                          json={"where": "cloud", "model": "claude-opus-5-5", "key": KEY})
    assert r.status_code == 403 and not tc.in_use()


def test_a_cloud_setting_without_the_owners_yes_reads_as_local(cloud):
    from puffpai import fsutil
    fsutil.atomic_write_json(tc._path(tc.SETTING_FILE), {  # noqa: SLF001
        "schema": tc.SCHEMA, "where": "cloud", "provider": "anthropic",
        "model": "claude-opus-5-5"})
    assert not tc.in_use()
    fsutil.atomic_write_json(tc._path(tc.SETTING_FILE), {  # noqa: SLF001
        "schema": tc.SCHEMA, "where": "cloud", "provider": "anthropic",
        "model": "claude-haiku-9", "consent": {"text": tc._sha(tc.CONSENT_TEXT)[:16]}})
    assert not tc.in_use()


# -- the consent sentence is true, and a change asks the owner again ------------------

#: An older consent sentence (it left out the goal, the finding asks and
#: the owner's own folder).
OLD_CONSENT = ("When Toolsmith installs an app, it sends Anthropic the job's name and "
               "what you asked it to do, that app's README and file list, and the "
               "commands it runs with their output. Keys and personal details it "
               "spots are taken out first. It never sends your files, chats or "
               "vault. Everything else in BuildBot stays on this computer.")


def test_the_consent_sentence_names_what_really_leaves():
    text = tc.CONSENT_TEXT
    for part in ("your goal and your bot's jobs",
                 "the names and descriptions of the apps it found",
                 "each app's README, docs and file list",
                 "the start of its setup files",
                 "the commands it runs with their output",
                 "If you point it at a folder of your own, it reads that folder the same way.",
                 "Keys and personal details it spots are taken out first.",
                 "It never sends your chats or your vault."):
        assert part in text, part
    assert text.endswith("Everything else in your bot stays on this computer.")
    assert "your files" not in text                   # the owner's own folder CAN be sent
    for bad in ("—", "–", "door", "beta"):
        assert bad not in text.lower()
    assert text != OLD_CONSENT


def _old_yes(model: str = "claude-opus-5-5") -> None:
    """The owner's setting as an earlier version wrote it: Anthropic, their yes to OLD_CONSENT."""
    from puffpai import fsutil
    fsutil.atomic_write_json(tc._path(tc.SETTING_FILE), {  # noqa: SLF001
        "schema": tc.SCHEMA, "where": "cloud", "provider": "anthropic", "model": model,
        "consent": {"at": "2026-10-01T00:00:00+00:00", "by": "owner",
                    "text": tc._sha(OLD_CONSENT)[:16], "device": "x"}})  # noqa: SLF001


def test_a_yes_to_the_old_sentence_reads_as_local_and_says_so(cloud):
    _turn_on(cloud, "claude-sonnet-5-5")              # the owner's key saved in the vault
    _old_yes("claude-sonnet-5-5")
    try:
        assert tc.read_setting()["where"] == "local" and not tc.in_use()
        assert tc.consent_outdated()
        st = cloud.client.get("/toolsmith/model", headers=DEV).get_json()
        assert st["where"] == "local" and st["consent_again"] is True
        assert st["line"] == ("Toolsmith's model: on this computer. "
                              "What Toolsmith sends to Anthropic now reads differently. "
                              "Read it and pick Anthropic again to keep using it. "
                              "Your key is still in your vault.")
        assert st["consent_text"] == tc.CONSENT_TEXT
        # The build holds with the same plain words; nothing quietly runs locally.
        assert tc.held() and tc.hold_line() == tc.RECONSENT_HOLD_LINE
        assert st["hold_line"] == tc.RECONSENT_HOLD_LINE
        assert "now reads differently" in tc.RECONSENT_HOLD_LINE
        # Nothing is sent on the old yes: a run gets no cloud scope at all.
        before = len(cloud.posts)
        assert _scope() is None
        with pytest.raises((tc.CloudError, tc.ScopeRefused)):
            tc.send(None, system="s", tools=[], messages=[
                {"role": "user", "content": [{"type": "text", "text": "x"}]}])
        assert len(cloud.posts) == before
        for line in (tc.RECONSENT_LINE, tc.RECONSENT_HOLD_LINE):
            assert "—" not in line and "–" not in line
    finally:
        tc.clear_wish()


def test_agreeing_to_the_new_sentence_brings_anthropic_back_without_a_new_key(cloud):
    _turn_on(cloud)
    calls_before = len(cloud.calls)
    _old_yes()
    released: list = []
    tc._resumers.append(lambda: released.append(True))  # noqa: SLF001 - the build's hook
    # The owner's one tap: Anthropic again, the new sentence on screen, no key pasted.
    r = cloud.client.post("/toolsmith/model", headers=DEV,
                          json={"where": "cloud", "model": "claude-opus-5-5"})
    assert r.status_code == 200, r.get_json()
    got = r.get_json()
    assert got["where"] == "cloud" and got["consent_again"] is False
    assert got["line"] == "Toolsmith's model: Anthropic Opus 5.5"
    assert tc.in_use() and not tc.consent_outdated() and not tc.held()
    assert tc.read_setting()["consent"]["text"] == tc._sha(tc.CONSENT_TEXT)[:16]  # noqa: SLF001
    assert keys._record_for(cloud.vault, "anthropic")["value"] == KEY  # noqa: SLF001
    assert len(cloud.calls) == calls_before           # no key check: the same key
    deadline = time.time() + 5
    while not released and time.time() < deadline:
        time.sleep(0.02)
    assert released == [True]                          # held jobs go on


def test_this_computer_ends_the_old_yes_and_the_hold(cloud):
    _turn_on(cloud)
    _old_yes()
    released: list = []
    tc._resumers.append(lambda: released.append(True))  # noqa: SLF001
    st = cloud.client.post("/toolsmith/model", headers=DEV, json={"where": "local"}).get_json()
    assert st["where"] == "local" and st["consent_again"] is False
    assert st["line"] == "Toolsmith's model: on this computer" and st["hold_line"] == ""
    assert not tc.consent_outdated() and not tc.held()
    deadline = time.time() + 5
    while not released and time.time() < deadline:
        time.sleep(0.02)
    assert released == [True]


# -- the request shape -----------------------------------------------------------

def test_the_body_and_headers_match_the_current_api(cloud):
    _turn_on(cloud)
    tc.send(_scope(), system="You are Toolsmith.", tools=[
        {"name": "zeta", "description": "z", "input_schema": {"type": "object"}},
        {"name": "alpha", "description": "a", "input_schema": {"type": "object"}}],
        messages=[{"role": "user", "content": [{"type": "text", "text": "the job"}]}],
        app_name="NewsApp")
    post = cloud.posts[-1]
    assert post["url"] == "https://api.anthropic.com/v1/messages"
    h = post["headers"]
    assert h["x-api-key"] == KEY and h["anthropic-version"] == "2023-06-01"
    assert h["content-type"] == "application/json"
    assert h["anthropic-beta"] == "server-side-fallback-2026-07-01"
    b = post["body"]
    assert b["model"] == "claude-opus-5-5" and b["max_tokens"] == 16000
    assert b["system"] == "You are Toolsmith."
    assert [t["name"] for t in b["tools"]] == ["alpha", "zeta"]
    assert b["cache_control"] == {"type": "ephemeral"}
    assert b["output_config"] == {"effort": "high"} and b["fallbacks"] == "default"
    assert "thinking" not in b and "tool_choice" not in b and "budget_tokens" not in json.dumps(b)


def test_a_model_that_refuses_fallbacks_is_asked_again_without_them(cloud):
    _turn_on(cloud, "claude-sonnet-5-5")
    cloud.replies = [(400, {"type": "error", "error": {"message": "fallbacks not supported"}}, {}),
                     (200, msg([{"type": "text", "text": "ok"}], "end_turn"), {}),
                     (200, msg([{"type": "text", "text": "ok"}], "end_turn"), {})]
    scope = _scope()
    tc.send(scope, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    tc.send(scope, system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    bodies = [p["body"] for p in cloud.posts]
    assert "fallbacks" in bodies[0] and "fallbacks" not in bodies[1] \
        and "fallbacks" not in bodies[2]
    assert "anthropic-beta" not in cloud.posts[2]["headers"]


def test_busy_and_server_errors_are_retried_honoring_retry_after(cloud):
    _turn_on(cloud)
    cloud.replies = [(429, {"error": {}}, {"Retry-After": "7"}),
                     (529, {"error": {}}, {}),
                     (200, msg([{"type": "text", "text": "ok"}], "end_turn"), {})]
    got = tc.send(_scope(), system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    assert got["stop_reason"] == "end_turn"
    sleeps = [c["s"] for c in cloud.calls if c["method"] == "SLEEP"]
    assert sleeps[0] == 7.0 and len(sleeps) == 2


def test_a_key_that_is_not_accepted_says_so_plainly(cloud):
    _turn_on(cloud)
    cloud.replies = [(401, {"type": "error"}, {})]
    with pytest.raises(tc.CloudError) as err:
        tc.send(_scope(), system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    assert err.value.message == ("Toolsmith's Anthropic key was not accepted. "
                                 "Check it in Settings.")


def test_no_network_falls_back_to_the_local_model_for_that_try(cloud, monkeypatch):
    _turn_on(cloud)
    cloud.offline = True
    with pytest.raises(tc.CloudOffline):
        tc.send(_scope(), system="s", tools=[], messages=[{"role": "user", "content": "x"}])
    # In Run.go: that try runs on the local model, with one plain line.
    from puffpai import brain_loop
    local_calls, lines = [], []
    monkeypatch.setattr(brain_loop, "run", lambda *a, **k: local_calls.append(1) or
                        {"text": "", "steps": 1})
    monkeypatch.setattr(T, "taken_ports", lambda extra=(): [])
    run = T.Run({"id": "j", "name": "News"}, [{"ref": "r1", "name": "NewsApp"}],
                lines.append)
    run.use_cloud = True
    run.go(object())
    assert local_calls and tc.OFFLINE_LINE in lines


# -- the loop ------------------------------------------------------------------------

THINK = {"type": "thinking", "thinking": "Let me look at the README first.",
         "signature": "EqQBCkYIBxgCKkBzaWduYXR1cmUtYnl0ZXMtdGhhdC1tdXN0LXN0YXk="}


def test_assistant_blocks_go_back_exactly_and_history_is_append_only(cloud):
    _turn_on(cloud)
    first = [THINK, {"type": "text", "text": "Downloading."},
             {"type": "tool_use", "id": "toolu_01A", "name": "download",
              "input": {"command": "x"}}]
    second = [{"type": "thinking", "thinking": "", "signature": "Zm9v"},
              {"type": "tool_use", "id": "toolu_02B", "name": "terminal",
               "input": {"command": "npm install"}}]
    cloud.replies = [(200, msg(first, "tool_use"), {}), (200, msg(second, "tool_use"), {}),
                     (200, msg([{"type": "text", "text": "Started."}], "end_turn"), {})]
    record: list = []
    tools = _tools(record)
    conv = L.Conversation("You are Toolsmith.", "The job: news")
    got = L.run(conv, scope=_scope(), ctx=_ctx(tools), tools=tools, max_steps=10,
                app_name="NewsApp")
    assert got["stop"] == "end_turn" and got["text"] == "Started."
    posts = [p["body"]["messages"] for p in cloud.posts]
    assert len(posts) == 3
    # byte for byte: the assistant turn sent back is the block list returned
    assert json.dumps(posts[1][1]["content"]) == json.dumps(first)
    assert json.dumps(posts[2][3]["content"]) == json.dumps(second)
    # every earlier request is an unchanged prefix of the next one
    for a, b in zip(posts, posts[1:]):
        assert [json.dumps(m, sort_keys=True) for m in b[:len(a)]] == \
               [json.dumps(m, sort_keys=True) for m in a]
    # KEEP_GOING appends; nothing earlier changes
    before = json.dumps(conv.messages)
    conv.add_user_text(T.KEEP_GOING)
    assert json.dumps(conv.messages).startswith(before[:-1])


def test_all_tool_results_go_back_in_one_user_message(cloud):
    _turn_on(cloud)
    calls = [{"type": "tool_use", "id": "toolu_A", "name": "terminal",
              "input": {"command": "ls"}},
             {"type": "tool_use", "id": "toolu_B", "name": "download",
              "input": {"command": "y"}}]
    cloud.replies = [(200, msg(calls, "tool_use"), {}),
                     (200, msg([{"type": "text", "text": "ok"}], "end_turn"), {})]
    record: list = []
    tools = _tools(record, fail={"download"})
    conv = L.Conversation("s", "task")
    L.run(conv, scope=_scope(), ctx=_ctx(tools), tools=tools, max_steps=5)
    assert [r[0] for r in record] == ["terminal", "download"]
    last = cloud.posts[1]["body"]["messages"][-1]
    assert last["role"] == "user" and len(last["content"]) == 2
    a, b = last["content"]
    assert (a["type"], a["tool_use_id"], b["tool_use_id"]) == ("tool_result", "toolu_A",
                                                                "toolu_B")
    assert "is_error" not in a and b["is_error"] is True


def test_a_refusal_ends_the_try_with_a_plain_line(cloud):
    _turn_on(cloud)
    cloud.replies = [(200, msg([], "refusal", stop_details={"category": "cyber"}), {})]
    tools = _tools([])
    got = L.run(L.Conversation("s", "t"), scope=_scope(), ctx=_ctx(tools), tools=tools,
                max_steps=5)
    assert got["stop"] == "refusal" and "would not help" in got["text"] and "cyber" in got["text"]


def test_max_tokens_goes_on_once(cloud):
    _turn_on(cloud)
    cloud.replies = [(200, msg([{"type": "text", "text": "part"}], "max_tokens"), {}),
                     (200, msg([{"type": "text", "text": "more"}], "max_tokens"), {})]
    tools = _tools([])
    got = L.run(L.Conversation("s", "t"), scope=_scope(), ctx=_ctx(tools), tools=tools,
                max_steps=5)
    assert len(cloud.posts) == 2 and got["stop"] == "end_turn"
    assert cloud.posts[1]["body"]["messages"][-1]["content"][-1]["text"] == L.CONTINUE_LINE


def test_a_try_that_grows_too_large_ends_rather_than_trimming(cloud):
    _turn_on(cloud)
    big = {"input_tokens": tc.CONTEXT_TOKENS, "output_tokens": 10}
    cloud.replies = [(200, msg([{"type": "tool_use", "id": "toolu_C", "name": "terminal",
                                 "input": {"command": "ls"}}], "tool_use", usage=big), {})]
    tools = _tools([])
    got = L.run(L.Conversation("s", "t"), scope=_scope(), ctx=_ctx(tools), tools=tools,
                max_steps=5)
    assert got["stop"] == "full" and len(cloud.posts) == 1


# -- what leaves ------------------------------------------------------------------

def test_outgoing_text_is_screened_for_keys_and_private_details(cloud):
    _turn_on(cloud)
    secret = "ghp_" + "A1b2C3d4" * 5
    calls = [{"type": "tool_use", "id": "toolu_D", "name": "terminal",
              "input": {"command": "cat .env"}}]
    cloud.replies = [(200, msg(calls, "tool_use"), {}),
                     (200, msg([{"type": "text", "text": "ok"}], "end_turn"), {})]

    def leaky(args, ctx=None):
        return {"ok": True, "output": f"GITHUB_TOKEN={secret}\ncall 555-867-5309"}
    tools = [brain_tools.Tool("terminal", "t", {"type": "object", "properties": {
        "command": {"type": "string"}}}, leaky, source="toolsmith")]
    L.run(L.Conversation("s", f"task with {secret}"), scope=_scope(), ctx=_ctx(tools),
          tools=tools, max_steps=5)
    sent = json.dumps([p["body"] for p in cloud.posts])
    assert secret not in sent and "555-867-5309" not in sent and KEY not in sent
    assert "[held in vault]" in sent


def test_each_call_writes_one_outside_line_and_a_usage_row_without_content(cloud):
    _turn_on(cloud)
    usage = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 2000,
             "cache_creation_input_tokens": 400}
    cloud.replies = [(200, msg([{"type": "text", "text": "secret words"}], "end_turn",
                               usage=usage), {})]
    tc.send(_scope("jobX"), system="s", tools=[],
            messages=[{"role": "user", "content": "the readme text"}], app_name="NewsApp")
    lines = [r for r in egress_ledger.read(hours=1) if r.get("service") == "toolsmith_cloud"]
    # Newest first: lines[0] is this call. The oldest is the key check's GET,
    # which sends no body, so its byte count is 0.
    assert lines and lines[0]["host"] == "api.anthropic.com"
    assert lines[0]["method"] == "POST"
    assert lines[0]["what"] == "toolsmith_install" and lines[0]["bytes_out"] > 0
    row = tc.read_usage()["jobs"]["jobX"]
    assert row["last"]["purpose"] == "Toolsmith install of NewsApp"
    assert row["last"]["model"] == "claude-opus-5-5" and row["last"]["provider"] == "anthropic"
    assert row["tokens"]["cache_read_input_tokens"] == 2000
    stored = open(tc._path(tc.USAGE_FILE), encoding="utf-8").read()  # noqa: SLF001
    assert "readme text" not in stored and "secret words" not in stored
    assert "about $" in tc.status()["spent_line"]


def test_the_cost_estimate(cloud):
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
         "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
    assert tc.cost_usd("claude-opus-5-5", u) == pytest.approx(4 + 20 + 0.20 + 5.0)
    assert tc.cost_usd("claude-sonnet-5-5", u) == pytest.approx(2 + 10 + 0.20 + 2.5)
    assert tc.cost_usd("claude-sonnet-5-5", {"output_tokens": 1000}) == pytest.approx(0.01)
    assert tc.cost_usd("other-model", u) == 0.0


# -- local mode unchanged ---------------------------------------------------------

def test_local_mode_uses_the_local_loop_and_never_the_cloud(cloud, monkeypatch):
    from puffpai import brain_loop
    seen = []
    monkeypatch.setattr(brain_loop, "run", lambda messages, **k: seen.append(
        [m["role"] for m in messages]) or {"text": "", "steps": 1})
    monkeypatch.setattr(tc, "send", lambda *a, **k: pytest.fail("cloud used in local mode"))
    monkeypatch.setattr(T, "taken_ports", lambda extra=(): [])
    run = T.Run({"id": "j", "name": "News"}, [{"ref": "r1", "name": "NewsApp"}],
                lambda _l: None)
    run.use_cloud = True                      # asked for, but the setting says local
    run.go(object())
    assert seen and seen[0] == ["system", "user"]
    # and with the cloud on, a brain handed in still stays local
    _turn_on(cloud)
    seen.clear()
    monkeypatch.setattr(T.Run, "go", lambda self, backend: (
        seen.append(getattr(self, "use_cloud", None)), None)[1])
    T.run_job({"id": "j", "name": "News"}, [{"ref": "r1", "name": "NewsApp"}],
              brain=object())
    assert seen == [False]


# -- the build-scoped key lease ---------------------------------------------------

ASK = {"system": "s", "tools": [], "messages": [{"role": "user", "content": "x"}]}


def _audit_text() -> str:
    path = pathlib.Path(tc._path("security_logs")) / "toolsmith_cloud_audit.jsonl"  # noqa: SLF001
    return path.read_text(encoding="utf-8") if path.exists() else ""


# -- the session key lease -------------------------------------------------------------

def _fake_clock(monkeypatch) -> dict:
    now = {"t": 5000.0}
    monkeypatch.setattr(tc, "_clock", lambda: now["t"])
    return now


def test_the_keep_choice_route_needs_a_paired_device_and_a_known_choice(cloud):
    _turn_on(cloud)
    assert cloud.client.post("/toolsmith/model/keep", headers=AUTH,
                             json={"keep": "4h"}).status_code == 403
    bad = cloud.client.post("/toolsmith/model/keep", headers=DEV, json={"keep": "forever"})
    assert bad.status_code == 400 and tc.keep_choice() == "1h"
    r = cloud.client.post("/toolsmith/model/keep", headers=DEV, json={"keep": "lock"})
    body = r.get_json()
    assert r.status_code == 200 and body["keep"] == "lock" and tc.keep_choice() == "lock"
    assert [c["id"] for c in body["keep_choices"]] == ["1h", "4h", "lock"]
    assert body["keep_question"] == "Keep Toolsmith's key ready while I'm building"
    # A junk file reads as the default.
    pathlib.Path(tc._path(tc.KEEP_FILE)).write_text("{not json", encoding="utf-8")  # noqa: SLF001
    assert tc.keep_choice() == "1h"


def test_the_lease_timer_lets_go_on_time(cloud, monkeypatch):
    now = _fake_clock(monkeypatch)
    _turn_on(cloud)
    assert tc.take_lease(_scope())
    assert tc._timer is not None and tc._timer.daemon  # noqa: SLF001
    now["t"] += 60 * 60 + 1
    tc._on_timer()  # noqa: SLF001 - what the timer does when it fires
    assert tc._lease is None and tc._timer is None  # noqa: SLF001


# -- the key ready from the wizard ----------------------------------------------------
#
# The owner may save the key in the wizard with the vault open and the vault can
# idle-lock while the owner goes on; the first install must not then pause for a
# vault the owner cannot open from the wizard.

def test_a_locked_vault_at_save_takes_nothing(cloud):
    cloud.vault.lock()
    r = cloud.client.post("/toolsmith/model", headers=DEV,
                          json={"where": "cloud", "model": "claude-opus-5-5", "key": KEY})
    assert r.status_code == 423 and tc._lease is None  # noqa: SLF001


def test_a_new_key_swaps_the_lease_with_no_old_key_held(cloud, monkeypatch):
    new = "sk-ant-api03-" + "R" * 40 + "_test"
    _turn_on(cloud)
    old = tc._lease  # noqa: SLF001
    assert old is not None and bytes(old._buf).decode() == KEY  # noqa: SLF001
    seen: list = []
    real_ends, real_starts = keys._lease_ends, keys._lease_starts  # noqa: SLF001

    def on_file() -> str:
        return keys._record_for(cloud.vault, "anthropic")["value"]  # noqa: SLF001

    def ends(sid):
        real_ends(sid)
        # Let go while the OLD key is still the one on file: before the write.
        seen.append(("ends", on_file() == KEY, tc._lease is None, not any(old._buf)))  # noqa: SLF001

    def starts(sid, v):
        seen.append(("starts", on_file() == new, tc._lease is None))  # noqa: SLF001
        real_starts(sid, v)
    monkeypatch.setattr(keys, "_lease_ends", ends)
    monkeypatch.setattr(keys, "_lease_starts", starts)
    # The fake Anthropic accepts only KEY; let the new key pass its check too.
    real_send = egress_doors._send

    def send(method, url, **kw):
        h = dict(kw.get("headers") or {})
        if h.get("x-api-key") == new:
            kw["headers"] = {**h, "x-api-key": KEY}
        return real_send(method, url, **kw)
    monkeypatch.setattr(egress_doors, "_send", send)
    r = cloud.client.post("/toolsmith/model", headers=DEV,
                          json={"where": "cloud", "model": "claude-opus-5-5", "key": new})
    assert r.status_code == 200, r.get_json()
    assert seen == [("ends", True, True, True), ("starts", True, True)]
    held = tc._lease  # noqa: SLF001
    assert held is not None and held is not old and not any(old._buf)  # noqa: SLF001
    assert bytes(held._buf).decode() == new  # noqa: SLF001
    log = _audit_text()
    assert log.index("its key changed") < log.rindex("you saved its key")
    # A key replaced from another page (the Keys page: keys.add_key) swaps it too.
    before = tc._lease  # noqa: SLF001
    keys.add_key("anthropic", KEY, vault=cloud.vault)
    assert tc.lease_active() and tc._lease is not before  # noqa: SLF001
    assert bytes(tc._lease._buf).decode() == KEY and not any(before._buf)  # noqa: SLF001
    with pytest.raises(tc.ScopeRefused):
        tc.key_saved(cloud.vault)                             # only the key broker says so


def test_the_wizard_registers_its_probe_and_counts_its_run_and_build(monkeypatch):
    from puffpai.wizard import service
    monkeypatch.setattr(tc, "_wizard_busy", None)
    service._hook_toolsmith_lease()  # noqa: SLF001
    assert tc._wizard_busy is service._wizard_going  # noqa: SLF001
    monkeypatch.setattr(service, "setup_in_progress", lambda *a, **k: False)
    monkeypatch.setattr(service, "_REST", {"thread": None})
    assert service._wizard_going() is False  # noqa: SLF001
    # A wizard only under way is not a running build: the owner's steps count instead.
    monkeypatch.setattr(service, "setup_in_progress", lambda *a, **k: True)
    assert service._wizard_going() is False  # noqa: SLF001
    alive = threading.Event()
    t = threading.Thread(target=alive.wait, daemon=True)
    t.start()
    monkeypatch.setattr(service, "_REST", {"thread": t})
    assert service._wizard_going() is True  # noqa: SLF001
    alive.set()
    t.join(5)
    assert service._wizard_going() is False  # noqa: SLF001


def test_the_inline_unlock_is_on_every_build_view_and_uses_the_vault_route():
    root = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "app"
    box = (root / "_components" / "Jobs" / "VaultWaitUnlock.tsx").read_text(encoding="utf-8")
    assert '"/vault/unlock"' in box and "{ pin: p }" in box
    assert "initialize" not in box                            # never makes a vault
    assert "console." not in box and "localStorage" not in box and "sessionStorage" not in box
    assert 'type="password"' in box and "Unlock" in box
    assert "—" not in box and "beta" not in box.lower()
    for rel in ("onboarding/Conversation.tsx", "onboarding/BuildHandoff.tsx",
                "_components/Jobs/JobsHome.tsx", "dashboard/apps/[id]/BotApps.tsx",
                "dashboard/jobs/[id]/page.tsx"):
        assert "<VaultWaitUnlock" in (root / rel).read_text(encoding="utf-8"), rel

def test_clean_hides_generated_secrets_in_env_and_compose_text():
    from puffpai.security import toolsmith_cloud as TC
    hexv = "0123456789abcdef" * 2
    text = (f'APP_KEY="{hexv}"\nDB_PASS=EXAMPLE9\n'
            "services:\n  db:\n    environment:\n      POSTGRES_PASSWORD: abc123\n"
            "    ports:\n      - \"5432:5432\"\n")
    out = TC.clean(text)
    assert hexv not in out and "EXAMPLE9" not in out and "abc123" not in out
    assert "POSTGRES_PASSWORD: " in out and '"5432:5432"' in out and "APP_KEY=" in out
    assert TC.clean(out) == out
