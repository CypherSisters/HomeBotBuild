"""The install path: defects that would make a stranger's install fail.

These are file-content guards, not behaviour tests, because the artefacts they
protect are a shell script and two Markdown files. Each case is one way a
stranger walking the documented install can fail, and each would silently
come back if someone re-copied an old snippet:

* a one-liner that fetches ``.../puffpai/main/scripts/install_ubuntu.sh``,
  which 404s. ``curl -fsSL`` writes nothing on a 404 and ``sudo bash`` of an
  empty stream exits 0, a silent no-op, the worst failure shape there is;
* a script that names ``dolphin-llama3:8b`` while the build runs
  ``gpt-oss:20b`` and ``qwen3:30b-a3b``;
* fetching an **x86_64** ``kiwix-tools`` tarball onto an arm64 appliance, for
  a component the project has a standing rule against shipping at all (the
  build reads ZIM files with its own ``zim_reader``);
* ``cd puffpai`` after cloning ``PuffPAI``, so on any case-sensitive
  filesystem every later step runs in ``$HOME``;
* long-running compose services with ``restart: "no"``, which do not come
  back after a reboot.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts" / "install_ubuntu.sh"
INSTALL_DOC = REPO / "docs" / "INSTALLATION.md"
README = REPO / "README.md"

#: The models the deployed config and agents/engine.py actually name.
REQUIRED_MODELS = ("gpt-oss:20b", "qwen3:30b-a3b", "qwen2.5vl:7b", "nomic-embed-text")

DOCS = (INSTALL_DOC, README)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── The one-liner ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_raw_url_points_at_the_stale_main_branch(doc: Path) -> None:
    """`main` is 256 commits behind and its installer 404s."""
    bad = re.findall(
        r"raw\.githubusercontent\.com/CypherSisters/[^/\s]+/main/", _text(doc), re.I
    )
    assert not bad, f"{doc.name} still fetches from main: {bad}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_docs_do_not_pipe_a_download_into_a_root_shell(doc: Path) -> None:
    """`curl... | sudo bash` turns a 404 into a silent successful no-op."""
    piped = re.findall(r"curl[^\n|]*\|\s*sudo\s+(?:bash|sh)", _text(doc))
    assert not piped, f"{doc.name} still pipes a download into a root shell: {piped}"


# ── The installer ───────────────────────────────────────────────────────────

def test_installer_is_strict_and_truncation_safe() -> None:
    text = _text(INSTALLER)
    assert "set -euo pipefail" in text
    # Everything lives in main, invoked on the last non-blank line, so a
    # truncated download runs nothing rather than half an installer.
    assert re.search(r"^main\(\)\s*\{", text, re.M), "no main() definition"
    last = [ln for ln in text.splitlines() if ln.strip()][-1]
    assert last.strip() == 'main "$@"', f"last line is {last!r}, not main \"$@\""


def test_installer_offers_dry_run_and_explicit_consent() -> None:
    text = _text(INSTALLER)
    for flag in ("--dry-run", "--yes", "--help"):
        assert flag in text, f"installer has no {flag}"
    assert "confirm()" in text, "no confirmation gate before the sudo actions"


def test_installer_pulls_the_models_the_build_actually_runs() -> None:
    text = _text(INSTALLER)
    for model in REQUIRED_MODELS:
        assert model in text, f"installer never pulls {model}"
    assert "dolphin-llama3" not in text, "installer still pulls a model this build does not run"


def test_installer_and_docs_ship_no_kiwix() -> None:
    """Standing rule for the reference build: own zim_reader, no kiwix-serve."""
    # Prose may mention kiwix-serve to say the build does not use it; what must
    # not survive is anything that INSTALLS it.
    for path in (INSTALLER, INSTALL_DOC):
        text = _text(path)
        assert "kiwix-tools_linux-x86_64" not in text, (
            f"{path.name} still fetches an x86_64 kiwix binary"
        )
        assert "/usr/local/bin/kiwix-serve" not in text, (
            f"{path.name} still installs kiwix-serve"
        )
    assert "kiwix" not in _text(INSTALLER).lower(), "the installer still mentions kiwix"


def test_installer_never_cds_into_the_wrong_case_directory() -> None:
    """`git clone.../PuffPAI` creates PuffPAI; `cd puffpai` then fails."""
    assert not re.search(r"^\s*cd\s+puffpai\s*$", _text(INSTALLER), re.M)


def test_installer_verifies_downloads_are_not_empty() -> None:
    """A 404 body must not be executed or treated as success."""
    text = _text(INSTALLER)
    assert "-s /tmp/ollama-install.sh" in text, "no emptiness check on the Ollama installer"
    assert "curl -fsSL" in text


def test_installer_smoke_checks_the_api() -> None:
    text = _text(INSTALLER)
    assert "/health" in text
    assert "journalctl -u puffpai-api" in text, "no diagnosis path when the smoke check fails"


def test_installer_never_prints_the_token() -> None:
    """The PAT is used once and must never reach the console or a log."""
    text = _text(INSTALLER)
    assert "run_secret" in text
    # The only place the real token may be interpolated is the second
    # (non-printed) argument of run_secret.
    for line in text.splitlines():
        if "PUFFPAI_GH_TOKEN" in line and "x-access-token" in line:
            assert "<redacted>" in line or line.lstrip().startswith(
                "\"git clone"
            ) or "run_secret" not in line, line


# ──: reboot durability ────────────────────────────────────────────

# The ephemeral radio runner is deliberately excluded: it is a one-shot.
LONG_RUNNING_COMPOSE = (
    "docker-compose.media.yml",
    "docker-compose.nodered.yml",
    "docker-compose.spotify.yml",
    "docker-compose.spotify-voice.yml",
    "searxng/docker-compose.searxng.yml",
)


@pytest.mark.parametrize("rel", LONG_RUNNING_COMPOSE)
def test_long_running_services_survive_a_reboot(rel: str) -> None:
    """jellyfin and calibre-web carried `restart: "no"` and stayed down."""
    path = REPO / rel
    if not path.exists():          # pragma: no cover - tree layout guard
        pytest.skip(f"{rel} not present")
    text = _text(path)
    policies = re.findall(r"^\s*restart:\s*(\S+)\s*$", text, re.M)
    assert policies, f"{rel} declares no restart policy at all"
    for policy in policies:
        assert policy.strip("\"'") in ("unless-stopped", "always"), (
            f"{rel} has restart: {policy} — it will not come back after a reboot"
        )
