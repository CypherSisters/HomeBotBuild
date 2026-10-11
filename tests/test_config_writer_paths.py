"""Nothing that WRITES a config file writes it into the code tree.

The miss this file pins: ``agents/preferences._config_path()`` hard-coded
``<REPO_ROOT>/config/puffpai_config.json``, so ``PUT /agents/names`` — renaming
the agents, a thing the product invites every owner to do — rewrote the SHIPPED,
tracked config inside the checkout on every install. On a git checkout that is a
tracked file going dirty; on a packaged or read-only install it is a write into
the program directory. Meanwhile the loader
(:func:`puffpai.config._config_dir_for`) had already moved on: it honours
``$PUFFPAI_CONFIG_DIR`` and, for a non-dev install, relocates the config dir
beside the state (seeded from the shipped copy). Reader and writer disagreed.

Three writers of that file are covered — the agent display names, the hatch's
identity and the profile manager's identity — plus the rule that makes the whole
class of bug impossible to reintroduce quietly: every one of them resolves
through :func:`puffpai.config.resolve_config_dir`.

No network, no model, no Orin.

Run:
    py -3 -m pytest tests/test_config_writer_paths.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server                       # noqa: E402
from puffpai import config as cfgmod                 # noqa: E402
from puffpai import hatch                            # noqa: E402
from puffpai import profile_manager                  # noqa: E402
from puffpai.agents import preferences as prefs      # noqa: E402

AUTH = {"X-API-Key": api_server._API_KEY}

#: The file an install must never touch: the one the repo ships and tracks.
SHIPPED = Path(cfgmod._find_repo_root()) / "config" / "puffpai_config.json"


@pytest.fixture
def relocated(tmp_path, monkeypatch):
    """A NON-DEV install: only ``$PUFFPAI_DATA_DIR`` is set.

    The suite's ``_isolate_config_dir`` normally sets ``$PUFFPAI_CONFIG_DIR``
    too, which would make this pass for the wrong reason — the explicit override
    was the one branch the old literal could not reach either. Dropping it
    leaves exactly the shape a packaged install has: state relocated, config
    dir derived from it and seeded from the shipped file.
    """
    monkeypatch.delenv(cfgmod.CONFIG_DIR_ENV, raising=False)
    monkeypatch.setenv(cfgmod.STATE_DIR_ENV, str(tmp_path / "state"))
    cfgmod._instance = None
    cfgmod._dev_state_cache.clear()
    prefs.reset_cache()
    api_server.app.config["TESTING"] = True
    yield tmp_path / "state" / "config"
    cfgmod._instance = None
    prefs.reset_cache()


def _shipped_bytes() -> bytes:
    return SHIPPED.read_bytes() if SHIPPED.exists() else b""


# ---------------------------------------------------------------------------
# 1. the repro: PUT /agents/names must not dirty the checkout
# ---------------------------------------------------------------------------

class TestRenameNeverTouchesTheShippedConfig:
    def test_put_agents_names_leaves_the_shipped_file_byte_identical(
            self, relocated):
        before = _shipped_bytes()

        client = api_server.app.test_client()
        resp = client.put('/agents/names', json={"names": {"caretaker": "Nanny"}},
                          headers=AUTH)

        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert resp.get_json()['names']['caretaker'] == "Nanny"
        # The `git status --short` assertion, expressed in bytes: the tracked
        # shipped config is exactly as the repo carries it.
        assert _shipped_bytes() == before

    def test_the_rename_landed_in_the_relocated_config_dir(self, relocated):
        client = api_server.app.test_client()
        client.put('/agents/names', json={"names": {"caretaker": "Nanny"}},
                   headers=AUTH)

        written = relocated / "puffpai_config.json"
        assert written.exists(), f"nothing was written to {written}"
        raw = json.loads(written.read_text(encoding="utf-8"))
        assert raw["agents"]["names"]["caretaker"] == "Nanny"

    def test_the_relocated_copy_keeps_the_rest_of_the_shipped_config(
            self, relocated):
        """Seeded, not created empty — a rename must not cost the owner their
        config. This is why the write is a raw-JSON edit of a seeded file."""
        client = api_server.app.test_client()
        client.put('/agents/names', json={"names": {"steward": "Butler"}},
                   headers=AUTH)

        raw = json.loads(
            (relocated / "puffpai_config.json").read_text(encoding="utf-8"))
        shipped = json.loads(SHIPPED.read_text(encoding="utf-8"))
        for key in shipped:
            if key != "agents":
                assert key in raw, f"the seeded copy lost the '{key}' section"

    def test_a_read_sees_what_the_write_wrote(self, relocated):
        client = api_server.app.test_client()
        client.put('/agents/names', json={"names": {"archivist": "Librarian"}},
                   headers=AUTH)

        resp = client.get('/agents/names', headers=AUTH)
        assert resp.status_code == 200
        assert resp.get_json()['names']['archivist'] == "Librarian"
        # And the module-level reader, which is what the dashboard and the
        # activity log go through.
        prefs.reset_cache()
        assert prefs.display_name("archivist") == "Librarian"


# ---------------------------------------------------------------------------
# 2. the rule, so the class of bug cannot come back quietly
# ---------------------------------------------------------------------------

#: Every module that WRITES puffpai_config.json, and the accessor it resolves
#: its target through. A new entry here is a new writer; a writer that is not
#: here is the bug this file exists for.
CONFIG_WRITERS = {
    "agents.preferences": lambda: prefs._config_path(),
    "hatch": lambda: hatch._config_json_path(),
    "profile_manager": lambda: profile_manager.config_path(),
}


class TestEveryWriterResolvesTheSameFile:
    @pytest.mark.parametrize("name", sorted(CONFIG_WRITERS))
    def test_writer_names_the_loaders_file_on_a_relocated_install(
            self, name, relocated):
        expected = os.path.join(cfgmod.resolve_config_dir(),
                                "puffpai_config.json")
        assert os.path.normpath(CONFIG_WRITERS[name]()) == \
            os.path.normpath(expected)
        # …and that file is NOT the shipped one.
        assert os.path.normpath(expected) != os.path.normpath(str(SHIPPED))

    @pytest.mark.parametrize("name", sorted(CONFIG_WRITERS))
    def test_writer_follows_the_explicit_config_dir_override(
            self, name, tmp_path, monkeypatch):
        monkeypatch.setenv(cfgmod.CONFIG_DIR_ENV, str(tmp_path / "own"))
        cfgmod._instance = None
        prefs.reset_cache()
        assert os.path.normpath(CONFIG_WRITERS[name]()) == os.path.normpath(
            str(tmp_path / "own" / "puffpai_config.json"))
        cfgmod._instance = None
        prefs.reset_cache()

    def test_a_dev_checkout_with_in_tree_state_keeps_the_repo_config(
            self, monkeypatch, tmp_path):
        """The behaviour a developer has today does not move.

        With neither env var set and state in the tree, ``_config_dir_for``
        answers ``<repo>/config`` — so every writer above still names the file
        it always did. (``_dev_checkout_has_state`` decides this; it is stubbed
        rather than faked on disk, because creating state in the real checkout
        is exactly what tests must not do.)
        """
        monkeypatch.delenv(cfgmod.CONFIG_DIR_ENV, raising=False)
        monkeypatch.delenv(cfgmod.STATE_DIR_ENV, raising=False)
        monkeypatch.setattr(cfgmod, "_dev_checkout_has_state",
                            lambda root: True)
        cfgmod._instance = None
        prefs.reset_cache()
        try:
            for name, accessor in sorted(CONFIG_WRITERS.items()):
                assert os.path.normpath(accessor()) == os.path.normpath(
                    str(SHIPPED)), name
        finally:
            cfgmod._instance = None
            prefs.reset_cache()
