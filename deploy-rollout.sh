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
# Image-change detection (IMAGE_BACKEND/IMAGE_FRONTEND, image_id).
# shellcheck source=image-diff.sh
source "./image-diff.sh"

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

    # Snapshot image IDs, pull, snapshot again: only roll out a component whose
    # image actually moved. `docker rollout` / `--force-recreate` recreate
    # containers unconditionally, so gating here is what saves the needless churn.
    local old_backend old_frontend new_backend new_frontend
    old_backend=$(image_id "$IMAGE_BACKEND")
    old_frontend=$(image_id "$IMAGE_FRONTEND")

    echo -e "${YELLOW}Pulling latest images...${NC}"
    docker compose pull web frontend celery_default beat telegram

    new_backend=$(image_id "$IMAGE_BACKEND")
    new_frontend=$(image_id "$IMAGE_FRONTEND")

    local backend_changed=0 frontend_changed=0
    [ "$old_backend" != "$new_backend" ] && backend_changed=1
    [ "$old_frontend" != "$new_frontend" ] && frontend_changed=1

    if [ "$backend_changed" -eq 0 ] && [ "$frontend_changed" -eq 0 ]; then
        echo -e "${GREEN}No new images — nothing to deploy.${NC}"
        return 0
    fi

    # Backend image backs web, celery_default, beat and telegram — gate them all
    # on the single backend-image check.
    if [ "$backend_changed" -eq 1 ]; then
        echo -e "${YELLOW}Rolling out web...${NC}"
        docker rollout -t 120 web

        echo -e "${YELLOW}Rolling out celery_default...${NC}"
        docker rollout -t 120 celery_default

        echo -e "${YELLOW}Restarting beat (cannot run two instances)...${NC}"
        docker compose up -d --force-recreate beat

        echo -e "${YELLOW}Restarting telegram (cannot run two instances)...${NC}"
        docker compose up -d --force-recreate telegram
    else
        echo -e "${GREEN}Backend image unchanged — skipping web/celery_default/beat/telegram.${NC}"
    fi

    if [ "$frontend_changed" -eq 1 ]; then
        echo -e "${YELLOW}Rolling out frontend...${NC}"
        docker rollout -t 120 frontend
    else
        echo -e "${GREEN}Frontend image unchanged — skipping frontend.${NC}"
    fi

    echo -e "${GREEN}Deploy complete!${NC}"

    # Announce the live versions on Discord (no-op without webhook URLs). This
    # script doesn't load .env, so pull the webhook vars from it explicitly.
    # Only announce components that were actually rolled out.
    _load_discord_env
    if [ "$backend_changed" -eq 1 ]; then
        notify_deploy "${DISCORD_BACKEND_WEBHOOK_URL:-}" "🚀" "Backend" "$(get_backend_version)" 5763719
    fi
    if [ "$frontend_changed" -eq 1 ]; then
        notify_deploy "${DISCORD_FRONTEND_WEBHOOK_URL:-}" "🎨" "Frontend" "$(get_frontend_version)" 5793266
    fi
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
