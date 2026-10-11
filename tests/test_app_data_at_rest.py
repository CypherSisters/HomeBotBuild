"""App-data at-rest hardening — file modes, atomic writes, and the boot sweep.

The mode spread these tests sweep is what a create-time umask produces when
nothing in the write path re-asserts a floor:

    alarmflow.json 0644
    calendar.json 0644 <- medications and bills
    fitfam.json 0664 <- health history
    mediahub.json 0644
    suggestions.json 0644
    freshtable.json 0600 <- the LEAST sensitive one is the tightest
    app_data/ 0755

i.e. the two most sensitive documents carry the loosest modes.
"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from puffpai.security.app_data_store import (  # noqa: E402
    APP_DATA_DIR_MODE,
    APP_DATA_FILE_MODE,
    app_data_mode_status,
    ensure_secure_dir,
    sweep_app_data_modes,
    write_json_secure,
)

#: chmod on Windows only toggles the read-only bit, so the mode ASSERTIONS are
#: POSIX-only. The behaviour assertions (atomicity, content, no leftovers) run
#: everywhere, because those are the ones a Windows dev checkout can break.
posix_only = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes; chmod is a no-op on Windows")


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ── write_json_secure ───────────────────────────────────────────────────────

def test_write_creates_file_with_expected_content(tmp_path):
    target = tmp_path / "calendar.json"
    write_json_secure(str(target), {"meds": [], "events": []})
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "meds": [], "events": []}


@posix_only
def test_new_file_is_owner_only_regardless_of_umask(tmp_path):
    """The floor must not depend on the umask of whatever launched the process.

    Without the floor the mode comes from the umask, and an appliance can be
    started by several different launchers over its life.
    """
    old = os.umask(0o000)  # the most permissive umask there is
    try:
        target = tmp_path / "fitfam.json"
        write_json_secure(str(target), {"users": {}})
        assert _mode(str(target)) == APP_DATA_FILE_MODE
    finally:
        os.umask(old)


@posix_only
def test_rewrite_tightens_a_file_that_was_already_loose(tmp_path):
    """The regression that let calendar.json sit at 0644 for months.

    ``open(path, 'w')`` truncates IN PLACE and preserves the existing mode, so
    every rewrite of an already-0644 document produced another 0644 document.
    A temp-file + rename cannot do that: the new inode carries the new mode.
    """
    target = tmp_path / "calendar.json"
    target.write_text('{"meds": []}', encoding="utf-8")
    os.chmod(str(target), 0o644)
    assert _mode(str(target)) == 0o644

    write_json_secure(str(target), {"meds": [{"id": "m1"}]})
    assert _mode(str(target)) == APP_DATA_FILE_MODE


@posix_only
def test_directory_is_created_owner_only(tmp_path):
    target = tmp_path / "state" / "app_data" / "trivia.json"
    write_json_secure(str(target), {"players": []})
    assert _mode(str(target.parent)) == APP_DATA_DIR_MODE


@posix_only
def test_ensure_secure_dir_tightens_an_existing_loose_directory(tmp_path):
    directory = tmp_path / "app_data"
    directory.mkdir(mode=0o755)
    os.chmod(str(directory), 0o755)
    ensure_secure_dir(str(directory))
    assert _mode(str(directory)) == APP_DATA_DIR_MODE


def test_write_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "alarmflow.json"
    write_json_secure(str(target), {"items": []})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["alarmflow.json"]


def test_failed_write_preserves_the_previous_document(tmp_path):
    """Atomicity is the reason nobody needs a.bak copy.

    A non-atomic write path makes hand-rolled backups look prudent, and a
    hand-rolled ``calendar.json.bak-*`` copy is world-readable calendar data.
    If a failed write cannot damage the document, the motive disappears.
    """
    target = tmp_path / "calendar.json"
    write_json_secure(str(target), {"meds": [{"id": "m1"}]})

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        write_json_secure(str(target), {"meds": [Unserialisable()]},
                          default=None)

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "meds": [{"id": "m1"}]}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["calendar.json"]


# ── boot sweep ──────────────────────────────────────────────────────────────

@posix_only
def test_sweep_tightens_a_umask_mode_spread(tmp_path):
    """The umask-produced spread from the module docstring, reproduced and swept."""
    observed = {
        "alarmflow.json": 0o644,
        "calendar.json": 0o644,
        "fitfam.json": 0o664,
        "mediahub.json": 0o644,
        "suggestions.json": 0o644,
        "freshtable.json": 0o600,
    }
    for name, mode in observed.items():
        path = tmp_path / name
        path.write_text("{}", encoding="utf-8")
        os.chmod(str(path), mode)
    os.chmod(str(tmp_path), 0o755)

    summary = sweep_app_data_modes(str(tmp_path))

    for name in observed:
        assert _mode(str(tmp_path / name)) == APP_DATA_FILE_MODE, name
    assert sorted(summary["tightened"]) == [
        "alarmflow.json", "calendar.json", "fitfam.json", "mediahub.json",
        "suggestions.json"]
    assert summary["already_secure"] == ["freshtable.json"]
    assert summary["failed"] == []
    assert summary["directory_secured"] is True
    assert _mode(str(tmp_path)) == APP_DATA_DIR_MODE


def test_sweep_reports_names_only_never_content(tmp_path):
    """The sweep must never open a file. Its whole output vocabulary is names.

    Enforced by giving it a document whose CONTENT is a distinctive marker and
    asserting that marker appears nowhere in the returned summary.
    """
    marker = "TESTFAKEPERSON-MEDICATION-MARKER"
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps({"meds": [{"name": marker}]}), encoding="utf-8")

    summary = sweep_app_data_modes(str(tmp_path))
    assert marker not in json.dumps(summary)


def test_sweep_ignores_non_json_files(tmp_path):
    """A sweep that chmods everything will one day chmod something load-bearing."""
    (tmp_path / "calendar.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "subdir").mkdir()

    summary = sweep_app_data_modes(str(tmp_path))
    touched = (set(summary["tightened"]) | set(summary["already_secure"])
               | set(summary["failed"]))
    assert "notes.txt" not in touched
    assert "subdir" not in touched
    if os.name == "posix":
        assert touched == {"calendar.json"}


def test_sweep_is_a_declared_no_op_where_modes_are_unenforceable(tmp_path):
    """On Windows the sweep must say so, not claim success or log an error.

    ``os.stat`` reports a synthetic 0666 there and ``os.chmod`` only toggles the
    read-only flag, so a naive check reads every file as world-readable and
    every chmod as failed — an ERROR line on every dev start, and a status block
    that would be wrong in whichever direction it guessed.
    """
    from puffpai.security import app_data_store

    (tmp_path / "calendar.json").write_text("{}", encoding="utf-8")
    summary = sweep_app_data_modes(str(tmp_path))
    status = app_data_mode_status(str(tmp_path))
    if app_data_store.MODES_ENFORCEABLE:
        assert "skipped" not in summary
        assert status["status"] in ("ok", "loose_modes")
    else:
        assert summary["skipped"]
        assert summary["failed"] == []
        assert status["status"] == "unsupported_platform"


def test_sweep_on_a_missing_directory_is_not_fatal(tmp_path):
    summary = sweep_app_data_modes(str(tmp_path / "does-not-exist" / "nested"))
    assert summary["failed"] == []


# ── status ──────────────────────────────────────────────────────────────────

@posix_only
def test_mode_status_counts_loose_files(tmp_path):
    for name, mode in (("calendar.json", 0o644), ("fitfam.json", 0o600)):
        path = tmp_path / name
        path.write_text("{}", encoding="utf-8")
        os.chmod(str(path), mode)

    before = app_data_mode_status(str(tmp_path))
    assert before["status"] == "loose_modes"
    assert before["files_loose"] == 1
    assert before["files_owner_only"] == 1

    sweep_app_data_modes(str(tmp_path))
    after = app_data_mode_status(str(tmp_path))
    assert after["status"] == "ok"
    assert after["files_loose"] == 0


def test_mode_status_publishes_no_filenames(tmp_path):
    (tmp_path / "calendar.json").write_text("{}", encoding="utf-8")
    assert "calendar" not in json.dumps(app_data_mode_status(str(tmp_path)))


# ── AppData goes through the helper ─────────────────────────────────────────

def test_app_data_save_uses_the_secure_writer(tmp_path, monkeypatch):
    """The point of the whole change: ONE serialiser, not one per caller."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib

    from puffpai import app_data as app_data_module
    importlib.reload(app_data_module)

    ad = app_data_module.AppData()
    ad.set_data("calendar", {"meds": [], "bills": [], "events": []})

    written = tmp_path / "app_data" / "calendar.json"
    assert written.exists()
    doc = json.loads(written.read_text(encoding="utf-8"))
    assert doc["meds"] == []
    if os.name == "posix":
        assert _mode(str(written)) == APP_DATA_FILE_MODE
        assert _mode(str(written.parent)) == APP_DATA_DIR_MODE

    importlib.reload(app_data_module)
