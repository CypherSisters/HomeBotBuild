"""
PuffPAI Firewall Manager — Cross-Platform Backend Abstraction

Supports multiple firewall backends:
  - WindowsFirewall: netsh advfirewall (Windows dev)
  - NftablesFirewall: nft (Linux, opt-in — see get_firewall_backend)
  - UnavailableFirewall: honest refusal where PuffPAI cannot write rules
  - MockFirewall: in-memory (testing)

WHY LINUX DEFAULTS TO "UNAVAILABLE"
-----------------------------------
The API must get its backend from the factory, not the back-compat alias
``FirewallManager`` (= WindowsFirewall): on the HomeBot appliance the alias
would shell every block/unblock/lockdown out to ``netsh`` on ARM64 Linux and
die with "netsh not found".

Selecting correctly is not enough, because on that appliance NO in-process
backend can work:

  * This release does not ship a host packet-filter unit or script; the rule
    builders here are used only when an operator enables the firewall
    integration.
  * ``puffpai-api.service`` runs ``User=puffpai`` with ``AmbientCapabilities=``
    empty and ``NoNewPrivileges=yes``. No CAP_NET_ADMIN, and NoNewPrivileges
    neuters setuid, so the process cannot reach ``sudo`` either.
  * ``sudo -n nft ...`` / ``sudo -n iptables ...`` answer "a password is
    required": the NOPASSWD grant covers ``systemctl <puffpai-*|ollama|docker>``
    and ``nft -f /etc/nftables.conf`` — nothing that could add a per-device
    rule.

Making a per-device block work would therefore require granting the 250 KB
Flask API CAP_NET_ADMIN (or a NOPASSWD iptables rule), i.e. giving any RCE in
it the ability to rewrite INPUT and DOCKER-USER — including the base rules
this appliance's whole inbound posture rests on. That is a privilege
EXPANSION, not a fix, so the capability is made honest instead: the write
paths refuse with ``NOT_AVAILABLE_CODE`` and the registry stops advertising
them. Enabling the nftables path stays possible on hardware where it really
works, but only by explicit opt-in (``PUFFPAI_FIREWALL_BACKEND=nftables``),
never by platform guess.

Security: All IP inputs are validated using Python's ipaddress module.
All subprocess calls use shell=False with argument lists to prevent injection.
"""

import subprocess
import json
import os
import sys
import ipaddress
import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import PurePath

from puffpai.statedir import resolve_runtime_dir

#: Repo root — three directory levels up from ``src/puffpai/firewall_manager.py``.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── state location (lazy) ────────────────────────────────────────────
#
# The block list and the protocol/port rule file are what the firewall is
# reconstructed from on restart. They must not live under the code tree,
# which on a hardened unit (ProtectSystem=strict) is read-only:
# _save_blocked_list() would raise and a device the user had just blocked
# would be unblocked again at the next start. Both resolve through
# statedir.resolve_runtime_dir on first use.
#
# The two defaults deliberately differ and are preserved verbatim: the block
# list has always sat at the REPO ROOT (<repo>/blocked_devices.json, not under
# data/), the rule file under <repo>/data. With $PUFFPAI_DATA_DIR set both
# land flat in it, which is the layout a deployment actually wants.
_DEFAULT_BLOCKED_DIR = _REPO_ROOT
_DEFAULT_RULES_DIR = os.path.join(_REPO_ROOT, "data")

#: Cached resolutions; ``None`` = unresolved. A reload re-runs these
#: assignments, which is how tests re-resolve after changing the environment.
_blocked_file_cache: str | None = None
_rules_file_cache: str | None = None

#: Test/ops override hooks under the historical constant names: ``None`` means
#: "resolve", any path pins that file. tests/test_firewall_manager.py isolates
#: the block list with ``mock.patch("puffpai.firewall_manager._BLOCKED_FILE",
#: ...)``, which lands here and wins over both the cache and the environment.
#:
#: Deliberately plain globals rather than a PEP 562 ``__getattr__`` shim that
#: synthesises a resolved path on read. ``monkeypatch.setattr`` records the old
#: value with getattr, so against a shim it records a *resolved path* and
#: re-assigns it at teardown -- a temporary override becomes permanent,
#: $PUFFPAI_DATA_DIR stops being honoured for the rest of the process, and the
#: firewall's state quietly goes back to the read-only code tree. Observed live
#: on another module during this migration.
_BLOCKED_FILE = None
_RULES_FILE = None


def _override(name: str) -> str | None:
    """Return an explicitly assigned module-level path, if there is one.

    str or Path both count -- silently ignoring a Path-valued override and
    resolving around it would put firewall state somewhere the operator did
    not ask for. Nothing else does: a bare ``MagicMock`` from a decorator-style
    ``@patch`` satisfies os.fspath (it answers ``__fspath__`` with a junk name
    like ``MagicMock/mock/140...``), and honouring that would write the block
    list to a junk path relative to the CWD.
    """
    value = globals().get(name)
    if not isinstance(value, (str, PurePath)):
        return None
    return os.fspath(value) or None


def _blocked_file() -> str:
    """Absolute path of the blocked-device list, resolved on first use."""
    global _blocked_file_cache
    override = _override("_BLOCKED_FILE")
    if override:
        return override
    if _blocked_file_cache is None:
        _blocked_file_cache = os.path.join(
            resolve_runtime_dir(_DEFAULT_BLOCKED_DIR), "blocked_devices.json")
    return _blocked_file_cache


def _rules_file() -> str:
    """Absolute path of the protocol/port rule file, resolved on first use."""
    global _rules_file_cache
    override = _override("_RULES_FILE")
    if override:
        return override
    if _rules_file_cache is None:
        _rules_file_cache = os.path.join(
            resolve_runtime_dir(_DEFAULT_RULES_DIR), "firewall_rules.json")
    return _rules_file_cache


#: Machine-readable code returned by every refused firewall WRITE when this
#: platform has no backend that could carry it out. Surfaces (the API routes,
#: the dashboard, the capability registry) key off this exact string rather
#: than parsing prose, so the message can be reworded without breaking them.
NOT_AVAILABLE_CODE = "not_available_on_this_platform"

_MAX_BLOCKED_DEVICES = 500
_MAX_RULES = 200
_RULE_NAME_PATTERN = re.compile(r'^[A-Za-z0-9_\-]+$')
_VALID_DIRECTIONS = {'inbound', 'outbound', 'both'}
_VALID_ACTIONS = {'allow', 'block'}
_VALID_PROTOCOLS = {'TCP', 'UDP', 'ALL'}
_PORT_PATTERN = re.compile(r'^(\*|\d{1,5}(-\d{1,5})?)$')


# ---------------------------------------------------------------------------
# Shared validation (used by all backends)
# ---------------------------------------------------------------------------

def validate_ip(ip: str):
    """Validate and parse an IP address string.

    Raises ValueError if the IP is invalid, loopback, multicast,
    reserved, or otherwise unsafe for firewall rules.
    """
    if not isinstance(ip, str):
        raise ValueError("IP address must be a string")
    ip = ip.strip()
    if not ip:
        raise ValueError("IP address cannot be empty")

    # Reject shell metacharacters before parsing
    dangerous_chars = set(';&|`$(){}[]<>!#\'"\\%\n\r\t ')
    if any(c in dangerous_chars for c in ip):
        raise ValueError(f"IP address contains invalid characters: {ip!r}")

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError(f"Invalid IP address: {ip!r}")

    if addr.is_loopback:
        raise ValueError(f"Cannot block loopback address {addr}")
    if addr.is_multicast:
        raise ValueError(f"Cannot block multicast address {addr}")
    if addr.is_link_local:
        raise ValueError(f"Cannot block link-local address {addr}")
    if addr.is_unspecified:
        raise ValueError(f"Cannot block unspecified address {addr}")
    if isinstance(addr, ipaddress.IPv4Address):
        if addr == ipaddress.IPv4Address('255.255.255.255'):
            raise ValueError("Cannot block broadcast address 255.255.255.255")
    if addr.is_reserved:
        raise ValueError(f"Cannot block reserved address {addr}")

    return addr


def _sanitize_rule_name(ip_str: str) -> str:
    """Build a safe firewall rule name from a validated IP string."""
    safe = ip_str.replace('.', '_').replace(':', '_')
    safe = re.sub(r'[^A-Za-z0-9_]', '', safe)
    rule_name = f"PuffPAI_Block_{safe}"
    if not _RULE_NAME_PATTERN.match(rule_name):
        raise ValueError(f"Generated rule name is invalid: {rule_name!r}")
    return rule_name


def _validate_reason(reason: str) -> str:
    """Sanitize the reason string for storage."""
    if not isinstance(reason, str):
        reason = str(reason)
    reason = reason[:200].strip()
    reason = re.sub(r'[\x00-\x1f\x7f]', '', reason)
    return reason if reason else "User requested"


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

class FirewallBackend(ABC):
    """Abstract base class for firewall backends.

    All backends share:
      - IP validation (via validate_ip)
      - Blocked device list persistence
      - Consistent return format: dict with 'success' and 'message'
    """

    def __init__(self, blocked_file: str | None = None):
        # Construction stays inert. api_server.py builds a FirewallManager at
        # MODULE SCOPE, so resolving (or reading) here would import
        # puffpai.config -- and with it the secrets provider -- in the middle
        # of importing the API server, which is the cycle statedir.py exists to
        # avoid. The constructor argument is kept as an override only; the path
        # and the list behind it are resolved on first real use.
        self._blocked_file_override = blocked_file
        self._blocked_devices: list | None = None

    @property
    def _blocked_file(self) -> str:
        """This backend's block list: constructor override, else resolved."""
        override = self._blocked_file_override
        if override is not None:
            # A Path argument is normalised; anything else is handed back as
            # given, exactly as when this was a plain attribute.
            return os.fspath(override) if isinstance(override, PurePath) else override
        return _blocked_file()

    @property
    def blocked_devices(self) -> list:
        """The block list, read from disk on first access (never at import)."""
        if self._blocked_devices is None:
            self._blocked_devices = self._load_blocked_list()
        return self._blocked_devices

    @blocked_devices.setter
    def blocked_devices(self, devices: list) -> None:
        self._blocked_devices = devices

    def _load_blocked_list(self) -> list:
        try:
            with open(self._blocked_file, 'r') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_blocked_list(self):
        path = self._blocked_file
        # The state root can be brand new on a relocated deployment; an
        # unblockable-because-unwritable list is the failure this migration is
        # about, so make the parent rather than raise into the caller.
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(self.blocked_devices, f, indent=2)

    @abstractmethod
    def _add_block_rule(self, ip_str: str, rule_name: str) -> dict:
        """Platform-specific: add a firewall block rule. Returns result dict."""

    @abstractmethod
    def _remove_block_rule(self, ip_str: str, rule_name: str) -> dict:
        """Platform-specific: remove a firewall block rule. Returns result dict."""

    @abstractmethod
    def _set_lockdown(self, enable: bool) -> dict:
        """Platform-specific: enable/disable network lockdown. Returns result dict."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether this backend's firewall tool is reachable."""

    @property
    def backend_name(self) -> str:
        return self.__class__.__name__

    #: Can this backend carry out a WRITE (block / unblock / lockdown / rule)?
    #:
    #: Deliberately a cheap declared property, NOT ``is_available()``: that one
    #: shells out to netsh/nft with a 5 s timeout, and ``get_status`` is polled
    #: by the dashboard. This answers the different, static question "does a
    #: backend that could ever work exist here at all", which is what a surface
    #: needs to decide whether to offer the control.
    writes_supported: bool = True

    #: Why writes are unsupported, in plain language. Empty when they are.
    unavailable_reason: str = ""

    def block_device(self, ip: str, reason: str = "User requested") -> dict:
        """Block a device by IP address."""
        try:
            validated_addr = validate_ip(ip)
        except ValueError as e:
            return {'success': False, 'message': f'Invalid IP: {e}'}

        reason = _validate_reason(reason)
        ip_str = str(validated_addr)

        if any(d.get('ip') == ip_str for d in self.blocked_devices):
            return {'success': False, 'message': f'{ip_str} is already blocked'}

        if len(self.blocked_devices) >= _MAX_BLOCKED_DEVICES:
            return {'success': False, 'message': f'Max {_MAX_BLOCKED_DEVICES} blocked devices reached'}

        rule_name = _sanitize_rule_name(ip_str)
        result = self._add_block_rule(ip_str, rule_name)

        if result['success']:
            self.blocked_devices.append({
                'ip': ip_str, 'rule_name': rule_name,
                'reason': reason, 'blocked_at': datetime.now().isoformat(),
            })
            self._save_blocked_list()

        return result

    def unblock_device(self, ip: str) -> dict:
        """Remove a firewall block rule for a device."""
        try:
            validated_addr = validate_ip(ip)
        except ValueError as e:
            return {'success': False, 'message': f'Invalid IP: {e}'}

        ip_str = str(validated_addr)
        rule_name = _sanitize_rule_name(ip_str)
        result = self._remove_block_rule(ip_str, rule_name)

        if result['success']:
            self.blocked_devices = [d for d in self.blocked_devices if d.get('ip') != ip_str]
            self._save_blocked_list()

        return result

    def lockdown_mode(self, enable: bool = True) -> dict:
        """Enable or disable full network lockdown."""
        if not isinstance(enable, bool):
            return {'success': False, 'message': 'enable must be a boolean'}
        result = self._set_lockdown(enable)
        if result['success']:
            self._lockdown_active = enable
        return result

    def get_status(self) -> dict:
        status = {
            'blocked_count': len(self.blocked_devices),
            'blocked_devices': self.blocked_devices,
            'backend': self.backend_name,
            'lockdown_active': getattr(self, '_lockdown_active', False),
            # Additive, and the whole point of the refusing backend: a reader of this
            # endpoint must be able to tell "no devices are blocked" from
            # "blocking is not something this box can do".
            'available': bool(self.writes_supported),
        }
        if not self.writes_supported:
            status['code'] = NOT_AVAILABLE_CODE
            status['reason'] = self.unavailable_reason
        return status

    # Aliases for load/save used by tests
    def load_blocked_list(self) -> list:
        self.blocked_devices = self._load_blocked_list()
        return self.blocked_devices

    def save_blocked_list(self):
        self._save_blocked_list()

    # ------------------------------------------------------------------
    # Protocol/port rule management
    # ------------------------------------------------------------------

    def _load_rules(self) -> list:
        try:
            with open(_rules_file(), 'r') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save_rules(self, rules: list):
        rules_file = _rules_file()
        os.makedirs(os.path.dirname(rules_file), exist_ok=True)
        with open(rules_file, 'w') as f:
            json.dump(rules, f, indent=2)

    def get_rules(self) -> list:
        return self._load_rules()

    def add_rule(self, name: str, direction: str, action: str,
                 protocol: str, port: str) -> dict:
        name = name.strip()[:80]
        direction = direction.lower()
        action = action.lower()
        protocol = protocol.upper()
        port = port.strip() or '*'

        if not name:
            return {'success': False, 'message': 'Rule name is required'}
        if direction not in _VALID_DIRECTIONS:
            return {'success': False, 'message': f'Invalid direction: {direction}'}
        if action not in _VALID_ACTIONS:
            return {'success': False, 'message': f'Invalid action: {action}'}
        if protocol not in _VALID_PROTOCOLS:
            return {'success': False, 'message': f'Invalid protocol: {protocol}'}
        if not _PORT_PATTERN.match(port):
            return {'success': False, 'message': f'Invalid port: {port}'}
        if port != '*':
            port_nums = [int(p) for p in port.split('-')]
            if any(p < 1 or p > 65535 for p in port_nums):
                return {'success': False, 'message': f'Port out of range (1-65535): {port}'}
            if len(port_nums) == 2 and port_nums[0] > port_nums[1]:
                return {'success': False, 'message': f'Invalid port range: {port}'}

        rules = self._load_rules()
        if len(rules) >= _MAX_RULES:
            return {'success': False, 'message': f'Max {_MAX_RULES} rules reached'}

        rule_id = f"rule_{datetime.now().strftime('%Y%m%d%H%M%S')}_{os.urandom(3).hex()}"

        os_result = self._apply_os_rule(rule_id, name, direction, action, protocol, port)
        os_applied = os_result.get('success', False)

        rule = {
            'id': rule_id,
            'name': name,
            'direction': direction,
            'action': action,
            'protocol': protocol,
            'port': port,
            'status': 'active' if os_applied else 'pending',
            'created_at': datetime.now().isoformat(),
        }
        rules.append(rule)
        self._save_rules(rules)

        msg = f'Rule "{name}" created'
        if not os_applied:
            msg += ' (OS rule requires admin privileges — rule saved but not yet enforced)'
        return {'success': True, 'message': msg, 'rule': rule}

    def delete_rule(self, rule_id: str) -> bool:
        rules = self._load_rules()
        target = None
        for r in rules:
            if r['id'] == rule_id:
                target = r
                break
        if not target:
            return False

        self._remove_os_rule(target)
        rules = [r for r in rules if r['id'] != rule_id]
        self._save_rules(rules)
        return True

    @abstractmethod
    def _apply_os_rule(self, rule_id: str, name: str, direction: str,
                       action: str, protocol: str, port: str) -> dict:
        """Platform-specific: create an OS-level firewall rule."""

    @abstractmethod
    def _remove_os_rule(self, rule: dict) -> dict:
        """Platform-specific: remove an OS-level firewall rule."""


# ---------------------------------------------------------------------------
# Windows backend (netsh advfirewall)
# ---------------------------------------------------------------------------

class WindowsFirewall(FirewallBackend):
    """Windows firewall backend using netsh advfirewall."""

    def _add_block_rule(self, ip_str: str, rule_name: str) -> dict:
        cmd = [
            'netsh', 'advfirewall', 'firewall', 'add', 'rule',
            f'name={rule_name}', 'dir=out', 'action=block',
            f'remoteip={ip_str}',
        ]
        return self._run_netsh(cmd, f'Blocked {ip_str}', rule_name)

    def _remove_block_rule(self, ip_str: str, rule_name: str) -> dict:
        cmd = [
            'netsh', 'advfirewall', 'firewall', 'delete', 'rule',
            f'name={rule_name}',
        ]
        return self._run_netsh(cmd, f'Unblocked {ip_str}')

    def _set_lockdown(self, enable: bool) -> dict:
        policy = 'blockinbound,blockoutbound' if enable else 'blockinbound,allowoutbound'
        action = "enabled" if enable else "disabled"
        cmd = ['netsh', 'advfirewall', 'set', 'allprofiles', 'firewallpolicy', policy]
        return self._run_netsh(cmd, f'Lockdown mode {action}')

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                ['netsh', 'advfirewall', 'show', 'allprofiles'],
                shell=False, capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _apply_os_rule(self, rule_id: str, name: str, direction: str,
                       action: str, protocol: str, port: str) -> dict:
        directions = ['in', 'out'] if direction == 'both' else \
                     ['in'] if direction == 'inbound' else ['out']
        for d in directions:
            safe_name = f"PuffPAI_{rule_id}_{d}"
            cmd = [
                'netsh', 'advfirewall', 'firewall', 'add', 'rule',
                f'name={safe_name}', f'dir={d}', f'action={action}',
            ]
            if protocol != 'ALL':
                cmd.append(f'protocol={protocol}')
                if port != '*':
                    cmd.append(f'localport={port}')
            else:
                cmd.append('protocol=any')
            result = self._run_netsh(cmd, f'Rule {name} applied ({d})')
            if not result['success']:
                return result
        return {'success': True, 'message': f'Rule "{name}" applied'}

    def _remove_os_rule(self, rule: dict) -> dict:
        rule_id = rule['id']
        direction = rule.get('direction', 'both')
        directions = ['in', 'out'] if direction == 'both' else \
                     ['in'] if direction == 'inbound' else ['out']
        for d in directions:
            safe_name = f"PuffPAI_{rule_id}_{d}"
            cmd = [
                'netsh', 'advfirewall', 'firewall', 'delete', 'rule',
                f'name={safe_name}',
            ]
            self._run_netsh(cmd, f'Rule removed ({d})')
        return {'success': True, 'message': 'Rule removed'}

    @staticmethod
    def _run_netsh(cmd: list, success_msg: str, rule_name: str = None) -> dict:
        try:
            result = subprocess.run(cmd, shell=False, capture_output=True,
                                    text=True, timeout=30)
            if "Ok." in result.stdout or result.returncode == 0:
                out = {'success': True, 'message': success_msg}
                if rule_name:
                    out['rule'] = rule_name
                return out
            else:
                # netsh writes most errors (incl. "requires elevation") to
                # STDOUT, not stderr -- capture both so failures are visible.
                detail = " ".join(
                    part.strip() for part in (result.stdout, result.stderr)
                    if part and part.strip()
                ) or f"netsh exited {result.returncode} with no output"
                if "elevation" in detail.lower() or "administrator" in detail.lower():
                    detail += " (run the PuffPAI sentinel elevated / as admin)"
                return {'success': False, 'message': detail}
        except subprocess.TimeoutExpired:
            return {'success': False, 'message': 'Firewall command timed out'}
        except FileNotFoundError:
            return {'success': False, 'message': 'netsh not found -- is this Windows?'}
        except Exception as e:
            return {'success': False, 'message': f'Firewall error: {type(e).__name__}'}


# ---------------------------------------------------------------------------
# Linux backend (nftables)
# ---------------------------------------------------------------------------

class NftablesFirewall(FirewallBackend):
    """Linux firewall backend using nftables (nft).

    Manages a PuffPAI-specific nftables table and set for blocked IPs.
    Table: inet puffpai
    Set:   blocked_v4 (type ipv4_addr) / blocked_v6 (type ipv6_addr)
    """

    _TABLE = 'inet puffpai'
    _SET_V4 = 'blocked_v4'
    _SET_V6 = 'blocked_v6'

    def _add_block_rule(self, ip_str: str, rule_name: str) -> dict:
        addr = ipaddress.ip_address(ip_str)
        set_name = self._SET_V6 if addr.version == 6 else self._SET_V4
        cmd = ['nft', 'add', 'element', self._TABLE, set_name,
               '{', ip_str, '}']
        return self._run_nft(cmd, f'Blocked {ip_str}', rule_name)

    def _remove_block_rule(self, ip_str: str, rule_name: str) -> dict:
        addr = ipaddress.ip_address(ip_str)
        set_name = self._SET_V6 if addr.version == 6 else self._SET_V4
        cmd = ['nft', 'delete', 'element', self._TABLE, set_name,
               '{', ip_str, '}']
        return self._run_nft(cmd, f'Unblocked {ip_str}')

    def _set_lockdown(self, enable: bool) -> dict:
        # Drop or accept all output traffic via chain policy
        policy = 'drop' if enable else 'accept'
        action = "enabled" if enable else "disabled"
        cmd = ['nft', 'chain', self._TABLE, 'output',
               '{', 'policy', policy, '}']
        return self._run_nft(cmd, f'Lockdown mode {action}')

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                ['nft', 'list', 'tables'],
                shell=False, capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _apply_os_rule(self, rule_id: str, name: str, direction: str,
                       action: str, protocol: str, port: str) -> dict:
        chain = 'output' if direction in ('outbound', 'both') else 'input'
        nft_action = 'drop' if action == 'block' else 'accept'
        rule_parts = ['nft', 'add', 'rule', self._TABLE, chain]
        if protocol != 'ALL':
            rule_parts.extend([protocol.lower(), 'dport', port])
        rule_parts.extend([nft_action, 'comment', f'"PuffPAI_{rule_id}"'])
        result = self._run_nft(rule_parts, f'Rule "{name}" applied')
        if not result['success']:
            return result
        if direction == 'both':
            chain2_parts = ['nft', 'add', 'rule', self._TABLE, 'input']
            if protocol != 'ALL':
                chain2_parts.extend([protocol.lower(), 'dport', port])
            chain2_parts.extend([nft_action, 'comment', f'"PuffPAI_{rule_id}"'])
            self._run_nft(chain2_parts, f'Rule "{name}" applied (input)')
        return {'success': True, 'message': f'Rule "{name}" applied'}

    def _remove_os_rule(self, rule: dict) -> dict:
        rule_id = rule['id']
        for chain in ('output', 'input'):
            try:
                list_cmd = ['nft', '-a', 'list', 'chain', self._TABLE, chain]
                result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=10)
                for line in result.stdout.splitlines():
                    if f'PuffPAI_{rule_id}' in line:
                        handle_match = re.search(r'handle\s+(\d+)', line)
                        if handle_match:
                            handle = handle_match.group(1)
                            del_cmd = ['nft', 'delete', 'rule', self._TABLE,
                                       chain, 'handle', handle]
                            self._run_nft(del_cmd, 'Rule removed')
            except Exception:
                pass
        return {'success': True, 'message': 'Rule removed'}

    def ensure_table(self) -> dict:
        """Create the PuffPAI nftables table and sets if they don't exist."""
        commands = [
            ['nft', 'add', 'table', self._TABLE],
            ['nft', 'add', 'set', self._TABLE, self._SET_V4,
             '{', 'type', 'ipv4_addr;', '}'],
            ['nft', 'add', 'set', self._TABLE, self._SET_V6,
             '{', 'type', 'ipv6_addr;', '}'],
            ['nft', 'add', 'chain', self._TABLE, 'output',
             '{', 'type', 'filter', 'hook', 'output', 'priority', '0;', '}'],
            ['nft', 'add', 'rule', self._TABLE, 'output',
             'ip', 'daddr', f'@{self._SET_V4}', 'drop'],
            ['nft', 'add', 'rule', self._TABLE, 'output',
             'ip6', 'daddr', f'@{self._SET_V6}', 'drop'],
        ]
        for cmd in commands:
            result = self._run_nft(cmd, 'Table setup OK')
            if not result['success']:
                return result
        return {'success': True, 'message': 'PuffPAI nftables table ready'}

    @staticmethod
    def _run_nft(cmd: list, success_msg: str, rule_name: str = None) -> dict:
        try:
            result = subprocess.run(cmd, shell=False, capture_output=True,
                                    text=True, timeout=30)
            if result.returncode == 0:
                out = {'success': True, 'message': success_msg}
                if rule_name:
                    out['rule'] = rule_name
                return out
            else:
                detail = " ".join(
                    part.strip() for part in (result.stdout, result.stderr)
                    if part and part.strip()
                ) or f"nft exited {result.returncode} with no output"
                if "permission" in detail.lower() or "not permitted" in detail.lower():
                    detail += " (nft requires root)"
                return {'success': False, 'message': detail}
        except subprocess.TimeoutExpired:
            return {'success': False, 'message': 'nft command timed out'}
        except FileNotFoundError:
            return {'success': False, 'message': 'nft not found -- is nftables installed?'}
        except Exception as e:
            return {'success': False, 'message': f'Firewall error: {type(e).__name__}'}


# ---------------------------------------------------------------------------
# Unavailable backend (no writable firewall on this platform)
# ---------------------------------------------------------------------------

class UnavailableFirewall(FirewallBackend):
    """Refuses every write, in plain language, and persists nothing.

    The alternative this replaces was worse in both directions: on the
    appliance ``WindowsFirewall`` failed with "netsh not found -- is this
    Windows?" (true, useless, and it still advertised the control), and
    ``MockFirewall`` would have reported ``success: True`` for a block that
    never happened, which is the one outcome a security control must never
    produce.

    READS still work. ``get_status`` and the device scan are honest and useful
    on any platform; only the mutations are refused.
    """

    writes_supported = False

    _DEFAULT_REASON = (
        "PuffPAI cannot change firewall rules on this platform, so it will not "
        "pretend to. Nothing was blocked or unblocked."
    )

    def __init__(self, blocked_file: str | None = None, reason: str = ""):
        self.unavailable_reason = reason or self._DEFAULT_REASON
        super().__init__(blocked_file)

    def _refuse(self) -> dict:
        return {'success': False, 'code': NOT_AVAILABLE_CODE,
                'message': self.unavailable_reason}

    def _add_block_rule(self, ip_str: str, rule_name: str) -> dict:
        return self._refuse()

    def _remove_block_rule(self, ip_str: str, rule_name: str) -> dict:
        return self._refuse()

    def _set_lockdown(self, enable: bool) -> dict:
        return self._refuse()

    def _apply_os_rule(self, rule_id: str, name: str, direction: str,
                       action: str, protocol: str, port: str) -> dict:
        return self._refuse()

    def _remove_os_rule(self, rule: dict) -> dict:
        return self._refuse()

    def is_available(self) -> bool:
        return False

    def add_rule(self, name: str, direction: str, action: str,
                 protocol: str, port: str) -> dict:
        """Refuse outright rather than save a rule as 'pending'.

        The base implementation writes the rule to disk and returns
        ``success: True`` with a "not yet enforced" note when the OS call
        fails, which is right for a Windows box that merely needs elevating
        and wrong here: nothing will ever come along and enforce it, so a
        saved rule would be a standing lie in the rules list.
        """
        return self._refuse()

    def delete_rule(self, rule_id: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# Mock backend (testing)
# ---------------------------------------------------------------------------

class MockFirewall(FirewallBackend):
    """In-memory firewall backend for testing. No real firewall calls."""

    def __init__(self, blocked_file: str | None = None, available: bool = True):
        self._mock_available = available
        self._mock_rules: set[str] = set()
        self._lockdown = False
        super().__init__(blocked_file)

    def _add_block_rule(self, ip_str: str, rule_name: str) -> dict:
        self._mock_rules.add(ip_str)
        return {'success': True, 'message': f'Blocked {ip_str}', 'rule': rule_name}

    def _remove_block_rule(self, ip_str: str, rule_name: str) -> dict:
        self._mock_rules.discard(ip_str)
        return {'success': True, 'message': f'Unblocked {ip_str}'}

    def _set_lockdown(self, enable: bool) -> dict:
        self._lockdown = enable
        action = "enabled" if enable else "disabled"
        return {'success': True, 'message': f'Lockdown mode {action}'}

    def _apply_os_rule(self, rule_id: str, name: str, direction: str,
                       action: str, protocol: str, port: str) -> dict:
        return {'success': True, 'message': f'Mock rule "{name}" applied'}

    def _remove_os_rule(self, rule: dict) -> dict:
        return {'success': True, 'message': 'Mock rule removed'}

    def is_available(self) -> bool:
        return self._mock_available


# ---------------------------------------------------------------------------
# Backend factory
# ---------------------------------------------------------------------------

#: Environment switch that opts a Linux host INTO a real write backend.
#: Absent/unrecognised means "no writes", which is the safe direction: a box
#: that cannot enforce a block must not offer one.
FIREWALL_BACKEND_ENV = "PUFFPAI_FIREWALL_BACKEND"

_LINUX_REASON = (
    "PuffPAI does not manage per-device firewall rules on this appliance: the "
    "API process runs unprivileged and cannot add or remove rules, and this "
    "release does not install a host packet filter. Nothing was changed. To "
    "block a device, use your router, or enable a firewall backend (see "
    "PUFFPAI_FIREWALL_BACKEND)."
)

_OTHER_PLATFORM_REASON = (
    "PuffPAI has no firewall backend for this operating system, so it will not "
    "claim to have blocked anything. Nothing was changed."
)


def get_firewall_backend(blocked_file: str | None = None) -> FirewallBackend:
    """Return the firewall backend for the current platform.

    Windows -> ``WindowsFirewall`` (netsh).
    Linux   -> ``NftablesFirewall`` ONLY when ``PUFFPAI_FIREWALL_BACKEND`` opts
               in ("nft"/"nftables"); otherwise ``UnavailableFirewall``.
    other   -> ``UnavailableFirewall``.

    The Linux default is deliberately the refusing backend, not nftables — see
    the module docstring. Selecting a write backend by platform guess can
    produce a "firewall" that reports a Windows engine on an ARM64 Jetson; a
    host that can genuinely enforce rules says so explicitly,
    and that opt-in is the same deliberate act as granting the unit
    CAP_NET_ADMIN, which it also requires.
    """
    if sys.platform == 'win32':
        return WindowsFirewall(blocked_file)

    if sys.platform.startswith('linux'):
        choice = (os.environ.get(FIREWALL_BACKEND_ENV) or "").strip().lower()
        if choice in ('nft', 'nftables'):
            return NftablesFirewall(blocked_file)
        return UnavailableFirewall(blocked_file, reason=_LINUX_REASON)

    return UnavailableFirewall(blocked_file, reason=_OTHER_PLATFORM_REASON)


# ---------------------------------------------------------------------------
# Backwards compatibility: FirewallManager = WindowsFirewall
# ---------------------------------------------------------------------------

FirewallManager = WindowsFirewall


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    fw = get_firewall_backend()
    print(f"Using backend: {fw.backend_name}")

    if len(sys.argv) < 2:
        print("Usage: python firewall_manager.py [block|unblock|lockdown|status] [ip]")
        sys.exit(1)

    action = sys.argv[1]

    if action == "block" and len(sys.argv) >= 3:
        result = fw.block_device(sys.argv[2], "Manual block via CLI")
        print(json.dumps(result, indent=2))
    elif action == "unblock" and len(sys.argv) >= 3:
        result = fw.unblock_device(sys.argv[2])
        print(json.dumps(result, indent=2))
    elif action == "lockdown":
        enable = sys.argv[2].lower() == "on" if len(sys.argv) >= 3 else True
        result = fw.lockdown_mode(enable)
        print(json.dumps(result, indent=2))
    elif action == "status":
        result = fw.get_status()
        print(json.dumps(result, indent=2))
    else:
        print("Unknown action. Use: block, unblock, lockdown, status")
        sys.exit(1)
