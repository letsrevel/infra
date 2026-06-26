# shellcheck shell=bash
# Discord deploy-notification helpers, sourced by deploy.sh and deploy-rollout.sh.
#
# Design rules:
#   - A deploy must NEVER fail because Discord is unreachable or unconfigured.
#     Every function here is best-effort and returns 0.
#   - If the relevant webhook URL is unset, the notification is a silent no-op.
#   - Backend and Frontend post to separate webhooks
#     (DISCORD_BACKEND_WEBHOOK_URL / DISCORD_FRONTEND_WEBHOOK_URL) so they can
#     land in different channels.

# Load DISCORD_*_WEBHOOK_URL from .env into the environment if not already set.
# Parsed line-by-line (not sourced) so an odd value can't break the caller —
# mirrors the parser in deploy.sh. Safe to call more than once.
_load_discord_env() {
    local env_file="${1:-.env}"
    [ -f "$env_file" ] || return 0
    local line key value
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
        key="${line%%=*}"
        [[ "$key" =~ ^DISCORD_[A-Z_]*WEBHOOK_URL$ ]] || continue
        value="${line#*=}"
        value="${value#\"}" && value="${value%\"}"
        value="${value#\'}" && value="${value%\'}"
        export "$key=$value"
    done < "$env_file"
}

# Minimal JSON string escaping (backslash + double-quote) for the payload.
_json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

# notify_deploy <webhook_url> <emoji> <component> <version> [color]
# Posts a small embed like "🚀 Backend v1.66.0 is live!". No-op if no webhook.
notify_deploy() {
    local webhook_url="$1" emoji="$2" component="$3" version="$4" color="${5:-5763719}"
    [ -z "$webhook_url" ] && return 0

    local title
    if [ -n "$version" ]; then
        title="$emoji $component $version is live!"
    else
        title="$emoji $component is live!"
    fi

    local payload
    payload=$(printf '{"embeds":[{"title":"%s","color":%s}]}' "$(_json_escape "$title")" "$color")

    if curl -fsS -m 10 -X POST "$webhook_url" \
            -H "Content-Type: application/json" \
            -d "$payload" >/dev/null 2>&1; then
        echo "  → Discord: notified ($title)"
    else
        echo "  → Discord: notification failed (non-fatal)"
    fi
    return 0
}

# Read the LIVE version from the just-deployed backend container. The package is
# named "revel" (settings.VERSION = importlib.metadata.version("revel")).
# Echoes "vX.Y.Z", or nothing if the container can't be queried.
get_backend_version() {
    local v
    v=$(docker compose exec -T web python -c \
        "from importlib.metadata import version; print(version('revel'))" 2>/dev/null \
        | tr -d '[:space:]') || return 0
    [ -z "$v" ] && return 0
    printf 'v%s' "${v#v}"
}

# Read the LIVE frontend version. Prefer PUBLIC_VERSION (the git tag baked at
# build time and shown to users); fall back to package.json. Echoes "vX.Y.Z".
get_frontend_version() {
    local v
    v=$(docker compose exec -T frontend sh -c 'printf "%s" "$PUBLIC_VERSION"' 2>/dev/null \
        | tr -d '[:space:]')
    if [ -z "$v" ]; then
        v=$(docker compose exec -T frontend node -e \
            'process.stdout.write(require("/app/package.json").version)' 2>/dev/null \
            | tr -d '[:space:]')
    fi
    [ -z "$v" ] && return 0
    printf 'v%s' "${v#v}"
}
