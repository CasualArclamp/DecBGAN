"""BGAN forward-link decoder — Inmarsat Family SL, ETSI TS 102 744.

A receiver for the forward bearer **F80T4.5X-8B**: 151.2 kBd 16-QAM, 189 kHz,
80 ms frames of 12096 symbols carrying 8 turbo-coded FEC blocks.

Typical use is through `tools/decode_wav.py` or `tools/gui.py`, but the layers
are usable directly:

    from bgan import recv, pipeline
    x, sr = recv.load_wav_iq("capture.wav")
    sym, info = pipeline.synchronise(x, sr)
    frame = pipeline.decode_frame(sym, info, 0)

Submodules, roughly bottom-up:

    spec      bearer parameters, unique words, constants (Table 5.2, 5.10)
    annex     Annex C.1/C.2 table loaders (interleaver, puncturing, mapping)
    turbo     SRCC encoder and termination (clause 5.3.8)
    decoder   max-log-MAP BCJR and soft demapper, numba-compiled
    mod       16-QAM mapping, scrambler, frame assembly (clause 5.3)
    recv      channelisation, carrier location, timing, frame sync
    pipeline  synchronise + decode_frame, end to end
    bctrl     ForwardBearerCodeRateParam, per-block level resolution (3-1)
    bulletin  BulletinBoard SDU and AVP list walking (3-1 clause 5.4.3)
    pcapout   pcap writers and IPv4 carving
    tx        reference transmitter, used to validate the receiver

Importing this package does not pull in numba or scipy; submodules are left to
be imported explicitly so that a caller who only wants, say, `bgan.spec` does
not pay for a JIT compile.

The Annex C data tables are ETSI copyright and are not distributed here. See
README.md for where to fetch them; `bgan.annex.annex_dir` looks in the
repository root, `work/` and `annex/`.
"""

__version__ = "0.2.0"

__all__ = [
    "annex", "bctrl", "bulletin", "decoder", "mod", "pcapout",
    "pipeline", "recv", "spec", "turbo", "tx",
]
