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

def channelise(path, secs=None, up=2, sps=4):
    """Raw WAV -> matched-filtered stream at `sps`, plus clock estimate.

    Returns (y, fs, rs_est, tau0, info). Symbol extraction is deliberately
    NOT done here -- see symbols_at / survey_taus for why the timing phase
    cannot be fixed once for the whole capture.
    """
    x, sr = recv.load_wav_iq(path, secs=secs)
    x = x - x.mean()
    raw_sr, raw_n = sr, len(x)

    # Upsample before any |x|^2 estimator so the symbol-rate line is unfolded.
    if up > 1 and spec.F80T45X8B.rs >= sr/2:
        x = resample_poly(x, up, 1)
        sr = sr*up

    y, fs, centre = recv.channelize(x, sr, sps=sps)
    rs_est, tau0 = recv.estimate_symbol_clock(y, fs)
    info = dict(raw_sr=raw_sr, raw_n=raw_n, secs=raw_n/raw_sr, sr=sr,
                centre=centre, rs=rs_est,
                ppm=(rs_est - spec.F80T45X8B.rs)/spec.F80T45X8B.rs*1e6,
                nframes=int(len(y)/(fs/rs_est))//MOS)
    return y, fs, rs_est, tau0, info


def symbols_at(y, fs, rs_est, tau, dtype=np.complex128):
    """Symbol stream sampled at timing phase `tau` (in samples)."""
    s = recv.extract_symbols(y, fs, rs_est, tau)
    s = s/np.sqrt(np.mean(np.abs(s)**2))
    return s.astype(dtype)


def front_end(path, secs=None, up=2, sps=4):
    """Backwards-compatible single-tau front end. Returns (symbols, info).

    Prefer decode_capture(): a single global timing phase loses most of the
    capture on any recording with dropped samples.
    """
    y, fs, rs_est, tau0, info = channelise(path, secs, up, sps)
    s = symbols_at(y, fs, rs_est, tau0)
    m4 = recv.timing_quality(s)
    info.update(m4=m4, esn0=recv.esn0_from_m4(m4), nframes=len(s)//MOS)
    return s, info


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


TAU_STEPS = 8


def survey_taus(y, fs, rs_est, tau0, ntau=TAU_STEPS, win=320, levels=None):
    """Per frame, find the best (timing phase, frame offset) pair.

    This exists because `extract_symbols` applies one timing phase to the
    whole capture, and that is wrong on any recording with dropped samples.
    At 4 samples/symbol, losing N samples shifts the symbol timing by
    (N mod 4)/4 of a symbol. Only the whole-symbol part shows up as a frame
    offset step; the fractional remainder moves every later sample off the
    eye centre and no integer-offset search can undo it.

    Measured on BGAN19: two runs of frames yielding 0/80 blocks with a global
    tau yield 80/80 at a shift of 5/8 symbol, while a run that was already at
    80/80 collapses to 0/80 at that same shift. The effect is real, large, and
    the dominant cause of lost data -- far more than fading, which accounts
    for 0.41 dB across the whole capture.

    The differential-UW metric tracks it closely (38 -> 81 on a dead run at
    the correct phase), so the phase can be found by correlation rather than
    by trial decoding.

    Returns (tau_idx, offs, lvls, mets), one entry per frame.
    """
    levels = levels or spec.LEVELS_F80T45X8B
    pats = {lv: diff_uw(lv) for lv in levels}
    period = fs/rs_est

    nfr = None
    best = None
    for k in range(ntau):
        s = symbols_at(y, fs, rs_est, tau0 + (k/ntau)*period, np.complex64)
        d = np.empty_like(s)
        d[0] = 0
        d[1:] = s[1:]*np.conj(s[:-1])
        if nfr is None:
            nfr = len(s)//MOS - 1
            best = (np.zeros(nfr, int), np.zeros(nfr, int),
                    [None]*nfr, np.full(nfr, -1.0))
        got = _corr(d, 1, MOS, pats)          # acquire on this phase
        off = got[1] if got else 0
        for f in range(nfr):
            lo = max(1, off + f*MOS - win) if f else max(1, off - 4)
            g = _corr(d, lo, 2*win if f else 8, pats)
            if g is None:
                continue
            m, p, lv = g
            if m > best[3][f]:
                best[0][f], best[1][f], best[2][f], best[3][f] = k, p, lv, m
            off = p - f*MOS
    return best


def decode_capture(path, secs=None, thr=0.90, level=None, search_levels=True,
                   ntau=TAU_STEPS, progress=None):
    """Full chain with per-frame timing recovery. Returns (records, info).

    records: list of (frame, block, level, agreement, payload_bits)
    """
    y, fs, rs_est, tau0, info = channelise(path, secs)
    if progress:
        progress(0.10, "searching timing phases")
    tau_idx, offs, lvls, mets = survey_taus(y, fs, rs_est, tau0, ntau)
    nfr = len(offs)
    info["nframes"] = nfr
    info["tau_hist"] = np.bincount(tau_idx, minlength=ntau).tolist()

    period = fs/rs_est
    recs = []
    const = []
    done = 0
    for k in range(ntau):
        which = np.flatnonzero(tau_idx == k)
        if not len(which):
            continue
        s = symbols_at(y, fs, rs_est, tau0 + (k/ntau)*period)
        if k == 0:
            m4 = recv.timing_quality(s)
            info.update(m4=m4, esn0=recv.esn0_from_m4(m4))
        for f in which:
            for b in range(8):
                blk = prepare(s, int(offs[f]), b)
                if blk is None:
                    continue
                if search_levels and not level:
                    bits, lv, ag = decode_block_anylevel(blk, thr)
                else:
                    bits, ag, _ = decode_block(blk, level or lvls[f])
                    lv = (level or lvls[f]) if ag > thr else None
                if lv is not None:
                    recs.append((int(f), b, lv, ag, mod.descramble(bits)))
                    if len(const) < 150:
                        const.append(blk[:400].astype(np.complex64))
            done += 1
            if progress and done % 4 == 0:
                progress(0.10 + 0.88*done/nfr,
                         f"decoded {done}/{nfr} frames - {len(recs)} blocks",
                         np.concatenate(const[-40:]) if const else None)
    recs.sort(key=lambda r: (r[0], r[1]))
    info["const"] = np.concatenate(const) if const else np.zeros(0, complex)
    return recs, info, (tau_idx, offs, lvls, mets)


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
    ap.add_argument("--ntau", type=int, default=TAU_STEPS,
                    help="timing phases to search per frame (1 = old "
                         "single-phase behaviour)")
    ap.add_argument("--no-search-levels", dest="search_levels",
                    action="store_false",
                    help="use the UW level for all 8 blocks (much faster, "
                         "but blocks 1-7 mostly will not decode)")
    ap.add_argument("--out", default="work/wav_payload.bin")
    a = ap.parse_args()

    def prog(frac, text):
        print(f"\r  [{100*frac:5.1f}%] {text}          ", end="", flush=True)

    recs, info, (tau_idx, offs, lvls, mets) = decode_capture(
        a.path, secs=a.secs, thr=a.thr, level=a.level,
        search_levels=a.search_levels, ntau=a.ntau, progress=prog)
    print()
    print(f"{Path(a.path).name}: {info['secs']:.1f} s @ {info['raw_sr']/1e3:.0f} kHz")
    print(f"  centre {info['centre']:+.1f} Hz   rs {info['rs']:.3f} Bd "
          f"({info['ppm']:+.1f} ppm)")
    print(f"  M4/M2^2 {info['m4']:.3f} -> Es/N0 {info['esn0']:.1f} dB   "
          f"{info['nframes']} frames")

    rel = offs - np.arange(len(offs))*MOS
    steps = np.flatnonzero(np.diff(rel) | np.diff(tau_idx)) + 1
    print(f"  timing phases used (of {a.ntau}): {info['tau_hist']}")
    print(f"  frame offset: {len(steps)+1} run(s), "
          f"span {rel.max()-rel.min()} symbols")
    for b, e in list(zip([0]+list(steps), list(steps)+[len(rel)]))[:24]:
        print(f"    frames {b:4d}..{e-1:4d}  offset {rel[b]:6d}  "
              f"tau {tau_idx[b]}/{a.ntau}  level {lvls[b]:>3}  "
              f"metric {np.median(mets[b:e]):5.1f}")

    ntot = len(offs)*8
    nok = len(recs)
    bybl = np.zeros(8, int)
    out = []
    for f, b, lv, ag, bits in recs:
        bybl[b] += 1
        out.append(bits)
    recs = [(f, b, lv, ag, i) for i, (f, b, lv, ag, _) in enumerate(recs)]
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
                            offs=offs, mets=mets, tau=tau_idx)
        print(f"  wrote {p} ({len(payload)} bytes) and {p.with_suffix('.npz')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
