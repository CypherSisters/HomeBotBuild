"""Uploads: the searchable copy never keeps a detail whose original was vaulted."""

from __future__ import annotations

import pytest

from puffpai import ingestion
from puffpai.ingestion import screen_text_for_pii
from puffpai.security.vault import Vault
from puffpai import plain_words  # noqa: E402  (the one banned list)

PW = "correct horse battery"
#: On top of plain_words' one list: privacy words a person misreads.
JARGON_EXTRA = ("pii", "redact", "re-upload")


@pytest.fixture()
def open_vault(tmp_path):
    v = Vault(vault_dir=str(tmp_path / "vault"))
    v.initialize(PW)
    v.unlock(PW)
    return v


@pytest.fixture()
def locked_vault(tmp_path):
    v = Vault(vault_dir=str(tmp_path / "vault"))
    v.initialize(PW)
    v.unlock(PW)
    v.lock()
    return v


def test_about_me_file_keeps_address_and_school_out(open_vault):
    text = ("About me. My name is Leo Marlow. I live at 42 Maple Street. "
            "I go to Lincoln Elementary. My birthday is March 3 2016.")
    res = screen_text_for_pii(text, "about.txt", vault=open_vault)
    for value in ("Leo Marlow", "42 Maple Street", "Lincoln Elementary",
                  "March 3 2016"):
        assert value not in res.text_for_index


def test_full_name_only_is_indexed_as_is(locked_vault):
    text = "Pattern by name: Jane Doe. Cast on 120 stitches."
    res = screen_text_for_pii(text, "hat.txt", vault=locked_vault)
    assert res.pii_detected and not res.pii_vaulted
    assert res.text_for_index == text


def test_clean_document_passes_through(locked_vault):
    text = "Stegosaurus had about 17 plates on its back."
    res = screen_text_for_pii(text, "dino.txt", vault=locked_vault)
    assert not res.pii_detected and res.text_for_index == text
