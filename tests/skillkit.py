"""Shared helpers for the skills tests. Not a test module.

Two things live here because four test files need them and a copy in each is a
copy that drifts:

* :func:`make_draft` -- write a valid skill package into a temp directory, with
  per-test overrides, so a refusal test can change exactly one field;
* :func:`fake_execute` -- run a skill package IN PROCESS against a broker,
  emulating what ``skills.runner`` does inside the jail.

``fake_execute`` exists so the store/install/dispatch/broker layers can be
tested on a box with no bubblewrap (every Windows dev box). It is NOT a
substitute for the jail and no test uses it to make a claim about isolation:
the isolation assertions live in ``test_skills_sandbox.py`` behind
``needs_jail`` and they run a real bubblewrap or they skip.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import importlib.util
import json
import os
import shutil
import sys

EXAMPLE_ID = "iss_position"


def example_dir():
    """The shipped hand-written example package."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "src", "puffpai", "skills_examples",
                        EXAMPLE_ID)


def load_example():
    root = example_dir()
    with open(os.path.join(root, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    with open(os.path.join(root, "tests_fixtures.json"), encoding="utf-8") as handle:
        fixtures = json.load(handle)
    with open(os.path.join(root, "skill.py"), encoding="utf-8") as handle:
        source = handle.read()
    return manifest, source, fixtures


def make_draft(folder, *, manifest=None, source=None, fixtures=None,
               manifest_patch=None, drop=()):
    """Write a skill package into *folder*. Returns the folder."""
    base_manifest, base_source, base_fixtures = load_example()
    manifest = dict(manifest if manifest is not None else base_manifest)
    if manifest_patch:
        manifest.update(manifest_patch)
    for key in drop:
        manifest.pop(key, None)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
    with open(os.path.join(folder, "skill.py"), "w", encoding="utf-8") as handle:
        handle.write(source if source is not None else base_source)
    with open(os.path.join(folder, "tests_fixtures.json"), "w",
              encoding="utf-8") as handle:
        json.dump(fixtures if fixtures is not None else base_fixtures, handle,
                  indent=2)
    return folder


def stage_example_draft(store, skill_id=EXAMPLE_ID):
    """Copy the shipped example into the store's drafts tree."""
    target = store.draft_dir(skill_id)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)
    for name in ("manifest.json", "skill.py", "tests_fixtures.json"):
        shutil.copyfile(os.path.join(example_dir(), name),
                        os.path.join(target, name))
    return target


class _Ctx:
    """The in-jail ``ctx`` contract, host-side, for :func:`fake_execute`."""

    def __init__(self, broker, data_dir):
        self._broker = broker
        self.data_dir = data_dir
        self.lines = []

    def _fetch(self, url, kind):
        answer = self._broker.handle({"t": "fetch", "url": url, "kind": kind})
        if not answer.get("ok"):
            raise RuntimeError(answer.get("error") or "refused")
        return answer.get("json") if kind == "json" else answer.get("text")

    def fetch_json(self, url):
        return self._fetch(url, "json")

    def fetch_text(self, url):
        return self._fetch(url, "text")

    def log(self, message):
        self.lines.append(str(message)[:500])


def fake_execute(skill_dir, payload, *, broker, run_root, timeout_s):
    """In-process stand-in for ``sandbox.execute``. No jail; tests only."""
    os.makedirs(os.path.join(run_root, "data"), exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "puffpai_skill_under_test", os.path.join(skill_dir, "skill.py"))
    module = importlib.util.module_from_spec(spec)
    # No __pycache__ inside the package: a skill directory is exactly three
    # files, and validate_dir refuses any other directory in it -- correctly.
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    ctx = _Ctx(broker, os.path.join(run_root, "data"))

    def _one(inputs):
        result = module.run(inputs or {}, ctx)
        assert isinstance(result, dict)
        return {"speech": str(result.get("speech") or ""),
                "data": result.get("data") or {}}

    try:
        if payload.get("mode") == "test":
            with open(os.path.join(skill_dir, "tests_fixtures.json"),
                      encoding="utf-8") as handle:
                fixtures = json.load(handle)
            checked = 0
            for case in fixtures.get("cases") or []:
                out = _one(case.get("inputs") or {})
                expect = case.get("expect") or {}
                for fragment in expect.get("speech_contains") or []:
                    assert fragment.lower() in out["speech"].lower(), fragment
                    checked += 1
                for field, value in (expect.get("data") or {}).items():
                    assert out["data"].get(field) == value, field
                    checked += 1
            assert checked, "the fixtures assert nothing"
            result = {"speech": f"{checked} assertion(s) passed",
                      "data": {"assertions": checked}}
        else:
            result = _one(payload.get("inputs") or {})
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "log": ctx.lines, "stderr": "", "requests": broker.count}
    return {"ok": True, "result": result, "error": "", "log": ctx.lines,
            "requests": broker.count, "stderr": ""}
