"""Regression tests pinning secret hygiene at two layers.

1. The suite-wide runtime guard (conftest.py). The dev machine's Credential
   Manager service "puffpai" holds a REAL HA_TOKEN and PUFFPAI_API_KEY, and
   api_server resolves PUFFPAI_API_KEY at import time (api_server.py:355) —
   generating and WRITING a new keychain entry if nothing resolves. These
   tests fail loudly if the conftest guard regresses.
2. A static (AST-only) scan of tests/ and scripts/ for credential reads that
   run at IMPORT time. scripts/probe_ha_ws*.py used to live in tests/ and
   read a real HA token at module top level, so merely COLLECTING them
   leaked it — before conftest's guard, and before any fixture, can run. The
   scan never imports or executes the files it inspects.
"""
import ast
import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import puffpai.security.secrets_provider as sp_mod
from tests.conftest import _TEST_API_KEY
from tests.modpatch import patched_modules
from tests._secret_asserts import _assert_secret_is


class TestSecretHygieneGuard:
    def test_keyring_tier_is_off(self):
        assert sp_mod._HAS_KEYRING is False

    def test_env_is_sanitized(self):
        assert os.environ.get("PUFFPAI_API_KEY") == _TEST_API_KEY
        assert "HA_TOKEN" not in os.environ
        assert "HA_ACCESS_TOKEN" not in os.environ

    def test_keychain_writes_are_blocked(self):
        keyring = pytest.importorskip("keyring")
        # Throwaway service name: if the guard were broken this would create a
        # junk entry under a service nothing uses — never touching the real
        # "puffpai" entries — and the test failure would flag the regression.
        with pytest.raises(RuntimeError, match="blocked by tests/conftest.py"):
            keyring.set_password("puffpai-test-guard-probe", "PROBE", "x")

    def test_api_server_key_resolves_synthetic(self):
        # Same firewall-mocked import recipe as test_ha_confirmation_tickets.
        mock_fw = MagicMock()
        mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
        with patched_modules(
                {"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
            if "puffpai.api_server" in sys.modules:
                mod = importlib.reload(sys.modules["puffpai.api_server"])
            else:
                mod = importlib.import_module("puffpai.api_server")
        # Leak-proof on purpose: if the guard regressed, the value on the left
        # is the machine's REAL key and must not be rendered by the failure.
        _assert_secret_is(mod._API_KEY, _TEST_API_KEY,
                          "api_server _API_KEY under the session guard")


# --------------------------------------------------------------------------
# Static scanner: credential reads that execute at import time.
# Pure AST — scanned files are never imported, exec'd, or resolved; only
# their own text is read. Nothing here can produce a secret VALUE, so plain
# asserts (not tests/_secret_asserts) are correct for the failure output.
# --------------------------------------------------------------------------

# A string-literal path is sensitive if it names a credential outright, or is
# a *config*.json (config\home_assistant_config.json — the historical leak).
_SENSITIVE_PATH_WORDS = ("token", "secret", "credential", "api_key", "apikey")

# Bare "key" is deliberately absent: too many innocent dict lookups.
_SENSITIVE_SUBSCRIPT_KEYS = frozenset({
    "token", "access_token", "api_key", "apikey", "secret", "client_secret",
    "password", "hf_token", "ha_token",
})

_PATH_READ_METHODS = frozenset({"read_text", "read_bytes"})
_KEYRING_READS = frozenset({"get_password", "get_credential"})


def _main_guard_kind(test: ast.expr):
    """Return 'eq'/'ne' for a `__name__ == / != "__main__"` test, else None."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    left, right = test.left, test.comparators[0]

    def _is_dunder(n):
        return isinstance(n, ast.Name) and n.id == "__name__"

    def _is_main(n):
        return isinstance(n, ast.Constant) and n.value == "__main__"

    if not ((_is_dunder(left) and _is_main(right))
            or (_is_main(left) and _is_dunder(right))):
        return None
    op = test.ops[0]
    if isinstance(op, ast.Eq):
        return "eq"
    if isinstance(op, ast.NotEq):
        return "ne"
    return None


def _module_level_statements(tree: ast.Module) -> list:
    """Statements that run on import.

    Descends into module-level if/for/while/with/try/match blocks, skipping
    the arm guarded by `if __name__ == "__main__"`. Does NOT descend into
    FunctionDef/AsyncFunctionDef bodies (they run on call, not import), nor
    into ClassDef bodies — class bodies *do* execute at import, but they are
    excluded deliberately to match the agreed scope of this check.
    """
    out: list = []
    _try_star = getattr(ast, "TryStar", ())

    def _collect(body):
        for stmt in body:
            out.append(stmt)
            if isinstance(stmt, ast.If):
                kind = _main_guard_kind(stmt.test)
                if kind == "eq":
                    _collect(stmt.orelse)          # the else of a main guard
                elif kind == "ne":
                    _collect(stmt.body)            # the non-main arm
                else:
                    _collect(stmt.body)
                    _collect(stmt.orelse)
            elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                _collect(stmt.body)
                _collect(stmt.orelse)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                _collect(stmt.body)
            elif isinstance(stmt, (ast.Try,) + ((_try_star,) if _try_star else ())):
                _collect(stmt.body)
                for handler in stmt.handlers:
                    _collect(handler.body)
                _collect(stmt.orelse)
                _collect(stmt.finalbody)
            elif isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    _collect(case.body)

    _collect(tree.body)
    return out


def _own_nodes(stmt: ast.stmt):
    """Yield the expression nodes a statement evaluates itself.

    Stops at nested statements (those are collected separately, or are
    deliberately out of scope) and at lambda bodies. Decorators and argument
    defaults on a nested def therefore still count as import-time code,
    because they are expressions hanging off the def statement.
    """
    def _walk(node, root):
        if not root:
            if isinstance(node, ast.stmt):
                return
            yield node
            if isinstance(node, ast.Lambda):
                defaults = list(node.args.defaults) + [
                    d for d in node.args.kw_defaults if d is not None]
                for d in defaults:
                    yield from _walk(d, False)
                return
        for child in ast.iter_child_nodes(node):
            yield from _walk(child, False)

    yield from _walk(stmt, True)


def _literal_str(node) -> str | None:
    """Static value of a string literal, or None if it is not resolvable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # Match on the literal fragments only; interpolations are unknowable.
        parts = [v.value for v in node.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        return "".join(parts) if parts else None
    return None


def _is_sensitive_path(text: str) -> bool:
    low = text.lower()
    if any(word in low for word in _SENSITIVE_PATH_WORDS):
        return True
    basename = low.replace("\\", "/").rsplit("/", 1)[-1]
    return "config" in basename and basename.endswith(".json")


def _dotted_name(node) -> str | None:
    """Dotted source name of a Name/Attribute chain, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_path_call(node) -> bool:
    """True for `Path(...)` / `pathlib.Path(...)`."""
    if not isinstance(node, ast.Call):
        return False
    name = _dotted_name(node.func)
    return bool(name) and name.split(".")[-1] == "Path"


def _read_path_argument(call: ast.Call):
    """Path-literal argument of a file read, or None if this is not one."""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "open" and call.args:
        return call.args[0]
    if isinstance(func, ast.Attribute) and func.attr in _PATH_READ_METHODS:
        # Only a Path(...) receiver: `resp.read_text()` is not a file read.
        if _is_path_call(func.value) and func.value.args:
            return func.value.args[0]
    return None


def _segment(source: str, node: ast.AST) -> str:
    try:
        text = ast.get_source_segment(source, node)
    except Exception:
        text = None
    if not text:
        try:
            text = ast.unparse(node)
        except Exception:
            text = f"<{type(node).__name__}>"
    text = " ".join(text.split())
    return text if len(text) <= 160 else text[:157] + "..."


def _scan_source(source: str, label: str) -> list:
    """Return `(rule, message)` findings for import-time credential reads.

    Rules: "path-literal" (3a), "sensitive-key" (3b), "keyring" (3c).
    """
    tree = ast.parse(source, filename=label)
    findings = []

    def _add(rule, node, why):
        findings.append(
            (rule, f"{label}:{node.lineno}: {why}: {_segment(source, node)}"))

    for stmt in _module_level_statements(tree):
        for node in _own_nodes(stmt):
            if isinstance(node, ast.Call):
                arg = _read_path_argument(node)
                if arg is not None:
                    literal = _literal_str(arg)
                    # Non-literal paths cannot be resolved statically; the
                    # subscript rule is the backstop for those.
                    if literal is not None and _is_sensitive_path(literal):
                        _add("path-literal", node,
                             "import-time read of a credential-ish path")
                name = _dotted_name(node.func)
                if (name and name.split(".")[-1] in _KEYRING_READS
                        and "keyring" in name.split(".")):
                    _add("keyring", node,
                         "import-time keyring credential lookup")
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                key = _literal_str(node.slice)
                if key is not None and key.lower() in _SENSITIVE_SUBSCRIPT_KEYS:
                    _add("sensitive-key", node,
                         f"import-time read of sensitive key {key!r}")
    return findings


def _scan_repo_tree() -> tuple:
    """Scan every tests/ and scripts/ .py file. Returns (findings, n_files)."""
    # Repo root from this file's own location — never from CWD.
    root = Path(__file__).resolve().parent.parent
    findings, n_files = [], 0
    for directory in ("tests", "scripts"):
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            n_files += 1
            rel = path.relative_to(root).as_posix()
            findings.extend(_scan_source(path.read_text(encoding="utf-8"), rel))
    return findings, n_files


class TestNoModuleLevelCredentialReads:
    """Pins: importing anything under tests/ or scripts/ reads no credential."""

    def test_repo_tree_is_clean(self):
        findings, n_files = _scan_repo_tree()
        # Guards against a path-resolution bug that scans nothing and passes.
        assert n_files >= 10, f"scanner found only {n_files} files — bad root?"
        assert not findings, (
            "credential reads at import time (hoist them into a function or "
            "an `if __name__ == \"__main__\":` block):\n"
            + "\n".join(msg for _, msg in sorted(findings)))

    def test_scanner_flags_the_historical_leak(self):
        # Verbatim shape of the old tests/probe_ha_ws.py top level.
        source = (
            "import json\n"
            "from pathlib import Path\n"
            "CFG = json.loads(Path(r\"C:\\x\\config\\home_assistant_config.json\")"
            ".read_text())\n"
            "TOKEN = CFG[\"token\"]\n"
        )
        rules = {rule for rule, _ in _scan_source(source, "probe.py")}
        assert "path-literal" in rules
        assert "sensitive-key" in rules

    def test_scanner_flags_module_level_open(self):
        findings = _scan_source('open("my_secret_key.txt").read()\n', "s.py")
        assert [r for r, _ in findings] == ["path-literal"]

    def test_scanner_ignores_reads_inside_a_function(self):
        source = (
            "import json\n"
            "from pathlib import Path\n"
            "def main():\n"
            "    cfg = json.loads(Path(\"fake_config.json\").read_text())\n"
            "    token = cfg[\"token\"]\n"
            "    open(\"fake_secret.txt\").read()\n"
            "    return token\n"
        )
        assert _scan_source(source, "s.py") == []

    def test_scanner_ignores_reads_under_the_main_guard(self):
        for guard in ('__name__ == "__main__"', '"__main__" == __name__'):
            source = (
                "import json\n"
                "from pathlib import Path\n"
                f"if {guard}:\n"
                "    cfg = json.loads(Path(\"fake_config.json\").read_text())\n"
                "    token = cfg[\"token\"]\n"
            )
            assert _scan_source(source, "s.py") == [], guard

    def test_scanner_ignores_innocent_subscripts(self):
        assert _scan_source('d = {}\nurl = d["url"]\n', "s.py") == []
