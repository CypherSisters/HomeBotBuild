"""HTTP surface for the hatch ritual and the adoption certificate.

Kept out of ``api_server.py`` on purpose: the routes live here as a
registration function so the (12k-line) server module only carries a
three-line hook. ``api_server`` calls :func:`register` at import time; the
endpoints inherit its before-request hooks, so they are API-key-gated and
rate-limited like every other non-public route (none of them is in
``_PUBLIC_ENDPOINTS``).

The dashboard reaches these through its same-origin ``/api/pai/...`` proxy,
which attaches the key server-side.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

from puffpai.logging_config import get_logger

logger = get_logger(__name__)


def register(app, *, on_name_change=None) -> None:
    """Attach the hatch/certificate routes to *app*.

    *on_name_change* is called with the new name whenever a hatch or rename
    lands, so the server process can refresh its import-time ``AI_NAME``
    without a restart (the deterministic identity answers must name the same
    Puff the dashboard shows).
    """
    from flask import Response, jsonify, request

    from puffpai import certificate, hatch

    def _notify_name(name: str) -> None:
        if on_name_change is None:
            return
        try:
            on_name_change(name)
        except Exception as exc:  # a UI nicety must never fail the hatch
            logger.warning("on_name_change hook failed: %s", exc)

    @app.route('/hatch/status', methods=['GET'])
    def hatch_status():
        """Whether this build has hatched, and as whom."""
        state = hatch.load_state()
        return jsonify({
            'hatched': state is not None,
            'name': (state or {}).get('name'),
            'hatched_at': (state or {}).get('hatched_at'),
            'hatch_date': (state or {}).get('hatch_date'),
            'replays': (state or {}).get('replays', 0),
            'ask_line': hatch.ask_line(),
            'certificate_formats':
                ['svg', 'png'] if certificate.png_available() else ['svg'],
        })

    @app.route('/hatch/preflight', methods=['GET'])
    def hatch_preflight():
        """Real probes only — never the unconditional /health."""
        require_voice = request.args.get('require_voice', '1') not in (
            '0', 'false', 'no')
        result = hatch.run_preflight(require_voice=require_voice)
        return jsonify(result)

    @app.route('/hatch', methods=['POST'])
    def hatch_perform():
        """Run the ritual (first hatch) or replay it.

        Body: ``{"name": "...", "rename": false, "require_voice": true}``.
        Replays keep the existing name unless ``rename`` is explicit.
        Refuses with 409 + the blocking checks when pre-flight fails.
        """
        body = request.get_json(silent=True) or {}
        name = body.get('name')
        rename = bool(body.get('rename'))
        require_voice = bool(body.get('require_voice', True))

        was_hatched = hatch.is_hatched()
        try:
            result = hatch.perform_hatch(
                name, rename=rename, require_voice=require_voice)
        except hatch.HatchRefused as refused:
            return jsonify({
                'success': False,
                'refused': True,
                'message': str(refused),
                'blocking': refused.blocking,
            }), 409
        except hatch.HatchNameRequired as exc:
            return jsonify({'success': False, 'message': str(exc)}), 400
        except hatch.HatchBusy as exc:
            return jsonify({'success': False, 'busy': True,
                            'message': str(exc)}), 409
        except hatch.HatchStateError as exc:
            # Pre-flight passed but a write still failed (rolled back where
            # possible). A structured 503 — never a bare traceback 500.
            return jsonify({'success': False, 'message': str(exc)}), 503

        if (not was_hatched) or (rename and name):
            _notify_name(result['name'])
        result['success'] = True
        return jsonify(result)

    @app.route('/hatch/certificate', methods=['GET'])
    def hatch_certificate():
        """The adoption certificate — SVG natively, PNG when Pillow exists."""
        fmt = (request.args.get('format') or 'svg').lower()
        if fmt not in ('svg', 'png'):
            return jsonify({'error': f'unknown format: {fmt}',
                            'formats': ['svg', 'png']}), 400
        try:
            payload = certificate.generate(fmt=fmt)
        except certificate.CertificateUnavailable as exc:
            status = 501 if fmt == 'png' and not certificate.png_available() \
                else 409
            return jsonify({
                'error': str(exc),
                'formats': ['svg', 'png'] if certificate.png_available()
                else ['svg'],
            }), status

        mimetype = 'image/svg+xml' if fmt == 'svg' else 'image/png'
        response = Response(payload, mimetype=mimetype)
        if request.args.get('download') in ('1', 'true', 'yes'):
            state = hatch.load_state() or {}
            stem = hatch.clean_name(state.get('name', 'puff')) or 'puff'
            safe = ''.join(c if c.isalnum() else '-' for c in stem).strip('-')
            response.headers['Content-Disposition'] = (
                f'attachment; filename="{safe or "puff"}-adoption-'
                f'certificate.{fmt}"')
        return response


__all__ = ["register"]
