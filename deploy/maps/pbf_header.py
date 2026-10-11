"""Print the OSM .pbf header as one JSON line (stdlib only).

Used by build_release.sh, run inside the pinned Valhalla image (it ships
python3), so the machine needs no osmium. The data date the screen and prints
show is the header's osmosis_replication_timestamp, which Geofabrik sets to
the moment their extract was cut.

Format: the file starts with [4-byte big-endian length][BlobHeader][Blob];
the first blob is the OSMHeader, a zlib-compressed HeaderBlock. Fields used
(osmformat.proto): 16 writingprogram, 17 source, 32 replication timestamp,
33 replication sequence number, 34 replication base url.
"""
import datetime
import json
import struct
import sys
import zlib


def fields(buf):
    """Yield (field_number, value) for a protobuf message."""
    i = 0
    while i < len(buf):
        key, i = varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            val, i = varint(buf, i)
        elif wire == 2:
            n, i = varint(buf, i)
            val, i = buf[i:i + n], i + n
        elif wire == 1:
            val, i = buf[i:i + 8], i + 8
        elif wire == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise ValueError("unsupported wire type %d" % wire)
        yield num, val


def varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def main(path):
    with open(path, "rb") as f:
        (hlen,) = struct.unpack(">i", f.read(4))
        bh = dict(fields(f.read(hlen)))
        if bh.get(1) != b"OSMHeader":
            raise SystemExit("first blob is not OSMHeader")
        blob = dict(fields(f.read(bh[3])))
    data = zlib.decompress(blob[3]) if 3 in blob else blob[1]
    out = {}
    for num, val in fields(data):
        if num == 16:
            out["writingprogram"] = val.decode("utf-8", "replace")
        elif num == 17:
            out["source"] = val.decode("utf-8", "replace")
        elif num == 32:
            out["replication_timestamp"] = datetime.datetime.fromtimestamp(
                val, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif num == 33:
            out["replication_sequence"] = val
        elif num == 34:
            out["replication_url"] = val.decode("utf-8", "replace")
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1])
