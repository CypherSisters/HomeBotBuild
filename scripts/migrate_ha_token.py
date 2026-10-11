#!/usr/bin/env python3
"""Move the Home Assistant long-lived token out of plaintext and into the vault.

The appliance keeps its HA long-lived access token in
``config/home_assistant_config.json`` in the clear (mode 600). This script is
the one-time migration that puts it in the vault instead.

**The owner runs this, once, with their PIN.** The vault's data key is wrapped
only under the PIN and the recovery code, so no script, installer, or service
account can open the vault without a human. That is the design working.

What it does, in order:

  1. Unlocks the vault with the PIN (``getpass`` -- never echoed, never in
     ``argv``, never logged).
  2. Enrolls the **service wrap** unless ``--strict``: a machine-bound wrap of
     the vault's Service Data Key, so the appliance can read the token after an
     unattended reboot with nobody awake. Requires that root has already
     installed the machine key with ``scripts/install_vault_service_key.sh``.
  3. Stores the token, AES-256-GCM encrypted, in the vault's boot-secret tier.
  4. **Verifies a live Home Assistant read using the vaulted copy** -- reading
     it back the same way the appliance will at boot, then calling HA with it.
  5. Only with ``--scrub``, and only if step 4 passed and a root-only backup of
     the original config exists, replaces the plaintext token with the
     ``MIGRATED_TO_VAULT`` marker.

Run it ON the appliance, THROUGH systemd-run, so the transient unit is handed
the machine key the same way ``puffpai-api`` is (see ``SYSTEMD_RUN_CMD``
below)::

    sudo systemd-run --pty --wait --collect \\
      --uid=puffpai --gid=puffpai \\
      --property=LoadCredential=puffpai-vault-service-key:/etc/puffpai/vault_service.key \\
      --setenv=PYTHONPATH=/opt/puffpai/src \\
      /opt/puffpai/venv/bin/python /opt/puffpai/scripts/migrate_ha_token.py

A plain ``sudo -u puffpai`` run is fine for ``--status`` and for ``--strict``,
but it CANNOT enroll the service wrap: the key is 0600 root:root by design and
an unprivileged process has no way to read it. The script says so, with the
command above, rather than quietly skipping the wrap.

``--status`` reports where the token lives and changes nothing. ``--dry-run``
prints every step it would take and changes nothing. Idempotent: re-running is
safe and reports what is already done.

No token, PIN, or recovery code is ever printed by this script.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

#: Root-only copy of the pre-migration config, taken by
#: scripts/install_vault_service_key.sh (which runs as root). --scrub refuses
#: without it: there must be exactly one rollback available, owned by root, and
#: the service user must not be able to alter it.
BACKUP_ENV = "PUFFPAI_HA_CONFIG_BACKUP"
DEFAULT_BACKUP = "/etc/puffpai/home_assistant_config.pre-vault.json"

#: HOW TO RUN THIS SO THE SERVICE WRAP CAN BE ENROLLED.
#:
#: The machine key is 0600 root:root, and this script must run AS puffpai (it
#: writes the vault keyring and the vault record; running it as root would
#: leave root-owned files the service cannot then write). Those two facts look
#: contradictory, and a plain `sudo -u puffpai` run cannot read the key.
#:
#: systemd-run resolves it: a transient unit gets the credential the same way
#: puffpai-api does -- a 0400 copy in its own tmpfs, in $CREDENTIALS_DIRECTORY
#: -- while the process itself runs unprivileged as puffpai. --pty gives
#: getpass a real terminal for the PIN.
SYSTEMD_RUN_CMD = """sudo systemd-run --pty --wait --collect \\
  --uid=puffpai --gid=puffpai \\
  --property=LoadCredential=puffpai-vault-service-key:/etc/puffpai/vault_service.key \\
  --setenv=PYTHONPATH=/opt/puffpai/src \\
  /opt/puffpai/venv/bin/python /opt/puffpai/scripts/migrate_ha_token.py"""


def backup_path() -> Path:
    return Path(os.environ.get(BACKUP_ENV, "").strip() or DEFAULT_BACKUP)


def _print_status() -> None:
    from puffpai.security.ha_vault import ha_token_migration_status
    from puffpai.security.ha_token_source import ha_token_status

    migration = ha_token_migration_status()
    live = ha_token_status()

    print("HA token")
    print("--------")
    print("  plaintext config       : " + migration["plaintext_config"])
    print("  plaintext token present: "
          + str(migration["plaintext_token_present"]))
    print("  migration marker set   : "
          + str(migration["migrated_marker_present"]))
    print("  plaintext fallback on  : "
          + str(migration["plaintext_fallback_allowed"])
          + "  (" + migration["fallback_env"] + ")")
    print()
    print("  resolved status        : " + str(live["status"]))
    print("  resolved from          : " + str(live["token_source"]))
    print("  strict mode            : " + str(live["strict_mode"]))
    print("  service wrap enrolled  : " + str(live["service_wrap_present"]))
    print("  machine key present    : " + str(live["machine_key_present"])
          + ("  (" + str(live["machine_key_source"]) + ")"
             if live["machine_key_present"] else ""))
    if live["machine_key_problem"]:
        print("  machine key problem    : " + str(live["machine_key_problem"]))
    print("  root-only backup       : " + str(backup_path())
          + "  exists=" + str(backup_path().exists()))


def _read_back_vaulted_token(vault, session_id: str, strict: bool):
    """Read the token back the way the APPLIANCE will, not the way we wrote it.

    Non-strict: the unattended machine-key path -- this is the actual proof
    that a 03:00 reboot will work. Strict: the PIN-session path, which is the
    only one strict mode leaves.
    """
    if not strict:
        token = vault.read_service_secret_unattended("ha_token.json")
        if isinstance(token, dict) and token.get("token"):
            return token["token"], "vault-service (unattended)"
    data = vault.read_service_secret("ha_token.json", session_id)
    if isinstance(data, dict) and data.get("token"):
        return data["token"], "vault-session (PIN)"
    return None, "not readable"


def _live_ha_check(token: str) -> tuple[bool, str]:
    """GET <ha_url>/api/ with the vaulted token. LAN only; short timeout."""
    try:
        import requests
        from puffpai.config import get_config
        url = get_config().ha.url.rstrip("/") + "/api/"
    except Exception as exc:
        return False, "could not build the check: " + type(exc).__name__
    try:
        resp = requests.get(
            url, headers={"Authorization": "Bearer " + token}, timeout=10)
    except Exception as exc:
        return False, "Home Assistant unreachable: " + type(exc).__name__
    if resp.status_code == 200:
        return True, "HTTP 200 from " + url
    return False, "HTTP " + str(resp.status_code) + " from " + url


def _next_steps(scrubbed: bool, strict: bool) -> None:
    print()
    print("Next steps")
    print("----------")
    if not scrubbed:
        print("  1. The plaintext token is STILL on disk. Re-run with --scrub")
        print("     once you are happy, to replace it with the marker.")
        print("     Same invocation -- --scrub re-runs the whole flow,")
        print("     enrolment included, so it needs the credential too:")
        print()
        print(SYSTEMD_RUN_CMD + " --scrub")
        return
    print("  1. Fail the plaintext fallback closed:")
    print("       sudo install -D -m 0644 \\")
    print("         /opt/puffpai/systemd/puffpai-api.service.d/"
          "20-ha-token-failclosed.conf \\")
    print("         /etc/systemd/system/puffpai-api.service.d/"
          "20-ha-token-failclosed.conf")
    print("       sudo systemctl daemon-reload "
          "&& sudo systemctl restart puffpai-api")
    print("  2. Confirm Home Assistant still answers:")
    print("       curl -sS -H \"X-API-Key: $PUFFPAI_API_KEY\" \\")
    print("         http://127.0.0.1:8000/system/status | "
          "python3 -m json.tool | grep -A8 home_assistant")
    print("     expect  \"status\": \"ready\"  and  "
          "\"token_source\": \""
          + ("vault-session" if strict else "vault-service") + "\"")
    if not strict:
        print("  3. Reboot once and confirm HA still works with nobody logged")
        print("     in. That is the whole point of the service wrap.")
    else:
        print("  3. STRICT MODE: Home Assistant stays down after every reboot")
        print("     until someone unlocks the vault with the PIN. That is what")
        print("     you asked for.")
    print("  4. Only then delete the rollback copy:")
    print("       sudo shred -u " + str(backup_path()))
    print()
    print("Rollback (any time before step 4):")
    print("  sudo cp " + str(backup_path())
          + " /opt/puffpai/config/home_assistant_config.json")
    print("  sudo chown puffpai:puffpai "
          "/opt/puffpai/config/home_assistant_config.json")
    print("  sudo chmod 600 /opt/puffpai/config/home_assistant_config.json")
    print("  sudo rm -f /etc/systemd/system/puffpai-api.service.d/"
          "20-ha-token-failclosed.conf")
    print("  sudo systemctl daemon-reload "
          "&& sudo systemctl restart puffpai-api")


def _dry_run(strict: bool, scrub: bool) -> int:
    from puffpai.security.ha_vault import ha_token_migration_status
    from puffpai.security.ha_token_source import ha_token_status
    from puffpai.security.vault import read_service_key

    _print_status()
    migration = ha_token_migration_status()
    live = ha_token_status()
    machine_key, key_reason = read_service_key()

    print()
    print("DRY RUN -- nothing below has been done")
    print("--------------------------------------")
    print("  would prompt for the vault PIN and unlock the vault")
    if strict:
        print("  would REMOVE any machine-bound service wrap (strict mode):")
        print("    Home Assistant will be unavailable after every reboot until")
        print("    someone unlocks the vault. That is the trade-off strict mode makes.")
    elif machine_key is not None:
        print("  would enroll the service wrap using the machine key at "
              + str(key_reason))
    elif "unreadable" in str(key_reason):
        print("  would SKIP the service wrap: " + str(key_reason))
        print("    -> the key exists but is 0600 root:root, as intended.")
        print("       Re-run this through systemd-run so the credential is")
        print("       handed in the same way puffpai-api gets it:")
        print()
        print(SYSTEMD_RUN_CMD)
    else:
        print("  would SKIP the service wrap: " + str(key_reason))
        print("    -> run scripts/install_vault_service_key.sh as root first,")
        print("       or the appliance cannot read the token after a reboot.")
    if migration["migrated_marker_present"]:
        print("  would store nothing: the config already holds the marker")
    elif migration["plaintext_token_present"]:
        print("  would store the token in the vault boot-secret tier "
              "(ha_token.json)")
    else:
        print("  would store nothing: no usable token in the plaintext config")
    print("  would read the token back the way the appliance will, then call")
    print("  Home Assistant with it and require HTTP 200")
    if scrub:
        print("  would then scrub the plaintext config to MIGRATED_TO_VAULT,")
        print("  but ONLY if that check passed and the root-only backup exists")
        print("    backup: " + str(backup_path())
              + "  exists=" + str(backup_path().exists()))
    else:
        print("  would NOT scrub (pass --scrub for that)")
    print()
    print("  current resolved source: " + str(live["token_source"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Migrate the Home Assistant token into the vault. "
                     "Owner-run, PIN-gated, idempotent."))
    parser.add_argument("--status", action="store_true",
                        help="report where the token lives; change nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every step this would take; change nothing")
    parser.add_argument(
        "--scrub", action="store_true",
        help=("also replace the plaintext token with the migration marker. "
              "Refused unless the live Home Assistant check passed AND the "
              "root-only backup exists."))
    parser.add_argument(
        "--strict", action="store_true",
        help=("do not enroll a machine-bound service wrap, and remove one if "
              "present. Home Assistant then needs a PIN unlock after every "
              "reboot."))
    args = parser.parse_args(argv)

    if args.status:
        _print_status()
        return 0

    from puffpai.security.ha_token_source import strict_mode
    strict = args.strict or strict_mode()

    if args.dry_run:
        return _dry_run(strict, args.scrub)

    if args.scrub and not backup_path().exists():
        print("Refusing to scrub: no root-only rollback copy at "
              + str(backup_path()) + "\n"
              "Take it as root first:\n"
              "  sudo /opt/puffpai/scripts/install_vault_service_key.sh "
              "--backup-only", file=sys.stderr)
        return 2

    from puffpai.security.ha_vault import (migrate_ha_token_to_vault,
                                           scrub_plaintext_ha_token)
    from puffpai.security import ha_token_source
    from puffpai.security.vault import Vault

    _print_status()
    print()
    print("  While this runs, do NOT change your PIN or generate a recovery")
    print("  code from the dashboard -- both rewrite the same vault keyring.")
    print()

    vault = Vault()
    if not vault.is_initialized:
        print("The vault is not set up on this box. Create a PIN first.",
              file=sys.stderr)
        return 1

    try:
        pin = getpass.getpass("Vault PIN (not echoed): ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled. Nothing changed.", file=sys.stderr)
        return 1
    if not pin:
        print("No PIN entered. Nothing changed.", file=sys.stderr)
        return 1

    try:
        session = vault.authenticate(pin)
    except Exception as exc:
        # Never interpolate anything that could carry the PIN.
        print("Could not unlock the vault: " + type(exc).__name__,
              file=sys.stderr)
        return 1
    finally:
        pin = ""
    sid = session.session_id

    # ---- 1. the boot path -------------------------------------------------
    if strict:
        vault.remove_service_wrap(sid)
        print("  service wrap : removed (strict mode)")
    else:
        enrolled = vault.enroll_service_wrap(sid)
        print("  service wrap : " + ("enrolled" if enrolled else "NOT enrolled"))
        if not enrolled:
            from puffpai.security.vault import read_service_key
            _, why = read_service_key()
            print("    " + str(why))
            if "unreadable" in str(why):
                print("    The key EXISTS but this process cannot read it --")
                print("    it is 0600 root:root by design. Re-run through")
                print("    systemd-run so the credential is handed in the same")
                print("    way puffpai-api gets it:")
                print()
                print(SYSTEMD_RUN_CMD)
            else:
                print("    Run scripts/install_vault_service_key.sh as root")
                print("    first, then re-run this script.")
            print("    Without the wrap the appliance cannot read the token")
            print("    after a reboot. Refusing to go further.", file=sys.stderr)
            return 3

    # ---- 2. store ---------------------------------------------------------
    # vault=vault is load-bearing, not decoration. Vault sessions are
    # instance-scoped (Vault.__init__ sets self._sessions = {}), so without it
    # the store runs against ha_vault._get_vault() -- a DIFFERENT object that
    # has never seen this session -- and reports "vault is locked", blaming a
    # PIN the owner typed correctly, and the migration cannot run at all.
    result = migrate_ha_token_to_vault(sid, scrub_plaintext=False, vault=vault)
    print("  stored in vault  : " + str(result["stored"]))
    print("  already migrated : " + str(result["already_stored"]))
    print("  " + result["reason"])
    if not (result["stored"] or result["already_stored"]):
        return 1
    ha_token_source.invalidate_cache()

    # ---- 3. verify BEFORE removing the thing that keeps the house working --
    token, how = _read_back_vaulted_token(vault, sid, strict)
    print()
    print("  read back via    : " + how)
    if not token:
        print("  The vaulted token could not be read back. NOT scrubbing.",
              file=sys.stderr)
        return 4
    ok, detail = _live_ha_check(token)
    token = ""  # drop the reference promptly
    print("  live HA check    : " + ("PASS" if ok else "FAIL") + " -- " + detail)
    if not ok:
        print("  Refusing to touch the plaintext config while the vaulted copy",
              file=sys.stderr)
        print("  has not proven it works. Fix Home Assistant and re-run.",
              file=sys.stderr)
        return 4

    # ---- 4. scrub, only now ----------------------------------------------
    scrubbed = False
    if args.scrub:
        scrubbed = scrub_plaintext_ha_token()
        print("  plaintext scrub  : " + ("done" if scrubbed else "FAILED"))
        ha_token_source.invalidate_cache()
        if not scrubbed:
            print("  The plaintext token is still on disk.", file=sys.stderr)

    print()
    _print_status()
    _next_steps(scrubbed, strict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
