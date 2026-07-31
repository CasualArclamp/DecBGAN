"""Decode a real BGAN F80T4.5X-8B capture end to end: WAV in, payload out.

This runs entirely on our own front end -- no external demodulator. That
matters, because the earlier "-220 ppm frame drift" that motivated a tracking
PLL was an artifact of consuming another tool's *resampled* demod output. On
the raw capture the symbol clock measures -0.2 ppm over 39 s, and the frame
offset is piecewise constant rather than drifting (see track_offsets).

Two facts about a 192 kHz capture of a 189 kHz signal, both of which cost me
time:

  * The capture is complex IQ, so the full 192 kHz span is usable and a 189 kHz
    signal fits. What does *not* fit is the 151.2 kHz cyclostationary line in
    |x|^2 -- that is a real-valued signal whose spectrum is only unique to
    sr/2 = 96 kHz, so the line folds to |151200-192000| = 40.8 kHz. Upsampling
    by 2 before squaring removes the fold. This is a property of the estimator,
    not of the recording.
  * 189 kHz of signal in 192 kHz leaves ~1.5 kHz of guard per side, which
    clips the RRC excess band. That is a genuine (small) ISI penalty.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bgan import spec, mod, recv, tx
from bgan import decoder as dec
from bgan.pipeline import verify_block
from bgan.turbo import map_to_symbols, turbo_encode

MOS, STEP, UWLEN, DAT, GRP = 12096, 137, 40, 136, 11


# --- front end ---------------------------------------------------------------

def front_end(path, secs=None, up=2, sps=4):
    """Raw WAV -> unit-power symbol stream. Returns (symbols, info dict)."""
    x, sr = recv.load_wav_iq(path, secs=secs)
    x = x - x.mean()
    raw_sr, raw_n = sr, len(x)

    # Upsample before any |x|^2 estimator so the symbol-rate line is unfolded.
    if up > 1 and spec.F80T45X8B.rs >= sr/2:
        x = resample_poly(x, up, 1)
        sr = sr*up

    y, fs, centre = recv.channelize(x, sr, sps=sps)
    rs_est, tau0 = recv.estimate_symbol_clock(y, fs)
    s = recv.extract_symbols(y, fs, rs_est, tau0)
    s = s/np.sqrt(np.mean(np.abs(s)**2))
    m4 = recv.timing_quality(s)
    return s, dict(raw_sr=raw_sr, raw_n=raw_n, secs=raw_n/raw_sr, sr=sr,
                   centre=centre, rs=rs_est,
                   ppm=(rs_est - spec.F80T45X8B.rs)/spec.F80T45X8B.rs*1e6,
                   m4=m4, esn0=recv.esn0_from_m4(m4), nframes=len(s)//MOS)


# --- frame acquisition -------------------------------------------------------

def diff_uw(level):
    b = mod.uw_bits(level)
    return np.where(b[1:] ^ b[:-1], -1.0, 1.0)


def _corr(d, start, span, pats):
    """Best (metric, offset, level) for a UW starting anywhere in [start, start+span)."""
    seg = d[start:start + span + UWLEN]
    if len(seg) < span + UWLEN:
        return None
    best = None
    for l, w in pats.items():
        c = np.abs(np.convolve(seg, w[::-1], mode="valid"))[:span]
        j = int(np.argmax(c))
        if best is None or c[j] > best[0]:
            best = (float(c[j]), start + j - 1, l)
    return best


def track_offsets(s, win=320, levels=None):
    """Per-frame UW offset. Acquire on frame 0, then search a window.

    The offset is piecewise constant on real captures: on BGAN19 it holds for
    20-40 frames then steps by tens to ~150 symbols. It is emphatically NOT a
    linear drift, so do not fit a period, and do NOT fold the correlation
    across frames -- a fold smears the plateaus together and lands on a
    sidelobe family that decodes nothing.

    Returns (offsets, levels, metrics) arrays, one entry per frame.
    """
    levels = levels or spec.LEVELS_F80T45X8B
    pats = {l: diff_uw(l) for l in levels}
    d = np.empty_like(s)
    d[0] = 0
    d[1:] = s[1:]*np.conj(s[:-1])

    nfr = len(s)//MOS - 1
    got = _corr(d, 1, MOS, pats)          # full scan to acquire
    if got is None:
        raise RuntimeError("capture too short to acquire")
    m0, off, lvl0 = got

    offs = np.empty(nfr, int)
    lvls = [None]*nfr
    mets = np.empty(nfr)
    offs[0], lvls[0], mets[0] = off, lvl0, m0      # frame 0 is the acquisition
    for f in range(1, nfr):
        lo = max(1, off + f*MOS - win)
        got = _corr(d, lo, 2*win, pats)
        if got is None:
            offs[f], lvls[f], mets[f] = off + f*MOS, lvls[f-1], 0.0
            continue
        m, p, l = got
        offs[f], lvls[f], mets[f] = p, l, m
        off = p - f*MOS                    # carry the plateau forward
    return offs, lvls, mets


# --- per-block extraction and decode ----------------------------------------

def block_symbols(z, fb, b):
    data, dpos, pil, ppos = [], [], [], []
    for gi in range(GRP):
        g = GRP*b + gi
        base = fb + UWLEN + STEP*g
        if base + DAT >= len(z):
            return None
        data.append(z[base:base + DAT])
        dpos.append(gi*137.0 + np.arange(DAT))
        pil.append(z[fb + 176 + STEP*g])
        ppos.append(gi*137.0 + 136.0)
    return (np.concatenate(data), np.concatenate(dpos),
            np.array(pil), np.array(ppos))


def prepare(z, fb, b):
    """Per-block amplitude normalisation and linear phase detrend from pilots.

    Both are per-block, not per-frame: a frame-wide fit produced no decodes at
    all on real captures. No rotation search is needed -- the pilot is a single
    known symbol (1111, the 45 deg outer corner), so the fit resolves the
    4-fold ambiguity outright. Measured: every block that decodes does so at
    rot=0; the other three rotations only ever win among blocks that fail.
    """
    got = block_symbols(z, fb, b)
    if got is None:
        return None
    d, dp, pl, pp = got
    sc = 1.34164/(np.mean(np.abs(pl)) + 1e-9)      # pilots -> outer-corner mag
    d, pl = d*sc, pl*sc
    c1, c0 = np.polyfit(pp, np.unwrap(np.angle(pl)), 1)
    return d*np.exp(-1j*(c0 + c1*dp))*np.exp(1j*np.pi/4)


def decode_block(blk, level, n0=0.5, iters=8):
    """Returns (payload_bits, parity_agreement, lr_ok)."""
    t = tx.tables(level)
    llr = dec.soft_demap(blk, n0)
    Ls, Lp, Lq = dec.deinterleave_llrs(llr, t.cipm)
    bits, _ = dec.turbo_decode(Ls, Lp, Lq, t.perm.astype(np.int64),
                               iters, dec.NXT, dec.PAR)
    df2, p2, q2 = turbo_encode(bits[:t.D], t.perm)
    slots = map_to_symbols(df2, p2, q2, t.cipm)
    hard = (llr < 0).astype(np.uint8)
    sel = t.cipm.kind != 0            # parity only; systematic bits inflate it
    ag = float(np.mean(slots[sel] == hard[sel]))
    return bits[:t.D], ag, verify_block(bits, t, llr)


def decode_block_anylevel(blk, thr=0.90, levels=None):
    """Identify the coding level by trial decode. Returns (bits, level, ag).

    Only block 0's level is signalled by the unique word. Blocks 1..7 carry it
    in a ForwardBearerCodeRateParam AVP inside block 0's payload, which we
    cannot yet locate reliably -- a 2-byte AVP matches noise far too often to
    trust. Trial decoding is used instead, and it is safe here because the
    parity test turns out to be an unambiguous discriminator: over ~480
    block-tries at 10 levels each, **no block ever passed at two levels**. So
    a unique passer identifies the level rather than guessing it.

    Measured on BGAN19: this lifts blocks 1-7 from 0-14% decoded (at the
    UW-signalled level) to 75-93%. The levels really do vary per frame, which
    is exactly the AVP doing its job.

    Returns level=None if zero or more than one level passes.
    """
    levels = levels or spec.LEVELS_F80T45X8B
    hits = []
    for L in levels:
        bits, ag, _ = decode_block(blk, L)
        if ag > thr:
            hits.append((bits, L, ag))
    return hits[0] if len(hits) == 1 else (None, None, 0.0)


# --- driver ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--secs", type=float, default=None)
    ap.add_argument("--thr", type=float, default=0.90)
    ap.add_argument("--level", default=None, help="force coding level")
    ap.add_argument("--no-search-levels", dest="search_levels",
                    action="store_false",
                    help="use the UW level for all 8 blocks (much faster, "
                         "but blocks 1-7 mostly will not decode)")
    ap.add_argument("--out", default="work/wav_payload.bin")
    a = ap.parse_args()

    s, info = front_end(a.path, secs=a.secs)
    print(f"{Path(a.path).name}: {info['secs']:.1f} s @ {info['raw_sr']/1e3:.0f} kHz")
    print(f"  centre {info['centre']:+.1f} Hz   rs {info['rs']:.3f} Bd "
          f"({info['ppm']:+.1f} ppm)")
    print(f"  M4/M2^2 {info['m4']:.3f} -> Es/N0 {info['esn0']:.1f} dB   "
          f"{info['nframes']} frames")

    offs, lvls, mets = track_offsets(s)
    rel = offs - np.arange(len(offs))*MOS
    steps = np.flatnonzero(np.diff(rel)) + 1
    print(f"  frame offset: {len(steps)+1} plateau(s), "
          f"range {rel.min()}..{rel.max()}, span {rel.max()-rel.min()} symbols")
    for i, (b, e) in enumerate(zip([0]+list(steps), list(steps)+[len(rel)])):
        print(f"    frames {b:4d}..{e-1:4d}  offset {rel[b]:6d}  "
              f"level {lvls[b]:>3}  metric {np.median(mets[b:e]):5.1f}")

    nok = ntot = 0
    out, recs = [], []
    bybl = np.zeros(8, int)
    for f in range(len(offs)):
        for b in range(8):
            blk = prepare(s, int(offs[f]), b)
            if blk is None:
                continue
            ntot += 1
            if a.level or not a.search_levels:
                bits, ag, _ = decode_block(blk, a.level or lvls[f])
                lvl = (a.level or lvls[f]) if ag > a.thr else None
            else:
                bits, lvl, ag = decode_block_anylevel(blk, a.thr)
            if lvl is None:
                continue
            nok += 1
            bybl[b] += 1
            recs.append((f, b, lvl, ag, len(out)))
            out.append(mod.descramble(bits))
    print(f"\n  {nok}/{ntot} blocks decoded ({100*nok/max(ntot,1):.1f}%)")
    print("  per block index: " +
          "  ".join(f"b{i} {bybl[i]}" for i in range(8)))
    if out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = np.packbits(np.concatenate(out)).tobytes()
        p.write_bytes(payload)
        # keep provenance: the PDU layer needs frame/block order, and a flat
        # concatenation of only the *accepted* blocks silently reorders things
        np.savez_compressed(p.with_suffix(".npz"),
                            frame=np.array([r[0] for r in recs]),
                            block=np.array([r[1] for r in recs]),
                            level=np.array([r[2] for r in recs]),
                            agree=np.array([r[3] for r in recs]),
                            bits=np.concatenate(out).astype(np.uint8),
                            lens=np.array([len(o) for o in out]),
                            offs=offs, mets=mets)
        print(f"  wrote {p} ({len(payload)} bytes) and {p.with_suffix('.npz')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
