# Open questions — things the spec does not pin down unambiguously

Each entry is something that must be resolved empirically or by a documented
guess. Anything decoded while one of these is unresolved is provisional.

## RESOLVED

### 1. Scrambler load order (clause 5.3.7) — resolved
Not ambiguous after all. The printed initial value "110 1001 0101 1001" is
**15** bits, not 16 (3+4+4+4), and equals 26969 = `0x6959` exactly, which fits
the 15-stage register directly.

Confirmed empirically: with polynomial 1 + X + X^15 the generated sequence has
period 32767 = 2^15-1 with exactly 16384 ones — a maximal-length m-sequence.
Incorrect taps do not produce that.

### 2. Puncturing / channel interleaving / symbol mapping — resolved
The Annex C.2 CIPM tables enumerate it explicitly; nothing is inferred. Verified
for all ten F80T4.5X levels:
- exactly 5984 bit slots, all global indices unique
- counts are exactly [DF, p, q]
- the global index space is the **unpunctured rate-1/3 codeword** of length
  3*DF: `[0,DF)` systematic, `[DF,2DF)` p-parity, `[2DF,3DF)` q-parity

**Trap:** do not renumber surviving parity bits by rank. Puncturing removes
some (14 of 2004 p-bits at L3, 4694 at H6), so a rank-based index is offset
from the encoder's own parity output index. Use `g - kind*DF`.

The largest global index is usually 3*DF-1 but need not be — at H2/H3/H4/H5/H6
the final q-parity bit is itself punctured.

### 3. 80 ms outer interleaver (clause 5.3.8.5) — resolved, does not apply
Clause 5.3.8.5 applies the outer interleaver only "In the case of FR80T2.5 and
FR80T5 bearers". F80T4.5X-8B is not one of them, so blocks are concatenated in
order with no outer interleaving.

The apparent "0.25" in the Annex B2 outer-interleaver column was my own
column-indexing error; 0.25 is the roll-off factor in the adjacent column.

### 4. SRCC convention (clause 5.3.8.2) — resolved
`a = u XOR s3 XOR s4`, `parity = a XOR s1 XOR s2 XOR s4`, state read left to
right as s1s2s3s4. Confirmed by the fact that this convention reproduces
Table 5.11 exactly: all 16 flush entries drive the encoder to state zero, and
do so with a == 0 throughout.

### 5. Coding level of blocks 1..7 — resolved by TS 102 744-3-1
Clause 5.7.16 (ForwardBearerCodeRateParam): on forward bearers that use **no
outer interleaving** — which includes F80T4.5X-8B — the AVP is *"only provided
if the coding rate changes from the coding rate of the first FEC block in the
frame which is implicitly signalled in the unique word"*.

So **absent the AVP, all 8 blocks use the UW-signalled level.** Inheriting
block 0's level is the specified default, not an assumption, and brute-forcing
ten levels per block is unnecessary.

When present the AVP carries up to 8 (block-num, coding-rate) pairs, each
applying from its block "for the rest of the frame or until another change".
For F80T45X-8B it refers to the **current** frame; for F80T2.5X/F80T5X it
refers to the *next* frame. Wire format (Figure 5.109):

    octet 1 : 1 0 0 1 0 | prm-len(3)      prm-len = n-1, n = entry count
    octet k : mii(1) | block-num(3) | coding-rate(4, two's complement)

CodeRate mapping is Table 5.20: -8..6 -> L8 L7 L6 L5 L4 L3 L2 L1 R H1 H2 H3
H4 H5 H6. The R80T0.5Q/R80T1Q column differs above +1 and does not apply here.

Implemented in `bgan/bctrl.py`, verified against the spec's own worked
EXAMPLE 1, which it reproduces exactly including the wire encoding.

### 5a. Per-block levels in practice — the AVP is present and active
Clause 5.7.16 says the AVP is sent only when the rate differs from block 0's.
On BGAN19 it is evidently sent constantly: measured levels are block 0 L3
(always, matching the UW), block 1 mostly R, blocks 2-4 H1/H2/H4, blocks 5-7
mostly H4, and they vary frame to frame. So "absent the AVP, inherit block 0"
remains the correct *default*, but on real traffic the AVP is rarely absent.

Until it can be located in the payload (see 8 below), `decode_wav.py`
identifies the level by trial decode. Justified because the parity test is
unambiguous: no block has ever passed at two levels out of ten. See
docs/VALIDATION.md.

### 8. Locating the ForwardBearerCodeRateParam AVP — RESOLVED
Found. It is the first AVP in the BulletinBoard's bb-avp-list, which starts
after `spot-beam-id` (bit 72 of the block-0 payload on BGAN19). The earlier
blind tag scan failed because a 2-byte AVP matches noise; the fix was to
anchor on the confirmed BulletinBoard rather than search.

Validated by prediction: the parsed AVP gives the coding level of every FEC
block in the frame, and matches the independent trial-decode search on
**103 blocks with 0 disagreements**. See docs/VALIDATION.md.

Still searched in practice, though: the BulletinBoard is only broadcast every
17th frame, so 16 frames in 17 have no BulletinBoard to read levels from.
Reading them there needs the generic BCtPDU/avp-list structure, not just the
BulletinBoard SDU.

## OPEN

### 6. Missing specification parts
We now hold 2-1 (physical layer), **3-1 (bearer control)** and 3-9 (user plane).
3-1 supplies the BCt-PDU/SDU structures, the BulletinBoard SDU, the AVP
catalogue and the CodeRate table. Still missing for full packet recovery:
- **3-2** Bearer Control Operation
- **3-3..3-8** logical channels, RLC/MAC, SNDCP-equivalent

3-1 does **not** address the physical-layer framing blocker below; it
reconfirms that the unique word implicitly signals block 0's coding level,
i.e. the UW is expected to be present.

Without these, the fallback is scanning decoded bytes for plausible IPv4
headers, as the previous project did. That works but loses damaged headers and
invents packets from noise. Any such packet must be labelled unvalidated.

### 7. Where the demodulator's implementation loss goes
Measured on BGAN1: PSD says Es/N0 14.1 dB, a matched filter with timing
quantised to 1/8 symbol recovers 11.4 dB effective. ~2.7 dB is unaccounted for
and is the target for the receive chain.

The M4/M2^2 estimator used for that figure is calibrated against the loopback
transmitter and is accurate to +/-0.25 dB over 8..20 dB, so the gap is real
rather than an artifact of the estimator.
