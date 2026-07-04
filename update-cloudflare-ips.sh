#!/usr/bin/env bash
#
# Fetches latest Cloudflare IP ranges and updates cloudflare_ips.conf.
# Reloads Caddy if it is currently running.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="$SCRIPT_DIR/cloudflare_ips.conf"
TEMP_FILE=$(mktemp)

# Cleanup on exit
trap 'rm -f "$TEMP_FILE"' EXIT

echo "Fetching Cloudflare IP ranges..."

# Fetch IPv4 and IPv6 lists
if ! IPS_V4=$(curl -fsSL --connect-timeout 10 --max-time 20 https://www.cloudflare.com/ips-v4); then
    echo "Error: Failed to fetch Cloudflare IPv4 ranges." >&2
    exit 1
fi

if ! IPS_V6=$(curl -fsSL --connect-timeout 10 --max-time 20 https://www.cloudflare.com/ips-v6); then
    echo "Error: Failed to fetch Cloudflare IPv6 ranges." >&2
    exit 1
fi

# Combine and clean up (remove empty lines)
ALL_IPS=$(echo -e "${IPS_V4}\n${IPS_V6}" | grep -E '^[0-9a-fA-F.:/]+$')

if [ -z "$ALL_IPS" ]; then
    echo "Error: Fetched IP list is empty or invalid." >&2
    exit 1
fi

# Write to temp file
echo "$ALL_IPS" > "$TEMP_FILE"

# Compare and update if changed
if [ -f "$CONF_FILE" ] && cmp -s "$CONF_FILE" "$TEMP_FILE"; then
    echo "Cloudflare IP ranges are already up-to-date."
else
    echo "Updating Cloudflare IP ranges in $CONF_FILE..."
    cp "$TEMP_FILE" "$CONF_FILE"
    chmod 644 "$CONF_FILE"

    # Reload Caddy if the stack is running
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        # Check if Caddy container is running
        if docker compose ps caddy 2>/dev/null | grep -E "running|Up" >/dev/null 2>&1; then
            echo "Reloading Caddy..."
            if ! docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
                echo "Warning: Caddy reload failed. Please check your Caddy config." >&2
            fi
        fi
    fi
fi
