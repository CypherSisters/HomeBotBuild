"""Scoped sys.modules patching that restores only the keys it touched.

``unittest.mock.patch.dict(sys.modules, ...)`` snapshots the whole module
table on entry and does ``clear() + update(snapshot)`` on exit, which evicts
every module first imported while the patch was active.  A fixture that
imports ``puffpai.api_server`` under such a patch therefore tears the module
and its transitive imports out of ``sys.modules`` on teardown; the next test
file to import them gets fresh duplicate module objects, and the suite fails
in ways that depend on file order.

``patched_modules`` swaps in only the named entries and, on exit, restores
exactly those keys.  Modules imported while the patch was active stay
imported, matching normal Python import semantics.
"""

import contextlib
import sys

_MISSING = object()


@contextlib.contextmanager
def patched_modules(replacements):
    saved = {name: sys.modules.get(name, _MISSING) for name in replacements}
    sys.modules.update(replacements)
    try:
        yield
    finally:
        for name, original in saved.items():
            if original is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
