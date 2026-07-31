"""Parse BulletinBoards out of a decoded capture and cross-check the levels.

Reads the .npz written by tools/decode_wav.py, locates the BulletinBoard SDUs
by their frame-no counter, parses the bb-avp-list, and checks the
ForwardBearerCodeRateParam against the coding levels that decode_wav.py found
independently by trial decode.

That cross-check is the point: a structural parse of a broadcast control
message and a brute-force search over ten levels have nothing in common, so
agreement between them is meaningful in a way that neither is alone.
"""
from __future__ import annotations
import argparse
import io
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bgan import bulletin                                    # noqa: E402

SPEC_TXT = Path("work/spec31.txt")


def avp_type_names():
    """BCtAVPType enum from the spec text, if it has been extracted."""
    if not SPEC_TXT.exists():
        return {}
    t = io.open(SPEC_TXT, encoding="utf-8", errors="replace").read()
    i = t.find("BCtAVPType ::=")
    j = t.find("} (0..255)", i)
    if i < 0 or j < 0:
        return {}
    return {int(n): nm
            for nm, n in re.findall(r"([a-z0-9\-]+)\s*\((\d+)\)", t[i:j])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("npz", nargs="?", default="work/wav_payload.npz")
    ap.add_argument("--uw-level", default="L3")
    ap.add_argument("--avps", type=int, default=4,
                    help="leading AVPs to show (the walk runs past the real "
                         "end of the list; see docs/VALIDATION.md)")
    a = ap.parse_args()

    names = avp_type_names()
    d = np.load(a.npz, allow_pickle=True)
    frame, block, level = d["frame"], d["block"], np.asarray(d["level"])
    lens, allbits = d["lens"], d["bits"]
    cum = np.concatenate([[0], np.cumsum(lens)])

    sel = [i for i in range(len(frame))
           if block[i] == 0 and str(level[i]) == a.uw_level]
    if not sel:
        print(f"no block-0 payloads at level {a.uw_level}")
        return 1
    pays = [allbits[cum[i]:cum[i] + lens[i]] for i in sel]
    fr = frame[sel]
    emp = {(int(frame[i]), int(block[i])): str(level[i])
           for i in range(len(frame))}

    off, mask, period = bulletin.confirm(pays, fr)
    n = len(pays)
    print(f"{n} block-0/{a.uw_level} payloads")
    print(f"  frame-no offset {off}; {mask.sum()} carry a BulletinBoard "
          f"({n/4096.0:.3f} expected by chance)")
    print(f"  broadcast period: {period} frames"
          if period else "  no single period explains the hits")

    agree = dis = miss = 0
    seen = Counter()
    for i in np.flatnonzero(mask):
        bb = bulletin.parse(pays[i])
        f = int(fr[i])
        lv = bb.block_levels(a.uw_level)
        marks = ""
        for b in range(8):
            g = emp.get((f, b))
            if g is None:
                marks += "."
                miss += 1
            elif g == lv[b]:
                marks += "Y"
                agree += 1
            else:
                marks += "x"
                dis += 1
        tags = ", ".join(names.get(x.type, f"0x{x.type:02x}")
                         for x in bb.avps[:a.avps])
        print(f"  f{f:4d} {bb}")
        print(f"        levels {lv} {marks}")
        print(f"        avps   {tags}")
        for x in bb.avps[:a.avps]:
            seen[(names.get(x.type, f"0x{x.type:02x}"), x.value.hex())] += 1

    print(f"\nAVP-predicted vs trial-decode levels: "
          f"{agree} agree, {dis} disagree, {miss} block(s) not decoded")
    print("\nconstant leading-AVP values:")
    for (nm, hexv), c in seen.most_common(8):
        if c == int(mask.sum()):
            print(f"  {nm:28} {hexv}   (all {c})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
