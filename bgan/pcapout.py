"""Write decoded output as pcap.

Two modes, because they make very different claims.

`write_blocks` is lossless and honest: every decoded FEC block becomes one
record on a private link type, timestamped from its frame index (80 ms per
frame). Nothing is interpreted. Use this to look at what was actually
recovered.

`write_ipv4` carves plausible IPv4 packets out of the payload. This is a
guess, but not a wild one -- a candidate is only accepted if its **header
checksum is valid**, which is a 16-bit constraint. Combined with the version,
IHL, length and protocol checks, the chance of random bytes producing an
accepted packet is on the order of 2^-16 per offset, so on a 700 kB payload
you would expect a handful of false accepts, not thousands.

It is still a carve, not a demux. Without TS 102 744-3-2..3-8 we do not have
the logical-channel and RLC/MAC layers needed to reassemble streams properly,
so packets split across FEC blocks, or landing in the ~60% of blocks that do
not decode, are simply lost. Treat the output as evidence, not as a faithful
record of the link.
"""
from __future__ import annotations
import struct

import numpy as np

PCAP_MAGIC = 0xA1B2C3D4
DLT_RAW = 101          # raw IP, no link layer
DLT_USER0 = 147        # private use; decoded FEC blocks
FRAME_SECS = 0.080


def _hdr(linktype, snaplen=65535):
    return struct.pack("<IHHiIII", PCAP_MAGIC, 2, 4, 0, 0, snaplen, linktype)


def _rec(ts, data):
    s = int(ts)
    us = int(round((ts - s)*1e6))
    if us >= 1000000:
        s, us = s + 1, us - 1000000
    return struct.pack("<IIII", s, us, len(data), len(data)) + data


def write_blocks(path, records, t0=0.0):
    """records: iterable of (frame_index, block_index, payload_bits).

    One pcap record per decoded FEC block, on DLT_USER0. The timestamp is the
    frame index times 80 ms, so block ordering and gaps stay visible.
    """
    n = 0
    with open(path, "wb") as f:
        f.write(_hdr(DLT_USER0))
        for frame, block, bits in records:
            b = np.asarray(bits, dtype=np.uint8)
            data = np.packbits(b).tobytes()
            f.write(_rec(t0 + frame*FRAME_SECS + block*FRAME_SECS/8, data))
            n += 1
    return n


def ipv4_checksum_ok(hdr):
    """Standard one's-complement check over the IPv4 header."""
    if len(hdr) < 20 or len(hdr) % 2:
        return False
    s = 0
    for i in range(0, len(hdr), 2):
        s += (hdr[i] << 8) | hdr[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return s == 0xFFFF


def carve_ipv4(payload, min_len=20, max_len=1500,
               protocols=(1, 6, 17, 47, 50, 58)):
    """Yield (offset, packet_bytes) for header-checksum-valid IPv4 packets.

    The checksum is what makes this defensible; without it, scanning for 0x45
    alone finds a "packet" every few hundred bytes in pure noise.
    """
    b = memoryview(payload)
    n = len(b)
    i = 0
    while i < n - min_len:
        v = payload[i]
        if (v >> 4) != 4:
            i += 1
            continue
        ihl = (v & 0x0F)*4
        if ihl < 20 or i + ihl > n:
            i += 1
            continue
        total = (payload[i + 2] << 8) | payload[i + 3]
        if not (ihl <= total <= max_len) or i + total > n:
            i += 1
            continue
        if payload[i + 9] not in protocols:
            i += 1
            continue
        if not ipv4_checksum_ok(payload[i:i + ihl]):
            i += 1
            continue
        yield i, bytes(b[i:i + total])
        i += total
    return


def write_ipv4(path, payload, t0=0.0, dt=0.001):
    """Carve IPv4 packets from `payload` and write them as a raw-IP pcap.

    Timestamps are synthetic (`t0 + k*dt`): a carved packet has no recoverable
    arrival time, and inventing one that looked real would be worse than
    obviously placeholder spacing.
    """
    out = list(carve_ipv4(payload))
    with open(path, "wb") as f:
        f.write(_hdr(DLT_RAW))
        for k, (_, pkt) in enumerate(out):
            f.write(_rec(t0 + k*dt, pkt))
    return len(out)


class LivePcap:
    """Append-only pcap for a live decode, flushed after every batch.

    Follow it live with `tail -c +1 -f file.pcap | wireshark -k -i -`, or open
    it and use File > Reload. The global header is written once, every `add_*` call flushes, so a crash
    or Ctrl-C leaves a valid file.

    `mode="blocks"`: one DLT_USER0 record per decoded FEC block, stamped
    `t0 + frame*80 ms` exactly as `write_blocks`.

    `mode="ipv4"`: a checksum-gated carve as in `carve_ipv4`, run incrementally.
    The last `max_len` bytes of payload are kept between batches so a packet
    that straddles a batch boundary is still found; offsets already emitted
    are never emitted twice. Timestamps are the wall-clock time the packet was
    carved, which is when we *knew* about it -- not when it crossed the air.
    Same caveat as the offline carve: no RLC/MAC reassembly, so packets split
    across FEC blocks or lost to undecoded blocks simply do not appear.
    """

    def __init__(self, path, mode="ipv4", t0=None, max_len=1500):
        import time
        if mode not in ("ipv4", "blocks"):
            raise ValueError(mode)
        self.mode = mode
        self.max_len = max_len
        self.t0 = time.time() if t0 is None else t0
        self.records = 0
        self._tail = b""        # trailing payload bytes kept for straddlers
        self._emitted_to = 0    # bytes of `_tail` already fully carved past
        self._f = open(path, "wb")
        self._f.write(_hdr(DLT_RAW if mode == "ipv4" else DLT_USER0))
        self._f.flush()

    def add_blocks(self, records):
        """records: iterable of (frame_index, block_index, payload_bits)."""
        n = 0
        for frame, block, bits in records:
            data = np.packbits(np.asarray(bits, dtype=np.uint8)).tobytes()
            self._f.write(_rec(self.t0 + frame*FRAME_SECS
                               + block*FRAME_SECS/8, data))
            n += 1
        self._f.flush()
        self.records += n
        return n

    def add_payload(self, payload, now=None):
        """Feed newly decoded payload bytes; returns packets written."""
        import time
        now = time.time() if now is None else now
        buf = self._tail + bytes(payload)
        skip = self._emitted_to          # packets ending before this are old
        n = 0
        last_end = skip
        for off, pkt in carve_ipv4(buf, max_len=self.max_len):
            end = off + len(pkt)
            if end <= skip:
                continue
            self._f.write(_rec(now + n*1e-6, pkt))
            n += 1
            last_end = end
        self._f.flush()
        self.records += n
        # keep only what a straddling packet could still need
        keep = min(len(buf), self.max_len)
        cut = len(buf) - keep
        self._tail = buf[cut:]
        self._emitted_to = max(0, max(last_end, skip) - cut)
        return n

    def close(self):
        if not self._f.closed:
            self._f.close()
