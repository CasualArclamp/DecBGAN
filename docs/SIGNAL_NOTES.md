# Measured properties of the captures

Recorded so results are not re-derived, and so the reasoning behind the
targeting decision is auditable.

## Hardware

RTL-SDR v4 (R828D + RTL2832U, **8-bit ADC**) + RTL-SDR Blog L-band patch
antenna with integrated LNA. Relevant limits:
- Supported sample rates: **225–300 kHz** and **900 kHz–3.2 MHz**. Nothing in
  between, so 302.4 kHz (2 sps) and 604.8 kHz (4 sps) are both unreachable.
- 8-bit ADC makes input level matter a great deal — see below.

## Files

All captures are RTL-SDR v4 + L-band patch, Bias-T on, **both AGCs off**,
IQ correction on, PPM 0, 16-bit baseband via SDR++.

| File | Centre | Rate | Dur | Carriers | Es/N0 | C/N0 |
|---|---|---|---|---|---|---|
| `BGAN very strong ...1543100000Hz` | 1543.100 MHz | 512 kHz | 60 s | 1 @ −0.4 kHz | **16.97 dB** | **68.8 dBHz** |
| BGAN1.wav | ? | 2.048 MHz | 92.7 s | 1 centred | 14.1 dB | 65.9 dBHz |
| BGAN2.wav | ? | 2.048 MHz | 74.8 s | 1 centred | — | — |
| `...1547298000Hz` | 1547.298 MHz | 512 kHz | 60 s | 1 @ +0.1 kHz | 9.53 dB | 61.3 dBHz |
| `...1550398000Hz` | 1550.398 MHz | 512 kHz | 57 s | 2 @ ∓100 kHz | 5.58 / 9.41 dB | 57.4 / 61.2 |
| `...1553500700Hz` | 1553.5007 MHz | 512 kHz | 62 s | 1 @ −1.2 kHz | 8.88 dB | 60.7 dBHz |
| BGAN3,4,6,7,8,9,10,15 | ? | 192 kHz | 4–32 s | 1, fills band | unmeasurable | — |

**Primary development target: the 1543.100 MHz capture.** At +12.0 dB margin
over L3 it clears every FEC level including H6 — the only capture that does.
BGAN1 is the secondary cross-check (independent, different day and centre
frequency, so a genuine second test rather than a re-run).

Carrier spacing measured at **200.6 kHz** (the 200 kHz raster) from the
two-carrier 1550.398 MHz capture.

All captures show the 151.2 kBd timing tone. The apparent −0.8 Hz error is
FFT bin quantisation (3.9 Hz bins), not a clock offset.

512 kHz with SDR++ 4x decimation is a good operating point: it holds the
189 kHz carrier plus clean noise-reference regions either side, and the
decimation buys ~6 dB against 8-bit quantisation noise.

**Do not chase input level.** At rms ~0.017–0.033 FS the quantisation noise
floor sits roughly 13 dB below the measured noise floor, so thermal noise
dominates and extra ADC headroom would buy nothing. Gain is already maxed.

### Warning about the 192 kHz files

At 192 kHz the 189 kHz carrier occupies essentially the whole band, so there
is **no noise-only region in the capture**. Any Es/N0 or SNR estimate derived
from these files is meaningless — there is nothing to reference the noise
against. The previous project's "~3.7 dB Es/N0" figure is almost certainly
this artifact, and it appears to have motivated loosening the turbo decoder's
parity-agreement threshold from 0.85 to 0.58, which manufactures false
positives rather than recovering data.

Use the 2.048 MHz captures for anything involving signal quality.

## Measurements on BGAN1.wav

Single RRC carrier, not a composite (checked with a 131072-point PSD:
flat top to +/-68 kHz, symmetric rolloff, noise floor reached at +/-98 kHz;
-3 dB half-width ~76.5 kHz => Rs ~= 153 kHz, matching 151.2 within
measurement error).

| Quantity | Value | Method |
|---|---|---|
| Symbol rate | 151 200.0 Bd, < 6 Hz error | timing tone in \|x\|^2, 19.6 dB over floor |
| Carrier offset | +427.6 Hz | spectral centroid, converges in 2 iterations |
| C/N0 | 65.9 dBHz | PSD integral vs guard-band N0 |
| Es/N0 | 14.1 dB | as above |
| Es/N0 | ~11.4 dB | M4/M2^2 = 1.41 at best 1/8-symbol timing phase |
| Peak level | 0.115 of full scale | **18.8 dB of unused headroom** |

The two Es/N0 figures bracket the truth; the M4/M2^2 one is pessimistic
because timing was quantised to 1/8 symbol.

**The signal is strong.** Margin over the Annex B2 required C/N0 is +9.1 dB
for L3 and stays positive through H5. No antenna upgrade is warranted; the
decoding problem is entirely in software.

## Recording advice for future captures

1. **Raise the RTL gain.** Peaks at 0.115 FS on an 8-bit ADC means roughly 15
   of 255 counts are in use — about 4 effective bits. Target peaks near 0.5 FS.
   This is free SNR and costs nothing.
2. **Keep 2.048 MHz** (or 1.024 MHz, which has fewer strong in-band
   neighbours and so suffers less 8-bit dynamic-range pressure). Do not record
   at 192 kHz.
3. **AGC off**, fixed manual gain — amplitude wander smears the constellation
   and is indistinguishable from noise after the fact.
4. Note the exact centre frequency with each capture.

## Gotcha for the demodulator

Feedforward Oerder-Meyr timing estimated per block and then fitted across the
capture picked a **visibly wrong timing phase** on this signal, costing
several dB. A brute-force sweep over timing phase shows a clear, unambiguous
optimum. Whatever timing recovery ends up in the decoder must be validated
against that sweep, not assumed correct because it converged.

---

# Real-signal receiver results (30 July 2026)

## Correction to the Es/N0 figures above

The PSD-derived Es/N0 (16.97 dB for the 1543.1 MHz capture) measures **thermal
noise only**. The effective Es/N0 at the slicer, measured by M4/M2^2 with
optimal timing, is **~9.8 dB**. The ~7 dB difference is tuner phase noise,
8-bit quantisation and distortion -- real impairments the demodulator sees but
a PSD integral does not.

Evidence this is right, not a receiver fault: the real amplitude histogram is
near-identical to synthetic generated at Es/N0 9.8 dB.

**Use the M4/M2^2 figure for link budgeting, not the PSD one.**

## What is confirmed about the 1543.1 MHz signal

| property | value | how |
|---|---|---|
| symbol rate | 151 200.00 Hz | strongest cyclostationary line, unconstrained search |
| modulation | 16-QAM | amplitude histogram matches synthetic at same Es/N0 |
| bandwidth | 189 kHz | fine PSD, single RRC carrier |
| **frame period** | **exactly 12096 symbols** | self-correlation 0.292 at lag 12096 vs 0.013 elsewhere |
| effective Es/N0 | ~9.8 dB | M4/M2^2, calibrated |
| timing | optimal | brute-force tau sweep confirms the estimator's choice |

151.2 kBd uniquely identifies F80T4.5X-8B (Table 5.1/5.2 -- no other forward
bearer uses it), and a 12096-symbol frame period is exactly 80 ms at that rate.
Both agree with the bearer identification.

## The blocker

**No UW or pilot amplitude structure is present.** Table 5.10 plus Figure 5.17
require 40 contiguous UW symbols and 88 pilots, all at the outer corner
(|s| = 1.342 at unit mean power). Measured:

- best 40-symbol window in the frame-averaged amplitude profile: **0.997**
  (frame mean 0.932; a real UW at 9.8 dB would read ~1.38)
- binning positions by frame-to-frame coherence, mean |s| rises with coherence
  but tops out at **1.128** (only 3 positions above 0.8 coherence)
- no comb of 88 positions at 137 spacing

So the frame timing is unambiguous but the *known symbols* are not where -- or
not what -- the spec says.

## Ruled out

- clock/frame drift (self-correlation is sharp at exactly 12096)
- timing phase (brute-force sweep; estimator is at the optimum)
- matched filter roll-off (beta sweep is monotonic, no ISI signature)
- wrong bearer (151.2 kBd is unique to F80T4.5X-8B)
- wrong UW figure (clause 5.3.4.2 names Figure 5.17 for F80T4.5X explicitly)
- wrong UW hex or frame parameters (re-read from Figure 5.15 and Table 5.10)
- receiver bugs generally: the identical pipeline decodes synthetic
  **56/56 blocks bit-exact** at Es/N0 12 dB

## Traps found while investigating

1. Folding |s| mod 137 to find pilots **does not work** and looks like a
   negative result. Each frame's 40-symbol UW shifts the pilot phase by
   40 mod 137, so the comb smears across frames. Always fold mod 12096.
   A synthetic control caught this; without one it would have been believed.
2. UW correlation folded over all 249 frames gave PSR 11.5, while 30-frame
   chunks gave PSR 23-58 at *inconsistent* offsets. Strong per-chunk peaks at
   wandering positions are spurious matches against static content, not a
   moving frame boundary. ~40% of consecutive frames are near-identical
   (103/248 pairs correlate > 0.5), which gives spurious correlation plenty
   of material to work with.

## Next hypotheses, untested

1. The capture is a **transponded/translated** signal whose outer constellation
   points are compressed by HPA saturation. Would explain a smeared histogram
   and a UW that correlates weakly but lacks outer-corner amplitude.
2. The UW/pilots are present but at **reduced power** relative to the spec.
3. Some **F80T4.5X-8B variant** in service differs from the 2015 V1.1.1 text.

Worth capturing a second, different beam to see whether the structure appears
there; if it never appears on any carrier, the cause is systematic rather than
signal-specific.

---

# Carrier recovery fixed; UW/pilot structure confirmed absent (30 July 2026)

## Fixed: blind carrier recovery

`recv.blind_carrier_recovery` is two-stage and now CFO-invariant from 0 to
8 kHz (gridness 0.4509 at every offset tested). Three bugs were found:

1. A single global 4th-power estimate is too weak for 16-QAM (not
   constant-modulus, so the 4th-power line is far weaker than QPSK's).
2. Per-block estimation alone fails above ~50 Hz. At the ~300 Hz residual
   these captures have, a 256-symbol block rotates 183 degrees and smears
   itself. Synthetic spun by 300 Hz recovered to gridness 0.078 -- almost
   exactly the real signal's 0.080, which is how the bug was identified.
3. A fixed 45 degree rotation: corner points sit at 45+n*90 deg, so
   `angle(sum(s^4))/4` returns phi + 45 deg.

**The first two figures produced looked like rings and appeared to show a
signal with no constellation. That was entirely this bug.** The control that
caught it was running `blind_derotate` on *synthetic* -- which the original
script never did, because the synthetic panels were already phase-aligned.

Also fixed: `pipeline.synchronise` applied pilot-aided CFO correction *on top
of* blind recovery, re-spinning the constellation (gridness 0.45 -> 0.0014).
Pilot CFO is now a residual check only.

## The real signal is clean 16-QAM

See `work/constellations.png`. At 1543.1 MHz all 16 constellation points
resolve as distinct blobs sitting on the Table 5.3 grid. Gridness 0.409,
between synthetic at 9.8 dB (0.398) and 12 dB (0.442).

The front end -- channeliser, timing, matched filter, symbol extraction,
carrier recovery -- is therefore validated against *real* signal, not just
against its own synthetic.

## Confirmed absent: unique word and pilots

Rotation-invariant constancy per frame position,
`|mean_f(S^4)| / mean_f(|S|^4)`, which is immune to the 90 degree cycle slips
that a continuity-tracked blind recovery inevitably suffers over millions of
symbols.

Synthetic control through the identical code path:
**UW 0.811, pilots 0.821, data 0.420** -- clean separation.

| capture | Es/N0 | gridness | best 40-window | verdict |
|---|---|---|---|---|
| 1543.100 | 9.37 | 0.409 | 0.624 | no UW/pilots |
| 1547.298 | 8.27 | 0.392 | 0.617 | no UW/pilots |
| 1553.5007 | 5.86 | 0.339 | 0.516 | no UW/pilots |
| 1550.398 lower | 5.20 | 0.313 | 0.423 | no UW/pilots |
| 1550.398 upper | 10.06 | 0.387 | 0.426 | no UW/pilots |
| BGAN1.wav | 10.27 | 0.432 | 0.709 | best candidate, still fails |

BGAN1 was followed up in detail: the constancy-mask correlation and the UW
correlation independently agree on offset 7812 (z = 11.2; PSR 15.0 for H1 vs
2.7 for the next candidate), and UW/pilot constancy is genuinely elevated
(0.610 / 0.573 vs 0.406 for data). **It still decodes 0/80 blocks**, at the
UW-signalled level and at all ten levels forced uniformly.

The decisive measurement is amplitude, which is phase-invariant and assumes
almost nothing: UW and pilots must be outer-corner symbols, |s| = 1.342 at
unit mean power (~1.38 including noise bias). Across every capture, **no
40-symbol window anywhere in the frame exceeds ~1.02**, against a frame mean
of 0.93.

Outer-corner symbols clearly exist in these signals -- the constellation
resolves all 16 points. They simply never occupy a fixed frame position.

## Where this leaves it

Confirmed for every capture: 151.2 kBd (unique to F80T4.5X-8B), 16-QAM,
189 kHz, and a frame period of exactly 12096 symbols = 80 ms at that rate.
Not present: the unique word and pilots that Table 5.10 and Figure 5.17
require, and no combination of framing and coding level decodes.

Further receiver work is not the bottleneck; the front end is proven correct
on real signal. What is needed is information:

1. **TS 102 744-3-1** (free from ETSI) -- would confirm whether deployed
   framing matches the 2015 V1.1.1 text, and supplies the BCt-PDU format and
   the Bearer Control AVP carrying per-block coding levels.
2. Independent confirmation that these particular carriers are BGAN forward
   bearers rather than another 151.2 kBd 16-QAM service in the same band.

---

# Bearer scan of the new basebands, and a loaded-carrier control (30 July 2026)

`tools/scan_bearers.py` detects every carrier in a capture and identifies its
symbol rate from the |x|^2 cyclostationary line, against all five forward
bearer rates in Table 5.2.

**Every carrier in all five new 512 kHz basebands is 151.2 kBd 16-QAM.**
Six carriers total (1550.398 holds two). No 33.6 kBd, 84 kBd or 8.4 kBd bearer
appears anywhere, so the F80T1X-4B cross-check is not available from these
captures.

Discriminator worth remembering: only the 151.2 kBd line lands on its exact
nominal (151199.2 Hz, within one FFT bin of 151200). The apparent 33.6 and
84 kBd lines sit at 33412 and 83875 Hz -- off-nominal by hundreds of Hz, i.e.
noise, not carriers. Judge a candidate rate by whether the tone is at the
*exact* rate, not by its amplitude.

## The loaded-carrier control

`BGAN very strong lots of data ...19-09-04` is a genuinely different regime
from the earlier captures, and it removes an explanation that was still open.

| | idle capture (17-46-31) | loaded capture (19-09-04) |
|---|---|---|
| frame-pair correlation, median | 0.458 | **0.023** |
| near-identical pairs (> 0.5) | 103 / 248 | **0 / 373** |
| independent pairs (< 0.1) | 50 | **371** |

The earlier analysis showed ~40% of consecutive frames near-identical, which
raised the possibility that averaging over repeated idle frames was hiding or
distorting the UW/pilot structure. On this fully loaded carrier that cannot
apply -- essentially every frame is independent. Result:

- constancy mean 0.386, best 40-window **0.544** (synthetic UW 0.811, data 0.420)
- best 40-symbol amplitude window **0.976** (frame mean 0.928, UW wants ~1.38)
- decode **0/96 blocks**

**So the unique word and pilots are absent regardless of traffic load.** Idle
frame repetition is ruled out as a cause.

## Cross-check still wanted

F80T1X-4B (33.6 kBd, 42 kHz channel, 4 FEC blocks) uses the *same* 40-symbol
UW and 88 pilots, and the Annex C tables for it are present in the archives we
hold, so it is fully decodable with the existing code. Finding one requires
tuning to a narrower carrier: 42 kHz wide instead of 189 kHz, so roughly a
quarter the width on the waterfall. Forward bearers occupy 1518-1559 MHz.

If a 33.6 kBd carrier shows the UW structure, the anomaly is specific to the
151.2 kBd carriers here. If it does not, the cause is systematic and lies in
the path shared by both -- receiver or standard interpretation.

---

# 33.6 kBd cross-check: the anomaly is systematic (30 July 2026)

`Small BGAN baseband_1532200400Hz_20-37-32` at 1532.2004 MHz, 512 kHz, 4 narrow
carriers at -175.4, -25.9, +24.6, +74.1 kHz.

| property | value |
|---|---|
| symbol rate | **33 600.3 Hz** — exactly nominal, all four carriers |
| bandwidth | 37-41 kHz (42 kHz channel) |
| frame period | **exactly 2688 symbols** (0.176 at lag 2688 vs 0.015 either side; 5376 also present) |
| modulation | **QPSK**, i.e. F80T1Q-4B — see below |
| UW / pilots | **absent** |

2688 symbols is exactly 80 ms at 33.6 kBd. So a second, independent bearer type
lands on exact Table 5.2 / Table 5.10 values for symbol rate and frame period
while showing no unique word and no pilots.

**Conclusion: the missing sync structure is systematic, not specific to the
151.2 kBd carriers.** Ten carriers across two bearer types and two widely
separated frequencies (1532 vs 1543-1553 MHz) all behave the same way.

## Modulation is QPSK, not 16-QAM

`tools/scan_bearers.py` initially labelled these 16-QAM. That was wrong and the
classifier has been left deliberately conservative as a result. M4/M2^2 came out
**1.239-1.295**, which is *below* 16-QAM's floor of 1.32 — impossible at any
SNR, because noise only moves the statistic upward from the constellation's own
value. QPSK's floor is 1.00 and it reads ~1.25 at 8 dB. Also mean|s| is uniform
at 0.95 across UW, pilot and data positions, as constant-modulus demands.

**Do not classify modulation from M4/M2^2 alone** — the QPSK-plus-noise and
16-QAM-plus-noise ranges overlap. Use the amplitude histogram or the
constellation.

## Two statistics that do NOT work, and why

1. **`|mean(S^4)|/mean(|S|^4)` is degenerate for QPSK.** All four QPSK points
   map to the same s^4, so the statistic tends to 1 at every position whether
   the symbol is fixed or random. It read 0.571 for *data* here versus 0.420
   for 16-QAM data. It is only meaningful for 16-QAM.

2. **Autocorrelating the coherence profile to find the pilot comb.** Real
   scored 0.767 at lag 30, the synthetic control 0.754 — no discrimination,
   because both are dominated by broad structure rather than the comb.

## The statistic that does work

Frame-pair coherence on **raw, non-derotated** symbols,
`|mean_f( S[f,k] * conj(S[f+1,k]) )|`. CFO contributes a constant phase advance
per frame so it cancels; it is modulation-agnostic; and it needs no carrier
recovery, so it cannot be spoiled by cycle slips.

Calibrated against a synthetic QPSK frame carrying a real UW and pilots:

| | UW | pilots | data |
|---|---|---|---|
| control, Es/N0 8 dB | **1.003** | **1.000** | 0.046 |
| control, Es/N0 12 dB | 1.006 | 1.002 | 0.042 |
| real -175.4 kHz | 0.257 | 0.197 | 0.144 |
| real -25.9 kHz | 0.278 | 0.197 | 0.169 |
| real +24.6 kHz | 0.347 | 0.235 | 0.185 |
| real +74.1 kHz | 0.260 | 0.179 | 0.137 |

The control separates 20:1. The real carriers separate about 1.8:1, and their
highest-coherence positions cluster in a narrow band (1348-1889 of 2688) with
irregular spacings of 2-11, not a comb at 30. That is localised static content,
not pilots.

## What is now excluded

- specific to the 151.2 kBd carriers (no: same on 33.6 kBd)
- specific to one frequency or beam (no: 1532 vs 1543-1553 MHz)
- idle-frame repetition confusing the averages (no: ruled out by the loaded
  capture, and these carriers show data coherence 0.14-0.19 either way)
- receiver bugs (the same pipeline decodes synthetic 4150/4150 bit-exact)
- wrong frame geometry (both bearers' frame periods come out at exactly the
  Table 5.10 value, from an assumption-free self-correlation scan)

## Leading hypotheses, in order

1. **A symbol-level randomisation tied to frame number**, applied after frame
   assembly. This would leave the constellation and the frame period intact
   while making no position constant across frames — which is precisely the
   observed signature. Clause 5.3.7's "Unique Words and Pilot Symbols are not
   scrambled" refers to the *bit* scrambler before FEC, and would not preclude
   it. Caveat: for the 16-QAM carriers a pure *rotation* is excluded, because
   amplitude is rotation-invariant and pilots still fail to reach 1.342; it
   would have to remap the constellation, not just rotate it.
2. **The deployed framing differs from V1.1.1** in where the UW/pilots sit.
3. **These are not TS 102 744 signals**, despite matching symbol rate,
   bandwidth, modulation family and 80 ms frame period on two bearer types.
   Hard to credit given how exactly the frame periods land, but not excluded.

What would settle it: a capture with independently known content, or output
from any working BGAN decoder to compare against.

---

# A false positive, and the control that caught it (30 July 2026)

Recorded because the false lead was persuasive and cost real time, and because
the mistake is easy to repeat.

## What looked like a breakthrough

Long averages destroy frame-pair coherence if the carrier offset drifts, since
the per-frame-pair phase advance is 2*pi*df*0.08. So short 16-frame windows with
a tracking loop were tried instead. Results looked compelling:

- short windows gave mask-correlation z up to 8.6, against a synthetic null
  whose ceiling was 4.4
- a tracking loop locked 13/46 windows on the 33.6 kBd carrier and 23-41/46 on
  the 151.2 kBd ones, while the same procedure locked **0/46** on synthetic
  data containing no UW, across five trials
- at the tracked offsets, pilots measured 1.08-1.30 against data 0.24-0.43;
  a perfectly coherent 16-QAM pilot gives |P|^2 = 1.80, so this read as ~72%
  coherence, far too large to be explained by selection

## Why it was wrong

The reported quantity was measured **at the offset chosen to maximise it**.
That is circular, and the synthetic null did not license it: synthetic QPSK with
30% frame repetition does not reproduce the real signal's short-range
correlation structure, which is what lets the mask find spurious alignments.

The correct control is a train/test split on the real data itself: pick the
alignment using the first half of the frames, then measure only on the held-out
second half.

| capture | offset | \|s\| pilots | \|s\| data | coh pilots | coh data |
|---|---|---|---|---|---|
| BGAN1.wav | 8362 | 0.928 | 0.930 | 0.366 | 0.362 |
| BGAN2.wav | 6856 | 0.908 | 0.909 | 0.291 | 0.293 |
| 1543.1 idle | 5195 | 0.924 | 0.931 | 0.481 | 0.498 |
| 1543.1 loaded | 4394 | 0.921 | 0.908 | 0.333 | 0.315 |
| 1547.298 | 1624 | 0.927 | 0.924 | 0.517 | 0.514 |

A real pilot would show |s| ~1.38 and coherence ~1.80 against data at ~0.95 and
~0.26. **Held out, there is no separation whatsoever.**

## Rules to keep

1. **Never report a statistic measured at the offset that maximises it.** Split
   train/test, always.
2. **A synthetic null is not sufficient** when the real signal has structure the
   synthetic lacks. Build the null from the real data (held-out alignment,
   circular shifts, or a deliberately wrong offset).
3Per-frame UW level detection was also only marginally above its wrong-offset
   control (fraction with peak/runner-up > 1.5: 0.33 vs 0.13), and the offset
   had itself been picked to maximise that correlation. Same defect.

## Standing conclusion, unchanged

No unique word and no pilots, on ten carriers across two bearer types
(151.2 kBd 16-QAM and 33.6 kBd QPSK) and two frequency regions, whether idle or
fully loaded. Symbol rates and frame periods match the standard exactly; the
sync symbols do not appear.

---

# The unique word IS present (30 July 2026) - supersedes the "absent" conclusion

## The prior decoder never worked, and that is how we got here

`C:\Users\Gaming\Desktop\BGAN` (the earlier DeepSeek-built decoder) was run
against the labelled synthetic reference, which it had never had:

- input: `SYNTHETIC_..._L3_30dB_clean` -- a clean, spec-compliant 30 dB signal
- its sync worked: found level L3 correctly, frames at exact 12096 multiples
- it reported **320/320 blocks decoded**, 80000 bytes
- **bit agreement with ground truth: 49.60%**

Checked across MSB/LSB bit order, inverted, and against both the plain and
scrambled payload: every variant 49.2-50.8%. Byte entropy 7.998 of 8.0 bits,
all 256 byte values present, block-to-block agreement 0.4997.

Its output was random noise. The "IP packets" and the readable text came from
pattern-matching noise: printable-ASCII fraction was 0.3713 against 0.363
expected for random bytes, so `strings` over 80 kB of noise always yields
fragments, and scanning random bytes for plausible IPv4 headers always hits.

Mechanism: the re-encode **agreement threshold of 0.58** accepted 100% of
blocks while every one was wrong. It never rejected anything, so it always
looked successful.

Its front end is sound, though, and one idea from it is better than mine.

## The differential UW correlator finds the UW

From their `uwsync.cpp`: the UW is BPSK-like, so the differential
`d_k = s_k * conj(s_{k-1})` is a known real +/-1 pattern. This is
**carrier-immune**, so unlike my frame-pair coherence it can be folded across
frames without phase drift cancelling it.

Correlate `d` against the 39-tap differential pattern per level, square, fold
modulo the frame period, and compare the winner against the runner-up:

| | winner PSR | ratio |
|---|---|---|
| synthetic 30 dB (UW known present) | 124.64 | 6.75 |
| synthetic 12 dB | 109.88 | 6.56 |
| **BGAN1, period 12096** | **16.71** | 5.32 |
| **1543.1 idle, period 12096** | **8.00** | 3.86 |
| **1543.1 loaded, period 12096** | **7.61** | 2.96 |
| BGAN1, wrong periods (12000/12100/11900/12200/12345) | 1.29-1.45 | 1.09-1.22 |
| synthetic, wrong period 12000 | 2.18 | 1.52 |

The true-period fold sits **6-12x above the wrong-period null**. Real detection.

## Why the earlier tests missed it

Amplitude and frame-to-frame coherence both assume the UW and pilots are
transmitted at the **outer corner** (|s| = 1.342), per Figure 5.17. The
differential correlator only needs the UW's phase-transition pattern and is
indifferent to amplitude.

The two results together say: the UW bit pattern is present at the specified
position and frame period, but **not at the outer-corner amplitude**. Every
"no UW/pilots" statement earlier in this file should be read as "no
outer-corner UW/pilots".

## Correction: the verifier is NOT false-positive-free on real data

`pipeline.verify_block` at `margin_nats=1200` measured 0 false positives over
4150 synthetic blocks. On real data it is **~2%**:

- wrong frame offsets, with per-frame level detection: **33/1600 = 2.1%**
- the offsets proposed by the UW search: 1.0%, 1.5%, 3.0%

Indistinguishable. The synthetic figure holds only because the LLR scaling
matches the channel there; on real data `n0` is wrong, so the likelihood ratio
is miscalibrated. **Any real-data decode claim needs a wrong-offset control at
matched search complexity.** Decoding still does not work.

## Where this leaves it

Confirmed: 151.2 kBd, 16-QAM, exact 12096-symbol frame period, and a UW bit
pattern detectable at the frame period by differential correlation.
Not working: decoding, and the UW/pilot amplitude does not match Figure 5.17.

Next: the constellation amplitude assignment for UW/pilots is the prime
suspect, since that is precisely the assumption that separates the test which
succeeds from the two that fail.

---

# RESOLVED: the signals decode. Corrections to everything above. (31 July 2026)

BGAN19.wav settled it. Every "cannot decode" and "no UW/pilots" conclusion
earlier in this file is **wrong** and is superseded by this section.

## The prior decoder works, and its output is real

Running it on `E:/SDRPPrecordings/BGAN19.wav` yields, in the payload:

    HTTP/1.1 206 Partial Content
    Connection: keep-alive
    1b6f4a75c93a6937389f7eadf6bfe96e0     (repeated ~4x)
    91daabd14407666c61e0d97a2d44aa4f8     (repeated ~4x)

Against a random-byte control of the same length:

| | BGAN19 decode | random control |
|---|---|---|
| byte entropy | **6.771** / 8.0 | 8.000 |
| zero bytes | **19.45%** | 0.39% |
| printable runs >= 16 chars | **16** | **0** |

Entropy well below random, a 50x excess of zeros (the clause 5.4 zero padding),
and repeated 32-character strings, which random data cannot produce. Parity
agreement reaches **0.978** with 232 blocks above 0.90.

## I was wrong twice, in opposite directions

1. First I concluded their decoder "never worked", from 49.60% bit agreement
   against my synthetic. **Wrong.** Its turbo decode is bit-perfect; the whole
   49.6% was a scrambler phase difference. `their_output XOR my_truth` is one
   fixed 2000-bit mask, identical across all 320 blocks, and that mask is
   exactly a phase of the same m-sequence. Applying it recovers my ground truth
   for **320/320** blocks.
2. Then, on BGAN9, I concluded the reverse was safe and that real captures do
   not decode. Also wrong -- BGAN9 genuinely does not decode, but **BGAN19
   does**. Testing one quiet file and generalising was the error.

## What was actually wrong in my receiver

1. **Scrambler phase.** The spec gives the seed (0x6959) but not the read-out
   order. Correct is **LSB-first preload, sequence starting after the 15 load
   bits** -- equivalently m-sequence phase 12095 from MSB-first. Now fixed in
   `bgan/mod.py` and confirmed bit-exactly against a known-good payload.
2. **Per-block pilot normalisation and phase detrend.** Scale each block so
   mean|pilot| = 1.34164, then detrend phase with a linear fit through that
   block's 11 pilot angles. A frame-wide smoothed phase produced **zero**
   decodes; per-block produces 0.974. This was the single biggest fix.
3. **Acquisition must be per-frame, never folded.** The frame position drifts
   (~-2.7 symbols/frame on BGAN19, i.e. an effective period near 12093.3, a
   -220 ppm resampling scale error). Folding the differential-UW correlation
   across frames smears the peak onto a sidelobe family: it returned 9184 when
   the decodable base was 8712, and nothing decoded. A single-frame scan finds
   it easily -- correlation 66.9 at the right offset against ~17 at +/-4
   symbols.

With those three, my decoder reproduces their frame-177 block-0 output
**bit-exactly** (XOR all-zero, agreement 1.0000, parity agreement 0.974).

## Why the earlier UW/pilot tests failed

They were run at frame offsets from my own folded acquisition, which the point
above shows were wrong. At the *correct* offset the block decodes. Note the
pilots still do not measure at outer-corner amplitude (0.966 vs 0.923 for data
even at the correct offset) -- their normalisation acts as a plain gain that
`s2` absorbs, so decoding does not depend on it. That anomaly is still
unexplained but is not load-bearing.

## Remaining work

Robust frame tracking under drift. Their second-order PLL, re-acquiring the UW
per frame within +/-100 symbols, gets 222 data blocks. My best so far is 53.
A linear period fit leaves residuals up to 203 symbols, so the drift is not
linear. This is the only piece still missing; the coding chain itself is
verified correct.

`tools/decode_real.py` holds the working chain.

## Carrier selection on multi-carrier captures (Aug 2026)

Reported symptom: several 512 kHz captures failed to decode, apparently
locking onto interference beside the signal. The cause turned out to be two
different things, neither of them RFI.

### 1. The strongest carrier is not always the right one

`baseband_1550398000Hz` holds **two** F80T4.5X-8B carriers, at -98.5 kHz and
+103.4 kHz. Neither is at 0 Hz, and `find_carrier` picked the stronger
(+103.4 kHz has 1.39x the in-band power). That one probes as pure noise:

    centre  -98.5 kHz  ->  rs   -0.6 ppm  UW metric 59.5  UW = R x98
    centre +103.4 kHz  ->  rs -181.3 ppm  UW metric 20.4  UW = scattered

Power is the wrong selector. `pick_carrier` now probes each candidate with a
cheap UW correlation (no turbo decoding) and ranks by how far it stands above
that capture's own noise floor, measured from deliberately wrong offsets in
the same file. Carriers that decode sit at 2.5-3.5x; ones that do not sit at
1.0-1.1x. A symbol clock more than 50 ppm from nominal is also disqualifying:
the failures came back at -181 and +100 ppm, which is the estimator finding no
tone and latching onto the edge of its search range.

### 2. Some captures hold no F80T4.5X-8B carrier at all

`Small BGAN baseband_1532200400Hz` contains **four 33.6 kBd carriers**
(F80T1X-4B, 42 kHz) and no 151.2 kBd carrier. Decoding it produced noise and
*still* reported a 97% yield forecast. Two fixes:

- `NoCarrier` is raised, and both the CLI and GUI report what the capture
  actually contains by symbol rate instead of failing obscurely.
- The forecast was calibrated against the file's own 90th percentile, so a
  capture where every frame scores ~20 had "100% of frames strong". It now
  compares against the noise floor measured at wrong offsets in the same file.

### Two bugs found while fixing the above

**refine_centre returned NaN on an empty band.** `sum(fr*w)/sum(w)` with no
power above the noise floor gives 0/0, and the NaN propagated silently through
the whole chain. Latent since the beginning; it only surfaced once candidate
carriers started being probed at positions holding no signal.

**Phantom candidates on a carrier's own shoulders.** The suppression guard in
`find_carriers` sits one bandwidth from each peak, so a single carrier also
produces candidates at +/-189 kHz. Those probe almost identically -- ratios
differing in the 4th decimal -- because `refine_centre` drags them back
towards the same carrier, but only partway, leaving ~1.3 kHz of residual
offset. That is invisible in a spectrum and fatal to the pilots: at 1.3 kHz
the phase advances ~7.4 rad between pilots 137 symbols apart, the unwrap in
`prepare()` breaks, and a capture that decodes 944/984 blocks decodes 0.
Candidates are now deduplicated on their *refined* centre, keeping the
strongest of each group.

### Results

    capture                        before        after
    1543.100 (lots of data)      944/984      944/984   unchanged
    1547.298 (two carriers)       92/1184     173/984    7.8% -> 17.6%
    1532.200 (33.6 kBd only)     noise+97%    NoCarrier, lists what is there
    1550.398 (two carriers)      wrong one    right one picked, still 0 decoded
    1543.100 (17-46-31)          0            0
    1553.500                     0            0
    BGAN19 / synthetic           unchanged    unchanged (4149/4149 bit-exact)

### Still unexplained

Three captures frame correctly and decode nothing. `1543.100_17-46-31` is the
clearest case: a single clean carrier, peak SNR 19.9 dB (higher than the file
that decodes 95.9%), timing tone 20.1 dB, UW metric 71.8 with L3 detected on
all 123 frames -- and every block decodes at chance.

Ruled out by measurement:

- **carrier offset** -- residual CFO is +256, -253, -443 Hz, all inside the
  552 Hz limit where the pilot unwrap breaks
- **clipping** -- peaks reach 5-8% of full scale, zero samples near rail
- **weak signal** -- 19.9 dB peak SNR, above the file that works
- **adjacent carriers** -- scan_bearers finds exactly one carrier
- **wrong bearer** -- F80T4.5X-8B is the only 151.2 kBd forward bearer defined

What is measurably different is constellation quality after pilot correction:
EVM 0.270 and 0.320 against 0.219 for the capture that decodes. So the symbols
really are worse, but not for any reason yet identified. Next thing to try is
the timing phase resolution (ntau) and the matched-filter roll-off, since both
affect EVM without touching PSD SNR.
