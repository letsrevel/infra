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

# component_behind <image_ref> <service...>
# Succeeds when any container (running or stopped) of these services runs an image ID
# that differs from the local tag's image, or was created from another image ref (a new
# REVEL_TAG with the same image ID). Used so a `deploy` that was cut short, or a `pull`
# done by hand, is still finished by the next `deploy`, and so a container never keeps
# an old ref that a later `docker compose up -d` would recreate without a rollout.
component_behind() {
    local ref=$1 cid ids want svc
    shift
    want=$(image_id "$ref")
    [ -n "$want" ] || return 1
    for svc in "$@"; do
        # A failed `ps` must stop the deploy: an empty list would read as "not behind".
        ids=$(docker compose ps -a -q "$svc") || { echo -e "${RED}docker compose ps failed for $svc${NC}" >&2; exit 1; }
        for cid in $ids; do
            [ "$(docker inspect --format '{{.Image}}' "$cid")" != "$want" ] && return 0
            [ "$(docker inspect --format '{{.Config.Image}}' "$cid")" != "$ref" ] && return 0
        done
    done
    return 1
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

# Zero-downtime deployment. Optional argument: a backend image tag (for example
# 3.2.0, or latest). With a tag, the deploy pulls that tag and, after the pull
# succeeds, writes REVEL_TAG=<tag> to .env. Without a tag, it uses REVEL_TAG from
# .env (default latest), as before.
deploy() {
    local tag="${1:-}"
    echo -e "${YELLOW}=== ZERO-DOWNTIME DEPLOY ===${NC}"
    if [ -n "$tag" ]; then
        use_backend_tag "$tag" || return 1
    fi
    echo -e "${YELLOW}Backend image: ${IMAGE_BACKEND}${NC}"

    # Snapshot image IDs, pull, snapshot again: only roll out a component whose
    # image actually moved. `docker rollout` / `--force-recreate` recreate
    # containers unconditionally, so gating here is what saves the needless churn.
    local old_backend old_frontend new_backend new_frontend
    old_backend=$(image_id "$IMAGE_BACKEND")
    old_frontend=$(image_id "$IMAGE_FRONTEND")

    echo -e "${YELLOW}Pulling images...${NC}"
    docker compose pull web frontend celery_default beat telegram
    # The pull succeeded, so the tag exists: keep it in .env for later compose commands.
    if [ -n "$tag" ]; then
        save_backend_tag "$tag"
        echo -e "${GREEN}Saved REVEL_TAG=${tag} in .env${NC}"
    fi

    new_backend=$(image_id "$IMAGE_BACKEND")
    new_frontend=$(image_id "$IMAGE_FRONTEND")

    local backend_changed=0 frontend_changed=0
    [ "$old_backend" != "$new_backend" ] && backend_changed=1
    [ "$old_frontend" != "$new_frontend" ] && frontend_changed=1
    # A container that still runs an older image than the local tag is also rolled
    # (an earlier deploy or a manual pull moved the tag but did not finish).
    component_behind "$IMAGE_BACKEND" web celery_default beat telegram && backend_changed=1
    component_behind "$IMAGE_FRONTEND" frontend && frontend_changed=1

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
        docker compose up -d --no-deps --force-recreate beat

        echo -e "${YELLOW}Restarting telegram (cannot run two instances)...${NC}"
        docker compose up -d --no-deps --force-recreate telegram
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

# Roll services onto the current .env / compose config WITHOUT pulling images or
# migrating, so an env-only change (e.g. a secret rotation) never ships new code.
# Same per-service sequence as `deploy`. With no arguments it rolls the app services;
# services outside the active compose profiles are skipped. Refuses a service whose running container uses an
# older image than the local tag. Never `up -d` the whole stack.
roll() {
    local order=(web celery_default beat telegram frontend) requested=("$@") svcs=() svc active
    [ "${#requested[@]}" -eq 0 ] && requested=("${order[@]}")

    # Known app services first, in deploy order, then anything else as given.
    for svc in "${order[@]}"; do
        case " ${requested[*]} " in *" $svc "*) svcs+=("$svc") ;; esac
    done
    for svc in "${requested[@]}"; do
        case " ${order[*]} " in *" $svc "*) ;; *) svcs+=("$svc") ;; esac
    done

    # A name that is not defined at all is a typo: fail before rolling anything.
    # One that is defined but sits in an inactive profile is skipped with a note.
    local defined bad=0
    defined=$(docker compose --profile '*' config --services)
    for svc in "${svcs[@]}"; do
        if ! grep -Fqx -- "$svc" <<<"$defined"; then
            echo -e "${RED}Unknown service: $svc (not defined in docker-compose.yml).${NC}" >&2
            bad=1
        fi
    done
    [ "$bad" -eq 0 ] || return 1

    active=$(docker compose config --services)

    # A local tag that moved since the containers were created (a `deploy` that pulled and then stopped before
    # its migrate/roll) would ship new code without its migrations: refuse and let `deploy` finish first.
    local cid cids running ref stale=0
    for svc in "${svcs[@]}"; do
        grep -Fqx -- "$svc" <<<"$active" || continue
        cids=$(docker compose ps -a -q "$svc") || { echo -e "${RED}docker compose ps failed for $svc${NC}" >&2; return 1; }
        # docker-rollout replaces the stopped replica of a mixed set and leaves the running one on its old
        # environment, so a rotation would look rolled while a replica still holds the old values.
        case "$svc" in
            web | celery_default | frontend)
                running=$(docker compose ps -q "$svc") || { echo -e "${RED}docker compose ps failed for $svc${NC}" >&2; return 1; }
                if [ -n "$running" ] && [ "$(wc -w <<<"$running")" -ne "$(wc -w <<<"$cids")" ]; then
                    echo -e "${RED}$svc has running and stopped replicas: roll would leave a running replica on the old .env. Remove the stopped container ('docker compose ps -a', then 'docker rm <id>') and re-run roll.${NC}" >&2
                    stale=1
                fi
                ;;
        esac
        for cid in $cids; do
            ref=$(docker inspect --format '{{.Config.Image}}' "$cid") || {
                echo -e "${RED}$svc: container changed during the check; re-run roll.${NC}" >&2
                return 1
            }
            # Only the images with migrations (backend, frontend) are checked; canary,
            # alertmanager, grafana and the like are never managed by `deploy`.
            case "$ref" in "$IMAGE_BACKEND" | "$IMAGE_FRONTEND") ;; *) continue ;; esac
            if [ -n "$(image_id "$ref")" ] && [ "$(docker inspect --format '{{.Image}}' "$cid")" != "$(image_id "$ref")" ]; then
                echo -e "${RED}$svc runs an older image than the local $ref: run '$0 deploy' (it applies the migrations through the web rollout and rolls the rest; roll never changes images).${NC}" >&2
                stale=1
            fi
        done
    done
    [ "$stale" -eq 0 ] || return 1

    for svc in "${svcs[@]}"; do
        if ! grep -Fqx -- "$svc" <<<"$active"; then
            echo -e "${YELLOW}Skipping $svc (defined, but not in the active compose profiles).${NC}"
            continue
        fi
        case "$svc" in
            web | celery_default | frontend)
                echo -e "${YELLOW}Rolling out $svc...${NC}"
                docker rollout -t 120 "$svc"
                ;;
            *)
                echo -e "${YELLOW}Recreating $svc...${NC}"
                docker compose up -d --no-deps --force-recreate "$svc"
                ;;
        esac
    done
    echo -e "${GREEN}Roll complete!${NC}"
}

# Show usage
usage() {
    echo "Usage: $0 {install|migrate|deploy [tag]|roll [service...]}"
    echo ""
    echo "Commands:"
    echo "  install  - Install docker-rollout plugin"
    echo "  migrate  - One-time migration (causes downtime, run once after config update)"
    echo "  deploy   - Zero-downtime deployment (use for all future deploys)."
    echo "             Optional tag pins the backend image, e.g. '$0 deploy 3.2.0'."
    echo "             The tag is saved in .env; '$0 deploy latest' removes the pin."
    echo "  roll     - Roll services onto the current .env, no pull, no migrate (secret rotation)"
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
        deploy "${2:-}"
        ;;
    roll)
        shift
        roll "$@"
        ;;
    *)
        usage
        exit 1
        ;;
esac
