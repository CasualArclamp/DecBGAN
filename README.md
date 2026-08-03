# BGAN forward-link decoder

A receiver for the Inmarsat BGAN forward bearer **F80T4.5X-8B** (151.2 kBd
16-QAM, 189 kHz, 80 ms frames), written against ETSI TS 102 744. It takes an
IQ recording and produces decoded payload bytes, parsed bearer-control
messages, and a pcap.

Written in Python with numba for the turbo decoder. No compiler needed.

![status](https://img.shields.io/badge/physical%20layer-validated-brightgreen)
![status](https://img.shields.io/badge/IP%20demux-carve%20only-orange)

---

## What it does

- **Front end** — channelisation, RRC matched filtering, symbol-clock
  recovery, frame acquisition and tracking, residual carrier-offset removal
  measured on the unique words, pilot-aided phase correction
- **FEC** — the full SRCC turbo codec: Annex C.1 turbo interleaver, Annex C.2
  puncturing / channel interleaving / QAM mapping, max-log-MAP BCJR decoding
  at all ten coding levels (L8…H6)
- **Bearer control** — FwdBCtPDU header decoding, BulletinBoard SDU parsing,
  AVP list walking,
  `ForwardBearerCodeRateParam` extraction (TS 102 744-3-1)
- **Output** — printable-string extraction, IPv4 carving with header-checksum
  validation, pcap export
- **GUI** — spectrum, live constellation, carrier/bearer info, strings, packets

## What it does *not* do

Being precise about this, because the gap is real:

- **~98% of blocks decode** on a good capture. Earlier versions managed 39%;
  the difference was a single global symbol-timing phase, which a recording
  with dropped samples invalidates (see docs/VALIDATION.md). Weak or
  interference-hit captures still do worse.
- **Captures down to ~5 dB Es/N0 decode**, where previously nothing below
  9 dB did. Four separate defects caused that, all found in Aug 2026: a
  biased spectral-centroid carrier estimate that left several hundred Hz of
  offset — enough to break the pilot phase unwrap and kill every block in a
  frame; a block-acceptance threshold placed inside the range that correct
  decodes occupy rather than above the range that wrong ones do; a carrier
  probe that judged candidates at a single timing phase; and a noise-floor
  measurement taken inside the very window used to find the signal, which
  rejected outright any capture whose framing put its unique word there.
  Six captures that decoded nothing now decode, four of them above 88%. See
  docs/SIGNAL_NOTES.md and docs/VALIDATION.md.
- **No protocol demux.** Payload is decoded FEC blocks concatenated, with
  silent gaps where blocks failed. IP packets are *carved* by scanning for
  valid IPv4 headers, not reassembled. The layers that specify how to do it
  properly (TS 102 744-3-3/-3-4 Bearer Connection, -3-5/-3-6 Adaptation)
  are freely available from ETSI; wiring them up is the next substantial
  job.
- **Forward link only.** Nothing on the return direction.
- **One bearer type.** F80T4.5X-8B. Others are defined in `bgan/spec.py` but
  untested.

---

## Install

```bash
pip install numpy scipy numba matplotlib
```

`tkinter` ships with CPython on Windows and macOS; on Debian/Ubuntu install
`python3-tk`.

### ETSI tables (required)

The decoder needs the Annex C data tables, which are ETSI copyright and are
**not** in this repository. They are free to download:

1. Go to <https://www.etsi.org/standards> and search for **TS 102 744-2-1**
2. Download the annex archives:
   - `ts_1027440201_AnnexC1_v010101p0.zip` — turbo interleaver (`*_TCI.TXT`)
   - `ts_1027440201_AnnexC2_v010101p0.zip` — puncturing/mapping (`*_CIPM.TXT`)
3. Extract each so you have these directories in the repository root:

```
ts_1027440201_AnnexC1_v010101p0/
ts_1027440201_AnnexC2_v010101p0/
```

`bgan/annex.py::annex_dir` also looks in `work/` and `annex/`.

For the bearer-control layer you additionally want **TS 102 744-3-1** (the
PDF), from the same place.

---

## Use

### GUI

Double-click **`start.cmd`** on Windows, or run **`./start.sh`** on
Linux/macOS. Either one checks for a working Python, installs the
dependencies on first run, warns if the ETSI tables are missing, and opens the
GUI. Both pass arguments through, so `start.cmd C:\path\capture.wav` opens
with that file already selected.

Or invoke it directly:

```bash
python tools/gui.py path/to/capture.wav
```

Spectrum and live constellation, a progress bar, carrier and bearer-control
readouts, then tabs for strings, parsed BulletinBoards, carved packets and a
log. Export buttons produce a lossless block pcap, a carved-IPv4 pcap, or the
raw payload; each is named after the capture it came from, so exports from
different recordings do not collide.

Selecting a capture reads its WAV header (instantly, no samples touched) and
shows length, sample rate, size, frame/block count and an estimated decode
time. **Max** fills in the full length; asking for more than the file holds is
clamped and logged.

Leave **search levels** on. Off is ~9x faster but only recovers block 0,
which is about an eighth of the data.

**Scan** runs the front end, framing and timing search but no turbo decoding —
about a ninth of the cost of a full decode (16 s vs 145 s on a 39 s capture).
It reports which unique words are present and where, how the framing and
timing move, and a conservative yield forecast, so a long capture can be
triaged before you commit to it.

Decode cost on the development machine is about **1.5 s of compute per second
of capture**, so a 60 s capture is about 90 seconds. Coding levels are read
from the BCtPDU layer on ~88% of frames and only searched on the rest.
The estimate shown updates when you toggle level search.

### Command line

```bash
python tools/decode_wav.py capture.wav --survey     # fast scan, no decoding
python tools/decode_wav.py capture.wav              # -> work/<capture>_payload.bin
python tools/parse_bulletin.py work/payload.npz     # bearer control
python tools/scan_bearers.py capture.wav            # what carriers are present
python tests/waterfall.py                           # codec vs Annex B2
```

`--survey` prints the unique words seen, the framing runs with their offsets
and timing phases, the UW-metric distribution and a yield forecast, in about a
ninth of the time a full decode takes.

### Capture settings

Recorded with an RTL-SDR v4 and an L-band patch antenna, via SDR++:

- **Sample rate** ≥ 512 kHz (192 kHz works but leaves only ~1.5 kHz of guard
  either side of a 189 kHz signal, which clips the RRC excess band)
- **Format** 16-bit stereo WAV (SDR++ baseband recording)
- BGAN forward carriers sit around 1518–1559 MHz

---

## Validation

Full detail in [`docs/VALIDATION.md`](docs/VALIDATION.md). Five independent
lines of evidence, summarised:

**1. Codec against the standard's own numbers.** `tests/waterfall.py` finds
the Es/N0 at 50% block error for each coding level and compares with Annex B2:
mean margin **+1.95 dB**, range +1.62 to +2.69, flat across levels.

**2. Traffic that cannot come from noise.** Decoding a real capture recovers a
DER-valid X.509 chain (`http://crl3.digicert.com/DigiCertGlobalRootG2.crl`,
correct SEQUENCE tags, extension OIDs, the DigiCert CPS arc), a well-formed
HTTP request, and coherent application data. BGAN15, recovered in Aug 2026
from a capture that previously reported no carrier at all, independently
yields `HEAD / HTTP/1.0`, `User-Agent: WhatsUp/1.0` — the same user agent as
the first capture, so the same network — plus `victronenergy`, an
`ST=California` certificate subject fragment, dozens of lines of consistent
ASCII-art logo, and a complete four-header HTTP response whose
`Content-Type: application/pkix-crl` is exactly the MIME type for the
certificate revocation lists the other capture was fetching.

**3. A counter the network maintains, not us.** The BulletinBoard's 12-bit
`frame-no` must advance one per 80 ms frame, so `(frame_no - frame_index) mod
4096` is constant. On one capture: **13/13** on-cycle frames matched,
**0/208** off-cycle, 0.054 expected by chance — and the hits fall on a strict
**17-frame cycle** that appears nowhere in the code. This reproduces on three
captures at two sample rates.

Synthetic loopback regression: **4149/4149 blocks bit-exact, zero false
positives.**

**4. Independent checks scale with yield.** Fixing per-frame symbol timing
took a real capture from 39.2% to 98.5% of blocks. The BulletinBoard count
rose 13 -> 28 (of 29 possible), the frame-no offset and 17-frame period were
unchanged, and AVP-predicted coding levels went from 103/0 to 219/0
agree/disagree. A decoder manufacturing blocks would not do that.

The same test carried the Aug 2026 threshold change. On `1553.500`, yield went
from 15 blocks to 1721; its BulletinBoard went from unmeasurable to **13/232**
on-cycle on a strict 17-frame period, and AVP-predicted levels from nothing to
**747 agree / 0 disagree**. On `1547.298`, 331 blocks -> 1887 and AVP agreement
125/0 -> **1441/0**. The two captures that gain no blocks are bit-identical on
every column.

**5. The acceptance test is calibrated against a labelled negative set.**
Correct decodes (ground truth from the synthetic generator, Es/N0 5-12 dB)
never scored below 0.7585 parity agreement; 37281 blocks that *cannot* decode
— wrong frame offset, shifted offset, matched-power Gaussian noise, drawn
from synthetic and real captures alike — never scored above 0.6023. The
threshold sits in that gap, with zero false accepts. See docs/VALIDATION.md.

---

## Layout

```
bgan/
  __init__.py   package docstring and submodule map
  spec.py       bearer definitions, unique words, constants
  annex.py      Annex C.1/C.2 table loaders
  turbo.py      SRCC encoder, termination
  decoder.py    max-log-MAP BCJR, soft demapper (numba)
  mod.py        16-QAM, scrambler, frame assembly
  recv.py       channelisation, timing, carrier, frame sync
  carrier.py    residual carrier offset, from the unique words
  pipeline.py   end-to-end synchronise + decode
  bctrl.py      code-rate AVP, per-block level resolution
  bulletin.py   BulletinBoard SDU, AVP list walking
  pcapout.py    pcap writers, IPv4 carving
  tx.py         reference transmitter (for validation)
tools/
  gui.py, decode_wav.py, parse_bulletin.py, scan_bearers.py,
  find_framenum.py, find_counter.py, make_test_iq.py
tests/
  waterfall.py, constellation.py
docs/
  VALIDATION.md, SIGNAL_NOTES.md, OPEN_QUESTIONS.md
```

[`docs/OPEN_QUESTIONS.md`](docs/OPEN_QUESTIONS.md) tracks what the spec does
not pin down, what was resolved and how, and what is still guesswork.

---

## Legal and ethical

This decodes a satellite downlink you can receive with a $30 SDR. Whether you
may lawfully receive, decode, record or act on such transmissions depends on
where you are — many jurisdictions restrict interception of communications not
intended for you, regardless of how easy they are to pick up. Check your local
law.

No decoded traffic is included in this repository, and none should be added.
Recovered payloads contain other people's communications.

Published for research and interoperability: BGAN is a documented ETSI
standard with no public open-source receiver.

## Licence

MIT — see [`LICENSE`](LICENSE).

ETSI specifications are separately copyright ETSI, are not covered by that
licence, and are not distributed here. See [`NOTICE`](NOTICE).
