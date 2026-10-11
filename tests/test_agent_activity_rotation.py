"""Size-based rotation for ``agent-activity.jsonl``.

``chat_activity`` writes a ``turn_handled`` event per (turn, agent) with
no bound, so the feed can grow past one entry per consent exchange. This
pins the rotation policy in
:mod:`puffpai.agent_activity`: roll at :data:`agent_activity.MAX_BYTES`, cap
at :data:`agent_activity.BACKUP_COUNT` generations, preserve the file's mode
across a roll, never truncate in place, and never lose the newest event to
``/agents/status`` / ``/agents/activity`` (both read the live file only,
which always holds the just-written entry because rotation happens before
the append that triggered it).
"""

from __future__ import annotations

import json
import os
import stat

import pytest

import puffpai.agent_activity as aa


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    aa.reset_cache()
    monkeypatch.setattr(aa, "MAX_BYTES", 200)  # small threshold, fast test
    yield
    aa.reset_cache()


def _write_n(path: str, n: int, agent: str = "cortex") -> None:
    for i in range(n):
        aa.log_activity(agent, "turn_handled", f"task {i}", "completed",
                        path=path)


def test_rotation_triggers_and_caps_generations(tmp_path):
    path = str(tmp_path / "agent-activity.jsonl")
    _write_n(path, 40)

    root, ext = os.path.splitext(path)
    generations = [f"{root}.{i}{ext}" for i in (1, 2, 3)]
    assert any(os.path.exists(g) for g in generations), (
        "expected at least one rotated generation")
    assert not os.path.exists(f"{root}.4{ext}"), "must cap at 3 generations"
    assert os.path.exists(path), "live file must still exist after rotation"


def test_rotation_preserves_file_mode(tmp_path):
    """Whatever mode the live file has going into a roll is the mode the new
    live file has coming out of it — compared against the file's own prior
    mode rather than a hardcoded value, since ``chmod``'s meaning is
    platform-dependent (Windows collapses to a read-only bit)."""
    path = str(tmp_path / "agent-activity.jsonl")
    aa.log_activity("cortex", "turn_handled", "seed", "completed", path=path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    before_mode = stat.S_IMODE(os.stat(path).st_mode)

    _write_n(path, 40)

    after_mode = stat.S_IMODE(os.stat(path).st_mode)
    assert after_mode == before_mode, (
        f"mode not preserved across rotation: {oct(before_mode)} -> "
        f"{oct(after_mode)}")


def test_rotation_never_truncates_in_place(tmp_path):
    """The rolled-away generation carries every line the live file had before
    the roll — the roll is a rename, never an in-place truncation."""
    path = str(tmp_path / "agent-activity.jsonl")
    lines = [json.dumps({"agent": "cortex", "event": "turn_handled",
                         "task": f"pre-existing {i}", "status": "completed"})
             for i in range(5)]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    assert os.path.getsize(path) >= aa.MAX_BYTES, (
        "fixture must already be over threshold so the next write rotates it")

    aa.log_activity("cortex", "turn_handled", "trigger", "completed", path=path)

    root, ext = os.path.splitext(path)
    with open(f"{root}.1{ext}", encoding="utf-8") as f:
        rolled_lines = [ln for ln in f.readlines() if ln.strip()]
    assert len(rolled_lines) == 5, "the rolled generation must carry all 5 prior lines"
    for i, ln in enumerate(rolled_lines):
        assert f"pre-existing {i}" in ln

    with open(path, encoding="utf-8") as f:
        live_lines = [ln for ln in f.readlines() if ln.strip()]
    assert len(live_lines) == 1
    assert "trigger" in live_lines[0]


def test_status_reader_sees_newest_event_across_a_rotation(tmp_path):
    """`/agents/status` and `/agents/activity` read the CURRENT file only.

    Rotation happens before the append that triggers it, so the live file
    always contains the entry that was just written — reading the current
    generation for the latest status is correct even immediately after a
    rotation. This test drives that scenario directly against the writer
    rather than the Flask route, to stay within this test's scope.
    """
    path = str(tmp_path / "agent-activity.jsonl")
    _write_n(path, 40)

    aa.log_activity("sentinel", "turn_handled", "final task", "completed",
                    path=path)

    with open(path, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert lines, "live file must not be empty right after a write"
    assert lines[-1]["agent"] == "sentinel"
    assert lines[-1]["task"] == "final task"
