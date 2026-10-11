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

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

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

_lock = threading.Lock()

#: Test/ops hook. Set to a path and every write goes there verbatim.
_PATH_OVERRIDE: "str | None" = None


def set_path_override(path: "str | None") -> None:
    """Point the ledger at *path* (tests, and an operator moving the trail)."""
    global _PATH_OVERRIDE
    with _lock:
        _PATH_OVERRIDE = path


def ledger_dir() -> str:
    """The directory the ledger lives in. Resolved lazily, never at import."""
    return resolve_runtime_dir(
        os.path.join(_REPO_ROOT, "security_logs"), env_subdir="security_logs")


def ledger_path() -> str:
    """Absolute path to the active ledger file."""
    if _PATH_OVERRIDE:
        return _PATH_OVERRIDE
    return os.path.join(ledger_dir(), LEDGER_NAME)


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


def append(record: dict, path: "str | None" = None) -> bool:
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
                    if isinstance(parsed, dict):
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


__all__ = [
    "BACKUP_COUNT",
    "DIR_MODE",
    "FILE_MODE",
    "LEDGER_NAME",
    "MAX_BYTES",
    "MAX_LEDGER_READ",
    "PUBLIC_FIELDS",
    "append",
    "caller_label",
    "ledger_dir",
    "ledger_path",
    "read_records",
    "recent_public",
    "safe_parts",
    "safe_url",
    "set_path_override",
]
