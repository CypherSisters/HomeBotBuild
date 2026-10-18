"""``puffpai start`` and ``puffpai stop``: one command opens your bot.

The closing screen used to end with homework: ``puffpai serve`` in one window,
``cd dashboard; npm install; npm run build; npm run start`` in another, an
address, and a separate command for the pairing code. The installer's "Start
<name>.cmd" already does all of that for an installed build; this is the same
thing for everybody else, as one command:

* starts the bot and its web page together, on this computer only
  (127.0.0.1), with the web page's usage reports switched off
  (``NEXT_TELEMETRY_DISABLED=1``);
* gets the web page ready first when it has to (its parts on first run, a
  fresh build when the source is newer than the last one);
* waits until both answer, then opens the browser, and prints the address and
  the pairing step in plain words, the code's end in this computer's time;
* stays in the window. Ctrl+C stops both. ``puffpai stop`` in another window
  stops exactly what ``start`` started, by the process numbers it wrote down
  (``<state>/running.json``, the same record the installer's Stop uses).

Nothing here goes to the internet. The only connections it opens are the two
"are you up yet?" checks, to 127.0.0.1.

Every program it starts comes by full path from
:mod:`puffpai.security.host_programs`: ``node`` is the one the
installer wrote down, npm is npm's own ``npm-cli.js`` run on that node, and
``taskkill`` / ``tasklist`` come from the Windows system folder. Never a bare
name, never ``shutil.which``. The web page's own children find that same node
first on their ``PATH``, and on Windows never in their working folder.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import subprocess

from puffpai import procflags
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from puffpai.security import host_programs

HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 3003
BACKEND_WAIT_S = 180
DASHBOARD_WAIT_S = 120
RUN_FILE = "running.json"


# ---------------------------------------------------------------------------
# where things are
# ---------------------------------------------------------------------------

def dashboard_dir() -> Optional[Path]:
    """This copy's web page folder, or None when it has none."""
    try:
        from puffpai import cli
        launchers = cli.installed_launchers()
    except Exception:
        launchers = None
    candidates = []
    if launchers and launchers.get("root"):
        candidates.append(Path(launchers["root"]) / "dashboard")
    candidates.append(Path(__file__).resolve().parents[2] / "dashboard")
    for path in candidates:
        if (path / "package.json").is_file():
            return path
    return None


def state_dir() -> Path:
    from puffpai.config import get_state_dir
    return Path(get_state_dir())


def run_file() -> Path:
    return state_dir() / RUN_FILE


def logs_dir() -> Path:
    path = state_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ports(args: argparse.Namespace) -> tuple[int, int]:
    """The bot's port and the web page's port: flag, then setting, then default."""
    backend = getattr(args, "port", None)
    if not backend:
        env = os.environ.get("PUFFPAI_PORT", "").strip()
        if env.isdigit():
            backend = int(env)
    if not backend:
        from puffpai.config import get_config
        backend = int(get_config().api_server.port)
    dash = getattr(args, "dashboard_port", None)
    if not dash:
        env = os.environ.get("PUFFPAI_DASHBOARD_PORT", "").strip()
        dash = int(env) if env.isdigit() else DEFAULT_DASHBOARD_PORT
    return int(backend), int(dash)


def page_url() -> str:
    """The web page address ``puffpai start`` opens, with no flags given."""
    env = os.environ.get("PUFFPAI_DASHBOARD_PORT", "").strip()
    port = int(env) if env.isdigit() else DEFAULT_DASHBOARD_PORT
    return f"http://{HOST}:{port}/"


# ---------------------------------------------------------------------------
# is it up?
# ---------------------------------------------------------------------------

def answers(url: str, timeout: float = 2.0) -> bool:
    """True when *url* on this computer answers 200. Loopback only."""
    import urllib.request
    if not url.startswith(f"http://{HOST}:"):
        return False
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # lan-egress: no proxy, loopback only
        with opener.open(url, timeout=timeout) as resp:  # lan-egress: loopback "is it up yet" check of the bot this command started, 127.0.0.1 only
            return resp.status == 200
    except Exception:
        return False


def alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        tasklist = host_programs.path("tasklist")
        if not tasklist:
            return False
        out = subprocess.run([tasklist, "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                             capture_output=True, text=True, check=False,
                             **host_programs.run_kwargs(), **procflags.hidden())
        return f'"{pid}"' in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_pid(pid: Any) -> bool:
    """Stop one process and its children, by number. True when it was running."""
    if not alive(pid):
        return False
    pid = int(pid)
    if os.name == "nt":
        taskkill = host_programs.path("taskkill")
        if taskkill:
            subprocess.run([taskkill, "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False,
                           **host_programs.run_kwargs(), **procflags.hidden())
        else:
            # No taskkill in the system folder: stop the process itself
            # (Windows' own TerminateProcess), without its children.
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    else:
        # The whole group only when the process leads its own group (start
        # makes one per child); otherwise the process alone, so this command
        # never signals its own group (procflags.own_group).
        group = procflags.own_group(pid)
        stopped = False
        if group is not None:
            try:
                os.killpg(group, signal.SIGTERM)
                stopped = True
            except OSError:
                stopped = False
        if not stopped:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    return True


def wait_up(label: str, url: str, proc: Optional[subprocess.Popen], seconds: float,
            log: Path, *, say: Callable[[str], None] = print,
            clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep,
            check: Callable[[str], bool] = answers) -> bool:
    say(f"  Starting {label}...")
    until = clock() + seconds
    while clock() < until:
        if check(url):
            say(f"  {label[:1].upper() + label[1:]} is ready.")
            return True
        if proc is not None and proc.poll() is not None:
            break
        sleep(1.0)
    say(f"  {label[:1].upper() + label[1:]} did not start. The last lines of {log}:")
    try:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
    except OSError:
        tail = []
    for line in tail:
        say(f"    {line}")
    return False


# ---------------------------------------------------------------------------
# the web page's parts and build
# ---------------------------------------------------------------------------

def _node() -> Optional[str]:
    """The node program BuildBot trusts, by full path (or None)."""
    return host_programs.path("node")


def _npm_cli(node: str) -> Optional[str]:
    """npm's own npm-cli.js next to *node*, by full path (or None)."""
    return host_programs.npm_cli(node)


def node_env(env: dict[str, str], node: str) -> dict[str, str]:
    """*env* for a node child: the trusted node's folder first on ``PATH``
    (so a script that runs ``node`` or ``npm`` by name gets that same node
    and its npm, never one planted earlier on ``PATH``), and on Windows the
    working folder is never searched for a program."""
    out = {k: v for k, v in env.items() if k.upper() != "PATH"}
    parts = [os.path.dirname(node)]
    for key, value in env.items():
        if key.upper() == "PATH" and value:
            parts.extend(p for p in value.split(os.pathsep) if p and p not in parts)
    out["PATH"] = os.pathsep.join(parts)
    if os.name == "nt":
        out["NoDefaultCurrentDirectoryInExePath"] = "1"
    return out


def web_page_ready(dash: Path, env: dict[str, str], *, say: Callable[[str], None] = print,
                   run: Callable[..., Any] = subprocess.run) -> bool:
    """Make sure the web page has its parts and a fresh build. False on failure."""
    node = _node()
    if not node:
        say("  Your bot's web page needs Node.js (version 22.12 or newer), and "
            "HomeBot has not found it.")
        say("  Get it from https://nodejs.org, then run HomeBot's setup again so "
            "it notes where Node.js is, then run puffpai start again.")
        return False
    env = node_env(env, node)
    log_dir = logs_dir()
    if not (dash / "node_modules" / "next").exists():
        npm_cli = _npm_cli(node)
        if not npm_cli:
            say("  Node.js is here but its npm part is missing. Reinstall Node.js "
                "from https://nodejs.org, then run puffpai start again.")
            return False
        say("  Getting the web page's parts. This happens once and takes a few "
            "minutes.")
        with open(log_dir / "web-page-parts.log", "w", encoding="utf-8") as fh:
            done = run([node, npm_cli, "ci", "--no-audit", "--no-fund"], cwd=str(dash),
                       env=env, stdout=fh, stderr=subprocess.STDOUT, check=False, **procflags.hidden())
        if getattr(done, "returncode", 1) != 0:
            say(f"  The web page's parts could not be fetched. Details are in "
                f"{log_dir / 'web-page-parts.log'}")
            return False
    say("  Checking the web page is up to date (if it rebuilds, that takes a few "
        "minutes).")
    with open(log_dir / "web-page-check.log", "w", encoding="utf-8") as fh:
        run([node, str(dash / "scripts" / "ensure-fresh-build.js")], cwd=str(dash),
            env=env, stdout=fh, stderr=subprocess.STDOUT, check=False, **procflags.hidden())
    if not (dash / ".next" / "BUILD_ID").is_file():
        say(f"  The web page could not be built. Details are in "
            f"{log_dir / 'web-page-check.log'}")
        return False
    return True


# ---------------------------------------------------------------------------
# the pairing line
# ---------------------------------------------------------------------------

def pairing_line(dash: Optional[Path]) -> str:
    """One plain line: how to get the code a phone or laptop types the first
    time. The code itself is printed by ``puffpai pair``, never into a log."""
    return ("The first time, a phone or laptop asks for a pairing code. Print "
            "it on this computer with: puffpai pair")


# The web page's pairing store, read and written by the same rules as
# dashboard/app/_lib/server/pairing.ts and dashboard/scripts/show-pairing-code.js.
# Keep these in step with CODE_ALPHABET, CODE_LENGTH and PAIR_CODE_TTL_SEC there.
PAIR_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
PAIR_CODE_LENGTH = 8
PAIR_CODE_TTL_S = 60 * 60
_PAIR_SECRET_RE = re.compile(r"^[0-9a-f]{64}\Z")
#: The pairing file is a few hundred bytes; a far larger one is not it.
_PAIR_FILE_MAX = 64 * 1024


class PairingStoreError(Exception):
    """The pairing file is there but cannot be read, used or written."""


def pairing_state_file(dash: Optional[Path]) -> Optional[Path]:
    """Where the web page keeps its pairing state: ``PUFF_PAIRING_STATE``, else
    ``<dashboard>/data/pairing.json`` (the web page runs in its own folder)."""
    override = os.environ.get("PUFF_PAIRING_STATE", "").strip()
    if override:
        return Path(override)
    return dash / "data" / "pairing.json" if dash is not None else None


def _pair_code_ok(code: object) -> bool:
    return (isinstance(code, str) and len(code) == PAIR_CODE_LENGTH
            and all(ch in PAIR_CODE_ALPHABET for ch in code))


def _read_pairing_state(path: Path) -> Optional[dict[str, Any]]:
    """The pairing state, or None when the file does not exist yet.

    Anything else that is wrong raises :class:`PairingStoreError`. A new state
    (and so a new pairing secret) is never made here: that would unpair every
    screen in the house. The web page makes the file the first time it runs.
    """
    try:
        if path.stat().st_size > _PAIR_FILE_MAX:
            raise PairingStoreError("too large")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PairingStoreError(type(exc).__name__) from None
    try:
        state = json.loads(raw)
    except ValueError:
        raise PairingStoreError("not JSON") from None
    if (not isinstance(state, dict)
            or not isinstance(state.get("secret"), str)
            or not _PAIR_SECRET_RE.match(state["secret"])
            or not _pair_code_ok(state.get("code"))):
        raise PairingStoreError("malformed")
    return state


def _pair_rotated_at(state: dict[str, Any]) -> Optional[datetime]:
    raw = state.get("codeRotatedAt")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _iso_ms(moment: datetime) -> str:
    """The time the way the web page writes it (JavaScript toISOString)."""
    return (moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def _write_pairing_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic, owner-only write: a temp file opened 0600 (never over an
    existing one), then a rename. The same discipline as writeState() in
    pairing.ts."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows: the folder's own permissions apply
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise PairingStoreError(type(exc).__name__) from None


def current_pairing_code(path: Path, now: Optional[datetime] = None
                         ) -> Optional[dict[str, Any]]:
    """``{"code", "expires_at", "rotated"}`` for this bot's pairing code, or
    None when the web page has not made its pairing file yet.

    An expired code (or one with no readable rotation time) is replaced first,
    exactly as the web page and its script do, so the printed code works. Only
    the code and its time change; every other field, the pairing secret
    included, is written back as it was, so paired screens stay paired.
    Raises :class:`PairingStoreError` for a file that cannot be used.
    """
    now = now or datetime.now(timezone.utc)
    state = _read_pairing_state(path)
    if state is None:
        return None
    rotated_at = _pair_rotated_at(state)
    rotated = False
    if rotated_at is None or now >= rotated_at + timedelta(seconds=PAIR_CODE_TTL_S):
        code = "".join(PAIR_CODE_ALPHABET[b % len(PAIR_CODE_ALPHABET)]
                       for b in secrets.token_bytes(PAIR_CODE_LENGTH))
        state = {**state, "code": code, "codeRotatedAt": _iso_ms(now)}
        _write_pairing_state(path, state)
        rotated_at, rotated = now, True
    return {"code": state["code"],
            "expires_at": rotated_at + timedelta(seconds=PAIR_CODE_TTL_S),
            "rotated": rotated}


def format_pairing_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def cmd_pair(args: argparse.Namespace) -> int:  # noqa: ARG001
    """``puffpai pair``: this bot's pairing code, and until when it works.

    Read from the web page's own pairing file (:func:`pairing_state_file`),
    the one ``node dashboard/scripts/show-pairing-code.js`` reads, so it needs
    the same thing that script needs: a person at this computer who can read
    that file. An expired code is replaced first, so what is printed works.
    Prints the CODE only, never the pairing secret, and writes nothing to any
    log: it goes to this terminal and nowhere else.
    """
    path = pairing_state_file(dashboard_dir())
    if path is None:
        print("  This copy of your bot has no web page, so it has no pairing "
              "code.")
        return 1
    try:
        info = current_pairing_code(path)
    except PairingStoreError:
        print(f"  I could not read or update the pairing code in {path}.")
        print("  Run puffpai pair as the same user that runs your bot. On a "
              "screen that is already paired, Settings, then Paired devices, "
              "shows it too.")
        return 1
    if info is None:
        print("  Your bot's web page has not made a pairing code yet. Start "
              "your bot with puffpai start, open its web page once, then run "
              "puffpai pair again.")
        return 1
    from puffpai import cli
    print("")
    print("  Pairing code for this bot:")
    print("")
    print(f"      {format_pairing_code(info['code'])}")
    print("")
    if info["rotated"]:
        print("  The last code had run out, so this is a new one.")
    print(f"  It works until {cli._local_clock(info['expires_at'])}, this "
          f"computer's time. After that, run puffpai pair again for a new one.")
    print("  Type it on the pairing screen of your bot's web page. That phone "
          "or laptop then stays paired for a year.")
    print("")
    return 0


# ---------------------------------------------------------------------------
# running record
# ---------------------------------------------------------------------------

def _write_record(record: dict[str, Any]) -> None:
    from puffpai import fsutil
    path = run_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fsutil.atomic_write_json(str(path), record)


def _read_record() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(run_file().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _forget_record() -> None:
    try:
        run_file().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------

def child_env(backend_port: int, dash_port: int, key: str) -> dict[str, str]:
    env = dict(os.environ)
    env["NEXT_TELEMETRY_DISABLED"] = "1"
    env["PUFFPAI_API_KEY"] = key
    env["PUFFPAI_BACKEND_URL"] = f"http://{HOST}:{backend_port}"
    env["PUFFPAI_DASHBOARD_URL"] = f"http://{HOST}:{dash_port}"
    env["PUFFPAI_PORT"] = str(backend_port)
    env["PUFFPAI_DASHBOARD_PORT"] = str(dash_port)
    # The web page keeps its pairing state in THIS build's state folder, so
    # it is told which folder that is, as an absolute path (a relative one
    # would resolve against the web page's own folder).
    env["PUFFPAI_STATE_DIR"] = str(state_dir().resolve())
    # ... and which build that folder is: the web page reads the id from
    # <state>/build_id.json and never works it out itself.
    from puffpai.security import build_identity
    build_identity.record(env["PUFFPAI_STATE_DIR"])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _api_key() -> str:
    from puffpai import api_server
    return api_server._load_or_create_api_key()  # noqa: SLF001 - the server's own resolver


def _spawn(argv: list[str], cwd: Path, env: dict[str, str], log: Path) -> subprocess.Popen:
    kwargs: dict[str, Any] = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    kwargs.update(procflags.hidden())       # no console window for the servers
    fh = open(log, "w", encoding="utf-8")
    return subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=fh,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)


def cmd_start(args: argparse.Namespace) -> int:
    say = print
    dash = dashboard_dir()
    if dash is None:
        say("  This copy of HomeBot has no web page folder, so there is nothing to "
            "open in a browser.")
        say("  You can talk to your bot here instead: puffpai chat")
        return 1
    backend_port, dash_port = ports(args)
    health = f"http://{HOST}:{backend_port}/health"
    page = f"http://{HOST}:{dash_port}/"
    prev = _read_record() or {}

    try:
        key = _api_key()
    except Exception as exc:  # the server's own refusal, in its own words
        say(str(exc))
        return 1
    env = child_env(backend_port, dash_port, key)
    say("")
    say("Opening your bot on this computer.")

    started: list[subprocess.Popen] = []
    record: dict[str, Any] = {"backend_pid": None, "dashboard_pid": None,
                              "backend_port": backend_port, "dashboard_port": dash_port,
                              "by": "puffpai start"}

    def stop_started() -> None:
        for proc in reversed(started):
            stop_pid(proc.pid)
        _forget_record()

    # The bot itself.
    if answers(health):
        if alive(prev.get("backend_pid")) and prev.get("backend_port") == backend_port:
            say("  Your bot is already running.")
            record["backend_pid"] = prev.get("backend_pid")
        else:
            say(f"  Something else on this computer is already using port "
                f"{backend_port}. Stop it, or pick another number: "
                f"puffpai start --port <number>")
            return 1
    else:
        log = logs_dir() / "bot.log"
        proc = _spawn([sys.executable, "-m", "puffpai", "serve", "--host", HOST,
                       "--port", str(backend_port)], Path.cwd(), env, log)
        started.append(proc)
        record["backend_pid"] = proc.pid
        _write_record(record)
        if not wait_up("your bot", health, proc, BACKEND_WAIT_S, log, say=say):
            stop_started()
            return 1

    # The web page.
    if answers(page + "api/health"):
        if alive(prev.get("dashboard_pid")) and prev.get("dashboard_port") == dash_port:
            say("  Your bot's web page is already running.")
            record["dashboard_pid"] = prev.get("dashboard_pid")
        else:
            say(f"  Something else on this computer is already using port "
                f"{dash_port}. Stop it, or pick another number: "
                f"puffpai start --dashboard-port <number>")
            stop_started()
            return 1
    else:
        if not web_page_ready(dash, env, say=say):
            stop_started()
            return 1
        node = _node()
        if not node:  # it was there a moment ago, in web_page_ready
            say("  Node.js was here a moment ago and now HomeBot cannot find it, "
                "so the web page did not open. Nothing else was changed.")
            say("  If Node.js was just updated or removed, install it from "
                "https://nodejs.org, run HomeBot's setup again so it notes where "
                "Node.js is, then run puffpai start again.")
            stop_started()
            return 1
        next_bin = dash / "node_modules" / "next" / "dist" / "bin" / "next"
        log = logs_dir() / "web-page.log"
        proc = _spawn([node, str(next_bin), "start", "-H", HOST, "-p", str(dash_port)],
                      dash, node_env(env, node), log)
        started.append(proc)
        record["dashboard_pid"] = proc.pid
        _write_record(record)
        if not wait_up("your bot's web page", page + "api/health", proc,
                       DASHBOARD_WAIT_S, log, say=say):
            stop_started()
            return 1
    _write_record(record)

    say("")
    say("Your bot is running on this computer.")
    say(f"Its web page: {page}")
    say(pairing_line(dash))
    if getattr(args, "no_browser", False) or os.environ.get("PUFFPAI_NO_BROWSER") == "1":
        say("Open that address in your web browser.")
    else:
        say("Opening your web browser now.")
        try:
            import webbrowser
            webbrowser.open(page)
        except Exception:
            say("The browser did not open. Open that address yourself.")
    if not started:
        return 0
    say("To stop it, press Ctrl+C here, or run puffpai stop in another window.")
    try:
        while all(p.poll() is None for p in started):
            time.sleep(1.0)
        say("")
        if not run_file().exists():
            # `puffpai stop` removes the record before it stops anything.
            stop_started()
            say("Your bot was stopped with puffpai stop.")
            return 0
        say("Part of your bot stopped on its own. Stopping the rest. Details are "
            f"in {logs_dir()}")
        stop_started()
        return 1
    except KeyboardInterrupt:
        say("")
        say("Stopping your bot...")
        stop_started()
        say("Stopped.")
        return 0


def cmd_stop(args: argparse.Namespace) -> int:  # noqa: ARG001
    record = _read_record()
    if not record:
        print("  Your bot was not running.")
        return 0
    # The record goes first, so the window running `puffpai start` can tell
    # this stop from a crash.
    _forget_record()
    any_stopped = False
    for label, key in (("web page", "dashboard_pid"), ("bot", "backend_pid")):
        if stop_pid(record.get(key)):
            print(f"  Stopped your {label}.")
            any_stopped = True
    if not any_stopped:
        print("  Your bot was not running.")
    return 0


def add_parsers(subparsers: Any) -> None:
    sub = subparsers.add_parser(
        "start", help="Start your bot and its web page together, and open it")
    sub.add_argument("--port", type=int, default=None,
                     help="The bot's port (default: your settings, 8000)")
    sub.add_argument("--dashboard-port", dest="dashboard_port", type=int, default=None,
                     help=f"The web page's port (default {DEFAULT_DASHBOARD_PORT})")
    sub.add_argument("--no-browser", dest="no_browser", action="store_true",
                     help="Print the address instead of opening the browser")
    sub.set_defaults(func=cmd_start)
    sub = subparsers.add_parser(
        "stop", help="Stop the bot and web page that puffpai start started")
    sub.set_defaults(func=cmd_stop)
    sub = subparsers.add_parser(
        "pair", help="Show the code a phone or laptop types to use your bot's "
                     "web page, and until when it works")
    sub.set_defaults(func=cmd_pair)


__all__ = ["add_parsers", "answers", "child_env", "cmd_pair", "cmd_start", "cmd_stop",
           "dashboard_dir", "node_env", "page_url", "pairing_line", "ports", "wait_up",
           "web_page_ready"]
