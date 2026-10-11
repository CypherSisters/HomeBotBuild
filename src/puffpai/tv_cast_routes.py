"""HTTP surface for TV casting. Logic lives in puffpai.tv_cast.

Two blueprints, on purpose:

* ``tvcast`` at ``/tvcast/*``: devices (from Home Assistant), channels, cast, stop. NOT
  public. The app-level ``_rate_limit`` + ``_check_api_key`` hooks cover it like
  every other route: the API key is the only check this backend makes, and any
  caller holding the key reaches these routes directly. The dashboard calls
  them through its ``/api/pai`` proxy, which holds the key and asks for a
  paired device only when ``PUFF_PAIRING_ENFORCE=all``; at the default
  ``knowledge`` stage any browser that can load the dashboard can cast and
  stop.
* ``castproxy`` at ``/cast/<id>/...``: the ONLY new keyless surface, because
  the TV that fetches it has no key and no cookie. Its two endpoints are
  in ``api_server._PUBLIC_ENDPOINTS`` with a written justification in
  ``endpoint_gate.PUBLIC_ENDPOINT_JUSTIFICATIONS``. What protects it: an
  unguessable id bound to one channel and one device IP, short-lived, revoked
  on Stop. Every miss is a bare 404 (wrong id, wrong IP, expired, revoked) so a
  prober learns nothing.

puff puff pass
"""

from __future__ import annotations

import logging
import time

from flask import Blueprint, Response, jsonify, request

from puffpai import tv_cast

logger = logging.getLogger("puffpai.tv_cast")

tvcast_bp = Blueprint("tvcast", __name__, url_prefix="/tvcast")
castproxy_bp = Blueprint("castproxy", __name__, url_prefix="/cast")


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _fail(exc: tv_cast.CastError):
    return jsonify({"success": False, "error": exc.code, "message": exc.message}), exc.status


def _guard(fn):
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except tv_cast.CastError as exc:
            return _fail(exc)
        except Exception as exc:  # class only: messages can carry URLs
            logger.warning("tv-cast %s failed (%s)", fn.__name__, type(exc).__name__)
            return jsonify({"success": False, "error": "internal",
                            "message": "Casting hit a problem. Try again."}), 500
    wrapper.__name__ = fn.__name__
    return wrapper


@tvcast_bp.route("/devices", methods=["GET"])
@_guard
def devices():
    mgr = tv_cast.get_manager()
    refresh = request.args.get("refresh") == "1"
    return jsonify({"success": True, **mgr.registry.public_list(mgr, refresh)})


@tvcast_bp.route("/channels", methods=["GET"])
@_guard
def channels():
    return jsonify({"success": True, **tv_cast.get_manager().channels()})


@tvcast_bp.route("/status", methods=["GET"])
@_guard
def status():
    return jsonify({"success": True, "casts": tv_cast.get_manager().status()})


@tvcast_bp.route("/library", methods=["GET"])
@_guard
def library():
    """One tab of the library (movies, shows, anime, music, books). Names only, never paths."""
    a = request.args
    return jsonify({"success": True, **tv_cast.get_manager().library(
        a.get("kind", ""), a.get("q", ""), a.get("start", "0"), a.get("letter", ""))})


@tvcast_bp.route("/library/children", methods=["GET"])
@_guard
def library_children():
    """A show's episodes or an album's songs."""
    return jsonify({"success": True,
                    **tv_cast.get_manager().library_children(request.args.get("id", ""))})


@tvcast_bp.route("/cast", methods=["POST"])
@_guard
def cast():
    """{"device", "channel"} for Live TV, or {"device", "item"} for the library."""
    b = _body()
    view = tv_cast.get_manager().start(str(b.get("device", "")),
                                       channel_id=str(b.get("channel", "") or ""),
                                       item_id=str(b.get("item", "") or ""))
    return jsonify({"success": True, "cast": view})


@tvcast_bp.route("/stop", methods=["POST"])
@_guard
def stop():
    device = str(_body().get("device", "") or "")
    if device and not tv_cast.ENTITY_RE.match(device):
        raise tv_cast.CastError("bad_device", "Pick a TV from the list.")
    n = tv_cast.get_manager().stop(device or None)
    return jsonify({"success": True, "stopped": n})


# ── the proxy the Apple TV fetches ─────────────────────────────────────────

_NOT_FOUND = ("not found", 404, {"Content-Type": "text/plain", "Cache-Control": "no-store"})
_EXT_ALLOWED = frozenset(tv_cast.CONTENT_TYPES)


@tvcast_bp.route("/diag", methods=["GET"])
@_guard
def diag():
    """Counters only (no ids, URLs, tokens or addresses). Key-gated like /tvcast/*."""
    return jsonify({"success": True,
                    "counters": tv_cast.diag_snapshot(reset=request.args.get("reset") == "1")})


def _serve(cid: str, key: str | None, ext: str | None):
    mgr = tv_cast.get_manager()
    what = "master" if key is None else (ext or "?")
    tv_cast.diag(f"req:{what}:{tv_cast.client_class(request.headers.get('User-Agent', ''))}")
    cast = mgr.lookup(cid, request.remote_addr or "")
    if cast is None:
        tv_cast.diag(f"answer:{what}:404")
        return Response(*_NOT_FOUND)
    t0 = time.monotonic()
    try:
        code, ctype, body = mgr.fetch(cast, key, ext)
    except tv_cast.CastError as exc:
        tv_cast.diag(f"answer:{what}:502:{exc.code}")
        logger.warning("tv-cast proxy refused (cast %s, %s)", cid[:6], exc.code)
        return Response("unavailable", 502, {"Content-Type": "text/plain",
                                             "Cache-Control": "no-store"})
    except Exception as exc:
        tv_cast.diag(f"answer:{what}:502:{type(exc).__name__}")
        logger.warning("tv-cast proxy failed (cast %s, %s)", cid[:6], type(exc).__name__)
        return Response("unavailable", 502, {"Content-Type": "text/plain",
                                             "Cache-Control": "no-store"})
    dt = time.monotonic() - t0
    tv_cast.diag(f"answer:{what}:{code}:{'slow' if dt > 5 else 'fast'}")
    if code != 200:
        return Response(*_NOT_FOUND) if code == 404 else \
            Response("unavailable", 502, {"Content-Type": "text/plain", "Cache-Control": "no-store"})
    return Response(body, 200, {"Content-Type": ctype, "Cache-Control": "no-store",
                                "X-Content-Type-Options": "nosniff"},
                    direct_passthrough=not isinstance(body, (bytes, str)))


@castproxy_bp.route("/<cid>/index.m3u8", methods=["GET"])
def master(cid):
    return _serve(cid, None, None)


@castproxy_bp.route("/<cid>/r/<key>.<ext>", methods=["GET"])
def resource(cid, key, ext):
    if ext not in _EXT_ALLOWED:
        return Response(*_NOT_FOUND)
    return _serve(cid, key, ext)


def register(app) -> None:
    app.register_blueprint(tvcast_bp)
    app.register_blueprint(castproxy_bp)


__all__ = ["tvcast_bp", "castproxy_bp", "register"]
