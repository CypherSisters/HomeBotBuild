"""Assertion helpers for values that may be REAL machine-derived secrets.

The problem these solve: a test that exercises the SecretsProvider chain
(vault -> keyring -> env) can resolve a value that is not a test literal at
all. On a dev machine the keyring tier reads Windows Credential Manager
service "puffpai", which legitimately holds the owner's real HA_TOKEN. A
plain ``assert provider.get("HA_TOKEN") == "jwt-abc"`` would print that real
value into pytest's failure output — and from there into CI logs, terminal
scrollback, and pasted bug reports.

Use these helpers instead of a bare ``assert`` whenever the left-hand side
comes out of the secrets chain, even when the tier under test is a stub:
modelling the pattern everywhere is what keeps it from being forgotten on
the one test that later gets pointed at the real chain.

Both helpers below defeat THREE separate pytest render paths, because on a
dev machine the resolved value can be the real keyring HA_TOKEN:
  1. assertion rewriting  -> collapse the comparison to a bool, so the
     rewritten explanation can only ever print "assert False";
  2. funcargs display (--tb=long, pytest's own default; this repo only
     escapes it via `addopts = --tb=short` in pytest.ini) and --showlocals
     -> these read the frame's f_locals directly, so `__tracebackhide__`
     drops the helper frame AND the value is `del`d before the assert;
  3. --full-trace, which ignores __tracebackhide__ -> the `del` is what
     saves us there, since a deleted name is absent from f_locals.
Call sites must pass the chain expression straight in (never bind it to a
test-frame local), or the test frame leaks it under --showlocals.
"""


def _shape(value) -> str:
    """Describe a resolved secret WITHOUT rendering its value."""
    return "None" if value is None else f"a value of length {len(value)}"


def _assert_no_secret(got, what: str) -> None:
    """Assert ``got`` is None without ever echoing it."""
    __tracebackhide__ = True
    ok = got is None
    shape = _shape(got)
    del got  # nothing secret may remain in this frame at assert time
    assert ok, f"{what}: expected None, got {shape}"


def _assert_secret_is(got, expected: str, what: str) -> None:
    """Assert a resolved secret equals a SYNTHETIC expected value.

    Only ``expected`` (a test literal) is ever rendered; whatever actually
    came back is reduced to a shape string and dropped.
    """
    __tracebackhide__ = True
    ok = got == expected
    shape = _shape(got)
    del got
    assert ok, f"{what}: expected {expected!r}, got {shape}"
