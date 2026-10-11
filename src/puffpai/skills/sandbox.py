"""The jail a skill runs in, and the stdio protocol pump that feeds it.

The jail is the boundary. Everything else in this package -- the lint, the
manifest rules, the consent card -- is either a message to the owner or a
tripwire; THIS is the thing that has to hold when a skill turns out to be
hostile, and it is written on that assumption.

**Why this file does not call ``agents.toolsmith._jail_argv``.** That builder
does identity binds only (``--ro-bind src src``), has exactly one writable path,
and terminates its argv at the command. Reaching for a second, non-identity
bind axis (``--ro-bind <skills>/<id> /skill``) so a skill directory could be
mounted somewhere else would have widened the one function whose narrowness is
the appliance's whole exec-isolation argument, for every caller, forever. So the
flags are copied here verbatim and the SHAPE of the problem is changed instead:
the trusted runner and a COPY of the skill are staged INTO the per-run root, so
there is still exactly one bind of one directory and ``/work`` is still the only
writable path. ``tests/test_skills_sandbox.py`` re-runs the toolsmith isolation
assertions against this builder so the two cannot drift.

**Why the staged skill copy is hashed after the run, not just chmod 0444.**
``--unshare-all`` does not include a user namespace remap: inside the jail the
process is the same uid as the service, so a file it owns can be chmod'ed back
and rewritten however the mode bits were set. The mode is a speed bump; the
hash comparison after the run is the control. A staged file whose hash moved
fails the run, disables the skill and is ledgered -- it means the code that ran
was not the code on the consent card.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shutil
import stat
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

#: The sandbox helper. Absolute on purpose: resolving via ``$PATH`` would let a
#: poisoned environment choose the "sandbox" that is supposed to contain it.
#: Same value as ``agents.toolsmith.BWRAP``; pinned equal by a test rather than
#: imported, so this module does not drag the whole agent stack in to run a
#: skill.
SKILL_BWRAP = "/usr/bin/bwrap"

#: Where the per-run root is mounted inside the jail, and the process's cwd.
SKILL_WORKDIR = "/work"

#: Read-only binds that make up the runtime. Identical to
#: ``agents.toolsmith.EXEC_RO_BINDS`` and for the identical reason: no ``/etc``,
#: and a missing source is skipped (``/lib64`` has no arm64 counterpart).
SKILL_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64")

#: The interpreter inside the jail. Comes from the read-only ``/usr`` bind.
SKILL_PYTHON = "python3"

#: Protocol and output ceilings. A line longer than this is a protocol error,
#: not a big message: the host reads it into memory.
MAX_PROTOCOL_LINE = 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024

#: How long the host waits for the next protocol line before it gives up on a
#: skill that has gone quiet mid-request. Bounded separately from the run
#: timeout so a wedged broker round-trip cannot sit on the whole budget.
REQUEST_DEADLINE_S = 15.0


class SandboxUnavailable(RuntimeError):
    """No usable bubblewrap. The capability is lost, never the isolation."""


class ProtocolError(RuntimeError):
    """The jailed process said something the protocol has no meaning for."""


def sandbox_available() -> bool:
    """True when a real jail can be built here. False on Windows, and on any
    box without bubblewrap -- where skills report ``offline`` rather than
    running unsandboxed."""
    return os.path.isfile(SKILL_BWRAP) and os.access(SKILL_BWRAP, os.X_OK)


def _skill_jail_argv(run_root: str) -> list:
    """The bubblewrap argv a skill run happens inside.

    Flags copied verbatim from ``agents.toolsmith._jail_argv``: no network
    namespace, dies with the parent, its own session, no environment, a
    read-only runtime, and ONE writable path (*run_root* at ``/work``). The
    only difference is the command, which is fixed here -- there is no
    caller-supplied command and no caller-supplied bind, because both of those
    would be a way to mount something back in.
    """
    argv = [SKILL_BWRAP,
            "--unshare-all",        # net namespace included: no egress, at all
            "--die-with-parent",    # cannot outlive the API server
            "--new-session",        # no shared tty -> no TIOCSTI injection
            "--clearenv"]
    for src in SKILL_RO_BINDS:
        if os.path.exists(src):
            argv += ["--ro-bind", src, src]
    argv += ["--proc", "/proc",
             "--dev", "/dev",
             "--tmpfs", "/tmp",
             "--bind", run_root, SKILL_WORKDIR,   # the ONLY writable host path
             "--chdir", SKILL_WORKDIR,
             "--setenv", "PATH", "/usr/bin:/bin",
             "--setenv", "HOME", SKILL_WORKDIR,
             "--setenv", "TMPDIR", "/tmp",
             "--", "/bin/sh", "-c",
             f"{SKILL_PYTHON} {SKILL_WORKDIR}/_runner.py"]
    return argv


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage(run_root: str, skill_dir: str, payload: dict) -> dict:
    """Lay out the per-run root. Returns ``{relpath: sha256}`` for the copy.

    Copies the skill's package files -- regular files only, symlinks refused --
    into ``run_root/skill``, writes the trusted runner as ``run_root/_runner.py``
    and the call payload as ``run_root/input.json``, and creates an empty
    ``run_root/data`` scratch directory.

    Phase 1 note: ``data/`` is per-run scratch and is destroyed with the run
    root. Persistent skill data is Phase 2; giving a skill a directory that
    survives its own run is a durability decision, and the restrictive default
    is the one that ships first.
    """
    from puffpai.skills import manifest as _manifest

    os.makedirs(run_root, exist_ok=True)
    staged_dir = os.path.join(run_root, "skill")
    os.makedirs(staged_dir, exist_ok=True)
    os.makedirs(os.path.join(run_root, "data"), exist_ok=True)

    hashes: dict[str, str] = {}
    for name in _manifest.PACKAGE_FILES:
        source = os.path.join(skill_dir, name)
        if os.path.islink(source) or not os.path.isfile(source):
            raise SandboxUnavailable(
                f"{name} in {skill_dir} is not a regular file; refusing to "
                f"stage it.")
        target = os.path.join(staged_dir, name)
        shutil.copyfile(source, target, follow_symlinks=False)
        try:
            os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:  # Windows dev boxes; the hash check is the control
            pass
        hashes[name] = _sha256(target)

    runner_src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "runner.py")
    shutil.copyfile(runner_src, os.path.join(run_root, "_runner.py"))
    with open(os.path.join(run_root, "input.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return hashes


def verify_staged(run_root: str, hashes: dict) -> list:
    """Names of staged files whose bytes moved during the run. Empty is good."""
    staged_dir = os.path.join(run_root, "skill")
    moved: list = []
    for name, expected in (hashes or {}).items():
        path = os.path.join(staged_dir, name)
        try:
            if os.path.islink(path) or _sha256(path) != expected:
                moved.append(name)
        except OSError:
            moved.append(name)
    return moved


def _pump(proc, broker, deadline_at: float, log: list) -> dict:
    """Drive the stdio protocol until the child returns a result.

    A reader thread owns the pipe so a child that writes nothing cannot wedge
    the host on ``readline``; the main loop owns the clock. Anything that is not
    a well-formed request or the final result is a protocol error, and a
    protocol error kills the child rather than being interpreted.
    """
    inbox: "queue.Queue" = queue.Queue()

    def _read():
        try:
            while True:
                line = proc.stdout.readline(MAX_PROTOCOL_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_PROTOCOL_LINE:
                    inbox.put(("oversized", None))
                    break
                inbox.put(("line", line))
        except Exception as exc:  # pragma: no cover - pipe teardown
            inbox.put(("error", str(type(exc).__name__)))
        finally:
            inbox.put(("eof", None))

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()

    while True:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the skill ran out of time")
        try:
            kind, line = inbox.get(timeout=min(remaining, REQUEST_DEADLINE_S))
        except queue.Empty:
            raise TimeoutError("the skill stopped answering") from None
        if kind == "oversized":
            raise ProtocolError("the skill wrote an oversized protocol line")
        if kind == "error":
            raise ProtocolError(f"the protocol pipe failed ({line})")
        if kind == "eof":
            raise ProtocolError("the skill exited without returning a result")

        try:
            message = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            raise ProtocolError("the skill wrote a line that is not JSON") from None
        if not isinstance(message, dict):
            raise ProtocolError("the skill wrote a protocol frame that is not an object")

        kind = message.get("t")
        if kind == "result":
            return message
        if kind == "log":
            text = str(message.get("text", ""))[:500]
            if len(log) < 50:
                log.append(text)
            continue
        if kind == "fetch":
            answer = broker.handle(message)
            proc.stdin.write((json.dumps(answer) + "\n").encode("utf-8"))
            proc.stdin.flush()
            continue
        raise ProtocolError(f"the skill sent an unknown frame {kind!r}")


def execute(skill_dir: str, payload: dict, *, broker, run_root: str,
            timeout_s: float) -> dict:
    """Run one skill call in the jail. Returns the runner's result envelope.

    Never trusts the child: a timeout, a protocol error and a tampered staged
    file all kill the process group and return ``ok: False`` with a reason a
    person can read. The caller decides what that means for the skill's state.
    """
    if not sandbox_available():
        raise SandboxUnavailable(
            f"skills need the sandbox helper ({SKILL_BWRAP}) and it is not "
            f"executable here; refusing to run skill code unsandboxed")

    hashes = stage(run_root, skill_dir, payload)
    argv = _skill_jail_argv(run_root)
    log: list = []

    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True)
    deadline_at = time.monotonic() + max(1.0, float(timeout_s))
    result: dict
    try:
        result = _pump(proc, broker, deadline_at, log)
        ok = True
        error = ""
    except TimeoutError as exc:
        result, ok, error = {}, False, str(exc)
    except ProtocolError as exc:
        result, ok, error = {}, False, f"protocol error: {exc}"
    except OSError as exc:  # pragma: no cover - pipe teardown
        result, ok, error = {}, False, f"sandbox io error: {type(exc).__name__}"
    finally:
        try:
            proc.kill()
        except Exception:  # pragma: no cover
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover
            pass
        try:
            stderr = (proc.stderr.read() or b"")[:MAX_STDERR_BYTES]
        except Exception:  # pragma: no cover
            stderr = b""

    tampered = verify_staged(run_root, hashes)
    if tampered:
        return {"ok": False, "tampered": tampered, "log": log,
                "error": ("the skill's own files changed while it ran, so the "
                          "code that ran is not the code you approved"),
                "stderr": stderr.decode("utf-8", "replace")}
    if not ok:
        return {"ok": False, "error": error, "log": log,
                "stderr": stderr.decode("utf-8", "replace")}
    return {"ok": bool(result.get("ok")), "result": result.get("result"),
            "error": str(result.get("error") or ""), "log": log,
            "requests": int(result.get("requests") or 0),
            "stderr": stderr.decode("utf-8", "replace")}


__all__ = [
    "MAX_PROTOCOL_LINE", "REQUEST_DEADLINE_S", "SKILL_BWRAP", "SKILL_PYTHON",
    "SKILL_RO_BINDS", "SKILL_WORKDIR", "ProtocolError", "SandboxUnavailable",
    "execute", "sandbox_available", "stage", "verify_staged",
]
