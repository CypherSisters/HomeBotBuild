"""The one place a Zone 3 policy engine is built.

Why this module exists
----------------------
Five call sites need an engine: ``api_server._get_zone3()`` and the four
``zone3_*`` LLM tools in ``assistant_tools``. If each built its own (the tools,
say, against ``PiholeController``, a sinkhole the appliance does not run), the
dashboard and the HTTP routes would report the truth about AdGuard while
PuffPAI's own voice answered from a Pi-hole that does not exist, and "turn
enforcement on" through the chat path would change nothing the household
could observe.

One factory, one singleton, one answer. Every surface — HTTP route, LLM tool,
dashboard — asks :func:`get_engine` and gets the same object, so they cannot
disagree about what is being blocked.

Where the enforce flag lives
----------------------------
``enforce`` is the difference between "PuffPAI is watching" and "PuffPAI is
blocking your television". Kept only in ``config/puffpai_config.json``
(``zone3.adguard_enforce``), which on a hardened appliance is outside the unit's
``ReadWritePaths``, ``POST /zone3/enforce`` could flip the running object
and nothing more, and a restart would silently put the house back to observe:
an owner who turned filtering on would find it off after a power cut, with no
indication it had ever been on.

The storage mirrors the egress doors exactly (see
:mod:`puffpai.security.egress_admin`): the owner's answer is written to a small
JSON overlay in the data directory the service already writes,
``$PUFFPAI_DATA_DIR/state/zone3_enforce.json``. Precedence is

    env (``PUFFPAI_ZONE3_ENFORCE``) > owner overlay > config > default-off

and :func:`enforce_source` says which tier decided, so a switch that looks dead
can be explained instead of guessed at. Default-off is deliberate: a Zone 3
whose state cannot be read is observing, never blocking. Failing closed here
would mean failing *into* silently filtering a household's DNS.

The overlay is NOT the appliance config on purpose. Adding
``/opt/puffpai/config`` to ``ReadWritePaths`` would let an RCE in the API server
rewrite every appliance setting — a large posture change bought to persist one
boolean. This file holds one boolean.

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: Environment variable that forces the flag regardless of any file. Present so
#: a deployment can pin observe-only (or pin enforcement) without depending on
#: what an API call did last, and so a test can pin it without touching disk.
ENFORCE_ENV = "PUFFPAI_ZONE3_ENFORCE"

#: Overlay filename under the resolved state directory.
OVERLAY_NAME = "zone3_enforce.json"

#: Owner-only. The file records a household security decision; there is no
#: reason for any other account on the appliance to read it.
OVERLAY_FILE_MODE = 0o600
OVERLAY_DIR_MODE = 0o700

#: Marker written into the file so a human (or a future migration) can tell at
#: a glance what it is without inferring from the filename.
OVERLAY_KIND = "puffpai.zone3.enforce"

_engine = None
_lock = threading.RLock()

#: Test seam. When set, :func:`overlay_path` returns this verbatim. Set through
#: :func:`set_overlay_path_for_tests` so a test never has to relocate the whole
#: data dir to exercise one boolean.
_OVERLAY_OVERRIDE: str | None = None


# ── where the flag is written ────────────────────────────────────────────────

def overlay_path() -> str:
    """Absolute path to the owner overlay holding the enforce flag."""
    if _OVERLAY_OVERRIDE:
        return _OVERLAY_OVERRIDE
    from puffpai.statedir import resolve_runtime_dir
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    return os.path.join(
        resolve_runtime_dir(os.path.join(repo_root, "state"), env_subdir="state"),
        OVERLAY_NAME)


def set_overlay_path_for_tests(path: str | None) -> None:
    """Point the overlay at *path* (or clear the override). Tests only."""
    global _OVERLAY_OVERRIDE
    _OVERLAY_OVERRIDE = path


def _env_override() -> bool | None:
    """The env tier, or None when unset/unparseable.

    An unparseable value is treated as unset rather than as False: a typo in a
    unit file should not read as a deliberate decision about the household's
    DNS. It is logged so the typo is discoverable.
    """
    raw = os.environ.get(ENFORCE_ENV)
    if raw is None:
        return None
    low = raw.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    logger.warning("%s=%r is not a boolean; ignoring it", ENFORCE_ENV, raw)
    return None


def read_overlay() -> bool | None:
    """The owner overlay's flag: True/False, or None when absent or broken.

    A broken overlay reads as *absent*, not as False, so the next tier down
    decides. It is logged, and :func:`enforce_source` will say ``"config"`` —
    which is the honest report, because that is what actually decided.
    """
    path = overlay_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("not a JSON object")
        value = loaded.get("enforce")
        if not isinstance(value, bool):
            raise ValueError("'enforce' is not a boolean")
        return value
    except Exception as exc:
        logger.warning("Zone 3 enforce overlay at %s is unreadable (%s); "
                       "falling back to config", path, exc)
        return None


def write_overlay(value: bool) -> str:
    """Record the owner's answer. Returns the path written.

    Atomic (temp file, fsync, chmod before rename) so a crash mid-write leaves
    the previous answer intact rather than a truncated file that reads as
    absent.
    """
    path = overlay_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, OVERLAY_DIR_MODE)
    except OSError:
        pass  # Windows implements a subset of chmod
    payload = {
        "version": 1,
        "kind": OVERLAY_KIND,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "enforce": bool(value),
    }
    tmp = os.path.join(directory, f".{OVERLAY_NAME}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, OVERLAY_FILE_MODE)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def _config_enforce() -> bool:
    try:
        from puffpai.config import get_config
        return bool(get_config().zone3.adguard_enforce)
    except Exception:
        return False


def resolve_enforce() -> bool:
    """The flag every caller should use: env > overlay > config > off."""
    env = _env_override()
    if env is not None:
        return env
    overlay = read_overlay()
    if overlay is not None:
        return overlay
    return _config_enforce()


def enforce_source() -> str:
    """Which tier decided the current value: env / overlay / config."""
    if _env_override() is not None:
        return "env"
    if read_overlay() is not None:
        return "overlay"
    return "config"


# ── the engine ───────────────────────────────────────────────────────────────

def get_engine():
    """The Zone 3 policy engine, or None when Zone 3 is disabled.

    Lazy singleton. ``enforce`` is baked into the AdGuard controller at
    construction from :func:`resolve_enforce`, so monitor-first is the state of
    the OBJECT — not a flag every call site has to remember to check.

    Returns None (rather than raising) when ``zone3.enabled`` is false or the
    controller cannot be constructed: Zone 3 being off is an ordinary
    deployment state, and every caller already has to render "not enabled".
    """
    global _engine
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        try:
            from puffpai.config import get_config
            cfg = get_config()
            if not cfg.zone3.enabled:
                return None
            from puffpai.zone3 import AdGuardController, PolicyEngine
            enforce = resolve_enforce()
            adguard = AdGuardController(
                base_url=cfg.zone3.adguard_url,
                username=cfg.zone3.adguard_username,
                password=cfg.zone3.adguard_password,
                enforce=bool(enforce),
            )
            _engine = PolicyEngine(adguard=adguard)
            # URL and mode only. Never the credential.
            logger.info("Zone 3 engine initialized (AdGuard: %s, enforce=%s "
                        "from %s)", cfg.zone3.adguard_url, bool(enforce),
                        enforce_source())
        except Exception as exc:
            logger.warning("Zone 3 init failed: %s", exc)
            return None
    return _engine


#: Longest per-device listing an enforce proposal renders before it collapses
#: the tail into "+N more". A proposal is meant to be READ before it is
#: approved; a hundred-line wall is skimmed, and a skimmed proposal is a
#: consent step that has stopped working. Twenty-five fits a screen.
ENFORCE_PREVIEW_LIMIT = 25


#: Appended to any preview row a chat tool recorded. Exact wording is asserted
#: in tests — it is the only place an owner is told that a rule about to go
#: live was written by the model rather than by them.
_TOOL_RECORDED_NOTE = "  (recorded by a chat tool, not yet approved)"


def _device_label(device: dict) -> str:
    """A name a person recognises, falling back to the IP."""
    for key in ("hostname", "manufacturer", "category"):
        value = str(device.get(key) or "").strip()
        if value and value.lower() != "unknown":
            return value
    return str(device.get("ip") or "unknown device")


def enforce_preview(engine=None, limit: int = ENFORCE_PREVIEW_LIMIT) -> dict:
    """What flipping enforcement ON would actually put live, device by device.

    Built here, not in the route or the tool, because a count is not a
    proposal. "About 42 recorded rule(s) go live" tells an owner nothing about
    WHICH devices are about to start being refused, and the one fact they most
    need — that the medical device and the fire panel are never blocked — is
    invisible in a total. Both surfaces (``POST /zone3/enforce``'s 409 and the
    ``zone3_enforce`` tool's spoken proposal) render this same structure, so
    the voice answer and the dashboard cannot describe different changes.

    ``rules`` per device is that device's recorded domain count — the domains
    its mode refuses. They sum to ``rules`` overall, which is the same number
    ``get_zone3_status()`` reports as ``total_recorded_domains``.

    Read-only: no controller call, no write, no ``sync_dns``. Minting must be
    free of side effects or the confirmation step becomes the thing it gates.
    """
    if engine is None:
        engine = get_engine()
    preview = {"managed": 0, "critical": 0, "rules": 0,
               "devices": [], "more": 0, "lines": [],
               "buckets": [], "bucket_rules": 0, "tool_recorded": 0,
               "breaks_integrations": []}
    if engine is None:
        return preview

    try:
        devices = list(engine.get_managed_devices())
    except Exception as exc:                     # a read that fails must not
        logger.warning("Zone 3 enforce preview unavailable: %s", exc)
        return preview                           # block the proposal

    rows = []
    for device in devices:
        mode = str(device.get("mode") or "balanced").lower()
        never_blocked = bool(device.get("is_safety_critical")) or \
            mode == "always_allow"
        count = 0 if never_blocked else len(device.get("blocked_domains") or [])
        rows.append({
            "name": _device_label(device),
            "ip": str(device.get("ip") or ""),
            "mode": mode.upper(),
            "rules": count,
            "never_blocked": never_blocked,
            # See DevicePolicy.recorded_by. An older ledger row has no field;
            # absent reads as "owner", because inventing model-authorship for
            # rows we cannot attribute would make the marking meaningless.
            "recorded_by": str(device.get("recorded_by") or "owner"),
        })

    preview["managed"] = len(rows)
    preview["critical"] = sum(1 for r in rows if r["never_blocked"])
    preview["rules"] = sum(r["rules"] for r in rows)
    preview["tool_recorded"] = sum(
        1 for r in rows if r["recorded_by"] == "tool" and not r["never_blocked"])

    # Never-blocked devices first: on a proposal to start filtering a house,
    # "your fire panel is exempt" is the line that most needs to survive the
    # cap. Then loudest first, so the biggest change is never the one trimmed.
    rows.sort(key=lambda r: (not r["never_blocked"], -r["rules"], r["ip"]))
    shown = rows[:max(0, int(limit))]
    preview["devices"] = shown
    preview["more"] = len(rows) - len(shown)

    lines = []
    for row in shown:
        where = f"{row['name']} ({row['ip']})" if row["name"] != row["ip"] \
            else row["ip"]
        if row["never_blocked"]:
            lines.append(f"{where} — ALWAYS_ALLOW, never blocked")
        else:
            # The provenance marker is the reason this preview exists at all:
            # flipping the switch turns EVERY recorded rule live at once,
            # including rows a chat tool wrote while nobody was watching, and
            # a row the owner never asked for should not be indistinguishable
            # from one they did.
            mark = (_TOOL_RECORDED_NOTE
                    if row.get("recorded_by") == "tool" else "")
            lines.append(
                f"{where} — {row['mode']}, {row['rules']} rule(s){mark}")
    if preview["more"]:
        lines.append(f"+{preview['more']} more device(s)")

    # ── Home Assistant integrations ───────────────────────────────
    #
    # Listed as their own rows, not folded into the device count. A bucket
    # rule's subject is the HUB, so it does not belong to any device row; and
    # a LOCKDOWN bucket is the one change in Zone 3 that deliberately BREAKS
    # something the household uses. That has to be said in the proposal, in
    # those words, before the switch is flipped — not discovered afterwards
    # when the lights stop answering.
    try:
        managed_buckets = list(engine.get_managed_buckets())
    except Exception as exc:
        logger.warning("Zone 3 bucket preview unavailable: %s", exc)
        managed_buckets = []

    bucket_rows = []
    for bucket in managed_buckets:
        mode = str(bucket.get("mode") or "balanced").lower()
        count = (0 if mode == "always_allow"
                 else len(bucket.get("blocked_domains") or []))
        bucket_rows.append({
            "id": str(bucket.get("bucket_id") or ""),
            "integration": str(bucket.get("integration") or ""),
            "devices": int(bucket.get("device_count") or 0),
            "mode": mode.upper(),
            "rules": count,
            "breaks_integration": mode == "lockdown" and count > 0,
            "recorded_by": str(bucket.get("recorded_by") or "owner"),
        })
    bucket_rows.sort(key=lambda b: (not b["breaks_integration"],
                                    -b["rules"], b["id"]))
    preview["buckets"] = bucket_rows
    preview["bucket_rules"] = sum(b["rules"] for b in bucket_rows)
    preview["tool_recorded"] += sum(1 for b in bucket_rows
                                    if b["recorded_by"] == "tool"
                                    and b["rules"])
    preview["breaks_integrations"] = [b["integration"] for b in bucket_rows
                                      if b["breaks_integration"]]

    for row in bucket_rows:
        mark = _TOOL_RECORDED_NOTE if row["recorded_by"] == "tool" else ""
        where = (f"{row['integration']} ({row['devices']} device(s), via "
                 f"Home Assistant)")
        if not row["rules"]:
            lines.append(f"{where} — {row['mode']}, nothing blocked{mark}")
        elif row["breaks_integration"]:
            lines.append(f"{where} — LOCKDOWN, {row['rules']} rule(s) — "
                         f"THIS BREAKS THE INTEGRATION{mark}")
        else:
            lines.append(
                f"{where} — {row['mode']}, {row['rules']} rule(s){mark}")

    preview["lines"] = lines
    return preview


def reset_engine() -> None:
    """Drop the singleton so the next :func:`get_engine` rebuilds it.

    Used by tests and by the enforce route after the flag changes tier — the
    controller carries ``enforce`` as construction state, so re-reading it means
    rebuilding rather than reaching in and mutating a private attribute.
    """
    global _engine
    with _lock:
        _engine = None


def set_enforce_persistent(enabled: bool) -> dict:
    """Flip observe <-> enforce AND record the answer so a restart keeps it.

    Order matters: the overlay is written FIRST, then the running engine is
    pushed. If the push fails, the recorded intent and the running state
    disagree — but they disagree in the direction where the *record* is what the
    owner asked for and the next restart converges on it, and the failure is
    reported. Pushing first and recording second would leave the opposite: a
    house that is being filtered with nothing on disk saying so.

    Returns the engine's own result dict plus ``persistent``, ``persisted_to``
    and ``source``.
    """
    engine = get_engine()
    if engine is None:
        return {"success": False, "message": "Zone 3 not enabled",
                "persistent": False}

    env = _env_override()
    if env is not None and env != bool(enabled):
        # Real write, completely inert — say so rather than let an owner
        # conclude the switch is broken.
        return {
            "success": False,
            "persistent": False,
            "message": (
                f"{ENFORCE_ENV}={os.environ.get(ENFORCE_ENV)!r} is forcing "
                f"enforcement {'on' if env else 'off'}. Unset it to control "
                "this from the dashboard."),
        }

    persisted_to = None
    persist_error = None
    try:
        persisted_to = write_overlay(bool(enabled))
    except Exception as exc:
        persist_error = str(exc)
        logger.error("Zone 3 enforce flag could not be recorded: %s", exc)

    result = dict(engine.set_enforce(bool(enabled)))
    result["persistent"] = persisted_to is not None
    result["persisted_to"] = persisted_to
    result["source"] = enforce_source()
    if persist_error:
        result["success"] = False
        result["persist_error"] = persist_error
        result["message"] = (
            (result.get("message") or "") +
            " — but the setting could not be written to disk, so a restart "
            "will put it back. " + persist_error).strip()
    return result
