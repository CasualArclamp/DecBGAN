"""Validate the turbo codec against the Annex B2 required C/N0 figures.

If the simulated waterfall does not land a sensible implementation margin
below the spec's required C/N0 for every coding level, the codec is wrong --
and it is far better to learn that here than to blame it on the signal later.

Annex B2 quotes C/N0 including implementation loss, so the ideal-AWGN
threshold measured here should sit roughly 1 to 2 dB *below* it.
"""
import sys, time
import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from bgan import tx, mod, spec
from bgan import decoder as dec

RS_DB = 10*np.log10(spec.F80T45X8B.rs)

# Annex B2 required C/N0 (dBHz), forward bearer F80T4.5X-8B
REQ_CN0 = {"L3": 56.8, "L2": 57.7, "L1": 58.7, "R": 59.6, "H1": 60.8,
           "H2": 61.9, "H3": 63.0, "H4": 64.2, "H5": 65.1, "H6": 66.2}


def block_error_rate(level, esn0_db, trials, rng, niter=8):
    t = tx.tables(level)
    D = t.D
    perm = t.perm.astype(np.int64)
    n0 = 10**(-esn0_db/10)
    bad = 0
    biterr = 0
    for _ in range(trials):
        data = rng.integers(0, 2, D, dtype=np.uint8)
        scr = mod.scramble(data)
        sym = tx.encode_block(data, level)
        r = sym + np.sqrt(n0/2)*(rng.standard_normal(len(sym)) +
                                 1j*rng.standard_normal(len(sym)))
        llr = dec.soft_demap(r, n0)
        Ls, Lp, Lq = dec.deinterleave_llrs(llr, t.cipm)
        bits, _ = dec.turbo_decode(Ls, Lp, Lq, perm, niter, dec.NXT, dec.PAR)
        e = int(np.sum(bits[:D] != scr))
        biterr += e
        if e:
            bad += 1
    return bad/trials, biterr/(trials*D)


def find_threshold(level, rng, trials=12, lo=0.0, hi=20.0):
    """Coarse sweep then bisect for the Es/N0 giving BLER ~ 0.5."""
    # coarse: 1 dB steps upward until it decodes
    grid = np.arange(lo, hi+0.01, 1.0)
    prev = None
    for e in grid:
        bler, _ = block_error_rate(level, e, max(4, trials//3), rng)
        if bler <= 0.25:
            if prev is None:
                return e, []
            a, b = prev, e
            break
        prev = e
    else:
        return None, []
    # bisect
    trace = []
    for _ in range(4):
        m = (a+b)/2
        bler, ber = block_error_rate(level, m, trials, rng)
        trace.append((m, bler, ber))
        if bler > 0.5:
            a = m
        else:
            b = m
    return (a+b)/2, trace


def main():
    rng = np.random.default_rng(2024)
    print(f"{'lvl':4s}{'req C/N0':>10s}{'req Es/N0':>11s}"
          f"{'sim thresh':>12s}{'margin':>9s}")
    print("-"*46)
    rows = []
    t0 = time.time()
    for lvl in ["L3", "L2", "L1", "R", "H1", "H2", "H3", "H4", "H5", "H6"]:
        req_cn0 = REQ_CN0[lvl]
        req_esn0 = req_cn0 - RS_DB
        th, _ = find_threshold(lvl, rng)
        if th is None:
            print(f"{lvl:4s}{req_cn0:10.1f}{req_esn0:11.2f}{'FAILED':>12s}")
            rows.append((lvl, req_esn0, None))
            continue
        margin = req_esn0 - th
        print(f"{lvl:4s}{req_cn0:10.1f}{req_esn0:11.2f}{th:12.2f}{margin:+9.2f}")
        rows.append((lvl, req_esn0, th))
    print("-"*46)
    good = [(r[1]-r[2]) for r in rows if r[2] is not None]
    print(f"implementation margin: mean {np.mean(good):+.2f} dB, "
          f"range {min(good):+.2f}..{max(good):+.2f} dB")
    print(f"({time.time()-t0:.0f}s)")
    print()
    print("Expect every margin positive and roughly 1-2 dB. A negative margin")
    print("means the codec is worse than the spec allows -- i.e. it is wrong.")


if __name__ == "__main__":
    main()
