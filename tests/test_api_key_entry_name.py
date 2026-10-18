"""One build has one password-store entry name, however its folder is reached.

``api_key_store.entry_name`` took its label from
the name the state folder was reached by, not from the canonical folder. One
build started through a junction, a link, another letter case or its short
(8.3) name got a different entry name, found no key under it, and made a new
one -- so the web page, which had the old key, was locked out.

Now the label comes from the canonical folder, like the id. An entry made
before this, under an older label for the SAME build id, is found and moved
to the one name (written, read back, then the old one removed) instead of a
new key being made. Everything here uses a dict standing in for the password
store; nothing touches the real one.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from puffpai.security import api_key_store, build_identity, secrets_provider

#: These tests are about keyring behaviour: each gets an in-memory stand-in
#: (tests/conftest.py) and may put its own in place. Never the real store.
pytestmark = pytest.mark.keyring_stub


class _Ring:
    """A dict standing in for the OS password store."""

    def __init__(self, can_delete: bool = True):
        self.entries: dict[tuple[str, str], str] = {}
        self.can_delete = can_delete

    def get_password(self, service, name):
        return self.entries.get((service, name))

    def set_password(self, service, name, value):
        self.entries[(service, name)] = value

    def delete_password(self, service, name):
        if not self.can_delete:
            raise RuntimeError("this store cannot delete")
        self.entries.pop((service, name), None)


def _use(monkeypatch, ring):
    monkeypatch.setattr(secrets_provider, "keyring", ring, raising=False)
    monkeypatch.setattr(secrets_provider, "_HAS_KEYRING", True)
    monkeypatch.setattr(secrets_provider, "_default_provider", None)
    return ring


@pytest.fixture
def ring(monkeypatch):
    return _use(monkeypatch, _Ring())


def _key(monkeypatch, state):
    from puffpai import api_server
    monkeypatch.setenv("PUFFPAI_STATE_DIR", state)
    monkeypatch.delenv("PUFFPAI_API_KEY", raising=False)
    monkeypatch.setattr(api_server, "_LEGACY_API_KEY_PATH",
                        os.path.join(state, "absent", "api_key.txt"))
    return api_server._load_or_create_api_key()


def _junction(link, target) -> bool:
    """A junction (Windows) or a symlink (elsewhere) at *link* to *target*."""
    if sys.platform == "win32":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, check=False)
        return done.returncode == 0 and os.path.isdir(link)
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return True


@pytest.fixture
def two_spellings(tmp_path):
    real = tmp_path / "real" / "botA"
    real.mkdir(parents=True)
    link = tmp_path / "botA-link"
    if not _junction(link, real):
        pytest.skip("cannot make a junction or a link here")
    return str(real), str(link)


def test_the_label_comes_from_the_canonical_folder(two_spellings):
    real, link = two_spellings
    assert build_identity.build_label(link) == build_identity.build_label(real) == "botA"
    assert api_key_store.entry_name(link) == api_key_store.entry_name(real)
    assert build_identity.old_labels(link) == ["botA-link"]
    assert build_identity.old_labels(real) == []


@pytest.mark.skipif(sys.platform != "win32", reason="letter case is Windows' own")
def test_another_letter_case_is_the_same_entry(tmp_path):
    folder = tmp_path / "BotCase"
    folder.mkdir()
    assert (api_key_store.entry_name(str(folder).upper())
            == api_key_store.entry_name(str(folder)))


def test_the_short_name_is_the_same_entry(tmp_path):
    folder = tmp_path / "a rather long bot folder name"
    folder.mkdir()
    short = build_identity._short_name(str(folder))  # noqa: SLF001
    if not short or os.path.basename(short) == folder.name:
        pytest.skip("this volume makes no short (8.3) names")
    assert api_key_store.entry_name(short) == api_key_store.entry_name(str(folder))
    assert build_identity.build_label(short) == "a-rather-long-bot-folder"


def test_the_move_needs_a_password_store(monkeypatch, two_spellings):
    monkeypatch.setattr(secrets_provider, "_HAS_KEYRING", False)
    _real, link = two_spellings
    assert api_key_store.move_old_entry(link) == ""
