""""What can leave your house" stays true.

The published list of what the appliance reaches must be generated from the
registry, not maintained by hand. A hand-maintained list is precisely what let
a founder-operated Azure endpoint ship un-enumerated while the README claimed
"100% offline" and ``/system/status`` reported ``chat_allow_egress:false``.

If this test fails, the fix is one command::

    python scripts/gen_egress_doc.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DOC = REPO_ROOT / "docs" / "WHAT_CAN_LEAVE_YOUR_HOUSE.md"
GENERATOR = REPO_ROOT / "scripts" / "gen_egress_doc.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_egress_doc", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_doc_exists():
    assert DOC.exists(), f"{DOC} is missing; run python scripts/gen_egress_doc.py"


def test_doc_matches_the_registry():
    rendered = _load_generator().render()
    assert DOC.read_text(encoding="utf-8") == rendered, (
        "docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md has drifted from the egress-door "
        "registry. Run: python scripts/gen_egress_doc.py")


def test_every_door_appears_in_the_doc():
    from puffpai.security.egress_doors import DOORS

    text = DOC.read_text(encoding="utf-8")
    for d in DOORS:
        assert f"`{d.id}`" in text, f"door {d.id} is not documented"
        for host in d.hosts:
            assert host in text, f"host {host} of door {d.id} is not documented"
        assert d.data_sent in text, f"door {d.id} does not say what leaves"


def test_the_doc_says_every_door_ships_off():
    """Every host-bearing door ships off, and the derived one ships contactless.

    ``connectivity_probe`` has no hosts of its own since the probe was pointed
    at the open doors instead of Cloudflare/Google, so "off" is the wrong word
    for it — with every other door shut it reaches nobody, and that is what the
    doc must say. The invariant is unchanged; only its wording splits in two.
    """
    text = DOC.read_text(encoding="utf-8")
    from puffpai.security.egress_doors import DOORS

    plain = [d for d in DOORS if not d.derived]
    derived = [d for d in DOORS if d.derived]
    assert text.count("**Ships:** off") == len(plain)
    assert text.count("**Ships:** contacts nothing until another door is open"
                      ) == len(derived)
    assert "**Every door ships off.**" in text


def test_every_declared_host_is_named_in_the_doc():
    """The stranger who greps the source for `http` must find it disclosed.

    Not a sample: EVERY host any door may dial is listed by name, so the doc
    cannot drift into being a flattering subset of the registry.
    """
    text = DOC.read_text(encoding="utf-8")
    from puffpai.security.egress_doors import DOORS

    missing = [(d.id, h) for d in DOORS for h in (d.hosts or ()) if h not in text]
    assert not missing, "hosts reachable but undisclosed: %r" % (missing,)


def test_every_third_party_door_names_its_party_in_the_doc():
    """A door onto someone else's service says whose, in the published doc."""
    text = DOC.read_text(encoding="utf-8")
    from puffpai.security.egress_doors import DOORS

    for d in DOORS:
        if d.third_party:
            assert d.third_party in text, d.id
            assert "third party" in text.lower()


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_doc_does_not_say_jellyfin_keeps_everything_inside():
    """Jellyfin can fetch internet channels itself, outside every switch here."""
    text = _flat(DOC.read_text(encoding="utf-8"))
    assert "Home Assistant, Jellyfin, Calibre" not in text
    assert "If a household adds an internet IPTV tuner to Jellyfin's Live TV" in text
    assert ("The `tv_free_channels` switch does not control Jellyfin-carried "
            "channels") in text


def test_the_free_channels_notes_say_the_kiosk_player_is_unchecked():
    """The relay checks every hop; the kiosk player's container does not."""
    from puffpai.security.egress_doors import door

    notes = door("tv_free_channels").notes
    for phrase in (
            "with no address check, no https check and no ledger line",
            "a playlist could point it at a device on your network",
            "The relay used for casting checks every hop; the kiosk player "
            "does not."):
        assert phrase in notes
        assert phrase in _flat(DOC.read_text(encoding="utf-8"))
