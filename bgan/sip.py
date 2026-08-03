"""SIP messages carved out of a decoded payload, and grouped into dialogs.

SIP (RFC 3261) is a text protocol shaped like HTTP, so this is close kin to
`findings.http_messages` and inherits its safety argument: `SIP/2.0` is a
seven-byte literal, and a candidate must additionally satisfy the start-line
grammar of RFC 3261 clause 7.1/7.2. Scanning a megabyte of noise for that
finds nothing -- see `scan_random` at the bottom, which is the control.

Carving, not demultiplexing. The payload has silent gaps wherever an FEC
block failed, so a message straddling one is cut, and `SipMessage.complete`
says which. A cut message is still worth showing -- the start line and the
first headers usually survive, and those carry the dialog identity -- but it
must not be presented as a faithful record.

"Assembly" here means two things, and neither is reassembling IP fragments:

  * a message body is taken from Content-Length when that header is present
    and sane, and only then. A body without a length cannot be bounded in a
    carved stream, so it is left off rather than guessed at.
  * messages are grouped into dialogs by Call-ID and ordered by CSeq, which
    is what turns a pile of packets into a call you can read.

Header values that carry authentication material -- Authorization and the
WWW-Authenticate family -- are replaced by their length. The structure stays
visible; the credential material does not land in a GUI tab or an export.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# RFC 3261 clause 7.1 plus the common extension methods. Anchored as a set so
# the start-line check is an exact match on the token, not a prefix test --
# "INVITEX sip:.. SIP/2.0" is not an INVITE.
METHODS = {
    "INVITE", "ACK", "BYE", "CANCEL", "OPTIONS", "REGISTER",
    "PRACK", "SUBSCRIBE", "NOTIFY", "PUBLISH", "INFO", "REFER", "MESSAGE",
    "UPDATE",
}

# Kept in dialog order rather than alphabetically: this is the order they are
# shown in, and it is the order that makes a message readable.
KEEP = ["From", "To", "Call-ID", "CSeq", "Contact", "Via", "Route",
        "Record-Route", "P-Asserted-Identity", "P-Called-Party-ID",
        "User-Agent", "Server", "Allow", "Supported", "Require", "Expires",
        "Max-Forwards", "Content-Type", "Content-Length", "Reason",
        "Retry-After", "Warning", "Subject", "Session-Expires",
        "Authorization", "WWW-Authenticate", "Proxy-Authorization",
        "Proxy-Authenticate"]
_KEEP = {h.lower(): h for h in KEEP}

# Compact forms, RFC 3261 clause 20 and RFC 3265/3515.
_COMPACT = {"f": "From", "t": "To", "i": "Call-ID", "m": "Contact",
            "v": "Via", "c": "Content-Type", "l": "Content-Length",
            "s": "Subject", "k": "Supported", "o": "Event", "r": "Refer-To",
            "b": "Referred-By", "e": "Content-Encoding"}

_SECRET = {"authorization", "www-authenticate", "proxy-authorization",
           "proxy-authenticate"}

_STATUS = re.compile(rb"^.IP/2\.0 (\d{3})(?: (.{0,120}))?$")
# A request URI must carry a scheme (RFC 3261 clause 8.1.1.1 -- sip, sips or
# tel in practice). Requiring it is what keeps the backward walk below from
# accepting an arbitrary run of bytes as a URI.
_URI_SCHEME = re.compile(rb"^[a-z][a-z0-9+.-]{1,14}:")


@dataclass
class SipMessage:
    offset: int
    kind: str                       # "request" | "response"
    start: str                      # the start line, verbatim
    method: str = ""                # request method, or the CSeq method
    uri: str = ""                   # request URI
    status: int = 0                 # response code
    reason: str = ""
    headers: list = field(default_factory=list)     # [(name, value)]
    body: bytes = b""
    complete: bool = False          # header block terminated by a blank line
    body_complete: bool = False     # body reached its Content-Length
    damaged: bool = False           # the S of "SIP/2.0" was some other byte

    def get(self, name):
        n = name.lower()
        for k, v in self.headers:
            if k.lower() == n:
                return v
        return ""

    @property
    def call_id(self):
        return self.get("Call-ID")

    @property
    def cseq(self):
        """(number, method) from the CSeq header, or (-1, "")."""
        m = re.match(r"\s*(\d+)\s+([A-Za-z]+)", self.get("CSeq"))
        return (int(m.group(1)), m.group(2).upper()) if m else (-1, "")

    @property
    def summary(self):
        if self.kind == "request":
            return f"{self.method} {self.uri}"
        return f"{self.status} {self.reason}".strip()

    @property
    def check(self):
        if not self.complete:
            return "truncated"
        if self.get("Content-Length") and not self.body_complete:
            return "truncated"
        return "intact"


def _clean(v, limit=200):
    """Value up to the first non-printable byte.

    Same reasoning as findings._headers: a header near the end of a decoded
    block has no CRLF to stop at, because what follows is not its own
    continuation but unrelated traffic. Without this the value swallows
    binary.
    """
    out = bytearray()
    for c in v.strip():
        if not (0x20 <= c <= 0x7e):
            break
        out.append(c)
    return out.decode("ascii")[:limit]


def _parse_headers(blob, i, limit=4096):
    """[(name, value)], index after the blank line, and whether one was found.

    Handles RFC 3261 clause 7.3.1 line folding (a continuation line starts
    with whitespace) and the clause 20 compact forms.
    """
    end = blob.find(b"\r\n\r\n", i, i + limit)
    complete = end >= 0
    seg = blob[i:end if complete else min(len(blob), i + limit)]
    lines = []
    for raw in seg.split(b"\r\n"):
        if raw[:1] in (b" ", b"\t") and lines:      # folded continuation
            lines[-1] += b" " + raw.strip()
        else:
            lines.append(raw)

    out = []
    for line in lines:
        if b":" not in line:
            continue
        k, _, v = line.partition(b":")
        ks = k.strip().decode("ascii", "replace").lower()
        name = _KEEP.get(ks) or _COMPACT.get(ks)
        if not name:
            continue
        if ks in _SECRET or name.lower() in _SECRET:
            out.append((name, f"<redacted, {len(v.strip())} bytes>"))
            continue
        val = _clean(v)
        if val:
            out.append((name, val))
    return out, (end + 4 if complete else len(blob)), complete


def _body(blob, i, headers):
    """(body, complete). Only ever bounded by Content-Length."""
    length = ""
    for k, v in headers:
        if k.lower() == "content-length":
            length = v
            break
    if not length.strip().isdigit():
        return b"", False
    n = int(length.strip())
    if n <= 0 or n > (1 << 20):
        return b"", n == 0
    return blob[i:i + n], i + n <= len(blob)


def messages(blob, limit=4096):
    """Every SIP message in `blob`, in offset order.

    Anchored on the `SIP/2.0` literal, which appears on the start line of
    both a request (at the end) and a response (at the start), so one scan
    finds both.
    """
    out = []
    # Anchored on "IP/2.0", one byte short of the full literal, because in
    # this bearer the leading S is often not there. Measured on the 224 s
    # 1534.499 capture: of five SIP responses, four have some other byte in
    # that position (0x18, 0x24, 0x68, 0xc4) and parse cleanly from "IP/2.0"
    # onward. Only 0x18 is a known Bearer Control PDU header octet, so the
    # cause is NOT established -- the payload is a concatenation of FEC
    # blocks carrying interleaved flows, and one of those splices in here.
    #
    # The relaxation costs one byte of a seven-byte literal and is paid for
    # by the three status digits and the grammar check below; scan_random
    # still measures zero false accepts over 8 MB.
    for m in re.finditer(rb"IP/2\.0", blob):
        if m.start() == 0:
            continue
        i = m.start() - 1               # where the S is, or should have been
        damaged = blob[i] != 0x53
        # Responses: the literal opens the line.
        line_end = blob.find(b"\r\n", i)
        if line_end < 0 or line_end - i > 200:
            continue
        st = _STATUS.match(blob[i:line_end])
        if st:
            code = int(st.group(1))
            if not 100 <= code <= 699:
                continue
            hdrs, after, complete = _parse_headers(blob, line_end + 2, limit)
            body, bok = _body(blob, after, hdrs)
            msg = SipMessage(
                offset=i, kind="response",
                start=blob[i:line_end].decode("ascii", "replace"),
                status=code,
                reason=_clean(st.group(2) or b"", 120),
                headers=hdrs, body=body,
                complete=complete, body_complete=bok, damaged=damaged)
            msg.method = msg.cseq[1]
            out.append(msg)
            continue

        # Requests: "<METHOD> <URI> SIP/2.0" ends at the literal, so walk
        # back over the URI and then the method.
        #
        # NOT back to the previous newline. What precedes a carved message is
        # the IP/UDP header it arrived in -- these captures show 45 .. 11 ..
        # 13c4 13d8, i.e. UDP 5060->5080 -- so there is no line ending to
        # find, and anchoring a regex at one silently matched nothing.
        j = i - 1
        if j < 2 or blob[j] != 0x20:
            continue
        k = j - 1
        while k >= 0 and j - k <= 1024 and blob[k] not in (0x20, 0x0d, 0x0a):
            k -= 1
        if k < 0 or blob[k] != 0x20 or k == j - 1:
            continue
        uri = blob[k + 1:j]
        if not _URI_SCHEME.match(uri) or not uri.isascii():
            continue
        s = k - 1
        while s >= 0 and k - s <= 12 and 0x41 <= blob[s] <= 0x5a:
            s -= 1
        run = blob[s + 1:k].decode("ascii", "replace")
        # The run over-reaches whenever the byte before the method happens to
        # be an uppercase letter, which in carved binary is about one time in
        # ten -- enough to lose a message. Take the longest suffix that is a
        # real method rather than the whole run.
        method = next((run[-n:] for n in range(len(run), 2, -1)
                       if run[-n:] in METHODS), "")
        if not method:
            continue
        ls = k - len(method)
        hdrs, after, complete = _parse_headers(blob, line_end + 2, limit)
        body, bok = _body(blob, after, hdrs)
        out.append(SipMessage(
            offset=ls, kind="request",
            start=blob[ls:line_end].decode("ascii", "replace"),
            method=method,
            uri=uri.decode("ascii", "replace"),
            headers=hdrs, body=body,
            complete=complete, body_complete=bok, damaged=damaged))
    out.sort(key=lambda x: x.offset)
    return out


# --- SDP, RFC 4566 ----------------------------------------------------------

@dataclass
class Media:
    kind: str                       # audio | video | ...
    port: int
    proto: str
    formats: list = field(default_factory=list)     # ["8 PCMA/8000", ...]
    address: str = ""


def sdp(body):
    """Media descriptions from an SDP body. [] if it is not SDP.

    Only the lines that say what the call actually is: connection address,
    media type and port, and the rtpmap names for the offered codecs.
    """
    if b"v=0" not in body[:64]:
        return []
    session_c = ""
    out, rtpmap = [], {}
    for raw in re.split(rb"\r\n|\n", body):
        line = _clean(raw, 400)
        if line[1:2] != "=":
            continue
        tag, val = line[0], line[2:]
        if tag == "c":
            parts = val.split()
            if out:
                out[-1].address = parts[-1] if parts else ""
            else:
                session_c = parts[-1] if parts else ""
        elif tag == "m":
            p = val.split()
            if len(p) >= 3 and p[1].isdigit():
                out.append(Media(kind=p[0], port=int(p[1]), proto=p[2],
                                 formats=p[3:], address=session_c))
        elif tag == "a" and val.lower().startswith("rtpmap:"):
            pt, _, name = val[7:].strip().partition(" ")
            rtpmap[pt.strip()] = name.strip()
    for md in out:
        md.formats = [f"{f} {rtpmap[f]}" if f in rtpmap else f
                      for f in md.formats]
    return out


# --- dialogs ----------------------------------------------------------------

@dataclass
class Dialog:
    call_id: str
    messages: list = field(default_factory=list)

    @property
    def offset(self):
        return min(m.offset for m in self.messages)

    @property
    def from_uri(self):
        return _uri(self._first("From"))

    @property
    def to_uri(self):
        return _uri(self._first("To"))

    def _first(self, name):
        for m in self.messages:
            v = m.get(name)
            if v:
                return v
        return ""

    @property
    def methods(self):
        seen = []
        for m in self.messages:
            if m.kind == "request" and m.method not in seen:
                seen.append(m.method)
        return seen

    @property
    def media(self):
        out = []
        for m in self.messages:
            out.extend(sdp(m.body))
        return out

    @property
    def outcome(self):
        """The most advanced thing that happened, as a short phrase."""
        codes = [m.status for m in self.messages if m.kind == "response"]
        reqs = {m.method for m in self.messages if m.kind == "request"}
        if "BYE" in reqs:
            return "call ended (BYE)"
        if any(200 <= c < 300 for c in codes):
            return "answered (2xx)"
        if "CANCEL" in reqs:
            return "cancelled"
        if any(c >= 400 for c in codes):
            bad = min(c for c in codes if c >= 400)
            return f"rejected ({bad})"
        if any(180 <= c < 200 for c in codes):
            return "ringing, no answer seen"
        return "no response seen"


_URI = re.compile(r"<?(sips?:[^>;\s]+)")


def _uri(header):
    m = _URI.search(header or "")
    if m:
        return m.group(1)
    return (header or "").strip()[:80]


def dialogs(msgs):
    """Group messages by Call-ID, in the order they appear in the payload.

    Ordered by offset, not by CSeq. CSeq counts within one direction of a
    dialog (RFC 3261 clause 8.1.1.5), so a BYE from the callee carries its
    own sequence and can be numerically lower than the INVITE it terminates
    -- sorting on it put the BYE first and made `from_uri` report the callee.
    Offset is wire order, which is the honest ordering for a carved stream.

    Messages with no recoverable Call-ID -- almost always ones cut before
    that header -- are each kept as their own single-message dialog rather
    than merged into a bogus shared one.
    """
    by_id, loose = {}, []
    for m in msgs:
        cid = m.call_id
        if cid:
            by_id.setdefault(cid, Dialog(cid)).messages.append(m)
        else:
            loose.append(Dialog("", [m]))
    for d in by_id.values():
        d.messages.sort(key=lambda m: m.offset)
    out = list(by_id.values()) + loose
    out.sort(key=lambda d: d.offset)
    return out


def scan(blob):
    """(messages, dialogs) for a payload."""
    msgs = messages(blob)
    return msgs, dialogs(msgs)


def scan_random(nbytes=8 << 20, seed=0):
    """False-accept control. Returns the message count over random bytes."""
    import numpy as np
    rng = np.random.default_rng(seed)
    return len(messages(rng.integers(0, 256, nbytes,
                                     dtype=np.uint8).tobytes()))
