"""Terminal addresses seen on the forward link, and their public IPs.

The forward link carries traffic **to** terminals, so every destination
address in a carved IPv4 packet is a terminal -- that is the whole idea, and
it needs no application-layer parsing. Sources are the correspondents the
terminals were talking to and are not listed as terminals.

Two independent routes to a public address, which is what makes the answer
trustworthy rather than a guess:

  A. **Echoed back.** A terminal that queries `myip.opendns.com` (A) or
     `o-o.myaddr.l.google.com` (TXT) is told its own public address, and the
     answer travels the forward link where it can be read.

  B. **Addressed directly.** A public address appearing as a *destination*
     is a terminal that is not behind carrier NAT.

Measured on the 224 s 1534.499 capture, the two agree exactly -- six
addresses, every one found by both -- and, more tellingly, each DNS answer
was delivered to the very address it named:

    "your public IP is 159.100.53.55"   ->  delivered to 159.100.53.55

which is only possible if the terminal holds that address directly. So these
terminals are not NATed; the private 10.x addresses that also appear belong
to terminals that are.

Confidence is reported, not assumed. A carved packet can pass its header
checksum by chance, and a handful of one-off "destinations" in the data are
plainly correspondents (Apple, Facebook) rather than terminals -- they show
up once or twice against 5-107 packets for a real terminal. `MIN_PACKETS`
separates them, and anything below it is reported as weak rather than
dropped silently.
"""
from __future__ import annotations

import ipaddress
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import pcapout

# Names whose answer is the querier's own public address.
ECHO_NAMES = ("myip.opendns.com", "o-o.myaddr.l.google.com",
              "myaddr.l.google.com", "whoami.akamai.net",
              "whoami.ds.akahelp.net", "resolver.dnscrypt.info")

# A single carved packet is not evidence of a terminal; a run of them is.
MIN_PACKETS = 3

ICMP_TYPES = {
    (0, 0): "echo reply", (8, 0): "echo request",
    (3, 0): "net unreachable", (3, 1): "host unreachable",
    (3, 2): "protocol unreachable", (3, 3): "port unreachable",
    (3, 4): "fragmentation needed", (3, 13): "administratively filtered",
    (5, 0): "redirect net", (5, 1): "redirect host",
    (11, 0): "TTL exceeded", (11, 1): "fragment reassembly time exceeded",
    (12, 0): "parameter problem",
}


def _addr(b, o):
    return ".".join(str(x) for x in b[o:o + 4])


# Classified explicitly rather than through ipaddress.is_private/is_global,
# whose meaning is neither stable across versions nor what is wanted here.
# On Python 3.14, 100.64.0.0/10 reports is_private False AND is_global False,
# so a carrier-NAT terminal -- precisely the space a satellite operator uses
# -- would fall through and be dropped; while 203.0.113.0/24 reports
# is_private True. Both are wrong for this purpose.
_PRIVATE = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10",                    # RFC 6598 carrier-grade NAT
))
_NOT_A_HOST = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "224.0.0.0/4",                      # multicast
    "240.0.0.0/4",                      # reserved, includes 255.255.255.255
))


def _kind(s):
    """'public' | 'private' | None for anything that cannot be a terminal."""
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return None
    if any(a in n for n in _NOT_A_HOST):
        return None
    if any(a in n for n in _PRIVATE):
        return "private"
    return "public"


# --- DNS, only enough of it to read an answer -------------------------------

def _name(b, i, end, hops=0):
    out = []
    while i < end:
        n = b[i]
        if n == 0:
            return ".".join(out), i + 1
        if n & 0xC0 == 0xC0:                        # compression pointer
            if hops > 8 or i + 1 >= end:
                return None, i
            nm, _ = _name(b, ((n & 0x3F) << 8) | b[i + 1], end, hops + 1)
            if nm:
                out.append(nm)
            return ".".join(out), i + 2
        i += 1
        if i + n > end:
            return None, i
        out.append(b[i:i + n].decode("ascii", "replace"))
        i += n
    return None, i


def dns_answers(blob, i, end):
    """(qname, value) for A and TXT answers of the response at `i`."""
    try:
        if not (int.from_bytes(blob[i + 2:i + 4], "big") & 0x8000):
            return                                  # a query has no answers
        if int.from_bytes(blob[i + 4:i + 6], "big") != 1:
            return
        an = int.from_bytes(blob[i + 6:i + 8], "big")
        if not 1 <= an <= 16:
            return
        j = i + 12
        q, j = _name(blob, j, end)
        if not q:
            return
        j += 4                                      # qtype, qclass
        for _ in range(an):
            _, j = _name(blob, j, end)
            if j + 10 > end:
                return
            rtype = int.from_bytes(blob[j:j + 2], "big")
            rdlen = int.from_bytes(blob[j + 8:j + 10], "big")
            j += 10
            if j + rdlen > end or rdlen > 512:
                return
            rd = blob[j:j + rdlen]
            if rtype == 1 and rdlen == 4:
                yield q, _addr(rd, 0)
            elif rtype == 16 and rdlen > 1 and rd[0] < rdlen:
                yield q, rd[1:1 + rd[0]].decode("ascii", "replace")
            j += rdlen
    except (IndexError, ValueError):
        return


def echoed_ips(blob):
    """{public_ip: (hits, [names])} from 'what is my IP' answers."""
    from . import findings
    out = defaultdict(lambda: [0, set()])
    for f in findings.dns_messages(blob):
        end = min(len(blob), f.offset + 1500)
        for q, v in dns_answers(blob, f.offset, end):
            if not any(e in q.lower() for e in ECHO_NAMES):
                continue
            if _kind(v) == "public":
                out[v][0] += 1
                out[v][1].add(q)
    return {k: (n, sorted(s)) for k, (n, s) in out.items()}


# --- terminals ---------------------------------------------------------------

@dataclass
class Terminal:
    addr: str
    kind: str                       # public | private
    packets: int = 0
    echoed: int = 0                 # times its own address came back by DNS
    echo_names: list = field(default_factory=list)
    self_confirmed: bool = False    # an echo naming it was delivered TO it

    @property
    def confidence(self):
        """Two independent signals -> confirmed; one strong -> probable."""
        if self.self_confirmed:
            return "confirmed"
        if self.echoed and self.packets >= MIN_PACKETS:
            return "confirmed"
        if self.packets >= MIN_PACKETS:
            return "probable"
        return "weak"


def terminals(blob, min_packets=MIN_PACKETS):
    """Terminal addresses on the forward link, strongest evidence first."""
    from . import findings
    packets = Counter()
    spans = []
    for off, pkt in pcapout.carve_ipv4(blob):
        spans.append((off, off + len(pkt), pkt))
        dst = _addr(pkt, 16)
        if _kind(dst):
            packets[dst] += 1

    echoes = echoed_ips(blob)

    # Which terminal each echo was delivered to. An answer naming X that
    # arrives at X is the strongest single piece of evidence available: it
    # says the terminal holds that address rather than sitting behind a NAT.
    delivered = defaultdict(Counter)
    for f in findings.dns_messages(blob):
        end = min(len(blob), f.offset + 1500)
        vals = [v for q, v in dns_answers(blob, f.offset, end)
                if any(e in q.lower() for e in ECHO_NAMES)]
        if not vals:
            continue
        for a, b, pkt in spans:
            if a <= f.offset < b:
                to = _addr(pkt, 16)
                for v in vals:
                    delivered[to][v] += 1
                break

    out = []
    for addr, n in packets.items():
        k = _kind(addr)
        hits, names = echoes.get(addr, (0, []))
        t = Terminal(addr=addr, kind=k, packets=n, echoed=hits,
                     echo_names=names,
                     self_confirmed=bool(delivered.get(addr, {}).get(addr)))
        out.append(t)
    # An echoed address that never appeared as a destination is still a
    # terminal's public IP -- the terminal itself may be NATed.
    for ip, (hits, names) in echoes.items():
        if ip not in packets:
            out.append(Terminal(addr=ip, kind="public", packets=0,
                                echoed=hits, echo_names=names))
    order = {"confirmed": 0, "probable": 1, "weak": 2}
    out.sort(key=lambda t: (order[t.confidence], -t.packets, t.addr))
    return out


def nat_pairs(blob):
    """{private_terminal: {public_ip: hits}} where an echo reached a 10.x host.

    Only populated when a NATed terminal asks for its own address; the reply
    then names the public side while travelling to the private one.
    """
    from . import findings
    spans = [(o, o + len(p), p) for o, p in pcapout.carve_ipv4(blob)]
    out = defaultdict(Counter)
    for f in findings.dns_messages(blob):
        end = min(len(blob), f.offset + 1500)
        for q, v in dns_answers(blob, f.offset, end):
            if not (any(e in q.lower() for e in ECHO_NAMES)
                    and _kind(v) == "public"):
                continue
            for a, b, pkt in spans:
                if a <= f.offset < b:
                    to = _addr(pkt, 16)
                    if _kind(to) == "private":
                        out[to][v] += 1
                    break
    return {k: dict(v) for k, v in out.items()}


# --- ICMP --------------------------------------------------------------------

@dataclass
class Icmp:
    offset: int
    src: str
    dst: str
    itype: int
    icode: int
    quoted: tuple = ()              # (src, dst, proto, sport, dport) if any

    @property
    def name(self):
        return ICMP_TYPES.get((self.itype, self.icode),
                              f"type {self.itype} code {self.icode}")


def _quoted(b):
    """The IPv4 header quoted inside an ICMP error, or () if it is not sane.

    Worth validating rather than trusting: the quote sits at a fixed offset,
    so a mis-carved ICMP produces one anyway, and an unchecked read yields
    flows like 0.0.0.0:0 -> 0.0.0.0:0 proto 32. Requiring a v4 header with a
    legal IHL, a plausible length, a real protocol and routable endpoints
    removed every such artefact from the captures here.
    """
    if len(b) < 8 + 20:
        return ()
    inner = b[8:]
    if (inner[0] >> 4) != 4:
        return ()
    iihl = (inner[0] & 0x0F) * 4
    if iihl < 20 or len(inner) < iihl:
        return ()
    total = int.from_bytes(inner[2:4], "big")
    if not iihl <= total <= 65535:
        return ()
    proto = inner[9]
    if proto not in (1, 6, 17, 47, 50, 58):
        return ()
    src, dst = _addr(inner, 12), _addr(inner, 16)
    if not (_kind(src) and _kind(dst)):
        return ()
    sp = dp = 0
    if proto in (6, 17) and len(inner) >= iihl + 4:
        sp = (inner[iihl] << 8) | inner[iihl + 1]
        dp = (inner[iihl + 2] << 8) | inner[iihl + 3]
    return (src, dst, proto, sp, dp)


def icmp(blob):
    """ICMP messages, with the header quoted inside an error decoded.

    The quote is the useful part. An unreachable or TTL-exceeded carries the
    first bytes of the packet that caused it, so it names a flow that may
    never have been captured directly.
    """
    out = []
    for off, pkt in pcapout.carve_ipv4(blob, protocols=(1,)):
        ihl = (pkt[0] & 0x0F) * 4
        b = pkt[ihl:]
        if len(b) < 8:
            continue
        t, c = b[0], b[1]
        if (t, c) not in ICMP_TYPES:
            continue                # unknown type: almost always a bad carve
        q = _quoted(b) if t in (3, 5, 11, 12) else ()
        out.append(Icmp(off, _addr(pkt, 12), _addr(pkt, 16), t, c, q))
    return out


def icmp_summary(blob):
    """(list_of_Icmp, Counter_of_names, set_of_flows_revealed_by_quotes)."""
    msgs = icmp(blob)
    tally = Counter(m.name for m in msgs)
    flows = {m.quoted for m in msgs if m.quoted}
    return msgs, tally, flows


def scan_random(nbytes=8 << 20, seed=0):
    """False-accept control: terminals and ICMP found in random bytes."""
    import numpy as np
    rng = np.random.default_rng(seed)
    blob = rng.integers(0, 256, nbytes, dtype=np.uint8).tobytes()
    ts = terminals(blob)
    strong = [t for t in ts if t.confidence != "weak"]
    return len(strong), len(icmp(blob))
