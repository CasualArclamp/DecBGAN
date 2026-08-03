"""Residual carrier-offset estimation from the unique words.

The problem this solves. `refine_centre` locates a carrier by the centroid
of its power spectrum. That is unbiased only if the spectrum is symmetric.
On captures whose spectrum is domed or tilted the centroid lands off the
true carrier, and the error survives channelisation as a residual frequency
offset of several hundred Hz to a couple of kHz.

Several hundred Hz is small enough to be invisible in every check the
receiver was making, and large enough to be fatal:

  * The differential UW correlation compares adjacent symbols, 6.6 us
    apart. At 857 Hz that is 0.036 rad, so framing and timing look perfect.
  * M4/M2^2, the |x|^2 timing tone and the PSD median are all blind to it.
  * prepare() fits phase across the pilots, which are 137 symbols = 906 us
    apart. At 857 Hz the phase advances 4.9 rad between consecutive pilots.
    That is past pi, so np.unwrap steps the wrong way and the fit -- and
    with it every block in the frame -- is destroyed.

The unwrap limit puts the cliff at 1/(2*906us) = 552 Hz. Below it a capture
decodes normally; above it, nothing decodes at all, which is why these
captures failed so completely rather than degrading.

Measured residuals, and what removing them recovers over 8 s:

    capture        ripple   residual      EVM         blocks
    1543.100b      1.2 dB    +176 Hz   0.192->0.170   64 -> 64
    1543.100a      3.5 dB    -857 Hz   0.456->0.142    0 -> 60
    1553.500       4.9 dB   -1625 Hz   1.020->0.331    0 ->  0

Spectral ripple and residual offset travel together -- a lopsided spectrum
is what biases the centroid -- which is why flattening the magnitude
response appeared to fix 1543.100a. It was not equalising anything; it was
symmetrising the spectrum so the centroid landed in the right place. A
33- to 513-tap least-squares equaliser trained on the same unique words
gives no further improvement on any capture tested, and costs 1547.298 all
six of its blocks. Do not reach for one before ruling this out.
"""
from __future__ import annotations

import numpy as np

from . import mod, spec
from .recv import _cubic, symbol_phase

UWLEN = spec.F80T45X8B.uw_syms
DEFAULT_SPAN_HZ = 6000.0
MIN_FRAMES = 8


def uw_observations(y, fs, rs_est, tau0, ntau, tau_idx, offs, lvls,
                    maxframes=400):
    """Received and known unique-word symbols, one row per frame.

    Returns (V, A) of shape (nframes, 40): V measured, A the symbols that
    should be there. Rows whose frame has no identified UW level, or which
    fall off either end of the capture, are dropped.
    """
    period = fs/rs_est
    nfr = len(offs)
    which = np.arange(nfr)
    if nfr > maxframes:
        which = np.unique(np.linspace(0, nfr - 1, maxframes).astype(int))

    # Where symbol 0 of each timing phase actually lands. Not rederived as
    # tau0 + j*period: extract_symbols drops the first instant when the
    # phase puts it at or below sample 1, and assuming otherwise is one
    # symbol out on roughly half of all captures.
    phase = {k: symbol_phase(y, fs, rs_est, tau0 + (k/ntau)*period)
             for k in np.unique(np.asarray(tau_idx)[which])}
    known = {lv: mod.uw_symbols(lv) for lv in set(lvls) if lv}

    V, A = [], []
    for f in which:
        lv = lvls[f]
        if lv is None:
            continue
        first, per = phase[tau_idx[f]]
        i = int(offs[f])
        if i < 0:
            continue
        pos = tau0 + (tau_idx[f]/ntau)*per + (i + first + np.arange(UWLEN))*per
        if pos[0] < 1.0 or pos[-1] >= len(y) - 3.0:
            continue
        i0 = np.floor(pos).astype(np.int64)
        v = _cubic(y, i0, pos - i0)
        g = np.sqrt(np.mean(np.abs(v)**2))
        if not np.isfinite(g) or g <= 0:
            continue
        V.append(v/g)
        A.append(known[lv])
    if not V:
        return np.zeros((0, UWLEN), complex), np.zeros((0, UWLEN), complex)
    return np.array(V), np.array(A)


def uw_evm(V, A):
    """Median per-frame EVM of the unique word, best complex scalar removed.

    The scalar goes because gain and carrier *phase* are not a defect --
    prepare() normalises both per block downstream. A phase *ramp* survives
    it, which is precisely what makes this a useful detector of the offset.
    """
    if not len(V):
        return float("nan")
    b = np.sum(np.conj(A)*V, axis=1)/np.sum(np.abs(A)**2, axis=1)
    num = np.linalg.norm(V - b[:, None]*A, axis=1)
    den = np.maximum(np.abs(b)*np.linalg.norm(A, axis=1), 1e-30)
    return float(np.median(num/den))


def estimate_cfo(V, A, rs, span_hz=DEFAULT_SPAN_HZ, nfft=8192):
    """Common carrier offset across all frames, in Hz. Returns (hz, quality).

    Each frame carries its own unknown carrier phase, so the estimator is
    coherent *within* a frame and non-coherent *across* frames -- the ML
    form for exactly that nuisance structure. Concretely: take the
    periodogram of v*conj(uw) per frame and sum the magnitudes.

    This replaced a per-frame Kay estimator plus a median. Kay is fine at
    high SNR but the unique word is only 40 symbols, so on a marginal
    capture individual frames scatter wildly -- 1553.500 gave a standard
    deviation of 15.2 kHz about its own median. Summing periodograms puts
    every frame's evidence into one peak instead of trying to average
    numbers that are individually meaningless.

    `quality` is the peak-to-median ratio of the summed periodogram. It read
    15-17 on every capture tested, including the ones where the estimate was
    poor, so it does NOT discriminate a good estimate from a bad one and is
    reported for diagnosis only. What gates the correction is the caller's
    own before/after unique-word EVM check.
    """
    if len(V) < MIN_FRAMES:
        return 0.0, 0.0
    Z = np.fft.fft(V*np.conj(A), nfft, axis=1)
    P = np.sum(np.abs(Z)**2, axis=0)
    f = np.fft.fftfreq(nfft, 1/rs)
    band = np.flatnonzero(np.abs(f) <= span_hz)
    if len(band) < 3:
        return 0.0, 0.0
    j = int(band[np.argmax(P[band])])

    # Parabolic interpolation on the log periodogram, so the estimate is not
    # quantised to the 18 Hz bin grid.
    lo, hi = (j - 1) % nfft, (j + 1) % nfft
    a, b, c = (np.log(max(P[k], 1e-300)) for k in (lo, j, hi))
    denom = a - 2*b + c
    d = 0.5*(a - c)/denom if abs(denom) > 1e-12 else 0.0
    d = float(np.clip(d, -0.5, 0.5))
    hz = float(f[j] + d*rs/nfft)
    return hz, float(P[j]/max(np.median(P[band]), 1e-300))


def derotate(y, fs, hz):
    """Remove a constant frequency offset from the channelised stream."""
    if not hz:
        return y
    n = np.arange(len(y), dtype=np.float64)
    return y*np.exp(-2j*np.pi*hz*n/fs)


def band_ripple(x, sr, rs=None, bw_factor=1.0, nperseg=4096, smooth=9):
    """Peak-to-peak magnitude ripple across the occupied band, in dB.

    A diagnostic only -- ripple is a *correlate* of the offset above, not
    the cause of the failure. Keep nperseg modest: at 65536 an 8 s capture
    gives only ~60 averages and the estimator's own variance is about the
    size of the effect, which is how 11-14 dB of imaginary in-band spurs
    once got reported.
    """
    from scipy.signal import welch
    rs = rs or spec.F80T45X8B.rs
    fr, P = welch(x, sr, nperseg=nperseg, return_onesided=False,
                  detrend=False)
    i = np.argsort(fr)
    fr, P = fr[i], P[i]
    if smooth > 1:
        P = np.convolve(P, np.ones(smooth)/smooth, "same")
    b = np.abs(fr) < rs*bw_factor/2
    if b.sum() < 8:
        return float("nan")
    d = 10*np.log10(np.maximum(P[b], 1e-30))
    return float(np.percentile(d, 95) - np.percentile(d, 5))
