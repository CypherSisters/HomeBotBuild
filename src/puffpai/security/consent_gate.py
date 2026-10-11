"""Consent-gated onboarding + Sentinel review gate (security-by-design).

Implements the ``approve_*`` surface from agent-config.json:

* ConsentStore   — per-device approval ledger, DEFAULT-PENDING. Entities
                   covered only by a legacy blanket approval (many entities
                   approved in one burst, zone3_applied=false on every row)
                   are flagged, never trusted.
* validate_automation — strict schema check/normalization for LLM-parsed
                   automations before Bridge ever sees them.
* sentinel_review — policy verdict (allow / flag / deny) on every automation
                   the agents author, with a JSONL audit trail.

ENFORCEMENT IS FLAG-GATED: set PUFFPAI_ENFORCE_CONSENT=1 to make deny
verdicts binding. Default (unset/0) is log-only "dry-run" so nothing
breaks until the owner flips the flag. Hard-fail is reserved for verdicts
that would target the host's own interfaces (never allowed).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

# statedir is a stdlib-only leaf, so importing it at module scope pulls in
# nothing — it defers the puffpai.config import (and with it the secrets
# provider) to call time. _STATE_DIR_ENV is the env-var NAME only;
# tests/test_statedir.py pins it to config.STATE_DIR_ENV, so reading it here
# cannot drift from canon.
from puffpai.statedir import _STATE_DIR_ENV, resolve_runtime_dir

logger = logging.getLogger(__name__)

# Re-entrant as insurance, not necessity: no current caller holds _LOCK across
# a helper that re-acquires it, but the rate-limit acquisitions below sit on
# read paths (_load, _in_legacy_blanket) reachable from status(), so a future
# caller that took _LOCK around a status() call would deadlock a plain Lock.
_LOCK = threading.RLock()

# ── state location (lazy) ─────────────────────────────────────────────
#
# These paths are NOT import-time constants built from a raw
# os.environ.get("PUFFPAI_DATA_DIR", ...): that has no strip, no "~"
# expansion, and takes a relative value verbatim (i.e. resolved against the
# CWD of whoever started the process, not the repo). On the appliance that
# could put the consent ledger and the review audit trail outside the unit's
# ReadWritePaths root, where writes fail and the sentinel's record of what it
# allowed is lost. They resolve on first use through puffpai.statedir, the
# stdlib-only leaf that reaches config.resolve_state_dir (the canon) at call
# time.
#
# The env-unset DEFAULT is the <repo>/data that ha_device_watcher and
# zone3/policy_engine use. It has to be: the watcher WRITES
# ha_device_approvals.jsonl under <repo>/data and this module READS that file
# as LEGACY_APPROVALS. If the two disagreed, the gate would read a trail that
# is never written, and every entity covered only by the legacy blanket
# approval would score "pending", a hard deny, instead of "legacy_blanket",
# which flags. Files in the legacy <repo>/src/puffpai/data directory are not
# orphaned: they are adopted file by file in _state_file() below.

#: Repo root — four directory levels up from
#: ``src/puffpai/security/consent_gate.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: Canonical default: the repo-root ``data`` directory, shared
#: with ha_device_watcher and zone3/policy_engine. Read-only on a hardened
#: deployment, which is why a deployment sets ``$PUFFPAI_DATA_DIR`` (or an
#: absolute ``paths.*`` entry).
_DEFAULT_DATA_DIR = os.path.join(_REPO_ROOT, "data")

#: The legacy default, ``src/puffpai/data``. Nothing is written here;
#: files an earlier release left behind are adopted individually by
#: :func:`_state_file`, which only READS from them — nothing is copied or
#: moved implicitly, so an operator's own migration stays the operator's.
_LEGACY_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

#: Resolved data directory; ``None`` = not resolved yet. A reload re-runs this
#: assignment, which is how the tests re-resolve after setting the env var.
_data_dir_cache: str | None = None

# Four test modules isolate the review log with
# ``monkeypatch.setattr(consent_gate, "REVIEW_LOG", ...)``. Assigning the name
# puts it in the module globals, where ``__getattr__`` never fires, so the
# resolvers below check for it explicitly and an override still wins. Clearing
# any leftover on (re)import keeps a reload a full reset, exactly as it was
# when these were plain constants.
for _name in ("CONSENT_PATH", "REVIEW_LOG", "LEGACY_APPROVALS"):
    globals().pop(_name, None)
del _name


def _data_dir() -> str:
    """Directory holding consent state, resolved on first use and cached.

    Lazy by construction: :mod:`puffpai.statedir` is a stdlib-only leaf, but
    the resolution it performs imports ``puffpai.config`` (which reaches the
    secrets provider), so it must not run at import time.

    The ``legacy=`` passed here is the canon's DIRECTORY-level adoption, which
    fires only when ``<repo>/data`` does not exist at all. Every checkout ships
    one, so on a real box it never fires; the per-file adoption in
    :func:`_state_file` is what actually rescues an upgraded box's ledgers.
    """
    global _data_dir_cache
    if _data_dir_cache is None:
        _data_dir_cache = resolve_runtime_dir(
            _DEFAULT_DATA_DIR, legacy=_LEGACY_DATA_DIR)
    return _data_dir_cache


def _override(name: str) -> str | None:
    """Return an explicitly assigned module-level path, if there is one."""
    value = globals().get(name)
    return value if isinstance(value, str) and value else None


#: Resolved LEGACY paths already reported as adopted, so the operator's cue to
#: move the file is logged once per file per process.
#: :meth:`ConsentStore._in_legacy_blanket` deliberately RE-READS after a
#: failed read, and re-resolves the path to do it. Without this, the one case
#: where both fire (adoption active AND the trail unreadable: env unset, new
#: file absent, old file locked by ha_device_watcher mid-write) would emit an
#: adoption line per status() call -- ten for a single three-target gate call --
#: burying the one line that says the trail cannot be read. Keyed on the legacy
#: path, which differs per file, so device_consent.json, sentinel_reviews.jsonl
#: and ha_device_approvals.jsonl each still get their own cue.
_adoption_reported: set[str] = set()


def _state_file(name: str) -> str:
    """Absolute path of one state file, with file-level legacy adoption.

    Adoption is per FILE, not the directory-level ``legacy=`` of the canon:
    ``<repo>/data`` exists in every checkout, so a directory probe would never
    fall through to the legacy ``src/puffpai/data``. It fires only when
    nothing else decided the location — no ``$PUFFPAI_DATA_DIR``, and no file
    of that name at the new path — and it only points reads and writes at the
    old file; nothing is copied or moved, so the warning is the operator's cue
    to move it themselves.

    The env test is "is the var set", NOT "did the resolved dir move". Those
    differ exactly when an operator points $PUFFPAI_DATA_DIR at the default
    data dir (absolute <repo>/data, or the relative value "data", which joins
    to the repo root): a dir comparison reads that as "unset", adopts, and the
    gate then reads AND WRITES its ledger in the read-only code tree with an
    env var explicitly set. The canon has the same shape — resolve_state_dir
    returns on a non-empty env_root before it ever probes legacy.

    The normpath comparison is a separate guard: when the directory-level
    ``legacy=`` in :func:`_data_dir` already resolved to the legacy directory,
    the two paths are the same file and there is nothing to adopt.
    """
    new = os.path.join(_data_dir(), name)
    legacy = os.path.join(_LEGACY_DATA_DIR, name)
    env_set = bool(os.environ.get(_STATE_DIR_ENV, "").strip())
    if (not env_set
            and os.path.normpath(legacy) != os.path.normpath(new)
            and not os.path.exists(new)
            and os.path.isfile(legacy)):
        # Rate-limited for the same reason every other report in this module
        # is: a resolution can now happen per call, not once per process.
        # _report_once is defined below -- name resolution is at call time.
        if _report_once(_adoption_reported, legacy):
            logger.warning(
                "using legacy consent state file %s; move it to %s to silence "
                "this", legacy, new,
            )
        return legacy
    return new


def _consent_path() -> str:
    return _override("CONSENT_PATH") or _state_file("device_consent.json")


def _review_log_path() -> str:
    return _override("REVIEW_LOG") or _state_file("sentinel_reviews.jsonl")


def _legacy_approvals_path() -> str:
    return _override("LEGACY_APPROVALS") or _state_file(
        "ha_device_approvals.jsonl")


#: Resolved review-log paths already reported as unwritable, so the error is
#: logged once per path per process rather than once per review. Rate-limiting
#: matters because the failure mode is persistent, not transient -- a read-only
#: state tree fails EVERY append, and a per-review line would bury the rest of
#: the log on a box that is already in trouble. Keyed on the resolved path, not
#: a single flag, so a genuine relocation (operator sets $PUFFPAI_DATA_DIR, or
#: the file-level adoption in _state_file() picks a different file) is reported
#: afresh. Plain assignment, like the path overrides above: a reload resets it.
_unwritable_reported: set[str] = set()


def _append_review(result: dict) -> None:
    """Append one verdict to ``sentinel_reviews.jsonl``; best-effort.

    Both gates go through this single helper so their audit treatment cannot
    drift apart.

    The OSError stays swallowed on purpose. The trail RECORDS a verdict, it
    does not participate in reaching one, so an unwritable log must not raise
    into the caller and turn a routine policy check into a request failure.
    What it must not do is stay silent: after the file-level legacy
    adoption in :func:`_state_file`, this path can legitimately resolve into
    the read-only code tree on a hardened appliance, and this log is the only
    record of what the gate allowed. Losing it silently is not acceptable.
    """
    review_log = _review_log_path()
    try:
        os.makedirs(os.path.dirname(review_log), exist_ok=True)
        with open(review_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(result) + "\n")
    except OSError as exc:
        # _LOCK is reused rather than adding a second lock: no caller holds it
        # across this helper (only ConsentStore.approve/deny take it, around a
        # mutate + _save that never appends), so this cannot re-enter.
        with _LOCK:
            first = review_log not in _unwritable_reported
            _unwritable_reported.add(review_log)
        if first:
            logger.error(
                "consent gate audit trail unwritable at %s: %s; review "
                "verdicts are NOT being recorded", review_log, exc,
            )


#: Resolved consent-ledger paths already reported as unusable, so the error is
#: logged once per path per process rather than once per ConsentStore(). Same
#: shape and same reasoning as _unwritable_reported above: the failure mode is
#: persistent (a corrupt ledger stays corrupt), every gated request can build a
#: store, and a per-read line would bury the rest of the log on a box that is
#: already in trouble. Keyed on the resolved path, not a single flag, so a
#: genuine relocation ($PUFFPAI_DATA_DIR, or the file-level adoption in
#: _state_file()) is reported afresh. Plain assignment: a reload resets it.
_ledger_unreadable_reported: set[str] = set()

#: Resolved consent-ledger paths already reported as holding malformed
#: RECORDS. Kept separate from _ledger_unreadable_reported even though it keys
#: on the very same path, for exactly the reason the comment below gives for
#: keeping the two FILES apart: the two reports are different severities with
#: different remedies -- dropping a record is survivable and the ledger stands,
#: an unusable file wants a restore from backup -- so one set would let the
#: benign WARNING permanently silence the severe ERROR on that path, in either
#: order. Today ``_load`` runs about once per process (``get_store`` is a
#: singleton), which makes that latent rather than live; but the rate limiting
#: here exists only on the premise that ``_load`` CAN run repeatedly, and
#: nothing stops a caller from constructing a ``ConsentStore`` directly. Either
#: the premise or a shared key would have to go; the key goes.
_ledger_records_reported: set[str] = set()

#: Resolved legacy-approval-trail paths already reported as unreadable. Kept
#: separate from _ledger_unreadable_reported on purpose: different file,
#: different consequence, different level (WARNING -- a missing legacy trail is
#: survivable, a corrupt ledger is not), so sharing one set would let either
#: file silence the other's first report. Same keying and reload semantics.
_legacy_unreadable_reported: set[str] = set()


def _report_once(reported: set[str], path: str) -> bool:
    """True the first time ``path`` is offered to ``reported`` this process.

    Read-and-add is atomic under _LOCK so two threads racing the same failed
    read cannot both decide they are first. _LOCK is reused rather than adding
    more locks, as in :func:`_append_review`; it is an RLock, so a caller that
    already holds it cannot deadlock here. The caller does its ``logger.*``
    call OUTSIDE the lock -- a handler can block on a full disk or a socket,
    and the gate must not stall a policy check behind its own log.
    """
    with _LOCK:
        first = path not in reported
        reported.add(path)
    return first


def __getattr__(name: str) -> str:
    """Serve the former module constants (PEP 562) from the lazy resolvers.

    ``puffpai.self_check`` reads ``consent_gate.CONSENT_PATH`` at call time to
    report where consent decisions live, and the test suites patch
    ``REVIEW_LOG``; both keep working without resolving anything at import.
    """
    if name == "CONSENT_PATH":
        return _consent_path()
    if name == "REVIEW_LOG":
        return _review_log_path()
    if name == "LEGACY_APPROVALS":
        return _legacy_approvals_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

SAFETY_CRITICAL_DOMAINS = {"lock", "cover", "alarm_control_panel", "camera", "siren"}
ALLOWED_TRIGGER_TYPES = {"time", "state", "event", "sun"}
ALLOWED_ACTION_TYPES = {"turn_off", "turn_on", "toggle", "dim", "notify", "lock", "unlock"}
MAX_BLAST_RADIUS = int(os.environ.get("PUFFPAI_MAX_BLAST_RADIUS", "10"))
_EXTERNAL_MARKERS = ("http://", "https://", "webhook.site", "requestbin", "ngrok")


def enforcement_enabled() -> bool:
    return os.environ.get("PUFFPAI_ENFORCE_CONSENT", "0").lower() in ("1", "true", "yes")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────── Consent store ─────────────────────────

class ConsentStore:
    """MAC/entity-keyed approval ledger. Default status: pending."""

    def __init__(self, path: str | None = None):
        self.path = path or _consent_path()
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """Read the ledger. Anything unusable scores DEFAULT-PENDING.

        Two grades of damage, deliberately treated differently:

        * The whole FILE is unusable -- unreadable, not UTF-8, not JSON, or
          JSON that is not an object. ``self._data = {}``, reported at ERROR.
        * Individual RECORDS are malformed, e.g. a bare string where the
          ``{"status": ...}`` object belongs. Those keys are dropped and the
          rest of the ledger stands, reported at WARNING. Nuking 103 good
          approvals over one bad row would recreate the unexplained mass denial
          this whole change exists to remove.

        Both are fail-closed: any entity left without a well-formed record
        scores "pending", so no damaged file can make one look approved.
        Between the two shape checks -- the top-level ``isinstance(data, dict)``
        and the per-record one -- nothing reaching ``self._data`` can raise
        ``AttributeError`` out of :meth:`status`'s ``rec.get(...)`` later.

        The exceptions stay swallowed rather than raised for the same reason
        :func:`_append_review`'s does: the gate must still reach a verdict when
        its state file is unavailable, and raising would turn a routine policy
        check into a request failure while ALSO losing the deny. What they must
        not do is stay silent -- the JSONDecodeError case in particular
        would be indistinguishable from "no ledger yet", and those two want opposite
        responses (restore a backup / do nothing). Absence is the only
        damage that passes without a word: every other unusable state gets a
        line, at most once per path per process, and the record-level and
        file-level reports rate-limit on SEPARATE sets so the survivable one
        cannot mute the severe one on the path they share.
        """
        detail: str | None = None
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            # No ledger yet: the normal state of a fresh box, and "nothing has
            # been approved" is a definitive answer, not a failed read.
            self._data = {}
            return
        except json.JSONDecodeError as exc:
            detail = f"corrupt or truncated JSON: {exc}"
        except UnicodeDecodeError as exc:
            # A ValueError, not an OSError, so neither handler around it would
            # have caught this; named explicitly rather than broadening to bare
            # ValueError, which would swallow genuine bugs along with it.
            detail = f"not valid UTF-8: {exc}"
        except OSError as exc:
            detail = f"unreadable: {exc}"
        else:
            if isinstance(data, dict):
                # Sanitize per RECORD, not per file. status() does
                # rec.get("status") on whatever is stored here, so one string
                # where an object belongs raises AttributeError out of every
                # later gate call. Dropping just the malformed records keeps
                # the other 102 approvals working: nuking the whole ledger over
                # one bad row would recreate the mass denial this all exists to
                # remove. A dropped record scores "pending" -- still closed.
                self._data = {k: v for k, v in data.items()
                              if isinstance(v, dict)}
                dropped = len(data) - len(self._data)
                if dropped and _report_once(
                        _ledger_records_reported, self.path):
                    logger.warning(
                        "consent ledger at %s holds %d malformed record(s) "
                        "(not JSON objects); those entities revert to "
                        '"pending", the rest of the ledger stands',
                        self.path, dropped,
                    )
                return
            # Valid JSON, wrong shape -- a truncated or interrupted write can
            # leave a bare list or string behind. Nothing is salvageable from
            # that, so it IS the whole-file corrupt case.
            detail = (f"corrupt: expected a JSON object, got "
                      f"{type(data).__name__}")

        self._data = {}
        if _report_once(_ledger_unreadable_reported, self.path):
            logger.error(
                "consent ledger unusable at %s (%s); every prior approve/deny "
                'reverts to "pending" until it is restored', self.path, detail,
            )

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def status(self, entity_id: str) -> str:
        rec = self._data.get(entity_id)
        if rec:
            return rec.get("status", "pending")
        if self._in_legacy_blanket(entity_id):
            return "legacy_blanket"          # approved pre-consent — flag, don't trust
        return "pending"

    def approve(self, entity_id: str, *, by: str = "user", zone: str | None = None) -> dict:
        with _LOCK:
            self._data[entity_id] = {
                "status": "approved", "approved_by": by, "zone": zone,
                "zone3_applied": zone == "zone3", "at": _utcnow(),
            }
            self._save()
        return self._data[entity_id]

    def deny(self, entity_id: str, *, by: str = "user", reason: str = "") -> dict:
        with _LOCK:
            self._data[entity_id] = {
                "status": "denied", "denied_by": by, "reason": reason, "at": _utcnow(),
            }
            self._save()
        return self._data[entity_id]

    def pending(self, entity_ids: list[str]) -> list[str]:
        return [e for e in entity_ids if self.status(e) in ("pending", "legacy_blanket")]

    _legacy_cache: set[str] | None = None

    def _in_legacy_blanket(self, entity_id: str) -> bool:
        """True when the legacy blanket approval covers this entity.

        The read error stays swallowed for the same reason
        :func:`_append_review`'s does: the gate must still reach a verdict when
        one of its state files is unavailable, and raising would turn a routine
        policy check into a request failure while ALSO losing the deny. The
        swallowed answer is the fail-closed one -- "not covered", which scores
        "pending" -- so an unreadable trail can never make an entity look
        approved. ``UnicodeDecodeError`` counts as a read failure here: it is a
        ValueError, so it would slip past an OSError-only handler and escape
        into the caller, which is neither fail-closed nor observable.

        What it must not be is PERMANENT. The result caches on the CLASS, once
        per process, so a single transient error (a permission blip, the file
        locked by ha_device_watcher mid-write, ENFILE) must not pin the process
        to "no legacy approvals exist" with no log line and no self-healing
        short of a restart -- to an operator, unexplained mass denial. So an
        ABSENT file is cached (its absence is a definitive answer, and it is the
        hot path on a fresh box) while a FAILED READ is reported once per path
        and NOT cached, so the next call retries and a transient error heals.
        The partially-filled ``found`` is discarded with it: caching half a
        trail would deny the rest of it for the life of the process.
        """
        if ConsentStore._legacy_cache is None:
            legacy_path = _legacy_approvals_path()
            found: set[str] = set()
            try:
                with open(legacy_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            # One junk line in an append-only JSONL trail is
                            # skipped, not fatal: a torn final write must not
                            # cost the approvals recorded cleanly before it.
                            continue
                        if not isinstance(row, dict):
                            # Valid JSON, wrong shape ("null", "[1,2]"). Skipped
                            # for the same reason, and it MUST be: row.get()
                            # raises AttributeError out of status() before
                            # _legacy_cache is ever assigned, so every later
                            # gated request raises too -- permanent denial by
                            # traceback, until an operator repairs the file.
                            continue
                        eid = row.get("entity_id") or row.get("device_id")
                        if eid:
                            found.add(eid)
            except FileNotFoundError:
                # No legacy trail at all -- a fresh box. Definitive, so cache.
                ConsentStore._legacy_cache = set()
            except (OSError, UnicodeDecodeError) as exc:
                # Deliberately NOT cached: return the fail-closed answer for
                # this call only, and let the next one retry the read.
                if _report_once(_legacy_unreadable_reported, legacy_path):
                    logger.warning(
                        "legacy approval trail unreadable at %s: %s; entities "
                        'covered only by the legacy blanket approval score '
                        '"pending" (a hard deny) until this read succeeds',
                        legacy_path, exc,
                    )
                return False
            else:
                ConsentStore._legacy_cache = found
        return entity_id in ConsentStore._legacy_cache


_store: ConsentStore | None = None


def get_store() -> ConsentStore:
    global _store
    if _store is None:
        _store = ConsentStore()
    return _store


# ─────────────────────── Schema validation ───────────────────────

_REQUIRED_KEYS = ("name", "trigger_type", "trigger_value", "action_type",
                  "action_entity_domain", "action_entity_hint")


def validate_automation(parsed: Any) -> tuple[bool, dict, list[str]]:
    """Validate + normalize an LLM-parsed automation. Returns (ok, normalized, errors)."""
    errors: list[str] = []
    if not isinstance(parsed, dict):
        return False, {}, ["parsed result is not a JSON object"]
    norm = dict(parsed)
    for key in _REQUIRED_KEYS:
        val = norm.get(key)
        if not isinstance(val, str) or not val.strip():
            errors.append(f"missing/empty required key: {key}")
    if isinstance(norm.get("trigger_type"), str) and norm["trigger_type"] not in ALLOWED_TRIGGER_TYPES:
        errors.append(f"trigger_type '{norm['trigger_type']}' not in {sorted(ALLOWED_TRIGGER_TYPES)}")
    if isinstance(norm.get("action_type"), str) and norm["action_type"] not in ALLOWED_ACTION_TYPES:
        errors.append(f"action_type '{norm['action_type']}' not in {sorted(ALLOWED_ACTION_TYPES)}")
    conds = norm.get("conditions", [])
    if conds and not isinstance(conds, list):
        errors.append("conditions must be a list")
    for key, val in list(norm.items()):
        if isinstance(val, str) and len(val) > 500:
            norm[key] = val[:500]
    hint = norm.get("action_entity_hint", "")
    if isinstance(hint, str) and hint and "." not in hint and "*" not in hint:
        errors.append("action_entity_hint must be a glob or entity_id (contains neither '.' nor '*')")
    return (not errors), norm, errors


# ─────────────────────── Sentinel review gate ───────────────────────

def sentinel_review(parsed: dict, states: list | None, task_text: str = "") -> dict:
    """Policy verdict on an agent-authored automation.

    Returns {action: allow|flag|deny, binding: bool, reasons: [...], targets: [...]}.
    ``binding`` is False unless PUFFPAI_ENFORCE_CONSENT=1 (dry-run default).
    A verdict is logged to sentinel_reviews.jsonl either way.
    """
    reasons: list[str] = []
    verdict = "allow"
    entity_ids = sorted({s.get("entity_id", "") for s in states} - {""}) if states else []

    hint = parsed.get("action_entity_hint", "") or ""
    domain = parsed.get("action_entity_domain", "") or ""
    targets = [e for e in entity_ids if fnmatch.fnmatch(e, hint)] if hint else []
    if not targets and hint and "." in hint and "*" not in hint:
        targets = [hint]  # explicit entity_id form

    # 1. Consent: every targeted entity must be approved
    store = get_store()
    unapproved = [t for t in targets if store.status(t) not in ("approved",)]
    legacy = [t for t in unapproved if store.status(t) == "legacy_blanket"]
    if unapproved:
        verdict = "flag"
        if legacy:
            reasons.append(f"{len(legacy)} target(s) only covered by the legacy "
                           f"blanket approval (no per-device consent): {legacy[:5]}")
        hard_pending = [t for t in unapproved if t not in legacy]
        if hard_pending:
            verdict = "deny"
            reasons.append(f"target(s) not consented: {hard_pending[:5]}")

    # 2. Blast radius
    if len(targets) > MAX_BLAST_RADIUS:
        verdict = "deny"
        reasons.append(f"blast radius {len(targets)} exceeds limit {MAX_BLAST_RADIUS}")

    # 3. Safety-critical domains need explicit consent, never legacy
    if domain in SAFETY_CRITICAL_DOMAINS and (unapproved or not targets):
        verdict = "deny"
        reasons.append(f"safety-critical domain '{domain}' requires per-device approval")

    # 4. No external/cloud egress in agent-authored automations
    blob = json.dumps(parsed).lower()
    hits = [m for m in _EXTERNAL_MARKERS if m in blob]
    if hits:
        verdict = "deny"
        reasons.append(f"external egress markers in automation: {hits}")

    # 5. Unmatched glob = the LLM hallucinated a target
    if hint and not targets:
        verdict = "deny"
        reasons.append(f"action_entity_hint '{hint}' matches no known entity")

    result = {
        "action": verdict,
        "binding": enforcement_enabled(),
        "reasons": reasons,
        "targets": targets,
        "reviewed_at": _utcnow(),
        "task_text": task_text[:200],
        "automation_name": parsed.get("name", ""),
    }
    _append_review(result)
    return result


# ─────────────── Target-list gate ───────────────
# Used by the automation *authoring* paths in api_server, where entity
# targets are already resolved (no hint/glob to re-match). Same policy
# pillars as sentinel_review: per-entity consent, blast radius,
# safety-critical domains. Same audit log, same enforcement flag.

def gate_targets(
    targets: list[str],
    *,
    name: str = "",
    task_text: str = "",
    authored_by: str = "agent",
) -> dict:
    """Consent-gate a resolved list of target entity_ids before an
    automation touching them is created in HA.

    Returns {action: allow|flag|deny, binding, reasons, targets, ...}.
    Callers MUST skip creation when action == "deny" and binding is True,
    and SHOULD attach the verdict to their response either way so dry-run
    denials are visible before PUFFPAI_ENFORCE_CONSENT is flipped.
    """
    reasons: list[str] = []
    verdict = "allow"
    targets = sorted({t for t in targets if t})
    store = get_store()

    unapproved = [t for t in targets if store.status(t) != "approved"]
    legacy = [t for t in unapproved if store.status(t) == "legacy_blanket"]
    hard_pending = [t for t in unapproved if t not in legacy]
    if legacy:
        verdict = "flag"
        reasons.append(
            f"{len(legacy)} target(s) only covered by the legacy blanket "
            f"approval (no per-device consent): {legacy[:5]}")
    if hard_pending:
        verdict = "deny"
        reasons.append(f"target(s) not consented: {hard_pending[:5]}")

    if len(targets) > MAX_BLAST_RADIUS:
        verdict = "deny"
        reasons.append(f"blast radius {len(targets)} exceeds limit {MAX_BLAST_RADIUS}")

    critical = [t for t in targets if t.split(".")[0] in SAFETY_CRITICAL_DOMAINS]
    critical_unapproved = [t for t in critical if store.status(t) != "approved"]
    if critical_unapproved:
        verdict = "deny"
        reasons.append(
            f"safety-critical target(s) require per-device approval: "
            f"{critical_unapproved[:5]}")

    result = {
        "action": verdict,
        "binding": enforcement_enabled(),
        "reasons": reasons,
        "targets": targets,
        "reviewed_at": _utcnow(),
        "task_text": task_text[:200],
        "automation_name": name,
        "authored_by": authored_by,
        "gate": "gate_targets",
    }
    _append_review(result)
    return result
