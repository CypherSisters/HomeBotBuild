"""Consent on an unattended run: what the wizard refuses to do when nobody is
there.

Two rules, and both of them are about a ``--yes`` run - the one where no card
was read, no size was shown and nobody said yes to anything:

* **Naming a source is not accepting its size.** ``--fetch-source <id>``
  without ``--accept-source-size`` downloads NOTHING. The consent is the second
  flag, and :func:`puffpai.setup_ops.fetch_source` is stubbed with a spy here
  so the assertion is "it was never called" rather than "no file appeared",
  which a refused connection would also satisfy.
* **Silence selects nothing.** A ``--yes`` run with no ``--fetch-source``
  records ``selected_by: "non_interactive"``, marks the Integrate card
  ``skipped: "non_interactive"``, and prints the ``no_consent`` field saying so
  - because a build record that just showed an empty list would read as "there
  was nothing to offer" instead of "nobody was asked".

Both runs are driven through the verb (:func:`puffpai.setup_cli.cmd_setup`,
which is the card wizard) with the real argument parser, so the flags under
test are the flags the CLI actually defines. They are postured ONLINE on
purpose - an offline build skips the catalog with ``skipped: "offline_build"``
and the consent gate would never be reached - and the brain is pointed at an
address where nothing answers. Nothing here touches the network: the only
function that would (``fetch_source``) is the spy, and ``source_preview``
documents that it makes no call at all.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from tests import _wizard_driver as driver


DEAD_URL = "http://127.0.0.1:11499"

#: A vetted row in this build's own catalog. Named, never fetched.
SOURCE_ID = "kiwix_astronomy_stackexchange"


def _run(root, monkeypatch, *flags):
    """One ``--yes`` run of the verb, with every download spied on."""
    import puffpai.config as config
    from puffpai import cli, setup_cli, setup_ops

    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(root / "state"))
    monkeypatch.setenv("PUFFPAI_CONFIG_DIR", str(root / "config"))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND",
                       "keyring.backends.null.Keyring")
    config.get_config(_force_reload=True)

    calls: list[tuple] = []

    def _spy(source_id, posture, **kwargs):
        calls.append((source_id, kwargs))
        return {"ok": False, "id": source_id, "error": "the spy fetches nothing"}
    monkeypatch.setattr(setup_ops, "fetch_source", _spy)

    parser = cli.build_parser()
    args = parser.parse_args([
        "setup", "--yes", "--stated-ram", "16",
        "--build-network", "online", "--ollama-url", DEAD_URL,
        "--skip-ha", *flags,
    ])
    screen = driver.Screen()
    with contextlib.redirect_stdout(screen):
        code = setup_cli.cmd_setup(args)
    manifest = json.loads(
        (root / "state" / "config" / "build_manifest.json")
        .read_text(encoding="utf-8"))
    config.get_config(_force_reload=True)
    return code, screen.text, manifest, calls


@pytest.fixture
def root(tmp_path):
    return driver.install_root(tmp_path, "consent")


def test_naming_a_source_without_accepting_its_size_downloads_nothing(
        root, monkeypatch):
    code, out, manifest, calls = _run(root, monkeypatch,
                                      "--fetch-source", SOURCE_ID)
    assert code == 0, out[-3000:]
    assert calls == [], f"a download ran without the size being accepted: {calls}"

    sources = manifest["knowledge"]["sources"]
    assert sources["fetched"] == []
    # It was named, so it is still on the record - as something not fetched,
    # which is a gap the owner can act on, not a silence.
    assert SOURCE_ID in sources["not_fetched"]


def test_the_same_run_with_the_size_accepted_is_the_one_that_may_download(
        root, monkeypatch):
    """The other half of the gate: with both flags the fetch is attempted.

    Without this, the test above would pass just as well on a build that never
    downloads anything at all, and the gate would be proving nothing.
    """
    code, out, _manifest, calls = _run(root, monkeypatch,
                                       "--fetch-source", SOURCE_ID,
                                       "--accept-source-size")
    assert code == 0, out[-3000:]
    assert [source_id for source_id, _kw in calls] == [SOURCE_ID]
    assert calls[0][1].get("size_acknowledged") is True


def test_a_yes_run_that_names_nothing_selects_nothing_and_says_why(
        root, monkeypatch):
    code, out, manifest, calls = _run(root, monkeypatch)
    assert code == 0, out[-3000:]
    assert calls == []

    sources = manifest["knowledge"]["sources"]
    assert sources["fetched"] == []
    assert sources["skipped"] == "non_interactive"

    # The card said so on the screen, in the words a person can act on.
    assert "Fetching none of them" in out
    assert "consent nobody gave is not consent" in out


def test_the_sources_card_records_who_selected_on_an_unattended_run(
        root, monkeypatch):
    """``selected_by`` is the provenance of the choice, not a list of ids.

    It lives in the run record (``wizard_run.json``), which is what a resumed
    run reads back, so that is where it is asserted.
    """
    _code, _out, _manifest, _calls = _run(root, monkeypatch)

    state = json.loads((root / "config" / "wizard_run.json")
                       .read_text(encoding="utf-8"))
    sections = state.get("sections") or {}
    selected_by = [section.get("selected_by")
                   for section in sections.values()
                   if isinstance(section, dict) and "selected_by" in section]
    assert selected_by == ["non_interactive"], sections
