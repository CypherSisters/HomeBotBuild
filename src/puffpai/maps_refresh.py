"""Monthly offline-map data refresh.

Downloads new Texas map data through ONE Bridge connection, "Map data
updates" (egress door ``map_data_updates``), which ships closed and which the
owner switches on. Then it hands the files to the map engines' own scripts,
``deploy/maps/build_release.sh`` and ``activate_release.sh``, which build a new
release beside the live one and swap it in only after it passes its checks.

Entry points (``deploy/maps/refresh.sh`` sets the environment for them)::

    python -m puffpai.maps_refresh run       # nightly (cron); acts only when due
    python -m puffpai.maps_refresh run-now   # one run, in the background, now
    python -m puffpai.maps_refresh status    # the last result, as JSON

What it promises:

* **Nothing leaves while the switch is off.** The switch is read before any
  socket, the job records ``OFF`` and stops. Every request then goes through
  :func:`puffpai.security.egress_doors.egress_request`, which checks the switch
  again, allows only ``download.geofabrik.de`` and
  ``download1.graphhopper.com``, re-checks every redirect, and writes the
  content-free ledger line. The switch is re-read every 64 MB during a
  download, so turning it off stops a download that is already running.
* **Nothing about the household goes with it.** GET and HEAD for two public
  files and their ``.md5``; the only header is ``User-Agent: PuffPAI-maps/1.0``
  (plus ``Range`` when resuming). No cookie, credential or identifier.
* **Directions are never left broken.** This module never touches
  ``current``. A failure anywhere leaves the live maps as they were; the build
  and activation scripts own that guarantee and are never bypassed.
* **Disk stays bounded.** At most one download folder exists. Files are
  streamed to disk, checked against size bounds and the published md5, and
  deleted once a build has passed. A failed build keeps them for ONE retry.

Copyright (c) 2025 Cipher Sisters. MIT License.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from puffpai.security import egress_doors

#: The Bridge connection every request here goes through.
DOOR_ID = "map_data_updates"
#: What the owner sees it called (the dashboard de-snakes the id to the same).
CONNECTION_NAME = "Map data updates"
USER_AGENT = "PuffPAI-maps/1.0"
OFF_REASON = "Map data updates is switched off."

MB = 1024 * 1024
GB = 1024 * MB


@dataclass(frozen=True)
class Source:
    """One upstream file, with the size a real copy of it must fall within."""

    key: str
    label: str
    url: str
    min_bytes: int
    max_bytes: int

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def md5_url(self) -> str:
        return self.url + ".md5"


#: Typical sizes: about 724 MB and 5.1 GB. The bounds are generous on both
#: sides; outside them the file is not the one we expect (an error page, a
#: truncated copy, or the wrong region).
SOURCES: tuple[Source, ...] = (
    Source("roads", "road map",
           "https://download.geofabrik.de/north-america/us/texas-latest.osm.pbf",
           300 * MB, 3 * GB),
    Source("places", "place list",
           "https://download1.graphhopper.com/public/north-america/usa/"
           "photon-dump-usa-1.0-latest.jsonl.zst",
           2 * GB, 15 * GB),
)

MD5_MAX_BYTES = 4096
CHUNK_BYTES = 1 * MB
#: Re-read the switch this often during a download.
RECHECK_BYTES = 64 * MB
#: (connect, read) seconds. The read timeout applies per chunk, not per file.
REQUEST_TIMEOUT = (20, 120)
#: Downloads (about 6 GB) plus build_release.sh's own 20 GB floor, plus margin.
MIN_FREE_BYTES = 32 * GB
#: A road file published within this long after the live data's timestamp is
#: the same cut (or a day newer); a monthly refresh does not rebuild for it.
SAME_DATA_SLACK = timedelta(days=2)
BUILD_TIMEOUT_S = 6 * 3600
ACTIVATE_TIMEOUT_S = 30 * 60
#: How long a script gets to clean up after SIGTERM before it is killed.
TERM_GRACE_S = 300
#: A kept download folder is reused for this many builds, then fetched fresh.
MAX_BUILD_ATTEMPTS = 2
LOG_KEEP = 12
#: The job runs nightly but only does anything when an update is DUE: no
#: completed check (OK, or SKIPPED after asking upstream) in this long.
REFRESH_EVERY = timedelta(days=28)
#: After this many FAILED nights in a row it stops retrying nightly and waits
#: until REFRESH_EVERY after the first failure of the streak.
MAX_FAILED_NIGHTS = 5

#: Where the API service's environment lives, so this job sees a switch the
#: deployer forced from the unit (see :func:`_adopt_service_env_override`).
SERVICE_UNIT = "puffpai-api.service"
SERVICE_ENV_FILE = "/etc/puffpai/api.env"
#: The env file a per-user install (install.sh) writes in its data dir.
USER_ENV_FILE_NAME = "homebot.env"

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STAGE_RE = re.compile(r"^\d{8}-\d{6}$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class RefreshError(Exception):
    """A failure, carrying the plain sentence the status file shows."""


class SwitchedOff(RefreshError):
    """The owner closed the connection while the job was running."""


# ── paths ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Paths:
    root: str
    deploy: str

    @classmethod
    def from_env(cls) -> "Paths":
        from puffpai.maps_directions import maps_root
        root = maps_root()
        deploy = os.environ.get("PUFFPAI_MAPS_DEPLOY_DIR")
        if not deploy:
            # src/puffpai/maps_refresh.py -> <repo>/deploy/maps
            here = os.path.dirname(os.path.abspath(__file__))
            deploy = os.path.join(os.path.dirname(os.path.dirname(here)),
                                  "deploy", "maps")
        return cls(root=root, deploy=deploy)

    @property
    def downloads(self) -> str:
        return os.environ.get("PUFFPAI_MAPS_DOWNLOADS") or os.path.join(
            self.root, "downloads")

    @property
    def status_file(self) -> str:
        return os.path.join(self.root, "refresh.status")

    @property
    def state_file(self) -> str:
        return os.path.join(self.root, "refresh.state.json")

    @property
    def logs(self) -> str:
        return os.path.join(self.root, "logs")

    @property
    def lock_file(self) -> str:
        return os.path.join(self.root, ".refresh.lock")

    @property
    def current_meta(self) -> str:
        return os.path.join(self.root, "current", "META.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text) -> datetime | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        # Python 3.10's fromisoformat does not take a trailing "Z".
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json(path: str, payload: dict) -> None:
    """Atomic write, owner and group readable only."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def read_current_meta(paths: Paths) -> dict:
    """The live release's META.json, or ``{}`` when no maps are installed."""
    return _read_json(paths.current_meta)


# ── the switch ───────────────────────────────────────────────────────────────

class ServiceSettingsUnreadable(RefreshError):
    """The API's env file is missing or unreadable: the job treats the
    connection as forced shut. Never consent."""


#: Where the job reads the API unit's env file. The box default is the real
#: file; a dev checkout or a test points PUFFPAI_MAPS_SERVICE_ENV_FILE at its
#: own file EXPLICITLY. A missing file is never read as "open".
SERVICE_ENV_FILE_VAR = "PUFFPAI_MAPS_SERVICE_ENV_FILE"


def service_env_file() -> str:
    return os.environ.get(SERVICE_ENV_FILE_VAR) or SERVICE_ENV_FILE


def service_env_files() -> list:
    """Every env file the API service on this machine may read.

    An explicit :data:`SERVICE_ENV_FILE_VAR` is the only one. Otherwise the
    appliance file, and the per-user install's file in ``$PUFFPAI_DATA_DIR``
    when that is set: an install has one of the two, and reading both means a
    force-shut in either one is seen.
    """
    explicit = os.environ.get(SERVICE_ENV_FILE_VAR)
    if explicit:
        return [explicit]
    files = [SERVICE_ENV_FILE]
    data_dir = (os.environ.get("PUFFPAI_DATA_DIR") or "").strip()
    if data_dir:
        files.append(os.path.join(data_dir, USER_ENV_FILE_NAME))
    return files


def _unreadable_reason(path: str) -> str:
    return ("Map data updates is treated as switched off because this job "
            f"can't read the box's service settings ({path}). Nothing was "
            "downloaded.")


def _missing_reason(path: str) -> str:
    return ("Map data updates is treated as switched off because the box's "
            f"service settings file ({path}) is missing. Nothing was "
            "downloaded.")


_BOM = "\ufeff"


def _parse_env_assignment(line: str, var: str) -> str | None:
    """The value *line* assigns to *var*, or None if it assigns something else.

    Accepts the shapes a person writes in an env file: a byte-order mark,
    leading whitespace, an ``export `` prefix, spaces around ``=``, single or
    double quotes, a CRLF ending. The key must equal *var* exactly, so
    ``<var>_X=1`` never matches.
    """
    text = line.replace(_BOM, "").strip()
    if text.startswith("export ") or text.startswith("export\t"):
        text = text[len("export"):].lstrip()
    key, sep, value = text.partition("=")
    if not sep or key.strip() != var:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value.strip()


def _env_file_value(var: str, path: str) -> str | None:
    """*var* from the env file at *path*; the last assignment wins.

    Only the one key is kept: every other line (the file holds the API key and
    other secrets) is discarded beyond the key comparison and is never logged.
    Read as utf-8-sig, and each line is BOM-stripped as well, so an editor's
    byte-order mark cannot hide a force-shut line. A MISSING or unreadable file
    raises :class:`ServiceSettingsUnreadable`: the job treats either as the
    switch forced shut.
    """
    value: str | None = None
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                found = _parse_env_assignment(line, var)
                if found is not None:
                    value = found
    except FileNotFoundError as exc:
        raise ServiceSettingsUnreadable(_missing_reason(path)) from exc
    except OSError as exc:
        raise ServiceSettingsUnreadable(_unreadable_reason(path)) from exc
    return value


def _unit_env_value(var: str) -> str | None:
    """*var* from the unit's ``Environment=`` lines (systemctl), if set."""
    try:
        out = subprocess.run(
            ["systemctl", "show", SERVICE_UNIT, "-p", "Environment",
             "--value"],
            capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        words = shlex.split(out)
    except ValueError:
        words = out.split()
    value: str | None = None
    for word in words:
        found = _parse_env_assignment(word, var)
        if found is not None:
            value = found
    return value


def _service_env_value(var: str) -> str | None:
    """*var* as the API service would see it, or None if the service sets none.

    systemd lets ``EnvironmentFile=`` override ``Environment=``, so the file
    is consulted last and wins. Raises :class:`ServiceSettingsUnreadable` when
    the env file is missing or cannot be read. Runs on every platform: there
    is no "not the box, so skip it" path that could read as open.
    """
    value = _unit_env_value(var)
    files = service_env_files()
    found = []
    readable = 0
    for path in files:
        try:
            from_file = _env_file_value(var, path)
        except ServiceSettingsUnreadable as exc:
            # One of several candidates being absent is normal; an existing
            # file that can't be read, or no file at all, is not.
            if len(files) > 1 and isinstance(exc.__cause__, FileNotFoundError):
                continue
            raise
        readable += 1
        if from_file is not None:
            found.append(from_file)
    if not readable:
        raise ServiceSettingsUnreadable(_missing_reason(" or ".join(files)))
    if not found:
        return value
    # Two files that disagree: anything but an "open" value wins.
    for from_file in found:
        if from_file.strip().lower() not in ("1", "true", "yes", "on"):
            return from_file
    return found[-1]


def _adopt_service_env_override() -> None:
    """Make this job read the switch exactly as the API service does.

    The owner's switch lives in the overlay file both processes read. A
    deployer can also FORCE it from the API unit's environment, and that wins
    over the overlay in the API, so the dashboard shows the forced state. This
    job runs from cron, outside that unit, so without this it could download
    while the screen says the connection is forced shut.
    """
    d = egress_doors.door(DOOR_ID)
    if d is None or d.env_var in os.environ:
        return
    value = _service_env_value(d.env_var)
    if value is not None:
        os.environ[d.env_var] = value


def switch_open(*, job: bool = False) -> bool:
    """Is "Map data updates" open right now? Opens no socket.

    ``job=True`` (the cron job) forgets the cached config and overlay first,
    so a switch the owner flipped in the API a moment ago is seen, and adopts
    the API unit's forced value if it has one.
    """
    if job:
        egress_doors.reset_cache()
        _adopt_service_env_override()
    allowed, _reason = egress_doors.egress_allowed(DOOR_ID)
    return bool(allowed)


def _switch_open_or_shut() -> bool:
    """:func:`switch_open` for the job, where unreadable settings mean shut."""
    try:
        return switch_open(job=True)
    except ServiceSettingsUnreadable:
        return False


# ── status ───────────────────────────────────────────────────────────────────

def read_status(paths: Paths | None = None) -> dict:
    return _read_json((paths or Paths.from_env()).status_file)


def _write_status(paths: Paths, result: str, reason: str, *,
                  now: datetime | None = None,
                  data_date: str | None = None,
                  release: str | None = None,
                  checked: bool = False,
                  reset_streak: bool = False) -> dict:
    """Write the status file, carrying the schedule bookkeeping forward.

    ``checked`` marks a run that completed an upstream check (OK, or SKIPPED
    because nothing newer exists): it starts the next 28-day window and ends
    any failure streak. A FAILED run extends the streak; at
    :data:`MAX_FAILED_NIGHTS` the reason says when it will try again.
    """
    previous = read_status(paths)
    when = now or _now()
    stamp = _iso(when)
    streak = 0 if reset_streak else int(previous.get("fail_streak") or 0)
    started = None if reset_streak else previous.get("fail_streak_started")
    if result == "FAILED":
        streak += 1
        started = started or stamp
        if streak >= MAX_FAILED_NIGHTS:
            first = _parse_iso(started) or when
            reason = (f"{reason} It has failed {streak} nights in a row, so it "
                      f"will wait and try again after "
                      f"{_iso(first + REFRESH_EVERY)[:10]}.")
    elif checked:
        streak, started = 0, None
    payload = {
        "last_run": stamp,
        "result": result,
        "reason": reason,
        "data_date": data_date,
        "release": release,
        "last_ok": stamp if result == "OK" else previous.get("last_ok"),
        "last_checked": stamp if checked else previous.get("last_checked"),
        "fail_streak": streak,
        "fail_streak_started": started,
    }
    if result == "RUNNING":
        payload["pid"] = os.getpid()
    _write_json(paths.status_file, payload)
    return payload


def next_due(st: dict) -> datetime | None:
    """When the nightly job will next do anything; None means tonight."""
    streak = int(st.get("fail_streak") or 0)
    if streak >= MAX_FAILED_NIGHTS:
        first = _parse_iso(st.get("fail_streak_started"))
        if first is not None:
            return first + REFRESH_EVERY
    checked = _parse_iso(st.get("last_checked"))
    if checked is not None and streak == 0:
        return checked + REFRESH_EVERY
    return None


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not sys.platform.startswith("linux"):
        return True  # cannot tell cheaply and safely; trust the file
    return os.path.exists(f"/proc/{pid}")


def status(paths: Paths | None = None) -> dict:
    """Read-only refresh status for Puff and the screen. Opens no socket."""
    paths = paths or Paths.from_env()
    st = read_status(paths)
    meta = read_current_meta(paths)
    try:
        open_now = switch_open()
    except Exception:  # a status read must never raise
        open_now = False
    result = st.get("result") or "NEVER"
    reason = st.get("reason") or "Map data has not been updated by this box yet."
    data_date = meta.get("data_date") if isinstance(meta.get("data_date"),
                                                    str) else None
    if result == "RUNNING" and not _pid_alive(st.get("pid")):
        result = "FAILED"
        reason = ("The last update stopped before it finished. "
                  + _still_using(data_date))
    if data_date:
        summary = f"Map data from {data_date}."
    else:
        summary = "No offline map data is installed yet."
    if not open_now:
        summary += " " + OFF_REASON
    elif result == "RUNNING":
        summary += " An update is running now."
    elif st.get("last_run"):
        summary += f" Last update check {str(st['last_run'])[:10]}: {reason}"
    return {
        "connection": {"id": DOOR_ID, "name": CONNECTION_NAME,
                       "open": open_now},
        "last_run": st.get("last_run"),
        "result": result,
        "reason": reason,
        "last_ok": st.get("last_ok"),
        "next_check": (_iso(next_due(st)) if next_due(st) else None),
        "data_date": data_date,
        "release": meta.get("release") if isinstance(meta.get("release"),
                                                     str) else None,
        "summary": summary,
    }


def _still_using(data_date: str | None) -> str:
    if data_date:
        return f"Directions still use the map data from {data_date}."
    return "Nothing changed."


# ── network: only ever through the Bridge connection ─────────────────────────

def _headers(extra: dict | None = None) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if extra:
        headers.update(extra)
    return headers


def _close(resp) -> None:
    for obj in (resp, getattr(resp, "_puffpai_egress_session", None)):
        closer = getattr(obj, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass


def _request(method: str, url: str, **kwargs):
    """The ONE outbound call in this module: through the door, or refused."""
    return egress_doors.egress_request(DOOR_ID, method, url,
                                       timeout=REQUEST_TIMEOUT, **kwargs)


def fetch_md5(source: Source, send=_request) -> str:
    """The md5 published beside *source* on the same host."""
    resp = send("GET", source.md5_url, headers=_headers(), stream=True)
    try:
        if resp.status_code != 200:
            raise RefreshError(
                f"The map site answered {resp.status_code} for the "
                f"{source.label} checksum, so nothing was downloaded.")
        body = b""
        for chunk in resp.iter_content(1024):
            body += chunk or b""
            if len(body) > MD5_MAX_BYTES:
                raise RefreshError(f"The {source.label} checksum file was "
                                   f"not what it should be.")
    finally:
        _close(resp)
    words = body.decode("ascii", "replace").split()
    digest = words[0].lower() if words else ""
    if not _MD5_RE.match(digest):
        raise RefreshError(f"The {source.label} checksum file was not what it "
                           f"should be.")
    return digest


def upstream_last_modified(source: Source, send=_request) -> datetime | None:
    resp = send("HEAD", source.url, headers=_headers())
    try:
        if resp.status_code != 200:
            return None
        raw = resp.headers.get("Last-Modified")
    finally:
        _close(resp)
    if not raw:
        return None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _new_md5():
    # Integrity against the md5 the host publishes, not a security boundary
    # (the transport is https to a declared host); FIPS builds need the flag.
    return hashlib.md5(usedforsecurity=False)


def _file_md5(path: str):
    hasher = _new_md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK_BYTES), b""):
            hasher.update(block)
    return hasher


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def download(source: Source, stage_dir: str, expected_md5: str, *,
             send=_request, log=None) -> str:
    """Stream *source* into *stage_dir*, resuming a partial copy. Returns the path.

    A ``.part`` file left by an interrupted run is resumed with ``Range``; if
    the server will not resume it, the copy restarts from zero. The finished
    file must fall within the size bounds and match *expected_md5*, or it is
    deleted and the run fails.
    """
    final = os.path.join(stage_dir, source.filename)
    part = final + ".part"
    for _attempt in range(2):
        offset = os.path.getsize(part) if os.path.exists(part) else 0
        if offset > source.max_bytes:
            _remove(part)
            offset = 0
        extra = {"Range": f"bytes={offset}-"} if offset else None
        resp = send("GET", source.url, headers=_headers(extra), stream=True)
        try:
            total: int | None = None
            if resp.status_code == 206 and offset:
                m = _CONTENT_RANGE_RE.match(
                    str(resp.headers.get("Content-Range") or "").strip())
                if not m or int(m.group(1)) != offset:
                    _remove(part)  # cannot line the pieces up: start over
                    continue
                total = int(m.group(3))
                mode = "ab"
            elif resp.status_code == 416 and offset:
                _remove(part)
                continue
            elif resp.status_code == 200:
                offset = 0
                mode = "wb"
                try:
                    total = int(resp.headers.get("Content-Length"))
                except (TypeError, ValueError):
                    total = None
            else:
                raise RefreshError(
                    f"The map site answered {resp.status_code} for the "
                    f"{source.label} file, so nothing changed.")
            if total is not None and not (source.min_bytes <= total
                                          <= source.max_bytes):
                _remove(part)
                raise RefreshError(
                    f"The {source.label} file is an unexpected size "
                    f"({total // MB} MB), so it was not downloaded.")
            if log:
                log(f"{source.key}: downloading"
                    + (f", resuming at {offset // MB} MB" if offset else "")
                    + (f", {total // MB} MB" if total else ""))
            hasher = _file_md5(part) if mode == "ab" else _new_md5()
            written = offset
            next_check = written + RECHECK_BYTES
            with open(part, mode) as fh:
                for chunk in resp.iter_content(CHUNK_BYTES):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > source.max_bytes:
                        fh.close()
                        _remove(part)
                        raise RefreshError(
                            f"The {source.label} file grew past "
                            f"{source.max_bytes // MB} MB, so it was "
                            f"stopped and deleted.")
                    fh.write(chunk)
                    hasher.update(chunk)
                    if written >= next_check:
                        next_check += RECHECK_BYTES
                        if not switch_open(job=True):
                            raise SwitchedOff(
                                "Map data updates was switched off during "
                                "the download, so it stopped.")
        finally:
            _close(resp)
        if total is not None and written != total:
            # Keep the partial copy: the next run resumes it.
            raise RefreshError(f"The {source.label} download ended early. It "
                               f"will pick up where it stopped next time.")
        if written < source.min_bytes:
            _remove(part)
            raise RefreshError(f"The {source.label} file was too small to be "
                               f"real map data, so it was deleted.")
        if hasher.hexdigest() != expected_md5:
            _remove(part)
            raise RefreshError(f"The {source.label} file did not match its "
                               f"published checksum, so it was deleted and "
                               f"nothing was built.")
        os.replace(part, final)
        if log:
            log(f"{source.key}: {written // MB} MB, checksum OK")
        return final
    raise RefreshError(f"The {source.label} download could not be resumed. "
                       f"It will start over next time.")


# ── the download folder ──────────────────────────────────────────────────────

STAGE_MANIFEST = "stage.json"


def _stage_dirs(paths: Paths) -> list[str]:
    try:
        names = sorted(os.listdir(paths.downloads))
    except OSError:
        return []
    return [os.path.join(paths.downloads, n) for n in names
            if _STAGE_RE.match(n) and os.path.isdir(
                os.path.join(paths.downloads, n))]


def prepare_stage(paths: Paths, md5s: dict, now: datetime) -> tuple[str, dict]:
    """The download folder for this run, and its manifest.

    Reuses the newest kept folder when it holds the SAME upstream files (same
    md5s) and has not used up its retry; deletes every other folder, so at most
    one download's worth of disk is ever held.
    """
    keep: str | None = None
    manifest: dict = {}
    for d in reversed(_stage_dirs(paths)):
        m = _read_json(os.path.join(d, STAGE_MANIFEST))
        if (keep is None and m.get("md5") == md5s
                and int(m.get("build_attempts") or 0) < MAX_BUILD_ATTEMPTS):
            keep, manifest = d, m
    for d in _stage_dirs(paths):
        if d != keep:
            shutil.rmtree(d, ignore_errors=True)
    if keep is None:
        keep = os.path.join(paths.downloads, now.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(keep, exist_ok=True)
        manifest = {"md5": md5s, "build_attempts": 0, "verified": {},
                    "created": _iso(now)}
        _write_json(os.path.join(keep, STAGE_MANIFEST), manifest)
    return keep, manifest


# ── the build and activation scripts ─────────────────────────────────────────

def _run_script(argv: list, *, env: dict, timeout: int, log_fh) -> int:
    """Run one deploy script to completion. On a timeout, SIGTERM first so the
    script's own traps clean up (build) or roll back (activation)."""
    if not argv or argv[0] != "bash":
        raise RefreshError("Refused to run anything but the map scripts.")
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                            stdout=log_fh, stderr=subprocess.STDOUT)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return 124


def _status_words(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.readline().split()
    except OSError:
        return []


# ── the job ──────────────────────────────────────────────────────────────────

class _Log:
    def __init__(self, paths: Paths, now: datetime):
        os.makedirs(paths.logs, exist_ok=True)
        self.path = os.path.join(paths.logs,
                                 f"refresh-{now.strftime('%Y%m%d-%H%M%S')}.log")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        self.fh = os.fdopen(fd, "a", encoding="utf-8")
        self._prune(paths.logs)

    @staticmethod
    def _prune(logs_dir: str) -> None:
        try:
            names = sorted(n for n in os.listdir(logs_dir)
                           if n.startswith("refresh-") and n.endswith(".log"))
        except OSError:
            return
        for name in names[:-LOG_KEEP]:
            _remove(os.path.join(logs_dir, name))

    def __call__(self, message: str) -> None:
        self.fh.write(f"{_iso(_now())} {message}\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


def _acquire_lock(path: str):
    """An exclusive, non-blocking lock, or None if another run holds it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    try:
        import fcntl
    except ImportError:  # not Linux: a dev checkout, never the box
        return fh
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def run(paths: Paths | None = None, *, send=_request,
        run_script=_run_script, now: datetime | None = None,
        force: bool = False) -> dict:
    """One nightly pass. Does nothing at all unless an update is due.

    Returns the status written, or a dict with ``"quiet": True`` (not due, or
    another run holds the lock) when nothing was written. ``force`` (run-now)
    skips the due check, never the switch.
    """
    paths = paths or Paths.from_env()
    now = now or _now()
    os.makedirs(paths.root, exist_ok=True)
    lock = _acquire_lock(paths.lock_file)
    if lock is None:
        return {"result": "BUSY", "reason": "An update is already running.",
                "quiet": True}
    try:
        previous = read_status(paths)
        due_at = next_due(previous)
        if not force and due_at is not None and now < due_at:
            # Local files only: no switch read, no socket, no status write.
            return {"result": "SKIPPED", "quiet": True,
                    "reason": f"Not due until {_iso(due_at)[:10]}."}
        reset = int(previous.get("fail_streak") or 0) >= MAX_FAILED_NIGHTS
        live = read_current_meta(paths)
        live_date = live.get("data_date") if isinstance(
            live.get("data_date"), str) else None
        live_release = live.get("release") if isinstance(
            live.get("release"), str) else None
        # Decided before ANY socket, DNS lookup included.
        try:
            open_now = switch_open(job=True)
        except ServiceSettingsUnreadable as exc:
            return _write_status(paths, "FAILED", str(exc), now=now,
                                 data_date=live_date, release=live_release,
                                 reset_streak=reset)
        if not open_now:
            return _write_status(paths, "OFF", OFF_REASON, now=now,
                                 data_date=live_date, release=live_release)
        _write_status(paths, "RUNNING", "Checking for newer map data.",
                      now=now, data_date=live_date, release=live_release,
                      reset_streak=reset)
        log = _Log(paths, now)
        try:
            result, reason, date, release = _refresh(
                paths, live, send=send, run_script=run_script, now=now,
                log=log)
        except SwitchedOff as exc:
            result, reason = "OFF", f"{exc} {_still_using(live_date)}"
            date, release = live_date, live_release
        except RefreshError as exc:
            result, reason = "FAILED", str(exc)
            date, release = live_date, live_release
        except egress_doors.EgressBlocked as exc:
            if not _switch_open_or_shut():
                result, reason = "OFF", f"{OFF_REASON} {_still_using(live_date)}"
            else:
                result = "FAILED"
                reason = ("A download tried to reach somewhere other than "
                          "the two map sites, so it was stopped. "
                          + _still_using(live_date))
            log(f"refused by the Bridge: {exc.reason}")
            date, release = live_date, live_release
        except Exception as exc:  # noqa: BLE001 - the status must be written
            # TYPE only: a transport error's message can quote a full URL.
            result = "FAILED"
            reason = (f"Could not finish the update ({type(exc).__name__}). "
                      + _still_using(live_date))
            date, release = live_date, live_release
        log(f"result {result}: {reason}")
        log.close()
        return _write_status(paths, result, reason, now=now, data_date=date,
                             release=release,
                             checked=result in ("OK", "SKIPPED"))
    finally:
        lock.close()


def _refresh(paths: Paths, live: dict, *, send, run_script, now: datetime,
             log) -> tuple[str, str, str | None, str | None]:
    live_date = live.get("data_date") if isinstance(live.get("data_date"),
                                                    str) else None
    live_release = live.get("release") if isinstance(live.get("release"),
                                                     str) else None
    still = _still_using(live_date)

    # 1. Room on disk, before anything is fetched.
    os.makedirs(paths.downloads, exist_ok=True)
    free = shutil.disk_usage(paths.downloads).free
    if free < MIN_FREE_BYTES:
        raise RefreshError(
            f"Not enough free disk space for new maps (about "
            f"{MIN_FREE_BYTES // GB} GB is needed, {free // GB} GB is free). "
            + still)

    # 2. Is there anything newer? Two tiny checksum files, then one HEAD.
    md5s = {s.key: fetch_md5(s, send=send) for s in SOURCES}
    state = _read_json(paths.state_file)
    if live and state.get("built_md5") == md5s:
        log("upstream checksums match the last build")
        return ("SKIPPED", "Already have the newest map data.", live_date,
                live_release)
    live_ts = _parse_iso(live.get("data_timestamp"))
    if live_ts is not None:
        upstream = upstream_last_modified(SOURCES[0], send=send)
        log(f"upstream road map last modified {upstream and _iso(upstream)}; "
            f"live data {_iso(live_ts)}")
        if upstream is not None and upstream <= live_ts + SAME_DATA_SLACK:
            return ("SKIPPED",
                    f"No newer map data yet. Directions use the map data "
                    f"from {live_date or _iso(live_ts)[:10]}.",
                    live_date, live_release)

    # 3. Download into the one staging folder.
    stage, manifest = prepare_stage(paths, md5s, now)
    files: dict[str, str] = {}
    for s in SOURCES:
        final = os.path.join(stage, s.filename)
        if manifest.get("verified", {}).get(s.key) == md5s[s.key] \
                and os.path.isfile(final):
            log(f"{s.key}: reusing the copy kept from the last attempt")
        else:
            final = download(s, stage, md5s[s.key], send=send, log=log)
            manifest.setdefault("verified", {})[s.key] = md5s[s.key]
            _write_json(os.path.join(stage, STAGE_MANIFEST), manifest)
        files[s.key] = final

    # 4. Build beside the live maps. The script never touches `current`.
    build = os.path.join(paths.deploy, "build_release.sh")
    activate = os.path.join(paths.deploy, "activate_release.sh")
    if not (os.path.isfile(build) and os.path.isfile(activate)):
        raise RefreshError("The map build scripts are not installed on this "
                           "box. " + still)
    env = dict(os.environ, MAPS_ROOT=paths.root)
    log("building a new release")
    rc = run_script(["bash", build, files["roads"], files["places"]],
                    env=env, timeout=BUILD_TIMEOUT_S, log_fh=log.fh)
    words = _status_words(os.path.join(paths.root, "build.status"))
    release = words[2] if len(words) >= 3 else ""
    built = (rc == 0 and words[:1] == ["READY"] and _RELEASE_RE.match(release)
             and release != live_release and os.path.isfile(
                 os.path.join(paths.root, "releases", release, "READY")))
    if not built:
        manifest["build_attempts"] = int(manifest.get("build_attempts") or 0) + 1
        _write_json(os.path.join(stage, STAGE_MANIFEST), manifest)
        # A script that died before writing its status (lock busy) leaves the
        # LAST run's line behind; never quote a stale READY as this result.
        state_word = (words[0] if words and words[0] != "READY"
                      else f"exit {rc}")
        log(f"build did not pass (exit {rc}, {state_word}); downloads kept")
        raise RefreshError(
            f"The new map data could not be built ({state_word}), so nothing "
            f"changed. " + still)

    # 5. The build passed: the downloads have done their job.
    shutil.rmtree(stage, ignore_errors=True)
    log(f"release {release} built; downloads removed; activating")

    # 6. Swap it in. The script checks it and rolls back by itself.
    rc = run_script(["bash", activate, release], env=env,
                    timeout=ACTIVATE_TIMEOUT_S, log_fh=log.fh)
    words = _status_words(os.path.join(paths.root, "activate.status"))
    outcome = words[0] if words else "no result"
    if rc != 0 or outcome != "OK":
        log(f"activation did not finish (exit {rc}, {outcome})")
        if outcome == "ROLLED_BACK":
            raise RefreshError("The new maps failed their final check, so the "
                               "box went back to the last good map data. "
                               + still)
        if outcome == "ROLLBACK_FAILED":
            raise RefreshError("The new maps failed their final check and the "
                               "old ones did not come back. Directions are "
                               "down until the maps are restored by hand.")
        raise RefreshError(f"The new maps could not be switched on "
                           f"({outcome}). " + still)

    new = read_current_meta(paths)
    new_date = new.get("data_date") if isinstance(new.get("data_date"),
                                                  str) else None
    _write_json(paths.state_file, {"built_md5": md5s, "release": release,
                                   "built_at": _iso(_now())})
    return ("OK", f"Map data updated to {new_date or 'the newest data'}.",
            new_date, release)


# ── command line ─────────────────────────────────────────────────────────────

def _run_now(paths: Paths) -> int:
    try:
        open_now = switch_open(job=True)
    except ServiceSettingsUnreadable as exc:
        print(str(exc))
        return 3
    if not open_now:
        print(f"{OFF_REASON} Turn it on in Settings > Egress first; nothing "
              f"was downloaded.")
        return 3
    os.makedirs(paths.root, exist_ok=True)
    subprocess.Popen(  # noqa: S603 - our own module, fixed argv
        [sys.executable, "-m", "puffpai.maps_refresh", "run", "--now"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    print("Map data update started in the background. Check it with: "
          "refresh.sh status")
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m puffpai.maps_refresh",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("run", "run-now", "status"))
    # Internal: the background child run-now starts (skips the due check).
    parser.add_argument("--now", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    paths = Paths.from_env()
    if args.command == "status":
        print(json.dumps(status(paths), indent=2))
        return 0
    if args.command == "run-now":
        return _run_now(paths)
    result = run(paths, force=args.now)
    if result.get("quiet") or (result.get("result") == "OFF"
                               and result.get("reason") == OFF_REASON):
        return 0  # nightly: silent unless something actually happened
    print(f"{result.get('result')}: {result.get('reason')}")
    return 1 if result.get("result") == "FAILED" else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
