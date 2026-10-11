# Device Sentinel

Device Sentinel watches for devices that appear on your network or your Home
Assistant host and alerts you about the ones it has not seen before. It
**ships disabled**. When `/sentinel/status` reports `"enabled": false`, it is
off by design, not broken.

## What it does

`src/puffpai/device_sentinel.py` is a background daemon that scans three device
surfaces every `DEFAULT_INTERVAL_SECONDS` (120 s) and compares what it sees to a
persisted baseline:

| Surface | Source | Quarantine action for an unknown device |
|---|---|---|
| IP network | host ARP table | firewall block via `firewall_manager` |
| USB | Home Assistant `usb/scan` WS | alert + refuse auto-onboard; device stays powered (software cannot unplug it) |
| HA entities | `HADeviceWatcher` | HA rejection |

Anything not in the baseline is quarantined, surfaced as a chat-visible alert
through `/conversation`, and appended to `sentinel_alerts.jsonl` for audit.
Repeat alerts for the same device within ten minutes are suppressed. The first
scan seeds the baseline from everything currently visible, so a fresh start
does not alert on the whole house. The baseline lives in
`sentinel_baseline.json` alongside the alert log.

Both files resolve from `$PUFFPAI_DATA_DIR` when it is set (the systemd drop-in
sets it, so they land in `/data/puffpai/state/`, the writable state dir that
gets backed up) and fall back to the repo-relative `data/` when it is unset.
That keeps the audit trail out of the read-only install tree at
`/opt/puffpai/data/`.

At every daemon start a quarantine self-test blocks and then unblocks the
TEST-NET address `192.0.2.123` and records the result as a `selftest` alert.
A failed self-test is pushed to chat: it means IP quarantine cannot work on
this host.

### What a block really does on the Orin

IP quarantine goes through the firewall backend. On Linux that backend refuses
every block unless you opt in with `PUFFPAI_FIREWALL_BACKEND=nftables` and grant
the API unit `CAP_NET_ADMIN` (see `src/puffpai/firewall_manager.py`). Without
that opt-in, a new IP device is alerted as "flagged (no auto-action taken)"
and the startup self-test reports FAILED. HA entity rejection does act without
the firewall opt-in: a pending entity is marked rejected in PuffPAI's device
list.

## Why it ships off

Auto-quarantine acts on a baseline of known devices. A baseline that is
stale, or that was seeded while some of your devices were asleep or off the
network, treats those devices as intruders when they reappear, and quarantine
blocks devices you trust. The sentinel is therefore opt-in, and the safe way to
turn it on is to watch its alerts before letting anything block.

## The switch

`_sentinel_enabled()` in `src/puffpai/api_server.py` is the single reader of
`PUFFPAI_SENTINEL_ENABLED` (`1`/`true`/`yes` = on; **absent = off**). Both
`start_background_services()` and the `/sentinel/status` endpoint go through
it, so the daemon and the status report cannot disagree about whether the
daemon is supposed to exist.

When it is off, startup prints one line to the journal
(`journalctl -u puffpai-api`) saying so and pointing at this document.

## What `/sentinel/status` reports when disabled

The daemon's own status fields are always returned: `running`,
`interval_seconds`, `baseline_size`, `seeded`, `last_scan_at`,
`last_scan_counts`, `pending_alerts`, `total_alerts`, `recent_errors`,
`quarantine_selftest`. With the sentinel off they read as zeros and nulls,
because nothing has scanned. Added on top:

```json
{
  "enabled": false,
  "disabled_reason": "Off by default: Device Sentinel is opt-in. ...",
  "enable_with": "PUFFPAI_SENTINEL_ENABLED=1",
  "docs": "docs/DEVICE_SENTINEL.md"
}
```

When enabled, `enabled` is `true` and the three explanatory fields are absent.
Calling the endpoint while disabled is safe: it builds the lazy singleton,
which neither scans the network nor starts a thread.

Other endpoints: `GET /sentinel/alerts` (`?all=true` includes acknowledged
ones), `POST /sentinel/alerts/<id>/acknowledge`, `POST
/sentinel/alerts/<id>/approve` (acknowledge and lift the block), and `POST
/sentinel/scan` (scan now).

## Not the sentinel: the Node-RED `network_scan` flow

Node-RED runs its own `network_scan` flow every 15 minutes as an **inventory**
path. It is unrelated to Device Sentinel and keeps running while the sentinel
is off. A ticking flow alongside a silent sentinel is the expected picture,
not a fault.

## Turning it on

In order:

1. **Start from a fresh baseline, or none.** Check
   `/data/puffpai/state/sentinel_baseline.json` (and the fallback
   `/opt/puffpai/data/`). If a baseline exists and is not recent, delete it and
   let the first scan reseed from the network as it is today.
2. **Set the flag.** A systemd drop-in is the tidier home:

   ```ini
   # /etc/systemd/system/puffpai-api.service.d/20-sentinel.conf
   [Service]
   Environment="PUFFPAI_SENTINEL_ENABLED=1"
   ```

   `/etc/puffpai/api.env` works too, but it is the secrets file; prefer the
   drop-in for non-secret config. Then `sudo systemctl daemon-reload && sudo
   systemctl restart puffpai-api`.
3. **Observe for one to two weeks.** Leave `PUFFPAI_FIREWALL_BACKEND` unset so
   IP devices are only flagged, and confirm every alert is a genuinely new
   device. A freshly seeded baseline can still miss a device that was asleep
   during the seed scan; it shows up here as a false alarm instead of a false
   block. Approve the ones you trust (`/sentinel/alerts/<id>/approve`).
4. **Only then enable enforcement.** Opt in to the nftables backend
   (`PUFFPAI_FIREWALL_BACKEND=nftables` plus `CAP_NET_ADMIN` on the unit) and
   restart. Blocks can be lifted through the approve endpoint or the consent
   flow (`/sentinel/consent/...`), so a block stays a decision a person can
   reverse.

Skipping straight from step 2 to enforcement, trusting a baseline nobody has
watched, blocks devices you own.
