"""Spot-beam IDs and the places they cover.

A BulletinBoard names its spot beam by number and nothing else, so turning
"beam 134" into "Brisbane" needs a table. Two ways to build one:

**From the broadcast, in principle.** TS 102 744-3-1 clause 5.4.10 defines a
SpotBeamMap AVP carrying, per beam, a polygon of SpotbeamVertex values:

    SpotBeamVertexValue = 360(lat + 90) + lon + 180        clause 5.4.10.3.4

so the network transmits the beam geography outright. `vertex_runs` below
searches a payload for it. It has not been caught yet: scanning the 5.1 MB
1538.099 and 7.7 MB 1534.499 payloads found no clustered vertex run anywhere
near Australia, which is unsurprising -- the full map is large and broadcast
on a much longer cycle than a two-minute capture. Worth re-running on a long
capture, because it would replace the table below with the authority.

**From observation, in practice.** `beams.json` maps beam IDs to place names,
and every decode appends what it saw to an observation log, so the map builds
itself as captures accumulate. Entries are labelled with where they came
from; nothing here is inferred from the signal, and an unknown beam is
reported as unknown rather than guessed at.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
TABLE = ROOT/"beams.json"
OBSERVATIONS = ROOT/"work"/"beam_observations.json"

# SpotbeamVertex, clause 5.4.10.3.4. Values at or above this are reserved.
VERTEX_RESERVED = 0xFE88


def vertex(v):
    """SpotbeamVertex -> (lat, lon) in degrees, or None if reserved."""
    if not 0 <= v < VERTEX_RESERVED:
        return None
    return v//360 - 90, v % 360 - 180


def vertex_runs(payload, minrun=5, box=25.0, min_distinct=4):
    """Runs of consecutive 16-bit values that decode to a tight coordinate
    cluster -- the shape a SpotBeamMap polygon would take.

    Validity alone filters nothing: 65160 of 65536 values decode to a legal
    coordinate. Clustering is the filter, and a constant run has to be
    excluded too or a stretch of zero padding scores as a perfect cluster at
    (-90, -180). Measured 0 runs over 5 MB of random bytes.
    """
    out = []
    for align in (0, 1):
        b = payload[align:]
        n = (len(b)//2)*2
        if n < 2*minrun:
            continue
        v = np.frombuffer(b[:n], dtype=">u2").astype(np.int32)
        lat, lon = v//360 - 90, v % 360 - 180
        ok = v < VERTEX_RESERVED
        near = ((np.abs(np.diff(lat)) <= box) & (np.abs(np.diff(lon)) <= box)
                & ok[:-1] & ok[1:])
        cut = np.flatnonzero(~near)
        for s, e in zip(np.r_[0, cut + 1], np.r_[cut + 1, len(v)]):
            if e - s < minrun or len(np.unique(v[s:e])) < min_distinct:
                continue
            la, lo = lat[s:e], lon[s:e]
            if la.max() - la.min() > box or lo.max() - lo.min() > box:
                continue
            out.append((align + 2*s,
                        list(zip(la.tolist(), lo.tolist()))))
    return out


# --- the table --------------------------------------------------------------

def load(path=None):
    """{beam_id: {"name":..., "source":..., "note":...}}. {} if absent."""
    p = Path(path or TABLE)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {int(k): v for k, v in d.get("beams", {}).items()
            if str(k).isdigit()}


def name(beam_id, path=None):
    """Place name for a beam, or None. Never guesses."""
    e = load(path).get(beam_id)
    return e.get("name") if e else None


def describe(beam_id, path=None):
    """'134 (Brisbane)' or '122 (unmapped)'."""
    if beam_id is None:
        return "none"
    n = name(beam_id, path)
    return f"{beam_id} ({n})" if n else f"{beam_id} (unmapped)"


# --- observations -----------------------------------------------------------

def record(beam_id, freq_hz=None, capture=None, rnc_id=None, bct_id=None,
           path=None):
    """Append one sighting. Keyed by (beam, freq, capture) so re-decoding the
    same file updates rather than duplicates."""
    if beam_id is None:
        return
    p = Path(path or OBSERVATIONS)
    try:
        obs = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        obs = []
    key = (beam_id, freq_hz, os.path.basename(capture or ""))
    obs = [o for o in obs
           if (o.get("beam"), o.get("freq_hz"),
               os.path.basename(o.get("capture") or "")) != key]
    obs.append(dict(beam=int(beam_id),
                    freq_hz=int(freq_hz) if freq_hz else None,
                    capture=os.path.basename(capture or "") or None,
                    rnc_id=rnc_id, bct_id=bct_id,
                    seen=datetime.now(timezone.utc)
                    .isoformat(timespec="seconds")))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obs, indent=1), encoding="utf-8")
    except OSError:
        pass
    return obs


def observations(path=None):
    try:
        p = Path(path or OBSERVATIONS)
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def by_beam(path=None):
    """{beam: {"name":..., "freqs":[...], "captures":n}} over all sightings."""
    out = {}
    tbl = load()
    for o in observations(path):
        b = o.get("beam")
        if b is None:
            continue
        e = out.setdefault(b, {"name": (tbl.get(b) or {}).get("name"),
                               "freqs": set(), "captures": 0})
        if o.get("freq_hz"):
            e["freqs"].add(o["freq_hz"])
        e["captures"] += 1
    for e in out.values():
        e["freqs"] = sorted(e["freqs"])
    return out
