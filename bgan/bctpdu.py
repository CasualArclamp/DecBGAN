"""Bearer Control PDU framing, found by its CRC (TS 102 744-3-1 clause 5.1.7).

This is what interrupts the byte stream and stops documents reassembling. It
was first found by reverse engineering -- a 7-byte run inserted mid-word in
recovered DER text -- and is confirmed here against the specification.

Structure, clause 5.1.2:

    FwdBCtPDUHeader | BCtSDU 1 .. BCtSDU n | bct-payload | CRC

where bct-payload is a BCnPDU or ALComPDU, both `OCTET STRING (SIZE(0..255))`.
That 255-octet ceiling is why the empirically measured period was ~260 bytes:
a full user-data PDU plus its header and CRC.

The CRC is the useful part, because it validates a parse rather than
suggesting one. Clause 5.1.7 specifies generator x16 + x12 + x5 + 1, register
initialised to all ones, data clocked in from bit 1 (the LSB) of the first
octet, and the ones complement appended low octet first. Clocking a whole PDU
*including* its CRC leaves 0xF0B8. That is standard HDLC/X-25 FCS, and the
residue is a self-test -- `crc_residue` is checked against it at import.

WHAT THIS DOES NOT DO. It locates PDU boundaries; it does not reassemble. The
BCtSDU walk and the per-connection ordering live in TS 102 744-3-3 and -3-4
and are not implemented. Concatenating detected payloads recovers 847 bytes of
a gzipped XML document that is several kB long, so this is a foundation, not
a finished demultiplexer.
"""
from __future__ import annotations

GOOD_RESIDUE = 0xF0B8
MAX_PDU = 264          # 255-octet payload + header + CRC, with slack
MIN_PDU = 20

_TAB = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0x8408 if _c & 1 else _c >> 1
    _TAB.append(_c)


def crc_residue(data, crc=0xFFFF):
    """Clock `data` through the BCtPDU CRC register. See GOOD_RESIDUE."""
    for b in data:
        crc = (crc >> 8) ^ _TAB[(crc ^ b) & 0xFF]
    return crc


def crc_append(body):
    """The two CRC octets for `body`, low octet first, as clause 5.1.7 maps them."""
    c = crc_residue(body) ^ 0xFFFF
    return bytes([c & 0xFF, (c >> 8) & 0xFF])


def crc_ok(pdu):
    """True when a PDU including its two CRC octets clocks to the residue."""
    return len(pdu) >= 3 and crc_residue(pdu) == GOOD_RESIDUE


# Self-test at import: the spec's own stated residue, which pins the
# polynomial, the bit order and the complement all at once.
assert crc_ok(b"\x0f\xaa\x55" + crc_append(b"\x0f\xaa\x55"))


# First header octet, clause 5.1.4:
#   bct-sdu-follows(1) | length-present(1) | bct-pdu-addr-type(2)
#   | comsig-or-ext-addr(1) | type(3)
#
# A CRC hit alone is not evidence. Trying ~245 lengths at every position finds
# one about every 268 bytes by chance, and measured over 300 kB of real
# payload the raw count was 1096 against 1122 expected -- 0.98x, pure noise.
#
# What is *not* noise is which header octet those hits carry. Three values
# take 510 of the 1096 where ~13 would be expected if the octet were random,
# i.e. 39x over-represented:
#
#   0xc9  1 1 00 1 001  sdu-follows, length, broadcast  -- carries BulletinBoard
#   0x58  0 1 01 1 000  length present, connection PDU  -- user data
#   0x18  0 0 01 1 000  no length octet, connection PDU -- user data
#
# 0xc9 is independently corroborated: it is the header value already measured
# on every one of 490 block-0 payloads on BGAN19. And 0x18 against 0x58 is
# exactly the 7- against 8-byte insertion found by diffing repeated copies of
# the same certificate, the extra octet being the length field.
#
# So detection requires CRC *and* header, the same two-condition discipline
# the block-acceptance test uses.
# Control on the combined test: 9 PDUs found in 300 kB of random bytes,
# against 39 in a 12 kB window of real payload -- one per 33 000 bytes against
# one per 308, so 108x denser where there is real framing.
KNOWN_HEADERS = {0x18, 0x58, 0xc9}


def header_len(first_octet):
    """Header octets before the payload, from the length-present bit."""
    return 4 if first_octet & 0x40 else 3


def find_pdus(blob, start=0, stop=None, headers=KNOWN_HEADERS,
              lo=MIN_PDU, hi=MAX_PDU):
    """Scan for CRC-valid PDUs whose header octet is a known one.

    Returns [(offset, length)]. Greedy and non-overlapping: on a hit the scan
    resumes at the end of that PDU, which is what makes a run of them
    meaningful -- consecutive valid CRCs are 65536^-n against chance.
    """
    out = []
    i = start
    end = len(blob) if stop is None else min(stop, len(blob))
    while i < end:
        hit = 0
        if blob[i] in headers:
            for L in range(lo, min(hi, end - i) + 1):
                if crc_residue(blob[i:i + L]) == GOOD_RESIDUE:
                    hit = L
                    break
        if hit:
            out.append((i, hit))
            i += hit
        else:
            i += 1
    return out


def payloads(blob, pdus, extra=2):
    """Payload bytes of each PDU, header and CRC removed.

    `extra` accounts for the BCtSDU octets between the PDU header and the
    payload proper, which are not yet parsed -- see the module docstring. It
    was fitted, not derived: 5 total header octets recovers 847 bytes of a
    gzipped XML document where 3 recovers 43 and 6 recovers 409. Treat it as a
    placeholder until the BCtSDU walk of TS 102 744-3-3 is implemented.
    """
    out = bytearray()
    for off, ln in pdus:
        h = header_len(blob[off]) + extra
        if ln > h + 2:
            out += blob[off + h:off + ln - 2]
    return bytes(out)
