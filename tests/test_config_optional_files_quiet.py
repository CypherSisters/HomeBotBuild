"""A fresh install has no Home Assistant or media server settings yet.

Their absence is normal, so loading the settings says nothing above debug
about them. A settings file that is there but broken is still a warning, and
a missing main settings file still is too.
"""
from __future__ import annotations

import pytest

from puffpai import config


class _Log:
    def __init__(self):
        self.warnings: list[str] = []
        self.debugs: list[str] = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args)

    def debug(self, msg, *args):
        self.debugs.append(msg % args)


@pytest.fixture
def log(monkeypatch):
    rec = _Log()
    monkeypatch.setattr(config, "logger", rec)
    return rec


@pytest.mark.parametrize("name", ["home_assistant_config.json",
                                  "media_server_config.json"])
def test_an_absent_optional_file_is_quiet(tmp_path, log, name):
    assert config._load_json(str(tmp_path / name)) == {}
    assert log.warnings == []
    assert log.debugs, "the absence is still noted at debug"


def test_a_broken_optional_file_is_still_a_warning(tmp_path, log):
    path = tmp_path / "home_assistant_config.json"
    path.write_text("{not json", encoding="utf-8")
    assert config._load_json(str(path)) == {}
    assert len(log.warnings) == 1


def test_an_absent_main_settings_file_is_still_a_warning(tmp_path, log):
    assert config._load_json(str(tmp_path / "puffpai_config.json")) == {}
    assert len(log.warnings) == 1
