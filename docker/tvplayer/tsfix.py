#!/usr/bin/env python3
"""tsfix: keep one video and one audio stream id across ad switches.

Reads MPEG-TS on stdin, writes MPEG-TS on stdout.

Ad-stitched free channels (Pluto and the like) switch to a new clip at every
ad break, and a new clip can carry its video (or audio) on a different stream
id (PID), announced in a new program table (PMT). ffmpeg only follows the
streams it saw at the start, so after such a switch it kept the sound but
drops every video frame: the picture freezes (ffmpeg logs "New video
stream 0:2" and the frame count sticks). ffmpeg's own -merge_pmt_versions
did not catch it.

So: remember the first program's video and audio ids, and whenever a later
program table names different ones, rewrite those packets onto the first ids.
The first program tables are what goes out every time a table comes by, so
downstream only ever sees one video and one audio stream. Timestamps are left
alone; the ffmpeg after this evens out their jumps.
"""
import sys

PKT = 188
VIDEO_TYPES = {0x01, 0x02, 0x1B, 0x24, 0x10}       # mpeg1/2, h264, hevc, mpeg4
AUDIO_TYPES = {0x03, 0x04, 0x0F, 0x11, 0x81, 0x87}  # mp2/mp3, aac, ac3, eac3


def pid_of(p):
    return ((p[1] & 0x1F) << 8) | p[2]


def payload(p):
    afc = (p[3] >> 4) & 0x3
    if afc in (0, 2):
        return None
    start = 4
    if afc == 3:
        start += 1 + p[4]
    return p[start:] if start < PKT else None


def section(p):
    """The table section in a packet that starts one, else None."""
    if not p[1] & 0x40:
        return None
    pl = payload(p)
    if not pl:
        return None
    ptr = pl[0]
    return pl[1 + ptr:]


def parse_pat(sec):
    if not sec or sec[0] != 0x00:
        return None
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    body = sec[8:3 + length - 4]
    for i in range(0, len(body) - 3, 4):
        program = (body[i] << 8) | body[i + 1]
        if program != 0:
            return ((body[i + 2] & 0x1F) << 8) | body[i + 3]
    return None


def parse_pmt(sec):
    """(video_pid, audio_pid) of a PMT section; either may be None."""
    if not sec or sec[0] != 0x02:
        return None
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    end = 3 + length - 4
    info_len = ((sec[10] & 0x0F) << 8) | sec[11]
    i = 12 + info_len
    video = audio = None
    while i + 5 <= end:
        stype = sec[i]
        epid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
        es_len = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
        if stype in VIDEO_TYPES and video is None:
            video = epid
        elif stype in AUDIO_TYPES and audio is None:
            audio = epid
        i += 5 + es_len
    return video, audio


def main():
    src = sys.stdin.buffer
    out = sys.stdout.buffer
    pmt_pid = None
    first_pat = first_pmt = None
    first_v = first_a = None
    remap = {}
    buf = b""
    while True:
        chunk = src.read(PKT * 512)
        if not chunk:
            break
        buf += chunk
        # Resync on the 0x47 sync byte if a read ever lands mid-packet.
        while buf and buf[0] != 0x47:
            buf = buf[1:]
        n = len(buf) // PKT * PKT
        data, buf = buf[:n], buf[n:]
        res = bytearray()
        for off in range(0, n, PKT):
            p = data[off:off + PKT]
            if p[0] != 0x47:
                continue
            pid = pid_of(p)
            if pid == 0:
                found = parse_pat(section(p))
                if found is not None:
                    pmt_pid = found
                if first_pat is None:
                    first_pat = bytes(p)
                res += first_pat
                continue
            if pmt_pid is not None and pid == pmt_pid:
                streams = parse_pmt(section(p))
                if streams:
                    v, a = streams
                    if first_pmt is None:
                        first_pmt, first_v, first_a = bytes(p), v, a
                    remap = {}
                    if v is not None and first_v is not None and v != first_v:
                        remap[v] = first_v
                    if a is not None and first_a is not None and a != first_a:
                        remap[a] = first_a
                if first_pmt is not None:
                    res += first_pmt
                continue
            target = remap.get(pid)
            if target is not None:
                q = bytearray(p)
                q[1] = (q[1] & 0xE0) | (target >> 8)
                q[2] = target & 0xFF
                res += q
            else:
                res += p
        out.write(res)
        out.flush()


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
