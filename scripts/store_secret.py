#!/usr/bin/env python3
"""Store a secret in the OS keyring.

Usage:
    python scripts/store_secret.py HA_TOKEN
    python scripts/store_secret.py NODERED_API_TOKEN
    python scripts/store_secret.py PUFFPAI_API_KEY
    python scripts/store_secret.py ADGUARD_PASSWORD
    python scripts/store_secret.py --list          # show which names exist (never values)
    python scripts/store_secret.py --check NAME    # verify a name resolves

Value is prompted with no echo — it never touches shell history or a file.
The SecretsProvider chain (vault → keyring → env) picks it up on next boot.
"""
import getpass
import sys

SERVICE = "puffpai"
KNOWN = [
    "HA_TOKEN", "NODERED_API_TOKEN", "PUFFPAI_API_KEY", "ADGUARD_PASSWORD",
    "JELLYFIN_API_KEY",
]


def main() -> int:
    try:
        import keyring
    except ImportError:
        print("ERROR: pip install keyring   (in the PuffPAI venv)")
        return 1

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    if args[0] == "--list":
        for name in KNOWN:
            has = keyring.get_password(SERVICE, name) is not None
            print(f"  [{'x' if has else ' '}] {name}")
        return 0

    if args[0] == "--check":
        name = args[1]
        ok = keyring.get_password(SERVICE, name) is not None
        print(f"{name}: {'FOUND' if ok else 'not set'}")
        return 0 if ok else 1

    name = args[0].upper()
    if name not in KNOWN:
        print(f"note: {name!r} is not in the known list — storing anyway")
    value = getpass.getpass(f"Paste value for {name} (input hidden): ").strip()
    if not value:
        print("empty value, aborting")
        return 1
    confirm = getpass.getpass("Paste again to confirm: ").strip()
    if value != confirm:
        print("values differ, aborting")
        return 1
    keyring.set_password(SERVICE, name, value)
    print(f"stored {name} in OS keyring (service '{SERVICE}'). "
          f"Verify: python scripts/store_secret.py --check {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
