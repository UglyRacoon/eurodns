#!/usr/bin/env bash
#
# EuroDNS — one-shot installer for the Smart-DNS service on a Linux server.
# Deploys: dnsmasq (DNS), nginx stream SNI proxy (geo-unblock), a DoH/DoT
# endpoint (via stunnel + a small python backend), and the website.
#
# Usage:
#   sudo ./deploy.sh [options]
#
# Options (or set as env vars before running):
#   --host DOMAIN     management/website hostname  (default: smartdns-<ip>.sslip.io)
#                     A real domain you point at this server is recommended.
#   --email ADDR      contact for Let's Encrypt (recommended; enables cert renewal)
#   --ipv6 ADDR       public IPv6 for the service (default: auto-detect, may be empty)
#   --no-ipv6         do not advertise/serve on IPv6 at all
#   --brand NAME      site brand word used in copy replacement (default: EuroDNS)
#
# What it needs: Debian 12 / Ubuntu 22.04+ with systemd + apt, root, and TCP/UDP
# 80,443,53,853 free (or already managed by your firewall).
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Please run as root (sudo)." >&2; exit 1; }
command -v apt-get >/dev/null || { echo "This installer supports apt-based Debian/Ubuntu only." >&2; exit 1; }

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# ---------------------------------------------------------------------------
# 0. Parse args / defaults
# ---------------------------------------------------------------------------
MGMT_HOST="${MGMT_HOST:-}"
EMAIL="${EMAIL:-}"
IPV6="${IPV6:-}"
USE_V6="${USE_V6:-1}"
BRAND="${BRAND:-EuroDNS}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)   MGMT_HOST="$2"; shift 2 ;;
    --email)  EMAIL="$2"; shift 2 ;;
    --ipv6)   IPV6="$2"; shift 2 ;;
    --no-ipv6) USE_V6=0; shift ;;
    --brand)  BRAND="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

log(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn(){ printf '\033[1;33m  ! %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. Detect the public IPv4 (and IPv6) of this server
# ---------------------------------------------------------------------------
log "Detecting server public addresses"
IPV4=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oE 'src [0-9.]+' | head -1 | awk '{print $2}')
if [ -z "${IPV4:-}" ]; then
  IPV4=$(curl -4 -fs --max-time 10 https://ifconfig.me || true)
fi
[ -n "${IPV4:-}" ] || { echo "Could not determine a public IPv4. Pass it manually?" >&2; exit 1; }
echo "  IPv4: $IPV4"

if [ "$USE_V6" = "1" ] && [ -z "${IPV6:-}" ]; then
  IPV6=$(ip -6 route get 2001:4860:4860::8888 2>/dev/null | grep -oE 'src [0-9a-fA-F:]+' | head -1 | awk '{print $2}') || true
fi
[ "$USE_V6" = "1" ] || IPV6=""
echo "  IPv6: ${IPV6:-<none>}"

if [ -z "$MGMT_HOST" ]; then
  # sslip.io resolves "<dashed-ip>.sslip.io" (and any subdomain) to that IP for free
  MGMT_HOST="smartdns.$(echo "$IPV4" | tr '.' '-').sslip.io"
  warn "No --host given; using auto sslip.io hostname: $MGMT_HOST"
  warn "For a branded, stable name set up a real domain and re-run with: --host dns.example.com"
fi
echo "  Management host: $MGMT_HOST"

# ---------------------------------------------------------------------------
# 2. Install packages
# ---------------------------------------------------------------------------
log "Installing packages (nginx, dnsmasq, stunnel, certbot, python3, dnsutils)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  nginx libnginx-mod-stream dnsmasq python3 dnsutils curl \
  certbot gnupg ca-certificates
# stunnel: prefer 'stunnel4' (Debian/Ubuntu). Try both names.
if apt-get install -y --no-install-recommends stunnel4 2>/dev/null; then :; else apt-get install -y --no-install-recommends stunnel 2>/dev/null || true; fi
STUNNEL_BIN=$(command -v stunnel || command -v stunnel4 || true)
[ -n "$STUNNEL_BIN" ] || { echo "stunnel not installed — DoT will be skipped." >&2; STUNNEL_BIN=""; }

# ---------------------------------------------------------------------------
# 3. Free port 53 (avoid systemd-resolved / stock stunnel4 clashes)
# ---------------------------------------------------------------------------
log "Ensuring port 53 is available for dnsmasq"
systemctl disable --now stunnel4 2>/dev/null || true          # we run our own stunnel unit
if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
  if ss -lntu 2>/dev/null | grep -q ':53 .*0\.0\.0\.0\|:53 .*::\|:53 .*127\.0\.0\.1'; then
    warn "systemd-resolved is using :53; disabling its stub listener."
    mkdir -p /etc/systemd/resolved.conf.d
    printf '[Resolve]\nDNS=1.1.1.1 8.8.8.8\nDNSStubListener=no\n' > /etc/systemd/resolved.conf.d/eurodns.conf
    systemctl restart systemd-resolved || true
  fi
fi
# prevent NetworkManager from fighting over :53 on some images
rm -f /etc/dnsmasq.conf.dpkg-old 2>/dev/null || true

# ---------------------------------------------------------------------------
# 4. Lay down files
# ---------------------------------------------------------------------------
log "Installing files under /opt/smartdns and /var/www/smartdns"
install -d /opt/smartdns /var/www/smartdns /etc/nginx/smartdns \
           /etc/nginx/stream-enabled /etc/dnsmasq.d /etc/stunnel /run/stunnel-eurodns

cp -f "$SCRIPT_DIR/domains.txt"            /opt/smartdns/domains.txt
cp -f "$SCRIPT_DIR/build.sh"               /opt/smartdns/build.sh
cp -f "$SCRIPT_DIR/backend/smartdns_backend.py" /opt/smartdns/smartdns_backend.py
chmod 755 /opt/smartdns/build.sh; chmod 644 /opt/smartdns/smartdns_backend.py

# site, with demo literals replaced for THIS server
rm -rf /var/www/smartdns/*
cp -rf "$SCRIPT_DIR/site/." /var/www/smartdns/
chown -R www-data:www-data /var/www/smartdns
find /var/www/smartdns -type d -exec chmod 755 {} \;
find /var/www/smartdns -type f -exec chmod 644 {} \;

OLD_HOST="smartdns.147-45-114-74.sslip.io"
OLD_HOST2="147-45-114-74.sslip.io"
if [ "$OLD_HOST" != "$MGMT_HOST" ] || [ "$IPV4" != "147.45.114.74" ]; then
  log "Rewriting endpoints inside the website to this server"
  grep -rl --include='*.html' --include='*.mobileconfig' \
      -e "$OLD_HOST" -e "$OLD_HOST2" -e '147\.45\.114\.74' -e '2a0d:6c2:17:51b::' \
      /var/www/smartdns 2>/dev/null | while read -r f; do
    sed -i \
      -e "s|$OLD_HOST|$MGMT_HOST|g" \
      -e "s|$OLD_HOST2|$(echo "$MGMT_HOST" | sed "s/^smartdns\.//")|g" \
      -e "s|147\.45\.114\.74|$IPV4|g" \
      -e "s|2a0d:6c2:17:51b::|${IPV6:-$IPV4}|g" \
      -e "s|EuroDNS|$BRAND|g" \
      "$f"
  done || true
fi

# env.conf consumed by build.sh and by the systemd units
cat > /opt/smartdns/env.conf <<EOF
# generated by deploy.sh on $(date -u +%FT%TZ)
IPV4=$IPV4
IPV6=$IPV6
MGMT_HOST=$MGMT_HOST
PROXY_IP=$IPV4
LISTEN_ADDR=127.0.0.1
LISTEN_PORT=8090
DNS_UPSTREAM_HOST=127.0.0.1
DNS_UPSTREAM_PORT=53
EOF
chmod 600 /opt/smartdns/env.conf

# ---------------------------------------------------------------------------
# 5. Render nginx/dnsmasq configs from templates
# ---------------------------------------------------------------------------
log "Rendering service configuration"
render(){ # in out  KEY=VAL ...
  local in="$1" out="$2"; shift 2
  local exprs=(); for kv in "$@"; do exprs+=("-e" "s|@@${kv%%=*}@@|${kv#*=}|g"); done
  sed "${exprs[@]}" "$in" > "$out"
}

L6_LISTEN=""
if [ -n "$IPV6" ]; then L6_LISTEN="    listen [::]:443;"; fi

render "$SCRIPT_DIR/conf/stream-map.conf"    /etc/nginx/stream-enabled/10-eurodns-map.conf    "MGMT_HOST=$MGMT_HOST"
render "$SCRIPT_DIR/conf/stream-server.conf" /etc/nginx/stream-enabled/20-eurodns-server.conf "LISTEN6=$L6_LISTEN"

LISTEN_ADDRESSES="127.0.0.1,$IPV4"
[ -n "$IPV6" ] && LISTEN_ADDRESSES="$LISTEN_ADDRESSES,$IPV6"
render "$SCRIPT_DIR/conf/dnsmasq.conf" /etc/dnsmasq.conf "LISTEN_ADDRESSES=$LISTEN_ADDRESSES"

L6_80=""
[ -n "$IPV6" ] && L6_80="    listen [::]:80;"
render "$SCRIPT_DIR/conf/site-http.conf" /etc/nginx/sites-available/eurodns.conf \
  "MGMT_HOST=$MGMT_HOST" "LISTEN6_80=$L6_80"

# make sure there is exactly one top-level `stream { include ... }`
if ! grep -q 'eurodns-stream.conf' /etc/nginx/nginx.conf; then
  cat > /etc/nginx/eurodns-stream.conf <<'S'
stream {
    include /etc/nginx/stream-enabled/*.conf;
}
S
  sed -i '/^http {/i include /etc/nginx/eurodns-stream.conf;\n' /etc/nginx/nginx.conf
fi

# enable site
ln -sf /etc/nginx/sites-available/eurodns.conf /etc/nginx/sites-enabled/eurodns.conf
rm -f /etc/nginx/sites-enabled/default

# ---------------------------------------------------------------------------
# 6. Generate the per-domain maps from domains.txt
# ---------------------------------------------------------------------------
log "Building DNS + proxy maps from domains.txt"
/opt/smartdns/build.sh

# ---------------------------------------------------------------------------
# 7. TLS certificate (Let's Encrypt, webroot) — needed for DoH/DoT
# ---------------------------------------------------------------------------
log "Obtaining Let's Encrypt certificate for $MGMT_HOST"
# start dnsmasq + nginx first so :80 answers the ACME challenge
systemctl enable dnsmasq nginx >/dev/null 2>&1 || true
systemctl restart dnsmasq || true
if ! nginx -t 2>/tmp/eurodns-nginxtest.err; then
  echo "  nginx config test failed:"; sed 's/^/    /' /tmp/eurodns-nginxtest.err >&2
  if grep -qi 'duplicate "stream"' /tmp/eurodns-nginxtest.err; then
    warn "There is already a 'stream{ ... }' block in your nginx. See 'Coexisting with an existing SNI fronting' in README.md — merge geo-map.inc into that block and remove /etc/nginx/eurodns-stream.conf."
  fi
  exit 1
fi
systemctl reload nginx 2>/dev/null || systemctl restart nginx

CERT_DIR="/etc/letsencrypt/live/$MGMT_HOST"
if [ ! -e "$CERT_DIR/fullchain.pem" ]; then
  CB_ARGS=(--webroot -w /var/www/smartdns -d "$MGMT_HOST" --non-interactive --agree-tos)
  if [ -n "$EMAIL" ]; then CB_ARGS+=(-m "$EMAIL"); else CB_ARGS+=(--register-unsafely-without-email); fi
  certbot certonly "${CB_ARGS[@]}" --keep-until-expiring || warn "certbot failed — check that A record for $MGMT_HOST points to $IPV4 and :80 is reachable."
else
  echo "  certificate already present, skipping."
fi

install -D -m 755 "$SCRIPT_DIR/hooks/renew-hook.sh" /etc/letsencrypt/renewal-hooks/deploy/00-eurodns.sh

# ---------------------------------------------------------------------------
# 8. DoT (stunnel) + backend units
# ---------------------------------------------------------------------------
if [ -n "$STUNNEL_BIN" ]; then
  log "Configuring DNS-over-TLS (stunnel) on :853"
  render "$SCRIPT_DIR/conf/stunnel-dot.conf" /etc/stunnel/eurodns-dot.conf \
     "MGMT_HOST=$MGMT_HOST" "STUNNEL_IPV6="
  # point the unit at the stunnel binary actually present on this box
  sed "s#/usr/bin/stunnel #${STUNNEL_BIN} #" "$SCRIPT_DIR/systemd/stunnel-dot.service" \
      > /etc/systemd/system/eurodns-dot.service
  cp -f "$SCRIPT_DIR/systemd/smartdns-backend.service" /etc/systemd/system/smartdns-backend.service
  systemctl daemon-reload
  systemctl enable eurodns-dot smartdns-backend >/dev/null 2>&1 || true
  systemctl restart eurodns-dot 2>/dev/null || warn "stunnel DoT failed to start (see: journalctl -u eurodns-dot)"
else
  cp -f "$SCRIPT_DIR/systemd/smartdns-backend.service" /etc/systemd/system/smartdns-backend.service
  systemctl daemon-reload
  systemctl enable smartdns-backend >/dev/null 2>&1 || true
fi
systemctl restart smartdns-backend nginx

# ---------------------------------------------------------------------------
# 9. Firewall hints (no-op if none active)
# ---------------------------------------------------------------------------
log "Firewall: open these if you have a firewall in front"
echo "  TCP/UDP 53, TCP 80, TCP 443, TCP 853"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  warn "ufw is active — allow the ports: sudo ufw allow 53,80,443,853/tcp; sudo ufw allow 53/udp"
fi

# ---------------------------------------------------------------------------
# 10. Report
# ---------------------------------------------------------------------------
log "Done. Endpoints for your devices:"
cat <<EOF
  Plain DNS (IPv4): $IPV4
  Plain DNS (IPv6): ${IPV6:-<none>}
  DoH URL : https://$MGMT_HOST/dns-query
  DoT host: $MGMT_HOST  (port 853)
  Website : https://$MGMT_HOST/
  Selftest: https://$MGMT_HOST/api/selftest
EOF
echo
if [ -n "$STUNNEL_BIN" ] && [ -s "$CERT_DIR/fullchain.pem" ]; then
  echo "Test it:  dig @${IPV4} gemini.google.com +short   (expect: $IPV4)"
  echo "          curl -s https://$MGMT_HOST/api/selftest | head -c 200; echo"
else
  warn "TLS cert or stunnel missing — DoH/DoT may not be live yet. Re-run after fixing DNS :80 reachability."
fi
echo "Manage domains: edit /opt/smartdns/domains.txt, then: /opt/smartdns/build.sh && systemctl restart dnsmasq"
