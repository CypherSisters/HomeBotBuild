"""Release gate: relative storage paths must land under $PUFFPAI_DATA_DIR.

The shipped `config/puffpai_config.json` gives `vault_data`, `chat_history`,
`./kiwix_rag_db` and friends as RELATIVE paths, which resolve inside the code
tree. On the appliance that tree is `/opt/puffpai`, mounted read-only by
`puffpai-api.service` (`ProtectSystem=strict`, `ReadWritePaths=/data/puffpai`),
so the Vault and the knowledge index fail with EROFS the moment they are
touched — while looking perfectly fine in a dev checkout. The install doc used
to ask the owner to hand-edit eight keys; a stranger following `install_orin.sh`
does not.

`puffpai.config.anchor_relative_state_paths` moves that into code: when
`$PUFFPAI_DATA_DIR` names a writable root, every relative storage path is
re-rooted under it. Absolute paths are untouched; with the variable unset,
nothing changes at all.
"""
import json
import os
from pathlib import Path

import pytest

from puffpai import config as cfgmod

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = REPO_ROOT / "config" / "puffpai_config.json"


def _pair(**overrides):
    """A fresh (PathConfig, ChromaDBConfig) pair with the shipped defaults."""
    path_cfg = cfgmod.PathConfig(**overrides)
    return path_cfg, cfgmod.ChromaDBConfig()


def test_shipped_config_still_uses_relative_storage_paths():
    """Guards the premise: if these ever ship absolute, this file is moot."""
    raw = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    relative = [
        k for k in ("chromadb_path", "vault_data_dir", "chat_history_dir",
                    "feedback_data_path")
        if isinstance(raw.get("paths", {}).get(k), str)
        and raw["paths"][k]
        and not os.path.isabs(raw["paths"][k])
    ]
    assert relative, (
        "the shipped config no longer has relative storage paths; either the "
        "fix moved into the file or someone dropped the keys"
    )


def test_every_relocatable_key_is_a_real_pathconfig_field():
    for key in cfgmod.RELOCATABLE_PATH_KEYS:
        assert key in cfgmod.PathConfig.__dataclass_fields__, key


def test_relative_paths_are_rerooted_under_the_data_dir(monkeypatch, tmp_path):
    root = tmp_path / "state"
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(root))
    path_cfg, chroma = _pair()
    cfgmod.anchor_relative_state_paths(
        path_cfg, chroma, repo_root=str(REPO_ROOT))

    assert path_cfg.vault_data_dir == str(root / "vault_data")
    assert path_cfg.chat_history_dir == str(root / "chat_history")
    assert path_cfg.chromadb_path == str(root / "kiwix_rag_db")
    assert path_cfg.feedback_data_path == str(root / "feedback_data.jsonl")
    # The legacy "chromadb" section names the same directory; leaving it
    # relative would send whichever reader still uses it to the read-only tree.
    assert chroma.path == str(root / "kiwix_rag_db")

    for key in cfgmod.RELOCATABLE_PATH_KEYS:
        value = getattr(path_cfg, key)
        if value:
            assert os.path.isabs(value), f"{key} is still relative: {value!r}"
            assert str(root) == os.path.commonpath([str(root), value])


def test_absolute_paths_are_left_alone(monkeypatch, tmp_path):
    """An operator who wrote /mnt/big/chroma meant it."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    chosen = os.path.abspath(os.sep + os.path.join("mnt", "big", "chroma"))
    vault = os.path.abspath(os.sep + os.path.join("srv", "vault"))
    path_cfg, chroma = _pair(chromadb_path=chosen, vault_data_dir=vault)
    cfgmod.anchor_relative_state_paths(
        path_cfg, chroma, repo_root=str(REPO_ROOT))
    assert path_cfg.chromadb_path == chosen
    assert path_cfg.vault_data_dir == vault


def test_dev_mode_is_unchanged(monkeypatch):
    """Env unset: a checkout keeps its state beside the code, as before."""
    monkeypatch.delenv("PUFFPAI_DATA_DIR", raising=False)
    path_cfg, chroma = _pair()
    before = {k: getattr(path_cfg, k) for k in cfgmod.RELOCATABLE_PATH_KEYS}
    before_chroma = chroma.path
    cfgmod.anchor_relative_state_paths(
        path_cfg, chroma, repo_root=str(REPO_ROOT))
    assert {k: getattr(path_cfg, k) for k in cfgmod.RELOCATABLE_PATH_KEYS} == before
    assert chroma.path == before_chroma


def test_empty_values_stay_empty(monkeypatch, tmp_path):
    """Empty means "derive at runtime" — filling it in would defeat that."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    path_cfg, chroma = _pair()
    assert path_cfg.security_logs_dir == ""
    cfgmod.anchor_relative_state_paths(
        path_cfg, chroma, repo_root=str(REPO_ROOT))
    assert path_cfg.security_logs_dir == ""
    # state_dir is deliberately NOT relocated: callers pass env_subdir to
    # resolve_state_dir(), and an absolute state_dir would outrank it.
    assert path_cfg.state_dir == ""
    assert "state_dir" not in cfgmod.RELOCATABLE_PATH_KEYS


def test_readonly_knowledge_sources_are_not_relocated(monkeypatch, tmp_path):
    """zim_dirs point at a library, not at writable state."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path / "state"))
    path_cfg, chroma = _pair(zim_dirs=["some/relative/zim"])
    cfgmod.anchor_relative_state_paths(
        path_cfg, chroma, repo_root=str(REPO_ROOT))
    assert path_cfg.zim_dirs == ["some/relative/zim"]


def test_loader_applies_it_end_to_end(monkeypatch, tmp_path):
    """The real config build, against the real shipped file."""
    root = tmp_path / "state"
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(root))
    cfg = cfgmod._build_config(repo_root=str(REPO_ROOT))
    assert cfg.paths.vault_data_dir == str(root / "vault_data")
    assert cfg.paths.chromadb_path == str(root / "kiwix_rag_db")
    assert cfg.chromadb.path == str(root / "kiwix_rag_db")


def test_resolve_state_dir_honours_the_anchored_value(monkeypatch, tmp_path):
    """The downstream consumer sees an absolute, writable path.

    ``resolve_state_dir`` ranks an ABSOLUTE configured value above the env var,
    so anchoring has to produce one — a relative value would have fallen
    through to step 3 and landed in the read-only code tree.
    """
    root = tmp_path / "state"
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(root))
    path_cfg, chroma = _pair()
    cfgmod.anchor_relative_state_paths(
        path_cfg, chroma, repo_root=str(REPO_ROOT))
    resolved = cfgmod.resolve_state_dir(
        path_cfg.vault_data_dir,
        repo_root=str(REPO_ROOT),
        default=os.path.join(str(REPO_ROOT), "vault_data"),
    )
    assert resolved == str(root / "vault_data")
    assert str(REPO_ROOT) != os.path.commonpath([str(REPO_ROOT), resolved]) or \
        not resolved.startswith(str(REPO_ROOT))
