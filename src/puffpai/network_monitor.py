"""
PuffPAI Network Traffic Monitor

Provides real-time visibility into outbound network connections, so anyone
can check that PuffPAI operates with zero cloud traffic during AI
conversations when lockdown mode is enabled.

Uses psutil for cross-platform network connection enumeration.
Falls back to netstat parsing if psutil is unavailable.
"""

from __future__ import annotations

import subprocess
import re
import socket
import time
from dataclasses import dataclass, field
from ipaddress import ip_address, IPv4Address, IPv6Address


@dataclass
class ConnectionInfo:
    """A single active network connection."""
    local_addr: str
    local_port: int
    remote_addr: str
    remote_port: int
    status: str
    pid: int | None = None
    process_name: str | None = None
    protocol: str = "tcp"

    @property
    def is_external(self) -> bool:
        """True if the remote address is outside localhost/LAN."""
        try:
            addr = ip_address(self.remote_addr)
        except ValueError:
            return True  # If we can't parse it, treat as external
        if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            return False
        if isinstance(addr, IPv4Address) and addr.is_private:
            return False
        if isinstance(addr, IPv6Address) and addr.is_private:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            'local': f"{self.local_addr}:{self.local_port}",
            'remote': f"{self.remote_addr}:{self.remote_port}",
            'status': self.status,
            'pid': self.pid,
            'process': self.process_name,
            'protocol': self.protocol,
            'is_external': self.is_external,
        }


@dataclass
class NetworkSnapshot:
    """A point-in-time snapshot of network activity."""
    timestamp: float = field(default_factory=time.time)
    connections: list[ConnectionInfo] = field(default_factory=list)
    error: str | None = None

    @property
    def total_connections(self) -> int:
        return len(self.connections)

    @property
    def external_connections(self) -> list[ConnectionInfo]:
        return [c for c in self.connections if c.is_external]

    @property
    def external_count(self) -> int:
        return len(self.external_connections)

    @property
    def is_isolated(self) -> bool:
        """True when there are zero external connections — network lockdown proof."""
        return self.external_count == 0

    def to_dict(self) -> dict:
        return {
            'timestamp': self.timestamp,
            'total_connections': self.total_connections,
            'external_connections': self.external_count,
            'is_isolated': self.is_isolated,
            'connections': [c.to_dict() for c in self.connections],
            'external': [c.to_dict() for c in self.external_connections],
            'error': self.error,
        }


class NetworkMonitor:
    """Cross-platform network connection monitor.

    Tries psutil first (fast, includes process info), falls back to
    netstat (always available on Windows/Linux/macOS).
    """

    def __init__(self):
        self._use_psutil = False
        try:
            import psutil
            self._psutil = psutil
            self._use_psutil = True
        except ImportError:
            self._psutil = None

    def snapshot(self) -> NetworkSnapshot:
        """Take a snapshot of all current network connections."""
        if self._use_psutil:
            return self._snapshot_psutil()
        return self._snapshot_netstat()

    def _snapshot_psutil(self) -> NetworkSnapshot:
        """Use psutil for fast, rich connection data."""
        snap = NetworkSnapshot()
        try:
            connections = self._psutil.net_connections(kind='inet')
            pid_names = {}
            for conn in connections:
                if conn.status == 'NONE' or not conn.raddr:
                    continue
                # Look up process name (cache per PID)
                proc_name = None
                if conn.pid:
                    if conn.pid not in pid_names:
                        try:
                            proc = self._psutil.Process(conn.pid)
                            pid_names[conn.pid] = proc.name()
                        except (self._psutil.NoSuchProcess,
                                self._psutil.AccessDenied):
                            pid_names[conn.pid] = None
                    proc_name = pid_names.get(conn.pid)

                snap.connections.append(ConnectionInfo(
                    local_addr=conn.laddr.ip if conn.laddr else '',
                    local_port=conn.laddr.port if conn.laddr else 0,
                    remote_addr=conn.raddr.ip if conn.raddr else '',
                    remote_port=conn.raddr.port if conn.raddr else 0,
                    status=conn.status,
                    pid=conn.pid,
                    process_name=proc_name,
                    protocol='tcp' if conn.type == socket.SOCK_STREAM else 'udp',
                ))
        except Exception as e:
            snap.error = f"psutil error: {e}"
        return snap

    def _snapshot_netstat(self) -> NetworkSnapshot:
        """Fallback: parse netstat output."""
        snap = NetworkSnapshot()
        try:
            result = subprocess.run(
                ['netstat', '-n', '-o'],
                capture_output=True, text=True, timeout=10, shell=False,
            )
            if result.returncode != 0:
                snap.error = f"netstat returned code {result.returncode}"
                return snap

            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith('Active') or line.startswith('Proto'):
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue

                proto = parts[0].lower()
                if proto not in ('tcp', 'udp'):
                    continue

                local = parts[1]
                remote = parts[2]
                status = parts[3] if len(parts) > 3 else 'UNKNOWN'
                pid = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None

                # Parse address:port
                local_addr, local_port = self._parse_addr(local)
                remote_addr, remote_port = self._parse_addr(remote)

                if remote_addr and remote_addr not in ('0.0.0.0', '*', '[::]'):
                    snap.connections.append(ConnectionInfo(
                        local_addr=local_addr,
                        local_port=local_port,
                        remote_addr=remote_addr,
                        remote_port=remote_port,
                        status=status,
                        pid=pid,
                        protocol=proto,
                    ))
        except FileNotFoundError:
            snap.error = "netstat not found"
        except subprocess.TimeoutExpired:
            snap.error = "netstat timed out"
        except Exception as e:
            snap.error = f"netstat error: {e}"
        return snap

    @staticmethod
    def _parse_addr(addr_str: str) -> tuple[str, int]:
        """Parse 'host:port' or '[ipv6]:port' into (host, port)."""
        try:
            if addr_str.startswith('['):
                # IPv6: [::1]:8080
                bracket_end = addr_str.index(']')
                host = addr_str[1:bracket_end]
                port = int(addr_str[bracket_end + 2:])
            else:
                last_colon = addr_str.rfind(':')
                host = addr_str[:last_colon]
                port = int(addr_str[last_colon + 1:])
            return host, port
        except (ValueError, IndexError):
            return addr_str, 0
