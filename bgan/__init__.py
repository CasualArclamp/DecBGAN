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
    carrier   residual carrier offset, measured on the unique words
    pipeline  synchronise + decode_frame, end to end
    bctrl     ForwardBearerCodeRateParam, per-block level resolution (3-1)
    bctpdu    Bearer Control PDU framing, located by its CRC (3-1 5.1.7)
    bulletin  BulletinBoard SDU and AVP list walking (3-1 clause 5.4.3)
    pcapout   pcap writers and IPv4 carving
    findings  certificates, DNS, HTTP and TLS carved from a payload
    sip       SIP messages carved from a payload, grouped into dialogs
    tx        reference transmitter, used to validate the receiver
    update    version check against the published VERSION file (opt-out)

Importing this package does not pull in numba or scipy; submodules are left to
be imported explicitly so that a caller who only wants, say, `bgan.spec` does
not pay for a JIT compile.

The Annex C data tables are ETSI copyright and are not distributed here. See
README.md for where to fetch them; `bgan.annex.annex_dir` looks in the
repository root, `work/` and `annex/`.
"""

# VERSION at the repository root is the source of truth, so that the update
# check can compare against one short file rather than parsing source. The
# literal below is only reached when that file is absent -- a vendored copy
# of the package without the repository around it.
def _read_version():
    from pathlib import Path
    try:
        v = (Path(__file__).resolve().parent.parent
             / "VERSION").read_text(encoding="utf-8").strip()
        return v or "0.4.0"
    except OSError:
        return "0.4.0"


__version__ = _read_version()

__all__ = [
    "annex", "bctpdu", "bctrl", "bulletin", "carrier", "decoder", "mod", "pcapout",
    "findings", "pipeline", "recv", "sip", "spec", "turbo", "tx", "update",
]
