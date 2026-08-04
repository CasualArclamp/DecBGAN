"""Spot-beam IDs and the places they cover.

A BulletinBoard names its spot beam by number and nothing else, so turning
"beam 134" into "Brisbane" needs a table. Two ways to build one:

**From the broadcast, in principle.** TS 102 744-3-1 clause 5.4.10 defines a
SpotBeamMap AVP carrying, per beam, a polygon of SpotbeamVertex values:

    SpotBeamVertexValue = 360(lat + 90) + lon + 180        clause 5.4.10.3.4

so the network transmits the beam geography outright. **This has now been
caught**, on the 25.6 MB payload of the 10 minute 1534.500 MHz capture, and
`spot_beam_map` below parses it. The record is 15 octets:

    <beam-id:8> 0x0c 0x06 <6 x SpotbeamVertex>

Seven beams arrive, twice each with identical geometry: the serving beam 134
and exactly its six neighbours, 119/120/133/135/147/148. Brisbane falls
inside 134's polygon by point-in-polygon test, so the broadcast confirms the
reported entry below rather than merely being consistent with it.

The beams tile as hexagons on a lattice: three longitude columns near +147,
+154 and +161, ids stepping +1 north within a column and +14 between columns
(119 -> 133 -> 147, 120 -> 134 -> 148). Shorter captures found nothing --
5.1 MB and 7.7 MB payloads yielded no records -- so catching the map needs a
long capture, and only the local neighbourhood is sent, not the whole map.

**From observation, in practice.** `beams.json` maps beam IDs to place names,
and every decode appends what it saw to an observation log, so the map builds
itself as captures accumulate. Entries are labelled with where they came
from; nothing here is inferred from the signal, and an unknown beam is
reported as unknown rather than guessed at.
"""
from __future__ import annotations

import json
import os
from collections import Counter
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

    Superseded by `spot_beam_map`, and kept only as the exploratory search
    that found the record format. Random bytes were the wrong control: real
    payload is full of low-value structure, and this returns 1154 runs on the
    25.6 MB 1534.500 payload against 1 on random bytes of the same size, of
    which exactly 14 are real. Use `spot_beam_map`, which keys on the record
    header instead of on clustering and scores 0 on the same control.
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


# A SpotBeamMap record, as measured on the 1534.500 MHz / 10 min capture:
#
#     <beam-id:8> 0x0c 0x06 <6 x SpotbeamVertex, 2 octets each>
#
# 0x0c is the vertex-list length in octets and 0x06 the vertex count, so the
# record describes its own size and can be checked rather than trusted. The
# count is fixed at six here because a spot beam is a hexagon; a map using
# another vertex count would need MAP_NVERT relaxed, and none has been seen.
MAP_NVERT = 6
MAP_HDR = bytes((2*MAP_NVERT, MAP_NVERT))       # 0x0c 0x06
MAP_LEN = 1 + len(MAP_HDR) + 2*MAP_NVERT        # 15 octets


def spot_beam_records(payload, box=25.0, min_distinct=4):
    """[(offset, beam_id, [(lat, lon)] * 6)] for every SpotBeamMap record.

    Three independent conditions have to hold at once, which is what makes a
    chance match improbable: the two header octets, six legal SpotbeamVertex
    values, and a cluster no wider than `box` degrees. Measured on the 25.6 MB
    1534.500 payload: 14 records, 7 beams each appearing exactly twice with
    identical geometry, against 0 on random bytes of the same size.
    """
    b = np.frombuffer(payload, dtype=np.uint8)
    if len(b) < MAP_LEN:
        return []
    tail = MAP_LEN - 2
    cand = np.flatnonzero((b[1:-tail] == MAP_HDR[0])
                          & (b[2:-tail + 1] == MAP_HDR[1]))
    out = []
    for i in cand.tolist():
        v = np.frombuffer(payload[i + 3:i + MAP_LEN], dtype=">u2")
        pts = [vertex(int(x)) for x in v]
        if any(p is None for p in pts) or len(set(v.tolist())) < min_distinct:
            continue
        la = [p[0] for p in pts]
        lo = [p[1] for p in pts]
        if max(la) - min(la) > box or max(lo) - min(lo) > box:
            continue
        out.append((i, int(b[i]), list(zip(la, lo))))
    return out


def spot_beam_map(payload, **kw):
    """{beam_id: polygon} from the broadcast map, majority shape per beam.

    This is the authority the hand-maintained table was standing in for. On
    the 1534.500 capture it returns the serving beam plus its six immediate
    neighbours -- 134 and {119, 120, 133, 135, 147, 148} -- which is what a UE
    needs to hand over and is presumably why only seven are sent.
    """
    seen = {}
    for _, bid, pts in spot_beam_records(payload, **kw):
        seen.setdefault(bid, Counter())[tuple(pts)] += 1
    return {b: list(c.most_common(1)[0][0]) for b, c in seen.items()}


def contains(polygon, lat, lon):
    """Is (lat, lon) inside `polygon`? Ray casting.

    Vertices are whole degrees -- SpotbeamVertexValue has no fractional part
    -- so this is coarse by construction and a point within a degree of an
    edge should be treated as undecided rather than as an answer.
    """
    inside = False
    n = len(polygon)
    for i in range(n):
        y0, x0 = polygon[i]
        y1, x1 = polygon[(i + 1) % n]
        if (y0 > lat) != (y1 > lat):
            if lon < x0 + (lat - y0)*(x1 - x0)/(y1 - y0):
                inside = not inside
    return inside


def locate(lat, lon, payload=None, table=None):
    """Beam ids whose broadcast polygon contains (lat, lon)."""
    m = table if table is not None else (
        spot_beam_map(payload) if payload else {})
    return sorted(b for b, p in m.items() if contains(p, lat, lon))


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
