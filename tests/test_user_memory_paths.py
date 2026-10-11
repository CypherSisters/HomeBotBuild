"""User-memory state-dir resolution — where per-user facts actually land.

:class:`~puffpai.user_memory.UserMemory` used to walk FOUR ``dirname()`` calls
up from ``src/puffpai/user_memory.py``, which lands on the repo's PARENT while
the comment claimed ``repo_root/user_memory``: every user's facts, preferences
and conversation history were written outside the checkout, and on a hardened
unit outside ``ReadWritePaths`` as well. It now resolves through
:func:`puffpai.statedir.resolve_runtime_dir`, with the old location kept as the
``legacy`` fallback so an existing box adopts its memory instead of starting
empty.

No writes touch the real repo tree: every case points the module's path
constants at ``tmp_path`` before constructing, because ``__init__`` creates the
directory it resolves. Run: pytest tests/test_user_memory_paths.py -q
"""

import json
import os

import pytest

from puffpai import user_memory as um
from puffpai.user_memory import UserMemory


@pytest.fixture(autouse=True)
def _no_ambient_state_dir(monkeypatch):
    """A PUFFPAI_DATA_DIR in the developer's shell must not decide these."""
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Point both module constants at tmp_path; neither directory exists yet.

    The legacy constant especially: this machine may well have a real
    ``C:\\Users\\owner\\PuffPAI\\user_memory`` left by the buggy version, and a
    test that let the module see it would flip to adoption and pass for the
    wrong reason.
    """
    default = tmp_path / "repo" / "user_memory"
    legacy = tmp_path / "parent" / "user_memory"
    monkeypatch.setattr(um, "_DEFAULT_MEMORY_DIR", str(default))
    monkeypatch.setattr(um, "_LEGACY_MEMORY_DIR", str(legacy))
    return default, legacy


# ---------------------------------------------------------------------------
# Where the memory file lands
# ---------------------------------------------------------------------------


def test_unset_env_with_no_legacy_dir_uses_the_repo_default(paths):
    default, _legacy = paths
    memory = UserMemory("alex")

    assert memory.memory_dir == os.path.normpath(str(default))
    assert os.path.isdir(default)


def test_env_root_wins_even_when_a_legacy_dir_exists(paths, tmp_path,
                                                     monkeypatch):
    """The hardened-unit case: $PUFFPAI_DATA_DIR is a decision, not a hint."""
    _default, legacy = paths
    legacy.mkdir(parents=True)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))

    memory = UserMemory("alex")

    assert memory.memory_dir == str(tmp_path / "state" / "user_memory")
    assert not any((tmp_path / "state" / "user_memory").iterdir())  # not legacy


def test_existing_legacy_dir_is_adopted_when_the_default_is_absent(paths):
    """An upgraded box keeps the memory the buggy path had accumulated."""
    _default, legacy = paths
    legacy.mkdir(parents=True)
    (legacy / "alex.json").write_text(
        json.dumps({"user_id": "alex", "topics": {"chickens": 3},
                    "facts": [], "preferences": {}, "recent_topics": [],
                    "last_updated": None}),
        encoding="utf-8")

    memory = UserMemory("alex")

    assert memory.memory_dir == os.path.normpath(str(legacy))
    assert memory.data["topics"] == {"chickens": 3}, "adopted, not re-created"


def test_default_dir_in_use_is_not_displaced_by_a_legacy_dir(paths):
    _default, legacy = paths
    _default.mkdir(parents=True)
    legacy.mkdir(parents=True)

    assert UserMemory("alex").memory_dir == os.path.normpath(str(_default))


def test_explicit_memory_dir_overrides_everything(paths, tmp_path, monkeypatch):
    _default, legacy = paths
    legacy.mkdir(parents=True)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    chosen = tmp_path / "explicit"

    memory = UserMemory("alex", memory_dir=str(chosen))

    assert memory.memory_dir == str(chosen)
    assert memory._file == str(chosen / "alex.json")


def test_memory_round_trips_through_the_resolved_dir(paths):
    default, _legacy = paths
    memory = UserMemory("alex")
    memory.data["preferences"]["response_style"] = "brief"
    memory.save()

    assert (default / "alex.json").is_file()
    assert UserMemory("alex").data["preferences"]["response_style"] == "brief"


# ---------------------------------------------------------------------------
# Anchors: the constants the cases above monkeypatch
# ---------------------------------------------------------------------------


def test_repo_root_is_the_repo_root_not_its_parent():
    """The off-by-one itself. Patched away in every case above, so pin it."""
    assert os.path.isfile(os.path.join(um._REPO_ROOT, "src", "puffpai",
                                       "user_memory.py"))
    assert os.path.isfile(os.path.join(um._REPO_ROOT, "pytest.ini"))


def test_default_is_in_the_repo_and_legacy_is_the_old_parent_path():
    assert um._DEFAULT_MEMORY_DIR == os.path.join(um._REPO_ROOT, "user_memory")
    assert um._LEGACY_MEMORY_DIR == os.path.join(
        os.path.dirname(um._REPO_ROOT), "user_memory")
    assert um._LEGACY_MEMORY_DIR != um._DEFAULT_MEMORY_DIR


def test_import_does_not_create_or_resolve_anything(paths):
    """Resolution is deferred to construction; importing must be inert."""
    default, legacy = paths
    assert not default.exists() and not legacy.exists()
