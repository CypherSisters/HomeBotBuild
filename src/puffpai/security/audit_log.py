"""
PuffPAI Security Audit Log
Append-only security event trail.

Logs all vault access, firewall changes, network scans, and
authentication attempts with timestamps.

Storage: JSON Lines format (one JSON object per line).
Production target: tamper-proof storage with cryptographic chaining.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# NOTE: this file is src/puffpai/security/audit_log.py, so three parents up is
# <repo>/src, NOT the repo root. <repo>/src/security_logs is inside the
# read-only code tree on the appliance, while the systemd unit grants write
# access to <repo>/security_logs; a ledger there would send every audit entry
# to the stderr fallback below. The directory comes from
# paths.security_logs_dir via resolve_state_dir(); <repo>/src/security_logs
# stays as the legacy fallback so an existing trail is not orphaned.
_SRC_DIR = Path(__file__).resolve().parent.parent.parent      # <repo>/src
_REPO_ROOT = _SRC_DIR.parent                                  # <repo>
_DEFAULT_LOG_DIR = _REPO_ROOT / "security_logs"
_LEGACY_LOG_DIR = _SRC_DIR / "security_logs"

_MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB
_MAX_ROTATED_FILES = 10


def _ensure_secure_mode(path) -> None:
    """Hold ``audit.jsonl`` at 0640, the same invariant the egress ledgers keep.

    At 0644 this file is a world-readable trail of vault access, firewall
    changes and auth attempts, next to two egress ledgers at 0640. The mode
    is enforced by
    :func:`puffpai.security.egress_ledger.ensure_secure_mode` so there is one
    definition of "a security log's mode", checked once per path per process.
    Never raises: an audit line is worth more than a mode bit.
    """
    try:
        from puffpai.security.egress_ledger import ensure_secure_mode
        ensure_secure_mode(str(path))
    except Exception:
        pass


def _forget_secure_mode(path) -> None:
    """Re-arm the mode check after a rotation renames the live file away."""
    try:
        from puffpai.security.egress_ledger import forget_mode_check
        forget_mode_check(str(path))
    except Exception:
        pass


def default_log_dir() -> Path:
    """Resolve the security-log directory from config, env, then repo default.

    Resolved per AuditLog construction rather than at import so config (which
    reaches the secrets provider) is never imported at module scope here.
    """
    try:
        from puffpai.config import get_config, resolve_state_dir
        cfg = get_config()
        return Path(resolve_state_dir(
            cfg.paths.security_logs_dir,
            repo_root=cfg.REPO_ROOT,
            default=str(_DEFAULT_LOG_DIR),
            env_subdir="security_logs",
            legacy=str(_LEGACY_LOG_DIR),
        ))
    except Exception:  # audit must never be unbuildable because config failed
        return _DEFAULT_LOG_DIR


class AuditLog:
    """Append-only security audit trail.

    All entries are written as JSON Lines with timestamps.
    Thread-safe for concurrent access from API server.
    """

    def __init__(self, log_file=None):
        self._log_file = (Path(log_file) if log_file
                          else default_log_dir() / "audit.jsonl")
        self._lock = threading.Lock()
        try:
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Fail closed: a security ledger with nowhere to write
            # is a hard fault for the caller. Name the path once so the reason
            # is in the journal instead of only the raised message.
            from puffpai.logging_config import get_logger, log_unwritable_once
            log_unwritable_once(get_logger(__name__), str(self._log_file),
                                "security audit log", exc)
            raise

    def log(self, action: str, user: str = "system", result: str = "success",
            details: str = "", extra: dict | None = None) -> dict:
        """Append a security event to the audit log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "epoch": time.time(),
            "action": str(action)[:100],
            "user": str(user)[:100],
            "result": str(result)[:50],
            "details": str(details)[:500],
        }
        if extra:
            try:
                sanitized = json.loads(json.dumps(extra, default=str)[:2000])
                entry["extra"] = sanitized
            except (TypeError, json.JSONDecodeError):
                entry["extra"] = {"raw": str(extra)[:200]}

        with self._lock:
            self._rotate_if_needed()
            try:
                with open(self._log_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
                _ensure_secure_mode(self._log_file)
            except IOError as e:
                print(f"[AUDIT FAILURE] Cannot write to {self._log_file}: {e}",
                      file=sys.stderr)
                print(f"[AUDIT ENTRY] {json.dumps(entry)}", file=sys.stderr)
        return entry

    def _rotate_if_needed(self):
        """Rotate log files when they exceed the size limit."""
        try:
            if not self._log_file.exists():
                return
            if self._log_file.stat().st_size < _MAX_LOG_SIZE:
                return
            for i in range(_MAX_ROTATED_FILES, 0, -1):
                src = self._log_file.parent / f"audit.{i}.jsonl"
                dst = self._log_file.parent / f"audit.{i + 1}.jsonl"
                if src.exists():
                    if i == _MAX_ROTATED_FILES:
                        src.unlink()
                    else:
                        src.rename(dst)
            self._log_file.rename(self._log_file.parent / "audit.1.jsonl")
            # The next append creates a new inode under the umask, so the
            # 0640 check has to run again for this path.
            _forget_secure_mode(self._log_file)
        except OSError:
            pass

    def query(self, action=None, user=None, result=None,
              limit: int = 100, since_epoch: float | None = None) -> list:
        """Query recent audit log entries."""
        entries = []
        try:
            if not self._log_file.exists():
                return []
            with open(self._log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if action and entry.get("action") != action:
                        continue
                    if user and entry.get("user") != user:
                        continue
                    if result and entry.get("result") != result:
                        continue
                    if since_epoch and entry.get("epoch", 0) < since_epoch:
                        continue
                    entries.append(entry)
        except IOError:
            pass
        entries.reverse()
        return entries[:limit]

    def get_failed_auth_count(self, window_seconds: int = 3600) -> int:
        """Count failed authentication attempts in a time window."""
        since = time.time() - window_seconds
        failures = self.query(action="vault_auth", result="failed",
                              since_epoch=since, limit=1000)
        return len(failures)

    def get_recent_activity(self, limit: int = 20) -> list:
        """Get the most recent audit log entries."""
        return self.query(limit=limit)
