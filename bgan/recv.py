"""Receiver front end: channelisation, timing, frame sync, carrier recovery.

Every stage here is validated against bgan.tx output at known impairments
before it is pointed at real IQ, so that a failure can be attributed to the
receiver rather than to the signal.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly, lfilter, welch

from . import spec, mod
from .tx import rrc


# --- IQ loading --------------------------------------------------------------

WAVE_PCM = 0x0001
WAVE_FLOAT = 0x0003
WAVE_EXTENSIBLE = 0xFFFE


@dataclass
class WavInfo:
    """What the fmt/data chunks say, without reading any samples."""
    sr: int
    channels: int
    bits: int
    is_float: bool
    data_off: int
    data_len: int

    @property
    def frame_bytes(self):
        return self.channels*self.bits//8

    @property
    def frames(self):
        return self.data_len//self.frame_bytes if self.frame_bytes else 0

    @property
    def secs(self):
        return self.frames/self.sr if self.sr else 0.0

    @property
    def format_name(self):
        return f"{self.bits}-bit {'float' if self.is_float else 'int'}"


def wav_info(path):
    """Parse a WAV header. Returns WavInfo; raises ValueError if unusable.

    Seeks chunk by chunk rather than parsing a fixed prefix of the file. SDR
    recorders routinely put a large chunk before `data` -- SDR# and SDRuno
    write an `auxi` chunk carrying centre frequency and a timestamp -- and a
    prefix-based walk silently fails to find `data` past it.

    WAVE_FORMAT_EXTENSIBLE (0xFFFE) is resolved through its SubFormat GUID,
    whose first two octets carry the real format tag. Recorders emit it
    routinely for anything that is not plain 16-bit stereo, so treating 0xFFFE
    as unknown would reject most float captures.
    """
    with open(path, "rb") as f:
        if f.read(4) != b"RIFF":
            raise ValueError("not a RIFF file")
        f.seek(4, 1)
        if f.read(4) != b"WAVE":
            raise ValueError("not a WAVE file")

        sr = ch = bits = None
        tag = None
        data_off = data_len = None
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            cid, sz = head[:4], struct.unpack("<I", head[4:8])[0]
            if cid == b"fmt ":
                b = f.read(sz)
                if len(b) < 16:
                    raise ValueError("truncated fmt chunk")
                tag, ch, sr, _, _, bits = struct.unpack("<HHIIHH", b[:16])
                if tag == WAVE_EXTENSIBLE:
                    if len(b) < 26:
                        raise ValueError("truncated WAVE_FORMAT_EXTENSIBLE")
                    tag = struct.unpack("<H", b[24:26])[0]
                if sz & 1:
                    f.seek(1, 1)
            elif cid == b"data":
                data_off = f.tell()
                # A recorder killed mid-capture leaves the size field at 0 or
                # unwritten, so trust the file for the real extent.
                end = Path(path).stat().st_size
                data_len = min(sz, end - data_off) if sz else end - data_off
                break
            else:
                f.seek(sz + (sz & 1), 1)

    if sr is None or data_off is None:
        raise ValueError("no fmt/data chunk found")
    if ch != 2:
        raise ValueError(f"expected 2 channels (I/Q), got {ch}")
    if tag not in (WAVE_PCM, WAVE_FLOAT):
        raise ValueError(f"unsupported WAV format tag 0x{tag:04x} "
                         "(want PCM or IEEE float)")
    is_float = tag == WAVE_FLOAT
    if is_float and bits not in (32, 64):
        raise ValueError(f"float WAV must be 32 or 64-bit, got {bits}")
    if not is_float and bits not in (8, 16, 24, 32):
        raise ValueError(f"PCM WAV must be 8/16/24/32-bit, got {bits}")
    return WavInfo(int(sr), int(ch), int(bits), is_float,
                   int(data_off), int(data_len))


def _decode_samples(buf, info):
    """Raw interleaved bytes -> float32 samples scaled to +/-1.0.

    Bit depth is a *format* conversion, not a resampling one: the sample
    instants are unchanged, only how each sample is spelled. Float WAV is
    already normalised to +/-1.0, so it is scaled by 1; integer PCM divides by
    its full scale. 8-bit PCM is the odd one out in the WAV spec -- unsigned,
    biased by 128 -- and reading it as signed puts a large DC step in the data.
    """
    if info.is_float:
        d = np.frombuffer(buf, dtype="<f4" if info.bits == 32 else "<f8")
        return d.astype(np.float32)
    if info.bits == 8:
        d = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
        return (d - 128.0)/128.0
    if info.bits == 16:
        return np.frombuffer(buf, dtype="<i2").astype(np.float32)/32768.0
    if info.bits == 24:
        # No native 24-bit dtype: widen three little-endian octets each, then
        # sign-extend by folding everything at or above 2^23 down a full turn.
        b = np.frombuffer(buf, dtype=np.uint8)
        b = b[:(len(b)//3)*3].reshape(-1, 3).astype(np.int32)
        d = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        d = np.where(d >= 1 << 23, d - (1 << 24), d)
        return d.astype(np.float32)/float(1 << 23)
    return np.frombuffer(buf, dtype="<i4").astype(np.float32)/float(1 << 31)


def load_wav_iq(path, secs=None, offset=0.0):
    """Load interleaved IQ from a WAV file. Returns (x, sample_rate).

    Accepts 8/16/24/32-bit PCM and 32/64-bit float, plain or
    WAVE_FORMAT_EXTENSIBLE. Whatever comes in, complex64 scaled to +/-1.0
    comes out, so nothing downstream sees the difference.
    """
    info = wav_info(path)
    fb = info.frame_bytes
    with open(path, "rb") as f:
        start = int(offset*info.sr)*fb
        f.seek(info.data_off + start)
        want = int(info.sr*secs)*fb if secs else info.data_len - start
        want = max(0, min(want, info.data_len - start))
        # Never hand a partial frame to the decoders below.
        buf = f.read(want - want % fb)
    d = _decode_samples(buf, info)
    if len(d) % 2:
        d = d[:-1]
    return (d[0::2] + 1j*d[1::2]).astype(np.complex64), info.sr


# --- carrier location --------------------------------------------------------

def find_carriers(x, sr, rs=None, bw_factor=1.25, n=4, sep_factor=1.0):
    """Candidate carrier centres, strongest first, separated by `sep_factor*bw`.

    Returns a list of (centre_hz, integrated_power). Power ranks them, but
    power is NOT a reliable way to choose between them: a capture can hold
    several carriers, and the strongest is not always the one that decodes.
    Measured on a two-carrier file, the stronger carrier (+103.4 kHz, 1.39x
    the in-band power) yields a UW metric of 20 -- pure noise -- while the
    weaker one at -98.5 kHz decodes cleanly at metric 60. Let the caller
    probe each candidate and pick on evidence; see decode_wav.pick_carrier.

    sep_factor must be >= 1: at 0.6 the suppression guard was narrower
    than the signal, so each real carrier also produced phantom
    candidates on its own shoulders at +/-113 kHz, which probe
    identically because refine_centre pulls them back to the same
    carrier anyway.
    """
    rs = rs or spec.F80T45X8B.rs
    bw = rs*bw_factor
    fr, P = welch(x, sr, nperseg=32768, return_onesided=False, detrend=False)
    i = np.argsort(fr)
    fr, P = fr[i], P[i]
    Ps = np.convolve(P, np.ones(64)/64, "same")
    n0 = np.median(np.sort(Ps)[:len(Ps)//5])
    win = max(1, int(bw/(fr[1]-fr[0])))
    integ = np.convolve(np.maximum(Ps - n0, 0), np.ones(win), "same")

    out = []
    guard = int(sep_factor*win)
    work = integ.copy()
    for _ in range(n):
        j = int(np.argmax(work))
        if work[j] <= 0:
            break
        out.append((float(fr[j]), float(work[j])))
        work[max(0, j - guard):j + guard + 1] = -1.0
    return out


def find_carrier(x, sr, rs=None, bw_factor=1.25):
    """Locate the strongest carrier and return its centre offset in Hz.

    Strongest only. Prefer find_carriers() plus a decodability probe when the
    capture may hold more than one carrier.
    """
    c = find_carriers(x, sr, rs, bw_factor, n=1)
    return c[0][0] if c else 0.0


def subprogress(progress, lo, hi):
    """Map a callee's 0..1 progress into the [lo, hi] slice of the caller's.

    A long stage reports its own progress from 0 to 1 without knowing where it
    sits in the whole decode; this places it. Returns None when there is no
    callback, so callers can pass the result straight down.
    """
    if progress is None:
        return None

    def g(frac, text, *a, **kw):
        progress(lo + (hi - lo)*min(max(frac, 0.0), 1.0), text, *a, **kw)
    return g


def refine_centre(x, sr, rs=None, iters=3, progress=None):
    """Refine a near-centred carrier to its spectral centroid."""
    rs = rs or spec.F80T45X8B.rs
    total = 0.0
    n = np.arange(len(x))
    for it in range(iters):
        if progress:
            progress(it/iters, f"refining carrier centre {it+1}/{iters}")
        fr, P = welch(x, sr, nperseg=32768, return_onesided=False, detrend=False)
        i = np.argsort(fr)
        fr, P = fr[i], P[i]
        b = np.abs(fr) < rs*0.75
        n0 = np.median(P[(np.abs(fr) > rs*0.72) & (np.abs(fr) < rs*0.95)])
        w = np.maximum(P[b]-n0, 0)
        tot = float(np.sum(w))
        if not np.isfinite(tot) or tot <= 0:
            # Nothing above the noise floor in this band, so there is no
            # centroid to move to. Returning NaN here poisons every later
            # stage silently -- it only surfaced once carrier candidates
            # started being probed at positions that hold no signal.
            break
        d = float(np.sum(fr[b]*w)/tot)
        x = x*np.exp(-2j*np.pi*d*n/sr)
        total += d
    return x, total


def channelize(x, sr, sps=4, rs=None, beta=None, span=16, centre=None,
               progress=None):
    """Shift the carrier to baseband, resample to `sps` samples/symbol and
    apply the receive matched filter. Returns (y, fs, centre_hz).

    The progress weights are measured, not guessed: on a 180 s segment this
    call takes 20.8 s, split mixdown 7.5%, refine_centre 70.6%, resample 9.9%,
    matched filter 11.9%. refine_centre dominates and reports per iteration,
    so the bar keeps moving through it.
    """
    rs = rs or spec.F80T45X8B.rs
    beta = spec.ROLLOFF if beta is None else beta
    if progress:
        progress(0.0, "mixing carrier to baseband")
    c = find_carrier(x, sr, rs) if centre is None else centre
    y = x*np.exp(-2j*np.pi*c*np.arange(len(x))/sr)
    y, extra = refine_centre(y, sr, rs,
                             progress=subprogress(progress, 0.075, 0.78))

    from math import gcd
    if progress:
        progress(0.78, "resampling to 4 samples/symbol")
    tgt = int(round(rs*sps))
    g = gcd(tgt, int(sr))
    y = resample_poly(y, tgt//g, int(sr)//g)

    if progress:
        progress(0.88, "applying matched filter")
    h = rrc(beta, sps, span)
    y = lfilter(h, 1, y)[len(h)//2:]
    return y, float(tgt), c+extra


# --- timing recovery ---------------------------------------------------------
# The |x|^2 spectral line at the symbol rate is ~19 dB above the floor on these
# captures, so a single global estimate of (rate, phase) beats any block-wise
# fit. Estimating rate coherently over the whole capture also absorbs clock
# error, which a fixed-rate resampler cannot.

def estimate_symbol_clock(y, fs, rs_nominal=None, search_ppm=200.0,
                          progress=None):
    """Estimate the true symbol rate and timing phase from the |y|^2 tone.

    Returns (rs_est, tau0) where tau0 is the timing phase in samples at t=0.

    Nearly all the time is in the accumulation passes -- on a 180 s segment,
    0.1 s for the coarse FFT against 16 s for the ten half-length passes below
    -- so progress is counted in those, per chunk.
    """
    rs_nominal = rs_nominal or spec.F80T45X8B.rs
    m = np.abs(y)**2
    m = m - m.mean()
    n = np.arange(len(m), dtype=np.float64)

    # coarse: FFT peak near the nominal rate (floor, so N <= len(m))
    N = 1 << int(np.floor(np.log2(min(len(m), 1 << 22))))
    M = np.fft.rfft(m[:N]*np.hanning(N))
    frq = np.fft.rfftfreq(N, 1/fs)
    lo = np.searchsorted(frq, rs_nominal*(1-search_ppm/1e6))
    hi = np.searchsorted(frq, rs_nominal*(1+search_ppm/1e6))
    k = lo + int(np.argmax(np.abs(M[lo:hi])))
    f0 = frq[k]

    # Fine: refine by phase slope rather than by searching. Accumulate the
    # tone over each half of the capture; the phase difference between the
    # halves gives the residual frequency error directly. Converges in a few
    # passes and costs one pass over the data each, instead of ~80.
    half = len(m)//2
    # Four refinement passes over both halves, then one final pass over all of
    # m: ten half-length accumulations, which is the unit `done` counts.
    NACC, done = 10.0, 0.0

    def accum(f, lo, hi, chunk=1 << 21):
        """Sum m[lo:hi] * exp(-j2pi f n/fs), chunked to bound memory."""
        nonlocal done
        tot = 0.0 + 0.0j
        for s in range(lo, hi, chunk):
            e = min(s+chunk, hi)
            idx = n[s:e]
            tot += np.sum(m[s:e]*np.exp(-2j*np.pi*f*idx/fs))
            if progress:
                progress(0.01 + 0.99*(done + (e - lo)/half)/NACC,
                         "estimating symbol clock")
        done += (hi - lo)/half
        return tot

    if progress:
        progress(0.01, "estimating symbol clock")
    f = f0
    for _ in range(4):
        c1 = accum(f, 0, half)
        c2 = accum(f, half, 2*half)
        if abs(c1) == 0 or abs(c2) == 0:
            break
        # centres of the two halves are half/2 apart
        dphi = np.angle(c2*np.conj(c1))
        f += dphi/(2*np.pi)*fs/half
    rs_est = f

    c = accum(rs_est, 0, len(m))
    # the tone peaks at the optimum sampling instant
    tau0 = -np.angle(c)/(2*np.pi)*(fs/rs_est)
    return float(rs_est), float(tau0)


def _cubic(y, i, mu):
    ym1, y0, y1, y2 = y[i-1], y[i], y[i+1], y[i+2]
    c0 = y0
    c1 = 0.5*(y1-ym1)
    c2 = ym1 - 2.5*y0 + 2*y1 - 0.5*y2
    c3 = 0.5*(y2-ym1) + 1.5*(y0-y1)
    return ((c3*mu + c2)*mu + c1)*mu + c0


def symbol_times(y, fs, rs_est, tau0):
    """Sample positions of the symbol instants, in the order and with the
    clipping that extract_symbols applies.

    Split out so that anything needing to know *where* symbol i came from
    cannot drift from what extract_symbols actually did. The head clipping is
    the subtle part: when tau0 <= 1 the first instant is dropped, so symbol i
    then sits at tau0 + (i+1)*period. Whether that happens depends on the
    timing phase, i.e. on roughly half of captures and on some of the eight
    phases survey_taus tries within a single capture. Rederiving the
    positions as tau0 + i*period looks obviously right and is one symbol out
    whenever the drop occurs -- it cost the equaliser a whole debugging pass,
    where the fit quietly absorbed the shift as a four-sample group delay.
    """
    period = fs/rs_est
    n = int((len(y)-4)/period) - 1
    pos = tau0 + period*np.arange(n)
    return pos[(pos > 1) & (pos < len(y)-3)]


def symbol_phase(y, fs, rs_est, tau0):
    """(first, period) such that symbol j sits at tau0 + (j + first)*period.

    The O(1) form of symbol_times, for callers that want a few positions
    rather than all of them: the unique-word estimators need 40 per frame,
    and the full table is ~72 MB per timing phase on a 60 s capture, times
    the eight phases survey_taus searches.

    `first` is derived with the same `> 1` predicate symbol_times uses rather
    than a rederived formula, because the whole point of both functions is
    that the head clipping must not be guessed at.
    """
    period = fs/rs_est
    probe = tau0 + period*np.arange(8)
    kept = np.flatnonzero(probe > 1)
    if not len(kept):
        raise ValueError("timing phase drops more than 8 leading symbols")
    return int(kept[0]), period


def extract_symbols(y, fs, rs_est, tau0):
    """Sample y at the symbol instants using cubic interpolation."""
    pos = symbol_times(y, fs, rs_est, tau0)
    i0 = np.floor(pos).astype(np.int64)
    return _cubic(y, i0, pos-i0)


def timing_quality(sym):
    """M4/M2^2 of the symbol stream. 1.32 is ideal 16-QAM, 2.0 is noise.
    Used to validate timing recovery rather than trusting convergence."""
    m2 = np.mean(np.abs(sym)**2)
    m4 = np.mean(np.abs(sym)**4)
    return float(m4/m2**2)


ESN0_M4_CAP_DB = 30.0
ESN0_M4_FLOOR_DB = -10.0


def esn0_from_m4(r, cap_db=ESN0_M4_CAP_DB, floor_db=ESN0_M4_FLOOR_DB):
    """Invert (1.32 rho^2 + 4 rho + 2)/(rho+1)^2 = r.

    Calibrated to +/-0.25 dB over 8..20 dB against the loopback transmitter.

    The expression tends to 1.32 from above as rho -> infinity, so at very high
    SNR estimation noise drives the measured r to or below 1.32, the quadratic
    degenerates and the naive inversion returns NaN. That NaN used to
    propagate into the demapper's n0 and silently kill every decode -- the
    cleanest possible signal was the one that failed. Saturate instead: above
    ~cap_db this estimator has no resolution anyway.
    """
    # The two degenerate ends mean OPPOSITE things and must not share a branch:
    #   r -> 1.32 is the noiseless limit  (very high SNR)
    #   r -> 2.00 is the Gaussian limit   (no signal)
    if not np.isfinite(r):
        return float(cap_db)
    if r <= 1.3205:
        return float(cap_db)
    if r >= 1.9995:
        return float(floor_db)
    a, b, c = 1.32-r, 4-2*r, 2-r
    disc = b*b - 4*a*c
    if disc < 0 or abs(a) < 1e-12:
        return float(floor_db)
    rho = (-b - np.sqrt(disc))/(2*a)
    if not np.isfinite(rho) or rho <= 0:
        return float(floor_db)
    return float(min(max(10*np.log10(rho), floor_db), cap_db))


# --- frame synchronisation ---------------------------------------------------

def uw_templates(normalise=True, levels=None):
    """The candidate unique words as symbol sequences.

    Defaults to the 10 levels F80T4.5X-8B actually uses. spec.UNIQUE_WORDS
    holds 15 (L8..H6) covering all bearer types; searching the 5 that this
    bearer cannot use only adds false-alarm opportunities.
    """
    levels = levels or spec.LEVELS_F80T45X8B
    return {lvl: mod.uw_symbols(lvl, normalise) for lvl in levels}


def frame_sync(sym, bearer=spec.F80T45X8B, templates=None):
    """Non-coherent UW search, folded over the frame period.

    Returns (offset, level, metric, all_scores) where offset is the index of
    the first UW symbol and level identifies which UW matched.

    Non-coherent because a residual carrier offset rotates the 40 UW symbols;
    at 264 us even a 100 Hz residual is under 10 degrees, so magnitude
    correlation is ample and needs no prior carrier recovery.
    """
    mos = bearer.mos
    templates = templates or uw_templates()
    nfr = len(sym)//mos
    if nfr < 2:
        raise ValueError(f"need at least 2 frames, have {nfr}")
    usable = nfr*mos
    s = sym[:usable]

    best = None
    scores = {}
    for lvl, t in templates.items():
        tc = np.conj(t[::-1])
        corr = np.convolve(s, tc, mode="valid")     # len usable-39
        p = np.abs(corr)**2
        pad = np.zeros(nfr*mos)
        pad[:len(p)] = p
        folded = pad.reshape(nfr, mos).sum(axis=0)
        j = int(np.argmax(folded))
        # peak-to-sidelobe: exclude a small guard around the peak
        g = np.ones(mos, bool)
        g[max(0, j-3):j+4] = False
        psr = folded[j]/np.mean(folded[g])
        scores[lvl] = (j, float(psr))
        if best is None or psr > best[2]:
            best = (j, lvl, float(psr))
    return best[0], best[1], best[2], scores


# --- carrier / phase recovery ------------------------------------------------

def pilot_phase(frame, bearer=spec.F80T45X8B):
    """Phase error at each pilot of one aligned frame."""
    _, pil, _ = mod.frame_layout(bearer)
    ref = mod.pilot_symbol()
    return np.angle(frame[pil]*np.conj(ref)), np.flatnonzero(pil)


def correct_phase(frame, bearer=spec.F80T45X8B, uw_level=None, smooth=9):
    """Estimate the carrier phase from the pilots and derotate the frame.

    A single pilot at Es/N0 rho has phase noise ~1/sqrt(2 rho): about 10
    degrees at 12 dB. Interpolating raw pilot phases therefore injects ~10
    degrees of jitter into every data symbol, which is severe for 16-QAM and
    costs several dB. So smooth first.

    Smoothing is done on the complex pilot products and the angle taken
    afterwards, rather than unwrapping phases and smoothing those: unwrap on
    noisy samples can take a wrong branch, and that failure is silent.

    `smooth` is the moving-average length in pilots; it trades phase-noise
    suppression against the ability to follow real phase movement. The UW
    anchors the start of the frame, where no pilot has been seen yet.
    """
    _, pil, _ = mod.frame_layout(bearer)
    pos = np.flatnonzero(pil).astype(float)
    ref = mod.pilot_symbol()
    z = frame[pil]*np.conj(ref)

    if uw_level is not None:
        uw = mod.uw_symbols(uw_level)
        # the whole UW coherently averaged is worth ~40 pilots, so it is a
        # strong anchor; weight it accordingly
        zu = np.sum(frame[:bearer.uw_syms]*np.conj(uw))/bearer.uw_syms
        z = np.concatenate([[zu*np.sqrt(bearer.uw_syms)], z])
        pos = np.concatenate([[bearer.uw_syms/2], pos])

    if smooth and smooth > 1 and len(z) > smooth:
        k = np.ones(smooth)/smooth
        zs = np.convolve(z, k, mode="same")
        # edges of 'same' are under-averaged; renormalise by the window that
        # actually contributed
        norm = np.convolve(np.ones(len(z)), k, mode="same")
        z = zs/norm
    ph = np.unwrap(np.angle(z))

    idx = np.arange(len(frame))
    return frame*np.exp(-1j*np.interp(idx, pos, ph))


def fine_cfo_from_pilots(sym, offset, level, bearer=spec.F80T45X8B, nframes=None):
    """Data-aided residual CFO from the known UW and pilot symbols.

    The spectral-centroid estimate in channelize() is only good to ~100 Hz,
    which is ample for the non-coherent UW search but not for demodulation.
    Once framing is known, the UW and the 88 pilots per frame are known
    symbols, so their phase advance measures the residual directly.

    Returns cfo in Hz.
    """
    mos = bearer.mos
    _, pil, _ = mod.frame_layout(bearer)
    pil_idx = np.flatnonzero(pil)
    uw = mod.uw_symbols(level)
    ref = mod.pilot_symbol()

    n = (len(sym)-offset)//mos
    if nframes:
        n = min(n, nframes)
    if n < 1:
        raise ValueError("no complete frames")

    # phase of each known symbol, against its absolute symbol index
    idx, ph = [], []
    for f in range(n):
        base = offset + f*mos
        fr = sym[base:base+mos]
        if len(fr) < mos:
            break
        idx.append(base + np.arange(bearer.uw_syms))
        ph.append(fr[:bearer.uw_syms]*np.conj(uw))
        idx.append(base + pil_idx)
        ph.append(fr[pil_idx]*np.conj(ref))
    idx = np.concatenate(idx).astype(np.float64)
    z = np.concatenate(ph)

    # Estimate the phase slope without unwrapping: correlate successive known
    # symbols separated by a fixed lag. Unwrapping across noisy pilots is
    # exactly the kind of thing that silently picks a wrong branch.
    order = np.argsort(idx)
    idx, z = idx[order], z[order]
    d = np.diff(idx)
    step = np.median(d[d > 0])
    sel = np.abs(d - step) < 1e-9
    if not np.any(sel):
        raise ValueError("no uniformly spaced known symbols")
    acc = np.sum(z[1:][sel]*np.conj(z[:-1][sel]))
    dphi = np.angle(acc)
    return float(dphi/(2*np.pi*step)*spec.F80T45X8B.rs)


# --- blind carrier recovery --------------------------------------------------
# Must run BEFORE frame sync. fine_cfo_from_pilots() needs framing, and framing
# needs a usable constellation, so a pilot-aided-only design is circular. This
# breaks the loop and is validated to be CFO-invariant from 0 to 8 kHz.

def blind_carrier_recovery(s, blk=64, corner_thresh=1.15):
    """Two-stage blind carrier recovery for 16-QAM.

    Stage 1: bulk CFO from the 4th-power spectral line over the whole capture.
    Stage 2: residual phase per short block from outer-ring symbols only.

    Both stages are needed. 16-QAM has a much weaker 4th-power line than QPSK
    (it is not constant-modulus), so a global estimate alone is fragile; and a
    per-block estimate alone fails above ~50 Hz, because at 300 Hz a
    256-symbol block already rotates 183 degrees and smears itself. Measured
    on synthetic at Es/N0 12 dB, per-block alone recovers a 50 Hz offset
    (gridness 0.365) but not 300 Hz (0.078); the two stages together give
    0.4509 at every offset from 0 to 8 kHz.

    'gridness' here is |mean(s^4)| / mean(|s|^4): ~0.45 for an aligned 16-QAM
    grid at 12 dB, ~0 for an unrecovered (rotationally symmetric) one.
    """
    z = s**4
    N = 1 << int(np.floor(np.log2(len(z))))
    Z = np.fft.fft(z[:N])
    k = int(np.argmax(np.abs(Z)))
    if k > N//2:
        k -= N
    df = (k/N)/4.0
    s = s*np.exp(-2j*np.pi*df*np.arange(len(s)))

    out = np.empty_like(s)
    prev = 0.0
    for i in range(0, len(s), blk):
        seg = s[i:i+blk]
        a = np.abs(seg)
        sel = seg[a > corner_thresh]
        if len(sel) < 4:
            sel = seg[a > np.percentile(a, 70)] if len(seg) > 4 else seg
        if len(sel) < 3:
            ph = prev
        else:
            # corner points sit at 45+n*90 deg, so angle(sum(s^4))/4 returns
            # phi + 45 deg; subtract it or the whole grid comes out rotated
            ph = np.angle(np.sum(sel**4))/4.0 - np.pi/4
            ph += np.round((prev-ph)/(np.pi/2))*(np.pi/2)
        prev = ph
        out[i:i+blk] = seg*np.exp(-1j*ph)
    return out


def gridness(s):
    """|mean(s^4)|/mean(|s|^4). ~0.45 for an aligned 16-QAM grid at 12 dB,
    ~0 when the carrier is unrecovered. Cheap health check on the front end."""
    return float(abs(np.mean(s**4))/np.mean(np.abs(s)**4))
