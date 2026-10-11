"""
PuffPAI Automation Webhook Client — centralized HTTP layer for the
automation engine (Node-RED).

Every flow is reached at /webhook/<name> on the Node-RED HTTP server.

Provides retry logic, structured error handling, and config-driven
webhook URLs so callers don't hardcode paths.

    from puffpai.automation_client import AutomationClient

    client = AutomationClient()
    ok, data = client.call("weather_alerts", {"lat": 39.74, "lon": -104.99})

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from puffpai.config import get_config
from puffpai.logging_config import redact_secrets

logger = logging.getLogger(__name__)

_DEFAULT_RETRIES = 2
_RETRY_BACKOFF = 0.5  # seconds between retries

# Webhook names this client will dispatch to. Kept as an explicit allow-list
# so a caller (or an LLM tool call) can't be talked into hitting an arbitrary
# path on the automation engine.
KNOWN_WEBHOOKS = frozenset({
    "block_device", "network_scan", "weather_alerts", "puffpai_voice",
    "daily_news", "calendar_sync", "meal_planner", "grocery_list",
    "daily_briefing", "daily_weather", "media_now_playing", "health_check",
    "good_morning", "leaving_home", "security_check",
})

#: Webhooks whose flow reaches the internet, and the Bridge switches its calls
#: belong to (``puffpai.security.egress_doors``). Node-RED opens those
#: connections itself, so the registry's request wrapper cannot stop them; this
#: table is how the backend refuses to START such a flow while a switch is off.
#: A flow listed here is refused when ANY of its switches is off, because a copy
#: of the flows that does not ask the switches itself would otherwise make every
#: call it knows. A webhook not listed here reaches only this house (the backend,
#: Home Assistant, Jellyfin) and is not affected.
WEBHOOK_SWITCHES: dict[str, tuple[str, ...]] = {
    "daily_weather": ("weather",),        # api.weather.gov
    "weather_alerts": ("weather",),       # api.weather.gov
    "daily_news": ("news_rss",),          # the feed in NEWS_RSS_URL
    "daily_briefing": ("weather", "news_rss"),
}


def switch_refusal(webhook_name: str) -> str | None:
    """The sentence to answer with when *webhook_name* may not start, else None.

    Asks the registry for each switch in :data:`WEBHOOK_SWITCHES`. Opens
    nothing. Each closed switch is recorded in the egress ledger as a refusal,
    like any other call the Bridge turned away. A registry that cannot be read
    counts as a closed switch.
    """
    doors = WEBHOOK_SWITCHES.get(webhook_name)
    if not doors:
        return None
    sentences: list[str] = []
    for door_id in doors:
        try:
            from puffpai.security.egress_doors import EgressBlocked, require_open
            try:
                require_open(door_id)
            except EgressBlocked as blocked:
                sentences.append(blocked.user_message)
        except Exception as exc:  # fail closed on a broken registry
            logger.warning("automation %s: could not read the %s switch: %s",
                           webhook_name, door_id, exc)
            sentences.append(f"The '{door_id}' switch could not be read, so "
                             f"nothing was started.")
    return " ".join(sentences) or None


def _resolve_engine_config(cfg: Any) -> Any:
    """Return the automation-engine (``nodered``) config section."""
    section = getattr(cfg, "nodered", None)
    if section is not None:
        return section
    raise ValueError(
        "No automation engine configured: expected a 'nodered' section in "
        "puffpai_config.json."
    )


class AutomationClient:
    """Thin wrapper around automation-engine webhook calls with retry."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ):
        cfg = _resolve_engine_config(get_config())
        self._base_url = (base_url or cfg.url).rstrip("/")
        self._timeout = timeout or cfg.timeout
        if max_retries is None:
            max_retries = getattr(cfg, "retries", None) or _DEFAULT_RETRIES
        self._max_retries = max_retries
        self._health_path = getattr(cfg, "health_path", None) or "/"
        self._webhooks = cfg.webhooks

    # ── Public API ────────────────────────────────────────────────

    def webhook_url(self, name: str) -> str:
        """Return the full URL for a named webhook.

        Supported names: see KNOWN_WEBHOOKS.
        """
        if name not in KNOWN_WEBHOOKS:
            raise ValueError(f"Unknown webhook name: {name!r}")
        path = getattr(self._webhooks, name)
        return f"{self._base_url}{path}"

    def call(
        self,
        webhook_name: str,
        payload: dict[str, Any],
        *,
        method: str = "POST",
    ) -> tuple[bool, dict[str, Any] | str]:
        """Call an automation webhook with retry logic.

        Returns (success: bool, data: dict | error_message: str).
        On success, data is the parsed JSON response.
        On failure, data is a human-readable error string. A flow that reaches
        the internet while its Bridge switch is off is not started at all; the
        string then names the switch (see :func:`switch_refusal`).
        """
        url = self.webhook_url(webhook_name)
        refused = switch_refusal(webhook_name)
        if refused:
            logger.info("automation %s not started: its Bridge switch is off",
                        webhook_name)
            return False, refused
        last_error = ""

        for attempt in range(1, self._max_retries + 1):
            try:
                if method.upper() == "POST":
                    resp = requests.post(url, json=payload, timeout=self._timeout)
                else:
                    resp = requests.get(url, params=payload, timeout=self._timeout)

                if resp.status_code == 200:
                    try:
                        return True, resp.json()
                    except ValueError:
                        return True, {"raw": resp.text}

                last_error = f"HTTP {resp.status_code}"
                logger.warning(
                    "automation %s returned %s (attempt %d/%d)",
                    webhook_name, resp.status_code, attempt, self._max_retries,
                )

            except requests.exceptions.ConnectionError:
                last_error = f"automation engine unreachable at {self._base_url}"
                logger.warning(
                    "automation connection failed for %s (attempt %d/%d)",
                    webhook_name, attempt, self._max_retries,
                )
            except requests.exceptions.Timeout:
                last_error = f"automation timeout ({self._timeout}s)"
                logger.warning(
                    "automation timeout for %s (attempt %d/%d)",
                    webhook_name, attempt, self._max_retries,
                )
            except requests.exceptions.RequestException as exc:
                # GET sends ``payload`` as the query string, and requests
                # stringifies the whole URL into the exception. No webhook
                # carries a credential today, but ``last_error`` is surfaced to
                # callers as well as logged, so mask anything credential-shaped
                # rather than relying on that staying true.
                last_error = redact_secrets(exc)
                logger.warning(
                    "automation request error for %s: %s (attempt %d/%d)",
                    webhook_name, last_error, attempt, self._max_retries,
                )

            if attempt < self._max_retries:
                time.sleep(_RETRY_BACKOFF * attempt)

        return False, last_error

    @property
    def base_url(self) -> str:
        """The automation engine's base URL."""
        return self._base_url

    def health_url(self) -> str:
        """URL used for the reachability probe (Node-RED editor root)."""
        return f"{self._base_url}{self._health_path if self._health_path.startswith('/') else '/' + self._health_path}"

    def is_reachable(self) -> bool:
        """Quick health check — does the engine answer HTTP 200?"""
        try:
            resp = requests.get(self.health_url(), timeout=3)
            return resp.status_code == 200
        except requests.exceptions.RequestException:
            return False
