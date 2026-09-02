#!/usr/bin/env bash
set -euo pipefail

# Issue the first certificate for the production Oasis proxy. Renewal is handled
# by the certbot Compose service; this script is only needed once per domain.

: "${ACME_EMAIL:?Set ACME_EMAIL in .env}"
: "${AUTH_DOMAIN:?Set AUTH_DOMAIN in .env}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this helper as root or grant Docker access to the current user." >&2
  exit 1
fi

if ! docker compose ps --status running reverse-proxy >/dev/null 2>&1; then
  echo "Start the production proxy first: docker compose --profile production up -d" >&2
  exit 1
fi

if docker compose run --rm certbot certificates 2>/dev/null | grep -q "Domains:.*${AUTH_DOMAIN}"; then
  echo "A certificate for ${AUTH_DOMAIN} already exists."
  exit 0
fi

docker compose run --rm certbot certonly \
  --webroot --webroot-path /var/www/certbot \
  --email "${ACME_EMAIL}" \
  --agree-tos \
  --no-eff-email \
  -d "${AUTH_DOMAIN}"

docker compose restart reverse-proxy
echo "Certificate issued and reverse proxy restarted."
