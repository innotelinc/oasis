#!/usr/bin/env bash
set -euo pipefail

# Simple verification that Zimbra installation completed successfully
# Checks:
# 1. zmcontrol is available (Zimbra install)
# 2. All required services are running
# 3. Ports 993, 995, 10000, 10443 are listening

info() { echo -e "[\e[1;34mVERIF\e[0m] $*"; }

# 1. Check zmcontrol exists
if ! command -v zmcontrol >/dev/null 2>&1; then
    info "Zimbra control command not found — installation may have failed"
    exit 1
fi
info "Zimbra control command is available"

# 2. Check service status
if zmcontrol status | grep -q "running"; then
    info "Zimbra services are running"
else
    info "Zimbra services are NOT running"
    exit 1
fi

# 3. Check ports
required_ports=("993" "995" "10000" "10443")
for port in "${required_ports[@]}"; do
    if ss -ltn | grep -q ":${port} "; then
        info "Port ${port} is listener"
    else
        info "Port ${port} is NOT listening"
        exit 1
    fi
done

info "All verification checks passed"
exit 0