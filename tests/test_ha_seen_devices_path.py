"""The device-discovery memo is written to the STATE dir, never the code tree.

If ``ha_discover_new_devices`` wrote
``<repo>/ha_seen_devices.json``, on the appliance that resolves to
``/opt/puffpai/ha_seen_devices.json``, inside the tree the systemd unit mounts
read-only, and the turn would die with ``[Errno 30] Read-only file system``.

Run: py -3.14 -m pytest tests/test_ha_seen_devices_path.py -q
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import assistant_tools                      # noqa: E402


class _Device:
    def __init__(self, entity_id, friendly_name):
        self.entity_id = entity_id
        self.friendly_name = friendly_name


class _Bridge:
    def __init__(self, *entity_ids):
        self._devices = [_Device(e, e.split(".")[-1]) for e in entity_ids]

    def list_devices(self, **_kwargs):
        return self._devices


@contextlib.contextmanager
def _legacy_import_path():
    """``src/puffpai`` on the path for the duration — see
    ``tests/test_ha_bridge_confirmation_gate.py``: ``create_tools`` builds every
    tool in one function and a few still use pre-package imports.
    """
    flat = str(Path(__file__).resolve().parents[1] / "src" / "puffpai")
    sys.path.insert(0, flat)
    try:
        yield
    finally:
        try:
            sys.path.remove(flat)
        except ValueError:
            pass


class _Harness:
    """The tool bodies are closures over ``self._ha_bridge`` — bind and go."""

    def __init__(self, bridge):
        self._ha_bridge = bridge


def _discovery_tool(bridge):
    """The bound ``ha_discover_new_devices`` callable for *bridge*."""
    with _legacy_import_path():
        made = assistant_tools.AssistantTools.create_tools(_Harness(bridge))
    for tool in made:
        if tool.name == "ha_discover_new_devices":
            return tool.func
    raise AssertionError("ha_discover_new_devices is not on the tool surface")


def _discover(bridge):
    return _discovery_tool(bridge)()


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    assistant_tools.reset_seen_devices_cache()
    yield tmp_path
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    assistant_tools.reset_seen_devices_cache()


def test_the_memo_resolves_under_the_state_dir(state_dir):
    path = assistant_tools.ha_seen_devices_path()
    assert Path(path).parent == state_dir
    assert os.path.basename(path) == "ha_seen_devices.json"


def test_discovery_writes_the_memo_there_and_nowhere_in_the_repo(state_dir):
    repo_root = Path(assistant_tools._REPO_ROOT).resolve()
    written: list[str] = []
    real_open = open

    def _watched_open(file, mode="r", *args, **kwargs):
        if "w" in str(mode) or "a" in str(mode):
            written.append(os.path.abspath(str(file)))
        return real_open(file, mode, *args, **kwargs)

    import builtins
    builtins.open = _watched_open
    try:
        reply = _discover(_Bridge("light.porch", "switch.fan"))
    finally:
        builtins.open = real_open

    assert "2 new controllable devices" in reply
    memo = Path(assistant_tools.ha_seen_devices_path())
    assert json.loads(memo.read_text(encoding="utf-8")) == ["light.porch",
                                                            "switch.fan"]
    assert written, "the memo was not written at all"
    for path in written:
        resolved = Path(path).resolve()
        assert repo_root not in resolved.parents and resolved != repo_root, \
            f"discovery wrote inside the code tree: {path}"


def test_a_second_run_reports_only_what_is_actually_new(state_dir):
    _discover(_Bridge("light.porch"))
    reply = _discover(_Bridge("light.porch", "switch.fan"))
    assert "1 new controllable devices" in reply
    assert "switch.fan" in reply


def test_a_legacy_memo_is_read_in_place_and_never_written_back(state_dir,
                                                              monkeypatch,
                                                              tmp_path):
    """An upgraded box must not announce the whole house as newly discovered."""
    legacy = tmp_path / "legacy" / "ha_seen_devices.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(["light.porch"]), encoding="utf-8")
    monkeypatch.setattr(assistant_tools, "_LEGACY_SEEN_DEVICES_PATH", str(legacy))

    reply = _discover(_Bridge("light.porch", "switch.fan"))

    assert "1 new controllable devices" in reply
    assert json.loads(legacy.read_text(encoding="utf-8")) == ["light.porch"]
    assert json.loads(Path(assistant_tools.ha_seen_devices_path())
                      .read_text(encoding="utf-8")) == ["light.porch",
                                                        "switch.fan"]


def test_a_read_only_state_dir_does_not_kill_the_turn(state_dir, monkeypatch):
    """The owner still gets their answer; the memo failure is a log line."""
    def _boom(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    # Bind the tool BEFORE the trap: it is the memo write that must survive a
    # read-only filesystem, not the tool-surface construction.
    discover = _discovery_tool(_Bridge("light.porch"))
    monkeypatch.setattr(assistant_tools.os, "makedirs", _boom)
    monkeypatch.setattr(assistant_tools, "ha_seen_devices_path",
                        lambda: "/nonexistent-ro/ha_seen_devices.json")

    reply = discover()
    assert "1 new controllable devices" in reply
