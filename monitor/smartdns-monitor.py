#!/usr/bin/env python3
# EuroDNS DoT monitor / self-heal loop.
#
# Follows the eurodns-resolver journal and scores every DNS-over-TLS session
# (i.e. every Android "Private DNS" connection). It prints a clear health line
# per session, writes a latest-status file, and AUTO-FIXES the known failure
# signatures (restarts the resolver to clear a stuck/cert state, and re-applies
# a config fix if the Android validation probe is ever hijacked). It then keeps
# waiting for the next phone request and re-scores, until sessions come back
# healthy (verdict=OK, real app traffic after the validation probe).
#
# Env:
#   PHONE_IP   optional; restrict to one client IP (empty = watch ALL DoT clients)
#   REMEDIATE  1 (default) enables the self-heal actions; 0 = observe only
#   RESOLVER_UNIT  default eurodns-resolver
#   STATUS_FILE    default /run/eurodns-monitor.status
#   MONITOR_LOG    default /var/log/eurodns/monitor.log
import os, re, sys, time, subprocess

PHONE_IP   = os.environ.get("PHONE_IP", "").strip()
REMEDIATE  = os.environ.get("REMEDIATE", "1").strip().lower() in ("1", "true", "yes")
UNIT       = os.environ.get("RESOLVER_UNIT", "eurodns-resolver")
STATUS     = os.environ.get("STATUS_FILE", "/run/eurodns-monitor.status")
LOGF       = os.environ.get("MONITOR_LOG", "/var/log/eurodns/monitor.log")
COOLDOWN   = int(os.environ.get("COOLDOWN", "300"))
NEVER_FILE = "/opt/smartdns/never-probe.fix"   # marker used by the probe-hijack remediation

CONNECT  = re.compile(r"DoT (\S+) CONNECT alpn=(\S+)")
QRY      = re.compile(r"DoT (\S+) (\S+) (A|AAAA|HTTPS|PTR|TXT|SVCB)\S* -> (\S+)")
CLOSE    = re.compile(r"DoT (\S+) CLOSE queries=(\d+) probe=(\S+) first=(\S+) verdict=(\S+)")
HSFAIL   = re.compile(r"DoT handshake fail (\S+): (.*)")

last_action = {}
sessions    = {}   # ip -> dict(connect_ts, alpn, nq, probe, first, proxied_probe)
history     = []   # rolling verdict lines

def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def emit(msg):
    line = "%s %s" % (ts(), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGF), exist_ok=True)
        with open(LOGF, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def set_status(text):
    try:
        d = os.path.dirname(STATUS)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(STATUS, "w") as f:
            f.write("%s %s\n" % (ts(), text))
    except Exception:
        pass

def action_due(key):
    now = time.time()
    if now - last_action.get(key, 0) < COOLDOWN:
        return False
    last_action[key] = now
    return True

def restart_resolver(reason):
    if not REMEDIATE or not action_due("restart:" + reason):
        return
    emit("AUTO-FIX: systemctl restart %s (reason=%s)" % (UNIT, reason))
    try:
        subprocess.call(["systemctl", "restart", UNIT])
    except Exception as e:
        emit("AUTO-FIX restart failed: %r" % e)

def fix_probe_hijack():
    # The validation probe MUST resolve to the real Google answer. If we ever answer
    # it locally, drop metric.gstatic.com from the mapped domains and rebuild.
    if not action_due("probe_hijack"):
        return
    emit("AUTO-FIX: probe was hijacked -> removing gstatic/metric from mapping + restart")
    try:
        open(NEVER_FILE, "w").write(ts() + "\n")
    except Exception:
        pass
    subprocess.call(["sed", "-i", "-E", "/metric\\.gstatic\\.com/d", "/opt/smartdns/domains.txt"])
    restart_resolver("probe_hijack")

def score_close(ip, nq, probe, first, verdict, alpn):
    ok = (verdict == "OK")
    tag = "PHONE" if (PHONE_IP and ip == PHONE_IP) else "client"
    line = "DoT %s %s: queries=%d probe=%s first=%s alpn=%s -> %s" % (
        tag, ip, nq, probe, first, alpn, "HEALTHY ✓" if ok else "REJECTED ✗")
    history.append(line)
    del history[:-25]
    emit("MONITOR " + line)
    set_status(("DoT OK" if ok else "DoT REJECTED") + " last=%s %s" % (ip, line.split("->",1)[1].strip()))
    if not ok:
        if alpn != "dot":
            emit("  cause: ALPN was %r (expected 'dot')" % alpn)
        restart_resolver("rejected")
    return ok

def main():
    emit("monitor started unit=%s phone_ip=%s remediate=%s" % (UNIT, PHONE_IP or "<all>", REMEDIATE))
    p = subprocess.Popen(["journalctl", "-u", UNIT, "-f", "-o", "cat", "--since", "now"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    for raw in p.stdout:
        line = raw.rstrip("\n")
        m = CONNECT.search(line)
        if m:
            ip, alpn = m.group(1), m.group(2)
            sessions[ip] = {"alpn": alpn, "nq": 0, "probe": "none", "first": "?", "proxied_probe": False}
            emit("DoT %s connected (alpn=%s)" % (ip, alpn))
            set_status("DoT connecting %s alpn=%s" % (ip, alpn))
            continue
        m = QRY.search(line)
        if m:
            ip, name, qtype, act = m.group(1), m.group(2), m.group(3), m.group(4)
            s = sessions.get(ip)
            if s is not None:
                s["nq"] += 1
                if s["first"] == "?":
                    s["first"] = name
                if "dnsotls-ds" in name or "metric.gstatic" in name:
                    s["probe"] = name
                    if act.startswith("LOCAL"):
                        s["proxied_probe"] = True
                        fix_probe_hijack()
            continue
        if (m := HSFAIL.search(line)):
            # Do NOT auto-restart on a single handshake failure: that is usually a
            # scanner / unrelated client's TLS noise. Real Android rejections show up
            # as a CLOSE with verdict=REJECTED, which IS remediated below.
            emit("DoT handshake fail (ignored, likely not the phone): %s: %s" % (m.group(1), m.group(2)))
            continue
        m = CLOSE.search(line)
        if m:
            ip, nq, probe, first, verdict = m.groups()
            nq = int(nq)
            s = sessions.pop(ip, {}) or {}
            alpn = s.get("alpn", "?")
            score_close(ip, nq, probe, first, verdict, alpn)
            continue
    emit("journal stream ended; exiting")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
