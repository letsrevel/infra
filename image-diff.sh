# shellcheck shell=bash
# Image-change detection helpers, sourced by deploy.sh and deploy-rollout.sh.
#
# Purpose: let a deploy skip work for a component whose image didn't actually
# change. `docker rollout` and `--force-recreate` recreate containers
# unconditionally, so without this a frontend-only release still churns the
# backend (and vice-versa). Cheap to avoid, so we avoid it.
#
# The two application images each back several compose services:
#   IMAGE_BACKEND  -> web, celery_default, beat, telegram, flower
#   IMAGE_FRONTEND -> frontend
# so a single backend-image check gates every backend service at once.
#
# Tags: REVEL_TAG pins the backend image and REVEL_FRONTEND_TAG the frontend image
# (docker-compose.yml reads the same names, default `latest`). As in Compose, a
# variable set in the shell wins over the one in ./.env.

# _env_file_value <KEY>: the last KEY=value in ./.env, with one pair of quotes and a
# " #" comment removed. Enough for an image tag, which has no quotes or spaces.
_env_file_value() {
    local line v
    [ -f .env ] || return 0
    line=$(grep -E "^[[:space:]]*$1=" .env | tail -n 1) || return 0
    v="${line#*=}"
    v="${v%% #*}"
    v="${v#"${v%%[![:space:]]*}"}"
    v="${v%"${v##*[![:space:]]}"}"
    case "$v" in \"*\" | \'*\') v="${v:1:${#v}-2}" ;; esac
    printf '%s' "$v"
}

# resolve_images: set IMAGE_BACKEND and IMAGE_FRONTEND to the refs Compose uses.
resolve_images() {
    local backend_tag frontend_tag
    backend_tag="${REVEL_TAG-$(_env_file_value REVEL_TAG)}"
    frontend_tag="${REVEL_FRONTEND_TAG-$(_env_file_value REVEL_FRONTEND_TAG)}"
    IMAGE_BACKEND="ghcr.io/letsrevel/revel:${backend_tag:-latest}"
    IMAGE_FRONTEND="ghcr.io/letsrevel/revel-frontend:${frontend_tag:-latest}"
}
resolve_images

# valid_tag <tag>: succeeds for a Docker tag (letters, digits, _ . -; 1-128 chars).
valid_tag() {
    [[ "$1" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]
}

# use_backend_tag <tag>: deploy this backend tag in the current run. It exports
# REVEL_TAG, so every `docker compose` call of this run uses the tag. It does not
# write .env: call save_backend_tag after the pull succeeds.
use_backend_tag() {
    if ! valid_tag "$1"; then
        echo "Invalid image tag: '$1' (use letters, digits, _ . -; for example 3.2.0 or latest)." >&2
        return 1
    fi
    if [ -L .env ]; then
        echo ".env is a symlink: set REVEL_TAG=$1 in the target file, then run the deploy without a tag." >&2
        return 1
    fi
    export REVEL_TAG="$1"
    resolve_images
}

# save_backend_tag <tag>: write REVEL_TAG=<tag> to ./.env, so that later compose
# commands (up -d, roll, rotation) keep the same image. Without this, they would
# read the old value and move the services off the deployed tag. The new file
# keeps the mode and owner of .env and replaces it atomically.
save_backend_tag() {
    local tmp=".env.revel-tag.$$"
    cp -p .env "$tmp"
    awk -v tag="$1" '
        /^[[:space:]]*REVEL_TAG=/ { if (!done) { print "REVEL_TAG=" tag; done = 1 }; next }
        { print }
        END { if (!done) print "REVEL_TAG=" tag }
    ' .env > "$tmp" || { rm -f "$tmp"; return 1; }
    mv "$tmp" .env
}

# image_id <image_ref>
# Prints the local image ID (sha256:...) for a tag, or nothing if the image is
# not present locally. Never fails the caller — a missing image is a valid
# "before" state (first-ever pull), reported as an empty string.
image_id() {
    docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true
}
