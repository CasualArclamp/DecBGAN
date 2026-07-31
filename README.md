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
  recovery, frame acquisition and tracking, pilot-aided phase correction
- **FEC** — the full SRCC turbo codec: Annex C.1 turbo interleaver, Annex C.2
  puncturing / channel interleaving / QAM mapping, max-log-MAP BCJR decoding
  at all ten coding levels (L8…H6)
- **Bearer control** — BulletinBoard SDU parsing, AVP list walking,
  `ForwardBearerCodeRateParam` extraction (TS 102 744-3-1)
- **Output** — printable-string extraction, IPv4 carving with header-checksum
  validation, pcap export
- **GUI** — spectrum, live constellation, carrier/bearer info, strings, packets

## What it does *not* do

Being precise about this, because the gap is real:

- **~39% of blocks decode** over a whole capture (86% over a clean window).
  The rest are lost to fades and frames that never lock.
- **No protocol demux.** Payload is decoded FEC blocks concatenated, with
  silent gaps where blocks failed. IP packets are *carved* by scanning for
  valid IPv4 headers, not reassembled. Doing it properly needs
  TS 102 744-3-2 … 3-8 (logical channels, RLC/MAC), which are not public.
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

```bash
python tools/gui.py path/to/capture.wav
```

Spectrum and live constellation, a progress bar, carrier and bearer-control
readouts, then tabs for strings, parsed BulletinBoards, carved packets and a
log. Export buttons produce a lossless block pcap, a carved-IPv4 pcap, or the
raw payload.

Leave **search levels** on. Off is ~10× faster but only recovers block 0,
which is about a quarter of the data.

### Command line

```bash
python tools/decode_wav.py capture.wav              # decode, write payload
python tools/parse_bulletin.py work/payload.npz     # bearer control
python tools/scan_bearers.py capture.wav            # what carriers are present
python tests/waterfall.py                           # codec vs Annex B2
```

### Capture settings

Recorded with an RTL-SDR v4 and an L-band patch antenna, via SDR++:

- **Sample rate** ≥ 512 kHz (192 kHz works but leaves only ~1.5 kHz of guard
  either side of a 189 kHz signal, which clips the RRC excess band)
- **Format** 16-bit stereo WAV (SDR++ baseband recording)
- BGAN forward carriers sit around 1518–1559 MHz

---

## Validation

Full detail in [`docs/VALIDATION.md`](docs/VALIDATION.md). Three independent
lines of evidence, summarised:

**1. Codec against the standard's own numbers.** `tests/waterfall.py` finds
the Es/N0 at 50% block error for each coding level and compares with Annex B2:
mean margin **+1.95 dB**, range +1.62 to +2.69, flat across levels.

**2. Traffic that cannot come from noise.** Decoding a real capture recovers a
DER-valid X.509 chain (`http://crl3.digicert.com/DigiCertGlobalRootG2.crl`,
correct SEQUENCE tags, extension OIDs, the DigiCert CPS arc), a well-formed
HTTP request, and coherent application data.

**3. A counter the network maintains, not us.** The BulletinBoard's 12-bit
`frame-no` must advance one per 80 ms frame, so `(frame_no - frame_index) mod
4096` is constant. On one capture: **13/13** on-cycle frames matched,
**0/208** off-cycle, 0.054 expected by chance — and the hits fall on a strict
**17-frame cycle** that appears nowhere in the code. This reproduces on three
captures at two sample rates.

Synthetic loopback regression: **4149/4149 blocks bit-exact, zero false
positives.**

---

## Layout

```
bgan/
  spec.py       bearer definitions, unique words, constants
  annex.py      Annex C.1/C.2 table loaders
  turbo.py      SRCC encoder, termination
  decoder.py    max-log-MAP BCJR, soft demapper (numba)
  mod.py        16-QAM, scrambler, frame assembly
  recv.py       channelisation, timing, carrier, frame sync
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

This decodes a satellite downlink you can receive with a €30 SDR. Whether you
may lawfully receive, decode, record or act on such transmissions depends on
where you are — many jurisdictions restrict interception of communications not
intended for you, regardless of how easy they are to pick up. Check your local
law.

No decoded traffic is included in this repository, and none should be added.
Recovered payloads contain other people's communications.

Published for research and interoperability: BGAN is a documented ETSI
standard with no public open-source receiver.

## Licence

MIT — see [`LICENSE`](LICENSE). ETSI specifications are separately copyright
ETSI and are not covered by it.
