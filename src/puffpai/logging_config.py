"""
PuffPAI Logging Configuration
==============================
Centralized logging setup for all PuffPAI modules.

Usage:
    from puffpai.logging_config import setup_logging

    setup_logging()           # INFO to console
    setup_logging("DEBUG")    # DEBUG to console
    setup_logging("INFO", log_file="puffpai.log")  # console + rotating file

Modules should use:
    import logging
    logger = logging.getLogger(__name__)
    logger.info("message")
"""

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler


# Logger hierarchy mirrors package structure:
#   puffpai
#   puffpai.security
#   puffpai.inference
#   puffpai.voice
#   puffpai.ui

_MAX_FILE_BYTES = 5 * 1024 * 1024   # 5 MB
_BACKUP_COUNT = 3
_CONSOLE_FORMAT = "[%(levelname).1s] %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False

#: journald's syslog priority for each Python level. systemd reads a leading
#: ``<N>`` on a line the service writes and files the entry at that priority;
#: with no prefix EVERY line lands at PRIORITY=6 (informational), whatever the
#: stream or the Python level.
#:
#: ``egress REFUSED`` is emitted by ``security.egress_doors`` with
#: ``logger.warning`` and appears in the journal prefixed ``[W]``; without the
#: prefix, ``journalctl -u puffpai-api -p warning`` returns NOTHING for it
#: because the line is filed at PRIORITY=6. An operator filtering by priority,
#: which is the ordinary way to find warnings, would see no egress refusals at
#: all; only a text grep would find them. This is severity metadata, not
#: a gate: no refusal is added, removed or changed by it.
_JOURNAL_PRIORITY = {
    logging.CRITICAL: 2,
    logging.ERROR: 3,
    logging.WARNING: 4,
    logging.INFO: 6,
    logging.DEBUG: 7,
}
_JOURNAL_DEFAULT_PRIORITY = 6


def journal_priority(levelno: int) -> int:
    """journald priority for a Python level, rounding DOWN to the named level
    below it so a custom level (e.g. 25) is never filed as more severe than it
    is."""
    best = _JOURNAL_DEFAULT_PRIORITY
    for level in sorted(_JOURNAL_PRIORITY):
        if levelno >= level:
            best = _JOURNAL_PRIORITY[level]
    return best


class _JournalLevelFormatter(logging.Formatter):
    """Console formatter that tells journald the line's real priority.

    Only installed when the process is actually writing to the journal
    (systemd sets ``$JOURNAL_STREAM``). In a terminal the prefix would be
    noise, and nothing parses it, so the format there is unchanged.
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        prefix = "<%d>" % journal_priority(record.levelno)
        # journald reads the prefix per LINE, so a multi-line record (a
        # traceback) gets it on every line rather than only the first.
        return "\n".join(prefix + line for line in text.split("\n"))


def _console_formatter() -> logging.Formatter:
    """The stderr formatter: journald-aware under systemd, plain elsewhere."""
    if os.environ.get("JOURNAL_STREAM"):
        return _JournalLevelFormatter(_CONSOLE_FORMAT)
    return logging.Formatter(_CONSOLE_FORMAT)

#: Mode for the log file. At INFO the log carries LLM reply fragments,
#: uploaded filenames and HA device names, so it should not be world-readable.
_LOG_FILE_MODE = 0o640


class _ModePreservingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that re-applies the file mode on every rollover.

    Rollover renames the current file (which keeps its mode) and opens a NEW
    one, created with whatever the process umask happens to be — systemd's
    default 0022 yields 0644. Without this the caller's one-time chmod at
    startup silently lapses the first time the log hits 5 MB, and the *active*
    log is the world-readable one. Enforced here rather than relying solely on
    UMask= in the unit, so the guarantee holds for any caller and needs no
    root to deploy.
    """

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, _LOG_FILE_MODE)
        except OSError:
            # Windows chmod only toggles the read-only bit, and a mode we
            # cannot set is not worth losing the log over.
            pass
        return stream


def make_rotating_file_handler(
    log_file: str,
    max_bytes: int = _MAX_FILE_BYTES,
    backup_count: int = _BACKUP_COUNT,
) -> RotatingFileHandler:
    """Build the project's standard size-capped, 0640 log handler.

    Public entry point to :class:`_ModePreservingRotatingFileHandler` for the
    services that configure their own logger instead of going through
    :func:`setup_logging` — currently ``puffpai.voice.service``, which owns a
    separate ``voice.log`` sink. Sharing the handler means one rotation policy
    and one file-mode guarantee: an unrotated, world-readable log cannot be
    reintroduced by a caller that simply forgot.

    Raises OSError if the file cannot be opened; callers decide whether to
    degrade to console-only.
    """
    handler = _ModePreservingRotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count,
        encoding="utf-8",
    )
    # A file that already exists from an earlier, non-rotating deployment keeps
    # its old (possibly 0664) mode until the first rollover; chmod it now so the
    # tightening does not wait for the log to fill.
    try:
        os.chmod(handler.baseFilename, _LOG_FILE_MODE)
    except OSError:
        pass
    return handler


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    force: bool = False,
) -> logging.Logger:
    """Configure logging for PuffPAI.

    Args:
        level: Log level string ("DEBUG", "INFO", "WARNING", "ERROR").
        log_file: Optional path for rotating file handler.
        force: Reconfigure even if already configured.

    Returns:
        The root 'puffpai' logger.
    """
    global _configured
    if _configured and not force:
        return logging.getLogger("puffpai")

    root_logger = logging.getLogger("puffpai")
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear existing handlers to avoid duplicates on reconfigure
    root_logger.handlers.clear()

    # Console handler (stderr)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG)
    console.setFormatter(_console_formatter())
    root_logger.addHandler(console)

    # Optional rotating file handler. Never let file trouble (locked by a
    # second instance mid-rotation, permissions) take logging down with
    # it — console output must survive.
    if log_file:
        try:
            file_handler = _ModePreservingRotatingFileHandler(
                log_file, maxBytes=_MAX_FILE_BYTES, backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(
                logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT)
            )
            root_logger.addHandler(file_handler)
        except OSError as exc:
            root_logger.warning(
                "File logging disabled (%s): %s", log_file, exc)

    # Also redact the ephemeral vault-view bearer token from records that reach
    # these "puffpai" sinks by *propagation* from a child logger — most
    # importantly Flask.log_exception, which logs "Exception on <request.path>
    # [<method>]" on app.logger ("puffpai.api_server") whenever a
    # /vault/view/<token> request raises an unhandled 500. The werkzeug filter
    # never sees that record (different logger tree), so without this the token
    # lands in journald AND the on-disk puffpai.log. A filter on the "puffpai"
    # logger would not catch it either: propagated records are dispatched to
    # ancestor *handlers* (callHandlers), and only the originating logger's
    # filters run — so attach the redactor at the handler level. Same stateless,
    # idempotent filter as the werkzeug access line; no second regex.
    for handler in root_logger.handlers:
        if not any(isinstance(f, _AccessLogVaultTokenFilter)
                   for f in handler.filters):
            handler.addFilter(_AccessLogVaultTokenFilter())

    # Keep the ephemeral vault-view bearer token out of werkzeug's per-request
    # access lines (journald in production). Idempotent; see the function.
    install_access_log_redaction()

    _configured = True
    return root_logger


# ── credential redaction for log/diagnostic strings ────────────────────────
# HTTP client exceptions stringify the *whole* request URL, query string
# included. Any API that authenticates with a query parameter (Pi-hole v5's
# ``?auth=<hash>`` is the one in this codebase) therefore hands its credential
# straight to ``logger.debug("... %s", exc)`` and to any error message built
# with an f-string. Redact at the point the exception becomes text, not at the
# handler: the same strings are also returned in HTTP responses.
#
# Longest alternatives first — ``\b`` will not match inside ``access_token``,
# so ``token`` alone would leave ``access_token=...`` untouched.
_SECRET_PARAMS = (
    "access_token", "refresh_token", "api_key", "apikey", "auth_token",
    "auth", "token", "password", "passwd", "pwd", "secret", "sig",
    "signature", "session_key", "key",
)

# An optional ``<prefix>_`` so the vault/keyring key names this codebase
# actually uses are covered without listing each one: HA_TOKEN, PUFFPAI_API_KEY,
# JELLYFIN_API_KEY, ADGUARD_PASSWORD, NODERED_API_TOKEN. The prefix must end in
# an underscore, which is what keeps ``monkey=banana`` from matching on "key".
_SECRET_NAME = (
    r"(?:[A-Za-z0-9]\w*_)?(?:" + "|".join(_SECRET_PARAMS) + r")")

# The value alternation tries the quoted forms first: Jellyfin's header is
# ``Token="<key>"``, and an unquoted-only pattern would stop at the opening
# quote and leave the key itself in the line.
_QUERY_SECRET_RE = re.compile(
    r"(?i)\b(" + _SECRET_NAME
    + r")=(?:\"[^\"]*\"|'[^']*'|[^&\s'\"<>]*)")

# ``Authorization: Bearer <jwt>`` / ``Basic <b64>`` if a header ever lands in
# an exception or a debug dump. The {16,} floor keeps ordinary prose
# ("Basic authentication failed") out of the match — credentials are longer
# than the English words that follow these keywords.
_AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}")

# JSON bodies and dict reprs: ``{"password": "..."}`` / ``{'api_key': '...'}``.
# The key must be quoted — an unquoted ``token:`` form would also match the
# userinfo half of ``//token:pass@host`` and swallow the hostname with it.
_MAPPING_SECRET_RE = re.compile(
    r"(?i)([\"'])(" + _SECRET_NAME
    + r")\1(\s*:\s*)(?:\"[^\"]*\"|'[^']*'|[^,}\s\]]+)")

# ``https://user:pass@host`` — urllib/requests echo userinfo in errors.
_URL_USERINFO_RE = re.compile(r"(?i)(//[^/\s:@]+):[^/\s@]+@")

_REDACTED = "<redacted>"


def redact_secrets(value: object) -> str:
    """Return ``str(value)`` with credential material masked.

    Use wherever an exception, URL, or third-party message is about to be
    logged *or* returned to a caller and could carry a credential — most
    often a secret in a query string, which HTTP client exceptions include
    verbatim. Non-secret detail (host, port, path, cause) is preserved so the
    line stays diagnostic.
    """
    text = value if isinstance(value, str) else str(value)
    text = _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", text)
    text = _MAPPING_SECRET_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}"
                  f"{m.group(3)}{_REDACTED}", text)
    text = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)} {_REDACTED}", text)
    text = _URL_USERINFO_RE.sub(rf"\1:{_REDACTED}@", text)
    return text


# ── access-log redaction for the token-bearing vault serves ────────────────
# TWO paths carry a short-lived bearer token in the URL PATH:
#   GET/DELETE /vault/view/<token>    the ephemeral doc serve (~50 s life)
#   GET        /vault/reveal/<token>  the one-time secret reveal
# Both are redacted by the same filter — one regex, one code path, so a new
# token-in-path route cannot be protected on one sink and not the other.
# GET/DELETE /vault/view/<token> carry a short-lived bearer token in the URL
# PATH. The production server is the werkzeug dev server (start_api.py ->
# app.run()); its per-request access line is logged on the "werkzeug" logger and
# carried to journald, so that token would sit in the local access log for its
# ~50 s life. It is loopback-only and is neither the PIN nor the decrypted
# bytes, so this is defence-in-depth — but a token in a log is still a token in
# a log. Redact just that one path segment; every other request line is left
# byte-for-byte intact. token_urlsafe(32) is [A-Za-z0-9_-], so the value class
# below stops only at a space/quote/query/fragment/slash boundary.
_VAULT_VIEW_PATH_RE = re.compile(r"(/vault/(?:reveal|view)/)[^/\s\"?#]+")

#: Cheap membership test used before the substitution above, so an ordinary
#: access line does no regex-substitution work at all.
_VAULT_TOKEN_PATHS_RE = re.compile(r"/vault/(?:reveal|view)/")


def _redact_vault_view(value: object) -> object:
    """Rewrite a ``/vault/view/<token>`` path segment to ``/vault/view/<redacted>``.

    Anything that is not a string carrying that path is returned unchanged and
    untouched, so werkzeug's non-string args (status code, size) survive.
    """
    if isinstance(value, str) and _VAULT_TOKEN_PATHS_RE.search(value):
        return _VAULT_VIEW_PATH_RE.sub(r"\1<redacted>", value)
    return value


class _AccessLogVaultTokenFilter(logging.Filter):
    """Redact the ephemeral vault-view token from werkzeug access-log records.

    werkzeug logs the request line as a deferred ``%s`` arg — ``record.args``
    is ``(request_line, code, size)`` and ``request_line`` is e.g.
    ``'GET /vault/view/<token> HTTP/1.1'`` — so the token is redacted on the
    args, before any handler formats them. Attaching this to the ``werkzeug``
    logger makes it run in that logger's ``filter()`` pass, which happens before
    the (shared, now-mutated) record propagates to whatever sink handles it
    (journald, the rotating file, ``logging.lastResort``); the token therefore
    reaches none of them. The record is never dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            new_args = tuple(_redact_vault_view(a) for a in args)
            if new_args != args:
                record.args = new_args
        elif isinstance(args, dict):
            new_map = {k: _redact_vault_view(v) for k, v in args.items()}
            if new_map != args:
                record.args = new_map
        # Defensive: a caller that pre-formats the path into ``msg`` with no
        # args (not werkzeug's shape, but cheap insurance for any other server).
        if isinstance(record.msg, str) and _VAULT_TOKEN_PATHS_RE.search(record.msg):
            record.msg = _VAULT_VIEW_PATH_RE.sub(r"\1<redacted>", record.msg)
        return True


# ── dashboard-poll access-log suppression ─────────────────────────────────
# With NOBODY using the box, around 90% of puffpai-api journal lines are
# werkzeug access lines from the dashboard's own poll loop (about 250 req/min
# at zero user activity, led by GET /apps/alarmflow/updated). journald on the
# appliance is VOLATILE (the journals live in /run/log/journal under
# RuntimeMaxUse=100M), so that noise cannot flood the disk; what it floods is
# the history: only a few hours of journal are retained, most of them these
# lines. Dropping them is what buys back the ability to debug something that
# happened this morning.
#
# Scope is deliberately narrow:
#   * only GET (a POST changes state and is always logged — the measured
#     POST /apps/mediahub/action poller is therefore NOT suppressed),
#   * only 2xx (every 4xx/5xx on these paths still logs, so a poller that
#     starts failing is still visible),
#   * only paths on the poll list below.
# Everything else — including the token-bearing /vault/view serves, which are
# redacted by the filter above and keep their audit line — is untouched.

#: Poll paths suppressed at 2xx GET. Overridable with
#: PUFFPAI_ACCESS_LOG_QUIET_PATHS (comma-separated path prefixes); set
#: PUFFPAI_ACCESS_LOG_QUIET_POLLS=0 to disable the whole filter and get the
#: firehose back for a debugging session.
DEFAULT_QUIET_POLL_PATHS = (
    "/apps/",                       # /apps/<app>/updated — the top 3 pollers
    "/dashboard/display",
    "/knowledge/suggestions/pending",
    "/api/media/now-playing",
    "/api/spotify/status",
    "/devices/pending",
    "/announcements",
    "/system/status",
    "/agents/status",
    # Measured on a running appliance: 6,169 api
    # journal lines in 30 min, 73% of them these two voice-state pollers, so
    # the volatile journal held only ~11 h. /ha/camera/* stays logged on
    # purpose: a camera view is worth an audit line.
    "/voice/state",
    "/voice/live",
    "/agents/activity",
    "/household",
)
# NOT on the list, deliberately:
#   /health   its 200 access line is the only marker of "the API is up" in the
#             log; boot -> /health is already UNMEASURABLE because the journal
#             retains ~6 h; silencing it would make that permanent. It was not
#             one of the measured top pollers either, so there is nothing to win.
#   POST anything  a POST changes state and is always logged (see the filter).

QUIET_POLLS_ENV = "PUFFPAI_ACCESS_LOG_QUIET_POLLS"
QUIET_PATHS_ENV = "PUFFPAI_ACCESS_LOG_QUIET_PATHS"

#: werkzeug's request line, e.g. ``GET /apps/alarmflow/updated HTTP/1.1``.
_ACCESS_REQUEST_LINE_RE = re.compile(r"^(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/")


def quiet_poll_paths() -> tuple:
    """Poll path prefixes to suppress, honouring the env override."""
    raw = os.environ.get(QUIET_PATHS_ENV)
    if raw is None:
        return DEFAULT_QUIET_POLL_PATHS
    paths = tuple(p.strip() for p in raw.split(",") if p.strip())
    return paths or DEFAULT_QUIET_POLL_PATHS


def _quiet_polls_enabled() -> bool:
    return os.environ.get(QUIET_POLLS_ENV, "1").strip().lower() not in (
        "0", "false", "no", "off")


def _is_quiet_poll_record(record: logging.LogRecord) -> bool:
    """True when this access line is a successful GET on a poll path.

    werkzeug logs ``record.args = (request_line, status_code, size)`` with the
    status code as a STRING ("200"). Anything that does not match that exact
    shape is left alone and logged — an unparseable record is never dropped.
    """
    args = record.args
    if not isinstance(args, tuple) or len(args) < 2:
        return False
    request_line, status = args[0], args[1]
    if not isinstance(request_line, str):
        return False
    try:
        code = int(str(status).strip())
    except (TypeError, ValueError):
        return False
    if not (200 <= code < 300):
        return False
    match = _ACCESS_REQUEST_LINE_RE.match(request_line)
    if match is None or match.group("method") != "GET":
        return False
    path = match.group("path").split("?", 1)[0]
    return any(path.startswith(prefix) for prefix in quiet_poll_paths())


class _AccessLogPollFilter(logging.Filter):
    """Drop 2xx GET access lines for the dashboard's poll endpoints.

    Returns ``False`` (drop) only for that exact class of record. Added AFTER
    :class:`_AccessLogVaultTokenFilter` so redaction has already run on
    whatever does get through; a dropped record was never emitted at all.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not _quiet_polls_enabled():
            return True
        return not _is_quiet_poll_record(record)


def install_access_log_poll_filter() -> None:
    """Attach the poll-noise filter to the ``werkzeug`` access logger.

    Idempotent and self-healing, like the redactor. Called from
    :func:`install_access_log_redaction` so every entry point that already
    installs redaction gets this too, with no new call sites to keep in sync.
    """
    werkzeug_logger = logging.getLogger("werkzeug")
    if not any(isinstance(f, _AccessLogPollFilter)
               for f in werkzeug_logger.filters):
        werkzeug_logger.addFilter(_AccessLogPollFilter())


def install_access_log_redaction() -> None:
    """Attach the vault-view token redactor to the ``werkzeug`` access logger.

    Idempotent (a second call adds nothing) and self-healing (re-adds the filter
    if something cleared it). Safe to call from every entry point — it is called
    from :func:`setup_logging` (the production path, via start_api.py, and the
    Windows scheduled-task path via puffpai.service) and again at ``api_server``
    import so redaction is active however the server is launched. Attaches to the
    logger, not a handler, so the record is redacted once, upstream of every sink.
    """
    werkzeug_logger = logging.getLogger("werkzeug")
    if not any(isinstance(f, _AccessLogVaultTokenFilter)
               for f in werkzeug_logger.filters):
        werkzeug_logger.addFilter(_AccessLogVaultTokenFilter())
    # Redaction first, poll-suppression second: a record that survives is
    # already redacted, and a record that is dropped was never emitted.
    install_access_log_poll_filter()
    install_access_log_cast_filter()


# ── TV cast proxy: never log /cast/ request lines ──────────────────────────
# /cast/<id>/... carries the live cast id in the PATH, and an Apple TV fetches
# a segment every few seconds, so these are filtered
# out of the access log entirely; puffpai.tv_cast logs start/stop
# with only the first 6 characters of the id instead. Any status, any method:
# a 404 probe line would still carry a guessed id, so it is dropped as well.
# Searched, never anchored: werkzeug wraps every NON-200 request line in ANSI
# colour codes ("\x1b[33mGET /cast/... HTTP/1.1\x1b[0m" for a 404), so a
# ^METHOD match misses exactly the probe and revoked-id lines.
_CAST_PATH_RE = re.compile(r"(?:^|\s|\x1b\[[0-9;]*m)/cast/")


def _names_cast_path(value: object) -> bool:
    return isinstance(value, str) and bool(_CAST_PATH_RE.search(value))


class _AccessLogCastFilter(logging.Filter):
    """Drop werkzeug access lines for /cast/ paths (and any msg naming one)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and any(_names_cast_path(a) for a in args):
            return False
        if isinstance(args, dict) and any(_names_cast_path(v) for v in args.values()):
            return False
        if _names_cast_path(record.msg):
            return False
        return True


def install_access_log_cast_filter() -> None:
    """Attach the /cast/ drop filter to the ``werkzeug`` logger. Idempotent."""
    werkzeug_logger = logging.getLogger("werkzeug")
    if not any(isinstance(f, _AccessLogCastFilter)
               for f in werkzeug_logger.filters):
        werkzeug_logger.addFilter(_AccessLogCastFilter())


def get_logger(name: str) -> logging.Logger:
    """Get a logger under the puffpai hierarchy.

    Usage:
        logger = get_logger(__name__)  # e.g. "puffpai.security.vault"
    """
    if not name.startswith("puffpai"):
        name = f"puffpai.{name}"
    return logging.getLogger(name)


# ── one-shot "this path is not writable" reporting ─────────────────────────
# A read-only state directory is a *deployment* fault: it does not heal on its
# own, and the code paths that hit it (event intake, the vault probe, the audit
# ledger) run on a timer. Logging per occurrence buries the journal in identical
# lines — the vault failure repeated every 60 s for the life of a deployment
# without anyone reading it. Report each (purpose, path) pair exactly once at
# ERROR, then let the caller degrade quietly.

_unwritable_reported: set[str] = set()


def log_unwritable_once(logger: logging.Logger, path: str, purpose: str,
                        exc: BaseException | None = None) -> bool:
    """Log an unwritable-path error once per process. Returns True if logged.

    Args:
        logger: the calling module's logger.
        path: the path that could not be written (named in the message).
        purpose: what the path is for, e.g. ``"agent event log"``.
        exc: the OSError that surfaced it, if any.
    """
    key = f"{purpose}\0{path}"
    if key in _unwritable_reported:
        return False
    _unwritable_reported.add(key)
    # Imported here so this module stays dependency-free at import time.
    from puffpai.config import UNWRITABLE_HINT
    logger.error(
        "%s is unavailable: cannot write %s (%s). %s",
        purpose, path, exc if exc is not None else "no error reported",
        UNWRITABLE_HINT,
    )
    return True


def reset_unwritable_reports() -> None:
    """Forget which paths have been reported (tests only)."""
    _unwritable_reported.clear()
