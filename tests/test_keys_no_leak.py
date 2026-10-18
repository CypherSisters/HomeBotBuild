"""The leak test: a random 40-character canary key, through a LOCAL stub vendor.

Not DEMO_KEY (it is in public docs and fixtures, so grepping for it proves
nothing). The canary is made fresh for this run, stored through the terminal
and the routes, used by chat searches (streamed and not), echoed back by the
vendor, refused by the vendor, broken by a transport error that names it, and
pasted into chat. Then the whole state folder, every log (DEBUG on, urllib3
included), the outside-traffic log, the saved chats, the config folder, every
response body and all captured output are searched for it: zero hits. The
vault files hold it only as ciphertext, so they are searched too.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys

import pytest

from keys_support import (PIN, canary, fast_vault, grep_tree, install_vendor,
                          machine_key_file, reset_keys, set_posture)
from puffpai import api_server, cli
from puffpai.config import get_config, get_state_dir
from puffpai.security import egress_doors, egress_ledger, keys

AUTH = {"Authorization": f"Bearer {api_server._API_KEY}"}
DEV = {**AUTH, "X-Puff-Device-Id": "deviceleak000001"}


@pytest.fixture
def world(monkeypatch, tmp_path):
    for d in egress_doors.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    reset_keys()
    set_posture("enumerated")
    vault_dir = cli._vault_dir(get_config())  # noqa: SLF001 - the one the CLI uses
    v = fast_vault(monkeypatch, vault_dir)
    monkeypatch.setattr(api_server, "_vault_instance", v)
    keys.set_vault_provider(lambda: v)
    machine_key_file(monkeypatch, tmp_path)
    log_dir = os.path.join(get_state_dir(create=True), "logs")
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.FileHandler(os.path.join(log_dir, "debug.log"),
                                  encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    for name in ("urllib3", "requests", "puffpai", "werkzeug"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    api_server.app.config.update(TESTING=True)
    yield {"vault": v, "client": api_server.app.test_client(),
           "log_dir": log_dir, "vault_dir": vault_dir}
    root.removeHandler(handler)
    handler.close()
    root.setLevel(old_level)
    reset_keys()

