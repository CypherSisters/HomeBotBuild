"""The trusted in-jail runner. Stdlib only; NOT a puffpai module at run time.

This file is copied into the per-run root as ``/work/_runner.py`` and executed
by the jail's own ``python3``. It never imports from :mod:`puffpai` -- inside
the jail there is no puffpai to import, and that is the point: the only code
from this appliance that runs in there is this one file.

What it owns:

* **The protocol.** Before the skill is imported, fd 1 is duplicated to a
  private descriptor and fd 1 itself is pointed at stderr, with ``sys.stdout``
  rebound to match. A ``print()`` in skill code therefore lands in the run log
  and CANNOT inject a protocol frame -- which would otherwise be a way for a
  skill to forge a fetch answer or a result. fd 0 gets the same treatment in
  reverse, so an ``input()`` cannot eat the host's reply.
* **The ``ctx`` contract.** ``ctx.fetch_json`` / ``ctx.fetch_text`` / ``ctx.log``
  and nothing else. Every fetch is a request to the host; the jail has no
  network namespace, so there is no second way out even for code that tries.
* **The tests.** The host's harness lives HERE, not in the package: the skill
  ships ``tests_fixtures.json`` (canned answers plus the assertions) and this
  file decides whether they passed. A skill that shipped its own ``tests.py``
  would be grading its own homework.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

import json
import os
import sys

WORK = "/work"
SKILL_DIR = os.path.join(WORK, "skill")

_PROTO_OUT = None
_PROTO_IN = None
_REQUESTS = 0


def _isolate_protocol():
    """Take fds 1 and 0 away from the skill before its code can touch them."""
    global _PROTO_OUT, _PROTO_IN
    out_fd = os.dup(1)
    in_fd = os.dup(0)
    os.dup2(2, 1)                      # print() -> stderr -> the run log
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)                # input() reads nothing, ever
    os.close(devnull)
    _PROTO_OUT = os.fdopen(out_fd, "wb", buffering=0)
    _PROTO_IN = os.fdopen(in_fd, "rb", buffering=0)
    sys.stdout = sys.stderr


def _send(frame):
    _PROTO_OUT.write((json.dumps(frame) + "\n").encode("utf-8"))


def _recv():
    chunks = []
    while True:
        byte = _PROTO_IN.read(1)
        if not byte:
            raise RuntimeError("the host closed the protocol")
        if byte == b"\n":
            break
        chunks.append(byte)
        if len(chunks) > 1024 * 1024:
            raise RuntimeError("the host sent an oversized frame")
    return json.loads(b"".join(chunks).decode("utf-8"))


class SkillError(Exception):
    """Raised into skill code when a fetch is refused."""


class Ctx:
    """Everything a skill may reach. Deliberately four methods wide."""

    def __init__(self, limits):
        self._max_requests = int(limits.get("max_requests", 4))
        self.data_dir = os.path.join(WORK, "data")

    def _fetch(self, url, kind):
        global _REQUESTS
        if _REQUESTS >= self._max_requests:
            raise SkillError("this skill has used all the requests it declared")
        _REQUESTS += 1
        _send({"t": "fetch", "url": str(url), "kind": kind})
        answer = _recv()
        if not answer.get("ok"):
            raise SkillError(str(answer.get("error") or "the request was refused"))
        return answer.get("json") if kind == "json" else answer.get("text")

    def fetch_json(self, url):
        """GET *url* through the host's door and parse it as JSON."""
        return self._fetch(url, "json")

    def fetch_text(self, url):
        """GET *url* through the host's door and return its text."""
        return self._fetch(url, "text")

    def log(self, message):
        """One line into the run log. Capped by the host."""
        _send({"t": "log", "text": str(message)[:500]})


def _load_skill():
    import importlib.util

    # No .pyc beside the staged copy. The host hashes that directory before and
    # after the run, and a skill package is exactly three files: a bytecode
    # cache appearing inside it is noise in a check whose whole value is that
    # it is exact.
    sys.dont_write_bytecode = True
    path = os.path.join(SKILL_DIR, "skill.py")
    spec = importlib.util.spec_from_file_location("puffpai_skill_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise RuntimeError("skill.py defines no run(inputs, ctx)")
    return module


def _check_result(result):
    if not isinstance(result, dict):
        raise RuntimeError("run() must return a dict")
    speech = result.get("speech")
    if not isinstance(speech, str) or not speech.strip():
        raise RuntimeError("run() returned no speech")
    data = result.get("data", {})
    if not isinstance(data, dict):
        raise RuntimeError("run() returned a non-object data field")
    return {"speech": speech[:1000], "data": data}


def _run_once(module, inputs, ctx):
    return _check_result(module.run(inputs, ctx))


def _run_tests(module, ctx):
    """Assert the fixtures' own expectations. Zero cases is a failure."""
    global _REQUESTS
    with open(os.path.join(SKILL_DIR, "tests_fixtures.json"), "r",
              encoding="utf-8") as handle:
        fixtures = json.load(handle)
    cases = fixtures.get("cases") or []
    if not cases:
        raise RuntimeError("the skill ships no test cases")
    checked = 0
    for index, case in enumerate(cases):
        _REQUESTS = 0
        result = _run_once(module, case.get("inputs") or {}, ctx)
        expect = case.get("expect") or {}
        for fragment in expect.get("speech_contains") or []:
            if fragment.lower() not in result["speech"].lower():
                raise RuntimeError(
                    "case %d: the answer does not contain %r; it said %r"
                    % (index, fragment, result["speech"]))
            checked += 1
        for field, value in (expect.get("data") or {}).items():
            if field not in result["data"]:
                raise RuntimeError("case %d: data has no %r" % (index, field))
            if result["data"][field] != value:
                raise RuntimeError(
                    "case %d: data[%r] is %r, expected %r"
                    % (index, field, result["data"][field], value))
            checked += 1
    if checked == 0:
        raise RuntimeError("the fixtures assert nothing")
    return {"speech": "%d assertion(s) passed" % checked,
            "data": {"assertions": checked, "cases": len(cases)}}


def main():
    _isolate_protocol()
    try:
        with open(os.path.join(WORK, "input.json"), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        limits = payload.get("limits") or {}
        ctx = Ctx(limits)
        module = _load_skill()
        if payload.get("mode") == "test":
            result = _run_tests(module, ctx)
        else:
            result = _run_once(module, payload.get("inputs") or {}, ctx)
        _send({"t": "result", "ok": True, "result": result,
               "requests": _REQUESTS})
    except SkillError as exc:
        _send({"t": "result", "ok": False, "error": str(exc)[:500],
               "requests": _REQUESTS})
    except Exception as exc:
        _send({"t": "result", "ok": False,
               "error": ("%s: %s" % (type(exc).__name__, exc))[:500],
               "requests": _REQUESTS})


if __name__ == "__main__":
    main()
