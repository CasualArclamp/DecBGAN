"""Locate the BulletinBoard frame-no field (TS 102 744-3-1 clause 5.4.3.3).

frame-no is a 12-bit INTEGER(0..4095) identifying the Bearer Control frame at
the RNC, so it advances by exactly one per 80 ms frame. Our own frame index
does too, which means

    (frame_no - frame_index) mod 4096

is a *constant* for every frame in which the BulletinBoard is present.

Testing for that constant, rather than for a delta between consecutive
records, matters: clause 5.4.3.0 says the BulletinBoard is transmitted at
regular intervals "but not necessarily in every frame". A consecutive-delta
test is broken by every gap; this one simply gets fewer votes.

It is also a genuine, unfakeable check on the decoder. A 12-bit field hitting
one value k times by chance has probability ~C(n,k)(1/4096)^k, so even a
handful of agreeing frames is decisive. Nothing about our own assumptions --
scrambler phase, interleaver, level search -- can manufacture it.
"""
from __future__ import annotations
import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.find_counter import bitfield          # noqa: E402


def scan(bits, frames, width=12, mod=4096):
    """Yield (votes, pos, const) for every bit offset, best const per offset."""
    n = bits.shape[1]
    for pos in range(0, n - width):
        v = bitfield(bits, pos, width, True)
        c = (v - frames) % mod
        val, k = Counter(c.tolist()).most_common(1)[0]
        yield k, pos, int(val)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("npz", nargs="?", default="work/wav_payload.npz")
    ap.add_argument("--min-records", type=int, default=20)
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()

    d = np.load(a.npz, allow_pickle=True)
    frame, block, level = d["frame"], d["block"], np.asarray(d["level"])
    lens, allbits = d["lens"], d["bits"]
    cum = np.concatenate([[0], np.cumsum(lens)])

    groups = defaultdict(list)
    for i in range(len(frame)):
        groups[(int(block[i]), str(level[i]))].append(i)

    out = []
    for (b, lv), idx in sorted(groups.items()):
        if len(idx) < a.min_records:
            continue
        n = int(lens[idx[0]])
        bits = np.stack([allbits[cum[i]:cum[i] + n] for i in idx])
        fr = frame[idx].astype(np.int64)
        for k, pos, const in scan(bits, fr):
            out.append((k, len(idx), b, lv, pos, const))
    out.sort(reverse=True)

    print(f"{'votes':>6}/{'recs':<5} blk lvl  {'bitpos':>6} {'const':>6}  "
          f"{'expected by chance':>18}")
    for k, n, b, lv, pos, const in out[:a.top]:
        exp = n/4096.0
        print(f"{k:6d}/{n:<5d}  b{b} {lv:<3} {pos:6d} {const:6d}  "
              f"{exp:18.3f}")
    if out and out[0][0] >= 5:
        k, n, b, lv, pos, const = out[0]
        print(f"\nBest: block {b} level {lv}, frame-no at bit {pos}, "
              f"offset {const}.")
        print(f"      {k} of {n} records agree; {n/4096.0:.3f} expected by "
              f"chance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
