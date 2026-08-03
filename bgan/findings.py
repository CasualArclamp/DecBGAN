"""Recognisable artefacts carved out of a decoded payload.

X.509 certificates, DNS messages, HTTP transactions and TLS handshakes, found
by scanning the concatenated FEC blocks. This is carving, not demultiplexing:
the payload has silent gaps wherever a block failed, so anything spanning one
is lost, and nothing here should be read as a faithful record of the session.

Every extractor validates *structurally* rather than by pattern alone, for the
same reason `pcapout.carve_ipv4` insists on the header checksum: scanning a
megabyte for a two-byte tag finds a hit every few hundred bytes in pure noise.
The rule applied throughout is that a candidate must parse to its own declared
length and end exactly where it said it would. A DER certificate whose three
children consume precisely the bytes the outer SEQUENCE claimed is not a
coincidence; a 0x30 byte is.

Measured false accepts on 8 MB of random bytes (tests at the bottom of this
docstring's module, run via scan_random): 0 certificates, 0 DNS messages,
0 TLS handshakes, 0 HTTP messages. The URL extractor is the loose one and is
marked as such -- it is a plain regex over printable runs and will pick up
fragments.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field


@dataclass
class Finding:
    kind: str                       # cert | dns | http | tls | url
    offset: int
    summary: str
    detail: list = field(default_factory=list)


# --- minimal DER ------------------------------------------------------------

def _der_len(b, i):
    """(length, index_after_length) or None. Rejects indefinite/oversized."""
    if i >= len(b):
        return None
    n = b[i]
    if n < 0x80:
        return n, i + 1
    k = n & 0x7f
    if k == 0 or k > 4 or i + 1 + k > len(b):
        return None                 # indefinite form is illegal in DER
    v = int.from_bytes(b[i + 1:i + 1 + k], "big")
    if v < 0x80 or (k > 1 and b[i + 1] == 0):
        return None                 # non-minimal encoding: not real DER
    return v, i + 1 + k


def _tlv(b, i):
    """(tag, content_start, content_end) or None."""
    if i >= len(b):
        return None
    tag = b[i]
    got = _der_len(b, i + 1)
    if got is None:
        return None
    ln, j = got
    if j + ln > len(b):
        return None
    return tag, j, j + ln


def _children(b, start, end):
    out = []
    i = start
    while i < end:
        t = _tlv(b, i)
        if t is None or t[2] > end:
            return None             # a child overran its parent
        out.append(t)
        i = t[2]
    return out if i == end else None    # must land exactly on the boundary


_OIDS = {
    bytes([0x55, 0x04, 0x03]): "CN",
    bytes([0x55, 0x04, 0x06]): "C",
    bytes([0x55, 0x04, 0x07]): "L",
    bytes([0x55, 0x04, 0x08]): "ST",
    bytes([0x55, 0x04, 0x0a]): "O",
    bytes([0x55, 0x04, 0x0b]): "OU",
}
_OID_SAN = bytes([0x55, 0x1d, 0x11])


def _name(b, start, end):
    """RDNSequence -> 'CN=x, O=y'. Best effort; unknown OIDs are skipped."""
    parts = []
    rdns = _children(b, start, end) or []
    for _t, s, e in rdns:                       # SET OF
        for _t2, s2, e2 in (_children(b, s, e) or []):   # SEQUENCE
            kids = _children(b, s2, e2) or []
            if len(kids) != 2 or kids[0][0] != 0x06:
                continue
            oid = b[kids[0][1]:kids[0][2]]
            val = b[kids[1][1]:kids[1][2]]
            key = _OIDS.get(oid)
            if key:
                parts.append(f"{key}={val.decode('utf-8', 'replace')}")
    return ", ".join(parts)


def _san(b, tbs_start, tbs_end):
    """dNSName entries from a subjectAltName extension, if present."""
    names = []
    stack = [(tbs_start, tbs_end)]
    while stack:
        s, e = stack.pop()
        for tag, cs, ce in (_children(b, s, e) or []):
            if tag == 0x06 and b[cs:ce] == _OID_SAN:
                continue
            if tag in (0x30, 0x31, 0xa3, 0xa0):
                stack.append((cs, ce))
    # simpler: find the extension by OID then read the OCTET STRING beside it
    i = tbs_start
    while True:
        j = b.find(_OID_SAN, i, tbs_end)
        if j < 0:
            break
        i = j + 1
        t = _tlv(b, j + len(_OID_SAN))
        if t and t[0] == 0x01:                  # optional critical BOOLEAN
            t = _tlv(b, t[2])
        if not t or t[0] != 0x04:               # extnValue OCTET STRING
            continue
        inner = _tlv(b, t[1])
        if not inner or inner[0] != 0x30:
            continue
        for tag, cs, ce in (_children(b, inner[1], inner[2]) or []):
            if tag == 0x82:                     # [2] dNSName
                names.append(b[cs:ce].decode("ascii", "replace"))
    return names


def x509_certs(blob, max_len=8192):
    """DER X.509 certificates. Validated by exact length consistency.

    Certificate ::= SEQUENCE { tbsCertificate SEQUENCE,
                               signatureAlgorithm SEQUENCE,
                               signatureValue BIT STRING }

    Requiring exactly those three children, each parsing to its own declared
    length and together consuming the outer SEQUENCE to the byte, is what
    makes this safe to run over a megabyte of mostly-binary payload.
    """
    out = []
    i = 0
    n = len(blob)
    while True:
        i = blob.find(b"\x30\x82", i)           # SEQUENCE, 2-byte length
        if i < 0 or i >= n:
            break
        start = i
        i += 1
        t = _tlv(blob, start)
        if t is None or t[2] - start > max_len:
            continue
        kids = _children(blob, t[1], t[2])
        if not kids or len(kids) != 3:
            continue
        if kids[0][0] != 0x30 or kids[1][0] != 0x30 or kids[2][0] != 0x03:
            continue
        tbs = _children(blob, kids[0][1], kids[0][2])
        if not tbs or len(tbs) < 6:
            continue
        # TBSCertificate: [0] version?, serial, sigalg, issuer, validity,
        # subject, subjectPublicKeyInfo, ...
        k = 1 if tbs[0][0] == 0xa0 else 0
        if len(tbs) < k + 6 or tbs[k][0] != 0x02:      # serialNumber INTEGER
            continue
        issuer, validity, subject = tbs[k + 2], tbs[k + 3], tbs[k + 4]
        if issuer[0] != 0x30 or validity[0] != 0x30 or subject[0] != 0x30:
            continue
        va = _children(blob, validity[1], validity[2])
        if not va or len(va) != 2 or va[0][0] not in (0x17, 0x18):
            continue
        sub = _name(blob, subject[1], subject[2])
        iss = _name(blob, issuer[1], issuer[2])
        if not sub and not iss:
            continue
        nb = blob[va[0][1]:va[0][2]].decode("ascii", "replace")
        na = blob[va[1][1]:va[1][2]].decode("ascii", "replace")
        det = [f"issuer   {iss}", f"validity {nb} .. {na}",
               f"serial   {blob[tbs[k][1]:tbs[k][2]].hex()}",
               f"{t[2]-start} bytes DER"]
        for d in _san(blob, kids[0][1], kids[0][2])[:12]:
            det.append(f"dNSName  {d}")
        out.append(Finding("cert", start, sub or iss, det))
        i = t[2]
    return out


def _isotime(v):
    """UTCTime YYMMDDHHMMSSZ or GeneralizedTime YYYYMMDDHHMMSSZ -> ISO date."""
    s = v.decode("ascii", "replace")
    if not s.endswith("Z") or not s[:-1].isdigit():
        return None
    d = s[:-1]
    if len(d) == 12:                            # UTCTime: 2-digit year
        yy = int(d[:2])
        d = ("19" if yy >= 50 else "20") + d
    elif len(d) != 14:
        return None
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def cert_fragments(blob, window=3000):
    """Certificates recognised by their validity block when the whole DER
    will not parse.

    Almost no certificate on these captures parses end to end, and that is
    expected rather than a bug: the payload is FEC blocks concatenated in
    order with no Bearer Connection reassembly, so a 1.5 kB certificate is
    interleaved with whatever else the terminal was carrying and its DER
    lengths stop lining up. Only objects small enough to sit inside one block
    -- DNS messages, HTTP headers, a ClientHello -- survive intact.

    What does survive is
        Validity ::= SEQUENCE { notBefore Time, notAfter Time }
    which in DER is a SEQUENCE holding exactly two UTCTime or GeneralizedTime
    values, each all digits and Z-terminated. That is a ~32-byte shape with
    almost no freedom in it, and the subject Name follows it immediately, so
    an anchor there recovers the two most interesting facts about a
    certificate -- who it is for and when it was issued -- from a fragment.

    Zero false accepts on 8 MB of random bytes.
    """
    out = []
    seen = set()
    for tt in (b"\x17\x0d", b"\x18\x0f"):
        i = 0
        while True:
            i = blob.find(tt, i)
            if i < 0:
                break
            start, i = i - 2, i + 1
            if start < 0 or start in seen:
                continue
            t = _tlv(blob, start)
            if t is None or t[0] != 0x30:
                continue
            kids = _children(blob, t[1], t[2])
            if not kids or len(kids) != 2:
                continue
            if any(k[0] not in (0x17, 0x18) for k in kids):
                continue
            nb = _isotime(blob[kids[0][1]:kids[0][2]])
            na = _isotime(blob[kids[1][1]:kids[1][2]])
            if not nb or not na or na <= nb:
                continue
            seen.add(start)
            sub = ""
            nxt = _tlv(blob, t[2])
            if nxt and nxt[0] == 0x30:
                sub = _name(blob, nxt[1], nxt[2])
            det = [f"valid {nb} .. {na}"]
            for d in _san(blob, t[2], min(len(blob), t[2] + window))[:10]:
                det.append(f"dNSName  {d}")
            out.append(Finding("cert", start,
                               sub or "(subject not recoverable)", det))
    return out


# --- DNS --------------------------------------------------------------------

_QTYPE = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX",
          16: "TXT", 28: "AAAA", 33: "SRV", 35: "NAPTR", 41: "OPT",
          43: "DS", 48: "DNSKEY", 65: "HTTPS", 255: "ANY"}
_HOSTCHAR = re.compile(rb"^[A-Za-z0-9_-]+$")


def _dns_name(b, i, end):
    """Uncompressed or pointer-terminated name. Returns (name, next_index)."""
    labels = []
    total = 0
    while i < end:
        n = b[i]
        if n == 0:
            return ".".join(labels), i + 1
        if n & 0xc0 == 0xc0:                    # compression pointer
            return ".".join(labels + ["*"]), i + 2
        if n > 63 or i + 1 + n > end:
            return None, i
        lab = b[i + 1:i + 1 + n]
        if not _HOSTCHAR.match(lab):
            return None, i
        labels.append(lab.decode("ascii"))
        total += n + 1
        if total > 253:
            return None, i
        i += 1 + n
    return None, i


def dns_messages(blob, max_len=1500):
    """DNS messages found by structure, not by port.

    Accepts only messages whose header counts are plausible, whose question
    section parses to a syntactically valid name, and whose QCLASS is IN.
    """
    out = []
    n = len(blob)
    for i in range(0, max(0, n - 16)):
        qd = int.from_bytes(blob[i + 4:i + 6], "big")
        if qd != 1:
            continue
        fl = int.from_bytes(blob[i + 2:i + 4], "big")
        if (fl >> 11) & 0xf:                    # opcode must be QUERY
            continue
        if (fl >> 4) & 0x7:                     # Z bits reserved, must be 0
            continue
        if (fl & 0xf) > 10:                     # RCODE
            continue
        an = int.from_bytes(blob[i + 6:i + 8], "big")
        ns = int.from_bytes(blob[i + 8:i + 10], "big")
        ar = int.from_bytes(blob[i + 10:i + 12], "big")
        if an > 64 or ns > 64 or ar > 64:
            continue
        name, j = _dns_name(blob, i + 12, min(n, i + max_len))
        if not name or "." not in name or j + 4 > n:
            continue
        qt = int.from_bytes(blob[j:j + 2], "big")
        qc = int.from_bytes(blob[j + 2:j + 4], "big")
        if qc != 1 or qt not in _QTYPE:
            continue
        kind = "response" if fl & 0x8000 else "query"
        out.append(Finding(
            "dns", i, f"{name}  {_QTYPE[qt]}  ({kind})",
            [f"id 0x{int.from_bytes(blob[i:i+2],'big'):04x}  "
             f"answers {an}, authority {ns}, additional {ar}"]))
    return out


# --- HTTP -------------------------------------------------------------------

_METHODS = (b"GET ", b"POST ", b"HEAD ", b"PUT ", b"DELETE ", b"OPTIONS ",
            b"CONNECT ", b"TRACE ", b"PATCH ")
_HDRS = ("host", "user-agent", "content-type", "server", "location",
         "content-length", "cache-control")


def _headers(blob, i, limit=2048):
    """Header lines following a start line, until a blank line.

    Values are truncated at the first non-printable byte. Without that they
    run on into whatever follows: a header near the end of a decoded block is
    not followed by its own continuation but by unrelated traffic, so the
    line has no CRLF to stop at and the "value" swallows binary.
    """
    end = blob.find(b"\r\n\r\n", i, i + limit)
    seg = blob[i:end if end > 0 else min(len(blob), i + limit)]
    out = []
    for line in seg.split(b"\r\n"):
        if b":" not in line:
            continue
        k, _, v = line.partition(b":")
        ks = k.decode("ascii", "replace").strip().lower()
        if ks not in _HDRS:
            continue
        clean = bytearray()
        for c in v.strip():
            if not (0x20 <= c <= 0x7e):
                break
            clean.append(c)
        if clean:
            out.append(f"{k.decode('ascii','replace').strip()}: "
                       f"{clean.decode('ascii')[:120]}")
    return out


def http_messages(blob):
    """HTTP request and status lines, with their interesting headers.

    `HTTP/1.` is a seven-byte literal, so this needs no further validation to
    be safe; the start-line grammar check is for tidiness, not safety.
    """
    out = []
    for m in re.finditer(rb"HTTP/1\.[01]", blob):
        i = m.start()
        # response: the version starts the line
        line_end = blob.find(b"\r\n", i)
        if line_end < 0 or line_end - i > 200:
            continue
        line = blob[i:line_end]
        if re.match(rb"HTTP/1\.[01] \d{3}", line):
            out.append(Finding("http", i,
                               line.decode("ascii", "replace"),
                               _headers(blob, line_end + 2)))
            continue
        # request: method and URI precede it on the same line
        ls = blob.rfind(b"\n", max(0, i - 8192), i) + 1
        head = blob[ls:line_end]
        if any(head.startswith(mm) for mm in _METHODS) and len(head) < 2048:
            out.append(Finding("http", ls,
                               head.decode("ascii", "replace"),
                               _headers(blob, line_end + 2)))
    return out


# --- HTTP bodies, i.e. documents --------------------------------------------

_EXT = {"text/html": ".html", "text/plain": ".txt", "text/css": ".css",
        "application/json": ".json", "text/x-json": ".json",
        "application/javascript": ".js", "text/javascript": ".js",
        "application/xml": ".xml", "text/xml": ".xml",
        "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
        "image/svg+xml": ".svg", "application/pkix-crl": ".crl",
        "application/pkix-cert": ".cer", "application/ocsp-response": ".ors",
        "application/octet-stream": ".bin"}


@dataclass
class Document:
    offset: int
    status: str
    ctype: str
    declared: int          # Content-Length, or -1 when chunked/unknown
    data: bytes
    note: str = ""

    @property
    def name(self):
        base = _EXT.get(self.ctype.split(";")[0].strip().lower(), ".bin")
        return f"doc_{self.offset:08d}{base}"

    @property
    def intact(self):
        """Does the body look like an uninterrupted run of its own type?

        There is no way to *prove* the bytes after a header belong to that
        response -- the payload is FEC blocks concatenated with no
        reassembly, so the next block may be someone else's traffic. What can
        be checked is self-consistency: a text body that is entirely
        printable, or a compressed body that inflates, almost certainly is
        the real thing. A text body that turns to binary partway is the
        signature of the response being cut off by an unrelated block.
        """
        if not self.data:
            return False
        if self.ctype.startswith(("text/", "application/json",
                                  "application/xml", "text/x-json")):
            ok = sum(1 for c in self.data if 0x09 <= c <= 0x7e or c in (0x0a, 0x0d))
            return ok/len(self.data) > 0.98
        return True


def _inflate(data, wbits):
    """Decompress as much as possible, tolerating a truncated tail."""
    d = zlib.decompressobj(wbits)
    out = bytearray()
    try:
        for k in range(0, len(data), 512):
            out += d.decompress(data[k:k + 512])
    except zlib.error:
        pass
    return bytes(out)


def _dechunk(blob, i, limit):
    """Transfer-Encoding: chunked. Stops at the first malformed size line."""
    out = bytearray()
    end = min(len(blob), i + limit)
    while i < end:
        nl = blob.find(b"\r\n", i, min(end, i + 20))
        if nl < 0:
            break
        try:
            n = int(blob[i:nl].split(b";")[0], 16)
        except ValueError:
            break
        if n == 0:
            break
        if nl + 2 + n > end:
            out += blob[nl + 2:end]         # truncated final chunk
            break
        out += blob[nl + 2:nl + 2 + n]
        i = nl + 2 + n + 2
    return bytes(out)


def http_bodies(blob, max_body=1 << 20):
    """HTTP response bodies, decompressed where the headers say to.

    Delimited by Content-Length or by chunk framing -- a response with
    neither cannot be bounded and is skipped rather than guessed at.

    Whether the bytes after a header really are that response's body cannot
    be established from this payload: there is no Bearer Connection
    reassembly, so the following block may belong to a different flow. Use
    Document.intact, which checks the body against its own declared type.
    """
    out = []
    for m in re.finditer(rb"HTTP/1\.[01] (\d{3})([^\r\n]{0,80})\r\n", blob):
        hs = m.end()
        he = blob.find(b"\r\n\r\n", hs, hs + 4096)
        if he < 0:
            continue
        try:
            hdrs = blob[hs:he].decode("ascii", "replace")
        except Exception:
            continue

        def hv(name, h=hdrs):
            g = re.search(rf"^{name}:\s*([^\r\n]*)", h, re.I | re.M)
            return g.group(1).strip() if g else ""

        ctype = hv("Content-Type") or "application/octet-stream"
        enc = hv("Content-Encoding").lower()
        te = hv("Transfer-Encoding").lower()
        cl = hv("Content-Length")
        body, declared, note = b"", -1, ""

        if "chunked" in te:
            body = _dechunk(blob, he + 4, max_body)
            note = "chunked"
        elif cl.isdigit():
            declared = int(cl)
            if declared == 0 or declared > max_body:
                continue
            body = blob[he + 4:he + 4 + declared]
            if len(body) < declared:
                note = f"truncated, {len(body)} of {declared}"
        else:
            continue                        # no way to bound the body

        if not body:
            continue
        if enc in ("gzip", "x-gzip"):
            raw, body = body, _inflate(body, 16 + zlib.MAX_WBITS)
            note = (note + "; " if note else "") + (
                f"gunzipped {len(raw)}->{len(body)}" if body
                else "gzip did not inflate")
        elif enc == "deflate":
            raw, body = body, (_inflate(body, zlib.MAX_WBITS)
                               or _inflate(body, -zlib.MAX_WBITS))
            note = (note + "; " if note else "") + (
                f"inflated {len(raw)}->{len(body)}" if body
                else "deflate did not inflate")
        elif enc == "br":
            note = (note + "; " if note else "") + "brotli, not decoded"
        if not body:
            continue
        out.append(Document(m.start(),
                            f"{m.group(1).decode()}{m.group(2).decode('ascii','replace')}".strip(),
                            ctype, declared, body, note))
    return out


_MARKUP = re.compile(
    rb"<!DOCTYPE\s+html|<html[\s>]|<\?xml[\s?]|<svg[\s>]|<rss[\s>]", re.I)


def markup_fragments(blob, span=4096):
    """Markup found without a usable HTTP header in front of it.

    A response whose header block failed to decode still leaves its body in
    the payload. These are fragments by definition -- the run is cut at the
    first non-text byte -- so they are reported separately from bodies with
    a Content-Length behind them.
    """
    out = []
    for m in _MARKUP.finditer(blob):
        i = m.start()
        j = i
        end = min(len(blob), i + span)
        while j < end and (0x09 <= blob[j] <= 0x7e or blob[j] in (0x0a, 0x0d)):
            j += 1
        if j - i < 64:
            continue
        out.append(Document(i, "no header", "text/html", -1, blob[i:j],
                            "fragment, no Content-Length"))
    return out


def documents(blob):
    """Everything reconstructable as a file, bodies first then fragments."""
    docs = http_bodies(blob)
    spans = [(d.offset, d.offset + len(d.data) + 4096) for d in docs]
    frags = [f for f in markup_fragments(blob)
             if not any(a <= f.offset < b for a, b in spans)]
    return sorted(docs + frags, key=lambda d: d.offset)


# --- TLS --------------------------------------------------------------------

def tls_hellos(blob):
    """TLS ClientHello SNI and ServerHello, validated by nested lengths.

    The record length must contain the handshake length exactly, which is a
    24-bit and a 16-bit field agreeing -- far too specific to hit by chance.
    """
    out = []
    n = len(blob)
    for i in range(0, max(0, n - 9)):
        if blob[i] != 0x16 or blob[i + 1] != 0x03 or blob[i + 2] > 0x04:
            continue
        rec = int.from_bytes(blob[i + 3:i + 5], "big")
        if not (4 <= rec <= 16384) or i + 5 + rec > n:
            continue
        ht = blob[i + 5]
        if ht not in (0x01, 0x02):
            continue
        hl = int.from_bytes(blob[i + 6:i + 9], "big")
        if hl + 4 != rec:                       # handshake must fill record
            continue
        p = i + 9
        if p + 34 > n:
            continue
        p += 2 + 32                             # version, random
        sl = blob[p]
        p += 1 + sl
        if ht == 0x02:
            out.append(Finding("tls", i, "ServerHello", []))
            continue
        if p + 2 > n:
            continue
        cs = int.from_bytes(blob[p:p + 2], "big")
        p += 2 + cs
        if p >= n:
            continue
        cm = blob[p]
        p += 1 + cm
        if p + 2 > n:
            continue
        ext_total = int.from_bytes(blob[p:p + 2], "big")
        p += 2
        stop = min(n, p + ext_total)
        sni = None
        while p + 4 <= stop:
            et = int.from_bytes(blob[p:p + 2], "big")
            el = int.from_bytes(blob[p + 2:p + 4], "big")
            p += 4
            if et == 0x0000 and p + 5 <= stop:
                ln = int.from_bytes(blob[p + 3:p + 5], "big")
                if p + 5 + ln <= stop:
                    sni = blob[p + 5:p + 5 + ln].decode("ascii", "replace")
            p += el
        out.append(Finding("tls", i,
                           f"ClientHello  SNI {sni}" if sni else "ClientHello",
                           []))
    return out


# --- URLs -------------------------------------------------------------------

_URL = re.compile(rb"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{4,200}")


def urls(blob):
    """Plain regex, and the one loose extractor here -- expect fragments."""
    seen = {}
    for m in _URL.finditer(blob):
        u = m.group().decode("ascii", "replace").rstrip(".,);'\"")
        seen.setdefault(u, m.start())
    return [Finding("url", o, u) for u, o in
            sorted(seen.items(), key=lambda kv: kv[1])]


# --- top level --------------------------------------------------------------

def scan(blob):
    """All extractors, findings sorted by offset."""
    full = x509_certs(blob)
    # A fragment inside a certificate that already parsed whole is the same
    # certificate seen twice, so drop it.
    spans = [(f.offset, f.offset + 8192) for f in full]
    frags = [f for f in cert_fragments(blob)
             if not any(a <= f.offset < b for a, b in spans)]
    out = (full + frags + dns_messages(blob) + http_messages(blob)
           + tls_hellos(blob) + urls(blob))
    out.sort(key=lambda f: (f.offset, f.kind))
    return out


def hosts(findings):
    """Hostnames implicated, from every source that names one.

    This is the answer to "who was this terminal talking to", which no single
    extractor gives on its own.
    """
    from collections import Counter
    c = Counter()
    for f in findings:
        if f.kind == "dns":
            c[f.summary.split()[0]] += 1
        elif f.kind == "tls" and "SNI " in f.summary:
            c[f.summary.split("SNI ", 1)[1].strip()] += 1
        elif f.kind == "cert":
            for d in f.detail:
                if d.startswith("dNSName  "):
                    c[d[9:].strip()] += 1
            for part in f.summary.split(","):
                if part.strip().startswith("CN="):
                    c[part.strip()[3:]] += 1
        elif f.kind == "http":
            for d in f.detail:
                if d.lower().startswith("host:"):
                    c[d.split(":", 1)[1].strip()] += 1
        elif f.kind == "url":
            m = re.match(r"https?://([^/:]+)", f.summary)
            if m:
                c[m.group(1)] += 1
    return c


def scan_random(nbytes=8 << 20, seed=0):
    """False-accept check. Returns {kind: count} over random bytes."""
    import numpy as np
    from collections import Counter
    rng = np.random.default_rng(seed)
    blob = rng.integers(0, 256, nbytes, dtype=np.uint8).tobytes()
    return Counter(f.kind for f in scan(blob))
