"""End-to-end receive pipeline: IQ in, decoded payload bits out."""
from __future__ import annotations
from dataclasses import dataclass, field

import numpy as np

from . import spec, mod, recv, tx, bctrl
from . import decoder as dec


@dataclass
class FrameResult:
    index: int
    level: str
    blocks_ok: list
    payloads: list
    esn0_db: float = float("nan")

    @property
    def n_ok(self):
        return sum(self.blocks_ok)


@dataclass
class SyncInfo:
    centre_hz: float
    rs_est: float
    tau0: float
    offset: int
    level: str
    psr: float
    cfo_hz: float
    esn0_db: float
    m4: float
    nframes: int


def synchronise(x, sr, sps=4, centre=None, bearer=spec.F80T45X8B):
    """Channelise, recover timing, find framing, refine carrier.

    Returns (symbols, SyncInfo). The symbols are CFO-corrected and normalised
    to unit mean power, but not yet phase-tracked.
    """
    y, fs, c = recv.channelize(x, sr, sps=sps, centre=centre)
    rs_est, tau0 = recv.estimate_symbol_clock(y, fs)
    s = recv.extract_symbols(y, fs, rs_est, tau0)
    s = s/np.sqrt(np.mean(np.abs(s)**2))

    m4 = recv.timing_quality(s)
    esn0 = recv.esn0_from_m4(m4)

    # Blind carrier recovery BEFORE frame sync. Pilot-aided CFO needs framing
    # and framing needs a usable constellation, so doing it the other way
    # round is circular.
    s = recv.blind_carrier_recovery(s)

    off, level, psr, _ = recv.frame_sync(s, bearer)

    # Pilot-aided CFO is now only a *residual check*, not a correction: blind
    # recovery has already removed the offset, so applying this on top would
    # re-spin the constellation. On a capture with no valid pilots it returns
    # noise, which is exactly how that bug showed up (gridness 0.45 -> 0.001).
    try:
        cfo = recv.fine_cfo_from_pilots(s, off, level, bearer)
    except Exception:
        cfo = float("nan")

    nfr = (len(s)-off)//bearer.mos
    return s, SyncInfo(c, rs_est, tau0, off, level, psr, cfo, esn0, m4, nfr)


def decode_frame(sym, info, index, level=None, niter=8, n0=None,
                 bearer=spec.F80T45X8B, levels=None, code_rate_avp=None):
    """Decode one frame. Returns FrameResult.

    Per-block coding levels follow TS 102 744-3-1 clause 5.7.16: with no
    ForwardBearerCodeRateParam AVP, every block uses the UW-signalled level,
    because on bearers without outer interleaving (this one) the AVP is sent
    only when the rate changes. Pass `code_rate_avp` (parsed by
    bgan.bctrl.parse_fwd_code_rate) when one has been recovered.

    `level` forces a single level for every block; `levels` sets them
    explicitly. Both are for testing.
    """
    base = info.offset + index*bearer.mos
    frame = sym[base:base+bearer.mos]
    if len(frame) < bearer.mos:
        raise ValueError("incomplete frame")

    uw_level = info.level
    frame = recv.correct_phase(frame, bearer, uw_level=uw_level)

    _, _, datamask = mod.frame_layout(bearer)
    data = frame[datamask]
    if n0 is None:
        e = info.esn0_db
        if not np.isfinite(e):
            e = 20.0        # never let a bad estimate poison every LLR
        n0 = 10**(-e/10)

    if levels is None:
        levels = ([level]*bearer.nblocks if level else
                  bctrl.resolve_block_levels(uw_level, code_rate_avp,
                                             bearer.nblocks))
    lv = levels
    ok, payloads = [], []
    for b in range(bearer.nblocks):
        blk = data[b*bearer.teo:(b+1)*bearer.teo]
        t = tx.tables(lv[b])
        llr = dec.soft_demap(blk, n0)
        Ls, Lp, Lq = dec.deinterleave_llrs(llr, t.cipm)
        bits, _ = dec.turbo_decode(Ls, Lp, Lq, t.perm.astype(np.int64),
                                   niter, dec.NXT, dec.PAR)
        good = verify_block(bits, t, llr)
        ok.append(good)
        payloads.append(mod.descramble(bits[:t.D]) if good else None)
    return FrameResult(index, lv[0], ok, payloads)


def verify_block(bits, t, slot_llr, margin_nats=1200.0):
    """Re-encode check via a likelihood ratio. Self-calibrating, no tuned threshold.

    A turbo decoder always outputs *something*, so a decode is only
    trustworthy if re-encoding it reproduces what the channel actually saw.
    There is no CRC at this layer, so this is the only available test.

    Compare two hypotheses for the observed pattern of disagreements between
    the re-encoded channel bits and the demapper's hard decisions:

      H_ok   : the decode is right, so bit i disagrees with probability
               p_i = 1/(1+exp|LLR_i|), the demapper's own implied error rate
      H_rand : the decode is wrong, so each bit disagrees with probability 1/2

    Accept when log P(observed | H_ok) - log P(observed | H_rand) > margin.

    Why not a z-score against the expected disagreement count, which is the
    obvious thing to do: its bias moves with SNR because the LLR scaling
    depends on the n0 estimate. Measured on the reference captures, correct
    decodes sat at z median +1.53 (max 6.03) at 12 dB but z median -5.42
    (max -3.36) at 4 dB, while wrong decodes at 4 dB started at z 5.62. No
    single z threshold separates both: 4.0 rejects good blocks at 12 dB, 6.0
    admits bad ones at 4 dB. The likelihood ratio has no such drift.

    And a *fixed* agreement threshold is worse still -- that is the knob that
    gets loosened when it starts rejecting good frames, and loosening it
    manufactures false positives rather than recovering data.

    margin_nats was chosen against the labelled reference set from
    tools/make_test_iq.py (5 captures, 4960 blocks, ground truth known):

        margin   accepted   bit-exact   false positives
           600       4152        4150                 2
          1200       4150        4150                 0
          2500       3968        3968                 0

    1200 keeps every correct block and rejects every incorrect one. It is
    tuned, but tuned against known ground truth and re-checkable at any time,
    which is the difference between a calibrated threshold and a fudged one.
    Re-run the reference sweep before changing it.
    """
    from .turbo import turbo_encode, map_to_symbols
    from scipy.special import expit

    D = t.D
    df2, p2, q2 = turbo_encode(bits[:D], t.perm)
    if not np.array_equal(df2, bits):
        return False          # decoded flush bits inconsistent with the data

    slots2 = map_to_symbols(df2, p2, q2, t.cipm)
    hard = (slot_llr < 0).astype(np.uint8)
    dis = (slots2 != hard)

    a = np.abs(slot_llr).astype(np.float64)
    p = np.clip(expit(-a), 1e-12, 1-1e-12)     # implied per-bit error prob
    ll_ok = float(np.sum(np.where(dis, np.log(p), np.log1p(-p))))
    ll_rand = slot_llr.size*np.log(0.5)
    return bool(ll_ok - ll_rand > margin_nats)


