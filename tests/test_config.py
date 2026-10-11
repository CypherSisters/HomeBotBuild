"""Tests for the centralized config module."""

import json
import os
import pytest
from unittest.mock import patch, MagicMock
from puffpai import config as cfgmod
from puffpai.config import (
    get_config, _build_config, PuffPAIConfig,
    OllamaConfig, HAConfig,
    ModelConfig, PathConfig,
    get_llm_backend, get_voice_models,
    CONFIG_DIR_ENV, STATE_DIR_ENV, resolve_state_dir,
)


@pytest.fixture(autouse=True)
def _own_the_path_environment(monkeypatch):
    """This file is the one that tests path RESOLUTION, so it owns the env.

    ``tests/conftest.py`` points ``$PUFFPAI_CONFIG_DIR`` and
    ``$PUFFPAI_DATA_DIR`` at temp directories for every test, so that no test
    can rewrite the shipped config or drop a Home Assistant token into the
    checkout. That is right for every other file and wrong for this one: a test
    here calls ``_build_config(<tmp repo root>)`` precisely to assert what the
    loader does when the environment says nothing, and an override would mean
    it was asserting the override instead.

    Cleared, not redirected: the tests that want a value set one themselves,
    and the ones that want "unset" are testing the fallback chain. Nothing here
    writes to the real directories -- ``_build_config`` only reads, and the
    per-user data dir is redirected by its own fixture in ``conftest.py``.
    """
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    yield


class TestBuildConfig:
    def test_loads_from_disk(self, tmp_path):
        """Test that config loads from a real config file."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "ollama": {"url": "http://test:1234", "model": "test-model"},
        }))

        cfg = _build_config(str(tmp_path))
        assert cfg.ollama.url == "http://test:1234"
        assert cfg.ollama.model == "test-model"
        assert cfg.REPO_ROOT == str(tmp_path)

    def test_defaults_when_missing(self, tmp_path, monkeypatch):
        """Test that defaults are used when config file is missing."""
        monkeypatch.delenv("PUFFPAI_OLLAMA_URL", raising=False)
        cfg = _build_config(str(tmp_path))
        # 11434 = stock Ollama port (the Orin appliance default). The laptop
        # keeps 11435 via config/puffpai_config.json or PUFFPAI_OLLAMA_URL.
        assert cfg.ollama.url == "http://localhost:11434"
        assert cfg.ollama.model == "qwen2.5:7b-instruct"
        assert cfg.ha.enabled is False

    def test_env_overrides_ollama_url(self, tmp_path, monkeypatch):
        """PUFFPAI_OLLAMA_URL wins over the config file."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "ollama": {"url": "http://localhost:11435"},
        }))
        monkeypatch.setenv("PUFFPAI_OLLAMA_URL", "http://orin.local:11434")
        cfg = _build_config(str(tmp_path))
        assert cfg.ollama.url == "http://orin.local:11434"

    def test_loads_ha_config(self, tmp_path):
        """Test Home Assistant config loading."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text("{}")
        (config_dir / "home_assistant_config.json").write_text(json.dumps({
            "url": "http://ha.local:8123",
            "token": "test-token",
            "enabled": True,
        }))

        cfg = _build_config(str(tmp_path))
        assert cfg.ha.url == "http://ha.local:8123"
        assert cfg.ha.enabled is True

    def test_ignores_unknown_keys(self, tmp_path):
        """Test that unknown keys in config don't crash."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "ollama": {"url": "http://test:1234", "unknown_key": "value"},
        }))

        cfg = _build_config(str(tmp_path))
        assert cfg.ollama.url == "http://test:1234"


class TestNoBuiltInHome:
    """Defaults carry no location and no one's Home Assistant entities."""

    def test_no_default_coordinates(self, tmp_path):
        cfg = _build_config(str(tmp_path))
        assert cfg.location == ""
        assert cfg.latitude is None
        assert cfg.longitude is None

    def test_coordinates_load_from_disk(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "latitude": 39.8, "longitude": "-89.6"}))
        cfg = _build_config(str(tmp_path))
        assert (cfg.latitude, cfg.longitude) == (39.8, -89.6)

    @pytest.mark.parametrize("raw", [
        {"latitude": 39.8},
        {"latitude": 39.8, "longitude": None},
        {"latitude": 91, "longitude": 10},
        {"latitude": 10, "longitude": "east"},
        {"latitude": True, "longitude": 10},
    ])
    def test_half_or_invalid_coordinates_are_unset(self, tmp_path, raw):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps(raw))
        cfg = _build_config(str(tmp_path))
        assert (cfg.latitude, cfg.longitude) == (None, None)

    def test_no_default_watched_entities(self, tmp_path):
        cfg = _build_config(str(tmp_path))
        assert cfg.ha_events.watch_entities == []

    def test_calibre_web_defaults_to_the_local_service(self, tmp_path):
        cfg = _build_config(str(tmp_path))
        assert cfg.media_server.calibre_web_url == "http://localhost:8083"


class TestGetConfig:
    def test_returns_config(self):
        """Test that get_config returns a PuffPAIConfig."""
        cfg = get_config(_force_reload=True)
        assert isinstance(cfg, PuffPAIConfig)

    def test_singleton_behavior(self):
        """Test that repeated calls return the same object."""
        cfg1 = get_config(_force_reload=True)
        cfg2 = get_config()
        assert cfg1 is cfg2


# ===========================================================================
# ModelConfig and PathConfig
# ===========================================================================

class TestModelConfig:
    """Tests for ModelConfig dataclass and its loading from JSON."""

    def test_model_config_defaults(self):
        """ModelConfig has sensible defaults for all fields."""
        mc = ModelConfig()
        assert mc.llm_gguf_path == ""
        assert mc.vosk_model_dir == "models/vosk-model-small-en-us"
        assert mc.piper_model_path == "models/en_US-lessac-medium.onnx"
        assert mc.wake_word_model_path == "models/hey_puffpai.tflite"
        assert mc.embedding_model == "all-MiniLM-L6-v2"
        assert mc.llm_n_ctx == 2048
        assert mc.llm_n_gpu_layers == 0

    def test_model_config_from_json(self, tmp_path):
        """ModelConfig loads custom values from JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "models": {
                "llm_gguf_path": "/path/to/model.gguf",
                "vosk_model_dir": "/custom/vosk",
                "llm_n_gpu_layers": 35,
            }
        }))

        cfg = _build_config(str(tmp_path))
        assert cfg.models.llm_gguf_path == "/path/to/model.gguf"
        assert cfg.models.vosk_model_dir == "/custom/vosk"
        assert cfg.models.llm_n_gpu_layers == 35
        # Non-overridden fields keep defaults
        assert cfg.models.embedding_model == "all-MiniLM-L6-v2"

    def test_model_config_ignores_unknown_keys(self, tmp_path):
        """Unknown keys in models section don't crash."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "models": {
                "llm_gguf_path": "test.gguf",
                "future_field": "should_be_ignored",
            }
        }))

        cfg = _build_config(str(tmp_path))
        assert cfg.models.llm_gguf_path == "test.gguf"
        assert not hasattr(cfg.models, "future_field")

    def test_model_config_on_root_config(self):
        """PuffPAIConfig includes models as a field."""
        cfg = get_config(_force_reload=True)
        assert isinstance(cfg.models, ModelConfig)


class TestPathConfig:
    """Tests for PathConfig dataclass and its loading from JSON."""

    def test_path_config_defaults(self):
        """PathConfig has sensible defaults for all fields."""
        pc = PathConfig()
        assert pc.zim_dirs == []
        assert pc.zim_files == []
        assert pc.chromadb_path == "./kiwix_rag_db"
        assert pc.vault_data_dir == "vault_data"
        assert pc.chat_history_dir == "chat_history"
        assert pc.feedback_data_path == "feedback_data.jsonl"

    def test_path_config_from_json(self, tmp_path):
        """PathConfig loads custom values from JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "paths": {
                "zim_dirs": ["F:\\", "D:\\zims"],
                "zim_files": ["/explicit/wiki.zim"],
                "chromadb_path": "/custom/chroma",
            }
        }))

        cfg = _build_config(str(tmp_path))
        assert cfg.paths.zim_dirs == ["F:\\", "D:\\zims"]
        assert cfg.paths.zim_files == ["/explicit/wiki.zim"]
        assert cfg.paths.chromadb_path == "/custom/chroma"
        # Non-overridden fields keep defaults
        assert cfg.paths.vault_data_dir == "vault_data"

    def test_empty_zim_dirs_default(self, tmp_path):
        """Without paths in JSON, zim_dirs defaults to empty list."""
        cfg = _build_config(str(tmp_path))
        assert cfg.paths.zim_dirs == []
        assert cfg.paths.zim_files == []

    def test_chromadb_path_backward_compat(self, tmp_path):
        """Legacy chromadb.path syncs to paths.chromadb_path."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "chromadb": {"path": "/legacy/chroma/db"},
        }))

        cfg = _build_config(str(tmp_path))
        # Legacy chromadb section still loads
        assert cfg.chromadb.path == "/legacy/chroma/db"
        # And syncs to paths.chromadb_path
        assert cfg.paths.chromadb_path == "/legacy/chroma/db"


# ===========================================================================
# LLM Backend Factory + Voice Model Paths
# ===========================================================================

class TestGetLLMBackend:
    """Tests for the get_llm_backend() factory function."""

    def test_prefers_llamacpp_when_gguf_exists(self, tmp_path):
        """get_llm_backend picks LlamaCppBackend when GGUF is configured."""
        model_file = tmp_path / "test.gguf"
        model_file.write_bytes(b"fake-gguf")

        mock_backend = MagicMock()
        mock_backend.is_available.return_value = True

        import puffpai.config as config_mod
        config_mod._backend_instance = None  # reset cache

        cfg = get_config(_force_reload=True)
        cfg.models.llm_gguf_path = str(model_file)

        with patch('puffpai.config.get_config', return_value=cfg), \
             patch('puffpai.inference.llamacpp_backend.LlamaCppBackend',
                   return_value=mock_backend) as MockCls:
            backend = get_llm_backend(_force_reload=True)
            assert backend is mock_backend
            MockCls.assert_called_once()

        config_mod._backend_instance = None

    def test_falls_back_to_ollama(self, tmp_path):
        """get_llm_backend falls back to OllamaBackend when no GGUF."""
        mock_ollama = MagicMock()
        mock_ollama.is_available.return_value = True

        import puffpai.config as config_mod
        config_mod._backend_instance = None

        cfg = get_config(_force_reload=True)
        cfg.models.llm_gguf_path = ""  # no GGUF

        with patch('puffpai.config.get_config', return_value=cfg), \
             patch('puffpai.inference.ollama_backend.OllamaBackend',
                   return_value=mock_ollama):
            backend = get_llm_backend(_force_reload=True)
            assert backend is mock_ollama

        config_mod._backend_instance = None

    def test_raises_when_none_available(self):
        """get_llm_backend raises RuntimeError when nothing works."""
        mock_ollama = MagicMock()
        mock_ollama.is_available.return_value = False

        import puffpai.config as config_mod
        config_mod._backend_instance = None

        cfg = get_config(_force_reload=True)
        cfg.models.llm_gguf_path = ""

        with patch('puffpai.config.get_config', return_value=cfg), \
             patch('puffpai.inference.ollama_backend.OllamaBackend',
                   return_value=mock_ollama), \
             pytest.raises(RuntimeError, match="No LLM backend available"):
            get_llm_backend(_force_reload=True)

        config_mod._backend_instance = None

    def test_caches_instance(self):
        """get_llm_backend returns cached singleton on second call."""
        mock_ollama = MagicMock()
        mock_ollama.is_available.return_value = True

        import puffpai.config as config_mod
        config_mod._backend_instance = None

        cfg = get_config(_force_reload=True)
        cfg.models.llm_gguf_path = ""

        with patch('puffpai.config.get_config', return_value=cfg), \
             patch('puffpai.inference.ollama_backend.OllamaBackend',
                   return_value=mock_ollama):
            b1 = get_llm_backend(_force_reload=True)
            b2 = get_llm_backend()  # should use cache
            assert b1 is b2

        config_mod._backend_instance = None

    def test_force_reload_clears_cache(self):
        """_force_reload=True creates a fresh backend."""
        mock1 = MagicMock()
        mock1.is_available.return_value = True
        mock2 = MagicMock()
        mock2.is_available.return_value = True

        import puffpai.config as config_mod
        config_mod._backend_instance = None

        cfg = get_config(_force_reload=True)
        cfg.models.llm_gguf_path = ""

        with patch('puffpai.config.get_config', return_value=cfg), \
             patch('puffpai.inference.ollama_backend.OllamaBackend',
                   side_effect=[mock1, mock2]):
            b1 = get_llm_backend(_force_reload=True)
            b2 = get_llm_backend(_force_reload=True)
            assert b1 is not b2

        config_mod._backend_instance = None


class TestGetVoiceModels:
    """Tests for get_voice_models() resolved paths."""

    def test_returns_resolved_paths(self):
        """get_voice_models returns dict with absolute paths."""
        result = get_voice_models()
        assert "vosk_model_dir" in result
        assert "piper_model_path" in result
        assert "wake_word_model_path" in result
        # All paths should be absolute
        for key, path in result.items():
            assert os.path.isabs(path), f"{key} should be absolute: {path}"


class TestResolveStateDir:
    """Precedence rules for writable-state directories.

    The appliance runs ProtectSystem=strict, so a package-relative state path
    is a runtime failure (EROFS) that a dev checkout cannot reproduce. These
    tests pin the escape hatches: an absolute config value first, then
    $PUFFPAI_DATA_DIR, then the shipped default.
    """

    def test_absolute_config_wins_over_env(self, tmp_path, monkeypatch):
        """An absolute configured path is honoured verbatim, env or not."""
        monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "from_env"))
        configured = tmp_path / "from_config"
        resolved = resolve_state_dir(
            str(configured), repo_root=str(tmp_path / "repo"),
            default=str(tmp_path / "default"))
        assert resolved == os.path.normpath(str(configured))

    def test_env_wins_over_default(self, tmp_path, monkeypatch):
        """With no configured value, $PUFFPAI_DATA_DIR beats the default."""
        monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "from_env"))
        resolved = resolve_state_dir(
            "", repo_root=str(tmp_path / "repo"),
            default=str(tmp_path / "default"))
        assert resolved == os.path.normpath(str(tmp_path / "from_env"))

    def test_env_subdir_is_appended(self, tmp_path, monkeypatch):
        """A named sub-directory hangs off the shared env root."""
        monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
        resolved = resolve_state_dir(
            "", repo_root=str(tmp_path / "repo"),
            default=str(tmp_path / "default"), env_subdir="vault_data")
        assert resolved == os.path.normpath(str(tmp_path / "state" / "vault_data"))

    def test_env_beats_relative_config(self, tmp_path, monkeypatch):
        """A relative config value is dev convenience; the env var outranks it.

        A relative value always lands inside the code tree, which is exactly
        what is read-only on the appliance, so it must not shadow the
        deployment-level override.
        """
        monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "from_env"))
        resolved = resolve_state_dir(
            "vault_data", repo_root=str(tmp_path / "repo"),
            default=str(tmp_path / "default"))
        assert resolved == os.path.normpath(str(tmp_path / "from_env"))

    def test_relative_config_resolves_against_repo_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv(STATE_DIR_ENV, raising=False)
        resolved = resolve_state_dir(
            "vault_data", repo_root=str(tmp_path / "repo"),
            default=str(tmp_path / "default"))
        assert resolved == os.path.normpath(str(tmp_path / "repo" / "vault_data"))

    def test_default_when_nothing_configured_goes_per_user(
            self, tmp_path, monkeypatch):
        """With nothing configured, state lands in the PER-USER directory.

        Not on the shipped repo-relative *default*, i.e. inside the code tree,
        which is unwritable under a hardened unit or Program Files, shared
        between accounts on a multi-user box, and destroyed by reinstalling over
        the top. ``tests/conftest.py`` points ``platform_paths.user_data_dir``
        at a temp root for the suite, so this asserts the ROUTE, not the literal
        %LOCALAPPDATA% path.
        """
        from puffpai import platform_paths

        monkeypatch.delenv(STATE_DIR_ENV, raising=False)
        resolved = resolve_state_dir(
            "", repo_root=str(tmp_path / "repo"),
            default=str(tmp_path / "default"))
        assert resolved == os.path.normpath(platform_paths.user_data_dir())
        assert resolved != os.path.normpath(str(tmp_path / "default"))

    def test_env_subdir_hangs_off_the_per_user_root(self, tmp_path, monkeypatch):
        """The per-user fallback takes *env_subdir* exactly as the env var does."""
        from puffpai import platform_paths

        monkeypatch.delenv(STATE_DIR_ENV, raising=False)
        resolved = resolve_state_dir(
            "", repo_root=str(tmp_path / "repo"),
            default=str(tmp_path / "default"), env_subdir="vault_data")
        assert resolved == os.path.normpath(
            os.path.join(platform_paths.user_data_dir(), "vault_data"))

    def test_dev_checkout_with_state_is_left_where_it_is(
            self, tmp_path, monkeypatch):
        """A checkout that has already RUN keeps its state; nobody's tree moves.

        The marker is a runtime artefact under ``<repo>/data``. Shipped content
        there (``workout_library.json``) deliberately does NOT count: a fresh
        clone has it and has decided nothing yet.
        """
        monkeypatch.delenv(STATE_DIR_ENV, raising=False)
        repo = tmp_path / "repo"
        (repo / "data").mkdir(parents=True)

        # Shipped content only -> still relocates.
        (repo / "data" / "workout_library.json").write_text("{}", encoding="utf-8")
        cfgmod._dev_state_cache.clear()
        assert resolve_state_dir(
            "", repo_root=str(repo),
            default=str(repo / "data")) != os.path.normpath(str(repo / "data"))

        # A real runtime artefact -> the checkout is kept.
        (repo / "data" / "agent_events.jsonl").write_text("", encoding="utf-8")
        cfgmod._dev_state_cache.clear()
        assert resolve_state_dir(
            "", repo_root=str(repo),
            default=str(repo / "data")) == os.path.normpath(str(repo / "data"))

    def test_legacy_used_only_when_new_location_is_absent(self, tmp_path, monkeypatch):
        """Upgrading a box must not silently start from empty state.

        *legacy* is weighed against
        the shipped default BEFORE the fallback moves anywhere, so a box with a
        previous release's directory does not get handed a fresh empty root.
        """
        monkeypatch.delenv(STATE_DIR_ENV, raising=False)
        legacy = tmp_path / "src" / "vault_data"
        legacy.mkdir(parents=True)
        resolved = resolve_state_dir(
            "", repo_root=str(tmp_path), default=str(tmp_path / "vault_data"),
            legacy=str(legacy))
        assert resolved == os.path.normpath(str(legacy))

        # Once the new location exists it wins.
        (tmp_path / "vault_data").mkdir()
        # ...and this is a checkout that has run, so the shipped default is
        # still the location in play (without that, the answer is the per-user
        # root, which is the point of the two tests above).
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "agent_events.jsonl").write_text("", encoding="utf-8")
        cfgmod._dev_state_cache.clear()
        resolved = resolve_state_dir(
            "", repo_root=str(tmp_path), default=str(tmp_path / "vault_data"),
            legacy=str(legacy))
        assert resolved == os.path.normpath(str(tmp_path / "vault_data"))

    def test_legacy_never_overrides_an_explicit_choice(self, tmp_path, monkeypatch):
        """An operator who names a path gets that path, even if empty."""
        legacy = tmp_path / "src" / "vault_data"
        legacy.mkdir(parents=True)
        chosen = tmp_path / "data" / "vault_data"

        monkeypatch.delenv(STATE_DIR_ENV, raising=False)
        assert resolve_state_dir(
            str(chosen), repo_root=str(tmp_path),
            default=str(tmp_path / "vault_data"),
            legacy=str(legacy)) == os.path.normpath(str(chosen))

        monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
        assert resolve_state_dir(
            "", repo_root=str(tmp_path), default=str(tmp_path / "vault_data"),
            legacy=str(legacy), env_subdir="vault_data",
        ) == os.path.normpath(str(tmp_path / "state" / "vault_data"))

    def test_path_config_exposes_the_state_keys(self):
        """The keys an appliance sets exist with non-committal defaults."""
        pc = PathConfig()
        assert pc.state_dir == ""
        assert pc.agent_events_dir == ""
        assert pc.security_logs_dir == ""

    def test_state_keys_load_from_json(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "paths": {
                "state_dir": "/data/puffpai/state",
                "agent_events_dir": "/data/puffpai/state/events",
                "security_logs_dir": "/data/puffpai/security_logs",
                "vault_data_dir": "/data/puffpai/vault_data",
            }
        }))
        cfg = _build_config(str(tmp_path))
        assert cfg.paths.state_dir == "/data/puffpai/state"
        assert cfg.paths.agent_events_dir == "/data/puffpai/state/events"
        assert cfg.paths.security_logs_dir == "/data/puffpai/security_logs"
        assert cfg.paths.vault_data_dir == "/data/puffpai/vault_data"


class TestVaultPathWiring:
    """The vault directory must follow config, not the package layout.

    Regression: security/vault.py resolved <repo>/src/vault_data (three
    parents up from security/ is src/, not the repo root), so on the appliance
    the vault sat in the read-only tree and had never initialised.
    """

    def test_default_vault_dir_honours_absolute_config(self, tmp_path, monkeypatch):
        from puffpai.security import vault as vault_mod

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        chosen = tmp_path / "data" / "puffpai" / "vault_data"
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "paths": {"vault_data_dir": str(chosen)}
        }))
        monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "ignored"))
        monkeypatch.setattr("puffpai.config._instance",
                            _build_config(str(tmp_path)))

        assert str(vault_mod.default_vault_dir()) == os.path.normpath(str(chosen))

    def test_vault_uses_the_configured_dir(self, tmp_path, monkeypatch):
        from puffpai.security.vault import Vault

        class StubAudit:
            def log(self, *args, **kwargs):
                return {}

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        chosen = tmp_path / "data" / "puffpai" / "vault_data"
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "paths": {"vault_data_dir": str(chosen)}
        }))
        monkeypatch.setattr("puffpai.config._instance",
                            _build_config(str(tmp_path)))

        vault = Vault(audit_log=StubAudit())
        assert str(vault._vault_dir) == os.path.normpath(str(chosen))
        # First use on an empty directory just creates it; no key material
        # exists until initialize() sets a PIN.
        assert chosen.is_dir()
        assert not (chosen / ".vault_key_salt").exists()

    def test_audit_log_dir_honours_absolute_config(self, tmp_path, monkeypatch):
        from puffpai.security import audit_log as audit_mod

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        chosen = tmp_path / "data" / "puffpai" / "security_logs"
        (config_dir / "puffpai_config.json").write_text(json.dumps({
            "paths": {"security_logs_dir": str(chosen)}
        }))
        monkeypatch.setattr("puffpai.config._instance",
                            _build_config(str(tmp_path)))

        assert str(audit_mod.default_log_dir()) == os.path.normpath(str(chosen))
        entry = audit_mod.AuditLog().log("test_action", details="wiring check")
        assert entry["action"] == "test_action"
        assert (chosen / "audit.jsonl").is_file()
