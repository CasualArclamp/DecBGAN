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
import re
import sys
from pathlib import Path

import numpy as np
from collections import Counter
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bgan import spec, mod, recv, tx, bctrl
from bgan import decoder as dec
from bgan.pipeline import verify_block
from bgan.turbo import map_to_symbols, turbo_encode

MOS, STEP, UWLEN, DAT, GRP = 12096, 137, 40, 136, 11
NL_ = chr(10)


class NoCarrier(Exception):
    """No candidate carrier looks like F80T4.5X-8B.

    Carries the probe table so the caller can say what WAS found.
    Raised rather than returning garbage: a capture holding only
    33.6 kBd bearers used to decode to noise and still report a 97%
    yield forecast."""

    def __init__(self, rows):
        self.rows = rows
        super().__init__(
            "no F80T4.5X-8B carrier found among "
            f"{len(rows)} candidate(s)")


# --- front end ---------------------------------------------------------------

def probe_centre(x, sr, centre, sps=4, nframes=6):
    """Cheap decodability probe for one candidate carrier. No turbo decoding.

    Returns (metric, floor, rs_ppm, level, refined_centre). `metric` is the median
    differential-UW correlation over a few frames; `floor` is the same
    statistic at deliberately wrong offsets, which calibrates what noise looks
    like in this capture rather than against a hardcoded number.
    """
    try:
        y, fs, c = recv.channelize(x, sr, sps=sps, centre=centre)
        rs, tau0 = recv.estimate_symbol_clock(y, fs)
        s = symbols_at(y, fs, rs, tau0, np.complex64)
    except Exception:
        return 0.0, 1.0, float("inf"), None, centre
    if not (np.isfinite(rs) and np.isfinite(tau0) and rs > 0):
        return 0.0, 1.0, float("inf"), None, centre
    d = np.empty_like(s)
    d[0] = 0
    d[1:] = s[1:]*np.conj(s[:-1])
    pats = {lv: diff_uw(lv) for lv in spec.LEVELS_F80T45X8B}

    nfr = min(nframes, len(s)//MOS - 1)
    if nfr < 2:
        return 0.0, 1.0, float("inf"), None, c
    hits, floor, lvls = [], [], []
    for f in range(nfr):
        got = _corr(d, 1 + f*MOS, MOS, pats)
        if got:
            hits.append(got[0])
            lvls.append(got[2])
        off = _corr(d, 1 + f*MOS + MOS//3, MOS//8, pats)   # deliberately wrong
        if off:
            floor.append(off[0])
    if not hits:
        return 0.0, 1.0, float("inf"), None, c
    lv = Counter(lvls).most_common(1)[0][0]
    ppm = (rs - spec.F80T45X8B.rs)/spec.F80T45X8B.rs*1e6
    return (float(np.median(hits)), float(np.median(floor) if floor else 1.0),
            float(ppm), lv, float(c))


def pick_carrier(x, sr, sps=4, ncand=4, min_ratio=1.8, max_ppm=50.0,
                 dedupe_hz=8000.0):
    """Choose among candidate carriers by decodability, not by power.

    A capture can hold several carriers, and the strongest need not be the one
    that decodes -- on a two-carrier file the stronger one is noise to this
    decoder while the weaker one decodes cleanly. So probe each and rank by
    how far its unique-word correlation stands above that capture's own noise
    floor.

    `min_ratio` is metric/floor. The floor is measured from deliberately wrong
    offsets in the same capture, so this adapts instead of relying on an
    absolute threshold. Observed: carriers that decode sit at 2.5-3.5x, ones
    that do not sit at 1.0-1.1x.

    `max_ppm` rejects a candidate whose symbol-clock estimate is implausible.
    A real F80T4.5X-8B carrier measures within a couple of ppm; the failures
    came back at -181 and +100 ppm, which is the estimator finding no tone and
    latching onto the edge of its search range.

    Returns (best_centre_or_None, table) where table is one row per candidate.
    """
    cands = recv.find_carriers(x, sr, n=ncand)
    rows = []
    for centre, power in cands:
        m, fl, ppm, lv, refined = probe_centre(x, sr, centre, sps)
        ratio = m/max(fl, 1e-9)
        ok = ratio >= min_ratio and abs(ppm) <= max_ppm
        rows.append(dict(centre=centre, refined=refined, power=power,
                         metric=m, floor=fl, ratio=ratio, ppm=ppm,
                         level=lv, ok=ok))

    # Deduplicate on the REFINED centre, not the raw one. The suppression
    # guard in find_carriers sits one bandwidth from each peak, so a single
    # carrier also throws phantom candidates onto its own shoulders. Those
    # probe almost identically (ratios differing in the 4th decimal) because
    # refine_centre drags them back towards the same carrier -- but only
    # partway, leaving ~1.3 kHz of residual offset. That is far too small to
    # notice in a spectrum and far too large for the pilots: at 1.3 kHz the
    # phase advances ~7.4 rad between pilots, the unwrap in prepare() breaks,
    # and a capture that decodes 944/984 blocks decodes 0.
    groups = []
    for r in sorted(rows, key=lambda r: -r["power"]):
        for g in groups:
            if abs(r["refined"] - g[0]["refined"]) < dedupe_hz:
                g.append(r)
                break
        else:
            groups.append([r])
    uniq = [g[0] for g in groups]          # strongest of each group
    for r in rows:
        r["duplicate"] = r not in uniq

    good = [r for r in uniq if r["ok"]]
    best = max(good, key=lambda r: r["ratio"])["centre"] if good else None
    return best, rows


def channelise(path, secs=None, up=2, sps=4, centre=None, autopick=True):
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

    carriers = None
    if centre is None and autopick:
        centre, carriers = pick_carrier(x, sr, sps)
        if centre is None:
            raise NoCarrier(carriers)

    y, fs, centre = recv.channelize(x, sr, sps=sps, centre=centre)
    rs_est, tau0 = recv.estimate_symbol_clock(y, fs)
    info = dict(raw_sr=raw_sr, raw_n=raw_n, secs=raw_n/raw_sr, sr=sr,
                carriers=carriers,
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


def safe_stem(path, maxlen=64):
    """Filesystem-safe stem of a capture name, for naming outputs after it.

    Long names are shortened from the middle: the head says what the
    recording is, the tail keeps the frequency and timestamp that actually
    distinguish one capture from another.
    """
    stem = Path(path).stem if path else "capture"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if len(stem) > maxlen:
        head = maxlen//3
        tail = maxlen - head - 1
        stem = (stem[:head].rstrip("._-") + "_"
                + stem[-tail:].lstrip("._-"))
    return stem or "capture"


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


def survey(path, secs=None, ntau=TAU_STEPS, progress=None):
    """Fast scan: what is on this carrier and where, without decoding.

    Runs the front end and the timing/framing search but no turbo decoding,
    which is ~90% of a full decode. Reports the unique words present, where
    the framing sits, how the timing phase moves, and a yield forecast, so a
    long capture can be triaged before committing to it.

    The forecast comes from the UW correlation metric, which predicts yield
    well: on BGAN19, runs of frames at metric >=70 decoded 93-99% of blocks
    while runs at 31-44 decoded 0-27%.

    It is deliberately conservative and is a lower bound, not a prediction.
    Calibration point: on BGAN19 it forecast 92% and the full decode returned
    98.5%. Treat it as "at least this much", and re-check it against a real
    decode before trusting it on a link that behaves differently.

    Cost: ~16 s for a 39 s capture, against ~145 s to decode the same file,
    so roughly a ninth of the price of finding out the hard way.

    Returns a dict.
    """
    y, fs, rs_est, tau0, info = channelise(path, secs)
    if progress:
        progress(0.5, "scanning framing and timing")
    tau_idx, offs, lvls, mets = survey_taus(y, fs, rs_est, tau0, ntau)
    s = symbols_at(y, fs, rs_est, tau0)
    m4 = recv.timing_quality(s)
    info.update(m4=m4, esn0=recv.esn0_from_m4(m4), nframes=len(offs))

    rel = offs - np.arange(len(offs))*MOS
    steps = np.flatnonzero(np.diff(rel) | np.diff(tau_idx)) + 1
    runs = []
    for b, e in zip([0] + list(steps), list(steps) + [len(rel)]):
        runs.append(dict(start=int(b), end=int(e - 1), n=int(e - b),
                         offset=int(rel[b]), tau=int(tau_idx[b]),
                         level=lvls[b], metric=float(np.median(mets[b:e]))))
    strong = float(np.percentile(mets, 90)) if len(mets) else 0.0
    # Calibrate against what a WRONG offset scores in this same capture.
    # Using a fraction of the file's own p90 was wrong: on a capture with
    # no usable carrier every frame scores ~20, so 100% of frames came out
    # "strong" and the forecast said 97% for a file that decodes nothing.
    d2 = np.empty_like(s)
    d2[0] = 0
    d2[1:] = s[1:]*np.conj(s[:-1])
    pats2 = {lv: diff_uw(lv) for lv in spec.LEVELS_F80T45X8B}
    fl = [ _corr(d2, 1 + f*MOS + MOS//3, MOS//8, pats2)
           for f in range(min(20, len(offs))) ]
    floor = float(np.median([g[0] for g in fl if g])) if any(fl) else 1.0
    good = float(np.mean(mets > 1.8*floor)) if len(mets) else 0.0
    info.update(uw_levels=dict(Counter(x for x in lvls if x)),
                tau_hist=np.bincount(tau_idx, minlength=ntau).tolist(),
                runs=runs, metric_med=float(np.median(mets)),
                metric_p90=strong, metric_floor=floor,
                frac_strong=good,
                est_yield=good*0.97)
    return info, (tau_idx, offs, lvls, mets)


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
    pred = LevelPredictor(uw_level=lvls[0] if len(lvls) else None)
    for k in range(ntau):
        which = np.flatnonzero(tau_idx == k)
        if not len(which):
            continue
        s = symbols_at(y, fs, rs_est, tau0 + (k/ntau)*period)
        if k == 0:
            m4 = recv.timing_quality(s)
            info.update(m4=m4, esn0=recv.esn0_from_m4(m4))
        for f in which:
            hints = None
            for b in range(8):
                blk = prepare(s, int(offs[f]), b)
                if blk is None:
                    continue
                if search_levels and not level:
                    bits, lv, ag = decode_block_fast(
                        blk, b, pred, thr,
                        hints[b] if hints else None)
                else:
                    bits, ag, _ = decode_block(blk, level or lvls[f])
                    lv = (level or lvls[f]) if ag > thr else None
                if lv is not None:
                    pay = mod.descramble(bits)
                    recs.append((int(f), b, lv, ag, pay))
                    if b == 0:
                        # block 0 carries this frame's code-rate AVP,
                        # so blocks 1-7 need not be searched blind
                        hints = levels_from_block0(pay, lv)
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


class LevelPredictor:
    """Orders the coding levels to try, cheapest-first, and learns as it goes.

    Only block 0's level is signalled by the unique word. Blocks 1-7 carry
    theirs in a ForwardBearerCodeRateParam AVP, and on this bearer that AVP
    applies to the *current frame only* -- measured: predicting a frame's
    levels from the most recent BulletinBoard is 100% right on the
    BulletinBoard's own frame and ~50% one frame later, and every consecutive
    BulletinBoard differs. So the levels cannot simply be carried forward.

    They are far from uniform, though. Measured over 3868 blocks:

        b0: L3 95%                      b4: H4 42%  H3 17%  L3 16%
        b1: R  44%  H4 24%  H1 14%      b5: H4 46%  L3 21%  H3 15%
        b2: H4 38%  R  18%  H3 11%      b6: H4 57%  L3 23%
        b3: H4 36%  H3 20%  H1 12%      b7: H4 65%  L3 23%

    and the same block index keeps its level between consecutive frames 61.8%
    of the time. Trying [last seen for this index] then [most frequent for
    this index] and stopping at the first success needs 1.93 trial decodes per
    block against 10 today -- expected trials per block:

        fixed order, try all ten (before)             10
        fixed order, stop at first success          5.37
        per-block-index frequency order             2.16
        previous frame, then per-index frequency    1.93

    Counts are learned from this capture rather than baked in, so a link with
    a different traffic mix tunes itself instead of being mispredicted.
    """

    def __init__(self, levels=None, uw_level=None):
        self.levels = list(levels or spec.LEVELS_F80T45X8B)
        self.seen = {b: Counter() for b in range(8)}
        self.last = {}
        if uw_level:
            self.last[0] = uw_level          # the UW tells us block 0 free

    def order(self, b, hint=None):
        out = []
        if hint:
            out.append(hint)
        p = self.last.get(b)
        if p and p not in out:
            out.append(p)
        for lv, _ in self.seen[b].most_common():
            if lv not in out:
                out.append(lv)
        for lv in self.levels:
            if lv not in out:
                out.append(lv)
        return out

    def note(self, b, lv):
        self.seen[b][lv] += 1
        self.last[b] = lv


BCTPDU_HDR_BITS = 24        # first BCtSDU starts here; measured, see below


def levels_from_block0(payload_bits, uw_level="L3", nblocks=8):
    """Read this frame's per-block coding levels out of block 0. None if absent.

    The first BCtSDU sits at bit 24 of the block-0 payload, immediately after
    a 3-octet FwdBCtPDUHeader. On BGAN19 that header is 0xc9 in all 490
    payloads, which decodes exactly as clause 5.1.4 says it should:
    bct-sdu-follows=1, length-present=1, bct-pdu-addr-type=broadcast -- which
    is precisely what clause 5.4.3.0 requires of the PDU carrying a
    BulletinBoard.

    On frames that are not carrying a BulletinBoard, that first SDU is the
    ForwardBearerCodeRateParam itself, so the levels can simply be read.

    This is validated by prediction rather than by assertion, which matters
    because an earlier blind scan for the same tag matched 98/98 frames on
    noise. Scored against 3868 levels established independently by trial
    decode, the AVP at bit 24 predicts **3387/3409 = 99.35%**, against 23.8%
    for assuming L3 everywhere. No other bit offset or byte position comes
    close: the next best manages 38%.

    Returns the level list, or None when byte 3 is not a code-rate tag (which
    is the BulletinBoard case, plus a handful of frames with neither).
    """
    b = np.asarray(payload_bits, dtype=np.uint8)
    p = BCTPDU_HDR_BITS//8
    by = np.packbits(b[:len(b)//8*8]).tobytes()
    if p >= len(by):
        return None
    tag = by[p]
    if not (0x90 <= tag <= 0x97):          # not fwd-bearer-code-rate-len-N
        return None
    n = (tag & 7) + 1
    if p + 1 + n > len(by):
        return None
    entries = bctrl.parse_fwd_code_rate(by[p:p + 1 + n])
    if not entries:
        return None
    return bctrl.resolve_block_levels(uw_level, entries, nblocks)


def decode_block_fast(blk, b, pred, thr=0.90, hint=None):
    """Trial-decode in predicted order, stopping at the first accepted level.

    Early stopping is safe here for a specific measured reason: across ~480
    block-tries at 10 levels each, no block ever passed at two levels. There
    is no second candidate to lose by stopping. The likelihood-ratio check is
    required as well as the parity threshold, so a stop needs two independent
    reasons to believe the level is right.

    Returns (bits, level, agreement) or (None, None, 0.0).
    """
    for lv in pred.order(b, hint):
        bits, ag, lr = decode_block(blk, lv)
        if ag > thr and lr:
            pred.note(b, lv)
            return bits, lv, ag
    return None, None, 0.0


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


def report_no_carrier(path, rows, secs=None):
    """Explain what was found instead, rather than just failing."""
    print("  No F80T4.5X-8B carrier found. Candidates probed:")
    for r in rows:
        why = []
        if r["ratio"] < 1.8:
            why.append(f"UW correlation {r['ratio']:.2f}x the noise floor")
        if abs(r["ppm"]) > 50:
            why.append(f"symbol clock {r['ppm']:+.0f} ppm off nominal")
        print(f"    {r['centre']/1e3:+9.1f} kHz  " +
              ("; ".join(why) if why else "accepted"))
    try:
        from scan_bearers import detect_carriers, identify
        x, sr = recv.load_wav_iq(path, secs=min(secs or 20, 20))
        x = x - x.mean()
        print(NL_ + "  What this capture actually contains:")
        for c in detect_carriers(x, sr):
            best, _, m4 = identify(x, sr, c)
            if best:
                print(f"    {c['centre']/1e3:+9.1f} kHz  bw {c['bw']/1e3:5.1f} kHz  "
                      f"-> {best[0]/1e3:.1f} kBd  {best[1]}")
        print(NL_ + "  This decoder handles F80T4.5X-8B (151.2 kBd) only.")
    except Exception as exc:
        print(f"  (bearer scan unavailable: {exc})")


# --- driver ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--secs", type=float, default=None)
    ap.add_argument("--thr", type=float, default=0.90)
    ap.add_argument("--level", default=None, help="force coding level")
    ap.add_argument("--survey", action="store_true",
                    help="fast scan only: unique words, framing, timing and a "
                         "yield forecast, with no turbo decoding")
    ap.add_argument("--ntau", type=int, default=TAU_STEPS,
                    help="timing phases to search per frame (1 = old "
                         "single-phase behaviour)")
    ap.add_argument("--no-search-levels", dest="search_levels",
                    action="store_false",
                    help="use the UW level for all 8 blocks (much faster, "
                         "but blocks 1-7 mostly will not decode)")
    ap.add_argument("--out", default=None,
                    help="payload path; defaults to work/<capture name>_payload.bin")
    a = ap.parse_args()

    def prog(frac, text, const=None):
        print(f"\r  [{100*frac:5.1f}%] {text}          ", end="", flush=True)

    if a.survey:
        import time
        t0 = time.time()
        try:
            info, (tau_idx, offs, lvls, mets) = survey(
                a.path, secs=a.secs, ntau=a.ntau, progress=prog)
        except NoCarrier as exc:
            print()
            report_no_carrier(a.path, exc.rows, a.secs)
            return 2
        el = time.time() - t0
        print()
        print(f"{Path(a.path).name}: {info['secs']:.1f} s @ "
              f"{info['raw_sr']/1e3:.0f} kHz")
        print(f"  centre {info['centre']:+.1f} Hz   rs {info['rs']:.3f} Bd "
              f"({info['ppm']:+.2f} ppm)")
        print(f"  M4/M2^2 {info['m4']:.3f} -> Es/N0 {info['esn0']:.1f} dB   "
              f"{info['nframes']} frames")
        print("  unique words seen: " +
              ", ".join(f"{k} x{v}" for k, v in
                        sorted(info["uw_levels"].items(),
                               key=lambda kv: -kv[1])))
        print(f"  timing phases (of {a.ntau}): {info['tau_hist']}")
        print(f"  UW metric: median {info['metric_med']:.1f}, "
              f"p90 {info['metric_p90']:.1f}; "
              f"{100*info['frac_strong']:.0f}% of frames strong")
        print(f"  forecast: at least ~{100*info['est_yield']:.0f}% of blocks "
              f"should decode ({int(info['est_yield']*info['nframes']*8)} of "
              f"{info['nframes']*8}) - conservative; on BGAN19 this said 92% "
              f"and the decode gave 98.5%")
        print(f"  scan took {el:.1f} s; full decode roughly {el*8:.0f} s")
        print(f"\n  {len(info['runs'])} framing run(s):")
        for r in info["runs"][:30]:
            print(f"    frames {r['start']:4d}..{r['end']:<4d} ({r['n']:3d})  "
                  f"offset {r['offset']:6d}  tau {r['tau']}/{a.ntau}  "
                  f"UW {r['level']:>3}  metric {r['metric']:5.1f}")
        if len(info["runs"]) > 30:
            print(f"    ... {len(info['runs'])-30} more")
        return 0

    try:
        recs, info, (tau_idx, offs, lvls, mets) = decode_capture(
            a.path, secs=a.secs, thr=a.thr, level=a.level,
            search_levels=a.search_levels, ntau=a.ntau, progress=prog)
    except NoCarrier as exc:
        print()
        report_no_carrier(a.path, exc.rows, a.secs)
        return 2
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
        p = Path(a.out) if a.out else (
            Path("work")/f"{safe_stem(a.path)}_payload.bin")
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
