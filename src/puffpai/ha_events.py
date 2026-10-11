"""Home Assistant event listener — pushes house events at the agent team.

A background daemon thread holds one WebSocket to HA, authenticates with the
long-lived token, subscribes to ``state_changed``, and forwards only the watched
entities to :func:`puffpai.agent_events.submit_agent_event`.

It is strictly **read-only**: the socket is used for ``auth`` and
``subscribe_events`` and nothing else, so a bug here can produce noise but never
an action on the house. Everything that could act still goes through the bus
(Sentinel veto + audit) on the receiving side.

Filtering rules (all in :func:`should_fire`, unit-tested without a socket):

* ``binary_sensor.*`` fires only on a transition **to** ``"on"`` — the ``off``
  edge of a motion sensor is not an event worth waking the team for.
* any transition involving ``unavailable``/``unknown`` (or a missing old state)
  never fires: an integration restarting is not a person at the shed.
* other domains fire on any real state change.
* a per-entity debounce (``ha_events.debounce_seconds``) collapses a camera that
  re-triggers every second into one event.

Keepalive: without it the socket drops every few hours with "Connection
timed out", which is websocket-client's own receive timeout firing in a
quiet gap between ``state_changed`` frames. So an idle socket
sends HA's ``{"type": "ping"}`` every :data:`PING_INTERVAL_SECONDS`; any frame
(the ``pong`` included) proves it alive. Only a ping that goes unanswered for
:data:`PONG_TIMEOUT_SECONDS` drops the socket, as a really dead link should.

Reconnects: a drop after a healthy session is re-subscribed at once and
quietly (DEBUG); only when that fails does the listener WARN once and fall
back to exponential backoff (5 s → 300 s cap), so a HA outage does not fill
the log and a blip does not show up in it.

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import fnmatch
import json
import socket
import threading
import time

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

#: States that mean "the integration is talking about itself", not the world.
IGNORED_STATES = frozenset({"unavailable", "unknown", "none", ""})

BACKOFF_START_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0

#: Idle time before the listener pings HA (HA's own frontend pings every 30 s
#: or so; this stays below the 30 s connect timeout it used to die on).
PING_INTERVAL_SECONDS = 20.0
#: How long a ping may go unanswered before the socket is declared dead.
PONG_TIMEOUT_SECONDS = 20.0
#: A session counts as HEALTHY (its drop is re-subscribed at once, and the
#: backoff resets) only after it stayed up this long or delivered a real
#: event. A socket that authenticates and then closes is a failure, not a
#: blip: without this it reconnected every 0.5 s forever.
HEALTHY_SESSION_SECONDS = 60.0
#: Backoff cap for a socket that keeps authenticating then closing.
FLAP_BACKOFF_MAX_SECONDS = 60.0
#: While it keeps failing, at most one WARNING per this many seconds.
WARN_EVERY_SECONDS = 600.0

_SUBSCRIBE_ID = 1


# ── pure helpers (no socket) ────────────────────────────────────────

def matches_watchlist(entity_id: str, patterns) -> bool:
    """True when *entity_id* matches any fnmatch glob in *patterns*."""
    if not entity_id:
        return False
    return any(fnmatch.fnmatch(entity_id, str(p)) for p in (patterns or []))


def derive_event_type(entity_id: str) -> str:
    """Map an entity id onto the event vocabulary the Concierge classifies."""
    lowered = (entity_id or "").lower()
    if "person" in lowered:
        return "camera_person"
    if "motion" in lowered:
        return "camera_motion"
    return "ha_state_change"


def should_fire(entity_id: str, old_state, new_state) -> bool:
    """Apply the transition rules described in the module docstring."""
    new = str(new_state).lower() if new_state is not None else ""
    old = str(old_state).lower() if old_state is not None else ""
    if new in IGNORED_STATES or old in IGNORED_STATES:
        return False
    if old == new:
        return False
    if (entity_id or "").startswith("binary_sensor."):
        return new == "on"
    return True


def ws_url_for(http_url: str) -> str:
    """Derive the HA WebSocket endpoint from the configured HTTP base URL."""
    base = (http_url or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return base + "/api/websocket"


# ── listener ────────────────────────────────────────────────────────

class HAEventListener:
    """Long-lived HA WebSocket subscriber feeding :mod:`puffpai.agent_events`."""

    def __init__(self, *, url: str, token: str, watch_entities=None,
                 debounce_seconds: int = 30, submit=None, ws_factory=None) -> None:
        self._url = url
        self._token = token
        self._watch = list(watch_entities or [])
        self._debounce = float(debounce_seconds)
        self._submit = submit
        self._ws_factory = ws_factory
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ws = None
        self._last_fired: dict[str, float] = {}
        self._connected = False
        self._next_id = _SUBSCRIBE_ID
        self._reconnects = 0
        self._got_event = False
        self._clock = time.monotonic

    # ── lifecycle ───────────────────────────────────────────────────
    def start(self) -> None:
        """Start the daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="ha-event-listener", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the thread to exit and close the socket."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "connected": self._connected,
            "watch_entities": list(self._watch),
            "debounce_seconds": self._debounce,
            "reconnects": self._reconnects,
        }

    # ── connection loop ─────────────────────────────────────────────
    def _run(self) -> None:
        clock = self._clock
        backoff = BACKOFF_START_SECONDS
        last_warn = None
        ever_connected = False
        while not self._stop.is_set():
            up_at = None
            quick_retry = False
            try:
                ws = self._connect()
                self._ws = ws
                self._connected = True
                self._got_event = False
                up_at = clock()
                if ever_connected:
                    self._reconnects += 1
                    logger.debug("HA event listener re-subscribed")
                else:
                    logger.info("HA event listener connected (%d watched "
                                "entities)", len(self._watch))
                ever_connected = True
                self._pump(ws)
            except Exception as exc:
                if self._stop.is_set():
                    break
                healthy = up_at is not None and (
                    self._got_event
                    or clock() - up_at >= HEALTHY_SESSION_SECONDS)
                if healthy:
                    # A drop after a healthy session: re-subscribe at once,
                    # quietly, with a fresh backoff and a fresh warning.
                    logger.debug("HA event listener dropped (%s); "
                                 "re-subscribing", exc)
                    quick_retry = True
                    backoff = BACKOFF_START_SECONDS
                    last_warn = None
                else:
                    # Never connected, or connected and closed straight away:
                    # a failure. One WARNING per WARN_EVERY_SECONDS.
                    now = clock()
                    if last_warn is None or now - last_warn >= WARN_EVERY_SECONDS:
                        logger.warning("HA event listener disconnected: %s",
                                       exc)
                        last_warn = now
                    else:
                        logger.debug("HA event listener retry failed: %s", exc)
            finally:
                self._connected = False
                ws, self._ws = self._ws, None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if quick_retry:
                if self._stop.wait(0.5):
                    break
                continue
            if self._stop.wait(backoff):
                break
            cap = (FLAP_BACKOFF_MAX_SECONDS if up_at is not None
                   else BACKOFF_MAX_SECONDS)
            backoff = min(backoff * 2, cap)

    def _create_connection(self, url: str):
        if self._ws_factory is not None:
            return self._ws_factory(url)
        import websocket  # imported lazily so the package stays optional
        return websocket.create_connection(url, timeout=30)

    def _connect(self):
        """Open the socket, authenticate, and subscribe to ``state_changed``."""
        ws = self._create_connection(ws_url_for(self._url))
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected HA greeting: {hello.get('type')!r}")
        ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            # Never echo the response: an auth failure body can carry the token.
            raise RuntimeError("HA websocket authentication failed")
        self._next_id = _SUBSCRIBE_ID
        ws.send(json.dumps({"id": _SUBSCRIBE_ID, "type": "subscribe_events",
                            "event_type": "state_changed"}))
        settimeout = getattr(ws, "settimeout", None)
        if callable(settimeout):
            # recv() now returns control every PING_INTERVAL_SECONDS of
            # silence, so the pump can ping instead of dying.
            settimeout(PING_INTERVAL_SECONDS)
        return ws

    def _send_ping(self, ws) -> None:
        # HA wants strictly increasing ids per connection (subscribe is 1).
        self._next_id += 1
        ws.send(json.dumps({"id": self._next_id, "type": "ping"}))

    def _pump(self, ws, *, clock=time.monotonic) -> None:
        """Read frames until the socket closes or ``stop()`` is called.

        A receive timeout is silence, not death: ping HA and keep reading.
        Only a ping unanswered for :data:`PONG_TIMEOUT_SECONDS` raises.
        """
        ping_sent_at = None
        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except Exception as exc:
                if not _is_timeout(exc) or self._stop.is_set():
                    raise
                now = clock()
                if ping_sent_at is None:
                    self._send_ping(ws)
                    ping_sent_at = now
                    continue
                if now - ping_sent_at >= PONG_TIMEOUT_SECONDS:
                    raise ConnectionError(
                        "HA websocket keepalive: no pong") from None
                continue
            if not raw:
                raise ConnectionError("HA websocket closed")
            ping_sent_at = None          # any frame proves the link alive
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if message.get("type") != "event":
                continue
            self._got_event = True
            try:
                self.handle_state_changed(message.get("event") or {})
            except Exception as exc:  # one bad frame must not drop the socket
                logger.debug("HA event handling failed: %s", exc)

    # ── event handling ──────────────────────────────────────────────
    def handle_state_changed(self, event: dict) -> bool:
        """Filter, debounce, and forward one ``state_changed`` event.

        Returns True when the event was submitted to the agent team.
        """
        if (event or {}).get("event_type") not in (None, "state_changed"):
            return False
        data = (event or {}).get("data") or {}
        entity_id = str(data.get("entity_id") or "")
        if not matches_watchlist(entity_id, self._watch):
            return False

        new_obj = data.get("new_state") or {}
        old_obj = data.get("old_state") or {}
        new_state = new_obj.get("state") if isinstance(new_obj, dict) else None
        old_state = old_obj.get("state") if isinstance(old_obj, dict) else None
        if not should_fire(entity_id, old_state, new_state):
            return False
        if not self._debounce_ok(entity_id):
            return False

        friendly = ""
        if isinstance(new_obj, dict):
            friendly = str((new_obj.get("attributes") or {}).get("friendly_name", ""))

        self._submit_event(derive_event_type(entity_id), {
            "entity_id": entity_id,
            "old_state": old_state,
            "new_state": new_state,
            "friendly_name": friendly,
        })
        return True

    def _debounce_ok(self, entity_id: str) -> bool:
        now = time.monotonic()
        last = self._last_fired.get(entity_id)
        if last is not None and (now - last) < self._debounce:
            return False
        self._last_fired[entity_id] = now
        return True

    def _submit_event(self, event_type: str, data: dict) -> None:
        submit = self._submit
        if submit is None:
            from puffpai.agent_events import submit_agent_event
            submit = submit_agent_event
        submit("ha", event_type, data)


def _is_timeout(exc: BaseException) -> bool:
    """A receive timeout from the socket or websocket-client (its
    ``WebSocketTimeoutException`` is not a ``TimeoutError`` subclass)."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    return type(exc).__name__ == "WebSocketTimeoutException"


# ── module singleton + startup wiring ───────────────────────────────

_listener: HAEventListener | None = None


def _resolve_ha() -> tuple[str, str, bool]:
    """Resolve (url, token, enabled) through the same chain the app uses."""
    from puffpai.config import get_config
    cfg = get_config()
    url, enabled = cfg.ha.url, bool(cfg.ha.enabled)
    token = ""
    try:
        from puffpai.security.ha_vault import get_ha_config
        vault_cfg = get_ha_config() or {}
        url = vault_cfg.get("url") or url
        enabled = bool(vault_cfg.get("enabled", enabled))
    except Exception as exc:  # vault optional; the token source still answers
        logger.debug("ha_vault unavailable, using config: %s", exc)
    # The credential itself comes from the one token source, so the
    # listener cannot end up on a different (or scrubbed-marker) token than the
    # bridge. Content-free on failure -- never log the token.
    try:
        from puffpai.security.ha_token_source import ha_token
        token = ha_token() or ""
    except Exception as exc:
        logger.debug("HA token source unavailable: %s", type(exc).__name__)
    return url, token, enabled


def start_listener() -> HAEventListener | None:
    """Start the shared listener if HA *and* ``ha_events`` are both enabled.

    Returns the listener, or None when disabled/unconfigured. Never raises.
    """
    global _listener
    if _listener is not None:
        return _listener
    try:
        from puffpai.config import get_config
        events_cfg = get_config().ha_events
        if not events_cfg.enabled:
            return None
        if not events_cfg.watch_entities:
            logger.info("HA event listener not started: ha_events.watch_entities is empty")
            return None
        url, token, enabled = _resolve_ha()
        if not enabled or not url or not token:
            return None
        listener = HAEventListener(
            url=url,
            token=token,
            watch_entities=events_cfg.watch_entities,
            debounce_seconds=events_cfg.debounce_seconds,
        )
        listener.start()
        _listener = listener
        return _listener
    except Exception as exc:
        logger.warning("HA event listener failed to start: %s", exc)
        return None


def get_listener() -> HAEventListener | None:
    return _listener


def stop_listener() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


__all__ = [
    "BACKOFF_MAX_SECONDS",
    "BACKOFF_START_SECONDS",
    "HAEventListener",
    "IGNORED_STATES",
    "PING_INTERVAL_SECONDS",
    "PONG_TIMEOUT_SECONDS",
    "derive_event_type",
    "get_listener",
    "matches_watchlist",
    "should_fire",
    "start_listener",
    "stop_listener",
    "ws_url_for",
]
