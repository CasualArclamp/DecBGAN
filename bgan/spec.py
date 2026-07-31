"""Constants from ETSI TS 102 744-2-1 V1.1.1 (2015-10), forward link.

Every value here has been read out of the specification and cross-checked
against a rendered image of the source figure/table -- the PDF's figures are
JBIG2 images, and several of the tables extract as text in a mangled column
order, so text extraction alone is not trusted.

Provenance is recorded per item as `spec 2-1 <clause/figure>`. Do not edit a
value here without re-checking it against the document.
"""

# --- Frame geometry, Table 5.10 (clause 5.3.3) -------------------------------
# Verified: 40 + 88 + 11968 == 12096 == 80 ms * 151.2 kBd, and
#           11968 / 136 == 88 pilots, and 8 * 1496 == 11968.

class Bearer:
    def __init__(self, name, rs, bits_per_sym, mos, uw_syms, pilot_syms,
                 data_syms, teo, nblocks):
        self.name = name
        self.rs = rs                      # symbol rate, Bd
        self.bits_per_sym = bits_per_sym
        self.mos = mos                    # modulator output symbols per 80 ms frame
        self.uw_syms = uw_syms            # start unique word length, symbols
        self.pilot_syms = pilot_syms      # total pilots per frame
        self.data_syms = data_syms        # data symbols between consecutive pilots
        self.teo = teo                    # turbo encoded output, symbols per FEC block
        self.nblocks = nblocks            # FEC blocks per frame

    @property
    def encoded_syms(self):
        return self.teo * self.nblocks

    @property
    def trailing_syms(self):
        """Data symbols after the last pilot.

        Clause 5.3.5 places a pilot after every `data_syms` symbols, giving
        `pilot_syms` pilots. That does not generally consume all the encoded
        symbols, so a run of data symbols follows the final pilot:

            MOS = uw + pilot_syms*(data_syms + 1) + trailing

        F80T4.5X-8B is the ONLY row of Table 5.10 where this is zero
        (88*136 == 11968 exactly), so code written against that bearer alone
        silently assumes an even division. The other twelve rows need this --
        e.g. F80T1X-4B leaves 8 and FR80T2.5X64-7B leaves 77. Verified against
        every row of Table 5.10.
        """
        return self.mos - self.uw_syms - self.pilot_syms*(self.data_syms + 1)

    def check(self):
        assert self.uw_syms + self.pilot_syms + self.encoded_syms == self.mos
        assert self.trailing_syms >= 0, self.name
        assert self.pilot_syms*self.data_syms + self.trailing_syms             == self.encoded_syms, self.name
        return True


# spec 2-1 Table 5.10 -- only the bearer we target is filled in for now.
F80T45X8B = Bearer(
    name="F80T4.5X-8B", rs=151200, bits_per_sym=4,
    mos=12096, uw_syms=40, pilot_syms=88, data_syms=136,
    teo=1496, nblocks=8,
)
assert F80T45X8B.check()

# Other Table 5.10 rows, added for the 33.6 kBd cross-check. F80T1X-4B is
# 16-QAM and reuses the Figure 5.17 UW mapping (clause 5.3.4.2 names both
# F80T1X-4B and F80T4.5X-8B). F80T1Q-* are QPSK and use Figure 5.16 instead,
# so they need a different UW/pilot constellation before they can be decoded.
F80T1X4B = Bearer(
    name="F80T1X-4B", rs=33600, bits_per_sym=4,
    mos=2688, uw_syms=40, pilot_syms=88, data_syms=29,
    teo=640, nblocks=4,
)
assert F80T1X4B.check()

F80T1Q4B = Bearer(
    name="F80T1Q-4B", rs=33600, bits_per_sym=2,
    mos=2688, uw_syms=40, pilot_syms=88, data_syms=29,
    teo=640, nblocks=4,
)
assert F80T1Q4B.check()

F80T1Q1B = Bearer(
    name="F80T1Q-1B", rs=33600, bits_per_sym=2,
    mos=2688, uw_syms=40, pilot_syms=88, data_syms=29,
    teo=2560, nblocks=1,
)
assert F80T1Q1B.check()

FRAME_MS = 80
ROLLOFF = 0.25          # spec 2-1 Annex B.1 (alpha column), and clause 5.2.3


# --- Unique words, Figure 5.15 ----------------------------------------------
# 40 bits each, MSB first, as 10 hex digits.
# Verified against a 210 dpi render of Figure 5.15 (all 15 rows).
UNIQUE_WORDS = {
    "L8": "E4564ADABD",
    "L7": "BED8B3EAD2",
    "L6": "F2F5F496A6",
    "L5": "C911364 28A".replace(" ", ""),
    "L4": "F9A42BB1AB",
    "L3": "D4E357299C",
    "L2": "4CB9D9D174",
    "L1": "6AAF7A6E4E",
    "R":  "C240E96587",
    "H1": "514BB8BA62",
    "H2": "B5896CCDDF",
    "H3": "A87B0DA6C9",
    "H4": "5A1A679D6F",
    "H5": "61FEA54943",
    "H6": "A32AD281C4",
}
assert all(len(v) == 10 for v in UNIQUE_WORDS.values())

# spec 2-1 Annex B.1: F80T4.5X-8B defines only these ten coding levels.
# The UW signals the level of the *first* FEC block in the frame; levels of
# subsequent blocks come from a Bearer Control AVP (spec 2-1 clause 5.3.4.0,
# referring to TS 102 744-3-1). Searching the other five UWs would only add
# false-lock opportunities.
LEVELS_F80T45X8B = ["L3", "L2", "L1", "R", "H1", "H2", "H3", "H4", "H5", "H6"]

# Information bits per FEC block, spec Annex B2 (FWD_Bearers sheet),
# for F80T4.5X-8B. TEO is 1496 symbols = 5984 channel bits in all cases.
INFO_BITS_F80T45X8B = {
    "L3": 2000, "L2": 2320, "L1": 2664, "R": 3000, "H1": 3440,
    "H2": 3840, "H3": 4224, "H4": 4640, "H5": 4920, "H6": 5120,
}

# --- Unique word / pilot symbol mapping, Figure 5.17 -------------------------
# For 16-QAM (X) bearers the UW uses only the two outermost constellation
# points on the I=Q diagonal:
#     UW bit 0 -> (b3,b2,b1,b0) = (0,1,0,1) -> (-3D/2, -3D/2)
#     UW bit 1 -> (b3,b2,b1,b0) = (1,1,1,1) -> (+3D/2, +3D/2)
# so the UW is effectively BPSK along the diagonal.
UW_BIT_NIBBLE = {0: 0b0101, 1: 0b1111}

# spec 2-1 clause 5.3.5: "The PSs for all bearers are identical to the Unique
# Word Symbol upper right quadrant" -> the constant 1111 point.
PILOT_NIBBLE = 0b1111


# --- Scrambler, clause 5.3.7 -------------------------------------------------
# 15-stage PN, polynomial 1 + X + X^15, re-initialised at the start of every
# FEC block, applied to information bits *before* FEC encoding.
# UWs and pilot symbols are not scrambled.
SCRAMBLER_POLY_TAPS = (1, 15)
SCRAMBLER_INIT = 0x6959          # spec writes "110 1001 0101 1001" = 6959h
SCRAMBLER_LEN = 15
# NOTE: the spec prints a 16-bit pattern for a 15-stage register. Which 15 of
# those bits load into the register (and in which order) is ambiguous from the
# text alone and MUST be resolved empirically -- see docs/OPEN_QUESTIONS.md.


# --- SRCC, clause 5.3.8.2 ----------------------------------------------------
# Two identical 16-state systematic recursive convolutional encoders.
SRCC_BACKWARD_OCT = 0o23         # 1 + X^3 + X^4
SRCC_FORWARD_OCT = 0o35          # 1 + X + X^2 + X^4
# The un-interleaved encoder is flushed to state zero with 4 tail bits chosen
# from Table 5.11 by end state; the interleaved encoder is not flushed.
