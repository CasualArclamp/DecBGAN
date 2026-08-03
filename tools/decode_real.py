"""Decode a real F80T4.5X-8B capture. IQ symbols in, payload bytes out.

Uses the per-block pilot normalisation and linear phase detrend that turned out
to be essential on real signal, and reports parity-only agreement (systematic
bits pass through trivially and would inflate the score at high code rates).
"""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bgan import spec, mod, tx
from bgan import decoder as dec
from bgan.turbo import map_to_symbols, turbo_encode

MOS, STEP, UWLEN, DAT, GRP = 12096, 137, 40, 136, 11


def diff_uw(level):
    b = mod.uw_bits(level)
    return np.where(b[1:] ^ b[:-1], -1.0, 1.0)


def find_frame_offset(z, levels=None, nprobe=12):
    """Acquire the frame base by differential UW correlation, PER FRAME.

    Do NOT fold the correlation across frames. The frame position drifts (about
    -2.2 symbols/frame on BGAN19), so a fold smears the peak and locks onto a
    sidelobe family: folding put the answer at 9184 when the decodable base was
    8712, and nothing decoded. Scanning single frames independently and taking
    the strongest peak gives the right base directly -- at the decodable
    position the correlation is 66.9 against ~17 at +/-4 symbols and 5-10
    elsewhere, so a single frame is ample.
    """
    levels = levels or spec.LEVELS_F80T45X8B
    d = np.empty_like(z)
    d[0] = 0
    d[1:] = z[1:]*np.conj(z[:-1])
    pats = {l: diff_uw(l) for l in levels}
    best = None
    nfr = min(nprobe, len(z)//MOS - 1)
    for f in range(nfr):
        seg0 = f*MOS
        for lvl, w in pats.items():
            c = np.abs(np.convolve(d[seg0:seg0+2*MOS], w[::-1], mode="valid"))
            j = int(np.argmax(c[:MOS]))
            if best is None or c[j] > best[2]:
                # corr index m corresponds to a UW starting at m-1
                best = ((seg0 + j - 1) % MOS, lvl, float(c[j]))
    return best


def block_symbols(z, fb, b):
    data, dpos, pil, ppos = [], [], [], []
    for gi in range(GRP):
        g = GRP*b + gi
        base = fb + UWLEN + STEP*g
        if base + DAT >= len(z):
            return None
        data.append(z[base:base+DAT])
        dpos.append(gi*137.0 + np.arange(DAT))
        pil.append(z[fb + 176 + STEP*g])
        ppos.append(gi*137.0 + 136.0)
    return (np.concatenate(data), np.concatenate(dpos),
            np.array(pil), np.array(ppos))


def prepare(z, fb, b, rot=0):
    """Per-block amplitude normalisation and linear phase detrend from pilots.

    Both are per-block, not per-frame: a frame-wide phase fit was not accurate
    enough on real captures and produced no decodes at all.
    """
    got = block_symbols(z, fb, b)
    if got is None:
        return None
    d, dp, pl, pp = got
    sc = 1.34164/(np.mean(np.abs(pl)) + 1e-9)      # pilots -> outer-corner mag
    d, pl = d*sc, pl*sc
    ang = np.unwrap(np.angle(pl))
    c1, c0 = np.polyfit(pp, ang, 1)
    return d*np.exp(-1j*(c0 + c1*dp))*np.exp(1j*(np.pi/4 + rot*np.pi/2))


def decode_block(blk, level, s2=0.5, iters=8):
    t = tx.tables(level)
    llr = dec.soft_demap(blk, s2)
    Ls, Lp, Lq = dec.deinterleave_llrs(llr, t.cipm)
    bits, _ = dec.turbo_decode(Ls, Lp, Lq, t.perm.astype(np.int64),
                               iters, dec.NXT, dec.PAR)
    df2, p2, q2 = turbo_encode(bits[:t.D], t.perm)
    slots = map_to_symbols(df2, p2, q2, t.cipm)
    hard = (llr < 0).astype(np.uint8)
    sel = t.cipm.kind != 0
    ag = float(np.mean(slots[sel] == hard[sel]))
    return bits[:t.D], ag


def main():
    path = sys.argv[1]
    # This tool has no likelihood-ratio check, so it keeps the old, tighter
    # threshold. decode_wav.ACCEPT_AGREEMENT is 0.70 only because it is
    # paired with verify_block; 0.70 on agreement alone would false-accept.
    thr = float(sys.argv[2]) if len(sys.argv) > 2 else 0.90
    z = np.fromfile(path, dtype=np.complex64).astype(np.complex128)
    phi, lvl, psr = find_frame_offset(z[:60*MOS])
    print(f"{len(z)} symbols; frame offset {phi}, UW level {lvl}, fold-PSR {psr:.1f}")
    nfr = (len(z) - phi)//MOS
    out, nok, ntot = [], 0, 0
    for f in range(nfr):
        fb = phi + f*MOS
        for b in range(8):
            blk = prepare(z, fb, b)
            if blk is None:
                continue
            bits, ag = decode_block(blk, lvl)
            ntot += 1
            if ag > thr:
                nok += 1
                out.append(mod.descramble(bits))
    print(f"{nok}/{ntot} blocks with parity agreement > {thr}")
    if out:
        payload = np.packbits(np.concatenate(out)).tobytes()
        Path("work/real_payload.bin").write_bytes(payload)
        print(f"wrote work/real_payload.bin ({len(payload)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --- per-frame frame tracking -------------------------------------------------
# The frame position is NOT at a fixed stride. On BGAN19 it drifts about
# -2.2 symbols/frame (the differential-UW fold peak moves 9184 -> 8249 across
# the capture), so decoding at a fixed base only works for a handful of frames
# near wherever the base was measured. Confirmed: at base 8712 only frames
# ~150-180 decode; frames 200-400 all sit at chance.
#
# So track it. Per frame, search a window around the prediction using the
# differential UW correlation over all candidate levels, then update the
# predicted period with a second-order loop.

def track_frames(z, nframes=None, phi0=None, win=120, levels=None):
    """Returns list of (frame_base, level, metric)."""
    levels = levels or spec.LEVELS_F80T45X8B
    d = np.empty_like(z)
    d[0] = 0
    d[1:] = z[1:]*np.conj(z[:-1])
    pats = {l: diff_uw(l) for l in levels}

    if phi0 is None:
        phi0, lvl0, _ = find_frame_offset(z[:60*MOS], levels)
    nframes = nframes or (len(z) - phi0)//MOS - 1

    def score(p):
        best = (0.0, levels[0])
        seg = d[p:p+40]
        if len(seg) < 40:
            return best
        for l, w in pats.items():
            m = abs(np.dot(w, seg[1:40]))
            if m > best[0]:
                best = (m, l)
        return best

    out = []
    base, period, drift = float(phi0), float(MOS), 0.0
    # threshold from off-frame positions, so it adapts to this capture
    ref = np.median([score(int(phi0 + k*MOS + MOS//3))[0]
                     for k in range(min(40, nframes))])
    thr = 3.0*ref
    for f in range(nframes):
        pred = base + (0.0 if f == 0 else period)
        ip = int(round(pred))
        cand = []
        for off in range(-win, win+1):
            p = ip + off
            if p < 1 or p + MOS >= len(z):
                continue
            m, l = score(p)
            cand.append((m, p, l))
        if not cand:
            break
        m, p, l = max(cand)
        if m > thr:
            err = p - pred
            if f > 0 and abs(err) < win:
                period += 0.10*err
                drift += 0.02*err
                period += drift
            base = float(p)
            out.append((p, l, float(m)))
        else:
            base = pred
            out.append((int(round(pred)), None, float(m)))
    return out
