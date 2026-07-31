"""Scan a baseband capture for every carrier present and identify its bearer.

Motivation: all six F80T4.5X-8B carriers examined so far are clean 16-QAM at
151.2 kBd with an exact 12096-symbol frame period, yet none carries the unique
word or pilots the standard requires. A narrower bearer (F80T1X-4B, 33.6 kBd)
uses the *same* 40-symbol UW and 88 pilots. If one of those shows the structure
and the 151.2 kBd carriers do not, the anomaly is specific to those carriers
rather than to this receiver or to the standard.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

import numpy as np
from scipy.signal import welch, resample_poly, firwin, lfilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bgan import recv, spec

# Table 5.2 forward symbol rates, plus the return-bearer rates that share them.
CANDIDATE_RS = {
    8400.0:   "F80T0.25Q-1B (QPSK, 10.5 kHz)",
    33600.0:  "F80T1X-4B / F80T1Q-4B (42 kHz)",
    84000.0:  "FR80T2.5X* (94.92 kHz)",
    151200.0: "F80T4.5X-8B (16-QAM, 189 kHz)",
    168000.0: "FR80T5X* (189.84 kHz)",
}


def detect_carriers(x, sr, min_bw=6e3, snr_db=3.0):
    """Find contiguous above-noise regions in the PSD.

    Returns list of dicts with centre, bandwidth and in-band power.
    """
    fr, P = welch(x, sr, nperseg=16384, return_onesided=False, detrend=False)
    i = np.argsort(fr)
    fr, P = fr[i], P[i]
    Ps = np.convolve(P, np.ones(24)/24, "same")
    noise = np.median(np.sort(Ps)[:len(Ps)//4])
    mask = Ps > noise*10**(snr_db/10)

    out = []
    k = 0
    df = fr[1]-fr[0]
    while k < len(mask):
        if not mask[k]:
            k += 1
            continue
        j = k
        while j < len(mask) and mask[j]:
            j += 1
        bw = (j-k)*df
        if bw >= min_bw:
            seg = slice(k, j)
            w = np.maximum(Ps[seg]-noise, 0)
            c = float(np.sum(fr[seg]*w)/max(np.sum(w), 1e-30))
            out.append(dict(centre=c, bw=float(bw),
                            power_db=float(10*np.log10(np.sum(w)*df + 1e-30)),
                            snr_db=float(10*np.log10(Ps[seg].max()/noise))))
        k = j
    return out


def tone_strength(x, sr, rs, span_hz=400.0):
    """Excess of the |x|^2 cyclostationary line at `rs`, in dB over local floor."""
    if rs >= sr/2:
        return -99.0, rs
    m = np.abs(x)**2
    m = m - m.mean()
    N = 1 << 18
    if len(m) < N:
        N = 1 << int(np.floor(np.log2(len(m))))
    w = np.hanning(N)
    acc = np.zeros(N//2+1)
    k = 0
    for s in range(0, len(m)-N, N//2):
        acc += np.abs(np.fft.rfft(m[s:s+N]*w))**2
        k += 1
        if k >= 24:
            break
    if k == 0:
        return -99.0, rs
    acc /= k
    frq = np.fft.rfftfreq(N, 1/sr)
    base = np.convolve(acc, np.ones(201)/201, "same")
    exc = 10*np.log10(acc/(base+1e-30))
    lo = np.searchsorted(frq, rs-span_hz)
    hi = np.searchsorted(frq, rs+span_hz)
    if hi <= lo:
        return -99.0, rs
    j = lo + int(np.argmax(exc[lo:hi]))
    return float(exc[j]), float(frq[j])


def channel_of(x, sr, centre, bw, out_sr=None):
    """Shift a carrier to baseband and decimate to a rate suited to its width."""
    y = x*np.exp(-2j*np.pi*centre*np.arange(len(x))/sr)
    want = out_sr or max(4*bw, 40e3)
    dec = max(1, int(sr/want))
    if dec > 1:
        taps = firwin(255, min(0.95, 0.9/dec))
        y = lfilter(taps, 1, y)[::dec]
    return y, sr/dec


def identify(x, sr, c):
    """Best-matching symbol rate for one detected carrier."""
    y, fs = channel_of(x, sr, c["centre"], c["bw"])
    best = None
    rows = []
    for rs, name in CANDIDATE_RS.items():
        if rs >= fs/2:
            rows.append((rs, name, -99.0, rs))
            continue
        db, f = tone_strength(y, fs, rs)
        rows.append((rs, name, db, f))
        if best is None or db > best[2]:
            best = (rs, name, db, f)
    # modulation hint from the symbol-magnitude kurtosis at the best rate
    m4 = float("nan")
    if best and best[2] > 6.0:
        yy, ffs = channel_of(x, sr, c["centre"], c["bw"],
                             out_sr=best[0]*4)
        from math import gcd
        tgt = int(round(best[0]*4))
        g = gcd(int(round(ffs)), tgt)
        yy = resample_poly(yy, tgt//g, int(round(ffs))//g)
        h = recv.rrc(spec.ROLLOFF, 4, 16)
        yy = lfilter(h, 1, yy)[len(h)//2:]
        rse, tau = recv.estimate_symbol_clock(yy, tgt, rs_nominal=best[0],
                                              search_ppm=500.0)
        sy = recv.extract_symbols(yy, tgt, rse, tau)
        m4 = recv.timing_quality(sy)
    return best, rows, m4


def main():
    files = sys.argv[1:]
    if not files:
        base = Path(os.environ.get("BGAN_CAPTURES", "."))
        files = sorted(str(p) for p in base.glob("*.wav"))
        if not files:
            print(f"no .wav captures in {base.resolve()}")
            print("pass paths as arguments, or set $BGAN_CAPTURES")
            return
    for f in files:
        x, sr = recv.load_wav_iq(f, secs=20)
        x = x - x.mean()
        print("="*78)
        print(Path(f).name)
        print(f"  {len(x)/sr:.1f} s @ {sr/1e3:.0f} kHz  (band +/-{sr/2e3:.0f} kHz)")
        cars = detect_carriers(x, sr)
        print(f"  {len(cars)} carrier region(s) detected")
        for c in cars:
            best, rows, m4 = identify(x, sr, c)
            print(f"  --- centre {c['centre']/1e3:+8.1f} kHz  "
                  f"bw {c['bw']/1e3:6.1f} kHz  peak SNR {c['snr_db']:4.1f} dB")
            for rs, name, db, f_ in sorted(rows, key=lambda r: -r[2])[:3]:
                mark = " <<<" if best and rs == best[0] else ""
                print(f"        {rs/1e3:8.1f} kBd  tone {db:5.1f} dB  "
                      f"@{f_:10.1f} Hz  {name}{mark}")
            if np.isfinite(m4):
                mod = ("QPSK" if m4 < 1.16 else
                       "16-QAM" if m4 < 1.55 else "noise/unclear")
                print(f"        M4/M2^2 {m4:.3f} -> {mod} "
                      f"(QPSK 1.00, 16-QAM 1.32, noise 2.00)")


if __name__ == "__main__":
    main()
