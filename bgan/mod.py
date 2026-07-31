"""16-QAM mapping, scrambler and frame assembly (TS 102 744-2-1 clause 5)."""
from __future__ import annotations
import numpy as np

from . import spec

# --- 16-QAM, Table 5.3 -------------------------------------------------------
# b3 b2 select I, b1 b0 select Q:
#     b3 = 0 -> I negative, 1 -> I positive
#     b2 = 0 -> |I| = D/2,  1 -> |I| = 3D/2
# and likewise (b1, b0) for Q. Gray coded along each axis.
#
# Cross-checked against Figure 5.17: 1111 -> (+3D/2,+3D/2), 0101 -> (-3D/2,-3D/2).
# Note Table 5.3 row 13 has a typo in the PDF, "-3cD/2" for -3D/2.

def _axis(hi, mag):
    return (1.0 if hi else -1.0) * (1.5 if mag else 0.5)


QAM16 = np.array([
    complex(_axis((n >> 3) & 1, (n >> 2) & 1),
            _axis((n >> 1) & 1, n & 1))
    for n in range(16)
])
# unit mean power
QAM16_NORM = QAM16 / np.sqrt(np.mean(np.abs(QAM16)**2))


def bits4_to_symbol(slots, normalise=True):
    """slots: (...,4) array of bits in CIPM order I1, I0, Q1, Q0.

    I1->b3, I0->b2, Q1->b1, Q0->b0.
    """
    slots = np.asarray(slots, dtype=np.uint8)
    n = (slots[..., 0].astype(int) << 3 | slots[..., 1].astype(int) << 2 |
         slots[..., 2].astype(int) << 1 | slots[..., 3].astype(int))
    return (QAM16_NORM if normalise else QAM16)[n]


# --- Scrambler, clause 5.3.7 -------------------------------------------------
# 15-stage register, polynomial 1 + X + X^15, re-initialised at the start of
# each FEC block, applied to information bits before FEC encoding.
#
# The spec's initial value "110 1001 0101 1001" is 15 bits and equals 0x6959
# (26969) exactly -- it fits the 15-stage register with no ambiguity.

from functools import lru_cache


@lru_cache(maxsize=8)
def _pn_cached(n, init):
    """n bits of the scrambling sequence for polynomial 1 + X + X^15.

    Recurrence s[k] = s[k-1] ^ s[k-15]. The register is preloaded with `init`
    read **LSB-first**, and the emitted sequence starts *after* those 15 load
    bits -- i.e. the first output is the first computed feedback bit.

    This convention is not a guess. The spec gives the initial value (0x6959)
    but not the read-out order, and the two plausible orders differ. This one
    is confirmed against real signal: decoding BGAN19.wav frame 177 block 0
    and descrambling with it reproduces a known-good payload bit-exactly
    (agreement 1.0000), and it is the phase under which real traffic
    descrambles to readable ASCII. The MSB-first alternative does not.

    Equivalent to the m-sequence phase 12095 relative to MSB-first.
    """
    seq = [(init >> i) & 1 for i in range(15)]      # LSB-first preload
    for _ in range(n):
        seq.append(seq[-1] ^ seq[-15])
    out = np.array(seq[15:15 + n], dtype=np.uint8)
    out.setflags(write=False)
    return out


def pn_sequence(n, init=spec.SCRAMBLER_INIT):
    """n bits of the scrambling sequence. Cached: the scrambler is reset at
    the start of every FEC block, so all blocks share one sequence."""
    return _pn_cached(int(n), int(init))


def scramble(bits, init=spec.SCRAMBLER_INIT):
    seq = pn_sequence(len(bits), init)
    return np.asarray(bits, dtype=np.uint8) ^ seq


descramble = scramble          # XOR is its own inverse


# --- Unique word and pilots --------------------------------------------------

def uw_bits(level):
    """40 bits of the unique word for `level`, MSB first."""
    h = spec.UNIQUE_WORDS[level]
    v = int(h, 16)
    return np.array([(v >> (39 - i)) & 1 for i in range(40)], dtype=np.uint8)


def uw_symbols(level, normalise=True):
    """UW as 16-QAM symbols: bit 0 -> 0101, bit 1 -> 1111 (Figure 5.17)."""
    b = uw_bits(level)
    n = np.where(b == 1, spec.UW_BIT_NIBBLE[1], spec.UW_BIT_NIBBLE[0])
    return (QAM16_NORM if normalise else QAM16)[n]


def pilot_symbol(normalise=True):
    """Pilots are the UW upper-right-quadrant point, 1111 (clause 5.3.5)."""
    return (QAM16_NORM if normalise else QAM16)[spec.PILOT_NIBBLE]


# --- Frame assembly, Table 5.10 ----------------------------------------------

def assemble_frame(block_symbols, level, bearer=spec.F80T45X8B):
    """Build one 80 ms frame.

    block_symbols: (nblocks, teo) complex, the channel symbols of each FEC
    block, already turbo-encoded and channel-interleaved.

    Layout: the 40-symbol start UW, then the concatenated block symbols with a
    pilot inserted after every `data_syms` of them. F80T4.5X-8B uses no outer
    interleaver (clause 5.3.8.5 applies it only to FR80T2.5/FR80T5), so blocks
    are simply concatenated in order.
    """
    b = bearer
    blocks = np.asarray(block_symbols)
    if blocks.shape != (b.nblocks, b.teo):
        raise ValueError(f"expected {(b.nblocks, b.teo)}, got {blocks.shape}")
    data = blocks.reshape(-1)
    assert len(data) == b.encoded_syms

    pilot = pilot_symbol()
    out = [uw_symbols(level)]
    for i in range(b.pilot_syms):
        out.append(data[i*b.data_syms:(i+1)*b.data_syms])
        out.append(np.array([pilot]))
    if b.trailing_syms:
        out.append(data[b.pilot_syms*b.data_syms:])
    frame = np.concatenate(out)
    assert len(frame) == b.mos, (len(frame), b.mos)
    return frame


def frame_layout(bearer=spec.F80T45X8B):
    """Return boolean masks (uw, pilot, data) over one frame, for the receiver."""
    b = bearer
    uw = np.zeros(b.mos, bool)
    pil = np.zeros(b.mos, bool)
    uw[:b.uw_syms] = True
    pos = b.uw_syms
    for _ in range(b.pilot_syms):
        pos += b.data_syms
        pil[pos] = True
        pos += 1
    # a run of data symbols follows the final pilot on every bearer except
    # F80T4.5X-8B, where it happens to be empty (see Bearer.trailing_syms)
    assert pos + b.trailing_syms == b.mos, (pos, b.trailing_syms, b.mos)
    dat = ~(uw | pil)
    assert dat.sum() == b.encoded_syms
    return uw, pil, dat
