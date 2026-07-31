"""Loaders for the ETSI TS 102 744-2-1 Annex C.1 / C.2 tables.

Annex C.1 (*_TCI.TXT)  -- turbo code interleaver permutation.
Annex C.2 (*_CIPM.TXT) -- channel interleaving, puncturing and QAM mapping.

Together these fully specify the coded-bit layout, so nothing about
puncturing has to be inferred.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ANNEX_C1 = "ts_1027440201_AnnexC1_v010101p0"
ANNEX_C2 = "ts_1027440201_AnnexC2_v010101p0"


def load_tci(path) -> np.ndarray:
    """Return perm[] where perm[q] is the data-bit index feeding interleaved
    position q. Length is DF (data + 4 flush bits)."""
    rows = []
    started = False
    for line in Path(path).read_text().splitlines():
        s = line.strip()
        if s.startswith("---"):
            started = True
            continue
        if not started or not s:
            continue
        a, b = s.split()
        rows.append((int(a), int(b)))
    q = np.array([r[0] for r in rows])
    d = np.array([r[1] for r in rows])
    if not np.array_equal(q, np.arange(len(q))):
        raise ValueError(f"{path}: Q-bit column is not 0..N-1")
    perm = d
    if sorted(perm.tolist()) != list(range(len(perm))):
        raise ValueError(f"{path}: data-bit column is not a permutation")
    return perm


@dataclass
class Cipm:
    name: str
    params: dict
    # per channel symbol, the global bit index carried by each of the 4 slots
    idx: np.ndarray        # (FECSy, 4) int, columns I1, I0, Q1, Q0
    kind: np.ndarray       # (FECSy, 4) uint8: 0=data '.', 1=p '+', 2=q '-'
    # decomposition into per-stream indices
    stream_pos: np.ndarray  # (FECSy, 4) index within its own stream

    @property
    def fecsy(self):
        return self.idx.shape[0]


_KIND = {".": 0, "+": 1, "-": 2}


def load_cipm(path) -> Cipm:
    text = Path(path).read_text().splitlines()
    name = text[0].strip()
    params = {}
    for line in text[1:8]:
        if line.strip().startswith("Symbol"):
            break
        for k, v in re.findall(r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?)", line):
            params[k] = float(v) if "." in v else int(v)

    idx, kind = [], []
    started = False
    for line in text:
        s = line.strip()
        if s.startswith("---"):
            started = True
            continue
        if not started or not s or s.startswith("Total"):
            continue
        f = s.split()
        if len(f) != 6:
            continue
        idx.append([int(f[1]), int(f[2]), int(f[3]), int(f[4])])
        pat = f[5]
        if len(pat) != 4:
            raise ValueError(f"{path}: bad pattern {pat!r}")
        kind.append([_KIND[c] for c in pat])

    idx = np.array(idx, dtype=np.int32)
    kind = np.array(kind, dtype=np.uint8)

    # The global index space is the *unpunctured* rate-1/3 codeword of length
    # 3*DF: [0,DF) systematic, [DF,2DF) p-parity, [2DF,3DF) q-parity. Verified
    # for all ten F80T4.5X levels -- max global index is exactly 3*DF-1.
    #
    # Do NOT renumber the surviving parity bits by rank: puncturing drops some
    # (e.g. 14 of 2004 p-bits at L3), so a rank-based index would be offset
    # from the encoder's own parity output index.
    DF = params["DF"]
    stream_pos = idx - kind.astype(np.int32)*DF

    return Cipm(name=name, params=params, idx=idx, kind=kind,
                stream_pos=stream_pos)


def verify_cipm(c: Cipm) -> dict:
    """Check the parsed table against the parameters in its own header."""
    p = c.params
    DF, N = p["DF"], p["N"]
    npar = p["p"]
    out = {}

    out["n_symbols"] = (c.fecsy, p["FECSy"])
    assert c.fecsy == p["FECSy"], out

    # every channel bit slot used exactly once
    assert c.idx.size == N, (c.idx.size, N)
    allidx = np.sort(c.idx.ravel())
    out["global_indices_unique"] = bool(len(np.unique(allidx)) == N)
    out["global_index_range"] = (int(allidx[0]), int(allidx[-1]))

    counts = [int((c.kind == k).sum()) for k in (0, 1, 2)]
    out["counts_data_p_q"] = counts
    out["expected_data_p_q"] = [DF, npar, npar]
    assert counts == [DF, npar, npar], out

    # data stream indices must be exactly 0..DF-1 and equal the global index
    dsel = c.kind == 0
    assert np.array_equal(np.sort(c.idx[dsel]), np.arange(DF)), "data idx != 0..DF-1"
    out["data_global_equals_stream"] = bool(
        np.array_equal(c.idx[dsel], c.stream_pos[dsel]))

    # Global index space is the unpunctured rate-1/3 codeword, length 3*DF.
    # The largest index need not be exactly 3*DF-1: the final q-parity bit may
    # itself be punctured (it is at H2).
    out["max_global"] = (int(allidx[-1]), 3*DF - 1)
    assert allidx[-1] < 3*DF, out["max_global"]

    for k, nm in ((1, "p"), (2, "q")):
        sel = c.kind == k
        g = c.idx[sel]
        assert g.min() >= k*DF and g.max() < (k+1)*DF, f"{nm} outside its block"
        sp = c.stream_pos[sel]
        assert sp.min() >= 0 and sp.max() < DF
        assert len(np.unique(sp)) == len(sp)
        out[f"{nm}_punctured"] = int(DF - npar)
    return out


def annex_dir(root: Path, which: str) -> Path:
    for cand in (root/"work"/which, root/which, root/"annex"/which):
        if cand.is_dir():
            return cand
    raise FileNotFoundError(which)
