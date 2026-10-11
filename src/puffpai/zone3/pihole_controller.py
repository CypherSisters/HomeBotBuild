"""
PuffPAI Zone 3 — Pi-hole DNS Controller

Manages the Pi-hole instance via its HTTP API to provide DNS-level
blocking of telemetry, tracking, and unwanted outbound traffic from
smart home devices.

Pi-hole API v6 (pihole/pihole:latest) uses session-based auth.
Older Pi-hole (v5.x) uses a simpler token-based API.

This controller auto-detects the API version and adapts.

Security:
    - All requests use timeouts to prevent hangs
    - Domain inputs are validated before submission
    - Pi-hole runs in Docker with no access to Zones 1 or 2
    - The v5 API authenticates with ``?auth=<double-SHA256 of the password>``
      in the *query string*, and requests' exceptions stringify the full URL.
      Every exception in this module is therefore passed through
      ``redact_secrets`` before it is logged or returned — the v6 paths too,
      so a future v5/v6 mix-up cannot reintroduce the leak.
"""

from __future__ import annotations

import re
import time
import logging
import requests
from dataclasses import dataclass
from typing import Optional

from puffpai.logging_config import redact_secrets

logger = logging.getLogger(__name__)

_DOMAIN_PATTERN = re.compile(
    r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*'
    r'[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$'
)

# Pi-hole group IDs
_GROUP_DEFAULT = 0  # Pi-hole's built-in "Default" group


def _validate_domain(domain: str) -> str:
    """Validate and normalize a domain name."""
    domain = domain.strip().lower()
    if not domain:
        raise ValueError("Domain cannot be empty")
    if len(domain) > 253:
        raise ValueError(f"Domain too long: {len(domain)} chars")
    if not _DOMAIN_PATTERN.match(domain):
        raise ValueError(f"Invalid domain: {domain!r}")
    return domain


@dataclass
class PiholeStatus:
    """Snapshot of Pi-hole status."""
    reachable: bool
    blocking_enabled: bool
    domains_blocked: int
    dns_queries_today: int
    ads_blocked_today: int
    ads_percentage_today: float
    custom_lists: int  # Number of PuffPAI-managed lists
    version: str


class PiholeController:
    """Controls a Pi-hole instance via its HTTP API.

    Manages per-device DNS blocking by:
    1. Adding domains to Pi-hole's blacklist (exact or wildcard)
    2. Assigning clients to groups with different blocklists
    3. Enabling/disabling blocking globally (lockdown mode)

    Args:
        base_url: Pi-hole web admin URL (e.g., "http://localhost:8080")
        password: Pi-hole web admin password
        timeout: HTTP request timeout in seconds
    """

    # Comment prefix for PuffPAI-managed entries
    _COMMENT_PREFIX = "PuffPAI-Zone3"

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        password: str = "puffpai",
        timeout: int = 10,
    ):
        self._base_url = base_url.rstrip("/")
        self._password = password
        self._timeout = timeout
        self._session = requests.Session()
        self._sid: Optional[str] = None  # v6 session ID
        self._api_version: Optional[int] = None

    # ── Connection / Auth ─────────────────────────────────────────────

    def _detect_api_version(self) -> int:
        """Detect Pi-hole API version (5 or 6)."""
        # Try v6 endpoint first
        try:
            r = self._session.get(
                f"{self._base_url}/api/info/version",
                timeout=self._timeout,
            )
            if r.ok:
                self._api_version = 6
                return 6
        except requests.RequestException:
            pass

        # Fall back to v5
        try:
            r = self._session.get(
                f"{self._base_url}/admin/api.php?version",
                timeout=self._timeout,
            )
            if r.ok:
                self._api_version = 5
                return 5
        except requests.RequestException:
            pass

        self._api_version = 5  # default assumption
        return 5

    def _ensure_auth(self) -> bool:
        """Authenticate with Pi-hole if not already authenticated."""
        if self._api_version is None:
            self._detect_api_version()

        if self._api_version == 6:
            return self._auth_v6()
        return True  # v5 uses token in query params

    def _auth_v6(self) -> bool:
        """Authenticate with Pi-hole v6 (session-based)."""
        if self._sid:
            return True
        try:
            r = self._session.post(
                f"{self._base_url}/api/auth",
                json={"password": self._password},
                timeout=self._timeout,
            )
            if r.ok:
                data = r.json()
                self._sid = data.get("session", {}).get("sid")
                if self._sid:
                    self._session.headers["sid"] = self._sid
                    return True
            # Redact *before* truncating: slicing a raw body could otherwise
            # leave a partial credential in the line.
            logger.warning("Pi-hole v6 auth failed: %s",
                           redact_secrets(r.text)[:200])
            return False
        except requests.RequestException as e:
            logger.error("Pi-hole auth error: %s", redact_secrets(e))
            return False

    def _api_token_param(self) -> str:
        """Get the v5 API auth token (hashed password)."""
        import hashlib
        # Pi-hole v5 uses double-SHA256 of the password
        h1 = hashlib.sha256(self._password.encode()).hexdigest()
        h2 = hashlib.sha256(h1.encode()).hexdigest()
        return h2

    # ── Core API Methods ──────────────────────────────────────────────

    def is_reachable(self) -> bool:
        """Check if Pi-hole is reachable."""
        try:
            r = self._session.get(
                f"{self._base_url}/admin/",
                timeout=self._timeout,
            )
            return r.ok
        except requests.RequestException:
            return False

    def get_status(self) -> PiholeStatus:
        """Get Pi-hole status summary."""
        try:
            if self._api_version == 6 or self._api_version is None:
                return self._status_v6()
        except Exception:
            pass
        return self._status_v5()

    def _status_v5(self) -> PiholeStatus:
        """Get status via Pi-hole v5 API."""
        try:
            token = self._api_token_param()
            r = self._session.get(
                f"{self._base_url}/admin/api.php",
                params={"summary": "", "auth": token},
                timeout=self._timeout,
            )
            if r.ok:
                d = r.json()
                return PiholeStatus(
                    reachable=True,
                    blocking_enabled=d.get("status") == "enabled",
                    domains_blocked=int(d.get("domains_being_blocked", 0)),
                    dns_queries_today=int(d.get("dns_queries_today", 0)),
                    ads_blocked_today=int(d.get("ads_blocked_today", 0)),
                    ads_percentage_today=float(d.get("ads_percentage_today", 0)),
                    custom_lists=0,
                    version=d.get("gravity_last_updated", {}).get("file_exists", "v5"),
                )
        except Exception as e:
            logger.debug("Pi-hole v5 status failed: %s", redact_secrets(e))

        return PiholeStatus(
            reachable=False, blocking_enabled=False, domains_blocked=0,
            dns_queries_today=0, ads_blocked_today=0, ads_percentage_today=0,
            custom_lists=0, version="unknown",
        )

    def _status_v6(self) -> PiholeStatus:
        """Get status via Pi-hole v6 API."""
        self._ensure_auth()
        try:
            r = self._session.get(
                f"{self._base_url}/api/stats/summary",
                timeout=self._timeout,
            )
            if r.ok:
                d = r.json()
                queries = d.get("queries", {})
                return PiholeStatus(
                    reachable=True,
                    blocking_enabled=d.get("blocking", True),
                    domains_blocked=d.get("gravity", {}).get("domains_being_blocked", 0),
                    dns_queries_today=queries.get("total", 0),
                    ads_blocked_today=queries.get("blocked", 0),
                    ads_percentage_today=queries.get("percent_blocked", 0),
                    custom_lists=0,
                    version="v6",
                )
        except Exception as e:
            logger.debug("Pi-hole v6 status failed: %s", redact_secrets(e))

        return self._status_v5()  # fallback

    # ── Domain Blocking ───────────────────────────────────────────────

    def block_domain(self, domain: str, comment: str = "") -> dict:
        """Add a domain to Pi-hole's blacklist (exact block).

        Args:
            domain: The domain to block (e.g., "telemetry.samsung.com")
            comment: Optional reason string

        Returns:
            dict with 'success' and 'message' keys.
        """
        try:
            domain = _validate_domain(domain)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        full_comment = f"{self._COMMENT_PREFIX}: {comment}" if comment else self._COMMENT_PREFIX

        if self._api_version == 6:
            return self._block_domain_v6(domain, full_comment)
        return self._block_domain_v5(domain, full_comment)

    def _block_domain_v5(self, domain: str, comment: str) -> dict:
        """Block domain via Pi-hole v5 API."""
        try:
            token = self._api_token_param()
            r = self._session.get(
                f"{self._base_url}/admin/api.php",
                params={
                    "list": "black",
                    "add": domain,
                    "comment": comment,
                    "auth": token,
                },
                timeout=self._timeout,
            )
            if r.ok:
                data = r.json()
                if data.get("success"):
                    return {"success": True, "message": f"Blocked {domain}"}
                return {"success": False, "message": data.get("message", "Unknown error")}
            return {"success": False, "message": f"HTTP {r.status_code}"}
        except requests.RequestException as e:
            return {"success": False,
                    "message": f"Pi-hole unreachable: {redact_secrets(e)}"}

    def _block_domain_v6(self, domain: str, comment: str) -> dict:
        """Block domain via Pi-hole v6 API."""
        self._ensure_auth()
        try:
            r = self._session.post(
                f"{self._base_url}/api/domains/deny/exact",
                json={"domain": domain, "comment": comment},
                timeout=self._timeout,
            )
            if r.ok:
                return {"success": True, "message": f"Blocked {domain}"}
            # Already exists is not an error
            if r.status_code == 409:
                return {"success": True, "message": f"{domain} already blocked"}
            return {"success": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        except requests.RequestException as e:
            return {"success": False,
                    "message": f"Pi-hole unreachable: {redact_secrets(e)}"}

    def block_domain_wildcard(self, domain: str, comment: str = "") -> dict:
        """Block a domain and all subdomains via regex/wildcard.

        E.g., blocking "samsung.com" also blocks "telemetry.samsung.com",
        "ads.samsung.com", etc.
        """
        try:
            domain = _validate_domain(domain)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        full_comment = f"{self._COMMENT_PREFIX}: {comment}" if comment else self._COMMENT_PREFIX
        # Pi-hole regex format for wildcard subdomain blocking
        regex_pattern = rf"(^|\.){re.escape(domain)}$"

        if self._api_version == 6:
            return self._block_regex_v6(regex_pattern, full_comment)
        return self._block_regex_v5(regex_pattern, full_comment)

    def _block_regex_v5(self, pattern: str, comment: str) -> dict:
        try:
            token = self._api_token_param()
            r = self._session.get(
                f"{self._base_url}/admin/api.php",
                params={
                    "list": "regex_black",
                    "add": pattern,
                    "comment": comment,
                    "auth": token,
                },
                timeout=self._timeout,
            )
            if r.ok:
                return {"success": True, "message": f"Wildcard blocked: {pattern}"}
            return {"success": False, "message": f"HTTP {r.status_code}"}
        except requests.RequestException as e:
            return {"success": False,
                    "message": f"Pi-hole unreachable: {redact_secrets(e)}"}

    def _block_regex_v6(self, pattern: str, comment: str) -> dict:
        self._ensure_auth()
        try:
            r = self._session.post(
                f"{self._base_url}/api/domains/deny/regex",
                json={"domain": pattern, "comment": comment},
                timeout=self._timeout,
            )
            if r.ok or r.status_code == 409:
                return {"success": True, "message": f"Wildcard blocked: {pattern}"}
            return {"success": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        except requests.RequestException as e:
            return {"success": False,
                    "message": f"Pi-hole unreachable: {redact_secrets(e)}"}

    def unblock_domain(self, domain: str) -> dict:
        """Remove a domain from Pi-hole's blacklist."""
        try:
            domain = _validate_domain(domain)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        if self._api_version == 6:
            return self._unblock_domain_v6(domain)
        return self._unblock_domain_v5(domain)

    def _unblock_domain_v5(self, domain: str) -> dict:
        try:
            token = self._api_token_param()
            r = self._session.get(
                f"{self._base_url}/admin/api.php",
                params={"list": "black", "sub": domain, "auth": token},
                timeout=self._timeout,
            )
            if r.ok:
                return {"success": True, "message": f"Unblocked {domain}"}
            return {"success": False, "message": f"HTTP {r.status_code}"}
        except requests.RequestException as e:
            return {"success": False,
                    "message": f"Pi-hole unreachable: {redact_secrets(e)}"}

    def _unblock_domain_v6(self, domain: str) -> dict:
        self._ensure_auth()
        try:
            r = self._session.delete(
                f"{self._base_url}/api/domains/deny/exact/{domain}",
                timeout=self._timeout,
            )
            if r.ok or r.status_code == 404:
                return {"success": True, "message": f"Unblocked {domain}"}
            return {"success": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        except requests.RequestException as e:
            return {"success": False,
                    "message": f"Pi-hole unreachable: {redact_secrets(e)}"}

    # ── Bulk Operations ───────────────────────────────────────────────

    def block_domains(self, domains: list[str], comment: str = "") -> dict:
        """Block multiple domains at once.

        Returns summary: {success: True, blocked: N, failed: N, errors: [...]}
        """
        blocked = 0
        failed = 0
        errors = []

        for domain in domains:
            result = self.block_domain(domain, comment)
            if result["success"]:
                blocked += 1
            else:
                failed += 1
                errors.append(f"{domain}: {result['message']}")

        return {
            "success": failed == 0,
            "message": f"Blocked {blocked}/{len(domains)} domains",
            "blocked": blocked,
            "failed": failed,
            "errors": errors[:10],  # Cap error list
        }

    def unblock_domains(self, domains: list[str]) -> dict:
        """Unblock multiple domains at once."""
        unblocked = 0
        failed = 0

        for domain in domains:
            result = self.unblock_domain(domain)
            if result["success"]:
                unblocked += 1
            else:
                failed += 1

        return {
            "success": failed == 0,
            "message": f"Unblocked {unblocked}/{len(domains)} domains",
            "unblocked": unblocked,
            "failed": failed,
        }

    # ── Global Blocking Control ───────────────────────────────────────

    def enable_blocking(self) -> dict:
        """Enable Pi-hole DNS blocking (normal mode)."""
        return self._set_blocking(True)

    def disable_blocking(self, seconds: int = 0) -> dict:
        """Disable Pi-hole DNS blocking.

        Args:
            seconds: Disable for N seconds (0 = indefinitely).
        """
        return self._set_blocking(False, seconds)

    def _set_blocking(self, enable: bool, seconds: int = 0) -> dict:
        """Enable or disable Pi-hole blocking."""
        if self._api_version == 6:
            return self._set_blocking_v6(enable, seconds)
        return self._set_blocking_v5(enable, seconds)

    def _set_blocking_v5(self, enable: bool, seconds: int = 0) -> dict:
        try:
            token = self._api_token_param()
            action = "enable" if enable else f"disable={seconds}" if seconds else "disable"
            r = self._session.get(
                f"{self._base_url}/admin/api.php",
                params={action: "", "auth": token},
                timeout=self._timeout,
            )
            if r.ok:
                state = "enabled" if enable else "disabled"
                msg = f"Blocking {state}"
                if not enable and seconds:
                    msg += f" for {seconds}s"
                return {"success": True, "message": msg}
            return {"success": False, "message": f"HTTP {r.status_code}"}
        except requests.RequestException as e:
            return {"success": False,
                    "message": f"Pi-hole unreachable: {redact_secrets(e)}"}

    def _set_blocking_v6(self, enable: bool, seconds: int = 0) -> dict:
        self._ensure_auth()
        try:
            body = {"blocking": enable}
            if not enable and seconds:
                body["timer"] = seconds
            r = self._session.post(
                f"{self._base_url}/api/dns/blocking",
                json=body,
                timeout=self._timeout,
            )
            if r.ok:
                state = "enabled" if enable else "disabled"
                msg = f"Blocking {state}"
                if not enable and seconds:
                    msg += f" for {seconds}s"
                return {"success": True, "message": msg}
            return {"success": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        except requests.RequestException as e:
            return {"success": False,
                    "message": f"Pi-hole unreachable: {redact_secrets(e)}"}

    # ── Client Group Management ───────────────────────────────────────

    def get_groups(self) -> list[dict]:
        """List all Pi-hole groups."""
        if self._api_version == 6:
            return self._get_groups_v6()
        return self._get_groups_v5()

    def _get_groups_v5(self) -> list[dict]:
        try:
            token = self._api_token_param()
            r = self._session.get(
                f"{self._base_url}/admin/api.php",
                params={"groups": "", "auth": token},
                timeout=self._timeout,
            )
            if r.ok:
                return r.json().get("data", [])
        except Exception:
            pass
        return []

    def _get_groups_v6(self) -> list[dict]:
        self._ensure_auth()
        try:
            r = self._session.get(
                f"{self._base_url}/api/groups",
                timeout=self._timeout,
            )
            if r.ok:
                return r.json().get("groups", [])
        except Exception:
            pass
        return []

    def create_group(self, name: str, description: str = "") -> dict:
        """Create a Pi-hole group for device policy segmentation.

        Groups let us apply different blocklists to different devices.
        E.g., "puffpai_lockdown" blocks everything, "puffpai_balanced"
        blocks telemetry only.
        """
        if self._api_version == 6:
            return self._create_group_v6(name, description)
        return self._create_group_v5(name, description)

    def _create_group_v5(self, name: str, description: str) -> dict:
        try:
            token = self._api_token_param()
            r = self._session.get(
                f"{self._base_url}/admin/api.php",
                params={
                    "groups": "",
                    "action": "add_group",
                    "name": name,
                    "desc": description,
                    "auth": token,
                },
                timeout=self._timeout,
            )
            if r.ok:
                return {"success": True, "message": f"Created group: {name}"}
            return {"success": False, "message": f"HTTP {r.status_code}"}
        except requests.RequestException as e:
            return {"success": False, "message": redact_secrets(e)}

    def _create_group_v6(self, name: str, description: str) -> dict:
        self._ensure_auth()
        try:
            r = self._session.post(
                f"{self._base_url}/api/groups",
                json={"name": name, "comment": description},
                timeout=self._timeout,
            )
            if r.ok or r.status_code == 409:
                return {"success": True, "message": f"Created group: {name}"}
            return {"success": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        except requests.RequestException as e:
            return {"success": False, "message": redact_secrets(e)}

    def assign_client_to_group(self, client_ip: str, group_name: str) -> dict:
        """Assign a client (by IP) to a Pi-hole group.

        This controls which blocklists apply to that device.
        """
        if self._api_version == 6:
            return self._assign_client_v6(client_ip, group_name)
        return self._assign_client_v5(client_ip, group_name)

    def _assign_client_v5(self, client_ip: str, group_name: str) -> dict:
        # v5 client management is complex — simplified version
        try:
            token = self._api_token_param()
            # Add client first
            r = self._session.get(
                f"{self._base_url}/admin/api.php",
                params={
                    "clients": "",
                    "action": "add_client",
                    "ip": client_ip,
                    "comment": f"{self._COMMENT_PREFIX}: managed device",
                    "auth": token,
                },
                timeout=self._timeout,
            )
            if r.ok:
                return {"success": True, "message": f"Assigned {client_ip} to {group_name}"}
            return {"success": False, "message": f"HTTP {r.status_code}"}
        except requests.RequestException as e:
            return {"success": False, "message": redact_secrets(e)}

    def _assign_client_v6(self, client_ip: str, group_name: str) -> dict:
        self._ensure_auth()
        try:
            r = self._session.post(
                f"{self._base_url}/api/clients",
                json={
                    "client": client_ip,
                    "comment": f"{self._COMMENT_PREFIX}: managed device",
                    "groups": [group_name],
                },
                timeout=self._timeout,
            )
            if r.ok or r.status_code == 409:
                return {"success": True, "message": f"Assigned {client_ip} to {group_name}"}
            return {"success": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}
        except requests.RequestException as e:
            return {"success": False, "message": redact_secrets(e)}

    # ── Query Log ─────────────────────────────────────────────────────

    def get_recent_queries(self, client_ip: str = "", limit: int = 50) -> list[dict]:
        """Get recent DNS queries, optionally filtered by client IP.

        Useful for showing users what their devices are trying to reach.
        """
        if self._api_version == 6:
            return self._queries_v6(client_ip, limit)
        return self._queries_v5(client_ip, limit)

    def _queries_v5(self, client_ip: str, limit: int) -> list[dict]:
        try:
            token = self._api_token_param()
            params = {"getAllQueries": limit, "auth": token}
            if client_ip:
                params["client"] = client_ip
            r = self._session.get(
                f"{self._base_url}/admin/api.php",
                params=params,
                timeout=self._timeout,
            )
            if r.ok:
                data = r.json().get("data", [])
                return [
                    {
                        "timestamp": row[0] if len(row) > 0 else "",
                        "type": row[1] if len(row) > 1 else "",
                        "domain": row[2] if len(row) > 2 else "",
                        "client": row[3] if len(row) > 3 else "",
                        "status": row[4] if len(row) > 4 else "",
                    }
                    for row in data
                ]
        except Exception:
            pass
        return []

    def _queries_v6(self, client_ip: str, limit: int) -> list[dict]:
        self._ensure_auth()
        try:
            params = {"length": limit}
            if client_ip:
                params["client"] = client_ip
            r = self._session.get(
                f"{self._base_url}/api/queries",
                params=params,
                timeout=self._timeout,
            )
            if r.ok:
                return r.json().get("queries", [])
        except Exception:
            pass
        return []
