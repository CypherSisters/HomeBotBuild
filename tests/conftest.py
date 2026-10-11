"""
Shared pytest fixtures for PuffPAI tests.
Mocks all external dependencies (Ollama, Kiwix, Node-RED, Home Assistant).
"""

import json
import logging
import os
import sys
import pytest

# Seed a synthetic API key BEFORE any test module (or this file's own later
# imports) can import puffpai.api_server, which resolves the key at import
# time via _load_or_create_api_key(). On a CI runner with no vault/env
# file/keyring entry anywhere, that import raises RuntimeError during
# collection -- before the _secret_hygiene fixture below ever gets a chance
# to run (fixtures don't execute during collection). setdefault so a
# genuinely-configured environment (e.g. a developer's real key) still wins.
os.environ.setdefault("PUFFPAI_API_KEY", "0123456789abcdef" * 4)

# Add src to path so puffpai package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

#: Egress doors the suite grants appliance-level consent to, and NOTHING more.
#: See ``_isolate_egress_overlay`` for why, and
#: ``tests/test_toolsmith_web_tools_egress.py`` for the test that pins this to
#: exactly these two so it cannot grow into a blanket "egress on in tests".
TEST_OPEN_DOORS = {"web_fetch": True, "web_search": True}


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

    config_dir = tmp_path_factory.mktemp("config_dir") / "config"
    try:
        shutil.copytree(SHIPPED_CONFIG_DIR, config_dir)
    except OSError:                      # a checkout without the shipped dir
        config_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path_factory.mktemp("state_dir")

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
    def _no_wire(_door):
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


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_keyring: allow this test to reach the real OS keychain "
        "(bypasses the suite-wide _HAS_KEYRING=False autouse guard)",
    )
