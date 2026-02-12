#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

cd "$(dirname "$0")"

# Defaults
DRAIN_TIMEOUT=600
DRAIN_INTERVAL=5
SKIP_CONFIRM=false

usage() {
    echo "Usage: $0 [options]"
    echo ""
    echo "Gracefully drain Celery tasks, stop all services, and reboot the server."
    echo "Docker services restart automatically on boot (restart: always)."
    echo ""
    echo "Options:"
    echo "  --timeout SEC   Max seconds to wait for tasks to drain (default: 600)"
    echo "  --interval SEC  Seconds between drain polls (default: 5)"
    echo "  -y, --yes       Skip confirmation prompt"
    echo "  --check          Dry-run: only check if safe to reboot, then exit"
    echo "  -h, --help      Show this help"
}

# Parse arguments
CHECK_ONLY=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --timeout)  DRAIN_TIMEOUT="$2"; shift 2 ;;
        --interval) DRAIN_INTERVAL="$2"; shift 2 ;;
        -y|--yes)   SKIP_CONFIRM=true; shift ;;
        --check)    CHECK_ONLY=true; shift ;;
        -h|--help)  usage; exit 0 ;;
        *)          echo -e "${RED}Unknown option: $1${NC}"; usage; exit 1 ;;
    esac
done

# Preflight checks
if [ ! -f .env ]; then
    echo -e "${RED}Error: .env file not found!${NC}"
    exit 1
fi

if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}Error: Docker is not running!${NC}"
    exit 1
fi

# --check mode: just report and exit
if [ "$CHECK_ONLY" = true ]; then
    echo -e "${YELLOW}Checking reboot readiness...${NC}"
    docker compose exec -T celery_default python manage.py reboot_check
    exit $?
fi

echo -e "${RED}╔══════════════════════════════════════╗${NC}"
echo -e "${RED}║       SAFE SERVER REBOOT             ║${NC}"
echo -e "${RED}╠══════════════════════════════════════╣${NC}"
echo -e "${RED}║  This will:                          ║${NC}"
echo -e "${RED}║  1. Stop beat + web (no new tasks)   ║${NC}"
echo -e "${RED}║  2. Wait for workers to drain        ║${NC}"
echo -e "${RED}║  3. Stop all remaining services      ║${NC}"
echo -e "${RED}║  4. Reboot the server                ║${NC}"
echo -e "${RED}║                                      ║${NC}"
echo -e "${RED}║  Services restart automatically      ║${NC}"
echo -e "${RED}║  on boot (restart: always).          ║${NC}"
echo -e "${RED}╚══════════════════════════════════════╝${NC}"
echo ""

if [ "$SKIP_CONFIRM" = false ]; then
    read -p "Continue? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# ── Step 1: Stop beat + web so no new tasks can be enqueued ──
echo ""
echo -e "${YELLOW}[1/4] Stopping beat and web (no new tasks can be enqueued)...${NC}"
docker compose stop beat web
echo -e "${GREEN}✓ Beat and web stopped${NC}"

# ── Step 2: Wait for workers to finish active & queued tasks ──
echo ""
echo -e "${YELLOW}[2/4] Waiting for Celery tasks to drain (timeout: ${DRAIN_TIMEOUT}s)...${NC}"
docker compose exec -T celery_default python manage.py reboot_check --wait --timeout "$DRAIN_TIMEOUT" --interval "$DRAIN_INTERVAL"
echo -e "${GREEN}✓ Workers drained${NC}"

# ── Step 3: Gracefully stop remaining services ──
echo ""
echo -e "${YELLOW}[3/4] Stopping all remaining services...${NC}"
docker compose stop -t 60
echo -e "${GREEN}✓ All services stopped${NC}"

# ── Step 4: Reboot ──
echo ""
echo -e "${YELLOW}[4/4] Rebooting server...${NC}"
echo -e "${GREEN}Services will restart automatically on boot.${NC}"
sudo reboot
