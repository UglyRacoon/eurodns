#!/bin/bash
# EuroDNS generator: turns /opt/smartdns/domains.txt into
#   - /etc/dnsmasq.d/10-smartdns-domains.conf        (address=/DOMAIN/IP for IPv4+IPv6)
#   - /etc/nginx/smartdns/geo-map.inc                (nginx stream map regex entries)
#
# Reads the proxy IPs from /opt/smartdns/env.conf (written by deploy.sh).
set -euo pipefail

ENV_FILE=${ENV_FILE:-/opt/smartdns/env.conf}
DOM=${DOM:-/opt/smartdns/domains.txt}
DNS_OUT=${DNS_OUT:-/etc/dnsmasq.d/10-smartdns-domains.conf}
NGX_DIR=${NGX_DIR:-/etc/nginx/smartdns}
NGX_OUT="$NGX_DIR/geo-map.inc"

# load config
if [ -f "$ENV_FILE" ]; then . "$ENV_FILE"; fi
IP4=${IPV4:-}
IP6=${IPV6:-}

if [ -z "$IP4" ]; then
    echo "ERROR: IPV4 not set in $ENV_FILE — cannot map domains to a proxy IP." >&2
    exit 1
fi

mkdir -p "$NGX_DIR" "$(dirname "$DNS_OUT")"
: > "$DNS_OUT"
: > "$NGX_OUT"

n=0
while read -r d; do
    # strip comments + CR, skip blanks
    d=${d%%#*}; d=$(echo "$d" | tr -d '\r' | xargs || true)
    [ -z "$d" ] && continue
    echo "address=/$d/$IP4" >> "$DNS_OUT"
    [ -n "$IP6" ] && echo "address=/$d/$IP6" >> "$DNS_OUT"
    re=$(printf '%s' "$d" | sed 's/\./\\./g')
    printf '    ~(^|\\.)%s$  %s:443;\n' "$re" "$d" >> "$NGX_OUT"
    n=$((n+1))
done < "$DOM"

echo "EuroDNS: generated $n domains -> $DNS_OUT and $NGX_OUT"
