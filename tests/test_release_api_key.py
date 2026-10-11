"""Release gate: no appliance may run on a shared, published API key.

The installer used to seed `PUFFPAI_API_KEY=CHANGE_ME` into three env files
and the backend accepted whatever it resolved. Every box built from this
repository would therefore have had the SAME "secret", printed in the repo, in
front of the household's documents, photos, vault status and LLM — and the
backend binds beyond loopback so a LAN peer could use it.

Two halves, tested here:

* the backend refuses to start on a placeholder (`validate_api_key`), and
* the installer generates a real one instead of seeding a placeholder.

The installer half is a source-text check rather than an execution: running
`install_orin.sh` for real needs a Jetson, root, and a live network.
"""
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.modpatch import patched_modules

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install_orin.sh"


@pytest.fixture(scope="module")
def mod():
    """Import api_server once, with firewall_manager mocked out.

    Same recipe as tests/test_api_key_failclosed.py. The import-time
    ``_API_KEY = validate_api_key(_load_or_create_api_key())`` resolves through
    the env tier the session conftest pre-seeds, so importing never reaches the
    provisioning path or the real OS keyring.
    """
    import importlib
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules(
            {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        if "puffpai.api_server" in sys.modules:
            return sys.modules["puffpai.api_server"]
        return importlib.import_module("puffpai.api_server")


# ── backend: what it refuses ────────────────────────────────────────────────

# None of these is a credential. They are the strings a half-finished install
# leaves behind.
REFUSED = [
    "CHANGE_ME",
    "change_me",
    "ChAnGe_Me",
    "changeme",
    "YOUR_API_KEY_HERE",
    "your-api-key-here",
    "REPLACE_ME",
    "placeholder",
    "TODO",
    "password",
    "secret",
    "",
    "   ",
    None,
    "short",             # 5 chars
    "0123456789abcde",   # 15 chars: one below the floor
]


@pytest.mark.parametrize("bad", REFUSED)
def test_placeholder_or_short_key_is_refused(mod, bad):
    with pytest.raises(RuntimeError) as excinfo:
        mod.validate_api_key(bad)
    message = str(excinfo.value)
    assert "refusing to start" in message.lower()
    # The operator needs to be told WHERE to fix it — and the place named has to
    # be a path THIS install actually has. The message used to recite
    # /etc/puffpai/api.env and "Orin appliance" on every platform, which sent an
    # owner installing on a laptop hunting for a directory that does not exist.
    # It now names the env file the installer really mints, computed from this
    # install's state directory.
    from puffpai.envfile import ENV_FILENAME, state_env_path
    assert ENV_FILENAME in message
    assert state_env_path() in message
    # All three tiers still get named, so a box that keeps its key elsewhere is
    # not left guessing.
    assert "PUFFPAI_API_KEY" in message
    assert "keyring" in message
    assert "openssl rand -hex 32" in message


def test_refusal_never_quotes_the_value(mod):
    """A fatal log line must not become the place the secret leaks."""
    almost = "0123456789abcde"          # refused for length, not for content
    with pytest.raises(RuntimeError) as excinfo:
        mod.validate_api_key(almost)
    assert almost not in str(excinfo.value)


ACCEPTED = [
    "a" * 16,                                    # exactly at the floor
    "0f3c9d2b5a7e1148c6d90b23fe4a7c815d2e6f90ab3c4d5e6f7089a1b2c3d4e5f",  # openssl rand -hex 32 shape
    "puffpai-test-" + "api-key-not-a-real-secret",  # the suite's own literal
]


@pytest.mark.parametrize("good", ACCEPTED)
def test_real_key_is_accepted(mod, good):
    assert mod.validate_api_key(good) == good


def test_surrounding_whitespace_is_stripped_not_counted(mod):
    key = "b" * 20
    assert mod.validate_api_key("  " + key + "\n") == key
    with pytest.raises(RuntimeError):
        mod.validate_api_key("   " + "c" * 10 + "  ")   # 10 chars of substance


def test_min_length_is_at_least_sixteen(mod):
    assert mod.MIN_API_KEY_LEN >= 16


def test_change_me_is_in_the_placeholder_set(mod):
    """The exact string the installer used to seed."""
    assert "change_me" in mod.PLACEHOLDER_API_KEYS


def test_module_key_passed_validation(mod):
    """The import-time binding goes through the gate, not around it."""
    assert mod._API_KEY == mod.validate_api_key(mod._API_KEY)


def test_matching_is_whole_value_not_substring(mod):
    """A random key containing "secret" or "test" must still be accepted."""
    assert mod.validate_api_key("secret" + "f" * 40)
    assert mod.validate_api_key("f" * 20 + "password")


# ── installer: what it writes ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def installer_text():
    return INSTALLER.read_text(encoding="utf-8")


def test_installer_seeds_no_placeholder_api_key(installer_text):
    """No LIVE line writes the placeholder. Comments about it are fine."""
    offenders = [
        line for line in installer_text.splitlines()
        if "PUFFPAI_API_KEY=CHANGE_ME" in line and not line.lstrip().startswith("#")
    ]
    assert offenders == [], (
        "scripts/install_orin.sh still seeds a placeholder API key: "
        + repr(offenders)
    )


def test_installer_generates_one_key_with_openssl(installer_text):
    assert "openssl rand -hex 32" in installer_text
    assert "resolve_shared_api_key" in installer_text


def test_installer_writes_the_same_key_to_every_env_file(installer_text):
    """All three consumers must carry the SAME value or the dashboard 401s."""
    for target in ('${ETC_DIR}/api.env', '${ETC_DIR}/dashboard.env',
                   '${INSTALL_DIR}/.env'):
        assert f'set_api_key_in_file "{target}"' in installer_text, target
    assert installer_text.count("PUFFPAI_API_KEY=${API_KEY_VALUE}") == 3


def test_installer_never_echoes_the_key(installer_text):
    """Nothing may print $API_KEY_VALUE to the terminal or the install log.

    Writing it INTO an env file is the whole point, so a line that redirects
    to a file is not an offender; a line that does not is.
    """
    offenders = []
    for line in installer_text.splitlines():
        if "API_KEY_VALUE" not in line:
            continue
        if not re.match(r"\s*(info|warn|die|printf|echo|log)\b", line):
            continue
        if ">>" in line or re.search(r'>\s*"', line):
            continue          # writes the key to a file, does not print it
        offenders.append(line.strip())
    assert offenders == [], offenders


def test_installer_writes_the_key_only_by_redirect_or_heredoc(installer_text):
    """Belt and braces: the key never reaches a command line either.

    An argv is world-readable in /proc for as long as the process runs, so the
    value must travel by heredoc, by shell redirect, or through the
    environment (awk's ENVIRON) — never as an argument.
    """
    bad = [
        line.strip() for line in installer_text.splitlines()
        if re.search(r'(sed|curl|awk|install|tee)\b[^\n]*\$\{?API_KEY_VALUE', line)
    ]
    assert bad == [], bad


def test_installer_still_marks_third_party_tokens_must_fill(installer_text):
    """We can mint our own key; we cannot mint someone else's token."""
    for name in ("HA_TOKEN=CHANGE_ME", "HA_ACCESS_TOKEN=CHANGE_ME",
                 "HA_BASE_URL=CHANGE_ME", "JELLYFIN_API_KEY=CHANGE_ME"):
        assert name in installer_text, name


def test_verify_phase_fails_loudly_on_a_placeholder(installer_text):
    """A box left with HA_TOKEN=CHANGE_ME must not exit 0."""
    assert "verify_fail" in installer_text
    assert "VERIFY FAILURE(S)" in installer_text
    verify = installer_text[installer_text.index("phase_verify() {"):]
    assert "is_placeholder" in verify
    assert "vault/mfa-status" in verify, (
        "the verify phase must exercise storage through a keyed endpoint"
    )


def test_verify_phase_drops_the_unreachable_401_branch(installer_text):
    """/health is public (api_server._PUBLIC_ENDPOINTS); 401 there is a bug."""
    verify = installer_text[installer_text.index("phase_verify() {"):]
    health_block = verify[:verify.index("vault/mfa-status")]
    assert "401|403) info" not in health_block
    assert "backend up and key-gated" not in health_block


def test_pairing_is_seeded_at_knowledge_not_all(installer_text):
    """A fresh box seeded with 'all' 401s before any device can pair.

    dashboard/app/_lib/server/pairing.ts, "staged enforcement": the kiosk
    browser on the appliance is just another unpaired client and there is
    deliberately no localhost bypass.
    """
    assert "PUFF_PAIRING_ENFORCE=knowledge" in installer_text
    assert "PUFF_PAIRING_ENFORCE=all\nENVFILE" not in installer_text
    # and the operator is told how to tighten it afterwards
    assert "PUFF_PAIRING_ENFORCE=all/' /etc/puffpai/dashboard.env" in installer_text \
        or "PUFF_PAIRING_ENFORCE=all/' ${ETC_DIR}/dashboard.env" in installer_text


def test_installer_placeholder_list_covers_the_backend_set(mod, installer_text):
    """The shell and the Python must mean the same thing by "placeholder"."""
    case_block = installer_text[installer_text.index("is_placeholder() {"):]
    case_block = case_block[:case_block.index("\n}\n")]
    for name in ("change_me", "your_api_key_here", "replace_me", "placeholder",
                 "todo"):
        assert name in case_block, name
        assert name in mod.PLACEHOLDER_API_KEYS, name
    assert "${#v}" in case_block and "16" in case_block, (
        "the installer must apply the same minimum length as the backend"
    )
