#!/usr/bin/env python3
"""tvwindow: show an MPEG-TS stream from stdin full screen, hardware decoded.

Owns its own X window (named "PuffPAI TV", asked to be full screen and to
bypass the compositor) and hands it to nv3dsink, so the picture covers the
kiosk cleanly with no title bar, dock or top bar, and the window can be found
by name. Decode is nvv4l2decoder (a Jetson's video engine); audio goes to
PulseAudio.

Ways this process ends, every one of which takes the window with it so the
dashboard underneath is what shows next:
  * a tap or click anywhere on the picture (the on-screen way back),
  * end of stream or a pipeline error,
  * no new video frame for STALL_SECONDS (FIRST_FRAME_SECONDS before the first),
  * SIGTERM / SIGINT (docker stop, i.e. the backend's Stop).
Exit code: 0 for a tap or a stop, 1 for anything that went wrong.
"""
import os
import signal
import sys
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402
from Xlib import X, Xatom, display  # noqa: E402

STALL_SECONDS = int(os.environ.get("TV_STALL_SECONDS", "20"))
FIRST_FRAME_SECONDS = 45

# Three separate chains; the demuxer's pads are linked by TYPE in on_pad_added.
# (gst-launch style "d. ! queue" links whichever stream the demuxer finds
# first, so audio could land on the video decoder and the stream would fail.)
_Q = "queue max-size-buffers=0 max-size-bytes=0 max-size-time=3000000000"
PIPELINE = (
    "tsdemux name=d "
    # identity does the clock sync: nv3dsink syncing by itself manages only
    # 3 to 4 fps on this box (measured), unsynced it renders at full rate.
    # nvvidconv (a VIC hardware copy) frees the decoder's own buffers at once.
    # Without it, a mid-stream format change (an ad at another size) hung the
    # decoder: it waits for every buffer back and the sink keeps the last one.
    f"{_Q} name=vq ! parsebin ! nvv4l2decoder ! nvvidconv "
    "! video/x-raw(memory:NVMM) ! identity sync=true "
    "! nv3dsink name=vsink sync=false "
    f"{_Q} name=aq ! decodebin name=adec ! audioconvert name=aconv "
    "! audioresample name=ares "
    "! pulsesink name=asink sync=true"
)
AUDIO_BRANCH = ("aq", "adec", "aconv", "ares", "asink")


def log(msg):
    print(f"tvwindow: {msg}", file=sys.stderr, flush=True)


def make_window(dpy):
    screen = dpy.screen()
    root = screen.root
    geo = root.get_geometry()
    win = root.create_window(
        0, 0, geo.width, geo.height, 0, screen.root_depth,
        X.InputOutput, X.CopyFromParent,
        background_pixel=screen.black_pixel,
        event_mask=X.ButtonPressMask | X.StructureNotifyMask,
    )
    win.set_wm_name("PuffPAI TV")
    win.set_wm_class("puffpai-tv", "PuffPAI TV")
    atom = dpy.intern_atom
    win.change_property(atom("_NET_WM_NAME"), atom("UTF8_STRING"), 8,
                        b"PuffPAI TV")
    # Full screen from the first map: above the top bar and dock, no frame.
    win.change_property(atom("_NET_WM_STATE"), Xatom.ATOM, 32,
                        [atom("_NET_WM_STATE_FULLSCREEN")])
    # Let mutter skip compositing this window (less work while it plays).
    win.change_property(atom("_NET_WM_BYPASS_COMPOSITOR"), Xatom.CARDINAL, 32,
                        [1])
    # Not mapped yet: see show_window. Waking the video engine can take 5 to
    # 20 s on this box (the kernel clock framework is slow to power it up
    # while the GPU is busy), and a black full-screen window for that long
    # would hide the dashboard for nothing.
    dpy.flush()
    return win


def main():
    Gst.init(None)
    dpy = display.Display()
    win = make_window(dpy)

    pipeline = Gst.parse_launch(PIPELINE)
    demux = pipeline.get_by_name("d")
    src = Gst.ElementFactory.make("fdsrc", "src")
    src.set_property("fd", 0)
    pipeline.add(src)
    src.link(demux)
    vsink = pipeline.get_by_name("vsink")
    # TV_VOLUME (0.0 to 1.0) scales this stream only; the room volume stays
    # the PulseAudio sink level, same as radio and voice.
    try:
        vol = min(1.0, max(0.0, float(os.environ.get("TV_VOLUME", "1"))))
    except ValueError:
        vol = 1.0
    pipeline.get_by_name("asink").set_property("volume", vol)
    bus = pipeline.get_bus()
    vq_sink = pipeline.get_by_name("vq").get_static_pad("sink")
    aq_sink = pipeline.get_by_name("aq").get_static_pad("sink")

    seen = {"audio": False}

    def relink(sinkpad, pad):
        # The newest stream of each kind wins. The demuxer can announce a
        # second program (new pads) before it removes the first one's.
        peer = sinkpad.get_peer()
        if peer is not None:
            peer.unlink(sinkpad)
        pad.link(sinkpad)

    def on_pad_added(_demux, pad):
        name = pad.get_name()
        if name.startswith("video"):
            relink(vq_sink, pad)
        elif name.startswith("audio"):
            seen["audio"] = True
            relink(aq_sink, pad)

    def on_no_more_pads(_demux):
        # A channel with no audio at all: drop the audio chain, or it would
        # hold the whole pipeline waiting for sound that never comes.
        if seen["audio"]:
            return
        seen["audio"] = True  # drop once

        def drop_audio():
            for el_name in AUDIO_BRANCH:
                el = pipeline.get_by_name(el_name)
                if el is not None:
                    el.set_state(Gst.State.NULL)
                    pipeline.remove(el)
            log("no sound in this stream")
            return False
        GLib.idle_add(drop_audio)

    demux.connect("pad-added", on_pad_added)
    demux.connect("no-more-pads", on_no_more_pads)

    # Hand our window over when the sink asks for it. Calling
    # set_window_handle earlier crashes nv3dsink: its X connection is not
    # open until it starts.
    def on_sync(_bus, msg):
        if GstVideo.is_video_overlay_prepare_window_handle_message(msg):
            msg.src.set_window_handle(win.id)

    bus.enable_sync_message_emission()
    bus.connect("sync-message::element", on_sync)

    loop = GLib.MainLoop()
    state = {"code": 0, "last_frame": None, "started": time.monotonic(),
             "frames": 0}

    def quit_with(code, why):
        if loop.is_running():
            state["code"] = code
            log(why)
            loop.quit()

    def show_window():
        # Map only once there is a picture to show (called on the main loop;
        # Xlib is not safe from the streaming thread).
        win.map()
        win.raise_window()
        dpy.flush()
        return False

    def on_frame(_pad, _info):
        now = time.monotonic()
        if state["last_frame"] is None:
            log(f"first frame after {now - state['started']:.1f}s")
            GLib.idle_add(show_window)
        state["last_frame"] = now
        state["frames"] += 1
        return Gst.PadProbeReturn.OK

    vsink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, on_frame)

    def on_bus(_bus, msg):
        if msg.type == Gst.MessageType.EOS:
            quit_with(1, "stream ended")
        elif msg.type == Gst.MessageType.ERROR:
            err, _dbg = msg.parse_error()
            quit_with(1, f"playback error: {err.message}")
        return True

    bus.add_signal_watch()
    bus.connect("message", on_bus)

    def on_x(_fd, _cond):
        while dpy.pending_events():
            ev = dpy.next_event()
            if ev.type == X.ButtonPress:
                quit_with(0, "tapped, back to the dashboard")
            elif ev.type == X.DestroyNotify:
                quit_with(0, "window closed")
        return True

    GLib.io_add_watch(dpy.fileno(), GLib.IO_IN, on_x)

    def watchdog():
        now = time.monotonic()
        last = state["last_frame"]
        if last is None and now - state["started"] > FIRST_FRAME_SECONDS:
            quit_with(1, "no picture arrived")
        elif last is not None and now - last > STALL_SECONDS:
            quit_with(1, "picture stopped")
        return True

    GLib.timeout_add_seconds(2, watchdog)

    def report():
        # One line a minute, so the log shows playback kept going (for
        # example straight through an ad break).
        log(f"playing: {state['frames']} frames since the last report")
        state["frames"] = 0
        return True

    GLib.timeout_add_seconds(int(os.environ.get("TV_REPORT_SECONDS", "60")), report)
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig,
                             lambda: quit_with(0, "stopped") or False)

    # Run on the system clock, not the audio sink's. At an ad switch the
    # audio branch can stall; with the audio clock the video (synced in
    # identity) then waits for it forever (seen on a recorded Pluto break).
    pipeline.use_clock(Gst.SystemClock.obtain())
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        try:
            win.destroy()
            dpy.flush()
            dpy.close()
        except Exception:
            pass
    return state["code"]


if __name__ == "__main__":
    sys.exit(main())
