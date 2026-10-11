# Kiosk mode, the appliance's wall-display face

## Why this exists

A browser left open on the Orin's own GNOME desktop, showing the animated
dashboard, can pin the GPU near 99% around the clock (about 28 W sustained on
the GPU/SOC rail). That trips the board's over-current protection ("System
throttled due to over-current" notifications) and slows everything, including
inference. Contributing factors: snap Chromium runs with GPU acceleration
disabled on Jetson (software rendering pins a CPU core too), GNOME Shell
compositing costs about 18% CPU on top, and screen blanking is often disabled.

If you want a monitor that keeps showing the dashboard, kiosk mode makes the
display a designed feature with a power budget, instead of a desktop someone
left logged in.

## Architecture (what `scripts/provision_kiosk.sh` installs)

GDM stays, but auto-logs the service user into a custom X session
("PuffPAI Kiosk", `/usr/share/xsessions/puffpai-kiosk.desktop`) instead of
GNOME. Reusing GDM keeps logind seat management, rootless X, and the session
D-Bus that snap Chromium needs, with no hand-rolled xinit units.

The session (`/opt/puffpai-kiosk/kiosk-session.sh`, config
`/etc/puffpai/kiosk.env`):

* **openbox** as a bare window manager (replaces GNOME Shell) + `unclutter`
  to hide the cursor.
* **Chromium `--kiosk`** on the dashboard, with
  `--autoplay-policy=no-user-gesture-required` so Puff's voice plays, crash
  bubbles suppressed, and an auto-restart loop (the session must never exit,
  GDM only auto-logs-in at boot). Waits up to 120 s for the dashboard
  service after boot. The normal Chromium profile is reused, so the saved
  tile layout (localStorage) carries over.
* **Scheduled blanking, not idle blanking.** A wall display receives no
  input, so input-idle DPMS would blank it immediately and forever. All idle
  paths are disabled; instead the session forces the display off during a
  configurable night window (default 23:00 to 06:30 box-local, i.e. back on
  before a 07:00 morning briefing) and back on in the morning.

The session alone does not stop the dashboard's animations from costing CPU
while the display is on. The dashboard's kiosk display mode does that: it
engages the motion governor so the page stays static and repaints nothing
between interactions. Set `KIOSK_URL` in `/etc/puffpai/kiosk.env` to the
dashboard URL with `?puffMode=kiosk` appended, for example
`http://127.0.0.1:3003/dashboard?puffMode=kiosk`.

## Install / update / revert

```
sudo ./scripts/provision_kiosk.sh            # install or update (idempotent)
sudo systemctl restart gdm3                  # apply now (or reboot)

sudo ./scripts/provision_kiosk.sh --revert   # normal GNOME login again
```

`scripts/install_orin.sh --with-kiosk` runs the same script.

`--revert` only disables auto-login and points the session back at Ubuntu;
packages and kiosk files stay installed.

## Getting a desktop back

The full GNOME desktop is still installed. Options, best first:

* From another machine: `sudo ./scripts/provision_kiosk.sh --revert && sudo
  systemctl restart gdm3`, log in at the greeter as usual.
* At the box: Ctrl+Alt+F3 for a text console, log in, run the same revert.
* One-off: at the GDM greeter (after revert, or if auto-login is off),
  the gear menu offers both "Ubuntu" and "PuffPAI Kiosk" sessions.

## Security note

Auto-login means the physical console boots straight into a session, but
that session only contains a full-screen browser on localhost. Reaching a
shell still requires a VT switch and a password at getty. For a home
appliance whose threat model already includes physical access, this is
an accepted trade-off.
