#!/usr/bin/env bash
# EuroDNS — chain Google's egress through a clean EU/US exit box (Option A).
#
# WHY: this Smart-DNS box's own IPv4/IPv6 are geolocated as RUSSIA by Google
# (verified: https://www.google.com/ serves intl/ru on both stacks; even free
# Cloudflare WARP egress from here reads RU). Gemini/other IP-geo-gated Google
# services therefore stay "unsupported in your country" no matter how the DNS
# transport is fixed. Only an exit whose IP Google geo-reads as a SUPPORTED
# region works. This script wires that exit over WireGuard and policy-routes
# ONLY Google's IP ranges through it (SSH/DNS/the phone's other traffic are
# untouched). Same mechanism validated with WARP, but pointed at a real EU peer.
#
# Usage (run ON the Smart-DNS box):
#   ./deploy-eu-exit.sh <EU_HOST> <EU_USER> <EU_PASS_or_-> [WG_PORT]
#     - <EU_PASS_or_-> : password (uses sshpass) or "-" for key auth.
#     - WG_PORT default 51820/udp (must be open inbound on the EU box).
# Reversible:  ./deploy-eu-exit.sh down
#
# The EU box is only an outbound relay for Google ranges (masquerade); it does
# NOT proxy the phone. The phone keeps "just enter DNS" pointing at this box.

set -uo pipefail
GOOGLE_CIDRS="$(cd "$(dirname "$0")" && grep -vE '^\s*#|^\s*$' google-cidrs.txt | tr "\n" " ")"
IFACE="wgexit"
V4_MAIN="10.77.0.1/24"
V4_EU="10.77.0.2/24"

run_eu() { # <host> <user> <auth> <cmd>
  local H="$1" U="$2" A="$3"; shift 3
  if [ "$A" = "-" ]; then ssh -o StrictHostKeyChecking=accept-new "$U@$H" "$@"
  else sshpass -p "$A" ssh -o StrictHostKeyChecking=accept-new "$U@$H" "$@"; fi
}
copy_eu() { # <host> <user> <auth> <src> <dst>
  local H="$1" U="$2" A="$3" S="$4" D="$5"
  if [ "$A" = "-" ]; then scp -o StrictHostKeyChecking=accept-new "$S" "$U@$H:$D"
  else sshpass -p "$A" scp -o StrictHostKeyChecking=accept-new "$S" "$U@$H:$D"; fi
}

if [ "${1:-}" = "down" ]; then
  wg-quick down "/etc/wireguard/$IFACE.conf" 2>/dev/null || ip link del "$IFACE" 2>/dev/null || true
  echo "down: $IFACE removed. (Run the analogous 'wg-quick down' on the EU box to clean it.)"
  exit 0
fi

EU_HOST="${1:?usage: deploy-eu-exit.sh <EU_HOST> <EU_USER> <EU_PASS_or_-> [WG_PORT]}"
EU_USER="${2:?need EU_USER}"
EU_PASS="${3:?need EU_PASS or '-'}"
WG_PORT="${4:-51820}"

command -v wg >/dev/null || { echo "install wireguard-tools first (apt-get install -y wireguard-tools)"; exit 1; }

# 1) generate OUR keypair + the EU-side onboarding script with our pubkey
MAIN_PRIV=$(wg genkey); MAIN_PUB=$(echo "$MAIN_PRIV" | wg pubkey)
cat > /tmp/eu-onboard.sh <<EOF
#!/usr/bin/env bash
set -e
export DEBIAN_FRONTEND=noninteractive
command -v wg >/dev/null || { apt-get update -qq && apt-get install -y -qq wireguard-tools; }
echo 1 > /proc/sys/net/ipv4/ip_forward
PUBIF=\$(ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if(\$i=="dev") print \$(i+1)}')
iptables -t nat -C POSTROUTING -o \$PUBIF -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -o \$PUBIF -j MASQUERADE
EU_PRIV=\$(wg genkey); EU_PUB=\$(echo \$EU_PRIV | wg pubkey)
cat > /etc/wireguard/$IFACE.conf <<CFG
[Interface]
PrivateKey = \$EU_PRIV
Address = $V4_EU
ListenPort = $WG_PORT
PostUp = echo 1 > /proc/sys/net/ipv4/ip_forward
PreDown = true
# peer = the Smart-DNS box
[Peer]
PublicKey = $MAIN_PUB
AllowedIPs = $V4_MAIN
CFG
wg-quick up $IFACE 2>/dev/null || { ip link del $IFACE 2>/dev/null; wg-quick up $IFACE; }
echo "EU_PUB=\$EU_PUB"
echo "EU_IFACE=$IFACE"
echo "EU_LISTEN=$WG_PORT"
EOF

copy_eu "$EU_HOST" "$EU_USER" "$EU_PASS" /tmp/eu-onboard.sh /tmp/eu-onboard.sh
EU_OUT=$(run_eu "$EU_HOST" "$EU_USER" "$EU_PASS" "bash /tmp/eu-onboard.sh") || { echo "EU onboarding failed:"; echo "$EU_OUT"; exit 1; }
echo "$EU_OUT"
EU_PUB=$(echo "$EU_OUT" | awk -F= '/EU_PUB/{print $2}')
[ -z "$EU_PUB" ] && { echo "no EU_PUB parsed"; exit 1; }

# 2) add the EU box's own peer->main using the main's real egress ip (best effort)
run_eu "$EU_HOST" "$EU_USER" "$EU_PASS" "wg set $IFACE peer $MAIN_PUB endpoint '' 2>/dev/null; true" >/dev/null 2>&1 || true

# 3) write + bring up the MAIN side tunnel
MAIN_EP="$EU_HOST:$WG_PORT"
cat > /etc/wireguard/$IFACE.conf <<CFG
[Interface]
PrivateKey = $MAIN_PRIV
Address = $V4_MAIN
PostUp = sysctl -w net.ipv4.ip_forward=1 >/dev/null
CFG
echo "Endpoint = $MAIN_EP" >> /etc/wireguard/$IFACE.conf
echo "AllowedIPs = $GOOGLE_CIDRS" >> /etc/wireguard/$IFACE.conf
echo "" >> /etc/wireguard/$IFACE.conf
echo "[Peer]" >> /etc/wireguard/$IFACE.conf
echo "PublicKey = $EU_PUB" >> /etc/wireguard/$IFACE.conf
echo "Endpoint = $MAIN_EP" >> /etc/wireguard/$IFACE.conf
echo "AllowedIPs = $GOOGLE_CIDRS" >> /etc/wireguard/$IFACE.conf

wg-quick down $IFACE 2>/dev/null || true
wg-quick up $IFACE
echo "routes via $IFACE: $(ip route | grep -c "dev $IFACE")"
echo "=== VERIFY: what region does Google see now (v4), through the tunnel? ==="
curl -4 -s --max-time 12 https://www.google.com/ | grep -oiE "intl/[a-z]+/|prefdom=[A-Z]{2}" | head -1
echo "(if it still says ru, the EU box's ASN is also RU-tagged — try another region/box)"
echo "=== if intl is EU/US, Gemini should now open on the phone with just the entered DNS ==="
