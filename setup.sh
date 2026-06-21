#!/usr/bin/env bash
#
# Revel self-hosting setup wizard.
#
#   git clone https://github.com/letsrevel/infra && cd infra
#   ./setup.sh
#
# Takes a fresh VPS to a running Revel instance: generates .env, selects a
# Caddyfile variant, fetches the geo datasets, and brings the stack up.
# Re-runnable — backs up an existing .env before overwriting.
set -euo pipefail

cd "$(dirname "$0")"

ENV_FILE=".env"
GEO_BASE_URL_DEFAULT="https://api.letsrevel.io/geo-data"

say()  { printf '\n\033[1;35m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$1"; }
ask() {
	local prompt="$1" default="${2:-}" answer
	read -rp "$prompt${default:+ [$default]}: " answer
	printf '%s' "${answer:-$default}"
}
ask_secret() {
	local prompt="$1" answer
	read -rsp "$prompt: " answer
	printf '\n' >&2
	printf '%s' "$answer"
}
yesno() {
	local answer
	answer="$(ask "$1 (y/n)" "${2:-n}")"
	[ "$answer" = "y" ] || [ "$answer" = "Y" ]
}
gen() { openssl rand -hex 32; }

# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
say "Preflight checks"
if ! command -v docker >/dev/null 2>&1; then
	warn "Docker not found."
	if yesno "Install Docker now (via get.docker.com)?" y; then
		curl -fsSL https://get.docker.com | sh
	else
		echo "Docker is required. Aborting."
		exit 1
	fi
fi
if ! docker compose version >/dev/null 2>&1; then
	echo "The Docker Compose v2 plugin is required (docker compose ...). Aborting."
	exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
	echo "openssl is required to generate secrets. Aborting."
	exit 1
fi
for port in 80 443; do
	if command -v ss >/dev/null 2>&1 && ss -ltn "( sport = :$port )" 2>/dev/null | grep -q LISTEN; then
		warn "Port $port is already in use."
		yesno "Continue anyway?" n || exit 1
	fi
done

if [ -f "$ENV_FILE" ]; then
	backup="${ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
	cp "$ENV_FILE" "$backup"
	say "Backed up existing $ENV_FILE -> $backup"
fi

# ---------------------------------------------------------------------------
# 2. Tier
# ---------------------------------------------------------------------------
say "Choose a tier"
echo "  slim = core only (~2 vCPU / 4 GB)."
echo "  full = everything incl. observability + antivirus (8 vCPU / 32 GB)."
tier="$(ask "Tier (slim/full)" slim)"

# ---------------------------------------------------------------------------
# 3. Domains
# ---------------------------------------------------------------------------
say "Domains"
frontend_domain="$(ask "Frontend domain" "example.com")"
api_domain="$(ask "API domain" "api.${frontend_domain}")"
docs_domain=""
grafana_domain=""
if [ "$tier" = "full" ]; then
	docs_domain="$(ask "Docs domain" "docs.${frontend_domain}")"
	grafana_domain="$(ask "Grafana domain" "grafana.${frontend_domain}")"
fi

# ---------------------------------------------------------------------------
# 4. Email
# ---------------------------------------------------------------------------
say "Email"
email_dry_run="True"
email_host=""
email_port="587"
email_user=""
email_password=""
default_from="Revel <noreply@${frontend_domain}>"
if yesno "Configure real SMTP now? (no = console/dry-run for testing)" n; then
	email_dry_run="False"
	email_host="$(ask "SMTP host" "smtp.example.com")"
	email_port="$(ask "SMTP port" 587)"
	email_user="$(ask "SMTP username")"
	email_password="$(ask_secret "SMTP password")"
	default_from="$(ask "From address" "Revel <noreply@${frontend_domain}>")"
fi

# ---------------------------------------------------------------------------
# 5. Optional integrations (default: off)
# ---------------------------------------------------------------------------
say "Optional integrations (skip any you don't need)"

feature_telegram="False"
telegram_token=""
if yesno "Enable Telegram bot?" n; then
	feature_telegram="True"
	telegram_token="$(ask "Telegram bot token")"
fi

feature_llm="False"
llm_api_key=""
if yesno "Enable LLM questionnaire evaluation?" n; then
	feature_llm="True"
	llm_api_key="$(ask_secret "LLM API key")"
fi

feature_malware="False"
if [ "$tier" = "full" ] && yesno "Enable ClamAV malware scanning?" y; then
	feature_malware="True"
fi

feature_org_creation="True"
if yesno "Single-org instance? (disables public organization creation)" y; then
	feature_org_creation="False"
fi

feature_stripe="no"
stripe_secret=""
stripe_publishable=""
stripe_account=""
default_currency="eur"
if yesno "Enable Stripe payments? (online ticket sales — advanced)" n; then
	feature_stripe="yes"
	stripe_secret="$(ask_secret "Stripe secret key (sk_...)")"
	stripe_publishable="$(ask "Stripe publishable key (pk_...)")"
	stripe_account="$(ask "Platform Stripe account id (acct_...)")"
	default_currency="$(ask "Default currency" eur)"
	if ! command -v jq >/dev/null 2>&1; then
		warn "jq not found — needed to capture Stripe webhook secrets automatically."
		warn "Install jq, or set STRIPE_WEBHOOK_SECRETS manually after setup."
	fi
fi

# ---------------------------------------------------------------------------
# 6. Reverse proxy variant
# ---------------------------------------------------------------------------
say "Reverse proxy"
if yesno "Are you behind Cloudflare (orange cloud)?" n; then
	caddyfile_path="./Caddyfile.cloudflare"
	behind_cloudflare="yes"
else
	caddyfile_path="./Caddyfile.generic"
	behind_cloudflare="no"
fi

# ---------------------------------------------------------------------------
# 7. Secrets
# ---------------------------------------------------------------------------
say "Generating secrets"
secret_key="$(gen)"
salt_key="$(gen)"
db_password="$(gen)"
grafana_password="$(gen)"

# Compose profiles per tier
profiles=""
if [ "$tier" = "full" ]; then
	profiles="observability,antivirus"
fi
[ "$feature_telegram" = "True" ] && profiles="${profiles:+$profiles,}telegram"

# ---------------------------------------------------------------------------
# 8. Write .env
# ---------------------------------------------------------------------------
say "Writing $ENV_FILE"
{
	echo "# Generated by setup.sh — re-run the wizard to regenerate."
	echo "COMPOSE_PROFILES=${profiles}"
	echo "CADDYFILE_PATH=${caddyfile_path}"
	echo ""
	echo "SECRET_KEY=${secret_key}"
	echo "SALT_KEY=${salt_key}"
	echo "DEBUG=False"
	echo ""
	echo "DB_NAME=revel"
	echo "DB_USER=revel"
	echo "DB_PASSWORD=${db_password}"
	echo "DB_HOST=pgbouncer"
	echo "DB_PORT=6432"
	echo "DB_USE_PGBOUNCER=True"
	echo ""
	echo "REDIS_HOST=redis"
	echo "REDIS_PORT=6379"
	echo ""
	echo "ALLOWED_HOSTS=${frontend_domain},${api_domain}"
	echo "SITE_DOMAIN=${api_domain}"
	echo "BASE_URL=https://${api_domain}"
	echo "FRONTEND_BASE_URL=https://${frontend_domain}"
	echo "CORS_ALLOWED_ORIGINS=https://${frontend_domain}"
	echo ""
	echo "FRONTEND_DOMAIN=${frontend_domain}"
	echo "API_DOMAIN=${api_domain}"
	echo "DOCS_DOMAIN=${docs_domain:-docs.${frontend_domain}}"
	echo "GRAFANA_DOMAIN=${grafana_domain:-grafana.${frontend_domain}}"
	echo ""
	echo "FEATURE_MALWARE_SCAN=${feature_malware}"
	echo "FEATURE_TELEGRAM=${feature_telegram}"
	echo "FEATURE_LLM_EVALUATION=${feature_llm}"
	echo "FEATURE_ORGANIZATION_CREATION=${feature_org_creation}"
	echo ""
	echo "EMAIL_DRY_RUN=${email_dry_run}"
	echo "EMAIL_HOST=${email_host}"
	echo "EMAIL_PORT=${email_port}"
	echo "EMAIL_HOST_USER=${email_user}"
	echo "EMAIL_HOST_PASSWORD=${email_password}"
	echo "EMAIL_USE_TLS=True"
	echo "DEFAULT_FROM_EMAIL=\"${default_from}\""
	echo ""
	echo "TELEGRAM_BOT_TOKEN=${telegram_token}"
	echo "LLM_API_KEY=${llm_api_key}"
	echo ""
	echo "STRIPE_SECRET_KEY=${stripe_secret}"
	echo "STRIPE_PUBLISHABLE_KEY=${stripe_publishable}"
	echo "STRIPE_ACCOUNT=${stripe_account}"
	echo "DEFAULT_CURRENCY=${default_currency}"
	echo ""
	echo "GRAFANA_ADMIN_USER=admin"
	echo "GRAFANA_ADMIN_PASSWORD=${grafana_password}"
	if [ "$tier" = "slim" ]; then
		echo ""
		echo "# Slim tuning"
		echo "PG_SHARED_BUFFERS=256MB"
		echo "PG_EFFECTIVE_CACHE_SIZE=1GB"
		echo "PG_MAINTENANCE_WORK_MEM=128MB"
		echo "PG_WORK_MEM=8MB"
		echo "PG_MAX_CONNECTIONS=50"
		echo "WEB_MEM_LIMIT=1500m"
		echo "CELERY_MEM_LIMIT=768m"
		echo "FRONTEND_MEM_LIMIT=512m"
		echo "REDIS_MEM_LIMIT=256m"
		echo "GUNICORN_WORKERS=2"
		echo "GUNICORN_THREADS=2"
		echo "CELERY_CONCURRENCY=2"
	fi
} >"$ENV_FILE"

# ---------------------------------------------------------------------------
# 9. Fetch geo data (BEFORE bring-up — web auto-migrates and loads cities)
# ---------------------------------------------------------------------------
say "Geo data"
mkdir -p geo-data
geo_base_url="$(ask "Geo-data base URL" "$GEO_BASE_URL_DEFAULT")"
echo "Downloading worldcities.csv (full city list)..."
if ! curl -fsSL "${geo_base_url}/worldcities.csv" -o geo-data/worldcities.csv; then
	warn "Full city list unavailable; the app falls back to the bundled 50-city mini list."
fi
if yesno "Download the IP2Location LITE GeoIP database?" n; then
	if ! curl -fsSL "${geo_base_url}/IP2LOCATION-LITE-DB5.BIN" -o geo-data/IP2LOCATION-LITE-DB5.BIN; then
		warn "GeoIP database unavailable; IP geolocation lookups return null."
	fi
fi
echo "Geo datasets are CC-BY / CC-BY-SA — keep geo-data/NOTICE if you redistribute them."

# ---------------------------------------------------------------------------
# 10. Cloudflare caveat
# ---------------------------------------------------------------------------
if [ "$behind_cloudflare" = "yes" ]; then
	say "Cloudflare caveat"
	cat <<'EOF'
Set your DNS records to "DNS only" (GREY cloud) first, so Caddy can obtain TLS
certificates from Let's Encrypt. Once the stack is up and certs are issued,
switch the records back to "Proxied" (ORANGE cloud).
EOF
	read -rp "Press Enter once your records are GREY-clouded to continue..."
fi

# ---------------------------------------------------------------------------
# 11. Bring up
# ---------------------------------------------------------------------------
say "Starting the stack"
docker compose up -d

say "Waiting for the web service to become healthy..."
for _ in $(seq 1 30); do
	if docker compose ps web 2>/dev/null | grep -q "healthy"; then
		break
	fi
	sleep 5
done

# ---------------------------------------------------------------------------
# 12. Stripe webhook provisioning (optional)
# ---------------------------------------------------------------------------
if [ "$feature_stripe" = "yes" ] && command -v jq >/dev/null 2>&1; then
	say "Provisioning Stripe webhooks"
	if secrets_json="$(docker compose exec -T web python manage.py provision_stripe_webhooks \
		--url "https://${api_domain}/api/stripe/webhook" --format json --force)"; then
		plat="$(printf '%s' "$secrets_json" | jq -r '.platform.secret')"
		conn="$(printf '%s' "$secrets_json" | jq -r '.connect.secret')"
		if [ -n "$plat" ] && [ -n "$conn" ] && [ "$plat" != "null" ] && [ "$conn" != "null" ]; then
			printf 'STRIPE_WEBHOOK_SECRETS=%s,%s\n' "$plat" "$conn" >>"$ENV_FILE"
			docker compose up -d web celery_default beat
			say "Stripe webhooks provisioned; secrets written to ${ENV_FILE}."
		else
			warn "Could not parse Stripe secrets — set STRIPE_WEBHOOK_SECRETS manually."
		fi
	else
		warn "Stripe webhook provisioning failed — run provision_stripe_webhooks manually."
	fi
fi

# ---------------------------------------------------------------------------
# 13. Next steps
# ---------------------------------------------------------------------------
say "Done. Next steps:"
echo "  - Create an admin user:  docker compose exec web python manage.py createsuperuser"
echo "  - Frontend:              https://${frontend_domain}"
echo "  - API:                   https://${api_domain}"
if [ "$tier" = "full" ]; then
	echo "  - Grafana:               https://${grafana_domain:-grafana.${frontend_domain}} (admin / see ${ENV_FILE})"
fi
if [ "$behind_cloudflare" = "yes" ]; then
	echo "  - Re-enable the Cloudflare proxy (ORANGE cloud) now that certs are issued."
fi
echo ""
echo "Full guide: https://docs.letsrevel.io/self-hosting"
