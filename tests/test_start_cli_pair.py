"""``puffpai pair`` prints this bot's own pairing code.

The code lives in the web page's pairing file (``PUFF_PAIRING_STATE``, else
``dashboard/data/pairing.json``), the one ``scripts/show-pairing-code.js``
and ``app/_lib/server/pairing.ts`` use. The command needs only what that
script needs: a person at this computer who can read the file. These tests pin
that it prints the code and never the secret, replaces a code that ran out
without touching the secret, never makes a new secret, and logs nothing.

No network; every test has its own pairing file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

from puffpai import start_cli as sc

SECRET = "cd" * 32
CODE = "ABCD2345"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _store(folder, *, rotated_at=None, code=CODE, extra=None):
    state = {"version": 1, "secret": SECRET, "code": code,
             "createdAt": "2026-10-01T00:00:00.000Z",
             "codeRotatedAt": _iso(rotated_at or datetime.now(timezone.utc))}
    state.update(extra or {})
    path = folder / "pairing.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def pair_file(tmp_path, monkeypatch):
    path = tmp_path / "pairing.json"
    monkeypatch.setenv("PUFF_PAIRING_STATE", str(path))
    return path


def _run(capsys):
    code = sc.cmd_pair(argparse.Namespace())
    return code, capsys.readouterr()


def test_it_prints_the_current_code_and_never_the_secret(tmp_path, pair_file,
                                                         capsys, caplog):
    _store(tmp_path)
    before = pair_file.read_text(encoding="utf-8")
    with caplog.at_level(logging.DEBUG):
        status, out = _run(capsys)
    assert status == 0
    assert "ABCD-2345" in out.out
    assert SECRET not in out.out + out.err
    assert "ABCD" not in caplog.text, "the code must never reach a log"
    assert pair_file.read_text(encoding="utf-8") == before, "a live code is not rewritten"


def test_a_code_that_ran_out_is_replaced_and_the_secret_is_kept(tmp_path,
                                                               pair_file,
                                                               capsys):
    _store(tmp_path, rotated_at=datetime.now(timezone.utc) - timedelta(hours=2),
           extra={"futureField": {"kept": True}})
    status, out = _run(capsys)
    assert status == 0
    after = json.loads(pair_file.read_text(encoding="utf-8"))
    assert after["secret"] == SECRET
    assert after["futureField"] == {"kept": True}
    assert after["createdAt"] == "2026-10-01T00:00:00.000Z"
    assert after["code"] != CODE
    assert len(after["code"]) == 8
    assert set(after["code"]) <= set(sc.PAIR_CODE_ALPHABET)
    rotated = datetime.fromisoformat(after["codeRotatedAt"].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - rotated).total_seconds()) < 60
    assert after["codeRotatedAt"].endswith("Z")
    assert f"{after['code'][:4]}-{after['code'][4:]}" in out.out
    assert "ABCD-2345" not in out.out
    assert SECRET not in out.out
    assert not list(tmp_path.glob("*.tmp")), "no temp file is left behind"


def test_an_unreadable_rotation_time_counts_as_run_out(tmp_path, pair_file,
                                                       capsys):
    _store(tmp_path, extra={"codeRotatedAt": "not a time"})
    status, _ = _run(capsys)
    assert status == 0
    assert json.loads(pair_file.read_text(encoding="utf-8"))["code"] != CODE


def test_no_pairing_file_means_no_new_secret(pair_file, capsys):
    status, out = _run(capsys)
    assert status == 1
    assert not pair_file.exists(), "a new secret would unpair every screen"
    assert "puffpai start" in out.out


@pytest.mark.parametrize("bad", [
    "{not json",
    json.dumps({"secret": "short", "code": CODE}),
    json.dumps({"secret": SECRET, "code": "abc"}),
    json.dumps({"secret": SECRET, "code": "ABCD0O1I"}),
    json.dumps(["not", "a", "state"]),
])
def test_a_broken_pairing_file_is_refused_and_left_alone(pair_file, capsys, bad):
    pair_file.write_text(bad, encoding="utf-8")
    status, out = _run(capsys)
    assert status == 1
    assert pair_file.read_text(encoding="utf-8") == bad
    assert SECRET not in out.out + out.err


def test_without_the_env_the_web_pages_own_file_is_used(tmp_path, monkeypatch,
                                                       capsys):
    monkeypatch.delenv("PUFF_PAIRING_STATE", raising=False)
    dash = tmp_path / "dashboard"
    (dash / "data").mkdir(parents=True)
    _store(dash / "data")
    monkeypatch.setattr(sc, "dashboard_dir", lambda: dash)
    status, out = _run(capsys)
    assert status == 0
    assert "ABCD-2345" in out.out


def test_no_web_page_and_no_env_says_so(monkeypatch, capsys):
    monkeypatch.delenv("PUFF_PAIRING_STATE", raising=False)
    monkeypatch.setattr(sc, "dashboard_dir", lambda: None)
    status, out = _run(capsys)
    assert status == 1
    assert "no web page" in out.out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_rewritten_file_is_owner_only(tmp_path, pair_file, capsys):
    _store(tmp_path, rotated_at=datetime.now(timezone.utc) - timedelta(hours=2))
    os.chmod(pair_file, 0o600)
    assert _run(capsys)[0] == 0
    assert stat.S_IMODE(os.stat(pair_file).st_mode) == 0o600
