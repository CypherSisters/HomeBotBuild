#!/usr/bin/env bash
#
# PuffPAI kiosk session — minimal wall-display session for the appliance.
#
# Runs instead of GNOME: openbox (window manager) + Chromium in kiosk mode
# pointed at the dashboard. Installed to /opt/puffpai-kiosk/kiosk-session.sh
# by scripts/provision_kiosk.sh; launched by GDM via the "PuffPAI Kiosk"
# xsession (/usr/share/xsessions/puffpai-kiosk.desktop).
#
# Config: /etc/puffpai/kiosk.env (see kiosk/kiosk.env.example).
# Design + revert instructions: docs/KIOSK_MODE.md.

set -u

KIOSK_ENV=/etc/puffpai/kiosk.env
# shellcheck disable=SC1090
[ -r "$KIOSK_ENV" ] && . "$KIOSK_ENV"

KIOSK_URL="${KIOSK_URL:-http://127.0.0.1:3003/dashboard}"
# Night window during which the display is blanked (24h HH:MM, box-local
# time). Set either to empty in kiosk.env to keep the screen on 24/7.
DISPLAY_OFF="${DISPLAY_OFF:-23:00}"
DISPLAY_ON="${DISPLAY_ON:-06:30}"

# Snap apps need a session bus; GDM normally provides one, this is a fallback.
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && command -v dbus-launch >/dev/null; then
    eval "$(dbus-launch --sh-syntax --exit-with-session)"
fi

# A wall display has no input, so input-idle blanking would blank it
# immediately and keep it black. Disable every idle path; blanking is done
# on schedule below via explicit "dpms force off/on".
xset s off
xset s noblank
xset dpms 0 0 0

openbox &
command -v unclutter >/dev/null && unclutter -idle 5 -root &

is_night() {
    local now off on
    now=$(date +%H:%M); off="$DISPLAY_OFF"; on="$DISPLAY_ON"
    if [[ "$off" < "$on" ]]; then
        [[ "$now" == "$off" || ( "$now" > "$off" && "$now" < "$on" ) ]]
    else
        [[ "$now" == "$off" || "$now" > "$off" || "$now" < "$on" ]]
    fi
}

if [ -n "$DISPLAY_OFF" ] && [ -n "$DISPLAY_ON" ]; then
    (
        prev=""
        while sleep 60; do
            if is_night; then cur=night; else cur=day; fi
            if [ "$cur" != "$prev" ]; then
                if [ "$cur" = night ]; then
                    xset dpms force off
                else
                    xset dpms force on
                fi
                prev="$cur"
            fi
        done
    ) &
fi

# The dashboard service may still be coming up after boot.
for _ in $(seq 1 120); do
    curl -fsS -o /dev/null --max-time 2 "$KIOSK_URL" && break
    sleep 1
done

CHROMIUM_FLAGS=(
    --kiosk "$KIOSK_URL"
    --noerrdialogs
    --disable-infobars
    --no-first-run
    --disable-session-crashed-bubble
    --hide-crash-restore-bubble
    --autoplay-policy=no-user-gesture-required
    --overscroll-history-navigation=0
)

# The session must never exit: GDM only auto-logs-in at boot, so a dead
# session would leave the greeter on the wall until the next reboot.
while true; do
    chromium "${CHROMIUM_FLAGS[@]}"
    sleep 2
done
