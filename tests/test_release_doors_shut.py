"""Release gate: the SHIPPED config must leave every egress door shut.

`config/puffpai_config.json` is what a stranger installs. Any door it leaves
open is a door the household never agreed to — the appliance's whole claim is
that nothing leaves the house until someone says so. The shipped file carried
`"web_search_consent": true`, which is the `web_search` door's
``legacy_config_key``, so a fresh box searched the internet on first boot.

This test reads the real file on disk (not a fixture) and resolves every door
against it with the environment and the owner overlay taken out of the way, so
the only thing left deciding is what we ship.
"""
import json
import os
from pathlib import Path

import pytest

from puffpai.security import egress_doors as ed

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_CONFIG = REPO_ROOT / "config" / "puffpai_config.json"


@pytest.fixture
def shipped_only(tmp_path, monkeypatch):
    """Resolve doors against the shipped config and nothing else.

    Three sources of "open" have to be removed or the test measures the
    developer's box instead of the release:

    1. ``PUFFPAI_EGRESS_*`` env vars — the highest-precedence tier.
    2. The owner overlay, which ``tests/conftest.py`` deliberately seeds with
       ``web_fetch``/``web_search`` open for the rest of the suite. Pointed at
       a path that does not exist, which :func:`_raw_overlay` reads as ``{}``.
    3. App- and skill-registered doors left behind by other test modules.
    """
    for key in [k for k in os.environ if k.startswith("PUFFPAI_EGRESS")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ed, "_config_path", lambda: str(SHIPPED_CONFIG))

    saved_apps = dict(ed._APP_DOORS)
    saved_skills = dict(ed._SKILL_DOORS)
    ed.clear_app_doors()
    ed.clear_skill_doors()
    ed.set_overlay_override(str(tmp_path / "no-overlay-here" / "doors.json"))
    ed.reset_cache()
    yield
    ed.set_overlay_override(None)
    ed._APP_DOORS.update(saved_apps)
    ed._SKILL_DOORS.update(saved_skills)
    ed.reset_cache()


def test_shipped_config_exists_and_parses():
    raw = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)


def test_no_shipped_door_flag_is_true():
    """The `egress_doors` section and every legacy key ship off or absent."""
    raw = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    section = raw.get(ed.CONFIG_SECTION) or {}
    open_flags = [k for k, v in section.items() if ed._as_bool(v) is True]
    assert open_flags == [], (
        f"config/puffpai_config.json ships {ed.CONFIG_SECTION} flags ON: "
        f"{open_flags}"
    )

    legacy_open = [
        d.legacy_config_key for d in ed.DOORS
        if d.legacy_config_key and ed._as_bool(raw.get(d.legacy_config_key)) is True
    ]
    assert legacy_open == [], (
        f"config/puffpai_config.json ships legacy consent keys ON: {legacy_open}. "
        f"web_search_consent is the one that shipped true."
    )


def test_web_search_consent_ships_false():
    """Named explicitly: this is the key that was wrong, so name it."""
    raw = json.loads(SHIPPED_CONFIG.read_text(encoding="utf-8"))
    assert raw.get("web_search_consent") is False


def test_every_door_resolves_shut_on_a_fresh_box(shipped_only):
    still_open = []
    for d in ed.all_doors():
        allowed, reason = ed.egress_allowed(d.id)
        if allowed:
            still_open.append((d.id, reason))
    assert still_open == [], (
        "these doors are OPEN on a freshly installed box: " + repr(still_open)
    )


def test_every_door_flag_resolves_to_no_consent(shipped_only):
    """`_resolve_flag` agrees: no tier says yes.

    Checked separately from :func:`egress_allowed` because the derived probe
    door reports its state from what the OTHER doors reach, so a regression
    that opened a flag could in principle hide behind it.
    """
    for d in ed.all_doors():
        decided, source, reason = ed._resolve_flag(d)
        assert decided is not True, (
            f"door {d.id!r} is switched ON by the {source} tier: {reason}"
        )
        assert d.default is False, f"door {d.id!r} has a default of True"
