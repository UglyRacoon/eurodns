#!/bin/bash
# EuroDNS — certbot deploy hook: reload services after a cert renewal.
# Called by certbot with the renewed certificate's details.
set -e
if command -v systemctl >/dev/null 2>&1; then
    systemctl reload-or-restart nginx        || true
    systemctl restart    eurodns-resolver      || true
fi
exit 0
