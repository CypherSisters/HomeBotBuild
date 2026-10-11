# puffpai-radio — the appliance audio player.
#
# radio_player.py runs this image with the entrypoint overridden to mpv, on the
# host network, as uid 1000, with the PulseAudio socket bind-mounted in. It
# serves BOTH live internet radio (one stream URL) and Jellyfin playlist queues
# (many URLs plus an X-Emby-Token header), so all it needs to contain is an mpv
# that can talk to PulseAudio.
#
# Keep the image present (a docker image prune removes it): without it every
# server-side play silently fails and falls back to the kiosk browser player,
# so radio only makes sound while the dashboard happens to be on screen.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends mpv ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["mpv"]
