#!/bin/bash
# EuroDNS generator: turns /opt/smartdns/domains.txt into
#   - /etc/nginx/smartdns/geo-map.inc   (nginx stream map: SNI -> real backend:443)
#
# DNS mapping is handled entirely by the authoritative eurodns-resolver (:53), which
# reads /opt/smartdns/domains.txt directly and answers A/AAAA + HTTPS/SVCB for those
# names (with a small never-proxy exception for captive-portal hosts). dnsmasq is only
# a loopback forwarder (:5353) and MUST NOT carry per-domain address= rules, or it would
# re-map exactly the connectivity-check hosts the resolver intentionally forwards.
#
# After editing domains.txt: run this (for the nginx map) then `systemctl restart
# eurodns-resolver` (for DNS/SVCB) and reload nginx.
#
# Reads the proxy IPs from /opt/smartdns/env.conf (written by deploy.sh).
set -euo pipefail

ENV_FILE=${ENV_FILE:-/opt/smartdns/env.conf}
DOM=${DOM:-/opt/smartdns/domains.txt}
NGX_DIR=${NGX_DIR:-/etc/nginx/smartdns}
NGX_OUT="$NGX_DIR/geo-map.inc"

# load config (IPV6 optional)
if [ -f "$ENV_FILE" ]; then . "$ENV_FILE"; fi
IP4=${IPV4:-}
[ -n "$IP4" ] || { echo "ERROR: IPV4 not set in $ENV_FILE." >&2; exit 1; }

mkdir -p "$NGX_DIR"
: > "$NGX_OUT"

n=0
while read -r d; do
    # strip comments + CR, skip blanks
    d=${d%%#*}; d=$(echo "$d" | tr -d '\r' | xargs || true)
    [ -z "$d" ] && continue
    re=$(printf '%s' "$d" | sed 's/\./\\./g')
    printf '    ~(^|\\.)%s$  %s:443;\n' "$re" "$d" >> "$NGX_OUT"
    n=$((n+1))
done < "$DOM"

echo "EuroDNS: generated $n domains -> $NGX_OUT  (DNS/SVCB served by eurodns-resolver)"
