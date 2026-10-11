# Zone 3, Network Edge Security Setup

Zone 3 is the boundary between your home and the internet. It runs **AdGuard
Home** on this box as the house's DNS resolver: it answers every name your
devices ask for, records what was asked, and, once you turn protection on, refuses the names that exist only to phone home.

**Linux + Docker only.** Zone 3 binds port 53 and is driven by a compose file
and a systemd unit. On Windows, `install.ps1 -WithZone3` prints why and changes
nothing; run the edge on a Linux machine or a Raspberry Pi instead, or bring
your own (see the last section).

It ships **inert twice over**, and both have to be undone before anything on
your network changes:

1. **Nothing points at it.** Your router still hands out its own address as the
   house DNS server until you change that one field.
2. **Protection is off** in the seeded configuration. Even a device pointed
   here gets every answer resolved. This is *monitor-first*: the rules are
   loaded and readable, and AdGuard never matches them
   (`src/puffpai/zone3/adguard_controller.py:15-19`).

## Prerequisites

- Linux, with Docker Engine and the compose plugin
- Port 53 free. On Ubuntu the `systemd-resolved` stub usually holds it,   `install.sh --with-zone3` checks and tells you
- A LAN IPv4 address on this box, in `PUFFPAI_BIND_IP` (written by
  `install.sh --with-zone3`; `ZONE3_BIND_IP` overrides it)
- HomeBot installed and running

## Step 1: Start AdGuard Home

`install.sh --with-zone3` changes nothing on your network. It checks Docker,
checks port 53, detects your LAN IPv4, writes it into the env file, and prints
these commands. You run them:

```bash
./install.sh --with-zone3
set -a; . ~/.local/share/puffpai/homebot.env; set +a
docker compose -f docker-compose.zone3.yml up -d
python scripts/zone3_bootstrap_adguard.py
```

On the appliance build the same thing is a unit, so it survives a reboot:
`sudo systemctl start puffpai-zone3`.

Verify:
```bash
docker ps | grep puffpai-adguard
```

The bootstrap script completes AdGuard's first-run wizard non-interactively,
sets a Quad9-over-DoH upstream, turns the query log **on** and protection
**off**, and loads the PuffPAI rule block. It is idempotent.

Admin panel: `http://127.0.0.1:3000` from this box, or
`http://<PUFFPAI_BIND_IP>:3000` from the LAN. Set your own password during the
bootstrap, there is no shipped default, because a DNS sinkhole with a
published password is a sinkhole anyone on the LAN owns. If you would rather
the admin UI not be on the LAN at all, delete the LAN `:3000` line in
`docker-compose.zone3.yml`; the loopback publish is what the backend uses.

Ports 53/tcp+udp and 3000/tcp are published on the LAN address **only**, never
on `0.0.0.0`: binding `:53` on all interfaces would collide with the
`systemd-resolved` stub on `127.0.0.53:53`, which is what this box itself
resolves through. Zone 3 must never be able to break the box's own name
resolution. This release ships **no host packet filter**, so those port
bindings are the only thing limiting reach.

## Step 2: Enable Zone 3 in PuffPAI Config

Edit `config/puffpai_config.json` (keys and defaults from
`src/puffpai/config.py:375-387`):

```json
"zone3": {
    "enabled": true,
    "adguard_url": "http://127.0.0.1:3000",
    "adguard_username": "",
    "adguard_password": "",
    "adguard_enforce": false,
    "auto_discover": true,
    "default_mode": "balanced"
}
```

`enabled` ships `false`, Zone 3 is the one component that can take a whole
household off the internet, so a person turns it on. Load `adguard_password`
through the secrets provider rather than leaving it in this file.

`adguard_enforce` is the monitor-first switch, and it is the last tier of
three: `PUFFPAI_ZONE3_ENFORCE` (env) beats the owner overlay
(`$PUFFPAI_DATA_DIR/state/zone3_enforce.json`) beats this key beats off
(`src/puffpai/zone3/engine.py:33,199-207`). Leave it `false` until you have
read a few days of query logs. While it is false the controller writes every
rule into AdGuard as a commented-out line, prefixed `! OBSERVE`, so you can
read exactly what *would* be blocked.

## Step 3: Test Safely (No Router Changes)

Zone 3 does NOT require changing your router's DNS. Traffic from devices you
leave alone stays untouched. Three safe options, in order:

### Option A

Point only **this box** at the resolver, and leave every other device alone:

```bash
# Ask AdGuard directly, without changing any system resolver setting
dig @127.0.0.1 -p 53 example.com          # if :53 is published on loopback
dig @"$PUFFPAI_BIND_IP" example.com       # the LAN publication
```

Then read the query log in the admin UI. Nothing on the network has changed,
and nothing needs to be reverted.

To send this box's own lookups through it for a while, add the LAN address as a
DNS server on the interface (`resolvectl dns <iface> <PUFFPAI_BIND_IP>`, or the
equivalent in your network manager) and revert with
`resolvectl revert <iface>`. Do not disable `systemd-resolved`.

### Option B: Target specific smart devices only

For WiFi-connected smart devices, set their DNS individually:
- Smart TVs: Settings > Network > DNS > Manual > this box's LAN IP
- IoT hubs (Hue Bridge, etc.): usually configurable via their app
- Some devices: a static IP + DNS in your router's DHCP reservations

Devices you don't change keep using the router's default DNS. Gaming consoles,
work laptops, all unaffected.

### Option C: Full network (when ready)

Only when you're confident, point the router's DNS at this box. See "Full
Network Deployment" at the bottom.

## Step 4: Initialize Zone 3 Groups

One-time, writes the PuffPAI rule block into AdGuard:

```bash
curl -X POST http://localhost:8000/zone3/setup \
  -H "Authorization: Bearer $PUFFPAI_API_KEY"
```

That first call answers **409 with a proposal**, not an error: it tells you how
many rules across how many devices it is about to write, and whether Zone 3 is
enforcing (they take effect at once) or observing (they are written commented
out). To go ahead, mint a ticket and present it back:

```bash
TICKET=$(curl -s -X POST http://localhost:8000/zone3/confirm \
  -H "Authorization: Bearer $PUFFPAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"kind":"zone3_setup","target":"zone3","action":"sync"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["ticket"])')

curl -X POST http://localhost:8000/zone3/setup \
  -H "Authorization: Bearer $PUFFPAI_API_KEY" -H "Content-Type: application/json" \
  -d "{\"ticket\": \"$TICKET\"}"
```

The ticket is single-use, bound to that one act, and expires in 90 seconds.

Or in chat: *"Set up Zone 3"*, HomeBot shows you the proposal and stops.
Confirming is yours; no tool call can mint the ticket.

Rules are written as a delimited block between whole-line markers
(`!#### PuffPAI-Zone3 BEGIN` … `END`). Everything outside the markers is yours
and is preserved verbatim on every write.

## Step 5: Discover and Secure Devices

In chat:
- *"What devices are on my network?"*
- *"Secure my smart devices"*
- *"Show me Zone 3 status"*

Via API. Reads need only the API key; **every write needs a ticket** minted at
`POST /zone3/confirm` (see Step 4). Calling a write without one returns 409 and
a plain-language proposal of exactly what would change, that is the supported
way to preview it.

```bash
# Status and discovery (read-only)
curl -H "Authorization: Bearer $PUFFPAI_API_KEY" http://localhost:8000/zone3/status
curl -H "Authorization: Bearer $PUFFPAI_API_KEY" http://localhost:8000/zone3/discover

# Auto-secure a device        kind: zone3_secure  target: <ip>  action: secure
curl -X POST http://localhost:8000/zone3/secure \
  -H "Authorization: Bearer $PUFFPAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"ip": "192.168.50.50", "mac": "50:01:BB:AA:CC:DD", "ticket": "..."}'

# Change a device's security mode   kind: zone3_mode  target: <ip>  action: <mode>
curl -X PUT http://localhost:8000/zone3/device/192.168.50.50/mode \
  -H "Authorization: Bearer $PUFFPAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"mode": "lockdown", "ticket": "..."}'

# Stop managing a device      kind: zone3_remove  target: <ip>  action: remove
# (the ticket may ride X-Zone3-Ticket instead, since DELETE bodies are awkward)
curl -X DELETE http://localhost:8000/zone3/device/192.168.50.50 \
  -H "Authorization: Bearer $PUFFPAI_API_KEY" -H "X-Zone3-Ticket: ..."
```

In monitor-first mode these all still work, and all still write nothing that
blocks: `/zone3/status` reports `"mode": "monitor-first"` and the chat surface
says *watching, not blocking*.

## Security Modes

| Mode | What it blocks | Device impact |
|------|---------------|---------------|
| **LOCKDOWN** | ALL known outbound domains (telemetry + ads + cloud) | Cloud features stop working. Maximum privacy. |
| **BALANCED** | Telemetry, ads, and tracking. Allows cloud services. | Device works normally. No spying. |
| **OPEN** | Ads and cross-device tracking only. | Full functionality. Minimal blocking. |
| **ALWAYS_ALLOW** | Nothing. Safety-critical devices only. | Medical devices, fire alarms, never blocked. |

## Default Mode Assignments

| Device Type | Default Mode | Why |
|------------|-------------|-----|
| IoT Camera (Ring, Wyze) | LOCKDOWN | Cameras should not phone home |
| Printer | LOCKDOWN | No reason for internet access |
| Unknown device | LOCKDOWN | Deny by default |
| Smart Speaker (Echo, Google) | BALANCED | Needs cloud for voice, but stop tracking |
| Smart TV (Roku, Samsung) | BALANCED | Needs streaming, block telemetry |
| IoT Appliance (Hue, Ecobee) | BALANCED | Needs cloud API, block analytics |
| Computer | OPEN | User's primary device |
| Phone / Tablet | OPEN | User's primary device |
| Gaming Console | OPEN | Needs online services |
| Medical Device | ALWAYS_ALLOW | Never block, safety critical |
| Fire/CO Alarm | ALWAYS_ALLOW | Never block, safety critical |
| Router / AP | ALWAYS_ALLOW | Blocking breaks your network |

The controller never blocks the host's own interfaces, in any mode.

## Troubleshooting

### Container will not start
- `docker logs puffpai-adguard`
- `PUFFPAI_BIND_IP` unset: the compose file fails loudly rather than binding
  `0.0.0.0`. Set it in the compose project's `.env`.
- Port 53 already held: `sudo ss -lnup 'sport = :53'`. On Ubuntu that is
  normally `systemd-resolved` on `127.0.0.53`, which does not conflict with a
  publish on the LAN address. A full-interface listener does.

### Health check reports unhealthy
The probe is `GET /login.html`, not `/control/status`: once the instance is
configured, every `/control` endpoint answers 401 without credentials, and
busybox `wget` treats 401 as failure.

### Rules are visible in the UI but nothing is blocked
That is monitor-first working as designed, every rule line is commented out
and prefixed `! OBSERVE`. Flip `adguard_enforce` (or the overlay, or
`PUFFPAI_ZONE3_ENFORCE`) only after reviewing the query log. Turning AdGuard's
own protection switch on while the controller is in monitor-first mode is
refused, on purpose.

### Devices not resolving DNS
- Check the router's DNS points at this box's LAN IP
- Devices may need to renew DHCP: disconnect/reconnect WiFi
- Read the query log in the admin UI to confirm the query arrived at all

## Device Identification

Zone 3 identifies devices using multiple signals, in priority order:

1. **Home Assistant**, if HA manages the device, its friendly name is used
   (e.g. "Living Room TV", "Kitchen Echo"). HA is optional; without it this
   signal is simply absent.
2. **Zone 3 managed list**, previously classified devices are remembered
3. **MAC OUI lookup**, manufacturer from the MAC prefix
4. **Hostname patterns**, "iphone", "roku", and friends
5. **mDNS / SSDP**, network service discovery

If a device shows as "Unknown", classify it in the dashboard or in chat:
*"The device at 192.168.50.50 is my smart fridge"*.

## Full Network Deployment (Optional)

Only when you're ready to protect ALL devices:

1. Find this box's LAN IP (`ip -4 addr`, it is the one in `PUFFPAI_BIND_IP`)
2. Open your router's admin panel
3. Set **Primary DNS** / "Static DNS 1" to that address
4. Set **Secondary DNS** to a public resolver as a fallback
5. Save and reboot the router

That one field is the whole deployment, and reverting it is the whole rollback.

### Common Router Brands

- **NETGEAR**: Advanced > Setup > Internet Setup > DNS Address
- **TP-Link**: Advanced > Network > DHCP Server > Primary DNS
- **Linksys**: Connectivity > Local Network > DHCP Server
- **Xfinity/Comcast**: Gateway > Connection > WiFi > Edit > DNS
- **Google Wifi/Nest**: Settings > Network > Advanced > DNS (Custom)
- **Eero**: Settings > Network Settings > DNS > Custom DNS

## Bring your own edge

Zone 3 is one way to have a network edge, not the only one, and HomeBot does
not require it. If you already run **Pi-hole**, **AdGuard Home** elsewhere on
the LAN, or a **firewall appliance** (pfSense, OPNsense, UniFi), say so in the
wizard and keep it. The wizard records the answer as data, what you have, and
where, and installs nothing
(`src/puffpai/zone_bridge.py:205-211`). Nothing else in HomeBot changes; the
Bridge page reports the network edge as not installed here, and every other
zone behaves exactly as it does with Zone 3 running.

A Pi-hole controller (`src/puffpai/zone3/pihole_controller.py`) is also in
the tree. It is not the shipped path and is not wired to
`docker-compose.zone3.yml`.
