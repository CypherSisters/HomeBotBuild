"""Load ``homebot.env`` — the file the installers mint — into the process env.

WHY THIS EXISTS
---------------
``install.sh`` / ``install.ps1`` mint ``<state>/homebot.env`` holding
``PUFFPAI_API_KEY=<64 hex>`` at 0600, print where it is, and then close with
``python start_api.py``. Nothing loaded that file. On Linux the systemd unit
the installer writes carries ``EnvironmentFile=-<state>/homebot.env``, so the
service path worked; the CLOSING INSTRUCTION and the Windows scheduled-task
recipe did not, because ``api_server`` resolves the API key at IMPORT time and
a miss is fatal by design. A fresh install therefore crashed on the very
command the installer told the owner to run, with an error naming
``/etc/puffpai/api.env`` on a box that has no such file.

So: the entrypoints call :func:`load_state_env` BEFORE importing
``puffpai.api_server``.

RULES
-----
* **Never override.** A variable already present in ``os.environ`` wins. The
  systemd unit, a Docker ``--env``, an operator's ``export`` and the scheduled
  task's user environment all outrank a file on disk, and a loader that
  clobbered them would make "I set PUFFPAI_API_BIND" untrue.
* **One file, chosen the same way state is.** ``$PUFFPAI_DATA_DIR`` if set,
  else the per-user data directory (:func:`puffpai.platform_paths.user_data_dir`)
  — i.e. exactly where the installer put it.
* **No value is ever logged.** Only the path, and the NAMES that were set.
* **Not fatal.** An absent or unreadable file is normal (dev checkout, keyring
  install, container with real env vars). The API-key resolver downstream is
  the thing that fails closed.

python-dotenv is a base dependency (``requirements.txt``: ``python-dotenv>=1.0.0``)
and is used when importable, so quoting/escaping matches what every other
dotenv consumer on the box does. The fallback parser below exists because a
missing optional import must not be the reason an appliance cannot start.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

logger = logging.getLogger(__name__)

#: The filename the installers mint. Kept here so the loader and the error
#: message that names it cannot drift.
ENV_FILENAME = "homebot.env"


def state_env_path() -> str:
    """Absolute path of the env file this install would load.

    ``$PUFFPAI_DATA_DIR/homebot.env`` when that is set, else
    ``<per-user data dir>/homebot.env``. Returned whether or not it exists:
    callers use it in the "looked here, found nothing" message.
    """
    from puffpai.platform_paths import data_dir
    return os.path.join(data_dir(), ENV_FILENAME)


def _parse(text: str) -> dict[str, str]:
    """Minimal ``KEY=value`` parser: the python-dotenv-less fallback.

    Handles ``#`` comments, blank lines, a leading ``export``, and one layer of
    matching single/double quotes. Deliberately does NOT do variable expansion:
    a ``$`` in a generated API key must survive verbatim.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name] = value
    return out


def _read(path: str) -> dict[str, str]:
    try:
        from dotenv import dotenv_values  # type: ignore
    except Exception:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return _parse(handle.read())
        except OSError as exc:
            logger.debug("env file %s unreadable: %s", path, exc)
            return {}
    try:
        return {k: v for k, v in dotenv_values(path).items() if v is not None}
    except Exception as exc:  # a malformed file must not stop the boot
        logger.warning("env file %s could not be parsed: %s", path, exc)
        return {}


def load_env_file(path: str) -> list[str]:
    """Set every variable from *path* that is not already in the environment.

    Returns the sorted NAMES that were set (never values). An absent file
    returns ``[]``.

    The names are also recorded with
    :func:`puffpai.security.secrets_provider.note_env_file_keys`, which is how
    the secrets chain knows a credential was provided DELIBERATELY by this
    install rather than left over in some other install's OS keychain.
    """
    if not path or not os.path.isfile(path):
        return []
    values = _read(path)
    applied: list[str] = []
    for name, value in values.items():
        if name in os.environ:
            continue
        os.environ[name] = value
        applied.append(name)
    if applied:
        logger.info("loaded %d variable(s) from %s: %s",
                    len(applied), path, ", ".join(sorted(applied)))
    note_env_file_keys(applied)
    return sorted(applied)


def note_env_file_keys(names: Iterable[str]) -> None:
    """Tell the secrets chain which names came from the env file.

    Isolated here (and never fatal) so the loader has no hard dependency on
    the security package's import graph.
    """
    names = [str(n) for n in names if n]
    if not names:
        return
    try:
        from puffpai.security import secrets_provider
        secrets_provider.note_env_file_keys(names)
    except Exception as exc:  # pragma: no cover - import-order safety
        logger.debug("could not record env-file provenance: %s", exc)


def load_state_env() -> tuple[str, list[str]]:
    """Load the install's ``homebot.env``. Returns ``(path, names_set)``.

    Call this BEFORE importing :mod:`puffpai.api_server`.
    """
    path = state_env_path()
    return path, load_env_file(path)
