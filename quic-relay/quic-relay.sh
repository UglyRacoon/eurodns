#!/usr/bin/env bash
# EuroDNS QUIC/HTTP-3 containment for Smart-DNS geo-unblock.
#
# Android Chrome (and Google apps) increasingly use HTTP/3 (QUIC) for the
# region/eligibility XHRs (alkimi, generativelanguage, batchexecute, ...).
# Even when our authoritative SVCB record advertises alpn=h2 (pushing the
# *initial* connection to TCP), a cached Alt-Svc or 0-RTT can still send QUIC
# straight to Google's real RU edge, bypassing our TCP SNI proxy -> "Gemini
# isn't supported in your country".
#
# This script makes inbound UDP/443 (the only port Chrome uses for QUIC to the
# IPs we hand out) a transparent L4 relay to a Google front-end (GFE). The GFE
# terminates the end-to-end QUIC handshake (real Google cert, SNI taken from the
# encrypted ClientHello we forward verbatim) and routes by SNI, while the source
# IP Google sees is our EU box (MASQUERADE). No TLS termination / MITM required.
#
# Prereq: hysteria (or any other UDP/443 service) must have been moved off :443.

set -uo pipefail

CMT="eurodns-quic"
GFE_HOST="${GFE_HOST:-www.google.com}"

GFE="$(getent ahostsv4 "$GFE_HOST" | awk '{print $1; exit}')"
if [ -z "$GFE" ]; then
  echo "quic-relay: cannot resolve $GFE_HOST" >&2
  exit 1
fi

sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true

# Remove any prior eurodns-quic rules (whatever GFE IP they point at), then add one.
# (iptables -S quotes the --comment value; strip quotes so the -D spec matches.)
iptables -t nat -S PREROUTING 2>/dev/null | grep "$CMT" | while read -r rule; do
  iptables -t nat $(printf '%s' "${rule//\"/}" | sed 's/^-A /-D /') 2>/dev/null || true
done
iptables -t nat -S POSTROUTING 2>/dev/null | grep "$CMT" | while read -r rule; do
  iptables -t nat $(printf '%s' "${rule//\"/}" | sed 's/^-A /-D /') 2>/dev/null || true
done

iptables -t nat -A PREROUTING  -p udp --dport 443 -m comment --comment "$CMT" -j DNAT --to-destination "$GFE":443
iptables -t nat -A POSTROUTING -d "$GFE"/32 -p udp --dport 443 -m comment --comment "$CMT" -j MASQUERADE

echo "quic-relay active: udp/443 -> $GFE:443 (masquerade, source = EU exit)"
