#!/usr/bin/env python3
# EuroDNS authoritative resolver (stdlib only). It runs on :53 and:
#   * answers A/AAAA/HTTPS(SVCB) for every domain in domains.txt -> the proxy IP,
#     synthesising a real HTTPS/SVCB record (alpn=h2, ipv4hint, ipv6hint) so modern
#     clients (Chrome/Google apps) connect to OUR box over TCP/HTTP2 even when they
#     would otherwise use QUIC / cached Alt-Svc / ECH.  THIS is what makes a plain
#     "just set the DNS" unblock work, exactly like commercial Smart-DNS.
#   * forwards all other queries to the local forwarder (dnsmasq on 127.0.0.1:5353).
#
# Config from environment (EnvironmentFile=/opt/smartdns/env.conf):
#   IPV4, IPV6 (optional), DNS_UPSTREAM_HOST (default 127.0.0.1), DNS_UPSTREAM_PORT
#   (default 5353), RESOLVER_PORT (default 53), DOMAINS_FILE (default /opt/smartdns/domains.txt)
import os, socket, struct, threading, collections

IPV4  = os.environ.get("IPV4", "").strip()
IPV6  = os.environ.get("IPV6", "").strip()
UP_H  = os.environ.get("DNS_UPSTREAM_HOST", "127.0.0.1")
UP_P  = int(os.environ.get("DNS_UPSTREAM_PORT", "5353"))
PORT  = int(os.environ.get("RESOLVER_PORT", "53"))
DOMF  = os.environ.get("DOMAINS_FILE", "/opt/smartdns/domains.txt")

T_A, T_AAAA, T_HTTPS, T_OPT = 1, 28, 65, 41

# --- load domain list ------------------------------------------------------
def load_domains():
    doms = set()
    try:
        with open(DOMF) as f:
            for line in f:
                line = line.split("#", 1)[0].strip().lower().rstrip(".")
                if line:
                    doms.add(line)
    except FileNotFoundError:
        pass
    return doms
DOMAINS = load_domains()

# OS/browser captive-portal & connectivity probes. These MUST resolve to the real
# Google servers (they do an HTTP:80 /generate_204 that our 443-only proxy can't
# answer), otherwise Android/Chrome mark the network "no internet" and stall.
# Exact hostnames only -> never affects the geo-unblock of real services.
NEVER = {
    "connectivitycheck.gstatic.com", "connectivitycheck.android.com",
    "connectivitycheck.google.com",
    "clients2.google.com", "clients3.google.com", "clients4.google.com", "clients5.google.com",
}

def is_mapped(name):
    n = name.lower().rstrip(".")
    if n in NEVER or any(n.endswith("." + s) for s in NEVER):
        return False
    return any(n == d or n.endswith("." + d) for d in DOMAINS)

# --- DNS wire helpers ------------------------------------------------------
def encode_name(name):
    out = b""
    for lbl in name.strip(".").split("."):
        b = lbl.encode()
        out += bytes([len(b)]) + b
    return out + b"\x00"

def parse_name(msg, off):
    """Return (name, next_offset) handling compression pointers."""
    parts, jumped, orig = [], False, off
    while True:
        ln = msg[off]
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack(">H", msg[off:off+2])[0] & 0x3FFF
            if not jumped:
                orig = off + 2
            jumped, off = True, ptr
            continue
        if ln == 0:
            off += 1
            break
        parts.append(msg[off+1:off+1+ln].decode("ascii", "replace"))
        off += 1 + ln
    name = ".".join(parts)
    if not name:
        name = "."
    return name, (orig if jumped else off)

def parse_question(msg):
    name, off = parse_name(msg, 12)
    qtype, qclass = struct.unpack(">HH", msg[off:off+4])
    return name, qtype, qclass, off + 4

def svcb_rdata():
    """SVCB/HTTPS RDATA: priority=1, target='.', params alpn=h2, ipv4hint, ipv6hint.
    SvcParams must be in ascending key order (1 alpn, 3 ipv4hint, 4 ipv6hint)."""
    rd = struct.pack(">H", 1) + b"\x00"        # priority 1, SvcDomainName = '.'
    params = []
    def pv(k, v):
        return struct.pack(">HH", k, len(v)) + v
    alpn = bytes([2]) + b"h2"                    # one protocol-ID: len 2, "h2"
    params.append(pv(1, alpn))                   # key 1 = alpn
    if IPV4:
        params.append(pv(3, socket.inet_aton(IPV4)))   # key 3 = ipv4hint
    if IPV6:
        params.append(pv(4, socket.inet_pton(socket.AF_INET6, IPV6)))  # key 4 = ipv6hint
    return rd + b"".join(params)

def answer_for(name, qtype):
    """Return list of (rtype, rdata) for a mapped name, or None -> caller forwards."""
    if qtype == T_A and IPV4:
        return [(T_A, socket.inet_aton(IPV4))]
    if qtype == T_AAAA and IPV6:
        return [(T_AAAA, socket.inet_pton(socket.AF_INET6, IPV6))]
    if qtype == T_HTTPS:
        return [(T_HTTPS, svcb_rdata())]
    if qtype in (T_A, T_AAAA, T_HTTPS):          # mapped but family not configured -> NODATA
        return []
    return None

def build_response(msg, name, qtype, rrs):
    tid = msg[:2]
    flags = 0x8000 | 0x0080 | 0x0400             # QR + RD(copy-ish) + AA
    hdr = tid + struct.pack(">HHHHH", flags, 1, len(rrs), 0, 0)
    qname_enc = encode_name(name)
    qsec = qname_enc + struct.pack(">HH", qtype, 1)
    ans = b""
    for rt, rd in rrs:
        ans += b"\xc0\x0c" + struct.pack(">HHIH", rt, 1, 300, len(rd)) + rd
    return hdr + qsec + ans

def forward(data):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(6)
    try:
        s.sendto(data, (UP_H, UP_P))
        r, _ = s.recvfrom(65535)
        return r
    except Exception:
        return struct.pack(">HHHHH", 0, 0x8081, 0, 0, 0)   # server-failure, don't hang client
    finally:
        s.close()

def handle(data, addr, sock):
    try:
        name, qtype, _qclass, _ = parse_question(data)
    except Exception:
        sock.sendto(forward(data), addr); return
    if is_mapped(name):
        rrs = answer_for(name, qtype)
        if rrs is not None:
            sock.sendto(build_response(data, name, qtype, rrs), addr); return
    sock.sendto(forward(data), addr)

def _bind(fam, addr):
    s = socket.socket(fam, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if fam == socket.AF_INET6:
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s.bind((addr, PORT))
    return s

def udp_loop(fam=socket.AF_INET, addr="0.0.0.0"):
    s = _bind(fam, addr)
    while True:
        data, a = s.recvfrom(65535)
        threading.Thread(target=handle, args=(data, a, s), daemon=True).start()

def _bind6_tcp(addr="::"):
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    s.bind((addr, PORT)); s.listen(128); return s

def tcp_loop(fam=socket.AF_INET, addr="0.0.0.0"):
    if fam == socket.AF_INET6:
        s = _bind6_tcp(addr)
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, PORT)); s.listen(128)
    def serve(c):
        # A DoT/TCP client (Android Private DNS, Chrome) keeps ONE connection open and
        # pipelines MANY queries over it. Serve them in a loop until the peer closes;
        # closing after a single answer makes Android fail with "Couldn't connect".
        c.settimeout(60)
        try:
            while True:
                ln = c.recv(2)
                if len(ln) < 2:
                    return
                (n,) = struct.unpack(">H", ln)
                if n == 0:
                    return
                data = b""
                while len(data) < n:
                    chunk = c.recv(n - len(data))
                    if not chunk:
                        return
                    data += chunk
                try:
                    name, qtype, _q, _ = parse_question(data)
                except Exception:
                    name, qtype = None, None
                if name and is_mapped(name):
                    rrs = answer_for(name, qtype)
                    resp = build_response(data, name, qtype, rrs) if rrs is not None else forward(data)
                else:
                    resp = forward(data)
                c.sendall(struct.pack(">H", len(resp)) + resp)
        except Exception:
            return
        finally:
            try:
                c.close()
            except Exception:
                pass
    while True:
        c, _ = s.accept()
        threading.Thread(target=serve, args=(c,), daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=tcp_loop, kwargs={"fam": socket.AF_INET}, daemon=True).start()
    threading.Thread(target=tcp_loop, kwargs={"fam": socket.AF_INET6, "addr": "::"}, daemon=True).start()
    threading.Thread(target=udp_loop, kwargs={"fam": socket.AF_INET6, "addr": "::"}, daemon=True).start()
    udp_loop()  # IPv4 UDP on the main thread
