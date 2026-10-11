"""
PuffPAI Dashboard Tile Registry — dynamic camera tiles.

Agent flow (Cortex / Bridge / Sentinel):
  - Cortex decides to add a camera (assistant tool ``dashboard_add_camera``).
  - Bridge (ha_bridge) resolves the user's words to a real HA camera entity
    — closed-world: if HA doesn't expose it, nothing gets added.
  - Sentinel (security.consent_gate ConsentStore) must not have the entity
    hard-denied/pending; every verdict is audited to
    security_logs/dashboard_tile_reviews.jsonl.

The Next.js dashboard reads tiles via GET /dashboard/cameras (api_server).
Registry persists at data/dashboard_cameras.json ($PUFFPAI_DATA_DIR when set).

Copyright (c) 2026 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from puffpai.statedir import resolve_runtime_dir

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Both paths are resolved per call, never frozen at import: under the hardened
# systemd unit only $PUFFPAI_DATA_DIR is writable, and an import-time constant
# would also drag puffpai.config (and the secrets provider) into the import
# graph. Each name below survives as a test/ops override hook — set it to a
# path and the accessor returns that verbatim; leave it None and the env
# decides, defaulting to the historical in-tree location.
_REGISTRY_PATH: str | None = None
_AUDIT_PATH: str | None = None


def _registry_path() -> str:
    """Camera registry — historically a ``data/`` file, so a relocated data dir
    holds it at its root (same convention as agent_events.jsonl)."""
    if _REGISTRY_PATH:
        return _REGISTRY_PATH
    return os.path.join(
        resolve_runtime_dir(os.path.join(_REPO_ROOT, "data")),
        "dashboard_cameras.json")


def _audit_path() -> str:
    """Sentinel verdict trail — a security log, so it keeps its own subtree
    under a relocated data dir (same convention as security/audit_log.py)."""
    if _AUDIT_PATH:
        return _AUDIT_PATH
    return os.path.join(
        resolve_runtime_dir(os.path.join(_REPO_ROOT, "security_logs"),
                            env_subdir="security_logs"),
        "dashboard_tile_reviews.jsonl")


_lock = threading.Lock()

# A fresh install has no camera tiles; the owner adds their own cameras (by
# voice or from the dashboard) and they are saved to the registry file.
_SEED: list[dict] = []


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> list[dict]:
    path = _registry_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, IOError):
            pass
    return [dict(x) for x in _SEED]


def _save(items: list[dict]) -> None:
    path = _registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _audit(verdict: str, entity_id: str, reasons: list[str], requested_by: str,
           task_text: str = "") -> None:
    try:
        path = _audit_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _utcnow(),
                "action": "dashboard_add_camera",
                "verdict": verdict,
                "entity_id": entity_id,
                "reasons": reasons,
                "requested_by": requested_by,
                "task_text": task_text[:300],
            }) + "\n")
        # Hold this security log at 0640 like the egress ledgers, not the
        # default 0644. One definition of the mode lives in egress_ledger;
        # never raises.
        try:
            from puffpai.security.egress_ledger import ensure_secure_mode
            ensure_secure_mode(path)
        except Exception:
            pass
    except IOError:
        pass  # auditing must never break the user path


def list_cameras() -> list[dict]:
    with _lock:
        return _load()


def remove_camera(entity_id: str) -> bool:
    with _lock:
        items = _load()
        remaining = [x for x in items if x.get("entity_id") != entity_id]
        if len(remaining) == len(items):
            return False
        _save(remaining)
        return True


def _default_label(friendly_name: str | None, entity_id: str) -> str:
    if friendly_name:
        return friendly_name
    name = entity_id.split(".", 1)[-1]
    return name.replace("_", " ").title()


def find_camera_entity(bridge, query: str):
    """Bridge step: resolve a user phrase to HA camera DeviceInfo objects.

    Returns a list of matching DeviceInfo (camera domain only). Closed-world:
    only entities Home Assistant actually exposes can ever match.
    """
    # controllable_only=False: cameras aren't in CONTROLLABLE_DOMAINS but we
    # only *view* them, never actuate.
    try:
        devices = bridge.list_devices(domain="camera", controllable_only=False)
    except TypeError:  # mock/legacy bridges without the kwargs
        devices = bridge.list_devices()
    devices = [d for d in devices if d.entity_id.startswith("camera.")]
    q = (query or "").strip().lower().strip("'\"")
    if not q:
        return devices
    # Exact entity_id
    exact = [d for d in devices if d.entity_id.lower() == q]
    if exact:
        return exact
    # Token overlap on friendly name / entity id
    tokens = [t for t in q.replace(".", " ").replace("_", " ").split()
              if t not in ("camera", "cam", "the", "my", "new", "a", "reolink", "feed")]
    if not tokens:
        return devices

    def score(d) -> int:
        hay = f"{d.friendly_name} {d.entity_id}".lower().replace("_", " ")
        return sum(1 for t in tokens if t in hay)

    scored = sorted(((score(d), d) for d in devices), key=lambda x: -x[0])
    best = [d for s, d in scored if s > 0 and s == scored[0][0]]
    return best


def sentinel_check(entity_id: str, requested_by: str, task_text: str = "",
                   label: str | None = None) -> tuple[bool, str]:
    """Sentinel step: consent verdict for exposing a camera tile.

    Mirrors consent_gate.sentinel_review semantics: approved or
    legacy_blanket -> allow (legacy is flagged), anything else -> deny.
    Every verdict is audited.
    """
    try:
        from puffpai.security.consent_gate import get_store
        status = get_store().status(entity_id)
    except Exception as exc:  # consent store unavailable — fail closed
        _audit("deny", entity_id, [f"consent store unavailable: {exc}"],
               requested_by, task_text)
        return False, f"Sentinel could not verify consent for {entity_id}: {exc}"

    if status == "approved":
        _audit("allow", entity_id, ["per-device consent on file"],
               requested_by, task_text)
        return True, "approved (per-device consent)"
    if status == "legacy_blanket":
        _audit("flag", entity_id,
               ["covered only by the legacy blanket approval"],
               requested_by, task_text)
        return True, ("allowed under the legacy blanket approval — consider "
                      "granting per-device consent")
    # No consent yet — don't dead-end: file a consent request and have the
    # LLM ask the user for permission on Sentinel's behalf.
    _audit("deny", entity_id, [f"consent status: {status} — consent request filed"],
           requested_by, task_text)
    nice = label or entity_id
    try:
        from puffpai.security import consent_requests as _creq
        _creq.create_request(
            entity_id, nice, "dashboard_add_camera",
            f"no per-device consent (status: {status})",
            requested_by="sentinel",
            context={"requested_by": requested_by, "task_text": task_text[:200]})
    except Exception:
        pass
    return False, (
        f"SENTINEL NEEDS USER PERMISSION: {nice} ({entity_id}) has no "
        f"per-device consent yet, so Sentinel filed a consent request. "
        f"Ask the user directly: 'Sentinel needs your permission to show "
        f"{nice} on the dashboard — should I approve it?' If they say yes, "
        f"call sentinel_approve_device; if no, call sentinel_deny_device.")


def add_camera_validated(bridge, query: str, label: str | None = None,
                         added_by: str = "agent", task_text: str = "") -> tuple[bool, str]:
    """Full Bridge -> Sentinel -> registry pipeline. Returns (ok, message)."""
    from puffpai.ha_bridge import HABridgeError
    try:
        matches = find_camera_entity(bridge, query)
    except HABridgeError as exc:
        return False, f"Home Assistant is unreachable: {exc}"

    if not matches:
        _audit("deny", query or "(none)",
               ["no matching camera entity in Home Assistant (closed world)"],
               added_by, task_text)
        return False, (
            f"I couldn't find a camera matching '{query}' in Home Assistant. "
            "If the Reolink hub sees it but HA doesn't yet, reload the "
            "Reolink integration (Settings > Devices & Services > Reolink > "
            "Reload) or check whether the new entity is disabled, then ask "
            "me again."
        )

    on_dash = {x["entity_id"] for x in list_cameras()}
    new = [d for d in matches if d.entity_id not in on_dash]
    if not new:
        only = matches[0]
        return False, (f"{only.friendly_name} ({only.entity_id}) is already "
                       "on the dashboard.")
    if len(new) > 1:
        opts = "\n".join(f"  - {d.friendly_name} ({d.entity_id})" for d in new[:6])
        return False, ("More than one camera matches — which one?\n" + opts)

    dev = new[0]
    ok, verdict = sentinel_check(dev.entity_id, added_by, task_text,
                                 label=_default_label(dev.friendly_name, dev.entity_id))
    if not ok:
        return False, verdict

    entry = {
        "entity_id": dev.entity_id,
        "label": label or _default_label(dev.friendly_name, dev.entity_id),
        "added_by": added_by,
        "added_at": _utcnow(),
    }
    with _lock:
        items = _load()
        items.append(entry)
        _save(items)
    return True, (f"Done — {entry['label']} ({dev.entity_id}) is now live on "
                  f"the dashboard. Sentinel verdict: {verdict}.")
