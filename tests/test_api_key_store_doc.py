"""api_key_store says truthfully where the vault is.

Its note said the vault "lives in the build's own state folder". It does not:
the vault is in the DATA folder (``paths.vault_data_dir``), its API key entry
has one name for every build, and the server never hands the vault to the
secrets provider when it starts (only the ``puffpai vault`` command does).
"""
from __future__ import annotations

from pathlib import Path

from puffpai.security import api_key_store

SRC = Path(__file__).resolve().parents[1] / "src" / "puffpai"


def test_the_note_does_not_claim_the_vault_is_in_the_state_folder():
    doc = " ".join(api_key_store.__doc__.split())
    assert "lives in the build's own state folder" not in doc
    assert "does NOT live in the build's state folder" in doc
    assert "vault_data_dir" in doc

