"""A tripwire over skill source. NOT the security boundary — the jail is.

Stated plainly because a reader who mistakes this for the boundary will
eventually widen it: every check here is bypassable by a determined author
(``getattr(__builtins__, "ex" + "ec")`` defeats the name list in one line). It
exists for two honest reasons:

1. it names, on the consent card, the things the owner would want to see
   flagged before approving code they are about to trust with a network door;
2. it catches the accident — a skill that imports ``os`` because the author
   copied a snippet — early, where the message can say what to do instead.

Everything it looks for is ALREADY impossible at run time: the jail has no
network namespace, no ``/opt``, no ``/data``, no environment, and the only
writable path is the per-run ``/work``.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import ast
import re

#: Modules a skill has no business importing. ``os``/``sys`` are included even
#: though the jail makes them near-useless: a skill that reaches for them is a
#: skill doing something the ``ctx`` contract did not anticipate, which is worth
#: a line on the card.
DENIED_MODULES = frozenset({
    "os", "sys", "socket", "subprocess", "ctypes", "shutil", "pathlib",
    "importlib", "multiprocessing", "threading", "signal", "resource",
    "http", "urllib", "requests", "httpx", "ftplib", "smtplib", "telnetlib",
    "pickle", "marshal", "shelve", "sqlite3", "tempfile", "glob", "pty",
})

#: Builtins that turn data into code, or reach the filesystem directly.
DENIED_NAMES = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "memoryview",
})

#: Attribute chains that are the usual way round a name check.
DENIED_ATTRS = frozenset({
    "__subclasses__", "__globals__", "__builtins__", "__bases__", "__mro__",
    "__code__", "__loader__", "__getattribute__",
})

#: Literals shaped like a credential. Phase 1 skills declare no secrets, so a
#: key-shaped constant in the source is either a hard-coded credential (which
#: the owner must see) or dead weight.
_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|passwd|password|token|bearer)\b\s*[=:]"
    r"|\bsk-[A-Za-z0-9]{16,}\b"
    r"|\bghp_[A-Za-z0-9]{20,}\b")


def lint_source(source: str) -> list[str]:
    """Problems found in *source*, worst first. Empty list means nothing seen.

    Never raises: a file that will not parse is itself the finding.
    """
    problems: list[str] = []
    text = source if isinstance(source, str) else ""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"skill.py does not parse as Python: {exc.msg} (line {exc.lineno})"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in DENIED_MODULES:
                    problems.append(
                        f"line {node.lineno}: imports '{alias.name}'. A skill "
                        f"reaches the world only through ctx; the jail has no "
                        f"network, no host filesystem and no environment.")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in DENIED_MODULES:
                problems.append(
                    f"line {node.lineno}: imports from '{node.module}'. A skill "
                    f"reaches the world only through ctx.")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in DENIED_NAMES:
                problems.append(
                    f"line {node.lineno}: uses '{node.id}'. Turning data into "
                    f"code (or opening a path) is not part of the skill "
                    f"contract.")
        elif isinstance(node, ast.Attribute):
            if node.attr in DENIED_ATTRS:
                problems.append(
                    f"line {node.lineno}: touches '{node.attr}', which is how "
                    f"a name check gets walked around.")

    for match in _SECRET_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        problems.append(
            f"line {line}: a credential-shaped literal. Phase 1 skills hold no "
            f"secrets — if this needs a key, it cannot be installed yet.")

    seen: list[str] = []
    for problem in problems:
        if problem not in seen:
            seen.append(problem)
    return seen


def has_run_entrypoint(source: str) -> bool:
    """True when the source defines a module-level ``def run(inputs, ctx)``."""
    try:
        tree = ast.parse(source if isinstance(source, str) else "")
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run":
            args = [a.arg for a in node.args.args]
            return len(args) >= 2
    return False


__all__ = ["DENIED_ATTRS", "DENIED_MODULES", "DENIED_NAMES",
           "has_run_entrypoint", "lint_source"]
