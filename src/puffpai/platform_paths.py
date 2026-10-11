"""Per-user default locations for writable state, on any OS.

HomeBotBuild installs on arbitrary Windows and Linux hardware, so nothing may
assume the Founders appliance layout (``/opt/puffpai`` for code,
``/data/puffpai`` for state). Those absolute paths stay correct where they
exist, but they must be a *deployment choice*, never a built-in default: on a
Windows laptop ``/data/puffpai`` is unwritable, and on a stock Linux user
account it does not exist either.

One rule, used everywhere:

    ``$PUFFPAI_DATA_DIR`` when set (this is what :mod:`puffpai.config`'s
    :func:`resolve_state_dir` already honours, and what the installers write
    into the environment file), otherwise the platform's per-user data
    directory:

        Windows   %LOCALAPPDATA%\\PuffPAI   (fallback ~/AppData/Local/PuffPAI)
        Linux     $XDG_DATA_HOME/puffpai    (fallback ~/.local/share/puffpai)

Nothing here creates directories or touches the filesystem beyond expanding
``~``; callers decide when to make a directory, and they already handle the
unwritable case.
"""

from __future__ import annotations

import os
import sys

#: Same env var name :mod:`puffpai.config` uses (``STATE_DIR_ENV``). Imported
#: by string rather than from config to keep this module dependency-free: it is
#: read at import time by low-level modules that must not pull in the config
#: singleton.
DATA_DIR_ENV = "PUFFPAI_DATA_DIR"


def user_data_dir() -> str:
    """The per-user state root for this OS, ignoring ``$PUFFPAI_DATA_DIR``."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if not base:
            base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.normpath(os.path.join(base, "PuffPAI"))
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.normpath(os.path.join(os.path.expanduser(base), "puffpai"))


def data_dir() -> str:
    """``$PUFFPAI_DATA_DIR`` if set, else :func:`user_data_dir`."""
    env = os.environ.get(DATA_DIR_ENV, "").strip()
    if env:
        return os.path.normpath(os.path.expanduser(env))
    return user_data_dir()


def data_path(*parts: str) -> str:
    """A path under :func:`data_dir`, e.g. ``data_path("models", "x.onnx")``."""
    return os.path.normpath(os.path.join(data_dir(), *parts))


__all__ = ["DATA_DIR_ENV", "user_data_dir", "data_dir", "data_path"]
