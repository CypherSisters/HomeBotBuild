"""State-dir resolution for the two odd-shaped writers: emergency prep and
profiles.

Both wrote to hardcoded repo-relative paths — ``<repo>/emergency_inventory.json``
and ``<repo>/config/`` — which are inside the read-only code tree on a hardened
unit (``ProtectSystem=strict`` with ``ReadWritePaths=/data/puffpai``). The
inventory write raises outright there; ProfileManager's two writes lose profile
edits and the assistant's own identity. Both now resolve through
:func:`puffpai.statedir.resolve_runtime_dir`, the one place that defines what
``$PUFFPAI_DATA_DIR`` means.

Nothing here touches the network or the real repo tree: every case that writes
points the module at *tmp_path* first, via the env var or the override hook.
Run: pytest tests/test_misc_state_paths.py -q
"""

import hashlib
import json
import os

import pytest

from puffpai import config as cfgmod
from puffpai import emergency_prep as ep
from puffpai import profile_manager as pm
from puffpai import statedir
from puffpai.config import resolve_state_dir


#: profile_manager serves these three from module __getattr__ (PEP 562), so
#: they exist as attributes but not as globals until something assigns one.
_PM_PATH_NAMES = ("_CONFIG_DIR", "_PROFILES_PATH", "_CONFIG_PATH")


@pytest.fixture(autouse=True)
def _no_override_hooks(monkeypatch):
    """Start and end every case with no override in place.

    The pop is not belt-and-braces. ``monkeypatch.setattr`` saves the old value
    with ``getattr``, which on a PEP 562 module returns the RESOLVED string --
    so its undo assigns that string back as a real global and freezes the name
    for the rest of the session. Popping restores the dynamic behaviour.
    """
    monkeypatch.setattr(ep, "_INVENTORY_FILE", None)
    for name in _PM_PATH_NAMES:
        pm.__dict__.pop(name, None)
    yield
    for name in _PM_PATH_NAMES:
        pm.__dict__.pop(name, None)


@pytest.fixture(autouse=True)
def _the_real_config_file_is_never_written():
    """Hard guard: puffpai_config.json is a FIXED repo path now.

    Any test that calls set_identity() without pinning _CONFIG_PATH would
    rewrite the developer's real <repo>/config/puffpai_config.json. Fail the
    test instead of corrupting the checkout. Digest, not content, so a failure
    cannot dump the file into the log.
    """
    real = os.path.join(pm._REPO_ROOT, "config", "puffpai_config.json")

    def digest():
        try:
            with open(real, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except FileNotFoundError:
            return None

    before = digest()
    yield
    assert digest() == before, f"test wrote to the real config file: {real}"


# ---------------------------------------------------------------------------
# EmergencyPrep — the inventory lives at the repo ROOT, not under data/
# ---------------------------------------------------------------------------


class TestEmergencyInventoryPath:
    def test_unset_env_keeps_the_repo_root_default(self, monkeypatch):
        """Byte-identical to the historical constant."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        assert ep._inventory_path() == os.path.join(
            ep._REPO_ROOT, "emergency_inventory.json")

    def test_env_set_relocates_to_the_state_dir_root(self, tmp_path, monkeypatch):
        """A "data"-dir file lands at the $PUFFPAI_DATA_DIR root, no subdir —
        the agent_events.jsonl precedent."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        assert ep._inventory_path() == os.path.join(
            str(tmp_path / "state"), "emergency_inventory.json")

    def test_relative_env_joins_the_repo_root_not_the_cwd(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        assert ep._inventory_path() == os.path.join(
            statedir._REPO_ROOT, "relstate", "emergency_inventory.json")

    def test_tilde_env_is_expanded(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "~/puff_state")
        resolved = ep._inventory_path()
        assert "~" not in resolved
        assert resolved == os.path.join(
            os.path.expanduser("~"), "puff_state", "emergency_inventory.json")

    @pytest.mark.parametrize("env_value", [None, "", "   ", "relstate",
                                           "~/puff_state", "<abs>"])
    def test_matches_the_canonical_resolver(self, env_value, tmp_path, monkeypatch):
        if env_value == "<abs>":
            env_value = str(tmp_path / "state")
        if env_value is None:
            monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

        assert ep._inventory_path() == os.path.join(resolve_state_dir(
            None, repo_root=statedir._REPO_ROOT, default=ep._REPO_ROOT),
            "emergency_inventory.json")

    def test_override_hook_wins_over_the_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        pinned = str(tmp_path / "pinned_inventory.json")
        monkeypatch.setattr(ep, "_INVENTORY_FILE", pinned)
        assert ep._inventory_path() == pinned
        assert ep.EmergencyPrep().inventory_file == pinned

    def test_save_creates_the_state_dir_and_writes_there(self, tmp_path, monkeypatch):
        """A fresh $PUFFPAI_DATA_DIR has no directory yet; the write must not
        fail on that."""
        state = tmp_path / "state" / "nested"
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(state))
        prep = ep.EmergencyPrep()
        assert not state.exists(), "constructing must not create anything"

        prep.add_supply("water", "bottled water", 12, "gallons")

        inventory = state / "emergency_inventory.json"
        assert inventory.is_file()
        data = json.loads(inventory.read_text(encoding="utf-8"))
        assert data["supplies"]["water"][0]["quantity"] == 12

    def test_missing_inventory_falls_back_to_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "empty"))
        assert ep.EmergencyPrep().inventory["supplies"] == {}


# ---------------------------------------------------------------------------
# ProfileManager — a directory-shaped state dir, so it keeps its name under $ENV
# ---------------------------------------------------------------------------


class TestProfileManagerPaths:
    def test_unset_env_keeps_the_repo_config_dir(self, monkeypatch):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        monkeypatch.delenv("PUFFPAI_CONFIG_DIR", raising=False)
        monkeypatch.setattr(cfgmod, "_dev_checkout_has_state", lambda root: True)
        cfgmod._instance = None
        expected = os.path.join(pm._REPO_ROOT, "config")
        assert pm.config_dir() == expected
        assert pm.profiles_path() == os.path.join(expected, "profiles.json")
        assert pm.config_path() == os.path.join(expected, "puffpai_config.json")

    def test_env_set_relocates_profiles_and_the_config_file_together(
            self, tmp_path, monkeypatch):
        """The state/config split.

        profiles.json is runtime state and follows the env var, keeping its
        "config" dir name under the data root (env_subdir). puffpai_config.json
        is CONFIG, and it moves too -- but it moves because the LOADER moved it:
        ``config._config_dir_for`` relocates a non-dev install's config dir
        beside its state and seeds it from the shipped copy. This module writes
        that file (set_identity), so naming anything else would mean writing the
        tracked shipped config while the appliance read the relocated one.
        """
        monkeypatch.delenv("PUFFPAI_CONFIG_DIR", raising=False)
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        cfgmod._instance = None
        assert pm.config_dir() == str(tmp_path / "state" / "config")
        assert pm.profiles_path() == str(
            tmp_path / "state" / "config" / "profiles.json")
        assert pm.config_path() == os.path.join(
            cfgmod.resolve_config_dir(), "puffpai_config.json")
        assert pm.config_path() != os.path.join(
            pm._REPO_ROOT, "config", "puffpai_config.json")

    def test_the_config_file_is_the_loaders_file_under_every_env_value(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv("PUFFPAI_CONFIG_DIR", raising=False)
        # Absolute values only. ``resolve_config_dir`` SEEDS the directory it
        # answers with (that is how a relocated install gets a writable copy of
        # the shipped config), so a relative value here would create
        # ``<repo>/relstate/config`` in the developer's checkout, the very
        # thing this file guards against. Relative resolution is covered by
        # ``test_relative_env_joins_the_repo_root_not_the_cwd``, which does not
        # reach the seeder.
        for env_value in ("", "   ", str(tmp_path / "state"),
                          str(tmp_path / "other")):
            monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)
            cfgmod._instance = None
            assert pm.config_path() == os.path.join(
                cfgmod.resolve_config_dir(), "puffpai_config.json")
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        cfgmod._instance = None
        assert pm.config_path() == os.path.join(
            cfgmod.resolve_config_dir(), "puffpai_config.json")

    def test_the_config_dir_override_does_not_move_the_config_file(
            self, tmp_path, monkeypatch):
        """``_CONFIG_DIR`` is this module's STATE hook: it moves profiles.json
        only. The identity file follows the loader (or ``_CONFIG_PATH``)."""
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        setattr(pm, "_CONFIG_DIR", str(tmp_path / "pinned"))
        assert pm.profiles_path() == str(tmp_path / "pinned" / "profiles.json")
        assert pm.config_path() == os.path.join(
            cfgmod.resolve_config_dir(), "puffpai_config.json")

    def test_relative_env_joins_the_repo_root_not_the_cwd(self, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", "relstate")
        assert pm.config_dir() == os.path.join(
            statedir._REPO_ROOT, "relstate", "config")

    @pytest.mark.parametrize("env_value", [None, "", "   ", "relstate",
                                           "~/puff_state", "<abs>"])
    def test_matches_the_canonical_resolver(self, env_value, tmp_path, monkeypatch):
        if env_value == "<abs>":
            env_value = str(tmp_path / "state")
        if env_value is None:
            monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        else:
            monkeypatch.setenv("PUFFPAI_DATA_DIR", env_value)

        assert pm.config_dir() == resolve_state_dir(
            None, repo_root=statedir._REPO_ROOT,
            default=pm._DEFAULT_CONFIG_DIR, env_subdir="config")

    def test_dir_override_wins_over_the_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        setattr(pm, "_CONFIG_DIR", str(tmp_path / "pinned"))
        assert pm.config_dir() == str(tmp_path / "pinned")
        assert pm.profiles_path() == str(tmp_path / "pinned" / "profiles.json")

    def test_file_overrides_win_and_move_only_their_own_file(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        setattr(pm, "_PROFILES_PATH", str(tmp_path / "p.json"))
        assert pm.profiles_path() == str(tmp_path / "p.json")
        assert pm.config_path() == os.path.join(
            cfgmod.resolve_config_dir(), "puffpai_config.json")

        setattr(pm, "_CONFIG_PATH", str(tmp_path / "c.json"))
        assert pm.config_path() == str(tmp_path / "c.json")

    def test_a_path_override_is_honoured_and_normalized(self, tmp_path, monkeypatch):
        """os.fspath, not str-only: a Path override must not be resolved
        around, or state lands somewhere the operator did not ask for.

        The accessor normalizes to str. The bare attribute reads back exactly
        what was assigned -- PEP 562 cannot intercept a name that is present in
        the globals -- which is still os.PathLike, so open() takes it either
        way. Anything reading the attribute must therefore not assume str.
        """
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        setattr(pm, "_PROFILES_PATH", tmp_path / "pathlib.json")
        assert pm.profiles_path() == str(tmp_path / "pathlib.json")
        assert os.fspath(pm._PROFILES_PATH) == str(tmp_path / "pathlib.json")
        # The instance takes the normalized value, so writes are str-pathed.
        assert pm.ProfileManager()._profiles_path == str(tmp_path / "pathlib.json")

    def test_a_junk_override_is_ignored(self, tmp_path, monkeypatch):
        """A bare MagicMock answers __fspath__ with a junk name; honouring it
        would write profiles to a junk path relative to the CWD."""
        from unittest import mock
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        setattr(pm, "_PROFILES_PATH", mock.MagicMock())
        assert pm.profiles_path() == str(
            tmp_path / "state" / "config" / "profiles.json")

    def test_deleting_an_override_restores_dynamic_resolution(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        setattr(pm, "_CONFIG_DIR", str(tmp_path / "pinned"))
        assert pm.config_dir() == str(tmp_path / "pinned")
        delattr(pm, "_CONFIG_DIR")
        assert pm.config_dir() == str(tmp_path / "state" / "config")


class TestProfilePathAttributeShim:
    """The three names are served by module __getattr__ (PEP 562).

    api_server's /fitness/program used to do ``from puffpai.profile_manager
    import _PROFILES_PATH`` inside the request handler and pass the value
    straight to open(); it now calls ``profiles_path()``. The shim keeps the
    old by-value spelling working -- resolved, never None -- for anything out
    of tree that still uses it.
    """

    def test_value_import_yields_the_resolved_path(self, monkeypatch):
        monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
        from puffpai.profile_manager import _PROFILES_PATH

        assert _PROFILES_PATH == pm.profiles_path()
        assert _PROFILES_PATH is not None
        assert os.path.isabs(_PROFILES_PATH)

    def test_value_import_relocates_with_the_env(self, tmp_path, monkeypatch):
        """A by-value importer re-imports per call, so a relocated deployment
        must be reflected without a restart."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        from puffpai.profile_manager import _PROFILES_PATH

        assert _PROFILES_PATH == str(
            tmp_path / "state" / "config" / "profiles.json")

    def test_the_endpoints_own_call_shape_loads_a_written_file(
            self, tmp_path, monkeypatch):
        """End to end in the shape the endpoint used before it switched to
        ``profiles_path()``: ProfileManager writes, then a by-value import
        reads the same file back."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        pm.ProfileManager().create_profile("Alex")

        from puffpai.profile_manager import _PROFILES_PATH
        with open(_PROFILES_PATH, encoding="utf-8") as fh:
            loaded = json.load(fh)
        assert [p["display_name"] for p in loaded["profiles"]] == ["Alex"]

    def test_all_three_names_resolve_and_agree_with_the_accessors(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        assert pm._CONFIG_DIR == pm.config_dir()
        assert pm._PROFILES_PATH == pm.profiles_path()
        assert pm._CONFIG_PATH == pm.config_path()

    def test_an_unknown_name_still_raises_attribute_error(self):
        with pytest.raises(AttributeError):
            pm._NOT_A_REAL_NAME

    def test_the_names_are_absent_from_globals_until_overridden(self):
        """Absent from __dict__ is what lets __getattr__ fire at all."""
        for name in _PM_PATH_NAMES:
            assert name not in pm.__dict__
            assert getattr(pm, name)  # served dynamically


class TestProfileManagerWrites:
    """Functional writes. The identity file is a FIXED repo path now, so every
    case here pins _CONFIG_PATH into tmp_path -- otherwise the write would land
    on the developer's real config (the autouse guard above would fail it)."""

    def test_writes_land_in_the_state_dir_and_create_it(
            self, tmp_path, monkeypatch):
        state = tmp_path / "state" / "nested"
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(state))
        config = tmp_path / "pinned" / "puffpai_config.json"
        setattr(pm, "_CONFIG_PATH", str(config))

        manager = pm.ProfileManager()
        profile = manager.create_profile("Alex", avatar_seed="rach")
        manager.set_identity(name="PuffPAI")

        profiles = state / "config" / "profiles.json"
        assert profiles.is_file(), "state dir created by the profiles write"
        assert config.is_file(), "config parent created by the identity write"
        assert json.loads(profiles.read_text(encoding="utf-8"))[
            "profiles"][0]["display_name"] == "Alex"
        assert json.loads(config.read_text(encoding="utf-8"))[
            "identity"]["name"] == "PuffPAI"
        assert profile["id"].startswith("usr_")

    def test_each_write_site_makes_its_own_parent(self, tmp_path, monkeypatch):
        """The two files can now sit in unrelated directories, so neither write
        may depend on the other's parent having been created."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        setattr(pm, "_CONFIG_PATH", str(tmp_path / "far" / "away" / "cfg.json"))

        pm.ProfileManager().set_identity(name="PuffPAI")

        assert (tmp_path / "far" / "away" / "cfg.json").is_file()
        assert not (tmp_path / "state" / "config" / "profiles.json").exists()

    def test_reads_and_writes_resolve_to_the_same_place(self, tmp_path, monkeypatch):
        """The identity merge READS puffpai_config.json before rewriting it; a
        read that resolved differently from the write would drop the other
        keys in that file on every save."""
        state = tmp_path / "state"
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(state))
        config = tmp_path / "pinned" / "puffpai_config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"unrelated": {"keep": True}}),
                          encoding="utf-8")
        setattr(pm, "_CONFIG_PATH", str(config))

        first = pm.ProfileManager()
        first.create_profile("Alex")
        first.set_identity(name="PuffPAI", avatar_style="bottts")

        merged = json.loads(config.read_text(encoding="utf-8"))
        assert merged["unrelated"] == {"keep": True}, "preserved other keys"

        second = pm.ProfileManager()
        assert second.get_identity()["name"] == "PuffPAI"
        assert second.get_identity()["avatar_style"] == "bottts"
        assert [p["display_name"] for p in second.get_all_profiles()] == ["Alex"]

    def test_construction_survives_an_unwritable_config_dir(
            self, tmp_path, monkeypatch):
        """The identity file is read-only on a hardened unit. Constructing a
        ProfileManager must not care -- only set_identity() may raise."""
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        setattr(pm, "_CONFIG_PATH", str(tmp_path / "nonexistent" / "cfg.json"))

        manager = pm.ProfileManager()  # must not raise, and must not mkdir
        assert not (tmp_path / "nonexistent").exists()
        assert manager.get_identity()["name"] == "PuffPAI", "shipped default"

    def test_nothing_is_written_to_the_repo_config_dir(self, tmp_path, monkeypatch):
        """Guard against a half-migrated write site: with the env set, the real
        <repo>/config must not gain, lose, or change a file.

        Compared by digest, not content — puffpai_config.json is a real config
        file and a failing assert must not dump it into the test log.
        """
        monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
        setattr(pm, "_CONFIG_PATH", str(tmp_path / "pinned.json"))
        repo_config = os.path.join(pm._REPO_ROOT, "config")

        def snapshot():
            if not os.path.isdir(repo_config):
                return {}
            return {
                name: hashlib.sha256(
                    open(os.path.join(repo_config, name), "rb").read()).hexdigest()
                for name in sorted(os.listdir(repo_config))
                if os.path.isfile(os.path.join(repo_config, name))
            }

        before = snapshot()

        manager = pm.ProfileManager()
        manager.create_profile("Alex")
        manager.set_identity(name="PuffPAI")

        assert snapshot() == before
