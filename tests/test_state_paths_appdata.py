"""``$PUFFPAI_DATA_DIR`` resolution for the app-data pair.

:mod:`puffpai.app_data` and :mod:`puffpai.suggestions` must not write to a
hardcoded ``<repo>/app_data``. On the appliance the code tree is mounted
read-only (``ProtectSystem=strict``), so every write there would fail; and if
the two modules built that path independently, nothing would stop them
drifting apart. They resolve through :func:`puffpai.statedir.resolve_runtime_dir`
with the SAME default and the SAME ``env_subdir``, which is what these tests
pin: identical directories under both env states, the historical path
byte-for-byte when the env var is unset, and no resolution at import time.

Pure stdlib — no HA, no network. Run: pytest tests/test_state_paths_appdata.py -q
"""

import importlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from puffpai import app_data as ad  # noqa: E402
from puffpai import suggestions as sug  # noqa: E402


#: Repo root, computed the way the modules do: three directories up from
#: ``src/puffpai/<module>.py``.
def _repo_root(mod) -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(mod.__file__))))


@pytest.fixture
def data_dir_env(monkeypatch):
    """Reload both modules under a chosen ``$PUFFPAI_DATA_DIR``.

    Both hold a module-level resolution cache, so the environment has to be in
    place *before* the reload that re-runs the ``= None`` assignment. Teardown
    undoes the env change and reloads once more, so a polluted cache never
    leaks into whatever runs next.
    """
    def _set(env_value):
        if env_value is None:
            monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("PUFFPAI_DATA_DIR", str(env_value))
        importlib.reload(ad)
        importlib.reload(sug)
        return ad, sug

    yield _set

    monkeypatch.undo()
    importlib.reload(ad)
    importlib.reload(sug)


# ── The unset-env default must not move ──────────────────────────────────────

def test_unset_env_keeps_the_literal_historical_app_data_dir(data_dir_env):
    """``<repo>/app_data`` — the exact expression the constant used to hold."""
    app_data, _ = data_dir_env(None)
    assert app_data._data_dir() == os.path.join(_repo_root(app_data), "app_data")


def test_unset_env_keeps_the_literal_historical_store_path(data_dir_env):
    """``<repo>/app_data/suggestions.json``, likewise unchanged."""
    _, suggestions = data_dir_env(None)
    assert suggestions._store_path() == os.path.join(
        _repo_root(suggestions), "app_data", "suggestions.json")


def test_whitespace_only_env_is_treated_as_unset(data_dir_env):
    """A blank unit-file value must not relocate state to an empty path."""
    app_data, suggestions = data_dir_env("   ")
    assert app_data._data_dir() == app_data._DEFAULT_DATA_DIR
    assert suggestions._store_path() == os.path.join(
        suggestions._DEFAULT_DATA_DIR, "suggestions.json")


# ── Env-set: canonical resolve_state_dir semantics ───────────────────────────

def test_absolute_env_relocates_both_under_one_app_data_subdir(data_dir_env, tmp_path):
    app_data, suggestions = data_dir_env(tmp_path)
    expected = os.path.join(str(tmp_path), "app_data")
    assert app_data._data_dir() == expected
    assert suggestions._store_path() == os.path.join(expected, "suggestions.json")


def test_relative_env_joins_the_repo_root_not_the_cwd(data_dir_env, monkeypatch, tmp_path):
    """The canon resolves a relative value against the repo, never the CWD.

    A raw ``os.environ.get`` would have followed whatever directory the service
    happened to start in — which is how state ends up outside ReadWritePaths.
    """
    monkeypatch.chdir(tmp_path)
    app_data, suggestions = data_dir_env("relstate")
    expected = os.path.join(_repo_root(app_data), "relstate", "app_data")
    assert app_data._data_dir() == expected
    assert suggestions._store_path() == os.path.join(expected, "suggestions.json")
    assert str(tmp_path) not in app_data._data_dir()


def test_tilde_env_is_expanded(data_dir_env):
    app_data, suggestions = data_dir_env("~/puff_state")
    expected = os.path.join(os.path.expanduser("~"), "puff_state", "app_data")
    assert "~" not in app_data._data_dir()
    assert app_data._data_dir() == expected
    assert suggestions._store_path() == os.path.join(expected, "suggestions.json")


# ── The two modules must never separate ──────────────────────────────────────

@pytest.mark.parametrize("env_value", [None, "   ", "relstate", "~/puff_state"])
def test_app_data_and_suggestions_always_share_a_directory(data_dir_env, env_value):
    """suggestions.json is *about* the app data; it must cohabit with it."""
    app_data, suggestions = data_dir_env(env_value)
    assert os.path.dirname(suggestions._store_path()) == app_data._data_dir()


def test_app_data_and_suggestions_share_an_absolute_data_dir(data_dir_env, tmp_path):
    app_data, suggestions = data_dir_env(tmp_path)
    assert os.path.dirname(suggestions._store_path()) == app_data._data_dir()


# ── Laziness: nothing resolves at import ─────────────────────────────────────

def test_import_does_not_resolve_the_state_dir(data_dir_env, tmp_path):
    """Resolution reaches puffpai.config, which reaches the secrets provider.

    Doing that while the module is still importing re-creates the import cycle
    documented in puffpai.agent_events, so the cache must still be empty
    immediately after a reload.
    """
    app_data, suggestions = data_dir_env(tmp_path)
    assert app_data._data_dir_cache is None
    assert suggestions._data_dir_cache is None
    app_data._data_dir()
    assert app_data._data_dir_cache is not None


def test_reload_resets_the_cache_so_a_new_env_takes_effect(data_dir_env, tmp_path):
    app_data, _ = data_dir_env(tmp_path / "first")
    assert app_data._data_dir() == os.path.join(str(tmp_path / "first"), "app_data")
    app_data, _ = data_dir_env(tmp_path / "second")
    assert app_data._data_dir() == os.path.join(str(tmp_path / "second"), "app_data")


# ── Writes actually land in the relocated directory ──────────────────────────

def test_appdata_instance_creates_and_uses_the_relocated_dir(data_dir_env, tmp_path):
    app_data, _ = data_dir_env(tmp_path)
    store = app_data.AppData()
    expected = tmp_path / "app_data"
    assert expected.is_dir(), "AppData() must create the resolved directory"
    for app in store.VALID_APPS:
        assert store._files[app] == os.path.join(str(expected), f"{app}.json")


def test_suggestion_store_round_trips_through_the_relocated_path(data_dir_env, tmp_path):
    _, suggestions = data_dir_env(tmp_path)
    suggestions._save([{"id": "s1", "kind": "schedule"}])

    landed = tmp_path / "app_data" / "suggestions.json"
    assert landed.is_file(), "the store must be written under $PUFFPAI_DATA_DIR"
    assert json.loads(landed.read_text(encoding="utf-8"))[0]["id"] == "s1"
    assert suggestions._load()[0]["id"] == "s1"


# ── The override hook is a plain global, not a PEP 562 shim ─────────────────

def test_store_path_hook_is_a_plain_none_global(data_dir_env, tmp_path,
                                                monkeypatch):
    """``_STORE_PATH`` is the override hook, defaulting to ``None``.

    It was briefly served by a module ``__getattr__`` instead. That broke
    ``monkeypatch.setattr``: the synthesized path was recorded as the "old"
    value, so teardown did not remove the name — it PINNED that path into the
    globals, silently outranking ``$PUFFPAI_DATA_DIR`` for the rest of the
    process and routing writes back into the real repo tree. A plain ``None``
    restores cleanly, which is what the round-trip below pins.
    """
    _, suggestions = data_dir_env(tmp_path)
    assert suggestions._STORE_PATH is None, "must be a real global, not a shim"
    resolved = os.path.join(str(tmp_path), "app_data", "suggestions.json")
    assert suggestions._store_path() == resolved

    with monkeypatch.context() as m:
        m.setattr(suggestions, "_STORE_PATH", str(tmp_path / "x.json"))
        assert suggestions._store_path() == str(tmp_path / "x.json")

    # Teardown must hand control back to the environment, not pin a path.
    assert suggestions._STORE_PATH is None
    assert suggestions._store_path() == resolved

    with pytest.raises(AttributeError):
        suggestions.NO_SUCH_PATH


def test_store_path_attribute_override_redirects_writes(data_dir_env, tmp_path, monkeypatch):
    """tests/test_suggestions.py isolates the store by assigning this name."""
    _, suggestions = data_dir_env(tmp_path)
    elsewhere = tmp_path / "elsewhere" / "suggestions.json"
    monkeypatch.setattr(suggestions, "_STORE_PATH", str(elsewhere))

    suggestions._save([{"id": "override"}])

    assert elsewhere.is_file()
    assert not (tmp_path / "app_data" / "suggestions.json").exists()
