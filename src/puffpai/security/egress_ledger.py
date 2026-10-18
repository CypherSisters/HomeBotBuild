"""The egress ledger — an append-only record of every door decision.

Why this exists
---------------
``security/egress_doors.py`` keeps a *last-used snapshot*: one entry per
door, overwritten on every decision. That answers "when did this door last
fire?" and nothing else. It is not an audit trail: four refusals in a row leave
one line, and a refusal followed by a success leaves no trace of the refusal at
all. An owner who wants to know *whether the appliance ever tried to phone home*
needs a record to read.

This module is the durable half. Every decision — allowed or refused — appends
one JSON object to a size-capped, rotated JSONL file beside the other security
logs. The snapshot stays, because ``/system/status`` wants an O(1) answer.

What is deliberately NOT in a record
-----------------------------------
* **No query strings.** A URL's query carries data — a search term, a place
  name, coordinates. :func:`safe_parts` splits a URL into host and path and
  throws the query, the fragment and any userinfo away before the record is
  built. There is no code path here that accepts a whole URL.
* **No bodies, no headers, no tokens.** ``bytes_out`` is a *length*, never
  content.
* **No household text.** ``detail`` is a flag-resolution reason or a method and
  a hostname, capped at :data:`_MAX_FIELD` characters.

A security log that quietly records the thing it is protecting is worse than no
log, because it is a second copy in a file nobody thinks of as sensitive.

Size, and the disk-flood rule
-----------------------------
The appliance already caps runaway logs (``puffpai-logcap.timer``, every five
minutes, truncating ``/var/log`` files over 500 MB). That guard covers
``/var/log`` only, and it is a truncation, not a rotation — it is a last resort,
not a design. A ledger on the data partition must bound itself: this one rotates
at :data:`MAX_BYTES` and keeps :data:`BACKUP_COUNT` generations, so the whole
trail can never exceed ``MAX_BYTES * (BACKUP_COUNT + 1)`` — 16 MB today. A door
being hammered costs the oldest generation, never the partition.

``dashboard_widgets`` writes its own ``widget_egress.jsonl`` through
:func:`append` for exactly this reason: uncapped, that file grows without
limit in the same directory, with nothing watching it.

Failure policy
--------------
Writing the ledger must never break a request: an unwritable path is reported
once (:func:`puffpai.logging_config.log_unwritable_once`) and then degrades
quietly. That is the opposite of ``security/audit_log.py``, which fails closed —
deliberately. The audit log is the record of *privileged actions taken*; this is
the record of *network decisions made*, and the decision itself is already
enforced by ``egress_doors`` before this module is reached. Losing a line here
loses evidence, not enforcement.

The outside-traffic log
-----------------------
A second, narrower log sits beside the ledger: ``<state>/logs/outside.jsonl``,
created owner-only, read by "What leaves your computer". One line is written
at every exit that reaches outside this computer, refusals included:
``egress_doors.egress_request``, ``egress_doors.build_probe_get``,
``source_fetch`` (per hop), ``catalogs.live.fetch_url`` and
``wizard.netcheck``. A line holds the time, service id, host, method, bytes in
and out, an outcome (``done`` / ``refused`` / ``failed``), a one-word reason
code from a closed list (:data:`REASON_CODES`), the HTTP status class and a
``what`` key from a closed list (:data:`WHAT`). Nothing else.

* **No query string, no fragment, no user:password.** :func:`safe_host` and
  :func:`safe_path` throw them away before a line is built.
* **A path only where the path carries no owner value.** Owner values (a town,
  a topic, a date the owner picked) go in the QUERY, never the path
  (``catalogs.api_seeds`` refuses a seed row that breaks it). Exits that can
  carry an address the owner typed or chose pass ``path=""`` and only the
  host is kept.
* **No key in a path, not even part of one.** :func:`clean_path` checks the
  whole path and every segment for a key BEFORE a long path is cut, drops the
  path if any of them looks like one, and cuts only at a ``/``.

Lines older than :data:`RETENTION_DAYS` are dropped and the file never grows
past :data:`MAX_BYTES`: when it would, the oldest lines go first, rewritten
through a temp file and ``os.replace``. :func:`append` sends a line built by
:func:`build_record` to this log and any other record to the ledger.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from puffpai import fsutil
from puffpai.logging_config import get_logger, log_unwritable_once
from puffpai.statedir import resolve_runtime_dir

logger = get_logger(__name__)

#: Repo root — this file is ``src/puffpai/security/egress_ledger.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: The ledger file, in the security-log directory the rest of the appliance
#: uses (``$PUFFPAI_DATA_DIR/security_logs`` on the appliance).
LEDGER_NAME = "egress.jsonl"

#: Rotate at 4 MB and keep 3 older generations: **16 MB is the hard ceiling**
#: for the whole trail, per ledger file. Sized against the appliance's real
#: traffic rather than a round number — the news widget alone refreshes six
#: feeds on a 5-minute TTL, so a busy appliance writes on the order of 1 MB of
#: ledger a day and this holds roughly a fortnight. A typical ``/data`` volume
#: has terabytes free, so the cap is about bounding a runaway, not about saving
#: space.
MAX_BYTES = 4 * 1024 * 1024
BACKUP_COUNT = 3

#: Hard ceiling on how many records one read may return, so a caller cannot ask
#: the owner UI to page the whole trail into memory in one request.
MAX_LEDGER_READ = 100

#: The directory holds a security trail; it is not for every account on the appliance.
DIR_MODE = 0o750
#: The ledger names hosts the household reached. Owner-readable only.
FILE_MODE = 0o640

#: Cap on any single string field, so one absurd input cannot dominate a
#: rotation generation.
_MAX_FIELD = 240

#: Frames belonging to the egress machinery itself, skipped when naming the
#: caller — the interesting frame is the one that wanted to reach the network.
_OWN_MODULES = (
    "puffpai.security.egress_ledger",
    "puffpai.security.egress_doors",
)

LOG_NAME = "outside.jsonl"
SCHEMA = "puffpai_outside_log/v1"

#: How long a line is kept.
RETENTION_DAYS = 30

#: The outside-traffic log shares :data:`MAX_BYTES` with the ledger: a service
#: hammering the network costs the oldest lines, never the disk.

#: At most this often (seconds) the age-based prune runs on append.
PRUNE_EVERY_S = 3600

#: Reader ceiling, so one request cannot page the whole file into memory.
MAX_READ = 2000

OUTCOMES = ("done", "refused", "failed")

#: The one-word reasons a refusal or failure may carry. Closed on purpose: a
#: free-text reason can hold a host or an address the owner typed.
REASON_CODES = frozenset({
    "", "switched_off", "bot_offline", "not_on_list", "inside_address",
    "cannot_look_up", "peer_moved", "not_encrypted", "redirected_away",
    "too_many_redirects", "sign_in_refused", "not_allowed", "no_answer",
    "bad_answer", "too_big", "robots_rule", "turned_off_here",
    # security.lan_guard: a call marked "home network" dialled the internet,
    # or a proxy that could carry it there.
    "not_home_network", "through_proxy",
    # source_fetch.fetch_small / get_archive: a download that was not
    # the exact version pinned, a host not on the caller's list, too many
    # redirects, a request header outside the short allowlist, a sign-up key
    # asked to go to a host it is not bound to, a key that could not be used.
    "sha_mismatch", "host", "redirects", "header_refused", "key_host",
    "key_unavailable",
    # a home DNS filter blocked the name (pinned_net.filter_block),
    # and the Bridge stopped something the owner had not switched on
    # (security.bridge_guard), or a proxy on this computer it was going
    # through while the Bridge is up.
    "network_filter", "bridge_up", "bridge_proxy",
})

#: What a request was for. Closed vocabulary: the log stores the KEY, the
#: owner's view renders the words. A caller cannot put its own text here.
WHAT: dict[str, str] = {
    "forecast": "asked for the weather forecast",
    "air_quality": "asked for air quality",
    "headlines": "asked for news headlines",
    "web_page": "read a web page",
    "town_lookup": "looked up a town name",
    "card_data": "asked for new data for a live card",
    "card_picture": "fetched a picture for a live card",
    "sample_look": "took one sample look at a service",
    "library_search": "searched the book library",
    "book_lookup": "looked up where to download a book",
    "book_download": "downloaded a book",
    "book_text": "asked for a list of books or the pages of one",
    "robots_rules": "read the site's rules for robots",
    "list_refresh": "updated a list of free services",
    "internet_check": "checked that the internet answers",
    "web_search": "searched the web",  # never the search words
    # finding and adding outside apps, code and pictures
    "code_search": "short search words",
    "picture_search": "short search words",
    "code_download": "a project you picked",
    "picture_download": "pictures you picked",
    "box_install": "code parts while building",
    "box_traffic": "what an added tool sends",
    "app_traffic": "what an added app sends",
    "app_parts": "parts an app loads, saved once",
    "request": "made a request",
    # a tool server the owner added (puffpai.mcp_client)
    "mcp_tools": "asked a tool server which tools it has",
    "mcp_call": "used a tool from a tool server",
    # the skill and tool shelves the bot searches, and the skill files it
    # downloads when the owner says yes.
    "shelf_search": "short search words, or the name of a skill you picked",
    # the owner's own GitHub account (never the token, never a project's files)
    "github_account": "your GitHub account or the list of your projects",
    # Toolsmith's cloud model: an app's README, file list, commands and output
    "toolsmith_install": "Toolsmith install of an app",
}

#: The service ids ``source_fetch.fetch_small`` / ``get_archive``
#: accept, each with the :data:`WHAT` key its lines carry. Closed, and kept in
#: the security package: the keyed hop (``egress_doors.keyed_first_hop``)
#: checks the owner's switch and writes its line from this table, so a caller
#: cannot log under a name of its own choosing or dodge a switch.
FETCH_SERVICES: dict[str, str] = {
    "code_search": "code_search",
    "picture_search": "picture_search",
    "code_download": "code_download",
    "picture_download": "picture_download",
    "app_parts": "app_parts",
    "box_install": "box_install",
    "reading_helper": "request",
    # one switch per shelf (outside_services.SHELF_ROWS).
    "shelf_clawhub": "shelf_search",
    "shelf_skills_sh": "shelf_search",
    "shelf_hermes": "shelf_search",
    "shelf_mcp_registry": "shelf_search",
    # the owner's own GitHub account (puffpai.github_account). The token
    # rides only the keyed first hop; the switch is the "github" row.
    "github": "github_account",
}

_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_:\-]{0,63}$")

#: Service ids the bot's own code makes in a fixed shape, which the key
#: filter would otherwise judge "random-looking" and drop (a whole line lost,
#: fail-open for the log): a locked-box project or an app's outside site
#: (``outside_switches.switch_id_box`` / ``switch_id_app``: 24 hex), and a
#: browser app's own outside traffic (``app:<app id>:web``, the id shape
#: ``integrate.records`` issues). Only these exact shapes skip the key check;
#: they carry a hash or an issued id, never a value someone typed.
_MADE_SERVICE_RE = re.compile(
    r"^(?:(?:app|box)_[0-9a-f]{24}|app:[a-z0-9][a-z0-9-]{0,29}-[a-z2-7]{8}:web)\Z")
_HOST_RE = re.compile(r"^[a-z0-9.\-:\[\]]{1,253}$")
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH",
                      "CONNECT"})
_MAX_PATH = 120

_lock = threading.Lock()
_state: dict[str, Any] = {"last_prune": 0.0, "reported": set()}

#: Test/ops hook. Set to a path and every write, to the ledger and to the
#: outside-traffic log, goes there verbatim. Each reader skips the other's
#: lines, so one override file serves both.
_PATH_OVERRIDE: "str | None" = None

#: The exact keys of a line built by :func:`build_record`. A record with this
#: shape belongs to the outside-traffic log; anything else is a ledger record.
_OUTSIDE_KEYS = frozenset({
    "t", "service", "host", "path", "method", "outcome", "code", "status",
    "bytes_in", "bytes_out", "what",
})


def set_path_override(path: "str | None") -> None:
    """Point the ledger and the outside-traffic log at *path* (tests, and an
    operator moving the trail)."""
    global _PATH_OVERRIDE
    with _lock:
        _PATH_OVERRIDE = path
        _state["last_prune"] = 0.0


def ledger_dir() -> str:
    """The directory the ledger lives in. Resolved lazily, never at import."""
    return resolve_runtime_dir(
        os.path.join(_REPO_ROOT, "security_logs"), env_subdir="security_logs")


def ledger_path() -> str:
    """Absolute path to the active ledger file."""
    if _PATH_OVERRIDE:
        return _PATH_OVERRIDE
    return os.path.join(ledger_dir(), LEDGER_NAME)


def log_path() -> str:
    """``<state>/logs/outside.jsonl``. Resolved on every call, never at import."""
    if _PATH_OVERRIDE:
        return _PATH_OVERRIDE
    from puffpai.config import get_state_dir
    return os.path.join(get_state_dir(), "logs", LOG_NAME)


def _is_outside_line(record: Any) -> bool:
    """True for a line of the outside-traffic log (see :data:`_OUTSIDE_KEYS`)."""
    return isinstance(record, dict) and set(record) == _OUTSIDE_KEYS


# ── sanitising ───────────────────────────────────────────────────────────────

def _clip(value: object) -> str:
    """One field, as a bounded string."""
    return str(value)[:_MAX_FIELD] if value is not None else ""


def safe_parts(url: object) -> "tuple[str, str]":
    """Split a URL into ``(host, path)``, discarding everything that carries data.

    The query string, the fragment and any ``user:pass@`` are dropped here and
    never reach a record. Returns ``("", "")`` for anything unparseable, because
    a ledger line with no host is better than one with a smuggled query in it.
    """
    if not isinstance(url, str) or not url.strip():
        return "", ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "", ""
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        # No host means this never parsed as a URL, so what looks like a path is
        # arbitrary caller text. Record nothing rather than smuggle it in.
        return "", ""
    path = parts.path or ""
    # Belt and braces: urlsplit already removed the query, but a caller may hand
    # us a pre-joined string that never had a scheme and so parsed as all-path.
    for sep in ("?", "#"):
        if sep in path:
            path = path.split(sep, 1)[0]
    return _clip(host), _clip(path)


def safe_url(url: object) -> str:
    """A URL with its query, fragment and credentials removed.

    Kept for the one existing caller whose log schema is a URL field
    (``dashboard_widgets._log_egress``). New code should record host and path
    separately via :func:`safe_parts`.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    host, path = safe_parts(url)
    if not host:
        return ""
    scheme = (parts.scheme or "https").lower()
    return _clip(f"{scheme}://{host}{path}")


def caller_label(extra_skip: "tuple[str, ...] | None" = None) -> str:
    """``module.function`` of the first frame outside the egress machinery.

    Best effort and never raises: an audit line whose caller reads ``unknown``
    is still worth having.
    """
    skip = _OWN_MODULES + tuple(extra_skip or ())
    try:
        frame = sys._getframe(1)
        while frame is not None:
            name = frame.f_globals.get("__name__", "")
            if not name.startswith(skip):
                return _clip(f"{name}.{frame.f_code.co_name}")
            frame = frame.f_back
    except Exception:  # frame introspection must never break a request
        pass
    return "unknown"


def safe_host(url_or_host: Any) -> str:
    """The lower-case host of a URL (or a bare host), or '' if unusable."""
    text = str(url_or_host or "").strip()
    if not text:
        return ""
    if "://" in text:
        try:
            host = urlsplit(text).hostname or ""
        except ValueError:
            return ""
    else:
        host = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        host = host.rsplit("@", 1)[-1]
    host = host.strip().rstrip(".").lower()
    return host if _HOST_RE.match(host) else ""


def safe_path(url: Any) -> str:
    """The path of a URL with query, fragment and credentials dropped, as
    :func:`clean_path` keeps it."""
    text = str(url or "").strip()
    if "://" not in text:
        return ""
    try:
        path = urlsplit(text).path or ""
    except ValueError:
        return ""
    return clean_path(path)


def _path_looks_like_key(path: str) -> bool:
    """True when the whole path, or any one of its segments, looks like a key
    -- as written and with %-escapes undone."""
    texts = [path]
    try:
        plain = unquote(path)
    except Exception:  # fail closed: an undecodable path is not kept
        return True
    if plain != path:
        texts.append(plain)
    for text in texts:
        if _looks_like_key(text):
            return True
        if any(_looks_like_key(seg) for seg in text.split("/") if seg):
            return True
    return False


def clean_path(path: Any) -> str:
    """A path as a log line may keep it, else ''.

    Query, fragment and ``;`` parameters are dropped. Then the key check runs
    on the WHOLE path and on every segment BEFORE anything is cut: a path in
    which any of them looks like a key is not kept at all. Only then is a
    path longer than :data:`_MAX_PATH` cut, and only at a ``/``: the segment
    the limit falls inside is dropped whole, never kept in part (a part of a
    key is too short to look like one, so it would pass any later check).
    """
    text = str(path or "")
    for sep in ("?", "#", ";"):
        text = text.split(sep, 1)[0]
    if not text or not text.startswith("/"):
        return ""
    if _path_looks_like_key(text):
        return ""
    if len(text) > _MAX_PATH:
        # The last '/' inside the limit; the path starts with '/', so >= 0.
        text = text[:text.rfind("/", 0, _MAX_PATH) + 1]
        if _path_looks_like_key(text):
            return ""
    return text


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _status_class(status: Any) -> str:
    number = _int_or_none(status)
    if number is None or not 100 <= number <= 599:
        return ""
    return f"{number // 100}xx"


def _looks_like_key(text: str) -> bool:
    """A service id, host or path that looks like a key or a password is not
    kept (a key some service takes in its address, a name someone chose).
    Fail closed: if the check breaks, it is treated as a key."""
    if not text:
        return False
    try:
        from puffpai import web_search
        return web_search.looks_like_pasted_key(text)
    except Exception:
        return True


def build_record(service: str, *, host: str, outcome: str, method: str = "GET",
                 path: str = "", code: str = "", status: Any = None,
                 bytes_in: Any = None, bytes_out: Any = None,
                 what: str = "request",
                 now: Optional[datetime] = None) -> Optional[dict]:
    """One sanitised line, or None when the inputs are not usable.

    Every field is checked against a shape or a closed list here, so a
    caller that passes something odd produces a smaller line, never a leak.
    """
    sid = str(service or "").strip().lower()
    if not _SERVICE_RE.match(sid) or (
            _looks_like_key(sid) and not _MADE_SERVICE_RE.match(sid)):
        return None
    if outcome not in OUTCOMES:
        return None
    clean_host = safe_host(host)
    if _looks_like_key(clean_host):
        clean_host = ""
    verb = str(method or "GET").strip().upper()
    if verb not in _METHODS:
        verb = "GET"
    kept_path = clean_path(path)
    reason = str(code or "")
    if reason not in REASON_CODES:
        reason = "not_allowed" if outcome == "refused" else "no_answer"
    key = what if what in WHAT else "request"
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "t": stamp.isoformat(timespec="seconds"),
        "service": sid,
        "host": clean_host,
        "path": kept_path,
        "method": verb,
        "outcome": outcome,
        "code": reason,
        "status": _status_class(status),
        "bytes_in": _int_or_none(bytes_in),
        "bytes_out": _int_or_none(bytes_out),
        "what": key,
    }


# ── writing ──────────────────────────────────────────────────────────────────

def _ensure_dir(path: str) -> None:
    """Create the ledger directory if it is missing, tightly.

    ``mode=`` on :func:`os.makedirs` is masked by the process umask (systemd's
    default 0022 would still give 0750 here, but a looser umask would not), so
    the mode is re-applied explicitly — and only on a directory this call
    created. An existing directory's permissions are somebody else's decision
    and are left alone.
    """
    directory = os.path.dirname(path)
    if not directory or os.path.isdir(directory):
        return
    os.makedirs(directory, mode=DIR_MODE, exist_ok=True)
    try:
        os.chmod(directory, DIR_MODE)
    except OSError:
        pass  # Windows chmod is a no-op for directory bits; not worth failing.


#: Paths whose mode this process has already reconciled. The check is a stat,
#: not a chmod, and doing it once per path per process keeps a per-line syscall
#: off the hot path while still repairing a file created before this module
#: owned it.
_MODE_CHECKED: "set[str]" = set()


def _ensure_mode(path: str) -> None:
    """Force :data:`FILE_MODE` on *path*, once per path per process.

    The old code only chmod'd a file it had just CREATED, so
    ``widget_egress.jsonl`` — which predates this appender — stayed world
    readable at 0644 on the appliance while ``egress.jsonl`` and ``voice.log``
    were 0640. A security log's mode is not a create-time detail; it is an
    invariant. Called after every open and after a rotation creates a new
    generation. On Windows ``chmod`` cannot express the group bit and is a
    documented near-no-op; failing here would cost a log line for nothing.
    """
    if path in _MODE_CHECKED:
        return
    _MODE_CHECKED.add(path)
    try:
        if (os.stat(path).st_mode & 0o777) != FILE_MODE:
            os.chmod(path, FILE_MODE)
    except OSError:
        pass


def ensure_secure_mode(path: str) -> None:
    """Public alias for :func:`_ensure_mode` — the 0640 invariant, shared.

    ``audit.jsonl`` (vault/firewall/auth events) and
    ``dashboard_tile_reviews.jsonl`` must be held at 0640 like ``egress.jsonl``
    and ``widget_egress.jsonl``, not left world readable at 0644. Those two
    ledgers live in other modules with their own appenders; rather than copy
    the chmod logic (and the "once per path per process" bookkeeping) into
    each, they call this. Same file mode, same cache, same silent-on-Windows
    behaviour.
    """
    _ensure_mode(path)


def forget_mode_check(path: str) -> None:
    """Drop *path* from the once-per-process cache, so the next write re-chmods.

    Called by an external rotator after it has renamed the live file away: the
    next append creates a NEW inode, which inherits the umask, not the mode of
    the file that was rolled.
    """
    _MODE_CHECKED.discard(path)


def _rotate_if_needed(path: str) -> None:
    """Roll the ledger over once it reaches :data:`MAX_BYTES`.

    ``egress.jsonl`` -> ``egress.1.jsonl`` -> ... -> ``egress.3.jsonl``, and the
    oldest generation is dropped. Called with :data:`_lock` held.
    """
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return
    except OSError:
        return  # not there yet, or unreadable — nothing to roll

    root, ext = os.path.splitext(path)
    for index in range(BACKUP_COUNT, 0, -1):
        src = f"{root}.{index}{ext}"
        if not os.path.exists(src):
            continue
        if index == BACKUP_COUNT:
            try:
                os.remove(src)
            except OSError:
                pass
        else:
            try:
                os.replace(src, f"{root}.{index + 1}{ext}")
            except OSError:
                pass
    try:
        os.replace(path, f"{root}.1{ext}")
    except OSError:
        pass
    else:
        # The next append creates a NEW file at *path*, so the mode has to be
        # reconciled again (and the rolled generation keeps the same mode it
        # already had).
        _MODE_CHECKED.discard(path)


def _append_decision(record: dict, path: "str | None" = None) -> bool:
    """Append one decision to the ledger. Returns True if it was written.

    *record* is written as given apart from a timestamp, so callers are
    responsible for having sanitised it — :func:`safe_parts`, :func:`safe_url`
    and :func:`caller_label` exist so they do not have to think about how.

    *path* writes to a different JSONL under the same rotation policy. It exists
    for the one other egress log in the tree (``dashboard_widgets``), which had
    its own unbounded appender; sharing this one gives it a size cap without
    changing its schema.
    """
    if not isinstance(record, dict):
        return False
    line = dict(record)
    line.setdefault(
        "ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    path = path or ledger_path()
    with _lock:
        try:
            _ensure_dir(path)
            _rotate_if_needed(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False,
                                        default=str) + "\n")
            _ensure_mode(path)
            return True
        except (OSError, TypeError, ValueError) as exc:
            # Reported once per process, then silent: a read-only state dir is a
            # deployment fault that does not heal, and one line per outbound
            # request would bury the journal.
            log_unwritable_once(logger, path, "egress ledger", exc)
            return False


def _report_once(path: str, exc: BaseException) -> None:
    reported = _state["reported"]
    if path in reported:
        return
    reported.add(path)
    logger.warning("outside-traffic log at %s could not be written (%s); "
                   "requests still run, lines are skipped",
                   path, type(exc).__name__)


def _prune_locked(path: str, now: datetime) -> None:
    """Drop lines past retention, then oldest lines past the size cap."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    cutoff = now - timedelta(days=RETENTION_DAYS)
    kept: list[str] = []
    for raw in lines:
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict) and "t" not in rec and "ts" in rec:
            # A ledger record sharing an override path: not this log's to age.
            kept.append(raw if raw.endswith("\n") else raw + "\n")
            continue
        try:
            when = datetime.fromisoformat(str(rec.get("t")))
        except (ValueError, TypeError, AttributeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            kept.append(raw if raw.endswith("\n") else raw + "\n")
    # Size cap: keep the newest lines that fit in half the cap, so the next
    # prune is not one line away.
    total = sum(len(k.encode("utf-8")) for k in kept)
    if total > MAX_BYTES:
        budget = MAX_BYTES // 2
        trimmed: list[str] = []
        used = 0
        for raw in reversed(kept):
            size = len(raw.encode("utf-8"))
            if used + size > budget:
                break
            trimmed.append(raw)
            used += size
        kept = list(reversed(trimmed))
    if len(kept) == len(lines):
        return
    fsutil.atomic_write(path, "".join(kept))


def _append_outside(record: Optional[dict], *,
                    now: Optional[datetime] = None) -> bool:
    """Append one line built by :func:`build_record`. Never raises."""
    if not isinstance(record, dict):
        return False
    try:
        path = log_path()
    except Exception as exc:  # no state dir: skip, never break a request
        logger.debug("outside-traffic log path unavailable: %s", exc)
        return False
    line = json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n"
    with _lock:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            clock = time.time()
            needs_prune = clock - _state["last_prune"] > PRUNE_EVERY_S
            try:
                needs_prune = needs_prune or os.path.getsize(path) + len(line) > MAX_BYTES
            except OSError:
                pass
            if needs_prune:
                _state["last_prune"] = clock
                try:
                    _prune_locked(path, now or datetime.now(timezone.utc))
                except OSError as exc:
                    logger.debug("outside-traffic log prune skipped: %s", exc)
            # One write() of one whole line on an O_APPEND descriptor: a reader
            # never sees half a line from this process.
            fsutil.atomic_append(path, line)
            return True
        except (OSError, TypeError, ValueError) as exc:
            _report_once(path, exc)
            return False


def record(service: str, **fields: Any) -> bool:
    """Build and append one line. The one call every exit makes. Never raises."""
    try:
        return _append_outside(build_record(service, **fields))
    except Exception as exc:  # the log must never break a request
        logger.debug("outside-traffic log record skipped: %s", exc)
        return False


def append(record: Optional[dict], path: "str | None" = None, *,
           now: Optional[datetime] = None) -> bool:
    """Append one record. Returns True if it was written; never raises.

    A line built by :func:`build_record` (exactly :data:`_OUTSIDE_KEYS`) goes
    to the outside-traffic log; any other record is a decision for the ledger
    (:func:`_append_decision`), or for the JSONL at *path* under the ledger's
    rotation policy.
    """
    if not isinstance(record, dict):
        return False
    if path is None and _is_outside_line(record):
        return _append_outside(record, now=now)
    return _append_decision(record, path)


def read_records(limit: int = 100, include_rotated: bool = False) -> "list[dict]":
    """The most recent *limit* records, newest last. For tests and the owner UI."""
    paths = [ledger_path()]
    if include_rotated:
        root, ext = os.path.splitext(ledger_path())
        paths = [f"{root}.{i}{ext}" for i in range(BACKUP_COUNT, 0, -1)] + paths
    out: "list[dict]" = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        parsed = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(parsed, dict) and not _is_outside_line(parsed):
                        out.append(parsed)
        except OSError:
            continue
    return out[-limit:] if limit and limit > 0 else out


#: Fields :func:`recent_public` will hand to a network-reachable caller. An
#: ALLOWLIST, not a denylist: the ledger's own schema carries ``host`` and
#: ``path`` (and ``dashboard_widgets`` writes a ``url``), and a reader built by
#: subtracting known-bad keys would start leaking the day someone adds a field.
#: Nothing here can carry household content -- ``purpose`` and ``third_party``
#: are constants copied from the door registry, ``stored`` is which of two
#: files a toggle landed in, and the rest are enums, booleans, integers and a
#: timestamp.
#: ``reason_code`` and ``address_class`` are here while the free-text ``reason``
#: is not, and the difference is the point: ``reason`` is a sentence this code
#: composed and can contain a hostname, while these two are members of closed
#: vocabularies fixed in ``security/egress_doors.py`` and
#: ``security/netaddr.py``. They are what lets the owner UI say "refused because
#: that name pointed at a private address on your own network" — the single most
#: useful refusal an owner can be shown — without the address itself ever
#: leaving the appliance's own decision path.
PUBLIC_FIELDS: "tuple[str, ...]" = (
    "ts", "door", "result", "kind", "event", "open", "by", "stored",
    "purpose", "third_party", "bytes_out", "bytes_in", "status",
    "reason_code", "address_class",
)


def recent_public(limit: int = 10, include_rotated: bool = True) -> "list[dict]":
    """The most recent *limit* decisions, projected onto :data:`PUBLIC_FIELDS`.

    This is the reader behind ``GET /system/egress/ledger``. It differs from
    :func:`read_records` in exactly one way that matters: it never returns the
    URL path, the query string or the host. The owner UI answers "which door,
    allowed or refused, when, how many bytes" -- none of which needs a URL, and
    a URL path on a household box is where a document name or a search term
    would hide.

    Read-only and tolerant: a missing file, an unreadable one, a half-written
    last line, or a rotation happening underneath the read all yield the
    records that could be read rather than an exception. Never raises.
    """
    try:
        count = int(limit)
    except (TypeError, ValueError):
        count = 10
    if count <= 0:
        return []
    count = min(count, MAX_LEDGER_READ)
    try:
        raw = read_records(limit=count, include_rotated=include_rotated)
    except Exception:  # a ledger fault must not break the status surface
        return []
    out: "list[dict]" = []
    for record in raw:
        if not isinstance(record, dict):
            continue
        out.append({key: record[key] for key in PUBLIC_FIELDS
                    if key in record})
    return out


# ── reading ──────────────────────────────────────────────────────────────────

def read(hours: float = 24.0, *, limit: int = 500,
         now: Optional[datetime] = None) -> list[dict]:
    """Lines from the last *hours*, newest first, at most *limit*. Never raises.

    Every line is re-checked through :func:`build_record` on the way out, so a
    hand-edited file cannot put a query string or free text in front of the
    owner.
    """
    try:
        hours = max(0.0, min(float(hours), RETENTION_DAYS * 24.0))
    except (TypeError, ValueError):
        hours = 24.0
    try:
        limit = max(1, min(int(limit), MAX_READ))
    except (TypeError, ValueError):
        limit = 500
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = moment - timedelta(hours=hours)
    try:
        path = log_path()
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except Exception:
        return []
    out: list[dict] = []
    for raw in reversed(lines):
        try:
            rec = json.loads(raw)
            when = datetime.fromisoformat(str(rec.get("t")))
        except (ValueError, TypeError, AttributeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff or when > moment + timedelta(minutes=5):
            continue
        clean = build_record(
            str(rec.get("service") or ""), host=str(rec.get("host") or ""),
            outcome=str(rec.get("outcome") or ""),
            method=str(rec.get("method") or "GET"),
            path=str(rec.get("path") or ""), code=str(rec.get("code") or ""),
            status=None, bytes_in=rec.get("bytes_in"),
            bytes_out=rec.get("bytes_out"), what=str(rec.get("what") or ""),
            now=when)
        if clean is None:
            continue
        status = str(rec.get("status") or "")
        clean["status"] = status if re.match(r"^[1-5]xx$", status) else ""
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def last_used(service: str) -> Optional[dict]:
    """The newest line for *service* in the last day, or None."""
    for rec in read(24.0, limit=MAX_READ):
        if rec["service"] == str(service).lower():
            return rec
    return None


__all__ = [
    "BACKUP_COUNT",
    "DIR_MODE",
    "FILE_MODE",
    "LEDGER_NAME",
    "LOG_NAME",
    "MAX_BYTES",
    "MAX_LEDGER_READ",
    "MAX_READ",
    "OUTCOMES",
    "PUBLIC_FIELDS",
    "FETCH_SERVICES",
    "REASON_CODES",
    "RETENTION_DAYS",
    "WHAT",
    "append",
    "build_record",
    "caller_label",
    "clean_path",
    "ensure_secure_mode",
    "forget_mode_check",
    "last_used",
    "ledger_dir",
    "ledger_path",
    "log_path",
    "read",
    "read_records",
    "recent_public",
    "record",
    "safe_host",
    "safe_parts",
    "safe_path",
    "safe_url",
    "set_path_override",
]
