"""
PuffPAI Zone 3 — AdGuard Home DNS Controller

Network-edge enforcement for the Bridge zone. AdGuard Home is the active path;
the Pi-hole controller (pihole_controller.py) stays for reference.

Deployment model (see docs/ZONE3_SETUP.md):
    1. `sudo systemctl start puffpai-zone3` runs AdGuard Home in a container
       ON THE APPLIANCE. It publishes :53 on the LAN address only.
    2. Point the ROUTER's DHCP "Static DNS 1" field at the appliance. That is
       the only step that puts Zone 3 in the path, and reverting that one
       field is the whole rollback.
    3. This controller talks to AdGuard's REST API over loopback.

MONITOR-FIRST: ``enforce=False`` (default) records every rule this controller
*would* apply to data/zone3_dns_rules.jsonl, and writes it into AdGuard as a
COMMENTED-OUT line so the owner can read exactly what would be blocked — but
AdGuard never matches it. Flip enforce=True only after reviewing a few days of
query logs.

Security:
    - Local-network only; HTTP basic auth to AdGuard's API (verified against
      v0.107.79: every /control endpoint answers 401 without it); timeouts on
      every call.
    - Domain + client inputs validated before submission.
    - Never blocks the host's own interfaces (a rule pinned to one of the
      host's own addresses, such as a VPN interface, would cut the host off).

── RULE SYNTAX: WHY RULES ARE WRITTEN AS A BLOCK ─────────────────────────
Tagging each rule with a trailing comment does not work::

    ||samsungads.com^ # PuffPAI-Zone3 telemetry

AdGuard Home accepts that string (HTTP 200), stores it, and shows it in the
UI — and never matches it. Adblock syntax has no trailing comment; the tag
becomes part of the pattern. Measured on v0.107.79 with the subscription
filters removed so only user rules could match::

    ||samsungads.com^ # PuffPAI-Zone3 telemetry
      -> check_host samsungads.com  = NotFilteredNotFound
    ||lgsmartad.com^
      -> check_host lgsmartad.com   = FilteredBlackList (filter_list_id 0)

So a trailing-comment rule is inert while ``block_domain`` returns True and
the audit trail says "applied". A monitor-first system that lies about what
it would enforce is worse than one that enforces nothing.

Rules are therefore written as a delimited BLOCK between whole-line markers, which
IS valid syntax, and the per-rule provenance lives on its own comment line:

    !#### PuffPAI-Zone3 BEGIN
    ! generated 2026-09-09T19:00:00+00:00  enforce=false
    ! 192.168.50.50  Samsung smart_tv  balanced  (ads)
    ! OBSERVE ||samsungads.com^$client=192.168.50.50
    !#### PuffPAI-Zone3 END

Everything OUTSIDE the markers is the owner's and is preserved verbatim on
every write.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

import requests

from puffpai.security.lan_http import lan_get, lan_post
from puffpai.security.netaddr import AddressRefused, ResolutionError

# statedir is stdlib-only and defers the puffpai.config import to call time,
# so importing it here at module scope cannot pull in the secrets provider
# or create a cycle the way importing puffpai.config directly would.
from puffpai.statedir import resolve_runtime_dir

logger = logging.getLogger(__name__)

_DOMAIN_PATTERN = re.compile(
    r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*'
    r'[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$'
)

#: Repo root — four directory levels up from
#: ``src/puffpai/zone3/adguard_controller.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

#: Cached absolute path of the monitor-first rule ledger; ``None`` = unresolved.
_rules_audit: str | None = None


def _rules_audit_path() -> str:
    """Absolute path of the rule audit trail, resolved on first use and cached.

    Resolving at import from a raw ``$PUFFPAI_DATA_DIR`` (no strip, no ``~``
    expansion, no repo-root join for a relative value) could put the one
    record of what the controller *would have* blocked outside the
    deployment's ReadWritePaths root, where the ``except OSError`` in
    :meth:`AdGuardController._audit` would silently drop it. So it goes through
    :func:`puffpai.statedir.resolve_runtime_dir`, the single resolver every
    other state directory uses.
    """
    global _rules_audit
    if _rules_audit is None:
        _rules_audit = os.path.join(
            resolve_runtime_dir(os.path.join(_REPO_ROOT, "data")),
            "zone3_dns_rules.jsonl")
    return _rules_audit


def _validate_domain(domain: str) -> str:
    domain = domain.strip().lower()
    if not domain:
        raise ValueError("Domain cannot be empty")
    if len(domain) > 253:
        raise ValueError(f"Domain too long: {len(domain)} chars")
    if not _DOMAIN_PATTERN.match(domain):
        raise ValueError(f"Invalid domain: {domain!r}")
    return domain


def _validate_client(client_ip: str) -> str:
    """A client selector for a ``$client=`` modifier: an IP, and only an IP.

    AdGuard also accepts CIDRs, MACs and client names here. We accept none of
    them: a name can contain the ``,`` and ``|`` that separate modifier values,
    a CIDR could widen one device's rule across the whole subnet, and a MAC
    cannot be resolved from inside the container's network namespace anyway
    (AdGuard would read its own bridge's ARP table, not the LAN's). One IP,
    validated by the stdlib, is the only selector that cannot be smuggled.
    """
    addr = ipaddress.ip_address(client_ip.strip())  # raises on garbage
    text = str(addr)
    if text in _own_addresses():
        raise ValueError(f"Refusing to target host-local address {text}")
    return text


def _own_addresses() -> set[str]:
    """Every IP the host answers to — rules must never target these."""
    own = {"127.0.0.1", "::1"}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            own.add(info[4][0])
    except OSError:
        pass
    return own


def block_rule(domain: str, client_ip: str | None = None) -> str:
    """The one place a blocking rule's TEXT is built. Validated, no comment."""
    domain = _validate_domain(domain)
    if client_ip:
        return f"||{domain}^$client={_validate_client(client_ip)}"
    return f"||{domain}^"


@dataclass
class AdGuardStatus:
    reachable: bool
    protection_enabled: bool
    version: str
    rules_count: int
    queries_today: int
    blocked_today: int


class AdGuardController:
    """Controls an AdGuard Home instance via its REST API.

    Args:
        base_url: e.g. "http://127.0.0.1:3000" — the appliance's own AdGuard.
        username/password: AdGuard Home web credentials (HTTP basic auth).
        enforce: False = monitor-first (rules recorded and written commented
                 out; AdGuard matches none of them).
        timeout: HTTP timeout seconds.
    """

    #: Whole-line markers delimiting the block this controller owns. Lines
    #: outside them belong to the owner and are never touched.
    BEGIN_MARKER = "!#### PuffPAI-Zone3 BEGIN — generated, do not edit inside"
    END_MARKER = "!#### PuffPAI-Zone3 END"
    #: A rule that is recorded but deliberately inert (monitor-first).
    OBSERVE_PREFIX = "! OBSERVE "
    #: Matches either marker regardless of the trailing prose, so a marker
    #: written by an older or newer build is still recognised and replaced.
    _BEGIN_RE = re.compile(r'^!#+\s*PuffPAI-Zone3\s+BEGIN\b')
    _END_RE = re.compile(r'^!#+\s*PuffPAI-Zone3\s+END\b')

    def __init__(self, base_url: str, username: str = "", password: str = "",
                 *, enforce: bool = False, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.auth = (username, password) if username else None
        self.enforce = enforce
        self.timeout = timeout

    # ── internals ─────────────────────────────────────────────────────
    # Both HTTP helpers go through puffpai.security.lan_http, which resolves
    # ``base_url`` and refuses any address that is not loopback/RFC1918/ULA
    # BEFORE a socket opens, then re-validates inside the connection factory.
    # ``base_url`` comes from config (`zone3.adguard_url`), i.e. a string an
    # operator edits; without this, one edited character would send the
    # household's AdGuard credential — and, on _post, its whole filtering
    # policy — to whatever that string now names. AddressRefused is caught
    # here and logged like any other failure: a refused write must read as
    # "the rule was not applied", never as success.

    def _get(self, path: str) -> Optional[dict | list]:
        try:
            r = lan_get(f"{self.base_url}/control{path}",
                        auth=self.auth, timeout=self.timeout)
            r.raise_for_status()
            return r.json() if r.text else {}
        except (requests.RequestException, ValueError, AddressRefused,
                ResolutionError) as exc:
            logger.warning("AdGuard GET %s failed: %s", path, exc)
            return None

    def _post(self, path: str, payload: dict) -> bool:
        try:
            r = lan_post(f"{self.base_url}/control{path}", json=payload,
                         auth=self.auth, timeout=self.timeout)
            r.raise_for_status()
            return True
        except (requests.RequestException, ValueError, AddressRefused,
                ResolutionError) as exc:
            logger.warning("AdGuard POST %s failed: %s", path, exc)
            return False

    def _audit(self, action: str, detail: dict, applied: bool) -> None:
        row = {"action": action, "detail": detail, "applied": applied,
               "enforce_mode": self.enforce,
               "at": datetime.now(timezone.utc).isoformat()}
        audit_path = _rules_audit_path()
        try:
            os.makedirs(os.path.dirname(audit_path), exist_ok=True)
            with open(audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError:
            pass

    def _current_rules(self) -> list[str]:
        st = self._get("/filtering/status") or {}
        return list(st.get("user_rules") or [])

    def _set_rules(self, rules: list[str]) -> bool:
        return self._post("/filtering/set_rules", {"rules": rules})

    # ── the owned block ───────────────────────────────────────────────
    def _split_rules(self, rules: list[str]) -> tuple[list[str], list[str]]:
        """``(owner_rules, our_block)`` — split at the markers.

        An unterminated BEGIN takes everything after it: the marker is ours, so
        what follows it was written by us. This is deliberate and deterministic;
        the alternative (refusing to write) would wedge the controller forever
        after one truncated update.
        """
        begin = end = None
        for i, line in enumerate(rules):
            if begin is None and self._BEGIN_RE.match(line.strip()):
                begin = i
            elif begin is not None and self._END_RE.match(line.strip()):
                end = i
                break
        if begin is None:
            return list(rules), []
        if end is None:
            return rules[:begin], rules[begin:]
        return rules[:begin] + rules[end + 1:], rules[begin:end + 1]

    def owner_rules(self) -> list[str]:
        """Rules the owner wrote by hand, i.e. everything outside our block."""
        return self._split_rules(self._current_rules())[0]

    def puffpai_rules(self) -> list[str]:
        """The lines inside our block, markers included."""
        return self._split_rules(self._current_rules())[1]

    def active_puffpai_rules(self) -> list[str]:
        """Only the lines inside our block that AdGuard will actually match.

        Comments (``!``…) are excluded, which is what makes an observe-mode
        block distinguishable from an enforcing one WITHOUT trusting the
        ``enforce`` flag we were constructed with.
        """
        return [ln for ln in self.puffpai_rules()
                if ln.strip() and not ln.lstrip().startswith("!")]

    def replace_rule_block(self, body: Iterable[str], *,
                           note: str = "") -> bool:
        """Rewrite our block to *body*, preserving every owner rule.

        *body* is rule TEXT (``||domain^…``) and ``!`` comment lines, in order.
        In monitor-first mode every rule line is prefixed with
        :data:`OBSERVE_PREFIX`, which makes it an AdGuard comment: recorded,
        readable in the UI, matched by nothing.
        """
        body = list(body)
        rendered: list[str] = [
            self.BEGIN_MARKER,
            f"! generated {datetime.now(timezone.utc).isoformat()}  "
            f"enforce={str(self.enforce).lower()}",
        ]
        if note:
            rendered.append(f"! {note}")
        if not self.enforce:
            rendered.append(
                "! MONITOR-FIRST: every rule below is commented out. AdGuard "
                "matches none of them.")
        for line in body:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("!"):
                rendered.append(stripped)
            elif self.enforce:
                rendered.append(stripped)
            else:
                rendered.append(self.OBSERVE_PREFIX + stripped)
        rendered.append(self.END_MARKER)

        owner, _previous = self._split_rules(self._current_rules())
        ok = self._set_rules(owner + rendered)
        self._audit("replace_rule_block",
                    {"rules": len(body), "owner_rules_preserved": len(owner),
                     "enforced": self.enforce}, ok)
        return ok

    def clear_rule_block(self) -> bool:
        """Remove our block entirely, leaving the owner's rules untouched."""
        owner, _ = self._split_rules(self._current_rules())
        ok = self._set_rules(owner)
        self._audit("clear_rule_block",
                    {"owner_rules_preserved": len(owner)}, ok)
        return ok

    # ── public API ───────────────────────────────────────────────────
    def is_reachable(self) -> bool:
        return self._get("/status") is not None

    def status(self) -> AdGuardStatus:
        st = self._get("/status")
        if not isinstance(st, dict):
            return AdGuardStatus(False, False, "", 0, 0, 0)
        stats = self._get("/stats")
        stats = stats if isinstance(stats, dict) else {}
        return AdGuardStatus(
            reachable=True,
            protection_enabled=bool(st.get("protection_enabled")),
            version=st.get("version", ""),
            # Our own ACTIVE rules, not every line in the file: a monitor-first
            # block is hundreds of lines and zero rules, and reporting the line
            # count as "rules" is how an operator concludes it is enforcing.
            rules_count=len(self.active_puffpai_rules()),
            queries_today=int(stats.get("num_dns_queries", 0) or 0),
            blocked_today=int(stats.get("num_blocked_filtering", 0) or 0),
        )

    def dns_info(self) -> dict:
        info = self._get("/dns_info")
        return info if isinstance(info, dict) else {}

    def upstreams(self) -> list[str]:
        return list(self.dns_info().get("upstream_dns") or [])

    def block_domain(self, domain: str, *, client_ip: str | None = None,
                     reason: str = "") -> bool:
        """Add one blocking rule to our block, keeping the rest of it."""
        rule = block_rule(domain, client_ip)
        body = [ln for ln in self.puffpai_rules()
                if not self._BEGIN_RE.match(ln.strip())
                and not self._END_RE.match(ln.strip())
                and not ln.strip().startswith("! generated ")
                and not ln.strip().startswith("! MONITOR-FIRST")]
        # Strip the observe prefix so the block is re-rendered for the CURRENT
        # enforce mode rather than freezing whichever mode wrote it first.
        body = [ln[len(self.OBSERVE_PREFIX):] if ln.startswith(self.OBSERVE_PREFIX)
                else ln for ln in body]
        if rule not in body:
            if reason:
                body.append(f"! {reason}")
            body.append(rule)
        self._audit("block_domain", {"rule": rule, "reason": reason},
                    self.enforce)
        return self.replace_rule_block(body)

    def unblock_domain(self, domain: str, *, client_ip: str | None = None) -> bool:
        rule = block_rule(domain, client_ip)
        body = []
        for ln in self.puffpai_rules():
            s = ln.strip()
            if self._BEGIN_RE.match(s) or self._END_RE.match(s):
                continue
            if s.startswith("! generated ") or s.startswith("! MONITOR-FIRST"):
                continue
            bare = s[len(self.OBSERVE_PREFIX):] if s.startswith(self.OBSERVE_PREFIX) else s
            if bare == rule:
                continue
            body.append(bare)
        self._audit("unblock_domain", {"rule": rule}, self.enforce)
        return self.replace_rule_block(body)

    def set_protection(self, enabled: bool) -> bool:
        """Global protection toggle.

        Turning protection OFF is always honoured — an appliance must always be
        able to stop filtering. Turning it ON in monitor-first mode is refused:
        it is the one call that could start blocking without the owner having
        flipped enforce, and a silent success there is exactly the failure this
        module exists to prevent.
        """
        if enabled and not self.enforce:
            self._audit("set_protection(refused)", {"enabled": True}, False)
            logger.warning(
                "Zone3 monitor-first: refusing to enable AdGuard protection "
                "while zone3.adguard_enforce is false")
            return False
        ok = self._post("/protection", {"enabled": bool(enabled)})
        self._audit("set_protection", {"enabled": bool(enabled)}, ok)
        return ok

    # ── per-client settings ──────────────────────────────────────────
    def clients(self) -> list[dict]:
        """Persistent clients AdGuard knows about (not auto-discovered ones)."""
        data = self._get("/clients")
        if not isinstance(data, dict):
            return []
        return list(data.get("clients") or [])

    def find_client(self, name: str) -> dict | None:
        return next((c for c in self.clients() if c.get("name") == name), None)

    def upsert_client(self, name: str, ids: list[str], *,
                      filtering_enabled: bool = True) -> bool:
        """Create or update a persistent client.

        ``filtering_enabled=False`` with ``use_global_settings=False`` is the
        mechanism behind ALWAYS_ALLOW: AdGuard skips filtering for that client
        entirely, so no rule anywhere — ours, the owner's, or a subscription
        somebody adds later — can block a medical device or a smoke alarm.
        This is applied in monitor-first mode too, because it can only ever
        allow more traffic, never less.
        """
        ids = [str(ipaddress.ip_address(i.strip())) for i in ids if str(i).strip()]
        if not ids:
            raise ValueError("a persistent client needs at least one IP")
        body = {
            "name": name,
            "ids": ids,
            "use_global_settings": False,
            "filtering_enabled": bool(filtering_enabled),
            "parental_enabled": False,
            "safebrowsing_enabled": False,
            "safe_search": {"enabled": False},
            "use_global_blocked_services": True,
            "blocked_services": [],
            "upstreams": [],
            "tags": [],
        }
        existing = self.find_client(name)
        if existing:
            ok = self._post("/clients/update", {"name": name, "data": body})
        else:
            ok = self._post("/clients/add", body)
        self._audit("upsert_client",
                    {"name": name, "ids": ids,
                     "filtering_enabled": bool(filtering_enabled)}, ok)
        return ok

    def delete_client(self, name: str) -> bool:
        if not self.find_client(name):
            return True
        ok = self._post("/clients/delete", {"name": name})
        self._audit("delete_client", {"name": name}, ok)
        return ok

    # ── query log ────────────────────────────────────────────────────
    def recent_queries(self, *, client_ip: str | None = None,
                       limit: int = 20) -> list[dict]:
        """Recent DNS questions, newest first, optionally for one client.

        Returns a trimmed projection — question, client, answer status, rule,
        time — rather than AdGuard's full record, so a security report never
        carries more of the household's browsing than it is showing.
        """
        path = f"/querylog?limit={max(1, min(int(limit), 500))}"
        if client_ip:
            path += f"&search={_validate_client(client_ip)}"
        data = self._get(path)
        if not isinstance(data, dict):
            return []
        out = []
        for row in (data.get("data") or []):
            question = row.get("question") or {}
            out.append({
                "time": row.get("time", ""),
                "client": row.get("client", ""),
                "domain": question.get("name", ""),
                "type": question.get("type", ""),
                "reason": row.get("reason", ""),
                "rule": row.get("rule", ""),
                "blocked": str(row.get("reason", "")).startswith("Filtered"),
            })
        return out
