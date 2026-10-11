"""Differential tests for :mod:`puffpai.statedir`.

The leaf exists so state-writing modules stop hand-rolling
``$PUFFPAI_DATA_DIR`` handling; that only pays off if the leaf agrees with
:func:`puffpai.config.resolve_state_dir` — the canon — on every input. So
nothing here asserts a hand-written expected path except where the canonical
answer is worth pinning independently: the expectation IS the canon, called
side by side.

Both tiers are covered:

* :func:`~puffpai.statedir.resolve_runtime_dir`, which delegates to the canon
  when ``puffpai.config`` imports (the normal case);
* :func:`~puffpai.statedir._fallback_runtime_dir`, the local replica used when
  ``puffpai.config`` cannot be imported at all. Without a suite driving it, a
  drifted replica would be found only on an appliance whose config had already
  failed to import, i.e. at the worst possible moment.
"""

import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from puffpai import statedir  # noqa: E402
from puffpai.config import STATE_DIR_ENV, resolve_state_dir  # noqa: E402


#: ``$PUFFPAI_DATA_DIR`` values worth distinguishing. ``None`` = unset;
#: ``"tmp"`` is a placeholder the fixture swaps for an absolute tmp_path, the
#: only form an appliance should actually use.
ENV_VALUES = [
    None,              # unset -> the module's shipped default
    "",                # set-but-empty -> same as unset
    "   ",             # whitespace-only -> same as unset (needs the strip)
    "tmp",             # absolute -> honoured verbatim
    "relstate",        # relative -> joined to the REPO ROOT, not the CWD
    "~/puff_state",    # tilde -> expanded
    "../sibling",      # relative and upward -> normalized off the repo root
    "  relstate  ",    # surrounding spaces -> stripped, then as above
]

ENV_SUBDIRS = ["", "uploads"]

MATRIX = [(env, sub) for env in ENV_VALUES for sub in ENV_SUBDIRS]
MATRIX_IDS = [f"env={env!r}-subdir={sub!r}" for env, sub in MATRIX]

#: The shipped-default shape these modules use: ``src/puffpai/data``.
DEFAULT = os.path.join(statedir._REPO_ROOT, "src", "puffpai", "data")


@pytest.fixture
def env_case(request, monkeypatch, tmp_path):
    """Apply one ``(env value, env_subdir)`` case; hand back *env_subdir*."""
    env_value, env_subdir = request.param
    if env_value is None:
        monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    else:
        if env_value == "tmp":
            env_value = str(tmp_path / "state")
        monkeypatch.setenv(STATE_DIR_ENV, env_value)
    return env_subdir


def canonical(env_subdir: str) -> str:
    """What the canon says, for the repo root and default the leaf uses."""
    return resolve_state_dir(
        None,
        repo_root=statedir._REPO_ROOT,
        default=DEFAULT,
        env_subdir=env_subdir,
    )


#: Filesystem shapes that decide whether a *legacy* directory is adopted. Each
#: case is (does the resolved default exist, does the legacy path exist, what
#: kind of legacy path). The adoption rule reads the filesystem, so the shape
#: is as much an input as the env value is — and the interesting half of these
#: cases (legacy present, default absent) is exactly what an appliance looks
#: like the first time it runs a release that moved its state directory.
LEGACY_SHAPES = [
    "no-legacy",          # legacy=None -> the argument must be optional
    "legacy-only",        # the adoption case: old dir there, new one not yet
    "default-only",       # new dir already in use -> legacy is history
    "both-exist",         # ditto, with the old dir still lying around
    "neither-exists",     # fresh box -> nothing to adopt
    "legacy-is-a-file",   # isdir(), not exists(): a stray file is not state
    "same-path",          # legacy == default -> never "adopted" onto itself
    "same-after-normpath",  # ...including when only normpath makes them equal
]


def build_legacy_shape(shape: str, tmp_path):
    """Build one ``(default, legacy)`` pair with the requested dirs on disk.

    Everything lives under *tmp_path*: these cases turn on whether a directory
    exists, so they must never consult (or create) the real repo tree.
    """
    default = str(tmp_path / "newstate")
    legacy = str(tmp_path / "oldstate")

    if shape == "no-legacy":
        legacy = None
    elif shape == "legacy-only":
        os.makedirs(legacy)
    elif shape == "default-only":
        os.makedirs(default)
    elif shape == "both-exist":
        os.makedirs(default)
        os.makedirs(legacy)
    elif shape == "neither-exists":
        pass
    elif shape == "legacy-is-a-file":
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write("not a state directory")
    elif shape == "same-path":
        legacy = default
        os.makedirs(default)
    elif shape == "same-after-normpath":
        # Equal only once normalized, and the directory exists -- the shape
        # that would misfire if a resolver compared the raw strings.
        legacy = os.path.join(tmp_path, "sub", "..", "newstate")
        os.makedirs(default)
    else:  # pragma: no cover - guards a typo in LEGACY_SHAPES
        raise AssertionError(f"unknown legacy shape {shape!r}")

    return default, legacy


def canonical_legacy(default: str, legacy: str | None, env_subdir: str) -> str:
    """What the canon says for a given default/legacy pair."""
    return resolve_state_dir(
        None,
        repo_root=statedir._REPO_ROOT,
        default=default,
        env_subdir=env_subdir,
        legacy=legacy,
    )


LEGACY_MATRIX = [(env, sub, shape)
                 for env, sub in MATRIX for shape in LEGACY_SHAPES]
LEGACY_MATRIX_IDS = [
    f"env={env!r}-subdir={sub!r}-{shape}" for env, sub, shape in LEGACY_MATRIX
]


@pytest.fixture
def legacy_env_case(request, monkeypatch, tmp_path):
    """Apply one ``(env value, env_subdir, legacy shape)`` case.

    Hands back ``(default, legacy, env_subdir)``. Kept separate from
    :func:`env_case` so the existing matrix tests stay exactly as they were.
    """
    env_value, env_subdir, shape = request.param

    if env_value is None:
        monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    else:
        if env_value == "tmp":
            env_value = str(tmp_path / "state")
        monkeypatch.setenv(STATE_DIR_ENV, env_value)

    default, legacy = build_legacy_shape(shape, tmp_path)
    return default, legacy, env_subdir


def _break_config_import(monkeypatch) -> None:
    """Make ``from puffpai.config import ...`` raise, as a broken tree would.

    ``None`` in ``sys.modules`` is how Python marks a module as unimportable;
    the import machinery raises ``ImportError`` rather than re-executing it.
    """
    monkeypatch.setattr(statedir, "_warned", False)
    monkeypatch.setitem(sys.modules, "puffpai.config", None)


# ---------------------------------------------------------------------------
# Tier 1: the delegating entry point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_case", MATRIX, ids=MATRIX_IDS, indirect=True)
def test_resolve_runtime_dir_matches_the_canonical_resolver(env_case) -> None:
    assert statedir.resolve_runtime_dir(
        DEFAULT, env_subdir=env_case) == canonical(env_case)


@pytest.mark.parametrize(
    "legacy_env_case", LEGACY_MATRIX, ids=LEGACY_MATRIX_IDS, indirect=True)
def test_resolve_runtime_dir_matches_the_canon_on_legacy(
    legacy_env_case,
) -> None:
    default, legacy, env_subdir = legacy_env_case
    assert statedir.resolve_runtime_dir(
        default, env_subdir=env_subdir, legacy=legacy,
    ) == canonical_legacy(default, legacy, env_subdir)


# ---------------------------------------------------------------------------
# Tier 2: the replica used when puffpai.config is unimportable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_case", MATRIX, ids=MATRIX_IDS, indirect=True)
def test_fallback_replica_matches_the_canonical_resolver(env_case) -> None:
    assert statedir._fallback_runtime_dir(
        DEFAULT, env_subdir=env_case) == canonical(env_case)


@pytest.mark.parametrize("env_case", MATRIX, ids=MATRIX_IDS, indirect=True)
def test_entry_point_degrades_to_the_replica_when_config_is_unimportable(
    env_case, monkeypatch
) -> None:
    """Drive the real degradation path, not just the replica in isolation."""
    expected = canonical(env_case)  # computed while config still imports
    _break_config_import(monkeypatch)

    assert statedir.resolve_runtime_dir(DEFAULT, env_subdir=env_case) == expected


@pytest.mark.parametrize(
    "legacy_env_case", LEGACY_MATRIX, ids=LEGACY_MATRIX_IDS, indirect=True)
def test_fallback_replica_matches_the_canon_on_legacy(legacy_env_case) -> None:
    default, legacy, env_subdir = legacy_env_case
    assert statedir._fallback_runtime_dir(
        default, env_subdir=env_subdir, legacy=legacy,
    ) == canonical_legacy(default, legacy, env_subdir)


@pytest.mark.parametrize(
    "legacy_env_case", LEGACY_MATRIX, ids=LEGACY_MATRIX_IDS, indirect=True)
def test_legacy_survives_the_degradation_to_the_replica(
    legacy_env_case, monkeypatch
) -> None:
    """Adoption must not change shape on the box whose config just broke."""
    default, legacy, env_subdir = legacy_env_case
    expected = canonical_legacy(default, legacy, env_subdir)
    _break_config_import(monkeypatch)

    assert statedir.resolve_runtime_dir(
        default, env_subdir=env_subdir, legacy=legacy) == expected


def test_config_unavailable_warns_once(caplog, monkeypatch) -> None:
    _break_config_import(monkeypatch)
    with caplog.at_level("WARNING", logger=statedir.logger.name):
        statedir.resolve_runtime_dir(DEFAULT)
        statedir.resolve_runtime_dir(DEFAULT)
    matched = [r for r in caplog.records
               if "puffpai.config unavailable" in r.getMessage()]
    assert len(matched) == 1


def test_config_unavailable_does_not_raise(monkeypatch) -> None:
    _break_config_import(monkeypatch)
    assert statedir.resolve_runtime_dir(DEFAULT)


# ---------------------------------------------------------------------------
# Anchors: things the differential tests take for granted
# ---------------------------------------------------------------------------


def test_repo_root_is_the_repo_root() -> None:
    """A wrong ``_REPO_ROOT`` would make every relative value land elsewhere.

    Both resolvers use it, so the differential tests above would agree with
    each other while every caller wrote state to the wrong tree.
    """
    root = statedir._REPO_ROOT
    assert os.path.isdir(os.path.join(root, "src", "puffpai"))
    assert os.path.isfile(os.path.join(root, "src", "puffpai", "statedir.py"))
    assert os.path.isfile(os.path.join(root, "pytest.ini"))


def test_env_var_name_matches_config() -> None:
    """The leaf duplicates the env-var name; keep the two pinned together."""
    assert statedir._STATE_DIR_ENV == STATE_DIR_ENV


def test_statedir_imports_only_stdlib_at_module_scope() -> None:
    """The leaf must stay importable from anywhere, ``puffpai.config`` included.

    A ``puffpai`` import at module scope would drag the secrets provider back
    into the import graph and defeat the module's reason to exist.
    """
    path = os.path.join(statedir._REPO_ROOT, "src", "puffpai", "statedir.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    imported = set()
    for node in tree.body:  # module scope only; function-local imports are fine
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert imported == {"__future__", "logging", "os"}
    assert not any(name.startswith("puffpai") for name in imported)


def test_unset_env_yields_the_shipped_default(monkeypatch) -> None:
    """One absolute anchor, so the matrix is not purely self-referential."""
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    assert statedir.resolve_runtime_dir(DEFAULT) == os.path.normpath(DEFAULT)
    assert statedir.resolve_runtime_dir(
        DEFAULT, env_subdir="uploads") == os.path.normpath(DEFAULT)


def test_unset_env_adopts_an_existing_legacy_dir(tmp_path, monkeypatch) -> None:
    """The upgrade case, pinned absolutely rather than against the canon."""
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    default, legacy = build_legacy_shape("legacy-only", tmp_path)

    assert statedir.resolve_runtime_dir(default, legacy=legacy) == legacy
    assert statedir._fallback_runtime_dir(default, legacy=legacy) == legacy


def test_env_root_is_never_displaced_by_a_legacy_dir(
    tmp_path, monkeypatch
) -> None:
    """An operator who set $PUFFPAI_DATA_DIR gets it, populated or not.

    This is the half of the rule that is easy to replicate wrongly: adoption
    reads the filesystem, and a replica that ran the check before the env
    branch would quietly send a hardened unit's writes back into the old
    in-tree directory, outside ReadWritePaths.
    """
    env_root = tmp_path / "state"
    monkeypatch.setenv(STATE_DIR_ENV, str(env_root))
    default, legacy = build_legacy_shape("legacy-only", tmp_path)

    for subdir, expected in (("", str(env_root)),
                             ("uploads", str(env_root / "uploads"))):
        assert statedir.resolve_runtime_dir(
            default, env_subdir=subdir, legacy=legacy) == expected
        assert statedir._fallback_runtime_dir(
            default, env_subdir=subdir, legacy=legacy) == expected


def test_replica_warns_when_it_adopts_a_legacy_dir(
    tmp_path, monkeypatch, caplog
) -> None:
    """Adoption is a silent state move unless someone says so out loud."""
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    default, legacy = build_legacy_shape("legacy-only", tmp_path)

    with caplog.at_level("WARNING", logger=statedir.logger.name):
        statedir._fallback_runtime_dir(default, legacy=legacy)

    messages = [r.getMessage() for r in caplog.records]
    assert any("legacy state directory" in m and legacy in m
               for m in messages), messages
