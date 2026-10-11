#!/usr/bin/env python3
"""hlsfetch: read an HLS stream and write its raw MPEG-TS to stdout.

  hlsfetch.py <file holding the playlist URL>

GStreamer's hlsdemux fetches the playlist and segments and decrypts AES-128
segments. The URL is read from a file (in the container's private tmpfs), not
taken as an argument: a Pluto URL carries a session token that names this
house's IP and rough location, and process arguments are visible to every
user on the host.
"""
import sys

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402


def main():
    with open(sys.argv[1], encoding="utf-8") as fh:
        url = fh.read().strip()
    Gst.init(None)
    pipe = Gst.parse_launch(
        "souphttpsrc name=src timeout=20 ! hlsdemux ! fdsink fd=1 sync=false")
    pipe.get_by_name("src").set_property("location", url)
    del url
    loop = GLib.MainLoop()
    code = {"rc": 0}

    def on_bus(_bus, msg):
        if msg.type == Gst.MessageType.EOS:
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            # The message only, never the debug string (it can hold the URL).
            print(f"hlsfetch: {err.message}", file=sys.stderr, flush=True)
            code["rc"] = 1
            loop.quit()
        return True

    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_bus)
    pipe.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipe.set_state(Gst.State.NULL)
    return code["rc"]


if __name__ == "__main__":
    sys.exit(main())
