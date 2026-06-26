#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

cd "$(dirname "$0")"

# Discord deploy-notification helpers (best-effort, never fail the deploy).
# shellcheck source=notify-discord.sh
source "./notify-discord.sh"

# Install docker-rollout if not present
install_rollout() {
    if [ -f ~/.docker/cli-plugins/docker-rollout ]; then
        echo -e "${GREEN}docker-rollout already installed${NC}"
    else
        echo -e "${YELLOW}Installing docker-rollout...${NC}"
        mkdir -p ~/.docker/cli-plugins
        curl -fsSL https://raw.githubusercontent.com/wowu/docker-rollout/master/docker-rollout \
            -o ~/.docker/cli-plugins/docker-rollout
        chmod +x ~/.docker/cli-plugins/docker-rollout
        echo -e "${GREEN}docker-rollout installed${NC}"
    fi
}

# One-time migration (causes brief downtime)
# Run this ONCE after updating config files
migrate() {
    echo -e "${YELLOW}=== ONE-TIME MIGRATION ===${NC}"
    echo -e "${RED}WARNING: This will cause brief downtime!${NC}"
    read -p "Continue? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi

    echo -e "${YELLOW}Stopping services...${NC}"
    docker compose down web frontend celery_default

    echo -e "${YELLOW}Starting services with new config...${NC}"
    docker compose up -d

    echo -e "${YELLOW}Restarting Alloy to pick up new config...${NC}"
    docker compose restart alloy

    echo -e "${YELLOW}Reloading Caddy config...${NC}"
    docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile

    echo -e "${GREEN}Migration complete!${NC}"
    echo -e "Future deploys will be zero-downtime using: ${YELLOW}$0 deploy${NC}"
}

# Zero-downtime deployment
deploy() {
    echo -e "${YELLOW}=== ZERO-DOWNTIME DEPLOY ===${NC}"

    echo -e "${YELLOW}Pulling latest images...${NC}"
    docker compose pull web frontend celery_default beat telegram

    echo -e "${YELLOW}Rolling out web...${NC}"
    docker rollout -t 120 web

    echo -e "${YELLOW}Rolling out frontend...${NC}"
    docker rollout -t 120 frontend

    echo -e "${YELLOW}Rolling out celery_default...${NC}"
    docker rollout -t 120 celery_default

    echo -e "${YELLOW}Restarting beat (cannot run two instances)...${NC}"
    docker compose up -d --force-recreate beat

    echo -e "${YELLOW}Restarting telegram (cannot run two instances)...${NC}"
    docker compose up -d --force-recreate telegram

    echo -e "${GREEN}Deploy complete!${NC}"

    # Announce the live versions on Discord (no-op without webhook URLs). This
    # script doesn't load .env, so pull the webhook vars from it explicitly.
    _load_discord_env
    notify_deploy "${DISCORD_BACKEND_WEBHOOK_URL:-}" "🚀" "Backend" "$(get_backend_version)" 5763719
    notify_deploy "${DISCORD_FRONTEND_WEBHOOK_URL:-}" "🎨" "Frontend" "$(get_frontend_version)" 5793266
}

# Show usage
usage() {
    echo "Usage: $0 {install|migrate|deploy}"
    echo ""
    echo "Commands:"
    echo "  install  - Install docker-rollout plugin"
    echo "  migrate  - One-time migration (causes downtime, run once after config update)"
    echo "  deploy   - Zero-downtime deployment (use for all future deploys)"
    echo ""
    echo "First time setup:"
    echo "  1. $0 install"
    echo "  2. $0 migrate"
    echo ""
    echo "Future deploys:"
    echo "  $0 deploy"
}

case "${1:-}" in
    install)
        install_rollout
        ;;
    migrate)
        install_rollout
        migrate
        ;;
    deploy)
        deploy
        ;;
    *)
        usage
        exit 1
        ;;
esac
