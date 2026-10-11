"""
PuffPAI Zone 3 (Bridge) — HTTP surface
======================================

A Flask Blueprint mounted at ``/bridge`` exposing the device inventory and the
local telemetry ledger to the dashboard's Drawbridge panel.

Authentication
--------------
There is no per-route decorator to apply: ``api_server`` gates the whole app
with two ``before_request`` hooks — ``_rate_limit`` then ``_check_api_key``
(api_server.py:905-906). Flask runs app-level ``before_request`` hooks for
blueprint routes exactly as it does for ``@app.route`` ones, so registering
this blueprint puts every ``/bridge/*`` path behind the same API key the rest
of the API uses, with the same rate limit, and nothing here may weaken that.
None of these endpoints are added to ``_PUBLIC_ENDPOINTS`` or to
``endpoint_gate.LOCAL_ONLY_ENDPOINTS``.

Every route is read-only with one exception: ``POST /bridge/refresh`` re-scans
the LAN and pulls the AdGuard query log. That still changes nothing outside
PuffPAI's own data directory — no filter rules, no blocks, no quarantine.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

bridge_bp = Blueprint("bridge", __name__, url_prefix="/bridge")


def _hours() -> int:
    """``?hours=`` clamped to 1..168. A bad value reads as the 24h default."""
    try:
        value = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        return 24
    return max(1, min(168, value))


@bridge_bp.route("/devices", methods=["GET"])
def bridge_devices():
    """The merged device roll. Served from the persisted inventory, not a scan."""
    try:
        from puffpai.bridge_inventory import get_inventory
        inventory = get_inventory()
        devices = inventory.devices()
        return jsonify({
            "success": True,
            "devices": devices,
            "count": len(devices),
            **inventory.status(),
        })
    except Exception:
        logger.exception("bridge devices failed")
        return jsonify({"success": False, "error": "device roll unavailable",
                        "code": "bridge_devices_failed"}), 500


@bridge_bp.route("/refresh", methods=["POST"])
def bridge_refresh():
    """Re-scan every source now, then pull the query log. Observe-only."""
    try:
        from puffpai.bridge_inventory import get_inventory
        devices = get_inventory().refresh()
    except Exception:
        logger.exception("bridge refresh failed")
        return jsonify({"success": False, "error": "device refresh failed",
                        "code": "bridge_refresh_failed"}), 500

    # A ledger pull failing must not fail the refresh: the device roll is the
    # part that works without AdGuard deployed.
    pull: dict = {"pulled": 0, "written": 0, "zone3": False}
    try:
        from puffpai.bridge_ledger import get_ledger
        pull = get_ledger().pull()
    except Exception:
        logger.exception("telemetry pull skipped")
        pull = {"pulled": 0, "written": 0, "zone3": False,
                "error": "telemetry pull failed"}

    return jsonify({
        "success": True,
        "devices": devices,
        "count": len(devices),
        "telemetry": pull,
    })


@bridge_bp.route("/telemetry/summary", methods=["GET"])
def bridge_telemetry_summary():
    """Per-device 24h (or ``?hours=``) rollup from the local ledger."""
    hours = _hours()
    try:
        from puffpai.bridge_ledger import get_ledger
        return jsonify({"success": True, **get_ledger().summary(hours)})
    except Exception:
        logger.exception("bridge telemetry summary failed")
        return jsonify({"success": False, "error": "telemetry summary unavailable",
                        "code": "bridge_telemetry_summary_failed"}), 500


@bridge_bp.route("/telemetry/device/<path:mac>", methods=["GET"])
def bridge_telemetry_device(mac: str):
    """One device's rollup. Accepts a MAC, an inventory key, or an IP."""
    hours = _hours()
    try:
        from puffpai.bridge_ledger import get_ledger
        summary = get_ledger().device_summary(mac, hours)
    except Exception:
        logger.exception("bridge telemetry device failed")
        return jsonify({"success": False, "error": "device telemetry unavailable",
                        "code": "bridge_telemetry_device_failed"}), 500

    device = None
    try:
        from puffpai.bridge_inventory import get_inventory
        device = get_inventory().get(mac)
    except Exception as exc:
        logger.info("inventory lookup for %s failed: %s", mac, exc)

    return jsonify({"success": True, "device": device, **summary})


@bridge_bp.route("/ha/buckets", methods=["GET"])
def bridge_ha_buckets():
    """The devices Home Assistant manages, bucketed by integration and tier."""
    hours = _hours()
    try:
        from puffpai import bridge_ha
        rows = bridge_ha.buckets(hours)
        return jsonify({"success": True, "hours": hours, "buckets": rows,
                        "counts": bridge_ha.counts(hours)})
    except Exception:
        logger.exception("bridge ha buckets failed")
        return jsonify({"success": False, "error": "managed device view unavailable",
                        "code": "bridge_ha_buckets_failed"}), 500


@bridge_bp.route("/ha/bucket/<path:bucket_id>", methods=["GET"])
def bridge_ha_bucket(bucket_id: str):
    """One bucket's devices, grouped by room, with their entities.

    A malformed id is a 404 and not a 400: the shape of the id is not a secret
    worth confirming, and "there is no such bucket" is the honest answer to
    both a typo and a probe.
    """
    hours = _hours()
    try:
        from puffpai import bridge_ha
        detail = bridge_ha.bucket(bucket_id, hours)
    except Exception:
        logger.exception("bridge ha bucket failed")
        return jsonify({"success": False, "error": "bucket unavailable",
                        "code": "bridge_ha_bucket_failed"}), 500
    if detail is None:
        return jsonify({"success": False, "error": "no such bucket",
                        "code": "bridge_ha_bucket_not_found"}), 404
    return jsonify({"success": True, "hours": hours, "bucket": detail})


@bridge_bp.route("/status", methods=["GET"])
def bridge_status():
    """Inventory age, source health, and the Zone 3 / enforcement posture."""
    payload: dict = {"success": True, "observe_only": True}

    try:
        from puffpai.bridge_inventory import get_inventory, is_enabled
        payload["inventory"] = get_inventory().status()
        payload["inventory_enabled"] = is_enabled()
    except Exception:
        logger.exception("bridge status: inventory unavailable")
        payload["inventory"] = {"error": "inventory unavailable"}
        payload["inventory_enabled"] = False

    try:
        from puffpai import bridge_ha
        payload["ha_buckets"] = bridge_ha.counts()
    except Exception:
        logger.exception("bridge status: HA buckets unavailable")
        payload["ha_buckets"] = {"buckets": 0, "devices": 0, "entities": 0,
                                 "cloud_buckets": 0, "lookups_24h": 0,
                                 "watching_24h": 0}

    try:
        from puffpai.bridge_ledger import get_ledger
        ledger_status = dict(get_ledger().status())
        # Never hand an absolute on-disk path to a client: basename only.
        ledger_path = ledger_status.pop("ledger_path", "")
        ledger_status["ledger_file"] = (
            Path(ledger_path).name if ledger_path else "")
        payload["ledger"] = ledger_status
    except Exception:
        logger.exception("bridge status: ledger unavailable")
        payload["ledger"] = {"error": "ledger unavailable"}

    # Posture is read straight off the config so the panel can say "observe" or
    # "enforce" truthfully. Every access is a getattr: this route must keep
    # working if Zone3Config gains or loses a field.
    zone3_enabled = False
    enforce = False
    try:
        from puffpai.config import get_config
        zone3 = getattr(get_config(), "zone3", None)
        zone3_enabled = bool(getattr(zone3, "enabled", False))
        enforce = bool(getattr(zone3, "adguard_enforce", False))
    except Exception as exc:
        logger.info("zone3 posture unavailable: %s", exc)
    payload["zone3_enabled"] = zone3_enabled
    payload["enforce"] = enforce
    payload["mode"] = "enforce" if (zone3_enabled and enforce) else "observe"

    return jsonify(payload)


def register(app) -> None:
    """Register the blueprint and, when enabled, start the refresher.

    Called once from ``api_server``. The background re-scan starts only when
    :func:`puffpai.bridge_inventory.is_enabled` says so (``PUFFPAI_BRIDGE_INVENTORY``
    or ``zone3.enabled``); the routes themselves are always mounted so the
    dashboard gets an honest "off" rather than a 404.
    """
    app.register_blueprint(bridge_bp)
    try:
        from puffpai.bridge_inventory import (
            DEFAULT_REFRESH_SECONDS, get_inventory, is_enabled)
        if is_enabled():
            get_inventory().start_refresher(DEFAULT_REFRESH_SECONDS)
            logger.info("Bridge inventory enabled; refresher started")
        else:
            logger.info("Bridge inventory routes mounted; refresher idle "
                        "(set PUFFPAI_BRIDGE_INVENTORY=1 or zone3.enabled)")
    except Exception as exc:
        logger.warning("Bridge inventory refresher not started: %s", exc)


__all__ = ["bridge_bp", "register"]
