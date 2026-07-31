"""Constellation diagrams: real captures vs synthetic at matched Es/N0.

The real signal has no frame lock, so phase recovery here is blind (4th-power),
which 16-QAM supports because the constellation has 90 degree rotational
symmetry. That leaves a 4-fold rotation ambiguity, which does not matter for
looking at the shape.
"""
import os
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import lfilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bgan import recv, spec, tx


def blind_derotate(s, blk=64):
    """Blind carrier recovery for 16-QAM, in two stages.

    Stage 1 removes the bulk CFO using the 4th-power spectral line over the
    whole capture. This is essential: a per-block estimator alone fails above
    ~50 Hz, because at 300 Hz a 256-symbol block already rotates 183 degrees
    and smears itself. Measured on synthetic, per-block alone recovers a
    50 Hz offset (gridness 0.37) but not 300 Hz (0.08).

    Stage 2 removes the residual and any phase noise per short block, using
    only outer-ring symbols, which behave like QPSK under the 4th power.
    """
    # stage 1: global CFO from the 4th-power line
    z = s**4
    N = 1 << int(np.floor(np.log2(len(z))))
    Z = np.fft.fft(z[:N])
    k = int(np.argmax(np.abs(Z)))
    if k > N//2:
        k -= N
    df = (k/N)/4.0                       # cycles per symbol
    s = s*np.exp(-2j*np.pi*df*np.arange(len(s)))

    # stage 2: per-block residual phase
    out = np.empty_like(s)
    prev = 0.0
    for i in range(0, len(s), blk):
        seg = s[i:i+blk]
        a = np.abs(seg)
        sel = seg[a > 1.15]
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


def real_symbols(path, secs=12):
    x, sr = recv.load_wav_iq(path, secs=secs)
    y, fs, c = recv.channelize(x, sr, sps=4)
    rs, tau = recv.estimate_symbol_clock(y, fs)
    s = recv.extract_symbols(y, fs, rs, tau)
    s = s/np.sqrt(np.mean(np.abs(s)**2))
    return blind_derotate(s), recv.esn0_from_m4(recv.timing_quality(s))


def synth_symbols(esn0, nframes=3, seed=4):
    rng = np.random.default_rng(seed)
    sym = np.concatenate([tx.random_frame("L3", rng)[1] for _ in range(nframes)])
    x = tx.to_iq(sym, sps=4, esn0_db=esn0, rng=rng)
    h = tx.rrc(0.25, 4, 16)
    s = lfilter(h, 1, x)[len(h)-1:][::4]
    return s/np.sqrt(np.mean(np.abs(s)**2))


def panel(ax, s, title, lim=2.0, bins=260):
    h, xe, ye = np.histogram2d(s.real, s.imag, bins=bins,
                               range=[[-lim, lim], [-lim, lim]])
    ax.imshow(np.log1p(h.T), origin="lower", extent=[-lim, lim, -lim, lim],
              cmap="inferno", interpolation="nearest", aspect="equal")
    g = np.array([-1.5, -0.5, 0.5, 1.5])/np.sqrt(2.5)
    gx, gy = np.meshgrid(g, g)
    ax.plot(gx.ravel(), gy.ravel(), "+", color="#39d0ff", ms=7, mew=1.3)
    ax.set_title(title, fontsize=9)
    ax.set_xticks([]); ax.set_yticks([])


def radial(ax, series, labels):
    ideal = np.array([np.sqrt(2), np.sqrt(10), np.sqrt(18)])/np.sqrt(10)
    for s, lb in zip(series, labels):
        a = np.abs(s)
        h, e = np.histogram(a, bins=200, range=(0, 2.0), density=True)
        ax.plot((e[:-1]+e[1:])/2, h, lw=1.4, label=lb)
    for r in ideal:
        ax.axvline(r, color="k", ls=":", lw=1)
    ax.set_xlabel("|symbol|  (unit mean power)", fontsize=8)
    ax.set_ylabel("density", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=7)
    ax.set_title("radial profile; dotted = ideal 16-QAM rings", fontsize=9)


def main():
    if len(sys.argv) > 1:
        files = [(Path(a).stem[:24], Path(a)) for a in sys.argv[1:]]
    else:
        base = Path(os.environ.get("BGAN_CAPTURES", "."))
        files = [(p.stem[:24], p) for p in sorted(base.glob("*.wav"))[:3]]
    if not files:
        print("pass capture paths as arguments, or set $BGAN_CAPTURES")
        return
    fig = plt.figure(figsize=(13.5, 8.4))
    fig.suptitle("F80T4.5X-8B constellations — real captures vs synthetic "
                 "(blind 4th-power phase recovery)", fontsize=11)

    reals, rlabels = [], []
    for i, (name, p) in enumerate(files):
        s, e = real_symbols(str(p))
        reals.append(s); rlabels.append(f"{name} ({e:.1f} dB)")
        ax = fig.add_subplot(2, 4, i+1)
        panel(ax, s, f"REAL {name}\neffective Es/N0 {e:.1f} dB")
        print(f"{name}: {len(s)} symbols, Es/N0 {e:.2f} dB")

    ax = fig.add_subplot(2, 4, 4)
    sn = synth_symbols(30)
    panel(ax, sn, "SYNTHETIC Es/N0 30 dB\n(ideal reference)")

    for i, e in enumerate((9.8, 12.0, 16.0)):
        ax = fig.add_subplot(2, 4, 5+i)
        panel(ax, synth_symbols(e), f"SYNTHETIC Es/N0 {e} dB")

    ax = fig.add_subplot(2, 4, 8)
    radial(ax, [reals[0], synth_symbols(9.8), sn],
           ["real 1543.1", "synth 9.8 dB", "synth 30 dB"])

    fig.tight_layout(rect=[0, 0, 1, 0.95])
    out = Path(__file__).resolve().parent.parent/"work"/"constellations.png"
    fig.savefig(out, dpi=115)
    print("wrote", out)


if __name__ == "__main__":
    main()
