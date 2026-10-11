#!/usr/bin/python3
"""Post one librespot player event to PuffPAI as VALID JSON.

Replaces the image's stock /app/bin/post-event-data.sh, which builds its
body by interpolating raw env vars between quotes:

    "ARTISTS": "'"$ARTISTS"'",

librespot separates multi-valued fields (ARTISTS, ALBUM_ARTISTS, COVERS)
with NEWLINES, and track/album names routinely contain double quotes and
backslashes. Either one produces a syntactically invalid body, so the whole
event is lost -- the backend can only shrug and carry the previous track
forward, leaving the Now Playing widget showing the wrong song for the rest
of the session. The stock script strips newlines from COVERS only.

Here the payload is assembled by json.dumps, so quoting is correct by
construction, and the multi-valued fields are sent as JSON ARRAYS -- which
is what they always were. media_hub.normalize_spotify_event already accepts
a list for artists.

Contract with the container:
  * invoked via ONEVENT_COMMAND (the entrypoint takes precedence over
    ONEVENT_POST_ENDPOINT and passes no --url), so the destination is read
    from ONEVENT_POST_ENDPOINT in the environment;
  * ALWAYS exits 0. This runs inside librespot's event hook -- a failed
    report is not worth disturbing playback, and a non-zero exit would be
    logged on every event.

puff puff pass
"""
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 5

# Single-valued fields, sent as strings (the stock script's list, minus its
# duplicate NAME key).
SCALAR_KEYS = (
    "PLAYER_EVENT", "TRACK_ID", "URI", "NAME", "LANGUAGE", "DURATION_MS",
    "IS_EXPLICIT", "ITEM_TYPE", "ALBUM", "NUMBER", "DISC_NUMBER",
    "DESCRIPTION", "PUBLISH_TIME", "SHOW_NAME", "POSITION_MS",
    "CONNECTION_ID", "USER_NAME", "CLIENT_ID", "CLIENT_NAME",
    "CLIENT_BRAND_NAME", "CLIENT_MODEL_NAME", "VOLUME", "SHUFFLE",
    "REPEAT", "AUTO_PLAY", "FILTER", "SINK_STATUS",
)

# Newline-separated by librespot -> real JSON arrays.
MULTI_KEYS = ("ARTISTS", "ALBUM_ARTISTS", "COVERS")


def collect():
    payload = {}
    for key in SCALAR_KEYS:
        val = os.environ.get(key)
        if val:
            payload[key] = val
    for key in MULTI_KEYS:
        raw = os.environ.get(key) or ""
        items = [part.strip() for part in raw.split("\n") if part.strip()]
        if items:
            payload[key] = items
    return payload


def main():
    endpoint = (os.environ.get("ONEVENT_POST_ENDPOINT") or "").strip()
    if not endpoint:
        return

    body = json.dumps(collect()).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS).close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - a report must never break playback
        print("post-event-json: %s" % exc, file=sys.stderr)
    sys.exit(0)
