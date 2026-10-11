#!/bin/bash
# tvplay: play one TV channel full screen on the kiosk display, hardware decoded.
#
#   tvplay url <https-url>        a public HLS stream (free channels with ads)
#   tvplay pluto <channel-id>     a Pluto TV channel (anonymous session, no account)
#   tvplay jellyfin <channel-id>  a Jellyfin Live TV channel on this box
#
# The Jellyfin key is never an argument. It is read at run time from the box's
# own 0600 config, mounted read-only at /run/puffpai/media.json, and written
# only to a header file in this container's private /tmp (a tmpfs). curl reads
# that file; the key never shows in a process list, a log or docker inspect.
#
# Video, in tvwindow.py: tsdemux -> nvv4l2decoder (a Jetson's video engine) ->
# nv3dsink (the GPU draws and scales). Jellyfin channels arrive as Jellyfin's
# TS remux through ffmpeg (copy only); free channels with ads go through
# hlsfetch.py, tsfix.py and ffmpeg (see play_hls). Audio: software AAC (a few percent of one
# core) -> PulseAudio, the same speakers as radio and voice.
#
# Exit = window gone. If the stream errors or stalls for 20 s, or either half
# of the pipeline dies, the watchdog at the bottom ends the rest and the
# container exits, so the dashboard underneath is back on screen. A stop
# (docker stop -> SIGTERM) does the same and, for a Jellyfin channel, closes
# the live stream so Jellyfin stops its own remux.
set -u
export HOME=/tmp  # GLib wants a writable home (dconf); the container has none
MODE="${1:-}"; ARG="${2:-}"
JF="${TV_JELLYFIN:-http://127.0.0.1:8096}"
HDR=/tmp/jf.hdr
LIVE_ID=""
PLAY_SESSION=""
DEVICE_ID=""
PIPE_PID=""
USES_FFMPEG=0

log() { echo "tvplay: $*" >&2; }

close_jellyfin() {
  [ -f "$HDR" ] || return 0
  if [ -n "$PLAY_SESSION" ]; then
    curl -s -m 5 -o /dev/null -X DELETE -H @"$HDR" \
      "$JF/Videos/ActiveEncodings?DeviceId=$DEVICE_ID&PlaySessionId=$PLAY_SESSION" || true
    PLAY_SESSION=""
  fi
  if [ -n "$LIVE_ID" ]; then
    curl -s -m 5 -o /dev/null -X POST -H @"$HDR" \
      "$JF/LiveStreams/Close?liveStreamId=$LIVE_ID" || true
    LIVE_ID=""
  fi
}

finish() {
  trap - TERM INT
  [ -n "$PIPE_PID" ] && kill -TERM -- -"$PIPE_PID" 2>/dev/null
  pkill -TERM -f tvwindow.py 2>/dev/null
  pkill -TERM -f hlsfetch.py 2>/dev/null
  pkill -TERM -x ffmpeg 2>/dev/null
  pkill -TERM -x curl 2>/dev/null
  close_jellyfin
  rm -f "$HDR" /tmp/src.url
  exit "${1:-0}"
}
trap finish TERM INT

# Decode/draw half: tvwindow.py (full-screen window, nvv4l2decoder, nv3dsink,
# PulseAudio). Reads MPEG-TS on stdin. It exits on a tap, an error, a stall or
# a stop, and the window goes with it.
play_ts() {
  exec python3 /usr/local/bin/tvwindow.py
}

# Remux half (Jellyfin channels): copy one video + one audio stream (ffmpeg's default pick = the
# highest-resolution video and the best audio), no re-encode. Without -copyts,
# ffmpeg evens out timestamp jumps in what Jellyfin sends.
REMUX=(-nostdin -hide_banner -loglevel error -fflags +genpts
       -c copy -streamid 0:256 -streamid 1:257 -f mpegts pipe:1)

# Fetch a URL without putting it on a command line: curl reads it from a
# config on a pipe. (Pluto URLs carry a session token naming the house's IP
# and rough location; process arguments are visible to everyone on the host.)
fetch() {
  local url=$1; shift
  case "$url" in *'"'*|*' '*|*$'\n'*) return 1;; esac
  curl -sL "$@" --config <(printf 'url = "%s"\n' "$url")
}

# Pick one quality level of an HLS master playlist: the highest bandwidth up
# to 12 Mbps. Given the master, the reader would first open and probe EVERY
# level; given one level it starts sooner, and audio and video come from the
# same level. Prints the chosen media playlist URL, or the input unchanged if
# it is not a master playlist.
pick_level() {
  local body eff base best
  body=$(fetch "$1" -m 10 --max-filesize 2000000 -w '\n%{url_effective}') \
    || { echo "$1"; return; }
  eff=${body##*$'\n'}; body=${body%$'\n'*}
  grep -q '#EXT-X-STREAM-INF' <<<"$body" || { echo "$1"; return; }
  best=$(awk '
    /^#EXT-X-STREAM-INF/ { bw = 0; if (match($0, /BANDWIDTH=[0-9]+/)) bw = substr($0, RSTART + 10, RLENGTH - 10) + 0; want = 1; next }
    want && !/^#/ && NF { if (bw <= 12000000 && bw > top) { top = bw; uri = $0 } want = 0 }
    END { print uri }' <<<"$body" | tr -d '\r')
  [ -n "$best" ] || { echo "$1"; return; }
  case "$best" in
    http://*|https://*) echo "$best" ;;
    /*) echo "$(sed -E 's#^(https?://[^/]+).*#\1#' <<<"$eff")$best" ;;
    *) base=${eff%%\?*}; echo "${base%/*}/$best" ;;
  esac
}

# Pluto: start an anonymous session (Pluto's own web-client boot call, no
# account), the way open-source Pluto playlist tools do. Its token lives 24 h
# and names the house's IP and rough location, so it stays inside this
# container: in a shell variable and a 0600 file in the private tmpfs, never
# in an argument, a log or a file on the box.
pluto_master() {
  local id=$1 cid boot stitcher params token
  cid=$(cat /proc/sys/kernel/random/uuid)
  boot=$(curl -s -m 15 "https://boot.pluto.tv/v4/start?appName=web&appVersion=8.0.0&deviceVersion=122.0.0&deviceModel=web&deviceMake=chrome&deviceType=web&clientID=$cid&clientModelNumber=1.0.0&channelID=$id&serverSideAds=false") \
    || return 1
  stitcher=$(jq -r '.servers.stitcher // empty' <<<"$boot")
  params=$(jq -r '.stitcherParams // empty' <<<"$boot")
  token=$(jq -r '.sessionToken // empty' <<<"$boot")
  unset boot
  [[ "$stitcher" =~ ^https://[a-z0-9.-]+\.pluto\.tv$ ]] || return 1
  [ -n "$params" ] && [ -n "$token" ] || return 1
  echo "$stitcher/v2/stitch/hls/channel/$id/master.m3u8?$params&jwt=$token&masterJWTPassthrough=true"
}

# Ad-stitched channels (Pluto and the like), at every switch between show and
# ad, restart their timestamps and can move the video to a new stream id;
# their segments are also AES-128 encrypted. So, in three steps:
#   1. hlsfetch.py (GStreamer hlsdemux) fetches and decrypts, handing on raw TS;
#   2. tsfix.py keeps the first video/audio stream ids for good (ffmpeg drops
#      video that moves to a new id: the picture froze at the ad);
#   3. ffmpeg's TS reader evens out the timestamp jumps (checked on a recorded
#      Pluto ad break: 0 jumps left over 240 s).
# Reading the playlist with ffmpeg directly froze the picture at the first ad,
# the same way Jellyfin does.
play_hls() {
  local src=$1
  umask 077; printf '%s\n' "$src" > /tmp/src.url
  set -m
  USES_FFMPEG=1
  ( python3 /usr/local/bin/hlsfetch.py /tmp/src.url \
      | python3 /usr/local/bin/tsfix.py \
      | ffmpeg -f mpegts -i pipe:0 "${REMUX[@]}" \
      | play_ts ) &
  PIPE_PID=$!
  # Optional (TV_LOG_BREAKS=1): log each timestamp reset (#EXT-X-DISCONTINUITY,
  # a switch between show and ad) as it enters the playlist, to prove playback
  # carries on through ad breaks. Off by default.
  if [ "${TV_LOG_BREAKS:-0}" = 1 ]; then
    ( last=""
      while sleep 6; do
        pl=$(fetch "$src" -m 8) || continue
        seq=$(sed -nE 's/^#EXT-X-DISCONTINUITY-SEQUENCE:([0-9]+).*/\1/p' <<<"$pl")
        n=$(grep -c '^#EXT-X-DISCONTINUITY$' <<<"$pl")
        # sequence + markers in view only grows when a NEW marker arrives
        now=$(( ${seq:-0} + n ))
        [ -n "$last" ] && [ "$now" -gt "$last" ] && log "ad break marker: the stream reset its timestamps (marker $now)"
        last="$now"
      done ) &
  fi
}

case "$MODE" in
  url)
    case "$ARG" in https://*) ;; *) log "not an https stream address"; exit 2;; esac
    play_hls "$(pick_level "$ARG")"
    ;;
  pluto)
    [[ "$ARG" =~ ^[0-9a-f]{24}$ ]] || { log "not a Pluto channel id"; exit 2; }
    MASTER=$(pluto_master "$ARG") || { log "Pluto did not start a session"; exit 4; }
    play_hls "$(pick_level "$MASTER")"
    unset MASTER
    ;;
  jellyfin)
    [[ "$ARG" =~ ^[0-9a-f]{32}$ ]] || { log "not a channel id"; exit 2; }
    KEY=$(jq -r '.jellyfin_api_key // empty' /run/puffpai/media.json 2>/dev/null)
    [ -n "$KEY" ] || { log "no media server key"; exit 3; }
    umask 077; printf 'X-Emby-Token: %s\n' "$KEY" > "$HDR"; unset KEY
    USER_ID=$(curl -s -m 10 -H @"$HDR" "$JF/Users" \
      | jq -r '[.[] | select(.Policy.IsAdministrator)][0].Id // .[0].Id // empty')
    [ -n "$USER_ID" ] || { log "media server did not answer"; finish 4; }
    # Ask Jellyfin for its own TS remux of the channel (video and audio copied,
    # no re-encode). Every Live TV source here is HLS, and Jellyfin's "static"
    # stream hands back the upstream playlist rather than a stream.
    PROFILE='{"DeviceProfile":{"MaxStreamingBitrate":120000000,"DirectPlayProfiles":[],"TranscodingProfiles":[{"Container":"ts","Type":"Video","VideoCodec":"h264,hevc,mpeg2video","AudioCodec":"aac,mp3,ac3,eac3,mp2","Protocol":"http","Context":"Streaming"}]}}'
    INFO=$(curl -s -m 30 -X POST -H @"$HDR" -H 'Content-Type: application/json' \
      -d "$PROFILE" \
      "$JF/Items/$ARG/PlaybackInfo?UserId=$USER_ID&AutoOpenLiveStream=true&IsPlayback=true")
    LIVE_ID=$(jq -r '.MediaSources[0].LiveStreamId // empty' <<<"$INFO")
    TC_URL=$(jq -r '.MediaSources[0].TranscodingUrl // empty' <<<"$INFO")
    unset INFO
    [ -n "$TC_URL" ] || { log "channel did not open"; finish 4; }
    # Jellyfin's URL carries the key as ApiKey=...; drop it, the header file
    # authenticates instead.
    TC_URL=$(sed -E 's/([?&])ApiKey=[^&]*&?/\1/; s/[?&]$//' <<<"$TC_URL")
    PLAY_SESSION=$(sed -nE 's/.*[?&]PlaySessionId=([0-9a-fA-F]+).*/\1/p' <<<"$TC_URL")
    DEVICE_ID=$(sed -nE 's/.*[?&]DeviceId=([^&]+).*/\1/p' <<<"$TC_URL")
    set -m
    USES_FFMPEG=1
    ( curl -s -N --fail -H @"$HDR" --speed-limit 1 --speed-time 20 "$JF$TC_URL" \
        | ffmpeg -i pipe:0 "${REMUX[@]}" | play_ts ) &
    PIPE_PID=$!
    ;;
  *)
    log "usage: tvplay url <https-url> | pluto <id> | jellyfin <channel-id>"; exit 2;;
esac

# Watchdog: neither half may outlive the other. If ffmpeg dies first, tvwindow
# would wait out its stall timer with a frozen picture; end it now instead.
sleep 3
while sleep 2; do
  [ "$USES_FFMPEG" = 1 ] && { pgrep -x ffmpeg >/dev/null || break; }
  pgrep -f tvwindow.py >/dev/null || break
done
log "stream ended"
finish 0
