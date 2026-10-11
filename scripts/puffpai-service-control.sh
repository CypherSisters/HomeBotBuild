#!/bin/sh
# puffpai-service-control — the ONLY privileged action the Caretaker may take.
#
# Purpose
#   The Caretaker agent runs unprivileged (User=puffpai) and therefore cannot
#   restart a wedged unit. This wrapper is the narrow, auditable hole through
#   which it may do exactly one thing: `systemctl restart <unit>` for a unit on
#   the hardcoded allowlist below. Nothing else. No start, no stop, no mask, no
#   daemon-reload, no arbitrary unit name.
#
# Why a wrapper at all
#   A broad sudoers grant, for example passwordless
#   `systemctl start|stop|restart|status` on `puffpai-*` plus ollama, docker,
#   nftables, nvpmodel, apt-get, nft -f and reboot, is far wider than a
#   watchdog needs: with it, a compromised or prompt-injected backend process
#   can stop the firewall or the AI engine. This wrapper lets the operator
#   REPLACE such a grant with a single line (see INSTALL below) and drop the
#   appliance's privileged surface from "most of systemd" to "restart two
#   units". Alongside a broader grant the wrapper is still useful as
#   defence-in-depth: it re-checks the unit name in a second, root-owned place
#   that the unprivileged process cannot edit.
#
# INSTALL (run as root, on the appliance)
#   install -o root -g root -m 0755 puffpai-service-control.sh \
#           /opt/puffpai/bin/puffpai-service-control
#   # /opt/puffpai/bin must be root-owned and NOT writable by the puffpai user,
#   # otherwise the wrapper is just a file the caller can rewrite.
#   chown root:root /opt/puffpai/bin && chmod 0755 /opt/puffpai/bin
#
#   Then, in `visudo -f /etc/sudoers.d/puffpai`, REPLACE the broad systemctl
#   line with:
#
#     puffpai ALL=(root) NOPASSWD: /opt/puffpai/bin/puffpai-service-control restart puffpai-api, \
#                                  /opt/puffpai/bin/puffpai-service-control restart puffpai-dashboard
#
#   Listing the exact argv means sudo itself refuses any other verb or unit, so
#   the allowlist is enforced twice by root-owned policy and once by the caller.
#   (Changing sudoers needs an administrator's password at the box: it is deliberately
#   NOT something the appliance can do to itself.)
#
# Contract
#   argv:   restart <unit>
#   stdout: systemctl's output
#   exit:   0 success | 2 refused (bad argv / unit not allowlisted) | other =
#           systemctl's own exit status
#
# Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.

set -eu

SYSTEMCTL=/usr/bin/systemctl
[ -x "$SYSTEMCTL" ] || SYSTEMCTL=/bin/systemctl

# Keep this list in lockstep with RESTARTABLE_UNITS in
# src/puffpai/agents/caretaker.py. Deliberately excluded: ollama, docker,
# nftables (blast radius), and puffpai-nodered (its ExecStart is `docker compose
# up -d`, and a recreated container can land on a different bridge network than
# the firewall allows — a restart there is a network-policy change, not a heal).
ALLOWED_UNITS="puffpai-api puffpai-dashboard"

refuse() {
    printf 'puffpai-service-control: refused: %s\n' "$1" >&2
    exit 2
}

[ "$#" -eq 2 ] || refuse "expected exactly 2 arguments (restart <unit>), got $#"

verb=$1
unit=$2

[ "$verb" = "restart" ] || refuse "verb must be 'restart', got '$verb'"

# Strict pattern before the allowlist: lowercase, digits and single hyphens
# only. Rejects paths, globs, shell metacharacters, '@' template instances,
# '.service' suffixes and anything with whitespace or a newline in it.
case $unit in
    *[!a-z0-9-]*) refuse "unit name has illegal characters: '$unit'" ;;
    ''|-*|*-)     refuse "malformed unit name: '$unit'" ;;
esac

allowed=0
for candidate in $ALLOWED_UNITS; do
    [ "$unit" = "$candidate" ] && allowed=1 && break
done
[ "$allowed" -eq 1 ] || refuse "unit not on the allowlist: '$unit'"

exec "$SYSTEMCTL" restart "$unit"
