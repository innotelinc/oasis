#!/bin/sh
# Oasis — renew Stalwart's TLS certificate and DNS records through Cerulean.
#
# Stalwart no longer talks to Technitium or to an ACME CA itself (its domains
# are dnsManagement/certificateManagement Manual — see stalwart_cerulean_only.py).
# Cerulean owns the wildcard and the zone; this fetches the current certificate
# material and republishes the generated DKIM/autoconfig records. Both steps are
# idempotent, so running this too often is harmless and a run that changes
# nothing (added=0, "already up to date") is the normal case.
set -eu
DIR="$(dirname "$0")"
python3 "$DIR/stalwart_install_cert.py" --apply
python3 "$DIR/cerulean_publish_zone.py" --all-domains --apply
