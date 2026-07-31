"""Hunt for a monotonic frame counter in decoded payloads.

The BulletinBoard SDU (TS 102 744-3-1) carries a 12-bit frame number that must
advance by exactly one per 80 ms frame. Nothing in noise does that, so locating
such a field is both a way to find the PDU and an independent proof that the
decode is correct -- far stronger than any re-encode check, which only shows
self-consistency.

Search: for every bit offset and every plausible width, read the field out of
each frame's payload and ask whether it tracks the frame index.
"""
from __future__ import annotations
import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def bitfield(bits, pos, width, msb_first=True):
    """Vectorised read of bits[:, pos:pos+width] as an integer per row."""
    w = bits[:, pos:pos + width].astype(np.int64)
    if not msb_first:
        w = w[:, ::-1]
    return w @ (1 << np.arange(width - 1, -1, -1))


def score_counter(vals, frames, width):
    """Fraction of consecutive frame pairs whose delta matches the frame gap.

    Uses modular arithmetic so wrap-around counts as a match, and requires the
    field to actually change (a constant field trivially matches gap 0).
    """
    mod = 1 << width
    d_val = np.diff(vals) % mod
    d_frm = np.diff(frames) % mod
    ok = d_val == d_frm
    return float(np.mean(ok)), int(np.sum(ok))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("npz", nargs="?", default="work/wav_payload.npz")
    ap.add_argument("--widths", default="8,10,12,14,16")
    ap.add_argument("--min-frames", type=int, default=25)
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()

    d = np.load(a.npz, allow_pickle=True)
    frame, block, level = d["frame"], d["block"], np.asarray(d["level"])
    lens, allbits = d["lens"], d["bits"]
    cum = np.concatenate([[0], np.cumsum(lens)])

    groups = defaultdict(list)
    for i in range(len(frame)):
        groups[(int(block[i]), str(level[i]))].append(i)

    widths = [int(w) for w in a.widths.split(",")]
    results = []
    for (b, lv), idx in sorted(groups.items()):
        if len(idx) < a.min_frames:
            continue
        n = int(lens[idx[0]])
        bits = np.stack([allbits[cum[i]:cum[i] + n] for i in idx])
        fr = frame[idx]
        for width in widths:
            for msb in (True, False):
                for pos in range(0, n - width):
                    v = bitfield(bits, pos, width, msb)
                    if len(np.unique(v)) < 3:
                        continue
                    f, k = score_counter(v, fr, width)
                    if f > 0.25:
                        results.append((f, k, len(idx) - 1, b, lv, pos,
                                        width, msb))
    results.sort(reverse=True)
    print(f"{len(groups)} (block, level) groups; "
          f"{sum(1 for g in groups.values() if len(g) >= a.min_frames)} "
          f"with >= {a.min_frames} frames\n")
    if not results:
        print("no field advances with the frame index above chance.")
        print("Chance for a w-bit field is 2^-w, so even a handful of hits")
        print("would stand out; none at all means the BulletinBoard is not a")
        print("plain counter at a fixed bit offset in these payloads.")
        return 0
    print(f"{'frac':>6} {'hits':>5}/{'pairs':<5} blk lvl  {'pos':>5} {'w':>3} order")
    for f, k, tot, b, lv, pos, width, msb in results[:a.top]:
        print(f"{f:6.3f} {k:5d}/{tot:<5d}  b{b} {lv:<3} {pos:5d} {width:3d} "
              f"{'msb' if msb else 'lsb'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
