"""BulletinBoard Bearer Control SDU (TS 102 744-3-1 clause 5.4.3).

The BulletinBoard is broadcast on the forward bearer to describe the current
bearer to all UEs. Clause 5.4.3.0 pins down where to find it: the SDU is the
first BCtSDU in a Broadcast BCtPDU, and that PDU "shall be transmitted as the
first BCtPDU in the first FEC Block of the frame" -- so it is at the head of
FEC block 0, whose coding level the unique word signals directly.

It is also the most valuable structure in the stack for proving a receiver is
correct, because of `frame-no`: a 12-bit counter advancing by one per 80 ms
frame. Our own frame index does too, so

    (frame_no - frame_index) mod 4096

must be constant. Nothing in the decoder can fabricate that -- not a wrong
scrambler phase, not a wrong interleaver, not the per-block level search.
Measured on BGAN19.wav: 13/13 on-cycle frames matched, 0/208 off-cycle frames
matched, and the hits fall on a strict 17-frame cycle. See docs/VALIDATION.md.

Layout, Figures 5.44/5.45, bit 8 = MSB of each octet:

    Octet 1 : x 0 0 0 1 | slength(3)     SDU header
    Octet 2 : rnc-id(8)
    Octet 3 : net-ver(4) | frame-no high nibble
    Octet 4 : frame-no low octet
    Octet 5 : spot-beam-present(1) | f-bearer(3) | bct-id(4)
    Octet 6 : spot-beam-id(8)            only when spot-beam-present
    then    : bb-avp-list                AVPList OPTIONAL

`spot-beam-present` is easy to get wrong and costly if you do. Figure 5.44
prints that bit as 0 only because it illustrates the sb-not-present CHOICE
arm; Figure 5.45 is the sb-present arm and inserts an extra spot-beam-id
octet. BGAN19 sets it in every confirmed BulletinBoard (spot-beam-id 134), so
reading it as a spare bit puts the AVP list 8 bits early -- which is exactly
the error that made the first AVP look like undefined type 134.

Walking the AVP list is then deterministic. Clause 5.7.1 allocates BCtAVPType
so that "the parameter length can be obtained from the lower three bits", so
each AVP occupies 1 + ((type & 7) + 1) octets. At the right offset all 78 AVPs
across the 13 confirmed BulletinBoards resolve to defined types; 8 bits either
side drops to 82-83%.

One field is still unexplained: octet 1 bit 6 reads 1 where both figures print
0. It affects no parsed field, and a one-bit misalignment is ruled out because
that would destroy the frame-no match, which is exact.

The SDU is preceded by the BCtPDU header, 3 octets here (first and third
octets fixed at 0xc9/0xc1). That length is measured, not read from the spec,
so BCTPDU_HDR_BITS may not generalise to other PDU types.
"""
from __future__ import annotations
from dataclasses import dataclass, field

import numpy as np

from . import bctrl

BCTPDU_HDR_BITS = 24        # measured on BGAN19; see caveat above
FRAME_NO_BITS = 12
FRAME_NO_MOD = 1 << FRAME_NO_BITS


@dataclass
class AVP:
    type: int
    value: bytes

    @property
    def param_len(self):
        return (self.type & 7) + 1


@dataclass
class BulletinBoard:
    slength: int
    rnc_id: int
    net_ver: int
    frame_no: int
    spot_beam_present: bool
    f_bearer: int
    bct_id: int
    spot_beam_id: int | None
    avps: list = field(default_factory=list)

    def code_rate_avp(self):
        """The ForwardBearerCodeRateParam entries, or None if not present."""
        for a in self.avps:
            if (a.type >> 3) == bctrl.FWD_CODE_RATE_TAG:
                return bctrl.parse_fwd_code_rate(bytes([a.type]) + a.value)
        return None

    def block_levels(self, uw_level, nblocks=8):
        """Per-block coding levels this BulletinBoard implies."""
        return bctrl.resolve_block_levels(uw_level, self.code_rate_avp(),
                                          nblocks)

    def __str__(self):
        sb = f" spot-beam-id={self.spot_beam_id}" if self.spot_beam_present \
             else ""
        return (f"BulletinBoard(rnc-id={self.rnc_id} net-ver={self.net_ver} "
                f"frame-no={self.frame_no} f-bearer={self.f_bearer} "
                f"bct-id={self.bct_id}{sb} avps={len(self.avps)})")


def _u(bits, pos, width):
    return int(np.asarray(bits[pos:pos + width], dtype=np.int64)
               @ (1 << np.arange(width - 1, -1, -1)))


def walk_avps(bits, pos, limit=64):
    """Walk an AVPList from bit offset `pos`, using the length-in-tag rule.

    Stops at the end of the payload or when an AVP would overrun it. Does not
    validate types -- `bgan.bctrl` and the caller decide what is meaningful.
    """
    n = (len(bits) - pos)//8
    if n <= 0:
        return []
    by = np.packbits(bits[pos:pos + n*8]).tobytes()
    out, p = [], 0
    while p < len(by) and len(out) < limit:
        ty = by[p]
        ln = (ty & 7) + 1
        if p + 1 + ln > len(by):
            break
        out.append(AVP(ty, by[p + 1:p + 1 + ln]))
        p += 1 + ln
    return out


def parse(payload_bits, hdr_bits=BCTPDU_HDR_BITS):
    """Parse a block-0 payload as BCtPDU header + BulletinBoard SDU.

    Returns a BulletinBoard, or None if the payload is too short. This does
    *not* establish that a BulletinBoard is actually present -- the SDU has no
    checksum and clause 5.4.3.0 sends it only at intervals -- so use `confirm`
    across several frames first.
    """
    b = np.asarray(payload_bits, dtype=np.uint8)
    if len(b) < hdr_bits + 48:
        return None
    p = hdr_bits
    sb = bool(b[p + 32])
    return BulletinBoard(
        slength=_u(b, p + 5, 3),
        rnc_id=_u(b, p + 8, 8),
        net_ver=_u(b, p + 16, 4),
        frame_no=_u(b, p + 20, FRAME_NO_BITS),
        spot_beam_present=sb,
        f_bearer=_u(b, p + 33, 3),
        bct_id=_u(b, p + 36, 4),
        spot_beam_id=_u(b, p + 40, 8) if sb else None,
        avps=walk_avps(b, p + (48 if sb else 40)),
    )


def confirm(payloads, frames, hdr_bits=BCTPDU_HDR_BITS):
    """Find the frame-no offset consistent across the most frames.

    `payloads` is a sequence of block-0 payload bit arrays and `frames` their
    frame indices. Returns (offset, mask, period): `mask` selects the payloads
    that really do carry a BulletinBoard, and `period` is the frame spacing if
    one value explains every hit (17 on BGAN19), else None.

    Hits expected by chance is len(payloads)/4096, so even a handful of
    agreeing frames is conclusive.
    """
    frames = np.asarray(frames, dtype=np.int64)
    fn = np.array([parse(p, hdr_bits).frame_no for p in payloads],
                  dtype=np.int64)
    off = (fn - frames) % FRAME_NO_MOD
    vals, counts = np.unique(off, return_counts=True)
    best = int(vals[np.argmax(counts)])
    mask = off == best

    period = None
    hit = frames[mask]
    if len(hit) > 2:
        gaps = np.diff(hit)
        g = int(np.gcd.reduce(gaps))
        if g > 1 and np.all(gaps % g == 0):
            period = g
    return best, mask, period
