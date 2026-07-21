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
IMAGE_BACKEND="ghcr.io/letsrevel/revel:latest"
IMAGE_FRONTEND="ghcr.io/letsrevel/revel-frontend:latest"

# image_id <image_ref>
# Prints the local image ID (sha256:...) for a tag, or nothing if the image is
# not present locally. Never fails the caller — a missing image is a valid
# "before" state (first-ever pull), reported as an empty string.
image_id() {
    docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true
}
