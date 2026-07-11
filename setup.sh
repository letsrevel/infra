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

# Host resource detection (Linux). Empty if unavailable (e.g. non-Linux dev box),
# in which case the wizard silently skips the fit checks.
HOST_CPUS=""
HOST_MEM_MB=""
command -v nproc >/dev/null 2>&1 && HOST_CPUS="$(nproc)"
[ -r /proc/meminfo ] && HOST_MEM_MB="$(awk '/^MemTotal:/ {printf "%d", $2/1024}' /proc/meminfo)"

# Convert a docker-style size (e.g. 1500m, 12g, 512M, 2G) to whole MB; 0 on parse failure.
to_mb() {
	local v="$1" n unit
	n="$(printf '%s' "$v" | tr -cd '0-9.')"
	unit="$(printf '%s' "$v" | tr -d '0-9.' | tr 'A-Z' 'a-z')"
	[ -n "$n" ] || { echo 0; return; }
	case "$unit" in
		g|gb) awk "BEGIN{printf \"%d\", $n*1024}" ;;
		m|mb|"") awk "BEGIN{printf \"%d\", $n}" ;;
		*) echo 0 ;;
	esac
}

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
if [ -n "$HOST_CPUS" ] && [ -n "$HOST_MEM_MB" ]; then
	echo "Detected host: ${HOST_CPUS} vCPU, ${HOST_MEM_MB} MB RAM."
fi
echo "  slim = core only (~2 vCPU / 4 GB)."
echo "  full = everything incl. observability + antivirus (8 vCPU / 32 GB)."
# Recommend a tier from the detected hardware (full only when the box is clearly big).
recommended_tier="slim"
if [ -n "$HOST_CPUS" ] && [ -n "$HOST_MEM_MB" ] && [ "$HOST_CPUS" -ge 8 ] && [ "$HOST_MEM_MB" -ge 24000 ]; then
	recommended_tier="full"
fi
tier="$(ask "Tier (slim/full)" "$recommended_tier")"
# Warn when the chosen tier doesn't fit the detected hardware.
if [ -n "$HOST_CPUS" ] && [ -n "$HOST_MEM_MB" ]; then
	if [ "$tier" = "full" ] && { [ "$HOST_CPUS" -lt 8 ] || [ "$HOST_MEM_MB" -lt 24000 ]; }; then
		warn "'full' targets ~8 vCPU / 32 GB but this host has ${HOST_CPUS} vCPU / ${HOST_MEM_MB} MB. The observability stack alone needs several GB — 'slim' is the safer choice."
		yesno "Continue with 'full' anyway?" n || tier="slim"
	fi
	if [ "$tier" = "slim" ] && { [ "$HOST_CPUS" -lt 2 ] || [ "$HOST_MEM_MB" -lt 3500 ]; }; then
		warn "Even 'slim' wants ~2 vCPU / 4 GB; this host has ${HOST_CPUS} vCPU / ${HOST_MEM_MB} MB. Expect OOM kills or sluggishness."
	fi
fi
# The tier is only a preset for the per-capability defaults below; every optional
# service is still individually toggleable, and each one drives BOTH its Compose
# profile and its matching FEATURE_* flag so .env never needs hand-editing (#19).
if [ "$tier" = "full" ]; then profile_default="y"; else profile_default="n"; fi

# ---------------------------------------------------------------------------
# 3. Domains
# ---------------------------------------------------------------------------
say "Domains"
frontend_domain="$(ask "Frontend domain" "example.com")"
api_domain="$(ask "API domain" "api.${frontend_domain}")"
grafana_domain=""
if [ "$tier" = "full" ]; then
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
# 5. Optional services & integrations — each profile drives its feature flag
# ---------------------------------------------------------------------------
say "Optional services (the tier preset just sets the defaults — toggle freely)"

# Observability stack (Grafana/Prometheus/Loki/Tempo/...). The flag MUST mirror
# the profile: with FEATURE_OBSERVABILITY=True but no collector running, the
# backend repeatedly fails to export OTLP to localhost:4318 (#19).
enable_observability="no"
feature_observability="False"
enable_profiling="no"
if yesno "Enable the observability stack (Grafana/Prometheus/Loki/Tempo)?" "$profile_default"; then
	enable_observability="yes"
	feature_observability="True"
	if yesno "Enable continuous profiling (Pyroscope/Alloy)?" "$profile_default"; then
		enable_profiling="yes"
	fi
fi

# ClamAV malware scanning. One decision drives both the antivirus profile and
# FEATURE_MALWARE_SCAN, available on any tier (#19).
enable_antivirus="no"
feature_malware="False"
if yesno "Enable ClamAV malware scanning?" "$profile_default"; then
	enable_antivirus="yes"
	feature_malware="True"
fi

feature_telegram="False"
telegram_token=""
if yesno "Enable Telegram bot?" n; then
	feature_telegram="True"
	telegram_token="$(ask_secret "Telegram bot token")"
fi

feature_llm="False"
llm_api_key=""
llm_model="openai/gpt-4o-mini"
if yesno "Enable LLM questionnaire evaluation?" n; then
	feature_llm="True"
	llm_model="$(ask "LLM model (provider/model)" "openai/gpt-4o-mini")"
	llm_api_key="$(ask_secret "LLM API key")"
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
# 5b. Google SSO (optional) — admin login and user login are SEPARATE toggles
# ---------------------------------------------------------------------------
say "Google SSO (optional)"
feature_google_sso="False"
google_client_id=""
google_client_secret=""
admin_sso="no"
sso_show_form="True"   # keep the admin username/password form unless SSO-only is chosen
google_superuser_list=""
if yesno "Configure Google SSO? (one Google OAuth app, used for admin and/or user login)" n; then
	google_client_id="$(ask "Google OAuth client ID")"
	google_client_secret="$(ask_secret "Google OAuth client secret")"
	if yesno "Enable Google SSO for USER-facing login?" y; then
		feature_google_sso="True"
	fi
	if yesno "Enable Google SSO for the Django ADMIN login?" n; then
		admin_sso="yes"
		google_superuser_list="$(ask "Admin email(s) allowed via SSO (comma-separated)")"
		if yesno "Make admin SSO-only? (hide the username/password form)" n; then
			sso_show_form="False"
		fi
	fi
fi

# ---------------------------------------------------------------------------
# 5c. Login canary (optional) — synthetic login monitor scraped by Prometheus
# ---------------------------------------------------------------------------
enable_canary="no"
canary_email=""
canary_password=""
if [ "$enable_observability" = "yes" ]; then
	say "Login canary (optional)"
	echo "A synthetic login monitor (its metrics are scraped by Prometheus). Needs a"
	echo "DEDICATED account with NO org/staff roles and 2FA DISABLED, or it crash-loops."
	if yesno "Enable the login canary?" n; then
		enable_canary="yes"
		canary_email="$(ask "Canary account email")"
		canary_password="$(ask_secret "Canary account password")"
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
# 6b. Resource limits & tuning — tier-suggested, fully overridable
# ---------------------------------------------------------------------------
# Pick the per-tier suggestions, then let the user review/override each value.
# These are written explicitly to .env for BOTH tiers so they are easy to tweak
# later. CPU caps must never exceed the host CPU count or Docker refuses to start
# the container (the full defaults — web 6.0 / celery 4.0 — are invalid on a 2-vCPU
# slim box, which is why slim suggests lower values).
say "Resource limits & tuning"
if [ "$tier" = "slim" ]; then
	web_cpu="2";          celery_cpu="1.5"
	web_mem="1500m";      celery_mem="768m";    frontend_mem="512m"
	redis_mem="256m";     redis_maxmem="200mb"
	gunicorn_workers="2"; gunicorn_threads="2"; celery_concurrency="2"
	pg_shared="256MB";    pg_cache="1GB";       pg_maint="128MB"
	pg_work="8MB";        pg_maxconn="50"
	prom_mem="512m";      prom_cpu="0.5"
	loki_mem="512m";      loki_cpu="0.5"
	tempo_mem="256m";     tempo_cpu="0.5"
	grafana_mem="512m";   grafana_cpu="0.5"
	pyroscope_mem="256m"; pyroscope_cpu="0.5"
	alloy_mem="256m";     alloy_cpu="0.25"
else
	web_cpu="6.0";        celery_cpu="4.0"
	web_mem="12g";        celery_mem="8g";      frontend_mem="2g"
	redis_mem="1g";       redis_maxmem="512mb"
	gunicorn_workers="6"; gunicorn_threads="4"; celery_concurrency="4"
	pg_shared="4GB";      pg_cache="16GB";      pg_maint="1GB"
	pg_work="32MB";       pg_maxconn="100"
	prom_mem="2g";        prom_cpu="1.0"
	loki_mem="1g";        loki_cpu="2.0"
	tempo_mem="512m";     tempo_cpu="2.0"
	grafana_mem="2g";     grafana_cpu="1.0"
	pyroscope_mem="512m"; pyroscope_cpu="2.0"
	alloy_mem="512m";     alloy_cpu="1.0"
fi
# Fit the suggested CPU caps to the detected host so they're always valid (a cap
# above the host CPU count makes Docker refuse to create the container). On a 2-vCPU
# box the slim suggestions already fit; this also rescues a 'full' choice on a
# smaller box and any odd core count.
if [ -n "$HOST_CPUS" ]; then
	awk "BEGIN{exit !($web_cpu > $HOST_CPUS)}" && web_cpu="$HOST_CPUS"
	awk "BEGIN{exit !($celery_cpu > $HOST_CPUS)}" && celery_cpu="$HOST_CPUS"
	awk "BEGIN{exit !($prom_cpu > $HOST_CPUS)}" && prom_cpu="$HOST_CPUS"
	awk "BEGIN{exit !($loki_cpu > $HOST_CPUS)}" && loki_cpu="$HOST_CPUS"
	awk "BEGIN{exit !($tempo_cpu > $HOST_CPUS)}" && tempo_cpu="$HOST_CPUS"
	awk "BEGIN{exit !($grafana_cpu > $HOST_CPUS)}" && grafana_cpu="$HOST_CPUS"
	awk "BEGIN{exit !($pyroscope_cpu > $HOST_CPUS)}" && pyroscope_cpu="$HOST_CPUS"
	awk "BEGIN{exit !($alloy_cpu > $HOST_CPUS)}" && alloy_cpu="$HOST_CPUS"
fi
echo "Suggested values for the '${tier}' tier are shown in [brackets]."
if yesno "Review / override the suggested CPU, memory and tuning values?" n; then
	echo "Press Enter to accept each suggestion, or type a new value."
	web_cpu="$(ask "web CPU limit (cores; must be <= host CPUs)" "$web_cpu")"
	celery_cpu="$(ask "celery CPU limit (cores; must be <= host CPUs)" "$celery_cpu")"
	web_mem="$(ask "web memory limit" "$web_mem")"
	celery_mem="$(ask "celery memory limit" "$celery_mem")"
	frontend_mem="$(ask "frontend memory limit" "$frontend_mem")"
	redis_mem="$(ask "redis container memory limit" "$redis_mem")"
	redis_maxmem="$(ask "redis maxmemory (keep below the redis memory limit)" "$redis_maxmem")"
	gunicorn_workers="$(ask "gunicorn workers" "$gunicorn_workers")"
	gunicorn_threads="$(ask "gunicorn threads" "$gunicorn_threads")"
	celery_concurrency="$(ask "celery concurrency" "$celery_concurrency")"
	pg_shared="$(ask "postgres shared_buffers" "$pg_shared")"
	pg_cache="$(ask "postgres effective_cache_size" "$pg_cache")"
	pg_maint="$(ask "postgres maintenance_work_mem" "$pg_maint")"
	pg_work="$(ask "postgres work_mem" "$pg_work")"
	pg_maxconn="$(ask "postgres max_connections" "$pg_maxconn")"
	if [ "$enable_observability" = "yes" ]; then
		prom_cpu="$(ask "prometheus CPU limit" "$prom_cpu")"
		prom_mem="$(ask "prometheus memory limit" "$prom_mem")"
		loki_cpu="$(ask "loki CPU limit" "$loki_cpu")"
		loki_mem="$(ask "loki memory limit" "$loki_mem")"
		tempo_cpu="$(ask "tempo CPU limit" "$tempo_cpu")"
		tempo_mem="$(ask "tempo memory limit" "$tempo_mem")"
		grafana_cpu="$(ask "grafana CPU limit" "$grafana_cpu")"
		grafana_mem="$(ask "grafana memory limit" "$grafana_mem")"
	fi
	if [ "$enable_profiling" = "yes" ]; then
		pyroscope_cpu="$(ask "pyroscope CPU limit" "$pyroscope_cpu")"
		pyroscope_mem="$(ask "pyroscope memory limit" "$pyroscope_mem")"
	fi
	if [ "$enable_observability" = "yes" ] || [ "$enable_profiling" = "yes" ]; then
		alloy_cpu="$(ask "alloy CPU limit" "$alloy_cpu")"
		alloy_mem="$(ask "alloy memory limit" "$alloy_mem")"
	fi
fi

# Re-validate after any override: a CPU cap above the host CPU count makes Docker
# refuse to create the container ("range of CPUs is from 0.01 to N.00").
if [ -n "$HOST_CPUS" ]; then
	for cpu in "$web_cpu" "$celery_cpu" "$prom_cpu" "$loki_cpu" "$tempo_cpu" "$grafana_cpu" "$pyroscope_cpu" "$alloy_cpu"; do
		if awk "BEGIN{exit !($cpu > $HOST_CPUS)}" 2>/dev/null; then
			warn "CPU limit ${cpu} exceeds this host's ${HOST_CPUS} CPUs — Docker will refuse to start that container. Lower CPU limit variables in ${ENV_FILE}."
		fi
	done
fi
# Advisory memory-budget check: sum the capped containers (postgres is uncapped, so
# leave headroom) and warn if it overcommits RAM. Observability adds several GB more.
if [ -n "$HOST_MEM_MB" ]; then
	budget_mb=$(( $(to_mb "$web_mem") + $(to_mb "$celery_mem") + $(to_mb "$frontend_mem") + $(to_mb "$redis_mem") ))
	if [ "$enable_observability" = "yes" ]; then
		budget_mb=$(( budget_mb + $(to_mb "$prom_mem") + $(to_mb "$loki_mem") + $(to_mb "$tempo_mem") + $(to_mb "$grafana_mem") ))
	fi
	if [ "$enable_profiling" = "yes" ]; then
		budget_mb=$(( budget_mb + $(to_mb "$pyroscope_mem") ))
	fi
	if [ "$enable_observability" = "yes" ] || [ "$enable_profiling" = "yes" ]; then
		budget_mb=$(( budget_mb + $(to_mb "$alloy_mem") ))
	fi
	if [ "$budget_mb" -gt 0 ] && [ "$budget_mb" -ge "$HOST_MEM_MB" ]; then
		warn "Capped containers alone request ~${budget_mb} MB but the host has ${HOST_MEM_MB} MB (and postgres is uncapped on top). Lower the *_MEM_LIMIT values in ${ENV_FILE} or pick a smaller tier."
	fi
fi

# ---------------------------------------------------------------------------
# 7. Secrets
# ---------------------------------------------------------------------------
say "Generating secrets"
secret_key="$(gen)"
salt_key="$(gen)"
db_password="$(gen)"
grafana_password="$(gen)"

# Compose profiles — one per enabled capability, matching the FEATURE_* flags (#19).
profiles=""
[ "$enable_observability" = "yes" ] && profiles="${profiles:+$profiles,}observability"
[ "$enable_profiling" = "yes" ] && profiles="${profiles:+$profiles,}profiling"
[ "$enable_antivirus" = "yes" ] && profiles="${profiles:+$profiles,}antivirus"
[ "$feature_telegram" = "True" ] && profiles="${profiles:+$profiles,}telegram"
[ "$enable_canary" = "yes" ] && profiles="${profiles:+$profiles,}canary"

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
	# `web` is required: the frontend's SSR calls hit the API over the internal
	# network as http://web:8000, so Django sees Host: web. localhost/127.0.0.1
	# cover the container healthcheck. Without these, requests 400 (DisallowedHost).
	echo "ALLOWED_HOSTS=${frontend_domain},${api_domain},web,localhost,127.0.0.1"
	echo "SITE_DOMAIN=${api_domain}"
	echo "BASE_URL=https://${api_domain}"
	echo "FRONTEND_BASE_URL=https://${frontend_domain}"
	echo "CORS_ALLOWED_ORIGINS=https://${frontend_domain}"
	echo ""
	echo "FRONTEND_DOMAIN=${frontend_domain}"
	echo "API_DOMAIN=${api_domain}"
	# GRAFANA_DOMAIN only when observability runs; otherwise the Caddyfile's
	# {$GRAFANA_DOMAIN:grafana.localhost} default keeps the block inert (no ACME).
	if [ "$enable_observability" = "yes" ]; then
		echo "GRAFANA_DOMAIN=${grafana_domain:-grafana.${frontend_domain}}"
	fi
	echo ""
	echo "FEATURE_MALWARE_SCAN=${feature_malware}"
	echo "FEATURE_OBSERVABILITY=${feature_observability}"
	echo "FEATURE_TELEGRAM=${feature_telegram}"
	echo "FEATURE_LLM_EVALUATION=${feature_llm}"
	echo "FEATURE_ORGANIZATION_CREATION=${feature_org_creation}"
	echo "FEATURE_GOOGLE_SSO=${feature_google_sso}"
	if [ "$enable_observability" = "yes" ]; then
		# The backend defaults OTLP to localhost:4318; point it at the tempo container.
		echo "OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318"
	fi
	if [ -n "$google_client_id" ]; then
		echo "GOOGLE_SSO_CLIENT_ID=${google_client_id}"
		echo "GOOGLE_SSO_CLIENT_SECRET=${google_client_secret}"
	fi
	if [ "$admin_sso" = "yes" ]; then
		echo "GOOGLE_SSO_SUPERUSER_LIST=${google_superuser_list}"
		echo "GOOGLE_SSO_STAFF_LIST=${google_superuser_list}"
	fi
	echo "SSO_SHOW_FORM_ON_ADMIN_PAGE=${sso_show_form}"
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
	echo "LLM_DEFAULT_MODEL=${llm_model}"
	echo "LLM_API_KEY=${llm_api_key}"
	echo ""
	echo "STRIPE_SECRET_KEY=${stripe_secret}"
	echo "STRIPE_PUBLISHABLE_KEY=${stripe_publishable}"
	echo "STRIPE_ACCOUNT=${stripe_account}"
	echo "DEFAULT_CURRENCY=${default_currency}"
	echo ""
	echo "GRAFANA_ADMIN_USER=admin"
	echo "GRAFANA_ADMIN_PASSWORD=${grafana_password}"
	if [ "$enable_canary" = "yes" ]; then
		echo ""
		echo "CANARY_EMAIL=${canary_email}"
		echo "CANARY_PASSWORD=${canary_password}"
	fi
	echo ""
	echo "# Resource limits & tuning (${tier}-tier suggestions; edit to re-size)."
	echo "# CPU caps must not exceed the host's CPU count or Docker won't start the container."
	echo "PG_SHARED_BUFFERS=${pg_shared}"
	echo "PG_EFFECTIVE_CACHE_SIZE=${pg_cache}"
	echo "PG_MAINTENANCE_WORK_MEM=${pg_maint}"
	echo "PG_WORK_MEM=${pg_work}"
	echo "PG_MAX_CONNECTIONS=${pg_maxconn}"
	echo "WEB_MEM_LIMIT=${web_mem}"
	echo "CELERY_MEM_LIMIT=${celery_mem}"
	echo "FRONTEND_MEM_LIMIT=${frontend_mem}"
	echo "REDIS_MEM_LIMIT=${redis_mem}"
	echo "REDIS_MAXMEMORY=${redis_maxmem}"
	echo "WEB_CPU_LIMIT=${web_cpu}"
	echo "CELERY_CPU_LIMIT=${celery_cpu}"
	echo "GUNICORN_WORKERS=${gunicorn_workers}"
	echo "GUNICORN_THREADS=${gunicorn_threads}"
	echo "CELERY_CONCURRENCY=${celery_concurrency}"
	echo ""
	echo "PROMETHEUS_MEM_LIMIT=${prom_mem}"
	echo "PROMETHEUS_CPU_LIMIT=${prom_cpu}"
	echo "LOKI_MEM_LIMIT=${loki_mem}"
	echo "LOKI_CPU_LIMIT=${loki_cpu}"
	echo "TEMPO_MEM_LIMIT=${tempo_mem}"
	echo "TEMPO_CPU_LIMIT=${tempo_cpu}"
	echo "GRAFANA_MEM_LIMIT=${grafana_mem}"
	echo "GRAFANA_CPU_LIMIT=${grafana_cpu}"
	echo "PYROSCOPE_MEM_LIMIT=${pyroscope_mem}"
	echo "PYROSCOPE_CPU_LIMIT=${pyroscope_cpu}"
	echo "ALLOY_MEM_LIMIT=${alloy_mem}"
	echo "ALLOY_CPU_LIMIT=${alloy_cpu}"
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
# 13. First-run admin + organization (interactive)
# ---------------------------------------------------------------------------
# bootstrap_admin prompts for the admin email/password and first org, pins the
# username to the email (matching public-API accounts), and prints the URLs.
admin_created="no"
if docker compose ps web 2>/dev/null | grep -q "healthy"; then
	if yesno "Create the admin user and first organization now?" y; then
		if docker compose exec web python manage.py bootstrap_admin; then
			admin_created="yes"
		else
			warn "Admin bootstrap did not complete — re-run it any time with:"
			warn "  docker compose exec web python manage.py bootstrap_admin"
		fi
	fi
else
	warn "The web service is not healthy yet; skipping admin creation. Check 'docker compose logs web'."
fi

# ---------------------------------------------------------------------------
# 14. Next steps
# ---------------------------------------------------------------------------
say "Done. Next steps:"
if [ "$admin_created" != "yes" ]; then
	echo "  - Create the admin + first org:  docker compose exec web python manage.py bootstrap_admin"
fi
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
