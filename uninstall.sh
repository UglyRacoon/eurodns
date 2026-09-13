#!/usr/bin/env bash
#
# EuroDNS — uninstaller. Reverses what deploy.sh installed.
#
# Usage:
#   sudo ./uninstall.sh [options]
#
# Options:
#   --yes              do not ask for confirmation
#   --purge-packages   also apt-purge nginx, dnsmasq, stunnel*, certbot (DANGER if
#                      those are shared with other services on the box — off by default)
#   --keep-cert        do NOT delete the Let's Encrypt certificate
#   --keep-data        do NOT remove /opt/smartdns or /var/www/smartdns
#
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Please run as root (sudo)." >&2; exit 1; }

AUTO_YES=0 PURGE_PKGS=0 KEEP_CERT=0 KEEP_DATA=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) AUTO_YES=1; shift ;;
    --purge-packages) PURGE_PKGS=1; shift ;;
    --keep-cert) KEEP_CERT=1; shift ;;
    --keep-data) KEEP_DATA=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

log(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info(){ printf '    %s\n' "$*"; }
run(){ printf '    $ %s\n' "$*"; eval "$*"; }

# Read MGMT_HOST from the install's env.conf before we tear it down (to target cert + include).
MGMT_HOST=""
if [ -f /opt/smartdns/env.conf ]; then
  # shellcheck disable=SC1091
  . /opt/smartdns/env.conf 2>/dev/null || true
  MGMT_HOST="${MGMT_HOST:-}"
fi

cat <<EOM
EuroDNS uninstaller.

  Services it will stop/remove : eurodns-resolver, smartdns-backend, eurodns-dot
  Config it will remove         : dnsmasq geo map, nginx site + stream include, stunnel conf
  Certificate host              : ${MGMT_HOST:-<unknown: run on the same server>}
  Website dir                   : /var/www/smartdns
  State dir                     : /opt/smartdns
  Packages                      : ${PURGE_PKGS:+PURGE (nginx/dnsmasq/stunnel/certbot)}${PURGE_PKGS:-left in place (default)}
EOM
if [ "$PURGE_PKGS" -eq 0 ]; then info "(pass --purge-packages to also remove apt packages)"; fi

if [ "$AUTO_YES" -ne 1 ]; then
  read -r -p "$(printf '\nProceed? Type yes to continue: ')" ans
  [ "$ans" = "yes" ] || { echo "Aborted."; exit 1; }
fi

# ---------------------------------------------------------------------------
log "Stopping + disabling systemd units"
for u in eurodns-resolver smartdns-backend eurodns-dot; do
  if systemctl list-unit-files --type=service 2>/dev/null | grep -q "^${u}.service"; then
    run "systemctl stop ${u} || true"
    run "systemctl disable ${u} || true"
    rm -f "/etc/systemd/system/${u}.service"
  else
    info "unit ${u}.service not present (skip)"
  fi
done
systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed 2>/dev/null || true

# ---------------------------------------------------------------------------
log "Removing nginx configuration"
rm -f /etc/nginx/sites-enabled/eurodns.conf \
      /etc/nginx/sites-available/eurodns.conf \
      /etc/nginx/stream-enabled/10-eurodns-map.conf \
      /etc/nginx/stream-enabled/20-eurodns-server.conf \
      /etc/nginx/eurodns-stream.conf
rm -rf /etc/nginx/smartdns
# strip the managed include line we inserted into nginx.conf (and our blank line)
if [ -f /etc/nginx/nginx.conf ] && grep -q 'eurodns-stream.conf' /etc/nginx/nginx.conf; then
  run "sed -i '/include .*eurodns-stream.conf/d' /etc/nginx/nginx.conf"
fi
# restore the stock default site if we had removed it and the file still exists
if [ ! -e /etc/nginx/sites-enabled/default ] && [ -e /etc/nginx/sites-available/default ]; then
  ln -sf ../sites-available/default /etc/nginx/sites-enabled/default
  info "re-enabled the default nginx site"
fi
if command -v nginx >/dev/null 2>&1; then
  nginx -t 2>/dev/null && (systemctl reload nginx 2>/dev/null || true) || warn_ignored=1
fi

# ---------------------------------------------------------------------------
log "Removing dnsmasq geo config"
rm -f /etc/dnsmasq.d/10-smartdns-domains.conf
# deploy.sh replaced /etc/dnsmasq.conf with ours; drop it so the package default applies
rm -f /etc/dnsmasq.conf
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files dnsmasq.service >/dev/null 2>&1; then
  run "systemctl try-restart dnsmasq || true"
fi

# ---------------------------------------------------------------------------
log "Removing stunnel (DoT) configuration"
rm -f /etc/stunnel/eurodns-dot.conf /etc/stunnel/dot.conf /etc/stunnel/smartdns-dot.conf

# ---------------------------------------------------------------------------
log "Removing certbot deploy hook"
rm -f /etc/letsencrypt/renewal-hooks/deploy/00-eurodns.sh

# ---------------------------------------------------------------------------
if [ "$KEEP_CERT" -eq 0 ] && [ -n "$MGMT_HOST" ] && command -v certbot >/dev/null 2>&1; then
  log "Deleting Let's Encrypt certificate for $MGMT_HOST"
  certbot delete --cert-name "$MGMT_HOST" --non-interactive 2>/dev/null || info "no cert named $MGMT_HOST to delete (skip)"
elif [ "$KEEP_CERT" -eq 1 ]; then
  info "keeping certificate (--keep-cert)"
fi

# ---------------------------------------------------------------------------
if [ "$KEEP_DATA" -eq 0 ]; then
  log "Removing website + state directories"
  rm -rf /var/www/smartdns /opt/smartdns
else
  info "keeping /opt/smartdns and /var/www/smartdns (--keep-data)"
fi

# ---------------------------------------------------------------------------
if [ "$PURGE_PKGS" -eq 1 ] && command -v apt-get >/dev/null 2>&1; then
  log "Purging packages"
  apt-get purge -y nginx nginx-common libnginx-mod-stream dnsmasq stunnel4 stunnel certbot 2>/dev/null || true
  apt-get autoremove -y --purge 2>/dev/null || true
fi

log "EuroDNS uninstalled."
cat <<EOM
  Left untouched (by design, unless you passed flags):
    - apt packages (system may share them)   -> use --purge-packages to remove
    - nginx/dnsmasq/stunnel programs         -> re-enable them if you disabled other services
  Verify nothing EuroDNS-owned remains:
    systemctl status eurodns-resolver smartdns-backend eurodns-dot
    ls /etc/nginx/stream-enabled /etc/dnsmasq.d /opt/smartdns /var/www/smartdns
EOM
