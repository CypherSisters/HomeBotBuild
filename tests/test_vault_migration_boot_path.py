"""The vault token boot path, end to end, and the one-source grep gate.

Two things are proved here.

**The 03:00 reboot.** A cold start with no human present, a service wrap
enrolled and a machine key installed, obtains the Home Assistant token.
"Unlock/recovery cannot lock the user out" is a hard requirement for a family
appliance: a design that leaves the lights dead until someone types a PIN is
not shippable.

**Strict mode.** When the owner opts into PIN-only credentials, the same cold
start reports ``vault_locked`` -- honestly, with no crash, no plaintext, and no
``Authorization: Bearer MIGRATED_TO_VAULT`` -- and recovers the moment the
vault is unlocked, with no restart.

Plus the gate that keeps it that way: nothing in the tree may read the token
field of ``home_assistant_config.json`` except the one source module.

Fake token material only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from puffpai.security import ha_token_source as src  # noqa: E402
from puffpai.security import ha_vault  # noqa: E402
from puffpai.security import vault as vault_mod  # noqa: E402
from puffpai.security.vault import Vault  # noqa: E402

FAKE_TOKEN = "eyJ0ZXN0IjoiZmFrZXBlcnNvbiJ9.not-a-real-token.Test-Fakeperson"
FAKE_MACHINE_KEY = "b" * 64
PIN = "vault-pin-1234"


@pytest.fixture
def appliance(tmp_path, monkeypatch):
    """A migrated appliance: token in the vault's boot-secret tier, service
    wrap enrolled, plaintext config scrubbed to the marker."""
    key = tmp_path / "vault_service.key"
    key.write_text(FAKE_MACHINE_KEY, encoding="utf-8")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv(vault_mod._SERVICE_KEY_ENV, str(key))
    vault_mod._service_key_warned.clear()

    cfg = tmp_path / "home_assistant_config.json"
    cfg.write_text(json.dumps({
        "url": "http://homeassistant.local:8123",
        "token": ha_vault.MIGRATED_MARKER,
        "enabled": True,
    }), encoding="utf-8")
    monkeypatch.setattr(ha_vault, "_HA_CONFIG_FILE", cfg)
    monkeypatch.setattr(ha_vault, "_FALLBACK_WARNED", False)

    vault_dir = tmp_path / "vault"
    setup = Vault(vault_dir=str(vault_dir))
    setup.initialize(PIN)
    sess = setup.authenticate(PIN)
    assert setup.enroll_service_wrap(sess.session_id) is True
    setup.write_service_secret("ha_token.json", {"token": FAKE_TOKEN},
                               sess.session_id)

    src.invalidate_cache()
    monkeypatch.setattr(src, "_from_env_or_keyring", lambda: None)
    return {"vault_dir": vault_dir, "config": cfg, "key": key}


def _cold_boot(appliance, monkeypatch):
    """A Vault exactly as puffpai-api has it at boot: no PIN, no session."""
    cold = Vault(vault_dir=str(appliance["vault_dir"]))
    monkeypatch.setattr(ha_vault, "_get_vault", lambda: cold)
    src.invalidate_cache()
    return cold


# ── the 03:00 reboot ────────────────────────────────────────────────────────

def test_cold_start_with_no_human_obtains_the_token(appliance, monkeypatch):
    monkeypatch.setattr(src, "strict_mode", lambda: False)
    cold = _cold_boot(appliance, monkeypatch)
    assert cold.has_live_session is False

    result = src.resolve_ha_token()
    assert result.token == FAKE_TOKEN
    assert result.source == src.SOURCE_VAULT_SERVICE
    assert result.locked is False


def test_cold_start_never_touches_the_plaintext_config(appliance, monkeypatch):
    """It cannot: the config holds the marker, and the marker is not a token."""
    monkeypatch.setattr(src, "strict_mode", lambda: False)
    _cold_boot(appliance, monkeypatch)
    assert src.ha_token() == FAKE_TOKEN
    on_disk = json.loads(appliance["config"].read_text(encoding="utf-8"))
    assert on_disk["token"] == ha_vault.MIGRATED_MARKER


def test_a_missing_machine_key_does_not_crash_the_boot(appliance, monkeypatch):
    monkeypatch.setattr(src, "strict_mode", lambda: False)
    appliance["key"].unlink()
    vault_mod._service_key_warned.clear()
    _cold_boot(appliance, monkeypatch)

    result = src.resolve_ha_token()
    assert result.token is None
    assert result.source is None
    status = src.ha_token_status()
    assert status["machine_key_present"] is False
    assert status["service_wrap_present"] is True   # the wrap is still enrolled


# ── strict mode: honest, fail-closed, and recoverable ───────────────────────

def test_strict_mode_reports_vault_locked_rather_than_guessing(appliance,
                                                               monkeypatch):
    monkeypatch.setattr(src, "strict_mode", lambda: True)
    _cold_boot(appliance, monkeypatch)

    result = src.resolve_ha_token()
    assert result.token is None
    assert result.locked is True
    assert "PIN" in result.detail

    status = src.ha_token_status()
    assert status["status"] == "vault_locked"
    assert status["strict_mode"] is True
    assert status["token_source"] is None


def test_strict_mode_never_falls_back_to_plaintext(tmp_path, appliance,
                                                   monkeypatch):
    """Even with a real token still sitting in the config and the fallback
    switch ON, strict mode must not read it."""
    appliance["config"].write_text(json.dumps({
        "url": "http://homeassistant.local:8123",
        "token": FAKE_TOKEN,
        "enabled": True,
    }), encoding="utf-8")
    monkeypatch.delenv(ha_vault._HA_FALLBACK_ENV, raising=False)
    assert ha_vault._plaintext_ha_fallback_allowed() is True

    monkeypatch.setattr(src, "strict_mode", lambda: True)
    _cold_boot(appliance, monkeypatch)
    assert src.ha_token() is None


def test_strict_mode_recovers_after_unlock_with_no_restart(appliance,
                                                           monkeypatch):
    """The recovery half of strict mode: down until unlocked, up immediately after.
    A cached miss would have made this take up to five minutes."""
    monkeypatch.setattr(src, "strict_mode", lambda: True)
    cold = _cold_boot(appliance, monkeypatch)
    assert src.ha_token() is None

    cold.authenticate(PIN)          # the owner types their PIN
    result = src.resolve_ha_token()  # no invalidate_cache, no restart
    assert result.token == FAKE_TOKEN
    assert result.source == src.SOURCE_VAULT_SESSION
    assert src.ha_token_status()["status"] == "ready"


def test_a_miss_is_never_cached(appliance, monkeypatch):
    monkeypatch.setattr(src, "strict_mode", lambda: True)
    _cold_boot(appliance, monkeypatch)
    src.resolve_ha_token()
    assert src._cached is None


# ── the marker is never a credential, on any path ───────────────────────────

def test_no_path_ever_returns_the_migration_marker(appliance, monkeypatch):
    for strict in (True, False):
        monkeypatch.setattr(src, "strict_mode", lambda strict=strict: strict)
        appliance["key"].unlink(missing_ok=True)
        vault_mod._service_key_warned.clear()
        _cold_boot(appliance, monkeypatch)
        assert src.ha_token() != ha_vault.MIGRATED_MARKER
        assert src.ha_token() != "YOUR_LONG_LIVED_ACCESS_TOKEN_HERE"


# ── the status payload is content-free ──────────────────────────────────────

def test_status_never_carries_the_token(appliance, monkeypatch):
    monkeypatch.setattr(src, "strict_mode", lambda: False)
    _cold_boot(appliance, monkeypatch)
    blob = json.dumps(src.ha_token_status())
    assert FAKE_TOKEN not in blob
    assert FAKE_MACHINE_KEY not in blob
    # Not even a length: that is a fingerprint of the credential type.
    assert str(len(FAKE_TOKEN)) not in blob


# ── HABridge fails closed, then recovers ────────────────────────────────────

def test_the_bridge_refuses_before_opening_a_socket_when_locked(appliance,
                                                                monkeypatch):
    from puffpai import ha_bridge

    monkeypatch.setattr(src, "strict_mode", lambda: True)
    _cold_boot(appliance, monkeypatch)

    bridge = ha_bridge.HABridge(url="http://ha.invalid:8123")
    bridge._enabled = True

    def _never(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("a request was made with no credential")

    monkeypatch.setattr(bridge._session, "get", _never)

    with pytest.raises(ha_bridge.HABridgeError) as exc:
        bridge._request("states")
    assert "locked in the vault" in str(exc.value)
    assert FAKE_TOKEN not in str(exc.value)


def test_the_bridge_picks_the_token_up_after_an_unlock(appliance, monkeypatch):
    from puffpai import ha_bridge

    monkeypatch.setattr(src, "strict_mode", lambda: True)
    cold = _cold_boot(appliance, monkeypatch)

    bridge = ha_bridge.HABridge(url="http://ha.invalid:8123")
    bridge._enabled = True
    assert not bridge._token

    cold.authenticate(PIN)
    bridge._ensure_token()
    assert bridge._token == FAKE_TOKEN
    assert bridge._session.headers["Authorization"] == "Bearer " + FAKE_TOKEN


def test_the_bridge_takes_an_explicit_token_unchanged(appliance, monkeypatch):
    """Tests and the setup wizard pass one; the source must not override it."""
    from puffpai import ha_bridge

    monkeypatch.setattr(src, "strict_mode", lambda: True)
    _cold_boot(appliance, monkeypatch)
    bridge = ha_bridge.HABridge(url="http://ha.invalid:8123", token="explicit")
    assert bridge._token == "explicit"


# ── one source: the grep gate ───────────────────────────────────────────────

SRC = REPO / "src" / "puffpai"

#: Only these may name the plaintext HA config file at all.
_CONFIG_FILE_ALLOWED = {
    Path("security/ha_vault.py"),   # THE source module: owns the file
    Path("config.py"),              # loads url/enabled, and pops the token
    Path("security/zones.py"),      # a filename in a zone classification list
    Path("security/ha_token_source.py"),  # prose only
}

#: Only config.py may reach the config object's HA token, and only to blank it.
#: ha_token_source.py is NOT here: its mentions are prose, and an AST check
#: does not see prose.
_HA_TOKEN_ATTR_ALLOWED = {
    Path("config.py"),
}


def _code_without_comments(path: Path) -> str:
    """Source with ``#`` comments stripped, string literals KEPT.

    Comments are where this change explains itself, so a gate that trips on
    them is noise. String literals stay, because an actual read of the
    plaintext config is a string literal -- stripping those would gut the gate.
    """
    import io
    import tokenize
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text  # unparseable: check it verbatim rather than skip it
    return "".join(t.string for t in toks if t.type != tokenize.COMMENT)


def _py_files():
    for path in SRC.rglob("*.py"):
        yield path, path.relative_to(SRC)


def test_only_the_source_module_reads_the_plaintext_ha_config():
    offenders = []
    for path, rel in _py_files():
        if rel in _CONFIG_FILE_ALLOWED:
            continue
        if "home_assistant_config" in _code_without_comments(path):
            offenders.append(str(rel))
    assert offenders == [], (
        "these modules reach for the plaintext HA config directly; route them "
        "through security/ha_token_source.ha_token(): " + ", ".join(offenders))


def test_config_py_drops_the_token_before_it_reaches_the_config_object():
    text = (SRC / "config.py").read_text(encoding="utf-8")
    assert 'ha_d.pop("token", None)' in text
    assert 'cfg.ha.token = ""' in text


def _config_ha_token_reads(path: Path) -> list:
    """Lines where this module reaches the config object's HA token.

    AST, not a regex. A gate that matches the literal dotted form only is
    walked straight past by code that binds the section first --

        ha_cfg = get_config.ha
        ... ha_cfg.token...

    -- which is how a reader in ``api_server.py`` (``firewall_devices``,
    ``_sentinel_privacy_audit``) would survive unnoticed, and silently become
    a dead branch once ``cfg.ha.token`` is blanked.

    So: bind-then-read is resolved. Any name assigned from an expression
    containing a ``.ha`` attribute is an alias, and ``<alias>.token`` counts,
    as does ``<anything>.ha.token`` directly.
    """
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:  # pragma: no cover
        return []

    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue
        if not any(isinstance(n, ast.Attribute) and n.attr == "ha"
                   for n in ast.walk(value)):
            continue
        for target in targets:
            for leaf in ast.walk(target):
                if isinstance(leaf, ast.Name):
                    aliases.add(leaf.id)

    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "token"):
            continue
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "ha":
            hits.append(node.lineno)
        elif isinstance(value, ast.Name) and value.id in aliases:
            hits.append(node.lineno)
    return sorted(set(hits))


def test_nothing_reads_cfg_ha_token():
    """A stale boot-time snapshot of the token is exactly what this gate forbids."""
    offenders = []
    for path, rel in _py_files():
        if rel in _HA_TOKEN_ATTR_ALLOWED:
            continue
        for line in _config_ha_token_reads(path):
            offenders.append(f"{rel}:{line}")
    assert offenders == [], (
        "these modules read the config object's HA token instead of the one "
        "source (security/ha_token_source.ha_token()): " + ", ".join(offenders))


def test_the_gate_actually_catches_the_alias_form():
    """Guard the guard. A dotted-form regex returns a clean sheet on exactly
    this code, although it reads the token."""
    src_text = (
        "from puffpai.config import get_config\n"
        "def f():\n"
        "    ha_cfg = get_config().ha\n"
        "    if ha_cfg.token:\n"
        "        return ha_cfg.token\n"
        "def g():\n"
        "    return get_config().ha.token\n"
        "def h():\n"
        "    cfg_ha = _cfg.ha\n"
        "    return cfg_ha.token\n"
        "def ok():\n"
        "    r = resolve_ha_token()\n"
        "    return r.token\n"          # NOT an offender: not from .ha
    )
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.py"
        probe.write_text(src_text, encoding="utf-8")
        hits = _config_ha_token_reads(probe)
    assert hits == [4, 5, 7, 10], hits


def test_every_consumer_goes_through_the_one_function():
    for rel in ("ha_bridge.py", "ha_events.py", "zigbee_manager.py",
                "api_server.py"):
        text = (SRC / rel).read_text(encoding="utf-8", errors="replace")
        assert "ha_token_source" in text, rel


def test_the_token_is_in_the_vaults_service_allowlist():
    assert src.VAULT_FILENAME in vault_mod.SERVICE_SECRET_FILES
    assert src.VAULT_FILENAME in vault_mod._MIGRATION_SKIP
