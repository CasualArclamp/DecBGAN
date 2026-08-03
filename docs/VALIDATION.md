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
came out at +1.47, +1.50, +1.50 dB â€” within 0.03 dB of each other despite
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
right â€” none of those are exercised here. Nor does it prove the scrambler
phase is right, since encode and decode share the same scrambler.

## Regression value

Re-run this after any change to the coding chain. A margin that goes negative,
or a spread that stops being flat across L3/L2/L1, means something in the
coding chain broke.

```bash
python tests/waterfall.py
```

## Real-signal validation â€” BGAN19.wav, decoded end to end

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

Both came from **frame block 2 at level H4** â€” blocks that did not decode at
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

Whole capture at the time: **1541 / 3928 blocks (39.2%)**, flat across block
indices (b0 221, b1 192, b2 189, b3 192, b4 188, b5 194, b6 185, b7 180),
median parity agreement 0.965. The prior decoder recovered 222 blocks,
essentially all block 0 -- which matches our b0 count of 221 almost exactly.

> **Superseded.** 39.2% was a bug, not a ceiling. Per-frame symbol timing
> takes the same capture to **98.5%** -- see "The 39% ceiling was a bug" at
> the end of this document. The figures in this section are kept as the
> before state.

```bash
python tools/decode_wav.py E:/SDRPPrecordings/BGAN19.wav
```

### Locating the AVP â€” since resolved, see below

The first attempt scanned all 8 bit alignments of block 0's payload for the
`10010|prm-len` tag and matched in 98/98 frames â€” i.e. it was matching noise,
because a 1-entry AVP is only 2 bytes. Rejected at the time.

It was solved later by anchoring on the confirmed BulletinBoard instead of
searching for the tag: see "The AVP list" below. The lesson is that a short
tag needs a known offset to be found, not a better scoring rule.

## The BulletinBoard frame-number check â€” the decisive test

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
37, 54, 71, 88, 156, 173, 275, 292, 309, 360, 377, 411, 445 â€” **every gap an
exact multiple of 17**. The BulletinBoard is on a strict 17-frame cycle, which
is precisely clause 5.4.3.0's "transmitted at regular intervals, but not
necessarily in every frame". We did not put a 17 anywhere.

The remaining fields are constant across all 13, as they should be:

    rnc-id 6   net-ver 1   f-bearer 1   bct-id 6

and the BCtPDU header's first and third octets are fixed at 0xc9 / 0xc1. The
first AVP octet is 0x86 â€” which is the most common non-zero byte in the entire
capture, exactly as a frequently repeated AVP tag should be.

### Why the earlier delta test nearly missed it

Scoring consecutive records by whether the field advanced with the frame gap
gave only 0.21, because the BulletinBoard is absent from 16 frames in every
17 and each gap breaks the chain. Testing for a *constant offset* instead is
immune to absence â€” missing frames simply do not vote. `tools/find_framenum.py`.

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

## The AVP list â€” levels read rather than searched

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

Two methods with nothing in common â€” one structural parse of a broadcast
control message, one brute-force search over ten levels â€” agreeing on 103
blocks. This is what retires open question 8.

### The network names itself

`plmn-info-len-3` carries `90 11 1f`, identical in all 13. Clause 5.7.34
defines PLMNInfoParam as `mcc SEQUENCE SIZE(3) OF Digit, mnc SEQUENCE SIZE(3)
OF Digit` â€” plain ordered digits, *not* the nibble-swapped 3GPP layout, which
decodes this to a nonsensical 4-digit MNC. Read straight:

    MCC 901, MNC 11 (third digit 0xF = 2-digit MNC)  ->  PLMN 901-11

901-11 is Inmarsat. Also constant across all 13: `nas-sys-info-len-6` =
`188608010101`, `maxdelay-and-delayrange` = `92`.

### Caveat on the walk

The valid-type run extends 10-16 AVPs, longer than the real list: with 95 of
256 type codes defined, a garbage octet still looks valid ~37% of the time, so
the walk runs past the end. Only the leading AVPs are trustworthy. The SDU's
`slength` field would bound it properly, but it sits in octet 1 â€” the one
octet known to read anomalously â€” so it is not yet trusted.

## Second capture â€” does any of this generalise?

Everything above came from one file. Repeating it on
`BGAN very strong lots of data baseband_1543100000Hz_19-09-04_30-07-2026.wav`
â€” different day, different frequency, 512 kHz instead of 192 kHz, no 2x
upsample needed â€” reproduces the whole stack.

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
physical necessity â€” same receiver location, same beam. It is not; see the
correction under the third capture below.)

Recovered payload is coherent application traffic â€” dozens of consistently
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

`BGAN 2 at once ...1547298000Hz...` (512 kHz, 12 s) is a weak case â€” two
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

## The 39% ceiling was a bug: one timing phase for the whole capture

Yield went from **39.2% to 98.5%** on BGAN19 by fixing a single wrong
assumption. Worth recording in full, because three plausible hypotheses were
tested and disproved first, and each disproof was itself the clue.

### What it was

`recv.extract_symbols` computes `pos = tau0 + period*arange(n)` â€” one timing
phase, chosen at t=0, applied to all 39 seconds.

We already knew this recording drops samples (that is the offset staircase).
At 4 samples/symbol, losing N samples shifts the symbol timing by
`(N mod 4)/4` of a symbol. The whole-symbol part shows up as a frame-offset
step and was already handled. **The fractional remainder was invisible**, and
it moved every subsequent sample off the eye centre for the rest of the
capture.

### Why the earlier hypotheses all failed

| hypothesis | test | result |
|---|---|---|
| fading | power, good vs failing frames | 0.41 dB apart; whole capture spans 1.7 dB |
| lost acquisition | retry at neighbouring good frame's offset | 3 of 126 fixed |
| tracker window too narrow | full 12096-position UW search + decode | 0 of 30 recovered |
| sample drop mid-frame | per-block offset search within a frame | 0 of 10 showed split |

Every one of those searched **integer symbol positions**. A fractional timing
error is the one thing none of them could see. The tests were sound; they were
all looking in the same wrong dimension.

An estimator trap on the way: per-frame EVM-to-nearest-constellation-point
*saturates*. Random symbols still land within half a grid spacing, so the
metric bottoms out near 0.4 and reports ~8 dB no matter how bad the frame is.
That produced an apparent contradiction â€” failing frames "at 8.34 dB" when
block 0 (always L3) needs only 3.22 dB â€” which was an artifact, not a finding.

### The proof

Sweeping the timing phase over one symbol period and re-decoding:

    tau shift     dead run A    dead run B    known-good run
        0.000       0/80          0/80            80/80
        0.500      50/80         45/80             0/80
        0.625      80/80         80/80             0/80
        0.875       0/80          0/80            31/80

Two runs yielding **nothing** go to **100%** at a 5/8-symbol shift, and a run
already at 98.9% collapses to 0% at that same shift. The symmetry is what
makes it conclusive: this is a timing-phase effect, not a lucky search.

### The fix

`survey_taus` evaluates 8 timing phases per frame and picks the best by
differential-UW correlation â€” no trial decoding needed, because the metric
tracks timing closely (38 -> 81 on a dead run at the right phase).
`decode_capture` then decodes each frame at its own phase, iterating one phase
at a time so memory stays flat. `--ntau 1` restores the old behaviour.

Unexpected bonus: the chosen phase wanders continuously, not only at dropouts
â€” all 8 phases are used roughly evenly (`[75, 69, 63, 56, 43, 47, 55, 83]`).
That is the residual -0.3 ppm clock error, worth about half a symbol across
39 s. Per-frame timing absorbs it for free.

### Result, and confirmation the extra blocks are real

                              before      after
    blocks decoded         1541/3928   3868/3928
                              39.2%       98.5%
    per block index      b0 221..b7 180   b0 490..b7 474
    payload                   680 kB     1749 kB
    IPv4 packets carved           71        230
    printable runs               184        830

The independent checks scale exactly as they should, which is the real
evidence â€” a decoder inventing blocks would not:

    BulletinBoards found          13         28   (of 29 possible)
    frame-no offset             2008       2008   (unchanged)
    broadcast period          17 fr      17 fr    (unchanged)
    AVP level prediction    103 / 0    219 / 0    (agree / disagree)

Newly recovered content includes Windows Update and Microsoft PKI URLs, HTTP
headers with `x-rewritten-path`, and a satellite ISP's content-filter block
page ("...contact your Satellite Service Administrator or Provider").

## Reading the coding levels instead of searching for them

The per-block coding levels were found by trial decode. They can now be *read*
on 88% of frames, from the BCtPDU layer.

### The header decodes exactly as the spec says

Clause 5.1.4 gives the FwdBCtPDUHeader as bct-sdu-follows(1),
length-present(1), bct-pdu-addr-type(2), comsig-or-ext-addr(1), then a 3-bit
type. On BGAN19, block 0's first octet is **0xc9 in all 490 payloads**, which
decodes as:

    bct-sdu-follows = 1     length-present = 1
    bct-pdu-addr-type = 0 = broadcast     com-sig-type = 1

Broadcast with a length field is precisely what clause 5.4.3.0 requires of the
PDU carrying a BulletinBoard. Blocks 1-7 are mostly `tbcn-id` addressed, i.e.
per-connection user data, which is also what one would expect.

### The first BCtSDU is at bit 24, and often it is the AVP

On frames not carrying a BulletinBoard, the first BCtSDU is the
ForwardBearerCodeRateParam itself, so the levels are simply there to be read.

This is established by prediction, not assertion -- which matters, because an
earlier blind scan for the same tag matched 98/98 frames on pure noise and
looked convincing. Scored against 3868 levels obtained independently by trial
decode:

    bit offset 0, byte 3 :  3387/3409   99.35%
    next best position   :               38.3%
    assume L3 everywhere :   921/3868   23.8%

No other of the ~320 (bit-shift, byte-position) combinations tested comes
close. The 22 misses are absorbed by using the read value as the first
candidate for trial decode rather than trusting it, so the result is lossless.

### Result

    frames where levels are read, not searched   432/490  (88.2%)
    full decode of BGAN19                        3868/3928 blocks in 59 s

Decode cost, same file and same output throughout:

    all ten levels tried per block          9.84 s/s   388 s
    + level predictor, stop at first hit    2.96 s/s   116 s
    + code-rate AVP read from block 0       1.50 s/s    59 s
    (floor: no level identification at all) 1.03 s/s

6.6x faster than where this started, with byte-identical output.

### What did not work

**Chaining PDUs by the length field.** If the length octet is "content length
from end of header to CRC", then walking header -> content -> next header
should repeatedly land on something that decodes as a header. It does not:
159 of 490 payloads overrun the block, and the second PDU's first octet is
scattered with no dominant value. The control settles it -- shuffling the
bytes of each payload gives *fewer* overruns (10.5%) than the real data
(32%), so this is not a real structure being read slightly wrong. The length
field is either not where Figure 5.22 puts it, or does not mean what
clause 5.1.5.3 says here.

**Carrying levels forward from the last BulletinBoard.** The AVP applies to
its own frame: prediction is 100% correct on the BulletinBoard's frame and
~50% one frame later, and every consecutive BulletinBoard differs.

**Parsing a BulletinBoard on frames that have none.** Calling `bulletin.parse`
wherever byte 3 was not a code-rate tag gave 56% level accuracy, against
99.35% for the direct AVP read. The BulletinBoard path is only trustworthy on
frames confirmed by the frame-no check, where it scores 219/0.

So the BCtPDU header is understood and the first SDU is located, but SDU
chaining beyond the first is not. The rule for it is in TS 102 744-3-3/-3-4
(Bearer Connection Layer), which turned out to be freely available from
ETSI after all -- see docs/OPEN_QUESTIONS.md item 6.

---

## Calibrating the block-acceptance test (Aug 2026)

A block is accepted only if **both** hold: the re-encoded parity agrees with
the demapper's hard decisions above a threshold, **and** `verify_block`'s
likelihood ratio clears its margin. Neither alone is sufficient, and until
now the threshold was in the wrong place. This is the calibration.

### Positives: ground truth from the synthetic generator

`tools/make_test_iq.py` saves the transmitted payload bits, so a decode can be
scored bit-exactly rather than "it converged". Seven captures, L3, 6 s each,
Es/N0 5 to 12 dB, 20 frames per capture, all 8 blocks, all 10 candidate
levels. A try is a true positive only when the level is the transmitted one
*and* the descrambled bits match the truth array exactly.

    Es/N0   correct blocks   min agreement   median   LR passes
      5 dB       159            0.7585       0.7992     100%
      6 dB       160            0.7879       0.8246     100%
      7 dB       160            0.8191       0.8518     100%
      8 dB       160            0.8457       0.8770     100%
      9 dB       160            0.8726       0.9010     100%
     10 dB       160            0.8967       0.9240     100%
     12 dB       160            0.9397       0.9631     100%

Agreement tracks SNR, because it is measured against hard decisions that are
themselves error-prone. That is exactly why a fixed 0.90 fails at low SNR.

### Negatives: blocks that cannot possibly decode

Three kinds, all drawn from the captures themselves so the noise is theirs:
frame offset displaced by MOS/3, offset shifted by 7 symbols, and circular
Gaussian noise at matched power. Run over both the synthetic set and five
real captures, all 10 levels each.

    synthetic negatives    21281 tries   max agreement 0.5895
    real-capture negatives 16000 tries   max agreement 0.6023
    pooled                 37281 tries   max agreement 0.6023

The false distribution is centred near 0.5 and its ceiling barely moves with
SNR, because a wrong decode is largely uncorrelated with the hard decisions.
The true distribution moves a lot. So the threshold belongs just above the
false ceiling, not somewhere in the middle of the true one.

### Result

    correct decodes, minimum over 1119 blocks     0.7585
    impossible tries, maximum over 37281 tries    0.6023

Nothing lies between. `ACCEPT_AGREEMENT = 0.70` sits in the gap, 0.098 above
the observed false ceiling and 0.059 below the observed true floor.

    threshold   recall of correct blocks   false accepts / 37281
       0.90            36.5%                      0
       0.85            65.1%                      0
       0.80            91.5%                      0
       0.70           100.0%                      0
       0.62           100.0%                      0

Per Es/N0, 0.90 against 0.70:

           5dB   6dB   7dB   8dB   9dB  10dB  12dB
    0.90    0%    0%    0%    0%   56%   99%  100%
    0.70  100%  100%  100%  100%  100%  100%  100%

### Why the likelihood ratio cannot replace it

On the same negatives, `verify_block` alone accepts 2.6-3.6% of tries at every
SNR. Across ten candidate levels that is a false level on roughly a quarter of
all blocks. Paired with the agreement threshold it gives zero false accepts on
all 37281. The pairing is the test; neither half is.

### Regression value

Re-run before changing `ACCEPT_AGREEMENT`. The scripts are throwaway but the
recipe is not: generate labelled captures across Es/N0, decode every block at
every level, label against the truth array, and pair each positive set with
negatives drawn from the same captures. A threshold moved without that is a
fudge, whichever direction it moves.

### The extra blocks corroborate on checks that share nothing with the test

The calibration above is ground truth, but it is the same *kind* of test as
the one being changed. These two are not: both depend on the network's own
internal consistency, and the decoder never uses either as an input.

  * **BulletinBoard frame-no.** A 12-bit counter the network advances once per
    80 ms frame, so `(frame_no - frame_index) mod 4096` must be constant.
  * **AVP-predicted coding levels.** Block 0 carries a
    ForwardBearerCodeRateParam naming the levels of blocks 1-7. Agreement with
    the level trial decode settled on is a prediction, not a measurement.

Over 20 s per capture, at the old threshold and the new:

    capture      thr    blocks     BulletinBoard frame-no      AVP levels
    1543.100b   0.90   1904/1984   15/244 on-cycle, period 17   1451 / 0
    1543.100b   0.70   1904/1984   15/244 on-cycle, period 17   1451 / 0
    1543.100a   0.90   1921/1984   14/247 on-cycle, period 17   1315 / 0
    1543.100a   0.70   1921/1984   14/247 on-cycle, period 17   1315 / 0
    1553.500    0.90     15/1984   too few to test                 0 / 0
    1553.500    0.70   1721/1984   13/232 on-cycle, period 17    747 / 0
    1547.298    0.90    331/1984    5/143 on-cycle, period 17     125 / 0
    1547.298    0.70   1887/1984    8/248 on-cycle, period 17    1441 / 0

On-cycle hits expected by chance are 0.03-0.06, so a single one is already
conclusive and these are 5 to 15, all on a strict 17-frame period.

The two captures that gain blocks gain 2188 further AVP agreements with **zero**
disagreements, and 1553.500 goes from having no measurable control plane at all
to a clean 17-frame BulletinBoard cycle. The two captures that gain nothing --
because at 12 dB every correct block already cleared 0.90 -- are bit-identical
on every column. A decoder manufacturing 1706 extra blocks does not do that.

Content is consistent too. 1553.500 decodes to **92.8% zero bytes**, 1633 of
1721 blocks at L3: an idle bearer sending filler, the same signature as the
all-zero L3 blocks already seen on BGAN19. False decodes emit uniform random
bytes, which are ~0.4% zeros. 1547.298 is the opposite, 0.7% zeros and high
entropy, consistent with encrypted user traffic -- and it is the 1441/0 AVP
agreement rather than the entropy that establishes those blocks are real.

### Full-capture regression, old behaviour against new

Same code path, same 12 s window, one process. OLD is no carrier-offset
correction, acceptance threshold 0.90, single-phase carrier probe.

    capture               OLD           NEW      residual offset   what fixed it
    1543.100b (control)   94.8%       94.9%        +205.2 Hz    nothing needed
    1543.100a              0.0%       96.5%        -860.6 Hz    carrier offset
    1553.500          nocarrier       82.9%       -1564.7 Hz    all three
    1550.398               0.0%       34.2%        -425.1 Hz    threshold
    1547.298              15.8%       92.7%        +103.9 Hz    threshold
    BGAN9             nocarrier       92.6%       -2418.5 Hz    carrier offset
    BGAN10                 0.0%       88.2%       -2532.4 Hz    carrier offset
    BGAN15            nocarrier       98.4%        -611.3 Hz    floor window
    1532.200          nocarrier   nocarrier                     correct: 33.6 kBd only

Unique-word EVM before and after correction, same order: 0.199->0.169,
0.464->0.146, 1.007->0.305, 0.414->0.353, 0.263->0.261, 2.029->0.193,
2.286->0.201, 0.348->0.165.

BGAN15 was added by the fourth fix (the noise-floor window) and needed all of
them: the floor fix to be found at all, then the carrier correction and the
threshold to decode. Its row reads nocarrier against the state before that
fix. Note the OLD column of the harness shares the corrected floor code, so
re-running it now reports 0/1184 for BGAN15 rather than nocarrier -- the
carrier is located but nothing decodes without the other two fixes.

Three things worth reading off this table:

  * The **192 kHz captures carry the largest offsets** (-2418, -2532 Hz), and
    their UW EVM reads 2.0-2.3 before correction. A carrier filling the whole
    band gives the centroid no symmetric shoulders to balance, so those files
    are the worst case for the old estimator, not merely the noisiest.
  * The fixes are **separable**. 1543.100a is carrier offset alone: 1921/1984
    at either threshold once corrected. 1547.298 is threshold alone, its
    offset being +103.9 Hz with EVM essentially unmoved. 1553.500 needs all
    three, scoring 15/1984 at threshold 0.90 even with its carrier fixed.
  * **1532.200 still reports nocarrier**, which is correct -- it holds four
    33.6 kBd F80T1X-4B carriers and no 151.2 kBd bearer at all. The carrier
    probe escalation did not manufacture one, which was the risk in letting a
    rejected candidate have a second look.

Synthetic captures, all 9 s, old against new:

    L3 12 dB          888/888  ->  888/888   (100%)
    L3 30 dB clean    888/888  ->  888/888   (100%)
    H6 16 dB          888/888  ->  888/888   (100%)
    R 12 dB +100 kHz  888/888  ->  888/888   (100%)
    L3 4 dB threshold   0/888  ->  735/888   ( 82.8%)

and the same at 12 s, after the noise-floor fix, confirming it disturbs
nothing: 992/992 on all four clean files and 0/992 -> 820/992 at 4 dB.

The 4 dB file is the strongest single result here, because it has ground
truth and sits **below** the 5-12 dB range the threshold was calibrated over.
All 735 accepted blocks are **bit-exact against the transmitted payload, zero
wrong**, and all 735 are at the transmitted level L3. So the threshold does
not start manufacturing blocks as soon as it leaves its calibration range;
the decoder simply stops finding them.

### BGAN15 — recovered by the floor fix, and it decodes to clear text

`pick_carrier` had been rejecting this capture outright because its unique
word happened to sit inside the window used to measure the noise floor (see
docs/SIGNAL_NOTES.md, defect 4). With the floor placed half a frame from the
UW instead, over 20 s:

    1956/1984 blocks (98.6%)
    carrier offset -530.8 Hz,  UW EVM 0.314 -> 0.168
    BulletinBoard frame-no  14/247 on-cycle (chance 0.06), period 17
    AVP-predicted levels    1515 agree / 0 disagree
    levels  L3 781, H5 441, H3 246, H6 141, H1 139

The level spread is itself informative: a real traffic mix across five coding
levels, not the single level a decoder locking onto one artifact would give.

The payload contains protocol text in clear:

    HEAD / HTTP/1.0
    Accept: */*
    User-Agent: WhatsUp/1.0
    victronenergy
    California1

A 12 s decode of the same file also yields a complete HTTP response,

    HTTP/1.1 304 Not Modified
    Content-Type: application/pkix-crl
    Cache-Control: public, max-age=474
    Connection: keep-alive

whose MIME type is the one for a certificate revocation list, matching the
DigiCert CRL URLs already recovered from BGAN19 -- and several dozen lines of
consistent ASCII-art logo (`MMMMMMMMMMO. lWMMMMMMMMMMMMMMMMMMWc .OMMMMMMMMMM`
and so on), interrupted only where a block failed to decode.

None of that can arise from noise, and it is independent of every threshold
and estimator in this repository. A four-header HTTP response with a correct
and contextually apt MIME type is not something a mis-tuned acceptance test
can fabricate. `WhatsUp/1.0` is the same user agent
recovered from BGAN19, so this is the same network. `victronenergy` is marine
and solar power equipment, which is what sits behind a BGAN terminal.
`California1` is an X.509 subject fragment (`ST=California`), consistent with
the certificate chains already recovered.

Note its offset, -530.8 Hz, is just *inside* the 552 Hz pilot-unwrap cliff.
So BGAN15 needed the floor fix to be found at all; the carrier correction then
took its unique-word EVM from 0.314 to 0.168.

---

## Carved findings: certificates, DNS, HTTP, TLS (Aug 2026)

`bgan/findings.py` recognises structured artefacts in a decoded payload. The
standard applied is the one `pcapout.carve_ipv4` set: scanning a megabyte for
a two-byte tag finds a hit every few hundred bytes in noise, so a candidate is
accepted only if it **parses to its own declared length and ends exactly where
it said it would**.

    extractor    validation
    cert         DER SEQUENCE with exactly three children (tbsCertificate,
                 signatureAlgorithm SEQUENCE, signatureValue BIT STRING),
                 each parsing to its declared length and together consuming
                 the outer SEQUENCE to the byte; TBS must hold a serial
                 INTEGER and a validity SEQUENCE of two time values
    cert frag    a SEQUENCE holding exactly two UTCTime/GeneralizedTime
                 values, all digits and Z-terminated, notAfter > notBefore
    dns          QDCOUNT 1, opcode QUERY, reserved Z bits zero, RCODE <= 10,
                 section counts <= 64, question name parses to valid labels
                 totalling <= 253 with QCLASS IN and a known QTYPE
    tls          record 0x16, version 0x03xx, handshake type 1 or 2, and the
                 24-bit handshake length + 4 equal to the 16-bit record
                 length exactly
    http         the 7-byte literal `HTTP/1.` plus a start-line grammar match
    url          plain regex -- the one loose extractor, labelled as such

False accepts over three independent 8 MB blocks of random bytes: **zero**, of
every kind including URLs. Throughput is 9.6 MB/s, so the scan is free
relative to a decode.

### Certificates mostly do not parse whole, and that is expected

Only four certificates parse end to end across the captures on hand; the rest
are recovered from their validity block. This is not a defect in the parser.
The payload is FEC blocks concatenated in frame/block order with **no Bearer
Connection reassembly**, so a 1.5 kB certificate is interleaved with whatever
else the terminal was carrying and its DER lengths stop lining up. Objects
small enough to sit inside a single block -- a DNS message, an HTTP header
block, a ClientHello -- survive intact; larger ones do not.

The fragment anchor exists because `Validity ::= SEQUENCE { notBefore Time,
notAfter Time }` is a ~32-byte shape with almost no freedom in it, and the
subject Name follows it immediately. That recovers the two most interesting
facts about a certificate -- who it is for, and when it was issued -- from a
fragment that will never parse as a whole.

### What the captures actually contain

`1543.100b`, 25 s, 1.18 MB of payload: 149 findings — 114 DNS messages,
11 TLS handshakes, 7 HTTP transactions, 3 certificates, 14 URLs. Hostnames
implicated include `edge.microsoft.com`, `login.microsoftonline.com`,
`update.eset.com`, `routerpool6.rlb.teamviewer.com`, `config.edge.skype.com`,
`star-mini.c10r.facebook.com` and `pagead2.googlesyndication.com`, plus a
`10.0.31.172.in-addr.arpa` PTR lookup that names the terminal's own subnet.
One certificate reads `C=US, O=Amazon, CN=Amazon Root CA 1`, valid
2015-05-25 to 2037-12-31.

`BGAN15`, 12 s: `myip.opendns.com` and `mqtt-rpc.victronenergy.com` (A and
AAAA), a certificate for `*.prod.do.dsp.mp.microsoft.com`, and the
`application/pkix-crl` responses. The Victron MQTT lookup and the
`*.iot.us-east-1.amazonaws.com` certificate on the other capture agree with
each other: this is marine/solar monitoring equipment reporting home.

This is corroboration of the decode as much as it is output. A DNS message
whose label lengths walk exactly to a terminating zero, followed by QCLASS IN
and a known QTYPE, is not something a mis-tuned acceptance threshold produces.

### Which captures actually carry findings

Measured over a 20 s decode of each, so the comparison is like for like:

    capture        blocks   payload   findings   /MB   hosts
    1543.100a       96.8%   0.63 MB      142     227     47
    1543.100b       96.0%   0.94 MB      104     110     41
    BGAN10          87.1%   0.59 MB       28      48      7
    BGAN15          98.6%   0.85 MB       36      42      9
    BGAN9           91.0%   0.67 MB       13      19      4
    1547.298        95.1%   0.68 MB        2       3      1
    1553.500        86.7%   0.44 MB        0       0      0

`1543.100a` is the richest by a factor of two, with 21 certificates in 20 s
against 3 for the capture actually named "lots of data". That is the capture
which decoded **nothing at all** before Aug 2026.

The two at the bottom are not failures of the extractors, and they agree with
the content analysis done independently:

  * `1553.500` yields zero findings while decoding 86.7% of its blocks. It is
    92.8% zero bytes -- an idle bearer sending filler. Nothing to find is the
    correct answer, and a scanner that returned findings here would be wrong.
  * `1547.298` yields two from 0.68 MB. It is 0.7% zeros and high entropy,
    i.e. encrypted bulk traffic with no plaintext handshakes in the window.

Payload size does not predict findings: `1543.100b` decodes half again as many
bytes as `1543.100a` and finds a third fewer things, because more of its bytes
are one bulk transfer rather than many small exchanges.
