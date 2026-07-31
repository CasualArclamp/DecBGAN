# Codec validation against Annex B2

`tests/waterfall.py` simulates each coding level over ideal AWGN with perfect
sync and finds the Es/N0 at which the block error rate crosses 50%. Annex B2
quotes required C/N0 *including* implementation loss, so a correct codec should
sit roughly 1-2 dB below it.

Run 30 July 2026, max-log-MAP, 8 iterations, extrinsic scaling 0.75:

| level | req C/N0 dBHz | req Es/N0 dB | sim threshold | margin |
|---|---|---|---|---|
| L3 | 56.8 | 5.00 | 3.22 | +1.79 |
| L2 | 57.7 | 5.90 | 4.28 | +1.62 |
| L1 | 58.7 | 6.90 | 5.22 | +1.69 |
| RE | 59.6 | 7.80 | 6.03 | +1.77 |
| H1 | 60.8 | 9.00 | 7.22 | +1.79 |
| H2 | 61.9 | 10.10 | 8.28 | +1.82 |
| H3 | 63.0 | 11.20 | 9.22 | +1.99 |
| H4 | 64.2 | 12.40 | 10.41 | +2.00 |
| H5 | 65.1 | 13.30 | 10.97 | +2.34 |
| H6 | 66.2 | 14.40 | 11.72 | +2.69 |

Mean margin +1.95 dB, range +1.62 to +2.69.

**Why this is convincing.** Before extrinsic scaling the first three levels
came out at +1.47, +1.50, +1.50 dB — within 0.03 dB of each other despite
completely different code rates, interleaver tables and puncturing patterns.
An error in the Annex C interleaving or in the puncturing decomposition would
produce erratic margins across levels, not a flat offset.

The upward drift at H5/H6 is expected: the spec allocates more implementation
margin at high code rates, where a real receiver is far more sensitive to
phase noise and EVM than the ideal AWGN simulated here.

## What this does and does not prove

Proves: the Annex C.1 turbo interleaver, the Annex C.2 puncturing/channel
interleaving decomposition, the SRCC polynomials and termination, the 16-QAM
mapping, the soft demapper, and the turbo decoder are mutually consistent and
perform as the standard requires.

Does not prove: that the frame sync, timing recovery or carrier recovery are
right — none of those are exercised here. Nor does it prove the scrambler
phase is right, since encode and decode share the same scrambler.

## Regression value

Re-run this after any change to the coding chain. A margin that goes negative,
or a spread that stops being flat across L3/L2/L1, means something in the
coding chain broke.

```bash
python tests/waterfall.py
```

## Real-signal validation — BGAN19.wav, decoded end to end

The strongest evidence the chain is correct, because it does not depend on any
of our own assumptions. Decoding 39.4 s of `E:/SDRPPrecordings/BGAN19.wav`
through `tools/decode_wav.py` recovers, in clear:

    http://cacerts.digicert.com/DigiCertGlobalRootG2.crt
    http://crl3.digicert.com/DigiCertGlobalRootG2.crl
    http://ocsp.digicert.com

embedded in correct DER: `0x30` SEQUENCE tags with consistent lengths, X.509v3
extension OIDs (`06 03 55 1D ..`), the OCSP access-method OID and the DigiCert
CPS arc `60 86 48 01 86 FD 6C`. That is an X.509 certificate chain from a TLS
handshake. Separately, a well-formed HTTP request:

    HTTP/1.0\r\nAccept: */*\r\nUser-Agent: WhatsUp/1.0\r\n\r\n

Neither can arise from noise. A wrong descrambler phase, a wrong interleaver
or an off-by-one anywhere in the chain destroys them completely.

Both came from **frame block 2 at level H4** — blocks that did not decode at
all before per-block level search was added.

### Three findings that changed the design

**1. The "-220 ppm frame drift" was self-inflicted.** It came from consuming
another tool's *resampled* demod output. On the raw capture the symbol clock
measures **-0.2 ppm** over the full 39.4 s (`|x|^2` line at 151199.965 Hz,
frame period 12095.997 symbols against a nominal 12096). No tracking PLL is
needed and fitting a period is actively wrong.

**2. The frame offset is piecewise constant, and every step is negative.**
Plateaus of 20-60 frames at a fixed offset, then a jump: -153, -97, -14, -77,
-132, -229, -35, -35, -70, -27, -14 ... Dropped samples can only lose time,
never gain it, so this is almost certainly SDR++ buffer overruns during
recording rather than anything in the signal. `track_offsets` handles it by
searching a window around the previous frame's offset, never by extrapolating
a period.

**3. Blocks 1-7 use different coding levels from block 0.** At the
UW-signalled level, block 0 decoded 68% and blocks 1-7 decoded 0-14%. The
giveaway that this was not a sync problem: block *1* was the worst of the
seven and blocks 5-7 the best -- the opposite of what degradation with
distance from the unique word would produce.

Trial-decoding all ten levels is safe here, and this is worth stating
precisely because it is the kind of search that usually manufactures false
positives: over ~480 block-tries at 10 levels each, **no block ever passed at
two levels**. Zero ambiguity. A unique passer therefore identifies the level
rather than guessing it. Result, per block index:

    fixed UW level : b0 68%   b1-b7  0-14%
    level search   : b0 87%   b1-b7 75-93%

Whole capture: **1541 / 3928 blocks (39.2%)**, flat across block indices
(b0 221, b1 192, b2 189, b3 192, b4 188, b5 194, b6 185, b7 180), median
parity agreement 0.965. The prior decoder recovered 222 blocks, essentially
all block 0 -- which matches our b0 count of 221 almost exactly.

```bash
python tools/decode_wav.py E:/SDRPPrecordings/BGAN19.wav
```

### Locating the AVP — since resolved, see below

The first attempt scanned all 8 bit alignments of block 0's payload for the
`10010|prm-len` tag and matched in 98/98 frames — i.e. it was matching noise,
because a 1-entry AVP is only 2 bytes. Rejected at the time.

It was solved later by anchoring on the confirmed BulletinBoard instead of
searching for the tag: see "The AVP list" below. The lesson is that a short
tag needs a known offset to be found, not a better scoring rule.

## The BulletinBoard frame-number check — the decisive test

Everything above validates the decoder against structure we recovered
ourselves. This one does not: it is a counter maintained by the network, and
no error in our chain can reproduce it.

TS 102 744-3-1 clause 5.4.3.3 defines `frame-no` as a 12-bit
INTEGER(0..4095) advancing by one per 80 ms frame. Our own frame index does
too, so `(frame_no - frame_index) mod 4096` must be a constant.

On BGAN19.wav, block 0 at L3, `frame-no` at bit 44:

    offset 2008
    13 / 13   block-0 payloads on a BulletinBoard frame matched
     0 / 208  block-0 payloads off  a BulletinBoard frame matched
    0.054     hits expected by chance

Perfect sensitivity, zero false positives. And the 13 hits fall on frames
37, 54, 71, 88, 156, 173, 275, 292, 309, 360, 377, 411, 445 — **every gap an
exact multiple of 17**. The BulletinBoard is on a strict 17-frame cycle, which
is precisely clause 5.4.3.0's "transmitted at regular intervals, but not
necessarily in every frame". We did not put a 17 anywhere.

The remaining fields are constant across all 13, as they should be:

    rnc-id 6   net-ver 1   f-bearer 1   bct-id 6

and the BCtPDU header's first and third octets are fixed at 0xc9 / 0xc1. The
first AVP octet is 0x86 — which is the most common non-zero byte in the entire
capture, exactly as a frequently repeated AVP tag should be.

### Why the earlier delta test nearly missed it

Scoring consecutive records by whether the field advanced with the frame gap
gave only 0.21, because the BulletinBoard is absent from 16 frames in every
17 and each gap breaks the chain. Testing for a *constant offset* instead is
immune to absence — missing frames simply do not vote. `tools/find_framenum.py`.

### The bit I first called spare was spot-beam-present

Octet 1 bit 6 and octet 5 bit 8 both read 1 where Figures 5.44/5.46 print 0,
and I initially wrote both off as spare bits this operator sets differently.
That was wrong for octet 5 bit 8, and the error was load-bearing.

Figure 5.44 prints that bit as 0 only because it illustrates the
**sb-not-present** arm of the `sb-presence` CHOICE. Figure 5.45 is the
**sb-present** arm and inserts an extra `spot-beam-id` octet before the AVP
list. BGAN19 sets the bit in all 13 BulletinBoards, with spot-beam-id 134.

Reading it as spare puts the AVP list 8 bits early, which is exactly why the
first AVP looked like undefined type 134 -- that "AVP tag" was the
spot-beam-id. A figure showing a literal 0 in a CHOICE arm is a statement
about *that arm*, not a constant.

Octet 1 bit 6 remains genuinely unexplained. It affects no parsed field.

The BCtPDU header length of 3 octets is measured, not read from the spec, so
`bulletin.BCTPDU_HDR_BITS` may not generalise to other PDU types.

## The AVP list — levels read rather than searched

Clause 5.7.1 allocates BCtAVPType so that "the parameter length can be
obtained from the lower three bits", i.e. each AVP occupies
`1 + ((type & 7) + 1)` octets. That makes the list walkable with no length
field and no guessing. Starting at the correct offset (bit 72, after
spot-beam-id):

    start bit 64:  64/78 AVPs resolve to a defined type   (82%)
    start bit 72:  78/78                                  (100%)
    start bit 80:  65/78                                  (83%)

All 13 BulletinBoards open with the same four AVPs in the same order:

    fwd-bearer-code-rate-len-N   (N varies per frame, as it must)
    plmn-info-len-3
    nas-sys-info-len-6
    maxdelay-and-delayrange

### The check that closes the control plane

`ForwardBearerCodeRateParam` parsed out of the BulletinBoard predicts the
coding level of every FEC block in that frame. Compared against the levels
found independently by trial decode:

    103 agree, 0 disagree

Two methods with nothing in common — one structural parse of a broadcast
control message, one brute-force search over ten levels — agreeing on 103
blocks. This is what retires open question 8.

### The network names itself

`plmn-info-len-3` carries `90 11 1f`, identical in all 13. Clause 5.7.34
defines PLMNInfoParam as `mcc SEQUENCE SIZE(3) OF Digit, mnc SEQUENCE SIZE(3)
OF Digit` — plain ordered digits, *not* the nibble-swapped 3GPP layout, which
decodes this to a nonsensical 4-digit MNC. Read straight:

    MCC 901, MNC 11 (third digit 0xF = 2-digit MNC)  ->  PLMN 901-11

901-11 is Inmarsat. Also constant across all 13: `nas-sys-info-len-6` =
`188608010101`, `maxdelay-and-delayrange` = `92`.

### Caveat on the walk

The valid-type run extends 10-16 AVPs, longer than the real list: with 95 of
256 type codes defined, a garbage octet still looks valid ~37% of the time, so
the walk runs past the end. Only the leading AVPs are trustworthy. The SDU's
`slength` field would bound it properly, but it sits in octet 1 — the one
octet known to read anomalously — so it is not yet trusted.

## Second capture — does any of this generalise?

Everything above came from one file. Repeating it on
`BGAN very strong lots of data baseband_1543100000Hz_19-09-04_30-07-2026.wav`
— different day, different frequency, 512 kHz instead of 192 kHz, no 2x
upsample needed — reproduces the whole stack.

    symbol clock          +1.1 ppm            (BGAN19: -0.2 ppm)
    blocks decoded        785/1984  39.6%     (BGAN19: 1541/3928  39.2%)
    per block index       b0 116 ... b7 91    flat, as on BGAN19
    BulletinBoard period  17 frames           independently derived
    AVP-predicted levels  47 agree 0 disagree (BGAN19: 103 / 0)

Fields that should be invariant are, and fields that should differ do:

    plmn-info-len-3          90111f          identical  (Inmarsat 901-11)
    maxdelay-and-delayrange  92              identical
    spot-beam-id             134             identical
    f-bearer / net-ver       1 / 1           identical
    nas-sys-info-len-6       748608010101    vs 188608010101 (first octet only)
    rnc-id / bct-id          29 / 9          vs 6 / 6

`spot-beam-id` stays 134 while `rnc-id` changes. (I first read that as a
physical necessity — same receiver location, same beam. It is not; see the
correction under the third capture below.)

Recovered payload is coherent application traffic — dozens of consistently
named ESET Remote Administrator agent module paths
(`/era-agent-sta/mod_039_confeng2_era_2439/em039_a64_n*.dll.nup`). The payload
is 5.7% zeros here against 14.6% on BGAN19, matching the operator's own
description of this capture as carrying more data.

### The negative-only offset steps are the recorder, not the signal

BGAN19 alone could not distinguish "dropped samples" from something in the
signal. This capture settles it. The staircase reappears with **every step
negative** (6456 -> 6359 -> 6248 -> 6082 -> 5888 -> ... -> 4808) on a
different day at a different sample rate, and the rate scales with the
recording load:

    192 kHz capture : -2.3 symbols/frame
    512 kHz capture : -6.6 symbols/frame
    ratio 2.87        vs sample-rate ratio 2.67

Time can only be lost, never gained, and losses scale with throughput. This is
SDR++ buffer overrun during recording. `track_offsets` is the right shape of
fix; a PLL fitting a period would still be wrong.

### Third capture, and a correction

`BGAN 2 at once ...1547298000Hz...` (512 kHz, 12 s) is a weak case — two
carriers overlap in band, the channeliser sees a blend, and only 92/1184
blocks decode (7.8%). Even so the control plane parses:

    rs               -0.3 ppm
    BulletinBoards   2 found (0.009 expected by chance)
    rnc-id / bct-id  29 / 9      same as the 1543.1 capture
    f-bearer         0           vs 1 on 1543.1
    spot-beam-id     133         vs 134
    plmn-info        90111f      Inmarsat again
    nas-sys-info     748508010101  vs 748608010101 (one octet)

The two hits are 85 frames apart = 5 x 17, consistent with the same 17-frame
cadence. `confirm()` reports no period here only because it requires more than
two hits before inferring one, which is the right conservatism.

**Correction.** On the second capture I wrote that spot-beam-id staying 134
while rnc-id changed was "physically sensible: the receiver is in one place so
the spot beam is the same". That reasoning was too strong. Here spot-beam-id
differs (133) between two carriers received at the same location on the same
day. A receiver can see carriers from several spot beams at once; the beam is
a property of the carrier, not of where the receiver happens to sit. The
earlier agreement was two carriers sharing a beam, not a constraint.

`f-bearer` 0 vs 1 with rnc-id and bct-id equal is the internally consistent
part: clause 5.4.3.5 defines f-bearer as the Forward Bearer Number *within*
the Bearer Control, so two bearers under one BCt is exactly the expected
shape.
