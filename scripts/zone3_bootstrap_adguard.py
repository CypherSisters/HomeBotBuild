#!/usr/bin/env python3
"""Seed and configure the Zone 3 AdGuard Home instance. Idempotent.

Run it on the appliance as root, with the venv interpreter (it drives docker,
and it imports bcrypt, which system python3 does not have):

    sudo /opt/puffpai/venv/bin/python /opt/puffpai/scripts/zone3_bootstrap_adguard.py
    sudo /opt/puffpai/venv/bin/python /opt/puffpai/scripts/zone3_bootstrap_adguard.py --check

WHAT IT DOES, IN ORDER
  1. Ensures an admin credential exists in ``config/.zone3.env`` (mode 0600,
     never committed, never printed). Generates a 32-character password on
     first run; reuses it forever after.
  2. If the AdGuard conf volume has no ``AdGuardHome.yaml``, renders
     ``config/zone3/AdGuardHome.seed.yaml`` into it with a bcrypt hash of that
     password, owned by uid 65534.

     WHY SEED AT ALL: AdGuard Home refuses its FIRST launch as anything but
     uid 0 ("this is the first launch of adguard home; you must run it as
     administrator"). A pre-seeded config means there is no first launch, so
     the resolver runs unprivileged from the very first boot.
  3. Brings the compose stack up and waits for the API.
  4. Re-applies the whole policy over the REST API — upstreams, filtering
     config, protection state, and PuffPAI's rule block rebuilt from the
     managed-device ledger. The API is authoritative; the seed file only has
     to be good enough to boot.

WHAT IT NEVER DOES
  * It never turns protection on. ``zone3.adguard_enforce`` in
    puffpai_config.json decides that, and ``POST /zone3/enforce`` flips the
    running engine. Monitor-first is not a suggestion.
  * It never touches the router, systemd-resolved, or any device's DNS.
  * It never prints, logs or echoes the admin password.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

COMPOSE_FILE = REPO_ROOT / "docker-compose.zone3.yml"
SEED_TEMPLATE = REPO_ROOT / "config" / "zone3" / "AdGuardHome.seed.yaml"
ENV_FILE = REPO_ROOT / "config" / ".zone3.env"
CONF_VOLUME = "puffpai_adguard_conf"
CONTAINER = "puffpai-adguard"
#: Small, already-present image used only to write into the named volume.
HELPER_IMAGE = "debian:bookworm-slim"
PASSWORD_PLACEHOLDER = "__ADGUARD_BCRYPT__"


def _run(cmd: list[str], *, input_bytes: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_bytes, capture_output=True,
                          check=check)


# ── credential ────────────────────────────────────────────────────────
def read_env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        return {}
    out = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def ensure_credential() -> tuple[str, str]:
    """``(username, password)``, generating and persisting one if needed."""
    env = read_env()
    if env.get("ADGUARD_PASSWORD"):
        return env.get("ADGUARD_USERNAME", "puffpai"), env["ADGUARD_PASSWORD"]

    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(32))
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start — not chmod-after, which leaves a window
    # in which the credential is world-readable.
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# PuffPAI Zone 3 (AdGuard Home) admin credential.\n"
                "# Owner-readable only (0600). Never commit, never log.\n"
                "ADGUARD_USERNAME=puffpai\n"
                f"ADGUARD_PASSWORD={password}\n")
    print(f"[zone3] generated a new admin credential -> {ENV_FILE} (mode 0600)")
    return "puffpai", password


def bcrypt_hash(password: str) -> str:
    import bcrypt
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


# ── seeding ───────────────────────────────────────────────────────────
def conf_seeded() -> bool:
    r = _run(["docker", "run", "--rm", "-v", f"{CONF_VOLUME}:/c",
              HELPER_IMAGE, "test", "-s", "/c/AdGuardHome.yaml"], check=False)
    return r.returncode == 0


def seed_conf(password: str) -> None:
    if not SEED_TEMPLATE.is_file():
        raise SystemExit(f"missing seed template: {SEED_TEMPLATE}")
    text = SEED_TEMPLATE.read_text(encoding="utf-8")
    if PASSWORD_PLACEHOLDER not in text:
        raise SystemExit(f"{SEED_TEMPLATE} has no {PASSWORD_PLACEHOLDER}")
    rendered = text.replace(PASSWORD_PLACEHOLDER, bcrypt_hash(password))
    _run(["docker", "volume", "create", CONF_VOLUME])
    # Piped through stdin so the rendered config — which carries the password
    # hash — is never written to a temp file on disk or shown in a process
    # argument list.
    _run(["docker", "run", "--rm", "-i", "-v", f"{CONF_VOLUME}:/c",
          HELPER_IMAGE, "sh", "-c",
          "cat > /c/AdGuardHome.yaml && chmod 600 /c/AdGuardHome.yaml && "
          "chown -R 65534:65534 /c"],
         input_bytes=rendered.encode("utf-8"))
    print(f"[zone3] seeded AdGuardHome.yaml into volume {CONF_VOLUME}")


def compose_up() -> None:
    _run(["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"])
    print("[zone3] compose up -d")


# ── policy ────────────────────────────────────────────────────────────
def wait_for_api(base_url: str, auth: tuple[str, str], timeout: int = 90):
    import requests
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            r = requests.get(f"{base_url}/control/status", auth=auth, timeout=5)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except requests.RequestException as exc:
            last = type(exc).__name__
        time.sleep(2)
    raise SystemExit(f"[zone3] AdGuard API did not come up ({last})")


def apply_policy(base_url: str, auth: tuple[str, str]) -> None:
    import requests

    def post(path: str, payload: dict) -> None:
        r = requests.post(f"{base_url}/control{path}", json=payload,
                          auth=auth, timeout=15)
        r.raise_for_status()

    # Upstream: Quad9 over DoH, bootstrapped by Quad9's own plain-DNS
    # addresses (a DoH URL is a hostname, and something has to resolve it), and
    # falling back to Quad9 over TLS if DoH is blocked or broken. This is the
    # ONE outbound path Zone 3 creates; it is declared in
    # docs/WHAT_CAN_LEAVE_YOUR_HOUSE.md.
    post("/dns_config", {
        "upstream_dns": ["https://dns.quad9.net/dns-query"],
        "bootstrap_dns": ["9.9.9.9", "149.112.112.112"],
        "fallback_dns": ["tls://dns.quad9.net"],
        "upstream_mode": "load_balance",
        "dnssec_enabled": True,
        "resolve_clients": True,
        # Never ask anyone outside the house to reverse-resolve an address
        # inside it: that would leak the shape of the LAN to the resolver.
        "use_private_ptr_resolvers": False,
        "local_ptr_upstreams": [],
    })
    # No subscription lists, no periodic list fetch. PuffPAI's rules are the
    # only rules, so turning protection on blocks exactly what was reviewed.
    post("/filtering/config", {"enabled": True, "interval": 0})

    from puffpai.config import get_config
    from puffpai.zone3 import AdGuardController, PolicyEngine
    cfg = get_config()
    enforce = bool(cfg.zone3.adguard_enforce)

    controller = AdGuardController(base_url, auth[0], auth[1], enforce=enforce)
    engine = PolicyEngine(adguard=controller)
    # set_enforce is the single switch: it rewrites the rule block for the
    # current mode AND sets protection to match, in one place.
    result = engine.set_enforce(enforce)
    print(f"[zone3] enforce={enforce} protection={result.get('protection_enabled')} "
          f"sync={json.dumps(result.get('sync', {}), sort_keys=True)}")
    if not enforce:
        print("[zone3] MONITOR-FIRST: every query is logged, nothing is blocked.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="report state and change nothing")
    ap.add_argument("--url", default="http://127.0.0.1:3000",
                    help="AdGuard base URL (default: loopback publish)")
    args = ap.parse_args()

    username, password = ensure_credential()
    seeded = conf_seeded()
    if args.check:
        print(json.dumps({
            "credential_file": str(ENV_FILE),
            "credential_present": bool(password),
            "conf_seeded": seeded,
            "compose_file": str(COMPOSE_FILE),
        }, indent=2))
        return 0

    if not seeded:
        seed_conf(password)
    else:
        print("[zone3] AdGuardHome.yaml already present; leaving it alone")

    compose_up()
    status = wait_for_api(args.url, (username, password))
    print(f"[zone3] AdGuard {status.get('version')} up, "
          f"protection_enabled={status.get('protection_enabled')}")
    apply_policy(args.url, (username, password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
