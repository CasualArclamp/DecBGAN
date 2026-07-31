"""Spec-compliant F80T4.5X-8B transmitter.

Exists to produce known-good test vectors: every receive stage is validated
against this before it is allowed near real IQ. A receiver that only ever sees
real signals cannot distinguish a spec misreading from low SNR.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import spec, mod
from .annex import ANNEX_C1, ANNEX_C2, annex_dir, load_cipm, load_tci
from .turbo import map_to_symbols, turbo_encode


@dataclass
class LevelTables:
    level: str
    perm: np.ndarray
    cipm: object

    @property
    def D(self):
        return int(self.cipm.params["D"])


_cache: dict[str, LevelTables] = {}


def annex_level_name(level):
    """Annex C filenames spell the 'R' level 'RE'. 'R' is canonical elsewhere
    (spec.LEVELS_F80T45X8B, spec.UNIQUE_WORDS), so translate only here."""
    return "RE" if level == "R" else level


def tables(level, root=None) -> LevelTables:
    if level in _cache:
        return _cache[level]
    root = Path(root or Path(__file__).resolve().parent.parent)
    c1 = annex_dir(root, ANNEX_C1)
    c2 = annex_dir(root, ANNEX_C2)
    fn = annex_level_name(level)
    t = LevelTables(level,
                    load_tci(c1/f"F80T45X{fn}_TCI.TXT"),
                    load_cipm(c2/f"F80T45X{fn}_CIPM.TXT"))
    _cache[level] = t
    return t


def encode_block(data_bits, level, scrambled=True):
    """One FEC block: scramble -> turbo encode -> puncture/interleave -> map.

    Returns (teo,) complex symbols.
    """
    t = tables(level)
    if len(data_bits) != t.D:
        raise ValueError(f"level {level} takes {t.D} data bits, got {len(data_bits)}")
    bits = mod.scramble(data_bits) if scrambled else np.asarray(data_bits, np.uint8)
    df, p, q = turbo_encode(bits, t.perm)
    slots = map_to_symbols(df, p, q, t.cipm)
    return mod.bits4_to_symbol(slots)


def make_frame(payloads, level, bearer=spec.F80T45X8B):
    """One 80 ms frame from `nblocks` payloads, all at the same coding level.

    Returns (mos,) complex symbols at unit mean power.
    """
    blocks = np.stack([encode_block(p, level) for p in payloads])
    return mod.assemble_frame(blocks, level, bearer)


def random_frame(level, rng, bearer=spec.F80T45X8B):
    t = tables(level)
    payloads = [rng.integers(0, 2, t.D, dtype=np.uint8)
                for _ in range(bearer.nblocks)]
    return payloads, make_frame(payloads, level, bearer)


def rrc(beta, sps, span):
    """Root-raised-cosine, unit energy."""
    n = span*sps
    t = np.arange(-n//2, n//2+1)/sps
    h = np.empty_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-9:
            h[i] = 1 - beta + 4*beta/np.pi
        elif beta > 0 and abs(abs(ti) - 1/(4*beta)) < 1e-9:
            h[i] = beta/np.sqrt(2)*((1+2/np.pi)*np.sin(np.pi/(4*beta)) +
                                    (1-2/np.pi)*np.cos(np.pi/(4*beta)))
        else:
            h[i] = (np.sin(np.pi*ti*(1-beta)) +
                    4*beta*ti*np.cos(np.pi*ti*(1+beta))) / \
                   (np.pi*ti*(1-(4*beta*ti)**2))
    return h/np.sqrt(np.sum(h**2))


def to_iq(symbols, sps=4, beta=spec.ROLLOFF, span=16, esn0_db=None,
          cfo_hz=0.0, rs=None, rng=None, timing_offset=0.0):
    """Pulse-shape symbols to an IQ waveform, optionally impaired.

    esn0_db applies AWGN at the given Es/N0; cfo_hz applies a carrier offset;
    timing_offset is a fractional-symbol delay.
    """
    rs = rs or spec.F80T45X8B.rs
    rng = rng or np.random.default_rng()

    up = np.zeros(len(symbols)*sps, dtype=complex)
    up[::sps] = symbols
    h = rrc(beta, sps, span)
    if timing_offset:
        # fractional delay folded into the pulse shape
        n = np.arange(-(len(h)//2), len(h)//2 + 1)
        hi = np.interp(n - timing_offset*sps, n, h)
        h = hi/np.sqrt(np.sum(hi**2))
    x = np.convolve(up, h)

    if cfo_hz:
        x = x*np.exp(2j*np.pi*cfo_hz*np.arange(len(x))/(rs*sps))

    if esn0_db is not None:
        # Es is the energy per symbol; with unit-mean-power symbols and a
        # unit-energy pulse, mean sample power is 1/sps.
        es = np.mean(np.abs(x)**2)*sps
        n0 = es/(10**(esn0_db/10))
        # The receive matched filter has unit energy, so white noise of
        # variance v per complex sample emerges with variance v per symbol.
        # Hence v must equal N0 -- NOT N0*sps. Getting this wrong offsets
        # every Es/N0 by 10*log10(sps).
        sigma = np.sqrt(n0/2)          # per real dimension, per sample
        x = x + sigma*(rng.standard_normal(len(x)) +
                       1j*rng.standard_normal(len(x)))
    return x
