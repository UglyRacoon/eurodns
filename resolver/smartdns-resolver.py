#!/usr/bin/env python3
# EuroDNS authoritative resolver (stdlib only). It runs on :53 and:
#   * answers A/AAAA/HTTPS(SVCB) for every domain in domains.txt -> the proxy IP,
#     synthesising a real HTTPS/SVCB record (alpn=h2, ipv4hint, ipv6hint) so modern
#     clients (Chrome/Google apps) connect to OUR box over TCP/HTTP2 even when they
#     would otherwise use QUIC / cached Alt-Svc / ECH.  THIS is what makes a plain
#     "just set the DNS" unblock work, exactly like commercial Smart-DNS.
#   * forwards all other queries to the local forwarder (dnsmasq on 127.0.0.1:5353).
# Responses echo the client's EDNS0 OPT record (Android's DoT validator requires it).
#
# Config from environment (EnvironmentFile=/opt/smartdns/env.conf):
#   IPV4, IPV6 (optional), DNS_UPSTREAM_HOST (default 127.0.0.1), DNS_UPSTREAM_PORT
#   (default 5353), RESOLVER_PORT (default 53), DOMAINS_FILE (default /opt/smartdns/domains.txt),
#   LOG_QUERIES (1 = log each query to the journal for debugging)
import os, socket, struct, threading
try:
    import ssl
except Exception:
    ssl = None

IPV4  = os.environ.get("IPV4", "").strip()
IPV6  = os.environ.get("IPV6", "").strip()
UP_H  = os.environ.get("DNS_UPSTREAM_HOST", "127.0.0.1")
UP_P  = int(os.environ.get("DNS_UPSTREAM_PORT", "5353"))
PORT  = int(os.environ.get("RESOLVER_PORT", "53"))
DOMF  = os.environ.get("DOMAINS_FILE", "/opt/smartdns/domains.txt")
LOGQ  = os.environ.get("LOG_QUERIES", "").strip().lower() in ("1", "true", "yes")
# Native DNS-over-TLS (no stunnel) with ALPN "dot" (RFC 7858) — required by Android's
# strict Private-DNS validator. Enable by setting DOT_CERT/DOT_KEY.
DOT_PORT  = int(os.environ.get("DOT_PORT", "853"))
DOT_CERT  = os.environ.get("DOT_CERT", "").strip()
DOT_KEY   = os.environ.get("DOT_KEY", "").strip()
DOT_READY = bool(ssl and DOT_CERT and DOT_KEY and os.path.exists(DOT_CERT) and os.path.exists(DOT_KEY))

T_A, T_AAAA, T_HTTPS, T_OPT = 1, 28, 65, 41
QT = {1: "A", 28: "AAAA", 65: "HTTPS", 12: "PTR", 16: "TXT", 5: "CNAME", 33: "SRV", 255: "ANY"}

def logq(s):
    if LOGQ:
        print(s, flush=True)

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
        if lbl:
            b = lbl.encode()
            out += bytes([len(b)]) + b
    return out + b"\x00"

def parse_name(msg, off):
    parts, jumped, orig = [], False, off
    while True:
        ln = msg[off]
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack(">H", msg[off:off + 2])[0] & 0x3FFF
            if not jumped:
                orig = off + 2
            jumped, off = True, ptr
            continue
        if ln == 0:
            off += 1
            break
        parts.append(msg[off + 1:off + 1 + ln].decode("ascii", "replace"))
        off += 1 + ln
    name = ".".join(parts) or "."
    return name, (orig if jumped else off)

def _skip_name(msg, off):
    while off < len(msg):
        ln = msg[off]
        if ln & 0xC0 == 0xC0:
            return off + 2
        if ln == 0:
            return off + 1
        off += 1 + ln
    return off

def parse_msg(msg):
    """Return {name,qtype,qclass,tid,edns,bufsize,do} or None on parse error."""
    if len(msg) < 12:
        return None
    tid = msg[:2]
    flags, qd, an, ns, ar = struct.unpack(">HHHHH", msg[2:12])
    name, off = parse_name(msg, 12)
    if off + 4 > len(msg):
        return None
    qtype, qclass = struct.unpack(">HH", msg[off:off + 4]); off += 4
    edns, bufsize, do = False, 512, False
    for _ in range(an + ns + ar):
        if off >= len(msg):
            break
        off = _skip_name(msg, off)
        if off + 10 > len(msg):
            break
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", msg[off:off + 10]); off += 10
        if rtype == T_OPT:
            edns, bufsize, do = True, rclass, bool(ttl & 0x8000)
        off += rdlen
    return {"name": name, "qtype": qtype, "qclass": qclass, "tid": tid,
            "rd": bool(flags & 0x0100), "edns": edns, "bufsize": bufsize, "do": do}

def svcb_rdata():
    rd = struct.pack(">H", 1) + b"\x00"        # priority 1, SvcDomainName = '.'
    def pv(k, v):
        return struct.pack(">HH", k, len(v)) + v
    params = [pv(1, bytes([2]) + b"h2")]        # alpn=h2
    if IPV4:
        params.append(pv(3, socket.inet_aton(IPV4)))
    if IPV6:
        params.append(pv(4, socket.inet_pton(socket.AF_INET6, IPV6)))
    return rd + b"".join(params)

def answer_for(qtype):
    """Return list of (rtype,rdata) for a mapped name, or None -> caller forwards."""
    if qtype == T_A:
        return [(T_A, socket.inet_aton(IPV4))] if IPV4 else []
    if qtype == T_AAAA:
        return [(T_AAAA, socket.inet_pton(socket.AF_INET6, IPV6))] if IPV6 else []
    if qtype == T_HTTPS:
        return [(T_HTTPS, svcb_rdata())]
    if qtype in (T_A, T_AAAA, T_HTTPS):
        return []
    return None

def build_response(msg, info, rrs):
    flags = 0x8000 | 0x0400 | (0x0100 if info["rd"] else 0)   # QR + AA + (RD echo)
    arcount = 1 if info["edns"] else 0
    hdr = info["tid"] + struct.pack(">HHHHH", flags, 1, len(rrs), 0, arcount)
    qsec = encode_name(info["name"]) + struct.pack(">HH", info["qtype"], info["qclass"])
    ans = b""
    for rt, rd in rrs:
        ans += b"\xc0\x0c" + struct.pack(">HHIH", rt, 1, 300, len(rd)) + rd
    extra = b""
    if info["edns"]:
        ttl = 0x8000 if info["do"] else 0
        extra = b"\x00" + struct.pack(">HHIH", T_OPT, info["bufsize"], ttl, 0)
    return hdr + qsec + ans + extra

def forward(data):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(6)
    try:
        s.sendto(data, (UP_H, UP_P))
        r, _ = s.recvfrom(65535)
        return r
    except Exception:
        return struct.pack(">HHHHH", 0, 0x8081, 0, 0, 0)
    finally:
        s.close()

def respond(data):
    """Decide the answer bytes for a raw DNS query (shared by UDP and TCP)."""
    info = parse_msg(data)
    if info is None:
        logq("parse-fail -> fwd %d bytes" % len(data))
        return forward(data)
    mapped = is_mapped(info["name"])
    rrs = answer_for(info["qtype"]) if mapped else None
    if mapped and rrs is not None:
        logq("%s %s%s -> LOCAL(%d)" % (info["name"], QT.get(info["qtype"], info["qtype"]),
                                       "/EDNS" if info["edns"] else "", len(rrs)))
        return build_response(data, info, rrs)
    logq("%s %s%s -> FORWARD" % (info["name"], QT.get(info["qtype"], info["qtype"]),
                                 "/EDNS" if info["edns"] else ""))
    return forward(data)

def handle(data, addr, sock):
    try:
        sock.sendto(respond(data), addr)
    except Exception as e:
        logq("udp handle err: %r" % e)

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
        # Keep the connection open and serve multiple pipelined queries; closing
        # after one answer makes Android report "Couldn't connect".
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
                r = respond(data)
                c.sendall(struct.pack(">H", len(r)) + r)
        except Exception as e:
            logq("tcp serve err: %r" % e)
        finally:
            try:
                c.close()
            except Exception:
                pass
    while True:
        c, _ = s.accept()
        threading.Thread(target=serve, args=(c,), daemon=True).start()

def dot_loop():
    """Native DNS-over-TLS on DOT_PORT with ALPN 'dot' (Android Private DNS requirement)."""
    if not DOT_READY:
        logq("DoT disabled (set DOT_CERT/DOT_KEY)"); return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_alpn_protocols(["dot"])
    except Exception as e:
        logq("ALPN set failed: %r" % e)
    try:
        ctx.load_cert_chain(DOT_CERT, DOT_KEY)
    except Exception as e:
        logq("DoT cert load failed: %r" % e); return
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", DOT_PORT)); srv.listen(128)
    logq("DoT listening on :%d (ALPN dot)" % DOT_PORT)

    def serve(c0, addr):
        try:
            c = ctx.wrap_socket(c0, server_side=True)
        except Exception as e:
            logq("DoT handshake fail %s: %r" % (addr[0], e))
            try: c0.close()
            except Exception: pass
            return
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
                    ch = c.recv(n - len(data))
                    if not ch:
                        return
                    data += ch
                r = respond(data)
                c.sendall(struct.pack(">H", len(r)) + r)
        except Exception as e:
            logq("DoT serve err: %r" % e)
        finally:
            try: c.close()
            except Exception:
                pass
    while True:
        c0, addr = srv.accept()
        threading.Thread(target=serve, args=(c0, addr), daemon=True).start()

if __name__ == "__main__":
    if DOT_READY:
        threading.Thread(target=dot_loop, daemon=True).start()
    threading.Thread(target=tcp_loop, kwargs={"fam": socket.AF_INET}, daemon=True).start()
    threading.Thread(target=tcp_loop, kwargs={"fam": socket.AF_INET6, "addr": "::"}, daemon=True).start()
    threading.Thread(target=udp_loop, kwargs={"fam": socket.AF_INET6, "addr": "::"}, daemon=True).start()
    udp_loop()  # IPv4 UDP on the main thread
