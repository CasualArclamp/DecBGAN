# Spot beams: adding your own

A BulletinBoard names its spot beam by number and nothing else. Turning
`spot-beam-id 134` into `Brisbane` needs a table, and that table is only as
good as the beams people put in it.

There are two places a beam can come from, and the code keeps them apart on
purpose so an observed fact and a guess never blur together.

## Where the numbers come from

Decode a capture and the GUI prints the beam in the **Bearer control** tab:

```
spot-beam-id       134 -- Brisbane
```

If it says `not in beams.json; add it to name this area`, that beam is
unknown and worth adding.

The number is broadcast in the BulletinBoard SDU at the head of FEC block 0,
so any capture that decodes at all will show it. It does not depend on
demodulating traffic.

## Adding a beam

**Do not edit `beams.json`.** Copy the example and edit the copy:

```bash
cp beams.local.json.example beams.local.json
```

```json
{
  "beams": {
    "118": { "name": "Melbourne", "source": "observed",
             "note": "1545.1 MHz, rnc 29" }
  }
}
```

`beams.local.json` is gitignored. It survives every `git pull`, never
conflicts with the shared table, and is not published by accident — a list of
the beams you can hear is a coverage map of where you listen from, which is
yours to share or not.

Entries there win over `beams.json` for the same id, so you can also correct
an entry you think is wrong without waiting for anyone.

Only `name` is required.

## The `source` field

This is the point of the whole file. Say where the entry came from:

| source | meaning |
|---|---|
| `broadcast` | decoded from a SpotBeamMap AVP. Authoritative. |
| `observed` | you worked it out from your own decodes and your own location |
| `reported` | someone told you |
| `local` | the default for `beams.local.json` if you leave it out |

`broadcast` entries carry their polygon centre and the places that fall
inside it by point-in-polygon test. Do not label an entry `broadcast` unless
`bgan.beams.spot_beam_map` actually returned it — a guess that looks
authoritative is worse than no entry.

## Getting the map from the signal instead

The network transmits the geography outright. TS 102 744-3-1 clause 5.4.10
defines a SpotBeamMap AVP whose vertices are

```
SpotBeamVertexValue = 360(lat + 90) + lon + 180
```

and `bgan/beams.py:spot_beam_map` parses it:

```python
from bgan import beams
raw = open("out/<capture>_payload.bin", "rb").read()
beams.spot_beam_map(raw)          # {beam_id: [(lat, lon)] * 6}
beams.locate(-27.47, 153.03, table=...)   # which beams contain a point
```

Two things to expect. It needs a **long capture** — nothing turned up in
5.1 MB or 6.5 MB payloads, while a 25.6 MB payload from a 10 minute recording
carried it twice. And you only get the **serving beam plus its six
neighbours**, not the whole map, which is all a terminal needs in order to
hand over.

If you do catch one, that beats everything else in this file. Send it
upstream.

## Sending beams back

Open a PR that adds them to `beams.json`, with `source` set honestly and a
`note` saying which carrier and RNC you saw it on. Keep your
`beams.local.json` as well — it will simply shadow identical entries.

## What is in there now

Seven beams from a decoded SpotBeamMap on the 1534.500 MHz carrier
(119, 120, 133, 134, 135, 147, 148), tiling eastern Australia and the Coral
and Tasman Seas, plus 104 and 118 as `reported` around Melbourne.

Those seven sit on a lattice worth knowing if you are guessing at a
neighbouring beam: longitude columns near +147, +154 and +161, with ids
stepping **+1 north within a column** and **+14 between columns**
(119 → 133 → 147, 120 → 134 → 148). It predicts where an unseen beam should
be — but a prediction is a `note`, not a `name`.
