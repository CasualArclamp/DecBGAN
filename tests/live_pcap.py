"""LivePcap must find the same IPv4 packets however the payload is cut up."""
# made by CasualArclamp
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bgan import pcapout  # noqa: E402


def pkt(n):
    h = bytearray(20)
    h[0], h[8], h[9] = 0x45, 64, 17
    h[2:4] = struct.pack(">H", 20 + n)
    h[12:20] = bytes([10, 0, 0, 1, 10, 0, 0, 2])
    s = sum((h[i] << 8) | h[i + 1] for i in range(0, 20, 2))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    h[10:12] = struct.pack(">H", ~s & 0xFFFF)
    return bytes(h) + bytes(range(n))


stream = b"\0" * 37 + pkt(100) + b"\xff" * 5 + pkt(200) + pkt(60) + b"\0" * 9
want = [p for _, p in pcapout.carve_ipv4(stream)]
assert len(want) == 3
fn = os.path.join(tempfile.mkdtemp(), "t.pcap")
for step in (1, 7, 64, 150, len(stream)):
    lp = pcapout.LivePcap(fn)
    got = sum(lp.add_payload(stream[i:i + step]) for i in range(0, len(stream), step))
    lp.close()
    assert got == 3, (step, got)
print("ok")
