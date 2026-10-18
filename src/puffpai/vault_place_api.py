"""HTTP routes for where the vault lives (Developer, Zone 1: Vault), read only.

Registered on the backend app by ``api_server`` (one line), so every route
sits behind the same API-key check, rate limit and CORS rules as the rest of
the backend; the dashboard reaches them only through its paired proxy
(``vault`` is on its address list).

Routes::

    GET  /vault/location          where the vault lives (read-only)
    GET  /vault/location/browse   ?folder=  the drives, or the folders inside
                                  one (names only, never files), for the
                                  folder pickers

The vault stays in the folder setup made it in: this build has no route that
moves it or points it at another vault. The browse needs a PAIRED device (the
same gate as switching outside services on). A PIN in the address (or
anything named like a PIN, code or token) is refused and never echoed.

Copyright (c) Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

bp = Blueprint("vault_place", __name__)

_NOT_PAIRED = "Pair this device first."
#: Query-string names that could carry a PIN. Refused on every route.
_SECRET_ARGS = ("pin", "current_pin", "password", "vault_pin", "secret")

_STATUS = {"bare_drive": 409, "vault_exists": 409, "vault_missing": 409,
           "has_vault": 409, "not_empty": 409, "same": 409,
           "wrong_pin": 403, "current_pin_needed": 403, "pin_needed": 400,
           "throttled": 429, "unreachable": 503, "copy_failed": 500}


def _guard(*, change: bool = True):
    """A PIN in the address, or a change from a device that is not paired."""
    from puffpai.security.pasted_key_guard import SECRET_QUERY_NAMES
    if any(name in request.args for name in _SECRET_ARGS) or any(
            word in str(name).lower() for name in request.args
            for word in SECRET_QUERY_NAMES):
        return jsonify({"ok": False, "message": "A PIN never goes in the "
                        "address. Nothing was changed."}), 400
    if change:
        from puffpai import outside_api
        if not outside_api.paired_device_id():
            return jsonify({"ok": False, "code": "not_paired",
                            "message": _NOT_PAIRED}), 403
    return None


def _refused(exc):
    return jsonify(exc.as_dict()), _STATUS.get(exc.code, 400)


def _server_vault():
    from puffpai import api_server
    return api_server._get_vault()  # noqa: SLF001


@bp.route("/vault/location", methods=["GET"])
def vault_location():
    bad = _guard(change=False)
    if bad:
        return bad
    from puffpai import outside_api
    from puffpai.security import vault_place
    out = vault_place.location(_server_vault())
    out["paired"] = bool(outside_api.paired_device_id())
    out["ok"] = True
    return jsonify(out)


@bp.route("/vault/location/browse", methods=["GET"])
def vault_location_browse():
    bad = _guard()
    if bad:
        return bad
    from puffpai.security import vault_place
    try:
        return jsonify(vault_place.browse(request.args.get("folder")))
    except vault_place.PlaceRefused as exc:
        return _refused(exc)


__all__ = ["bp"]
