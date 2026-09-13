#!/usr/bin/env python3
# EuroDNS backend (stdlib only):
#   * DoH endpoint   /dns-query     (RFC 8484) -> forwards to local dnsmasq
#   * Self-test      /api/selftest  -> proves the geo-unblock works end-to-end
#
# All configuration comes from environment variables (set by systemd via
# EnvironmentFile=/opt/smartdns/env.conf), so nothing here is hardcoded.
import socket, base64, json, ssl, struct, os, datetime, subprocess, urllib.parse
import http.server

LISTEN_ADDR   = os.environ.get("LISTEN_ADDR", "127.0.0.1")
LISTEN_PORT   = int(os.environ.get("LISTEN_PORT", "8090"))
DNS_UPSTREAM  = (os.environ.get("DNS_UPSTREAM_HOST", "127.0.0.1"),
                 int(os.environ.get("DNS_UPSTREAM_PORT", "53")))
# Public IP the transparent SNI proxy is served on (what backends will see us as).
PROXY_IP      = os.environ.get("PROXY_IP", "").strip()
# Hosts the self-test checks (comma separated).
GEO_HOSTS     = [h.strip() for h in os.environ.get(
    "SELFTEST_HOSTS", "gemini.google.com,chatgpt.com,claude.ai,open.spotify.com").split(",") if h.strip()]


def detect_egress_ip():
    """Best-effort detection of this host's outbound IP (UDP connect, no packets sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


class DNSError(Exception):
    pass


def dns_query(raw):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(8)
    try:
        s.sendto(raw, DNS_UPSTREAM)
        data, _ = s.recvfrom(65535)
        return data
    finally:
        s.close()


def build_a_query(name):
    hdr = os.urandom(2) + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0)
    q = b""
    for part in name.split("."):
        q += bytes([len(part)]) + part.encode()
    q += b"\x00\x00\x01\x00\x01"   # A, IN
    return hdr + q


def parse_a_answers(msg):
    if len(msg) < 12:
        return []
    qd, an = struct.unpack(">HH", msg[4 + 2:4 + 6])
    off = 12
    for _ in range(qd):
        while msg[off] != 0:
            off += msg[off] + 1
        off += 1 + 4
    out = []
    for _ in range(an):
        if msg[off] & 0xC0 == 0xC0:
            off += 2
        else:
            while msg[off] != 0:
                off += msg[off] + 1
            off += 1
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", msg[off:off + 10])
        off += 10
        if rtype == 1 and rclass == 1:
            out.append(".".join(str(b) for b in msg[off:off + rdlen]))
        off += rdlen
    return out


def resolve_a(host):
    """Prefer system `dig` against our own dnsmasq (authoritative + reliable)."""
    try:
        out = subprocess.check_output(
            ["dig", "+short", "+time=3", "+tries=1", "@" + DNS_UPSTREAM[0], host, "A"],
            text=True, timeout=8)
        ips = [l.strip() for l in out.splitlines()
               if l.strip() and all(c in "0123456789." for c in l.strip())]
        if ips:
            return ips
    except Exception:
        pass
    try:
        return parse_a_answers(dns_query(build_a_query(host)))
    except Exception:
        return []


def fetch_over_proxy(host, path="/", timeout=15):
    """Connect to PROXY_IP:443 with SNI=host (simulates a client that resolved
    host -> our IP), so the stream proxy forwards to the real backend."""
    ctx = ssl.create_default_context()
    s = socket.create_connection((PROXY_IP, 443), timeout=timeout)
    ss = ctx.wrap_socket(s, server_hostname=host)
    req = ("GET %s HTTP/1.1\r\nHost: %s\r\n"
           "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
           "Accept: text/html\r\nConnection: close\r\n\r\n" % (path, host))
    ss.sendall(req.encode())
    data = b""
    try:
        while True:
            chunk = ss.recv(8192)
            if not chunk:
                break
            data += chunk
            if len(data) > 1_500_000:
                break
    except Exception:
        pass
    ss.close()
    return data


BLOCK_MARKERS = [
    b"isn't currently supported in your country",
    b"is not currently supported in your country",
    "isn\u2019t currently supported in your country".encode("utf-8"),
    b"Stay tuned",
    b"not available in your region",
    b"unavailable in your country",
]


def selftest():
    results = {"generated_at": datetime.datetime.utcnow().isoformat() + "Z",
               "proxy_ip": PROXY_IP or None, "services": []}
    dns_ok = bool(PROXY_IP)
    for host in GEO_HOSTS:
        ips = resolve_a(host)
        dns_hit = bool(PROXY_IP) and (PROXY_IP in ips)
        dns_ok = dns_ok and dns_hit
        if PROXY_IP:
            try:
                body = fetch_over_proxy(host, "/")
                head = body.split(b"\r\n\r\n", 1)[0].decode(errors="replace")
                status = int(head.split(" ", 2)[1]) if head.startswith("HTTP") else 0
                text = body.split(b"\r\n\r\n", 1)[-1]
                blocked = any(m in text for m in BLOCK_MARKERS)
                ok = bool(status) and not blocked
            except Exception:
                status, blocked, ok = 0, None, False
        else:
            status, blocked, ok = 0, None, dns_hit
        results["services"].append({
            "host": host,
            "dns_resolves_to_proxy": dns_hit,
            "dns_answers": ips,
            "http_status": status,
            "geo_block_detected": bool(blocked) if blocked is not None else None,
            "reachable_and_unblocked": ok,
        })
    results["dns_mapping_ok"] = dns_ok
    results["all_unblocked"] = all(s["reachable_and_unblocked"] for s in results["services"])
    return results


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/dns-query":
            self.send_error(404); return
        if self.headers.get("Content-Type") != "application/dns-message":
            self.send_error(415); return
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            self._send(200, "application/dns-message", dns_query(raw))
        except Exception as e:
            self.send_error(502, str(e))

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/dns-query":
            q = urllib.parse.urlparse(self.path).query
            b64 = urllib.parse.parse_qs(q).get("dns", [None])[0]
            if not b64:
                self.send_error(400); return
            raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
            try:
                self._send(200, "application/dns-message", dns_query(raw))
            except Exception as e:
                self.send_error(502, str(e))
        elif p == "/api/selftest":
            self._send(200, "application/json", json.dumps(selftest(), ensure_ascii=False))
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not PROXY_IP:
        PROXY_IP = detect_egress_ip()
    http.server.ThreadingHTTPServer((LISTEN_ADDR, LISTEN_PORT), H).serve_forever()
