"""Generate synthetic F80T4.5X-8B baseband IQ as an SDR++-compatible WAV.

Produces a known-good reference signal: same container, sample rate and bit
depth as the real captures, so it can be opened in SDR++ or fed to any tool
that reads those, and compared like-for-like against a real recording.

The transmitted payload bits are saved alongside as .npz, so a decoder can be
scored bit-exactly rather than just "it converged".
"""
from __future__ import annotations
import argparse
import os
import struct
import sys
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bgan import spec, tx


def write_wav_iq(path, x, sr, peak=0.35):
    """Write complex IQ as 16-bit stereo PCM (I=left, Q=right).

    peak sets the headroom. Real captures from this setup sit near 0.05-0.12
    of full scale; 0.35 is comfortably above that without clipping.
    """
    m = np.max(np.abs(np.concatenate([x.real, x.imag])))
    g = (peak/m) if m > 0 else 1.0
    i = np.clip(np.round(x.real*g*32767), -32768, 32767).astype("<i2")
    q = np.clip(np.round(x.imag*g*32767), -32768, 32767).astype("<i2")
    inter = np.empty(2*len(i), dtype="<i2")
    inter[0::2] = i
    inter[1::2] = q
    data = inter.tobytes()

    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36+len(data)))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 2, sr, sr*4, 4, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)
    return len(data)


def generate(level="L3", seconds=10.0, sr=512000, esn0_db=12.0, cfo_hz=0.0,
             carrier_hz=0.0, seed=1234, bearer=spec.F80T45X8B):
    """Build the waveform. Returns (x, payloads, info)."""
    rng = np.random.default_rng(seed)
    nframes = int(round(seconds/0.080))

    payloads, frames = [], []
    for _ in range(nframes):
        p, fr = tx.random_frame(level, rng, bearer)
        payloads.append(p)
        frames.append(fr)
    symbols = np.concatenate(frames)

    # pulse shape at an integer 4 sps, then resample to the target rate
    SPS = 4
    x = tx.to_iq(symbols, sps=SPS, beta=spec.ROLLOFF, span=16, rng=rng)
    tgt = int(round(bearer.rs*SPS))
    from math import gcd
    g = gcd(int(sr), tgt)
    x = resample_poly(x, sr//g, tgt//g)

    # Es/N0 -> noise variance at this sample rate.
    #   Es = P_signal / Rs ;  N0 = sigma^2 / fs
    #   => sigma^2 = P_signal * fs / (Rs * snr)
    if esn0_db is not None:
        ps = float(np.mean(np.abs(x)**2))
        snr = 10**(esn0_db/10)
        var = ps*sr/(bearer.rs*snr)
        x = x + np.sqrt(var/2)*(rng.standard_normal(len(x)) +
                                1j*rng.standard_normal(len(x)))

    tone = cfo_hz + carrier_hz
    if tone:
        x = x*np.exp(2j*np.pi*tone*np.arange(len(x))/sr)

    info = dict(level=level, nframes=nframes, sr=sr, esn0_db=esn0_db,
                cfo_hz=cfo_hz, carrier_hz=carrier_hz, seed=seed,
                symbols=len(symbols), seconds=len(x)/sr)
    return x, payloads, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.environ.get("BGAN_CAPTURES", "work"),
                    help="output directory (env BGAN_CAPTURES)")
    ap.add_argument("--level", default="L3")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--sr", type=int, default=512000)
    ap.add_argument("--esn0", type=float, default=12.0)
    ap.add_argument("--cfo", type=float, default=0.0)
    ap.add_argument("--carrier", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    x, payloads, info = generate(a.level, a.seconds, a.sr, a.esn0,
                                 a.cfo, a.carrier, a.seed)
    out = Path(a.out)
    tag = a.tag or f"{a.level}_{a.esn0:g}dB"
    stem = f"SYNTHETIC_F80T45X8B_{tag}_baseband_{a.sr}Hz"
    wav = out/f"{stem}.wav"
    n = write_wav_iq(wav, x, a.sr)
    np.savez_compressed(out/f"{stem}_truth.npz",
                        payloads=np.array(payloads, dtype=np.uint8),
                        **{k: v for k, v in info.items() if k != "level"},
                        level=info["level"])
    print(f"{wav.name}")
    print(f"   {info['seconds']:.2f} s, {a.sr} Hz, 16-bit stereo IQ, {n/1e6:.1f} MB")
    print(f"   level {info['level']}, {info['nframes']} frames, "
          f"Es/N0 {a.esn0} dB, CFO {a.cfo:+g} Hz, carrier {a.carrier:+g} Hz")
    a0 = np.array(payloads)
    print(f"   truth: {stem}_truth.npz  payloads{a0.shape} "
          f"(frames x blocks x bits)")
    return wav


if __name__ == "__main__":
    main()
