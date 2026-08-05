# Command-line use

The GUI (`start.cmd` / `start.sh`) is the easier way in. The CLI is what you
want for batch work, for a headless machine, and for anything you need to
script or repeat.

Everything below is run from the repository root. On Linux/macOS with a venv
built by the launcher, `python` means `.venv/bin/python`.

---

## The short version: WAV in, pcap out

```bash
python tools/decode_wav.py capture.wav --pcap
```

That decodes the whole capture and writes, next to the payload:

| file | what it is |
|---|---|
| `work/<capture>_payload.bin` | decoded block payloads, concatenated |
| `work/<capture>_payload.npz` | the same bits *with* frame/block/level provenance |
| `work/<capture>_ipv4.pcap` | IPv4 packets carved from the payload |

Measured on a 25 s slice of a 10 minute capture: 2335 of 2488 blocks (93.9%),
1125 carved packets — 628 TCP, 363 UDP, 132 ICMP, 2 GRE.

### Read the pcap

```bash
wireshark work/<capture>_ipv4.pcap
```

It is a **raw-IP** pcap (DLT_RAW, linktype 101) — no Ethernet header, because
there was never one. Wireshark handles this natively.

### What the carve does and does not claim

A candidate is accepted only if its **IPv4 header checksum is valid**, which
is a 16-bit constraint, so false accepts run at roughly 2^-16 per offset — a
handful on a large payload, not thousands.

It is still a carve, not a demux. Without TS 102 744-3-2..3-8 there is no
RLC/MAC layer here, so a packet split across FEC blocks, or landing in a block
that did not decode, is simply gone. Treat it as evidence, not as a faithful
record of the link. If a bearer carries no IP at all you get an empty pcap and
a line saying so — that is a real answer, not a failure.

---

## Before a long decode: survey first

A full decode of a 10 minute capture takes minutes. A survey takes seconds and
tells you whether it is worth it.

```bash
python tools/decode_wav.py capture.wav --survey
```

It runs the front end and the framing/timing search but no turbo decoding, and
reports the carrier offset, symbol rate in ppm, Es/N0, which unique words are
present, how the timing phase moves, and a yield forecast. The forecast is
deliberately conservative — on one capture it said 92% and the decode returned
98.5%.

If it forecasts near zero, the capture is not going to decode and the survey
just saved you the wait.

---

## Which carrier is even in the file

```bash
python tools/scan_bearers.py capture.wav [more.wav ...]
```

With no arguments it scans every `*.wav` in the current directory, or in
`$BGAN_CAPTURES`. For each carrier it finds, it reports centre, bandwidth,
SNR, the best-matching bearer by symbol-rate tone, and an M4/M2² modulation
estimate (QPSK 1.00, 16-QAM 1.32, noise 2.00).

Worth running when `decode_wav.py` exits with "no F80T4.5X-8B carrier found" —
it will tell you what *is* there.

---

## Every option

```
python tools/decode_wav.py CAPTURE.wav [options]
```

| option | effect |
|---|---|
| `--secs N` | decode only the first N seconds |
| `--survey` | fast triage, no turbo decoding (above) |
| `--pcap [PATH]` | carve IPv4 to a raw-IP pcap |
| `--pcap-blocks [PATH]` | every decoded FEC block as a pcap record |
| `--out PATH` | payload path; default `work/<capture>_payload.bin` |
| `--segment N` | decode in N-second segments; `0` forces whole |
| `--jobs N` | worker threads; default one per core, or `$BGAN_JOBS` |
| `--level L` | force a coding level instead of searching |
| `--no-search-levels` | use the UW level for all 8 blocks — much faster, but blocks 1–7 mostly will not decode |
| `--ntau N` | timing phases searched per frame (default 8; `1` is the old single-phase behaviour) |
| `--thr F` | parity-agreement threshold, default 0.70 |
| `--no-cfo` | skip residual carrier-offset correction (diagnosis only) |

Two of those deserve a warning.

**`--thr`** was calibrated against ground truth: correct decodes bottom out at
0.7585 agreement, impossible blocks top out at 0.6023, and nothing at all
falls between. The default sits in that gap. Loosening it because it is
rejecting frames you believe in is how false positives get manufactured.

**`--ntau 1`** reproduces a real bug: one global timing phase for a whole
capture, which any recording with dropped samples invalidates. It is there for
comparison, not for use.

---

## The lossless pcap

```bash
python tools/decode_wav.py capture.wav --pcap-blocks
```

One record per decoded FEC block on DLT_USER0 (linktype 147), timestamped from
the frame index at 80 ms per frame, so block ordering and gaps stay visible.
Nothing is interpreted — this is what was actually recovered, before any
guessing about what it means.

Wireshark shows linktype 147 as `USER0` with no dissector, which is correct:
there is no standard dissector for BGAN FEC blocks. Use it to see structure,
timing and loss patterns rather than to read traffic.

---

## Long captures and memory

A decode costs roughly **49 MB of RAM per second of capture**, measured. A
25 minute recording decoded whole would need about 70 GB, so it is segmented
automatically once it will not fit in a ~4 GB budget:

```bash
python tools/decode_wav.py long.wav              # auto-segments if needed
python tools/decode_wav.py long.wav --segment 180   # force 3 minute segments
python tools/decode_wav.py long.wav --segment 0     # force whole, whatever the cost
```

Segmenting re-estimates clock and framing in each segment but picks the
carrier once over the whole capture — re-picking it per segment was measured
as a net loss (96.8% yield fell to 47.7%). Frame indices stay continuous
across segments, so the BulletinBoard frame-number check still sees one
timeline.

Segment length is not free either way. Measured across sizes on a capture
decoding 6257 blocks whole: 10 s gave 99.2%, 30 s 101.7%, 60 s 99.5%, 120 s
100.0%. Below about 20 s the carrier fit starts to fail.

---

## Input formats

Any WAV of **2-channel interleaved IQ**:

- 8/16/24/32-bit PCM, 32/64-bit float
- plain or `WAVE_FORMAT_EXTENSIBLE`
- an `auxi` chunk before `data` (SDR#, SDRuno) is skipped correctly

Sample rate is arbitrary — the front end resamples to 4 samples/symbol through
a gcd, so 192 kHz, 512 kHz and 2.048 MHz all work without a flag.

Header only, no decode:

```bash
python -c "import sys;sys.path.insert(0,'.');from bgan import recv;i=recv.wav_info('capture.wav');print(i.sr,'Hz',i.format_name,f'{i.secs:.1f} s')"
```

---

## Bearer control from a decode

`--pcap` gives you the user plane. The control plane comes out of the `.npz`:

```bash
python tools/parse_bulletin.py work/<capture>_payload.npz
```

This locates BulletinBoard SDUs by their frame-number counter, parses the AVP
list, and cross-checks the `ForwardBearerCodeRateParam` against the coding
levels the decoder found independently by trial decode. That cross-check is
the point — a structural parse and a brute-force search over ten levels have
nothing in common, so agreement between them means something.

`--avps N` controls how many leading AVPs are shown. The walk runs past the
real end of the list; see `docs/VALIDATION.md`.

---

## The carvers the GUI has

Spot beams, SIP, RTP and terminal addresses are GUI tabs with no CLI flag yet.
They all take the payload bytes, so they are one-liners against a
`_payload.bin`:

```python
import sys; sys.path.insert(0, ".")
from bgan import beams, sip, terminals
raw = open("work/capture_payload.bin", "rb").read()

beams.spot_beam_map(raw)      # {beam_id: [(lat, lon)] * 6} from a SpotBeamMap
sip.scan(raw)                 # (messages, dialogs), reassembled
terminals.terminals(raw)      # terminal IPs, with a confidence per address
```

On a 25 s payload those give 2 SIP messages in 1 dialog and 28 terminals.
`spot_beam_map` returns `{}` there — the map is broadcast on a long cycle and
needs a capture of several minutes.

See `docs/BEAMS.md` for what the beam map does and does not cover.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | decoded (possibly 0 blocks — check the yield line) |
| 2 | no F80T4.5X-8B carrier found; the probe table is printed |

`2` is deliberately not `1`: a capture holding only 33.6 kBd bearers used to
decode to noise and still report a 97% yield forecast, so "no carrier here" is
now a distinct, loud answer.

---

## If it will not decode

**"no F80T4.5X-8B carrier found"** — run `scan_bearers.py` to see what is
actually in the file. Often it is a real capture of a different bearer.

**`FileNotFoundError: ts_1027440201_AnnexC1_v010101p0`** — the ETSI Annex C
tables are missing. They are ETSI copyright so they are not in this
repository, but they are a free download; see the README.

**0% blocks decoded with a healthy-looking survey** — look at the `UW EVM`
figure the survey prints on its carrier-offset line. It runs ~0.17 on captures
that decode and ~0.45 on captures that do not, and it is the one statistic
that sees a residual carrier offset the UW metric cannot: captures scoring
60–72 on the UW metric have decoded nothing at all.
