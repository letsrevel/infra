#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Revel Infrastructure Deployment${NC}"
echo "=================================="
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo -e "${RED}Error: .env file not found!${NC}"
    echo "Please create a .env file from .env.example:"
    echo "  cp .env.example .env"
    echo "  # Then edit .env with your configuration"
    exit 1
fi

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}Error: Docker is not running!${NC}"
    exit 1
fi

# Check if Docker Compose is available
if ! command -v docker compose &> /dev/null; then
    echo -e "${RED}Error: Docker Compose is not installed!${NC}"
    exit 1
fi

echo -e "${YELLOW}Checking configuration...${NC}"

# Load environment variables
source .env

# Check critical environment variables
REQUIRED_VARS=("DB_NAME" "DB_USER" "DB_PASSWORD")
MISSING_VARS=()

for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
        MISSING_VARS+=("$var")
    fi
done

if [ ${#MISSING_VARS[@]} -ne 0 ]; then
    echo -e "${RED}Error: Missing required environment variables:${NC}"
    for var in "${MISSING_VARS[@]}"; do
        echo "  - $var"
    done
    exit 1
fi

echo -e "${GREEN}✓ Configuration validated${NC}"
echo ""

# Parse command line arguments
COMMAND=${1:-up}

case $COMMAND in
    up)
        echo -e "${YELLOW}Starting services...${NC}"
        docker compose up -d
        echo ""
        echo -e "${GREEN}✓ Services started${NC}"
        echo ""
        echo "Access your services at:"
        echo "  - Frontend: https://beta.letsrevel.io"
        echo "  - API: https://beta-api.letsrevel.io"
        echo "  - Flower: https://flower.letsrevel.io"
        echo "  - Grafana: https://grafana.letsrevel.io"
        echo ""
        echo "To view logs, run: docker compose logs -f"
        ;;

    down)
        echo -e "${YELLOW}Stopping services...${NC}"
        docker compose down
        echo -e "${GREEN}✓ Services stopped${NC}"
        ;;

    restart)
        echo -e "${YELLOW}Restarting services...${NC}"
        docker compose restart
        echo -e "${GREEN}✓ Services restarted${NC}"
        ;;

    logs)
        SERVICE=${2:-}
        if [ -z "$SERVICE" ]; then
            docker compose logs -f
        else
            docker compose logs -f "$SERVICE"
        fi
        ;;

    ps)
        docker compose ps
        ;;

    pull)
        echo -e "${YELLOW}Pulling latest images...${NC}"
        docker compose pull
        echo -e "${GREEN}✓ Images updated${NC}"
        ;;

    update)
        echo -e "${YELLOW}Updating services...${NC}"
        docker compose pull
        docker compose up -d
        # `docker compose up -d` only recreates a container when its image or
        # compose spec changes — NOT when an edited *mounted config file* changes.
        # So Caddyfile / Alloy / Prometheus config edits would otherwise be ignored.
        # Reload/restart the config-mounted services explicitly:
        echo -e "${YELLOW}Applying config changes (caddy reload, alloy/prometheus restart)...${NC}"
        # Graceful, validated reload — if the new Caddyfile is invalid, Caddy keeps
        # serving the OLD config (no downtime); we finish applying the other
        # configs, then fail the deploy so the problem can't go unnoticed.
        CADDY_RELOAD_FAILED=0
        if ! docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
            CADDY_RELOAD_FAILED=1
            echo -e "${RED}WARNING: caddy reload failed — old config is still serving. Fix the Caddyfile and re-run.${NC}"
        fi
        # These don't serve user traffic, so a restart is fine to pick up config.
        docker compose restart alloy prometheus
        if [ "$CADDY_RELOAD_FAILED" -ne 0 ]; then
            echo -e "${RED}✗ Update incomplete: caddy is still serving the previous config.${NC}"
            exit 1
        fi
        echo -e "${GREEN}✓ Services updated${NC}"
        ;;

    backup)
        echo -e "${YELLOW}Creating database backup...${NC}"
        BACKUP_FILE="backup_$(date +%Y%m%d_%H%M%S).sql"
        docker compose exec -T revel_postgres pg_dump -U "$DB_USER" "$DB_NAME" > "$BACKUP_FILE"
        echo -e "${GREEN}✓ Backup created: $BACKUP_FILE${NC}"
        ;;

    *)
        echo "Usage: $0 {up|down|restart|logs|ps|pull|update|backup}"
        echo ""
        echo "Commands:"
        echo "  up       - Start all services"
        echo "  down     - Stop all services"
        echo "  restart  - Restart all services"
        echo "  logs     - View logs (optionally specify service name)"
        echo "  ps       - List running services"
        echo "  pull     - Pull latest Docker images"
        echo "  update   - Pull and restart services"
        echo "  backup   - Create database backup"
        exit 1
        ;;
esac
