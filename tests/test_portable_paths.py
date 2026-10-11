"""Portability contract: state paths and Linux-only tooling on any OS.

HomeBotBuild installs on arbitrary Windows and Linux hardware. These tests pin
the two rules that make that true:

  1. Writable state resolves to ``$PUFFPAI_DATA_DIR`` when set, and to the
     platform's per-user data directory otherwise — never to ``/data/puffpai``,
     which exists only on the Founders appliance.
  2. A Linux-only external tool that is absent produces an honest "not
     available" result, not an exception and not a crash at import.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys

import pytest

from puffpai import platform_paths


def _pristine_platform_paths():
    """A fresh, UNPATCHED copy of ``puffpai.platform_paths``.

    ``conftest``'s autouse ``_isolate_user_data_dir`` replaces
    ``platform_paths.user_data_dir`` with a stub returning a temp directory, so
    that no test writes into the developer's real state directory. That is right
    for every test that RESOLVES a path and wrong for the two below, which assert
    what the platform rule itself IS — against the stub they would pass no matter
    what the function did, which is worse than failing.

    Its docstring names the escape hatch: "a test that means to assert the real
    platform layout ... asserts on the module's logic directly." Loading a second
    module object under a private name does exactly that, and leaves the patched
    one — and therefore every other test — untouched. A bare
    ``importlib.reload`` would not: it mutates the shared module in place and
    fights the monkeypatch's own undo.
    """
    spec = importlib.util.spec_from_file_location(
        "_pristine_platform_paths", platform_paths.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 1. per-user state root ─────────────────────────────────────────────────

def test_env_wins_over_the_per_user_default(monkeypatch, tmp_path):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    assert platform_paths.data_dir() == os.path.normpath(str(tmp_path))


def test_env_is_expanded_and_normalised(monkeypatch):
    monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/hb//state/")
    resolved = platform_paths.data_dir()
    assert "~" not in resolved
    assert resolved == os.path.normpath(resolved)


def test_default_is_per_user_not_the_appliance_path(monkeypatch):
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    resolved = platform_paths.data_dir()
    assert os.path.isabs(resolved)
    assert resolved.replace("\\", "/") != "/data/puffpai"
    assert not resolved.replace("\\", "/").startswith("/data/puffpai")


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows rule")
def test_windows_default_follows_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    real = _pristine_platform_paths()
    assert real.user_data_dir() == os.path.join(str(tmp_path), "PuffPAI")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX rule")
def test_linux_default_follows_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    real = _pristine_platform_paths()
    assert real.user_data_dir() == os.path.join(str(tmp_path), "puffpai")


def test_data_dir_creates_nothing(monkeypatch, tmp_path):
    target = tmp_path / "not-created-yet"
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(target))
    platform_paths.data_path("models", "x.onnx")
    assert not target.exists()


# ── 2. Linux-only tooling degrades honestly ────────────────────────────────

def test_pulse_socket_default_follows_the_state_root(monkeypatch, tmp_path):
    """The Pulse socket must not be hard-wired to the appliance's /data path."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PUFFPAI_PULSE_SOCKET", raising=False)
    import puffpai.audio_volume as av
    av = importlib.reload(av)
    try:
        assert av._SOCKET == os.path.join(os.path.normpath(str(tmp_path)), "pulse.sock")
    finally:
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        importlib.reload(av)


def test_volume_reports_unavailable_without_pactl(monkeypatch):
    """No pactl (every Windows box, many Linux ones) must not raise."""
    import puffpai.audio_volume as av
    monkeypatch.setattr(av.shutil, "which", lambda name: None)
    # A spawn attempt would be the bug this guards: fail loudly if one happens.
    monkeypatch.setattr(av.subprocess, "run", _never_called)

    assert av.pactl_available() is False
    assert av.get_state() == (None, False)
    assert av.set_percent(50) is None
    assert av.nudge(10) is None
    assert av.set_mute(True) is False

    result = av._pactl("get-sink-volume", "@DEFAULT_SINK@")
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode != 0
    assert "not available on this platform" in result.stderr


def _never_called(*args, **kwargs):  # pragma: no cover - only runs on failure
    raise AssertionError("no subprocess may be spawned when pactl is absent")


def test_caretaker_refuses_service_control_off_linux(monkeypatch):
    """Restarting a systemd unit is a refusal off Linux, not an exception."""
    from puffpai.agents import caretaker
    monkeypatch.setattr(caretaker.platform, "system", lambda: "Windows")
    agent = caretaker.CaretakerAgent.__new__(caretaker.CaretakerAgent)
    assert caretaker.CaretakerAgent._restart_argv(agent, "puffpai-api") is None


# ── 3. import-time safety ──────────────────────────────────────────────────

@pytest.mark.parametrize("module", [
    "puffpai.platform_paths",
    "puffpai.audio_volume",
    "puffpai.voice.service",
    "puffpai.voice.ducking",
    "puffpai.agents.caretaker",
    "puffpai.now_playing",
    "puffpai.media_hub",
    "puffpai.firewall_manager",
])
def test_module_imports_on_this_platform(module):
    """Every platform-touching module must import on whatever OS runs the tests."""
    importlib.import_module(module)
