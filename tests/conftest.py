"""
Shared pytest fixtures for PuffPAI tests.
Mocks all external dependencies (Ollama, Kiwix, Node-RED, Home Assistant).
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import types

import pytest

# Seed a synthetic API key BEFORE any test module (or this file's own later
# imports) can import puffpai.api_server, which resolves the key at import
# time via _load_or_create_api_key(). On a CI runner with no vault/env
# file/keyring entry anywhere, that import raises RuntimeError during
# collection -- before the _secret_hygiene fixture below ever gets a chance
# to run (fixtures don't execute during collection). setdefault so a
# genuinely-configured environment (e.g. a developer's real key) still wins.
os.environ.setdefault("PUFFPAI_API_KEY", "0123456789abcdef" * 4)

# ── Test isolation from the developer's real machine ───────────────────────
# Importing puffpai.api_server resolves the app API key at import time. Left
# alone that reads, and on a fresh machine WRITES, the developer's real OS
# keyring, and would drop a generated key in their real per-user state
# directory. Neither belongs in a test run, so before any test module is
# imported we:
#   1. pin PUFFPAI_STATE_DIR at a throwaway directory,
#   2. seed a test-only PUFFPAI_API_KEY so resolution never needs to mint one,
#   3. install an inert `keyring` stub whose set_password refuses, so nothing
#      under test can reach the OS credential store.
# Each is set only if the environment has not already chosen a value.
os.environ.setdefault(
    "PUFFPAI_STATE_DIR", tempfile.mkdtemp(prefix="puffpai_test_state_"))
# A bot with every first app on (see turn_on_first_apps below), from the very
# first import: the router reads the apps' words once, at import time. The
# five household apps ship on in this build, so this is the shipped state.
os.makedirs(os.environ["PUFFPAI_STATE_DIR"], exist_ok=True)
if not os.path.exists(os.path.join(os.environ["PUFFPAI_STATE_DIR"], "apps_on.json")):
    with open(os.path.join(os.environ["PUFFPAI_STATE_DIR"], "apps_on.json"), "w",
              encoding="utf-8") as _fh:
        _fh.write('{"on": ["alarmflow", "calendar", "fitfam", "freshtable", "trivia"]}')
# The pairing code is per build (<state>/pairing/pairing.json), and a
# test must never find, claim or rotate the checkout's own
# dashboard/data/pairing.json either. Tests about the per-build rules clear
# this themselves (tests/test_per_build_secrets.py).
os.environ.setdefault(
    "PUFF_PAIRING_STATE",
    os.path.join(os.environ["PUFFPAI_STATE_DIR"], "pairing", "pairing.json"))
# The setup wizard checks whether the internet answers (one TCP connect to the
# model registry). A test never does: the check is recorded as "not checked",
# which also means nothing a test runs is ever downloaded by default. A test
# that is about the check replaces wizard.netcheck.internet_reachable itself.
os.environ.setdefault("PUFFPAI_WIZARD_NET_PROBE", "off")
# "Looks good" now builds what it approved (the factory step): a live card is
# made from one sample look at the service. A test never makes that look; a
# test about the look switches it back on itself.
os.environ.setdefault("PUFFPAI_BUILD_PROBES", "off")
# The owner's chat goes through the tool loop
# (puffpai.brain_loop). The many older chat tests are about the shortcut chain
# and the no-tools model call, which are PARKED (kept for voice, keyless
# callers and a model that cannot call tools), so they run with the loop off.
# The loop's own tests switch it on (tests/test_brain_loop_chat.py).
os.environ.setdefault("PUFFPAI_TOOL_LOOP", "off")
# A thing's page recipe asks the brain once, in the background, for friendlier
# words (puffpai.things). A test never reaches a real model that way; the
# things tests switch it on with a fake brain.
os.environ.setdefault("PUFFPAI_THINGS_BRAIN", "off")
# The find step also asks the free libraries for books (puffpai.finding.books).
# The older find tests are about the shelves and code sites, so books
# are off; the books tests switch them on with recorded library answers.
os.environ.setdefault("PUFFPAI_FIND_BOOKS", "off")

# Add src to path so puffpai package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
# The shared helpers beside the tests (keys_support, finding_fakes, ...) import
# by their bare names.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

#: Egress doors the suite grants appliance-level consent to, and NOTHING more.
#: See ``_isolate_egress_overlay`` for why, and
#: ``tests/test_toolsmith_web_tools_egress.py`` for the test that pins this to
#: exactly these two so it cannot grow into a blanket "egress on in tests".
TEST_OPEN_DOORS = {"web_fetch": True, "web_search": True}

# ── No test ever reaches the real OS password store ───────────────────────
# A test run must not leave Credential Manager entries named
# PUFFPAI_API_KEY.state.<id> ("state" is the pinned test state folder's name),
# whether or not something imported the real ``keyring`` first, and in child
# processes a test starts too. So:
#
# 1. every process this run starts, this one included, is told to use
#    keyring's "fail" backend (PYTHON_KEYRING_BACKEND), so even the real
#    ``keyring`` package cannot reach the OS store from a test;
# 2. the Windows calls that write or delete a stored credential are replaced
#    by traps that record the attempt (the entry's name only);
# 3. every test gets a fresh stand-in ``keyring`` module (the
#    ``_keyring_guard`` fixture below): reads find nothing and writes raise.
#    A test about keyring behaviour opts in with
#    ``@pytest.mark.keyring_stub`` and gets an in-memory one that keeps what
#    is written (the ``keyring_entries`` fixture);
# 4. a test that reached the real store FAILS, naming itself. A test may
#    still put its own keyring stand-in in place (several older tests
#    stub ``sys.modules["keyring"]`` to drive one branch); the marker is
#    for tests that want this file's in-memory one.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

#: What reached the real OS password store during the current test.
REAL_KEYRING_TOUCHES: list = []

_STUB_MARK = "__puffpai_test_keyring__"


def _keyring_stub(entries=None):
    """A stand-in ``keyring`` module. *entries* (a dict) makes it keep writes;
    without it, reads find nothing and writes raise."""
    stub = types.ModuleType("keyring")
    setattr(stub, _STUB_MARK, True)

    def _get_password(service, name):  # noqa: D103 - stub
        return None if entries is None else entries.get((service, name))

    def _set_password(service, name, value):  # noqa: D103 - stub
        if entries is None:
            raise RuntimeError(
                "blocked by tests/conftest.py: tests must not write to "
                "the OS keyring "
                f"(service={service!r}, entry={name!r}); a test about keyring "
                "behaviour is marked @pytest.mark.keyring_stub")
        entries[(service, name)] = value

    def _delete_password(service, name):  # noqa: D103 - stub
        if entries is None:
            raise RuntimeError(
                "blocked by tests/conftest.py: tests must not delete from "
                "the OS keyring; a test about "
                "keyring behaviour is marked @pytest.mark.keyring_stub")
        entries.pop((service, name), None)

    stub.get_password = _get_password           # type: ignore[attr-defined]
    stub.set_password = _set_password           # type: ignore[attr-defined]
    stub.delete_password = _delete_password     # type: ignore[attr-defined]
    stub.errors = types.SimpleNamespace(        # type: ignore[attr-defined]
        KeyringError=RuntimeError, PasswordDeleteError=RuntimeError)
    return stub


def _real_store_trap(what: str):
    def _trap(*args, **kwargs):
        target = kwargs.get("TargetName")
        if target is None and args and isinstance(args[0], dict):
            target = args[0].get("TargetName")
        REAL_KEYRING_TOUCHES.append(f"{what} ({target or 'unnamed entry'})")
        raise RuntimeError(f"tests must not {what} the real OS password store")
    return _trap


def _trap_real_password_store() -> None:
    import importlib
    for name in ("win32ctypes.pywin32.win32cred", "win32cred"):
        try:
            module = importlib.import_module(name)
        except Exception:  # not installed on this computer
            continue
        for call, what in (("CredWrite", "write to"), ("CredDelete", "delete from")):
            if hasattr(module, call):
                setattr(module, call, _real_store_trap(f"{what}, {name}.{call},"))
    real = sys.modules.get("keyring")
    if real is not None and not getattr(real, _STUB_MARK, False):
        # Something imported the real package before this file ran.
        for module in (real, sys.modules.get("keyring.core")):
            if module is None:
                continue
            module.set_password = _real_store_trap("write to")
            module.delete_password = _real_store_trap("delete from")


_trap_real_password_store()
sys.modules["keyring"] = _keyring_stub()

try:
    from puffpai import config as _puffpai_config  # noqa: E402
except Exception:  # a tree that does not import yet still collects
    _puffpai_config = None

#: The real resolver, captured before anything replaces it, for the one test
#: that is allowed to ask what the per-user directory would be. ``None`` when
#: this tree's config has no such resolver.
_REAL_USER_STATE_ROOT = getattr(_puffpai_config, "_user_state_root", None)


@pytest.fixture(autouse=True)
def _isolate_puffpai_logging():
    """Snapshot/restore the global 'puffpai' logger around every test.

    setup_logging() mutates the shared 'puffpai' logger (level, handlers) and
    the module-level _configured flag; without restoration, tests that call it
    (test_logging_config.py, test_integration.py) leak that state into later
    caplog-based tests and make the suite order-dependent.
    """
    import puffpai.logging_config as lc
    logger = logging.getLogger("puffpai")
    saved_handlers = logger.handlers[:]
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_disabled = logger.disabled
    saved_configured = lc._configured
    yield
    for handler in logger.handlers:
        if handler not in saved_handlers:
            handler.close()
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)
    logger.propagate = saved_propagate
    logger.disabled = saved_disabled
    lc._configured = saved_configured


@pytest.fixture
def temp_dir(tmp_path):
    """Temporary directory for test files."""
    return tmp_path


@pytest.fixture
def mock_config(tmp_path):
    """Create a temporary puffpai_config.json with test values."""
    config = {
        "ollama": {
            "url": "http://localhost:11435",
            "model": "test-model",
            "temperature": 0.7,
        },
        "nodered": {
            "url": "http://localhost:1880",
            "timeout": 5,
            "webhooks": {
                "block_device": "/webhook/block-device",
                "network_scan": "/webhook/network-scan",
            },
        },
        "chromadb": {"path": str(tmp_path / "test_chroma")},
        "api_server": {"host": "127.0.0.1", "port": 8000},
        "nabu": {
            "enabled": False,
            "wake_word": "hey puffpai",
            "voice_max_length": 500,
        },
        "identity": {
            "name": "TestBot",
            "avatar_style": "thumbs",
            "avatar_seed": "test",
        },
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "puffpai_config.json"
    config_file.write_text(json.dumps(config))
    return config_file


@pytest.fixture
def mock_ha_config(tmp_path):
    """Mock Home Assistant config."""
    config = {
        "url": "http://localhost:8123",
        "token": "test-token-12345",
        "enabled": True,
    }
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "home_assistant_config.json"
    config_file.write_text(json.dumps(config))
    return config_file


@pytest.fixture
def mock_blocked_devices(tmp_path):
    """Create a temporary blocked_devices.json."""
    blocked_file = tmp_path / "blocked_devices.json"
    blocked_file.write_text(json.dumps([]))
    return blocked_file


# Split literal: a single "...api-key...=<24+ chars>" line trips the
# pre-commit secret scan's shape check. The runtime value is unchanged.
_TEST_API_KEY = "puffpai-test-" + "api-key-not-a-real-secret"


@pytest.fixture(scope="session", autouse=True)
def _secret_hygiene():
    """Session-wide: no test may read or write the real OS keychain.

    1. secrets_provider._HAS_KEYRING = False — the keyring tier (Windows
       Credential Manager service "puffpai", which holds a REAL HA_TOKEN)
       is off for the whole suite.
    2. PUFFPAI_API_KEY is set to a synthetic value in the env so
       api_server's import-time _load_or_create_api_key() resolves via the
       env tier instead of falling through to its generate path — which
       would OVERWRITE the machine's real Credential Manager entry via
       keyring.set_password (api_server.py:341-350).
    3. HA_TOKEN / HA_ACCESS_TOKEN are removed from the env so the env tier
       cannot resolve a real HA credential either.
    4. keyring.set_password / delete_password are replaced with a raiser —
       no code path reached from a test may mutate Credential Manager.

    Session-scoped so it precedes every module/class fixture that imports
    or reloads api_server. Per-test opt-in for keychain READS is
    @pytest.mark.real_keyring (see _real_keyring_optin); writes stay
    blocked unconditionally.
    """
    mp = pytest.MonkeyPatch()
    import puffpai.security.secrets_provider as sp_mod
    mp.setattr(sp_mod, "_HAS_KEYRING", False)
    mp.setenv("PUFFPAI_API_KEY", _TEST_API_KEY)
    mp.delenv("HA_TOKEN", raising=False)
    mp.delenv("HA_ACCESS_TOKEN", raising=False)
    try:
        import keyring

        def _blocked(*a, **k):
            raise RuntimeError(
                "blocked by tests/conftest.py: tests must never write the "
                "OS keychain (real Credential Manager entries live here)"
            )

        mp.setattr(keyring, "set_password", _blocked)
        mp.setattr(keyring, "delete_password", _blocked)
    except ImportError:
        pass
    yield
    mp.undo()


# The app key puffpai.api_server held when the run first saw it.
_PINNED_API_KEY = {}


@pytest.fixture(scope="module", autouse=True)
def _api_server_key_stays():
    """Every test module sees the app key api_server resolved at collection.

    Test modules build their auth header once, at import, from
    ``api_server._API_KEY``. That value comes from the environment as it was
    during collection, before ``_secret_hygiene`` swaps in ``_TEST_API_KEY``.
    A test that reloads ``puffpai.api_server`` (to pin how the key resolves)
    re-reads the key from the swapped environment, so every later module's
    header was stale and its routes answered 401. After each module the key
    and its source go back to the first values seen.
    """
    mod = sys.modules.get("puffpai.api_server")
    if mod is not None and not _PINNED_API_KEY:
        _PINNED_API_KEY.update(key=mod._API_KEY,
                               source=getattr(mod, "_API_KEY_SOURCE", None))
    yield
    mod = sys.modules.get("puffpai.api_server")
    if mod is None:
        return
    if not _PINNED_API_KEY:
        _PINNED_API_KEY.update(key=mod._API_KEY,
                               source=getattr(mod, "_API_KEY_SOURCE", None))
        return
    mod._API_KEY = _PINNED_API_KEY["key"]
    if hasattr(mod, "_API_KEY_SOURCE"):
        mod._API_KEY_SOURCE = _PINNED_API_KEY["source"]


@pytest.fixture(autouse=True)
def _real_keyring_optin(request, monkeypatch):
    """Opt-in: @pytest.mark.real_keyring re-enables keychain READS only."""
    if request.node.get_closest_marker("real_keyring"):
        import puffpai.security.secrets_provider as sp_mod
        monkeypatch.setattr(sp_mod, "_HAS_KEYRING", True)
    yield


#: The shipped config directory. Read from, never written to, during a test run.
SHIPPED_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path_factory, monkeypatch):
    """No test writes the shipped config, and none drops a credential in it.

    Two things used to land in the developer's checkout when a test exercised
    the setup wizard: ``config/puffpai_config.json`` was rewritten in place (so
    the next test, and the next developer, ran against whatever the last wizard
    test happened to choose), and ``config/home_assistant_config.json`` was
    CREATED -- a gitignored file holding a Home Assistant token, written by a
    test, on a machine that also runs the appliance. ``git status`` stayed clean
    for the second one precisely because it is gitignored, which is the worst
    combination: a credential file nobody sees appear.

    The tmp dir is a **copy** of the shipped one, not an empty directory. An
    empty config dir would make every test run against dataclass defaults -- a
    different appliance from the one that ships -- and would quietly stop
    testing the shipped values. Copying keeps the inputs identical and makes
    only the writes disposable.

    ``PUFFPAI_DATA_DIR`` is pointed at a temp path in the same breath, because
    the two are written together: ``setup_state.json``, the pending-apply
    record and the build manifest go to the state dir, and isolating one half
    of a wizard run is not isolating it.

    The config singleton is dropped on the way in and on the way out. Without
    that, a singleton built from the real config dir during collection would
    keep handing out ``CONFIG_DIR = <repo>/config`` -- and ``setup_ops`` writes
    to ``get_config().CONFIG_DIR``, so the isolation would be cosmetic.

    A test that wants a different config dir just sets the env var itself:
    ``monkeypatch`` inside the test runs after this fixture and wins.
    """
    import shutil

    try:
        from puffpai import config as cfgmod
    except Exception:
        yield
        return

    # The copy sits at <data>/config, where this build keeps its own records
    # (build_manifest.manifest_path), so a test that writes a build record
    # into CONFIG_DIR and the code that reads it agree on one file.
    state_dir = tmp_path_factory.mktemp("state_dir")
    config_dir = state_dir / "config"
    try:
        # The catalog snapshots are large and every reader opens the shipped
        # copy under the repo, so a per-test copy of them is pure disk churn.
        shutil.copytree(SHIPPED_CONFIG_DIR, config_dir,
                        ignore=shutil.ignore_patterns("catalog_snapshots", "*.npz", "*.gz"))
    except OSError:                      # a checkout without the shipped dir
        config_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv(cfgmod.CONFIG_DIR_ENV, str(config_dir))
    monkeypatch.setenv(cfgmod.STATE_DIR_ENV, str(state_dir))
    cfgmod._instance = None
    yield
    cfgmod._instance = None


@pytest.fixture(autouse=True)
def _isolate_config_health():
    """No test inherits the degraded configs a previous one recorded.

    ``config_health`` keeps its records in a process global, and nothing in a
    normal run clears it. A test that records a degraded catalog on purpose --
    to prove the wizard's health gate asks before building on one -- therefore
    left every later test in the same process building on a degraded config.
    The symptom was the wizard goldens: run alone they pass, run after
    ``test_wizard_flow`` the manifest grows a ``config_health`` section and the
    gate refuses the build outright ("the wizard wrote no manifest"). That is
    leakage, not a golden that moves, so it is fixed here rather than
    normalised away in the golden -- the section is real when it is there.
    """
    try:
        from puffpai import config_health
    except Exception:
        yield
        return

    config_health.clear()
    yield
    config_health.clear()


@pytest.fixture(autouse=True)
def _isolate_lan_posture():
    """No test makes a real DNS query through the LAN-posture check, and no
    test inherits the run posture a previous one declared.

    ``security/lan_posture`` resolves a URL's host on the WRITE path, because
    that is the only place a single-label name gets checked before it lands in
    a config file. Left alone in a test run that means a real query per wizard
    write -- slow, offline-hostile, and genuinely wrong: this developer's
    resolver answers ``homeassistant.local`` with a public wildcard address, so
    the shipped default URL read as a posture violation on one machine and not
    on another.

    Nothing resolves here. That is not "everything is fine": an unresolvable
    name falls back to the NAME-FORM verdict, which is the check that runs at
    config load anyway, so ``.local`` stays LAN-presumed and a registered-
    looking domain stays public. A test that needs a real resolving answer --
    the single-label name a search domain sends off-site is the interesting one
    -- installs its own resolver, and that outranks this.

    ``clear_active_posture`` is the other half: ``persist_posture`` declares the
    run posture in a process global, and a wizard test that ran first would
    otherwise decide what a later test's writes are judged against.
    """
    try:
        from puffpai.security import lan_posture
    except Exception:
        yield
        return

    def _nothing_resolves(host, port):
        from puffpai.security import netaddr
        raise netaddr.ResolutionError(f"no resolver in tests ({host})")

    lan_posture.set_resolver_override(_nothing_resolves)
    lan_posture.clear_active_posture()
    yield
    lan_posture.set_resolver_override(None)
    lan_posture.clear_active_posture()


@pytest.fixture(autouse=True)
def _isolate_user_data_dir(tmp_path_factory, monkeypatch):
    """No test ever writes to the developer's REAL per-user state directory.

    ``config.resolve_state_dir``'s last fallback is
    ``platform_paths.user_data_dir()`` — ``%LOCALAPPDATA%\\PuffPAI`` on Windows,
    ``~/.local/share/puffpai`` on Linux. On a machine that also RUNS PuffPAI
    that directory is live household state, and a test with ``$PUFFPAI_DATA_DIR``
    unset (which is most of them) would resolve straight into it.

    Pointing the function at a per-session temp root keeps the fallback under
    test — the resolution order, the env var, the dev-checkout exception all
    still behave exactly as they do in production — while the bytes land
    somewhere disposable. A test that means to assert the real platform layout
    calls ``platform_paths.user_data_dir`` through its own monkeypatch, or
    asserts on the module's logic directly.
    """
    try:
        from puffpai import config as _cfgmod, platform_paths
    except Exception:
        yield
        return
    root = str(tmp_path_factory.mktemp("user_data_dir"))
    monkeypatch.setattr(platform_paths, "user_data_dir", lambda: root)
    # The dev-checkout answer is cached per repo root; a test that creates or
    # removes <repo>/data state must not inherit a previous test's answer.
    _cfgmod._dev_state_cache.clear()
    yield
    _cfgmod._dev_state_cache.clear()


@pytest.fixture(autouse=True)
def _reset_ha_token_cache():
    """security/ha_token_source.py caches a successful resolution
    for five minutes so scrypt does not run on every HABridge construction.
    That cache is process-global, so without this a token resolved in one test
    leaks into the next one's assertions."""
    try:
        from puffpai.security.ha_token_source import invalidate_cache
    except Exception:
        yield
        return
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture(autouse=True)
def _ha_bridge_kept():
    """Every test hands the process-wide Home Assistant bridge back as it was.

    Saving Home Assistant settings rebuilds a running bridge with the new
    address and token (setup_ops.set_ha_config). In a test that bridge holds
    the test's token and outlived it, so a later announcement through the agent
    team dialled the real homeassistant.local: the outside-traffic guard
    stopped the pairing watcher mid-report, and its window never closed.
    """
    module = sys.modules.get("puffpai.ha_bridge")
    before = getattr(module, "_instance", None) if module is not None else None
    yield
    module = sys.modules.get("puffpai.ha_bridge")
    if module is not None and getattr(module, "_instance", None) is not before:
        module._instance = before


@pytest.fixture(autouse=True)
def _isolate_egress_overlay(tmp_path_factory):
    """Point the egress-door owner overlay at a per-test path.

    ``egress_doors.overlay_path()`` falls back to ``<repo>/state/`` when
    ``$PUFFPAI_DATA_DIR`` is unset, which is every dev checkout and every test
    run. Without this, one test that toggles a door (or a developer who once
    ran the API locally) leaves a file that silently OPENS doors for every
    later test — measured: it broke 15 tests across three egress files in
    exactly that way. The overlay outranks the config file by design, so tests
    that repoint only ``_config_path`` cannot defend themselves.

    Each test gets its own overlay path, so nothing a test writes escapes into
    the next one or into the developer's checkout.

    **That overlay seeds exactly two doors open: ``web_fetch`` and
    ``web_search``.** Read this before adding a third.

    Those two are the Toolsmith's, and they are real gates:
    ``agents/toolsmith.py`` asks ``require_open`` before its own SSRF
    checks, so with them shut every fetch and search refuses before a socket
    opens. That is the intended production posture and it is what the door's
    OWN tests assert. But several hundred pre-existing tests exercise the
    machinery BEHIND that consent — SSRF gates, redirect scoping, budget
    accounting, the planning loop's sealed host scope — and they were written
    when appliance-level consent did not exist. Seeding it here keeps those
    suites testing what they are about, instead of all failing at a new first
    gate and hiding real regressions behind one cause.

    Consequences to keep in mind:

    * A test that means to assert the SHUT behaviour must say so explicitly.
      Two established patterns work and both outrank this fixture: set the
      ``PUFFPAI_EGRESS_*`` environment variable (env beats the overlay), or
      call ``set_overlay_override`` with your own file, as
      ``test_egress_api.py`` does.
    * Every OTHER door stays shut here, which is the registry default. This
      fixture is not "egress on in tests"; it is "the two Toolsmith doors are
      consented to, so the gates behind them are what is under test".
    * ``tests/test_toolsmith_web_tools_egress.py`` asserts this set is exactly
      those two, so it cannot quietly grow into a blanket opt-out.
    """
    try:
        from puffpai.security import egress_doors
    except Exception:
        yield
        return
    overlay = tmp_path_factory.mktemp("egress_overlay") / "egress_doors.json"
    overlay.write_text(json.dumps({
        "version": 1,
        "kind": egress_doors.OVERLAY_KIND,
        "doors": dict(TEST_OPEN_DOORS),
    }), encoding="utf-8")
    egress_doors.set_overlay_override(str(overlay))
    yield
    egress_doors.set_overlay_override(None)
    egress_doors.reset_cache()


@pytest.fixture
def every_door_shut(tmp_path):
    """A build whose doors are all shut, the way a fresh install really ships.

    ``_isolate_egress_overlay`` (above) seeds ``web_fetch`` and ``web_search``
    OPEN for the whole suite, and that is the right default for the several
    hundred tests that exercise the machinery behind those gates. It is the
    wrong default for the wizard's Secure card, which compares the doors the
    build RECORDED against the doors that are actually open and reports "a door
    is open that this build did not record" when they differ. Under the
    suite-wide overlay that gap is always reported, and it says nothing about
    the product: a real offline build opens no door (verified against
    ``door_states()`` on a fresh data dir — all twelve read shut, source
    ``default``).

    Ask for this fixture in any test that means "the build as shipped".
    """
    from puffpai.security import egress_doors

    overlay = tmp_path / "no_doors_open.json"
    overlay.write_text(json.dumps({
        "version": 1,
        "kind": egress_doors.OVERLAY_KIND,
        "doors": {},
    }), encoding="utf-8")
    previous = egress_doors.overlay_path()
    egress_doors.set_overlay_override(str(overlay))
    egress_doors.reset_cache()
    yield
    egress_doors.set_overlay_override(previous)
    egress_doors.reset_cache()


#: What every declared door host resolves to during the test run. Any globally
#: routable literal would do; this one is IANA's, the address ``example.com``
#: answered with for years, so it can never be a household's own. Nothing dials
#: it — the guard below sees to that — and a test that means to assert a PRIVATE
#: answer has to install one itself.
TEST_PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture(autouse=True)
def _offline_door_resolver():
    """No test makes a real DNS query through the egress-door registry.

    ``security/egress_doors._check_url`` resolves a declared host and refuses
    unless every answer is globally routable — the control that closes
    manifest-declared SSRF. That check is only
    honest if it really looks the name up, so without this fixture every test
    that walks an OPEN door would query the household's resolver: slow, flaky,
    offline-hostile, and a test suite that quietly egresses.

    Every host resolves to one public address here. A test that needs a
    different answer — a name that points into the LAN, a name that will not
    resolve, a split public/loopback answer — calls
    ``egress_doors.set_resolver_override`` itself, and that call outranks this
    fixture for the duration of the test.
    """
    try:
        from puffpai.security import egress_doors
    except Exception:
        yield
        return
    egress_doors.set_resolver_override(
        lambda host, port: [TEST_PUBLIC_ADDRESS])

    # And no test builds a real transport by accident. Every host resolving to
    # a public literal above means the wrapper's checks PASS, and without this
    # the pinned session would then dial 93.184.216.34 for real — which is how
    # a suite acquires four-minute connect timeouts and a live egress it never
    # meant to make. A test that wants a transport stubs this itself (the
    # ``fake_requests``/``fake_get``/``transport`` fixtures do); a test that
    # wants the REAL pin restores it (test_security_hostname_not_destination.py does).
    real_send = egress_doors._send

    class _StubbedSend:
        """The per-door session, answered by a test's stand-in for
        ``egress_doors._send`` (the engine's tests stub that one seam; keys,
        searches and sign-ins go through this session in this build)."""

        def request(self, method, url, **kwargs):
            return egress_doors._send(method, url, pin=None, **kwargs)

        def close(self):
            pass

    def _no_wire(_door, **_kw):
        if egress_doors._send is not real_send:
            return _StubbedSend()
        raise AssertionError(
            "a test reached the wire through an egress door. Stub "
            "egress_doors._pinned_session (see tests/conftest.py) or, if the "
            "real pinned transport is the thing under test, restore it.")

    original = egress_doors._pinned_session
    egress_doors._pinned_session = _no_wire
    yield
    egress_doors._pinned_session = original
    egress_doors.set_resolver_override(None)


@pytest.fixture(autouse=True)
def _isolate_egress_policy(tmp_path_factory):
    """Point the egress POLICY overlay at a per-test path, with none set.

    Same hazard as the door overlay: ``egress_policy.policy_path()`` falls back
    to ``<repo>/state/`` when ``$PUFFPAI_DATA_DIR`` is unset, so one test that
    flips ``chat_allow_egress`` would otherwise leave a file that changes every
    later test's answer.

    Unlike the doors, nothing is seeded: the opt-in ships OFF, the tests that
    care set it themselves, and a chat turn acquiring egress by default in the
    test suite is exactly the accident this file exists to prevent.
    """
    try:
        from puffpai.security import egress_policy
    except Exception:
        yield
        return
    egress_policy.set_policy_override(
        str(tmp_path_factory.mktemp("egress_policy") / "egress_policy.json"))
    yield
    egress_policy.set_policy_override(None)
    egress_policy.reset_cache()


@pytest.fixture(autouse=True)
def _isolate_used_approvals(tmp_path_factory):
    """Point the persisted used-approval ledger (ha_pending_apply) at a
    per-test file, so one test's spent approval never leaks into the next or
    into the developer's checkout."""
    try:
        from puffpai import ha_pending_apply
    except Exception:
        yield
        return
    ha_pending_apply.set_used_path_override(
        str(tmp_path_factory.mktemp("used_approvals") / "ha_used_approvals.json"))
    yield
    ha_pending_apply.set_used_path_override(None)


@pytest.fixture(autouse=True)
def _paired_devices_stand_in(request, monkeypatch):
    """Most route tests send a made-up ``X-Puff-Device-Id`` and are about
    something else. For them any well-formed id counts as a paired device,
    exactly as before the check existed. A test marked ``real_pairing`` gets
    the real check against the pairing store (tests/test_paired_device_gate.py).
    Production code has no such switch."""
    try:
        from puffpai.security import paired_devices
    except ImportError:
        yield
        return
    paired_devices.forget()
    if request.node.get_closest_marker("real_pairing") is not None:
        yield
        paired_devices.forget()
        return

    def _any(session_id):
        sid = str(session_id or "")
        if not sid:
            return None
        return {"id": "test-device1", "name": "", "label": f"test device {sid[:6]}"}

    monkeypatch.setattr(paired_devices, "lookup", _any)
    yield


@pytest.fixture(autouse=True)
def _keyring_guard(request, monkeypatch):
    """A fresh stand-in ``keyring`` for every test; the real store never.

    See the note at the top of this file. Fails the test (in teardown, so it
    cannot be swallowed by an ``except``) if anything reached the real OS
    password store.
    """
    marked = request.node.get_closest_marker("keyring_stub") is not None
    entries: dict = {}
    stub = _keyring_stub(entries if marked else None)
    request.node.puffpai_keyring_entries = entries if marked else None
    monkeypatch.setitem(sys.modules, "keyring", stub)
    provider = sys.modules.get("puffpai.security.secrets_provider")
    if provider is not None:
        monkeypatch.setattr(provider, "keyring", stub, raising=False)
    before = len(REAL_KEYRING_TOUCHES)
    yield
    touched = REAL_KEYRING_TOUCHES[before:]
    del REAL_KEYRING_TOUCHES[before:]
    problems = []
    if touched:
        problems.append("reached the REAL OS password store: " + "; ".join(touched))
    if problems:
        pytest.fail(f"{request.node.nodeid} " + " and ".join(problems),
                    pytrace=False)


@pytest.fixture
def keyring_entries(request):
    """What a ``@pytest.mark.keyring_stub`` test's stand-in keyring holds:
    ``{(service, entry): value}``."""
    entries = getattr(request.node, "puffpai_keyring_entries", None)
    if entries is None:
        pytest.fail("keyring_entries needs @pytest.mark.keyring_stub", pytrace=False)
    return entries


#: The five household apps (puffpai.first_apps). They ship on in this build,
#: and the pinned test state is a bot that has all five on; the first-apps
#: tests start from a state with no on-set (tests/test_first_apps.py).
FIRST_APPS = ("alarmflow", "calendar", "fitfam", "freshtable", "trivia")


def turn_on_first_apps(state_dir: str) -> None:
    """Make *state_dir* a bot that has every first app turned on."""
    import json
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "apps_on.json"), "w", encoding="utf-8") as fh:
        json.dump({"on": list(FIRST_APPS)}, fh)


@pytest.fixture(autouse=True)
def _router_app_words_kept():
    """Turning a first app on or off reloads the router's app words
    (router.reload_app_words). Each test gets back the words it started with,
    so one test's bot does not route the next test's sentences."""
    router = sys.modules.get("puffpai.router")
    saved = ({n: getattr(router, n) for n in ("_PACKS", "_APP_KEYWORDS", "_ALL_AGENT_KEYWORDS")
              if hasattr(router, n)}
             if router is not None else None)
    yield
    if router is not None and saved is not None:
        for name, value in saved.items():
            setattr(router, name, value)


@pytest.fixture(autouse=True)
def _pin_state_roots(monkeypatch, _no_real_ollama, _isolate_config_dir):
    """Pin the state root next to the data root; forbid the real per-user one.

    ``_isolate_config_dir`` has already pointed ``$PUFFPAI_DATA_DIR`` at a
    per-test temp folder. ``$PUFFPAI_STATE_DIR`` is set to its ``state``
    subfolder, which is where ``config.get_state_dir`` lands with the data root
    alone, so a test that reads the variable and a test that asks
    ``get_state_dir`` see the same folder. That folder is a bot with every
    first app on.

    Where this tree's config names the real per-user directory through
    ``config._user_state_root``, that function is replaced by one that raises:
    a test that reaches for the real machine fails, loudly, naming itself.
    """
    from puffpai import config

    data = os.environ.get("PUFFPAI_DATA_DIR") or tempfile.mkdtemp(
        prefix="puffpai_test_data_")
    state = os.path.join(data, "state")
    turn_on_first_apps(state)
    monkeypatch.setenv("PUFFPAI_DATA_DIR", data)
    monkeypatch.setenv("PUFFPAI_STATE_DIR", state)

    def _forbidden() -> str:
        raise AssertionError(
            "a test resolved the REAL per-user state directory "
            "(config._user_state_root). Pin PUFFPAI_STATE_DIR, or pass an "
            "explicit path - nothing in the suite may touch the developer's "
            "own install.")

    if hasattr(config, "_user_state_root"):
        monkeypatch.setattr(config, "_user_state_root", _forbidden)
    config.get_config(_force_reload=True)
    _invalidate_bot_identity()
    # The installed-apps list is cached per process. Read under the last
    # test's roots, it would show that test's installed apps to this one:
    # drop it whenever the roots move.
    _forget_installed_apps()
    try:
        yield
    finally:
        _invalidate_bot_identity()
        _forget_installed_apps()
        config._instance = None


def _invalidate_bot_identity() -> None:
    try:
        from puffpai import bot_identity
    except ImportError:
        return
    bot_identity.invalidate()


def _forget_installed_apps() -> None:
    try:
        from puffpai import app_registry
    except ImportError:
        app_registry = None
    if app_registry is not None and hasattr(app_registry, "_cache_lock"):
        with app_registry._cache_lock:
            app_registry._cached_manifests = None
    # The same for what the bot may reach: the run setting and the outside
    # service switches are cached per process too. Read under the last test's
    # build record, an "only the services you said yes to" setting would leak
    # into a test whose build has none: drop them whenever the roots move.
    try:
        from puffpai.security import egress_doors
    except ImportError:
        egress_doors = None
    if egress_doors is not None:
        egress_doors.reset_cache()
    try:
        from puffpai.security import outside_switches
    except ImportError:
        outside_switches = None
    if outside_switches is not None:
        outside_switches.forget()


@pytest.fixture
def allow_real_user_state_root(monkeypatch):
    """Undo the booby trap, for the one test whose subject IS that resolver.

    ``test_the_default_state_dir_is_outside_any_checkout`` exists to prove the
    default lands outside the checkout, which it cannot do without asking for the
    default. It only READS the path. Any other test asking for this fixture
    should be asked why.
    """
    from puffpai import config
    if _REAL_USER_STATE_ROOT is not None:
        monkeypatch.setattr(config, "_user_state_root", _REAL_USER_STATE_ROOT)
    yield


@pytest.fixture(autouse=True)
def _reset_cli_stdin_latch():
    """Start every test with the wizard's end-of-file latch cleared.

    ``cli._read_line`` remembers that stdin ran out so a redirected run stops
    prompting (see its docstring). That latch belongs to one wizard run; in a
    test process, one test reaching end of input must not make every later
    test's prompt come back empty.
    """
    cli = sys.modules.get("puffpai.cli")
    reset = getattr(cli, "_reset_stdin_state", None)
    if reset is not None:
        reset()
    yield
    if reset is not None:
        reset()


@pytest.fixture(autouse=True)
def _no_open_question_left_over():
    """Start every test with no open clarifying question.

    ``pending_clarifier`` keeps the chat's last question ("what time?") per
    conversation, process-wide. A test that leaves one open makes a later
    test's ordinary sentence ("what is this song") read as its answer.
    """
    clarifier = sys.modules.get("puffpai.pending_clarifier")
    reset = getattr(clarifier, "reset", None)
    if reset is not None:
        reset()
    yield
    if reset is not None:
        reset()


@pytest.fixture(autouse=True)
def _fresh_request_budget():
    """Start every test with the web server's per-address request count
    cleared. Every test client is 127.0.0.1, so on a fast computer the whole
    run shares one 600-a-minute budget and a later test gets "Rate limit
    exceeded" for requests it never made. The limit itself is unchanged;
    a test about it makes its own requests."""
    api = sys.modules.get("puffpai.api_server")
    if api is not None and isinstance(getattr(api, "_rate_limit_store", None), dict):
        api._rate_limit_store.clear()
    yield


@pytest.fixture(autouse=True)
def _no_real_downloads(monkeypatch, request):
    """No test downloads a book or re-reads an outside list for real.

    The download and list-refresh code reach the network through one seam
    each; a test that is about a download replaces that seam with its own
    stand-in (its monkeypatch runs after this one and wins). The tests of
    source_fetch's own seam (tests/test_source_fetch_kiwix.py) keep the real
    function: it goes through egress_request, whose transport the
    ``_no_wire`` fixture above already refuses.
    """
    import importlib

    def _refuse(*_a, **_k):
        raise OSError("tests never go online")

    names = ["puffpai.source_fetch", "puffpai.catalogs.live"]
    if request.module.__name__.endswith("test_source_fetch_kiwix"):
        names.remove("puffpai.source_fetch")
    for name in names:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(module, "_default_transport"):
            monkeypatch.setattr(module, "_default_transport", _refuse)
    yield


@pytest.fixture(autouse=True)
def first_fetches(monkeypatch):
    """Switching a live card on starts no background fetch in a test.

    ``feeds.start_first_fetch`` normally starts a thread; a thread that outlives
    its test would write into the next test's state folder. Here it only
    records the app id. A test about the first fetch reads this list and runs
    ``feeds._run_first_fetch`` itself.
    """
    started: list[str] = []
    try:
        from puffpai import feeds
    except ImportError:
        feeds = None
    if feeds is not None and hasattr(feeds, "_first_fetch_starter"):
        monkeypatch.setattr(feeds, "_first_fetch_starter",
                            lambda app_id: started.append(app_id) or True)
    yield started


#: Ports a local Ollama listens on (the default, and a second one a developer
#: may run beside it).
_OLLAMA_PORTS = (11434, 11435)


@pytest.fixture(autouse=True)
def _no_real_ollama(monkeypatch, request):
    """Every test sees the default Ollama address and never reaches a real one.

    A developer who points PUFFPAI_OLLAMA_URL (or OLLAMA_HOST) at their own
    Ollama was changing what a dozen address tests saw; a test that needs an
    address sets its own. And no test may talk to a real Ollama: a connect
    to its ports is refused, so a probe reads as "nothing answered". The one
    exception is tests/test_llm_benchmark.py, whose subject IS the local
    Ollama on this computer (it still may not leave this computer).
    """
    import socket

    live_ollama = request.module.__name__.endswith("test_llm_benchmark")

    monkeypatch.delenv("PUFFPAI_OLLAMA_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    real_connect = socket.socket.connect

    real_connect_ex = socket.socket.connect_ex
    real_execute = subprocess.Popen._execute_child

    def _connect(sock, address):
        if (not live_ollama and isinstance(address, tuple)
                and len(address) >= 2 and address[1] in _OLLAMA_PORTS):
            raise ConnectionRefusedError("tests never talk to a real Ollama")
        _outside_exit_guard(address)
        return real_connect(sock, address)

    def _connect_ex(sock, address):
        _outside_exit_guard(address)
        return real_connect_ex(sock, address)

    def _execute_child(self, args, *rest, **kw):
        words = [args] if isinstance(args, (str, bytes)) else list(args or [])
        if any(_NET_PROGRAM_RE.search(str(w)) for w in words[:3]):
            _outside_exit_guard(("a program: " + " ".join(map(str, words[:2])), 0))
        return real_execute(self, args, *rest, **kw)

    monkeypatch.setattr(socket.socket, "connect", _connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex)
    monkeypatch.setattr(subprocess.Popen, "_execute_child", _execute_child)
    yield


# ── The single-exit rule, at run time ──────────────────────────────────────
# tests/test_outside_single_exit.py reads the source; this watches what the
# suite actually does. Any connection to somewhere other than this computer,
# or any fetching program started, that comes from this tree's own code must be
# opened by one of the listed exits (which log and pin it) or by a line that
# says it is a home-network call (``# lan-egress: <why>``). Anything else
# raises OutsideExitViolation - a BaseException, so an ``except Exception``
# that swallows network errors cannot hide it - and is kept for the summary.

_SRC_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), "src", "puffpai")
_EXIT_FILES = tuple(os.path.normcase(os.path.join(_SRC_ROOT, *rel.split("/")))
                    for rel in ("security/egress_doors.py",
                                "security/pinned_net.py", "source_fetch.py",
                                "wizard/netcheck.py"))
_NET_PROGRAM_RE = __import__("re").compile(
    r"(?i)(?:^|[\\/\s])(?:curl|wget|invoke-webrequest|invoke-restmethod|bitsadmin"
    r"|ncat|telnet|sftp|scp|ssh|rsync)(?:\.exe)?(?:$|\s)")
OUTSIDE_EXIT_VIOLATIONS: list = []


class OutsideExitViolation(BaseException):
    """This tree's code reached outside this computer past the listed exits."""


def _is_this_computer(address) -> bool:
    host = address[0] if isinstance(address, tuple) and address else address
    if not isinstance(host, str):
        return True  # a Unix socket path or similar: not the network
    host = host.lower().strip("[]")
    return (host in ("localhost", "::1", "0.0.0.0", "")
            or host.startswith("127.") or host.startswith("::ffff:127."))


def _outside_exit_guard(address) -> None:
    import linecache
    if _is_this_computer(address) and not str(address[0]).startswith("a program"):
        return
    frame = sys._getframe(2)
    site = None
    src = os.path.normcase(_SRC_ROOT)
    while frame is not None:
        name = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
        if name in _EXIT_FILES:
            return
        if site is None and name.startswith(src):
            site = frame
        frame = frame.f_back
    if site is None:
        return  # the test's own code, or a library a test drives directly
    number = site.f_lineno
    near = [linecache.getline(site.f_code.co_filename, n)
            for n in range(max(1, number - 3), number + 1)]
    if any("# lan-egress:" in line for line in near):
        # The marker says "home network": hold it to that. A marked call to
        # an internet address is a violation too (security.lan_guard refuses
        # it in the running app).
        from puffpai.security import lan_guard
        if lan_guard.is_home(address):
            return
        where = f"{os.path.relpath(site.f_code.co_filename, _SRC_ROOT)}:{number}"
        OUTSIDE_EXIT_VIOLATIONS.append(f"{where} -> {address!r} (marked LAN)")
        raise OutsideExitViolation(
            f"{where} is marked '# lan-egress:' but reached {address!r}, "
            f"which is not this computer or a home-network address")
    where = (f"{os.path.relpath(site.f_code.co_filename, _SRC_ROOT)}:{number}")
    OUTSIDE_EXIT_VIOLATIONS.append(f"{where} -> {address!r}")
    raise OutsideExitViolation(
        f"{where} reached {address!r} without a listed exit (egress_doors, "
        f"source_fetch, netcheck) or a '# lan-egress: <why>' marker")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if OUTSIDE_EXIT_VIOLATIONS:
        terminalreporter.section("outside traffic past the listed exits")
        for row in dict.fromkeys(OUTSIDE_EXIT_VIOLATIONS):
            terminalreporter.write_line(row)


def pytest_sessionfinish(session, exitstatus):
    if OUTSIDE_EXIT_VIOLATIONS and session.exitstatus == 0:
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def _no_real_installs(request, monkeypatch):
    """Tests never install a program on this computer, never start Docker
    Desktop and never wait for it (Docker and app runtimes are
    installed after the one Yes). A test of the installer replaces these
    seams itself."""
    try:
        from puffpai import deps
    except ImportError:
        yield
        return
    monkeypatch.setattr(deps, "_run_installer",
                        lambda argv, extra_env=None: {"code": 1, "output": "tests never install"})
    monkeypatch.setattr(deps, "_start_docker", lambda: None)
    monkeypatch.setattr(deps, "_sleep", lambda s: None)
    monkeypatch.setattr(deps, "DOCKER_WAIT_S", 0)
    deps._FAILED.clear()
    yield
    deps._FAILED.clear()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_keyring: allow this test to reach the real OS keychain "
        "(bypasses the suite-wide _HAS_KEYRING=False autouse guard)",
    )
    config.addinivalue_line(
        "markers", "keyring_stub: the test is about keyring behaviour; it "
        "gets an in-memory stand-in that keeps what is written "
        "(keyring_entries), and may put its own stand-in in place")
    config.addinivalue_line(
        "markers", "real_pairing: the test is about the paired-device check; "
        "X-Puff-Device-Id is checked against a real pairing store")
