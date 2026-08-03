"""RTP audio streams carved out of a decoded payload, and decoded to PCM.

This sits one layer below `bgan.sip`: SIP/SDP negotiates a call, and the audio
itself travels in RTP (RFC 3550) over UDP. So this reuses `pcapout.carve_ipv4`
-- which already gates on the IPv4 header checksum -- takes the UDP payloads,
and looks for RTP among them.

The safety argument is the SSRC. An RTP header has version 2 in its top two
bits, which a random byte satisfies one time in four, so a single "RTP packet"
proves nothing. A *stream* is several packets that share one 32-bit
synchronisation source and carry the same payload type in sequence-number
order -- and several unrelated noise bytes agreeing on a full SSRC is the same
vanishingly unlikely coincidence the checksum rules out for IPv4. `MIN_PACKETS`
is the gate; below it, nothing is reported. Measured: 0 streams on 8 MB of
random bytes, and 0 on the 1534.499 capture, whose 20 version-2 hits are all
singletons.

Decoding covers G.711 -- PCMU (payload type 0) and PCMA (type 8) -- which is
what the BGAN voice service uses and what the SDP in these captures offers.
The G.711 tables are the ITU-T / Sun reference algorithm, checked by round
trip and against known codewords in the tests. Other payload types are
recognised and reported but not decoded; comfort noise (13) and
telephone-event (RFC 4733) are not audio and are skipped.

Carving, not demuxing: the payload has gaps wherever an FEC block failed, so a
stream is whatever packets survived, and lost packets leave gaps in the audio
rather than being concealed. What comes out is honest about what got through.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass, field

import numpy as np

from . import pcapout

# Payload types we can turn into samples, RFC 3551 Table 4/5. Everything else
# is reported by number but not decoded.
PT_NAMES = {
    0: "PCMU", 3: "GSM", 4: "G723", 8: "PCMA", 9: "G722", 15: "G728",
    18: "G729", 13: "CN", 101: "telephone-event",
}
DECODABLE = {0: "PCMU", 8: "PCMA"}

MIN_PACKETS = 4                 # a stream is at least this many, sharing SSRC
CLOCK_HZ = 8000                 # G.711 sample rate


# --- G.711, ITU-T / Sun reference -------------------------------------------

def _ulaw_table():
    u = (~np.arange(256, dtype=np.int32)) & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t = t << ((u & 0x70) >> 4)
    val = np.where(u & 0x80, 0x84 - t, t - 0x84)
    return val.astype(np.int16)


def _alaw_table():
    a = np.arange(256, dtype=np.int32) ^ 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    t = np.where(seg == 0, t + 8, t + 0x108)
    shift = np.where(seg > 1, seg - 1, 0)
    t = t << shift
    val = np.where(a & 0x80, t, -t)
    return val.astype(np.int16)


ULAW = _ulaw_table()
ALAW = _alaw_table()


def decode_g711(data, pt):
    """G.711 bytes -> int16 PCM at 8 kHz. Empty for a non-G.711 type."""
    b = np.frombuffer(data, dtype=np.uint8)
    if pt == 0:
        return ULAW[b]
    if pt == 8:
        return ALAW[b]
    return np.zeros(0, dtype=np.int16)


# --- RTP ---------------------------------------------------------------------

@dataclass
class Packet:
    offset: int
    src: str
    dst: str
    sport: int
    dport: int
    ssrc: int
    seq: int
    timestamp: int
    pt: int
    marker: bool
    payload: bytes


def _ip(pkt, o):
    return ".".join(str(b) for b in pkt[o:o + 4])


def packets(payload):
    """Every plausible RTP packet, one per checksum-valid UDP datagram.

    Per-packet validation is deliberately loose (version 2, a sane header
    length, a non-empty payload); the stream grouping below is what makes a
    detection trustworthy. A lone packet here is not yet evidence of anything.
    """
    out = []
    for off, pkt in pcapout.carve_ipv4(payload, protocols=(17,)):
        ihl = (pkt[0] & 0x0F) * 4
        u = pkt[ihl:]
        if len(u) < 8:
            continue
        ulen = (u[4] << 8) | u[5]
        body = u[8:ulen] if 8 <= ulen <= len(u) else u[8:]
        if len(body) < 12 or (body[0] >> 6) != 2:
            continue
        cc = body[0] & 0x0F
        ext = (body[0] >> 4) & 1
        hlen = 12 + 4 * cc
        if len(body) < hlen + 1:
            continue
        if ext:                 # skip a header extension if present
            if len(body) < hlen + 4:
                continue
            xlen = (body[hlen + 2] << 8) | body[hlen + 3]
            hlen += 4 + 4 * xlen
            if len(body) < hlen + 1:
                continue
        out.append(Packet(
            offset=off, src=_ip(pkt, 12), dst=_ip(pkt, 16),
            sport=(u[0] << 8) | u[1], dport=(u[2] << 8) | u[3],
            ssrc=int.from_bytes(body[8:12], "big"),
            seq=(body[2] << 8) | body[3],
            timestamp=int.from_bytes(body[4:8], "big"),
            pt=body[1] & 0x7F, marker=bool(body[1] & 0x80),
            payload=bytes(body[hlen:])))
    return out


@dataclass
class Stream:
    ssrc: int
    src: str = ""
    dst: str = ""
    sport: int = 0
    dport: int = 0
    pt: int = 0
    packets: list = field(default_factory=list)

    @property
    def codec(self):
        return PT_NAMES.get(self.pt, f"PT{self.pt}")

    @property
    def decodable(self):
        return self.pt in DECODABLE

    @property
    def offset(self):
        return min(p.offset for p in self.packets)

    @property
    def lost(self):
        """Packets missing from the sequence-number span, i.e. gaps."""
        seqs = sorted(p.seq for p in self.packets)
        span = ((seqs[-1] - seqs[0]) & 0xFFFF) + 1
        return max(0, span - len(set(seqs)))

    @property
    def seconds(self):
        return len(self.pcm()) / CLOCK_HZ if self.decodable else 0.0

    def pcm(self):
        """Decoded int16 samples, packets in sequence order.

        Gaps are left as gaps -- a lost packet is not concealed with silence,
        because in a carved stream a "gap" is as likely a failed FEC block as
        a real network loss, and inventing samples would misrepresent both.
        """
        if not self.decodable:
            return np.zeros(0, dtype=np.int16)
        ordered = sorted(self.packets, key=lambda p: p.seq)
        return np.concatenate(
            [decode_g711(p.payload, self.pt) for p in ordered]
            or [np.zeros(0, dtype=np.int16)])

    def summary(self):
        d = f"{self.src}:{self.sport} -> {self.dst}:{self.dport}"
        tail = (f", {self.seconds:.1f}s audio" if self.decodable
                else ", not decoded")
        gap = f", {self.lost} lost" if self.lost else ""
        return (f"{self.codec}  {d}  ssrc {self.ssrc:#010x}  "
                f"{len(self.packets)} pkts{gap}{tail}")


def streams(payload, min_packets=MIN_PACKETS):
    """RTP streams in `payload`, each ≥ `min_packets` packets sharing an SSRC.

    Grouped by (src, dst, ssrc). The payload type is taken as the commonest in
    the group, so an odd stray type does not split a stream, and packets not
    of that type are dropped from it.
    """
    from collections import Counter
    groups = {}
    for p in packets(payload):
        groups.setdefault((p.src, p.dst, p.ssrc), []).append(p)

    out = []
    for (src, dst, ssrc), pkts in groups.items():
        if len(pkts) < min_packets:
            continue
        pt = Counter(p.pt for p in pkts).most_common(1)[0][0]
        keep = [p for p in pkts if p.pt == pt]
        if len(keep) < min_packets:
            continue
        first = min(keep, key=lambda p: p.offset)
        out.append(Stream(ssrc=ssrc, src=src, dst=dst,
                          sport=first.sport, dport=first.dport,
                          pt=pt, packets=keep))
    out.sort(key=lambda s: s.offset)
    return out


def write_wav(stream, path):
    """Write a stream to an 8 kHz mono 16-bit WAV. Returns the sample count."""
    pcm = stream.pcm()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(CLOCK_HZ)
        w.writeframes(pcm.tobytes())
    return len(pcm)


def scan(payload):
    return streams(payload)


def scan_random(nbytes=8 << 20, seed=0):
    """False-accept control. Returns the stream count over random bytes."""
    rng = np.random.default_rng(seed)
    return len(streams(rng.integers(0, 256, nbytes, dtype=np.uint8).tobytes()))
