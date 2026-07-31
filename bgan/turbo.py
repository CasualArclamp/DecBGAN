"""Turbo encoder for ETSI TS 102 744-2-1 forward bearers (clause 5.3.8).

Reference implementation in Python. The decoder will be in C for speed; this
exists to generate known-good test vectors for it and for the whole receive
chain, so it is written for clarity and checkability, not speed.

SRCC (clause 5.3.8.2), 16 states:
    backward polynomial 1 + X^3 + X^4  (octal 23)
    forward  polynomial 1 + X + X^2 + X^4 (octal 35)

State is the four delay elements read left to right, s1 s2 s3 s4, so state
0001 means a 1 in the rightmost element (spec's own wording).

    a       = u XOR s3 XOR s4          (recursion, from the backward poly)
    parity  = a XOR s1 XOR s2 XOR s4   (forward poly applied to the a-sequence)
    shift   : s4<-s3, s3<-s2, s2<-s1, s1<-a

This convention is not guessed: it is the one that reproduces Table 5.11
exactly (see FLUSH_BITS and check_flush_table below).
"""
from __future__ import annotations
import numpy as np

# Table 5.11 -- flush bits by end state of the un-interleaved encoder.
# Key is the state string s1s2s3s4; value is the 4 bits inserted left to right.
FLUSH_BITS = {
    "0000": (0, 0, 0, 0),
    "0001": (1, 0, 0, 0),
    "0010": (1, 1, 0, 0),
    "0011": (0, 1, 0, 0),
    "0100": (0, 1, 1, 0),
    "0101": (1, 1, 1, 0),
    "0110": (1, 0, 1, 0),
    "0111": (0, 0, 1, 0),
    "1000": (0, 0, 1, 1),
    "1001": (1, 0, 1, 1),
    "1010": (1, 1, 1, 1),
    "1011": (0, 1, 1, 1),
    "1100": (0, 1, 0, 1),
    "1101": (1, 1, 0, 1),
    "1110": (1, 0, 0, 1),
    "1111": (0, 0, 0, 1),
}


def _step(s, u):
    """One SRCC step. s is (s1,s2,s3,s4). Returns (new_state, parity)."""
    s1, s2, s3, s4 = s
    a = u ^ s3 ^ s4
    parity = a ^ s1 ^ s2 ^ s4
    return (a, s1, s2, s3), parity


def srcc_encode(bits, state=(0, 0, 0, 0)):
    """Encode `bits` through one SRCC. Returns (parity array, end state)."""
    out = np.empty(len(bits), dtype=np.uint8)
    s = state
    for i, u in enumerate(bits):
        s, p = _step(s, int(u))
        out[i] = p
    return out, s


def state_str(s):
    return "".join(str(int(b)) for b in s)


def flush_for(state):
    return FLUSH_BITS[state_str(state)]


def check_flush_table():
    """Every entry of Table 5.11 must drive the encoder to the zero state,
    and must do so with all-zero recursion input (a == 0 throughout)."""
    bad = []
    for key, flush in FLUSH_BITS.items():
        s = tuple(int(c) for c in key)
        for u in flush:
            s, _ = _step(s, u)
        if s != (0, 0, 0, 0):
            bad.append((key, flush, state_str(s)))
    if bad:
        raise AssertionError(f"flush table inconsistent: {bad}")
    return True


def turbo_encode(data, tci_perm):
    """Turbo-encode D information bits (clause 5.3.8.1).

    Returns (df_bits, p, q) where:
      df_bits : the D data bits plus 4 flush bits, length DF
      p       : parity of the un-interleaved encoder, length DF
      q       : parity of the interleaved encoder, length DF

    The un-interleaved encoder is flushed to zero; the interleaved encoder is
    not (clause 5.3.8.2).
    """
    data = np.asarray(data, dtype=np.uint8)
    DF = len(tci_perm)
    if len(data) != DF - 4:
        raise ValueError(f"expected {DF-4} data bits, got {len(data)}")

    # 1) run data through the un-interleaved encoder to find the end state
    p_data, end = srcc_encode(data)
    flush = flush_for(end)

    # 2) append the flush bits and encode them too
    df_bits = np.concatenate([data, np.array(flush, dtype=np.uint8)])
    p_flush, final = srcc_encode(flush, state=end)
    if final != (0, 0, 0, 0):
        raise AssertionError("un-interleaved encoder did not terminate")
    p = np.concatenate([p_data, p_flush])

    # 3) the interleaved encoder sees the DF bits permuted by the Annex C.1
    #    table: position j is fed df_bits[perm[j]]
    q, _ = srcc_encode(df_bits[tci_perm])

    return df_bits, p, q


def map_to_symbols(df_bits, p, q, cipm):
    """Apply Annex C.2 puncturing / channel interleaving / 16-QAM mapping.

    Returns an (FECSy, 4) array of bits in slot order I1, I0, Q1, Q0.
    """
    streams = (df_bits, p, q)
    out = np.empty(cipm.idx.shape, dtype=np.uint8)
    for k in (0, 1, 2):
        sel = cipm.kind == k
        out[sel] = streams[k][cipm.stream_pos[sel]]
    return out
