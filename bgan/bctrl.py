"""Bearer Control layer bits needed by the physical decoder.

From ETSI TS 102 744-3-1. Only the parts that affect demodulation live here;
full BCt-PDU parsing belongs above this layer.
"""
from __future__ import annotations

import numpy as np

from . import spec

# Table 5.20: CodeRate value -> bearer subtype, for "All other Bearers"
# (the R80T0.5Q/R80T1Q column differs above +1 and does not apply to us).
CODE_RATE = {
    -8: "L8", -7: "L7", -6: "L6", -5: "L5", -4: "L4", -3: "L3",
    -2: "L2", -1: "L1", 0: "R", 1: "H1", 2: "H2", 3: "H3",
    4: "H4", 5: "H5", 6: "H6",
    # 7 is n/a for these bearers
}
CODE_RATE_INV = {v: k for k, v in CODE_RATE.items()}

FWD_CODE_RATE_TAG = 0b10010          # top 5 bits of octet 1, Figure 5.109


def parse_fwd_code_rate(octets):
    """Parse a ForwardBearerCodeRateParam AVP (clause 5.7.16, Figure 5.109).

    Returns list of (modulation_index_increase, block_num, level), or None if
    the octets are not this AVP.

        octet 1 : 1 0 0 1 0 | prm-len(3)   where prm-len = n-1
        octet k : mii(1) | block-num(3) | coding-rate(4, signed)
    """
    b = bytes(octets)
    if len(b) < 2 or (b[0] >> 3) != FWD_CODE_RATE_TAG:
        return None
    n = (b[0] & 0x07) + 1
    if len(b) < 1 + n:
        return None
    out = []
    for k in range(n):
        v = b[1+k]
        mii = (v >> 7) & 1
        blk = (v >> 4) & 0x07
        cr = v & 0x0F
        if cr >= 8:
            cr -= 16                  # 4-bit two's complement, INTEGER(-8..7)
        lvl = CODE_RATE.get(cr)
        if lvl is None:
            return None
        out.append((mii, blk, lvl))
    return out


def build_fwd_code_rate(entries):
    """Inverse of parse_fwd_code_rate, for round-trip testing."""
    n = len(entries)
    if not 1 <= n <= 8:
        raise ValueError("1..8 BlockRate entries")
    out = [(FWD_CODE_RATE_TAG << 3) | (n-1)]
    for mii, blk, lvl in entries:
        cr = CODE_RATE_INV[lvl] & 0x0F
        out.append((mii << 7) | ((blk & 7) << 4) | cr)
    return bytes(out)


def resolve_block_levels(uw_level, avp=None, nblocks=None):
    """Per-FEC-block coding levels for one frame.

    Clause 5.7.16: on forward bearers with no outer interleaving -- which
    includes F80T4.5X-8B -- the ForwardBearerCodeRateParam AVP is "only
    provided if the coding rate changes from the coding rate of the first FEC
    block in the frame which is implicitly signalled in the unique word".

    So with no AVP, every block uses the UW-signalled level. That is the
    specified behaviour, not an assumption, and it means brute-forcing all ten
    levels per block is unnecessary.

    A signalled rate applies from its block-num "for the rest of the frame or
    until another change is signalled".
    """
    nblocks = nblocks or spec.F80T45X8B.nblocks
    levels = [uw_level]*nblocks
    if not avp:
        return levels
    for _mii, blk, lvl in sorted(avp, key=lambda e: e[1]):
        for i in range(blk, nblocks):
            levels[i] = lvl
    return levels
