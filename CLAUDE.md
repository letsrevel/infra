# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Important Constraints

**ABSOLUTELY FORBIDDEN COMMANDS:**
- **NEVER** run `git commit` or `git push` on main. ALWAYS open PRs.
- **NEVER** perform `ssh` or `scp` operations on the server without the user giving you explicit permissions
- The user will manually handle all git operations and file transfers to the server

## Working Environment

**Local Development:** Commands are run locally on the developer's machine. To execute commands on the production server, use `ssh revel "cd infra && <command>"` format.

**Server Directory:** The infrastructure is deployed in the `infra` directory on the production server.

## Repository Overview

This is the infrastructure repository for **Revel**, a Django-based application platform. It contains the complete Docker Compose setup orchestrating all application services, infrastructure components, and a comprehensive observability stack. This repository is part of a multi-repo architecture alongside:

- **revel-backend** - Django REST API with business logic, living at `../revel-backend`
- **revel-frontend** - SvelteKit web application, living at `../revel-frontend`
- **infra** (this repo) - Deployment and infrastructure configuration

The application runs on a **Hetzner CCX33** instance (8 vCPU, 32GB RAM, 240GB disk).

## Architecture

Services, networks, volumes, and resource limits are all defined in `docker-compose.yml` — read it for the current topology. Non-obvious points:

- `alloy` (eBPF profiling collector) requires privileged mode.
- `./media` bind mount is shared between `web`, `celery_default`, and `telegram`.
- `./sentinel` holds LLM sentinel data.

## Common Commands

Prefer `./deploy.sh` over raw compose for lifecycle operations: `./deploy.sh up` (start with validation), `./deploy.sh update` (pull + redeploy latest images), `./deploy.sh backup` (database backup).

### Configuration Reloads

```bash
# Reload Prometheus configuration (without restart)
docker compose exec prometheus kill -HUP 1

# Reload Alertmanager configuration
docker compose restart alertmanager

# Reload Caddy configuration
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## Environment Configuration

The `.env` file controls all configuration. Critical variables:

**Database:**
- `DB_USE_PGBOUNCER=True` - Always use PgBouncer in production
- `DB_HOST=pgbouncer` and `DB_PORT=6432` when using PgBouncer
- `DB_CONN_MAX_AGE=0` when using PgBouncer (connection pooling conflicts)

**Observability:**
- `ENABLE_OBSERVABILITY=True` - Must be enabled for tracing
- `TRACING_SAMPLE_RATE=0.1` - 10% sampling to reduce overhead
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318` - Tempo traces endpoint

**Alerting:**
- `PUSHOVER_USER_KEY` and `PUSHOVER_APP_TOKEN` - Required for mobile alerts
- Alert severities: `critical` (priority 2), `warning` (priority 1), `info` (priority 0)

## Alerting Architecture

Alerts are defined in `observability/alerts/`:
- `infrastructure.yml` - System resources, database, Redis, observability services
- `application.yml` - HTTP errors, Celery, ClamAV, auth failures

**Alert Workflow:**
1. Prometheus evaluates rules every 15s
2. Alerts fire after specified duration (e.g., `for: 5m`)
3. Alertmanager groups by `alertname`, `cluster`, `service`
4. Pushover sends mobile notification based on severity
5. Critical alerts require acknowledgment and retry every 60s

**Inhibition Rules:**
- `ServiceDown` suppresses latency warnings
- `PostgresDown` suppresses connection warnings

## Deployment Domains

Configured in `Caddyfile`:
- `beta.letsrevel.io` - Frontend (port 3000)
- `beta-api.letsrevel.io` - API (port 8000), media files served from `/srv/revel_media`
- `flower.letsrevel.io` - Celery monitoring (port 5555)
- `grafana.letsrevel.io` - Grafana (port 3000)

**Important:** The `/metrics` endpoint is blocked on the API domain for security.

## Tuning & Configuration

Tuning values live in `docker-compose.yml` and the config files under version control — read them there. The one cross-service contract worth restating: PgBouncer runs in **transaction pool mode**, so Django must use `DB_CONN_MAX_AGE=0` (see Environment Configuration above). Beat uses `DatabaseScheduler` — periodic tasks are stored in PostgreSQL, not in code.

## Security Considerations

- Never expose PostgreSQL, Redis, or Prometheus ports externally
- `/metrics` endpoint on API is blocked in Caddyfile
- All services run within `revel_network` (isolated bridge network)
- Flower protected by Google SSO (`GOOGLE_SSO_*` variables)
- Grafana requires admin credentials (`GRAFANA_ADMIN_*`)
- ClamAV scans uploads before storage

## Troubleshooting

**Service won't start:**
1. Check dependencies: `docker compose ps` (look for unhealthy dependencies)
2. Review logs: `docker compose logs [service_name]`
3. Verify environment variables: `docker compose config` shows interpolated values

**Database connection issues:**
- Ensure `DB_HOST=pgbouncer` and `DB_PORT=6432` in `.env`
- Check PgBouncer health: `docker compose exec pgbouncer pg_isready -h localhost -p 6432`
- Verify PostgreSQL: `docker compose exec revel_postgres pg_isready`

**Alert not firing:**
1. Check Prometheus rules loaded: `curl http://localhost:9090/api/v1/rules`
2. Verify alert is firing: `curl http://localhost:9090/api/v1/alerts`
3. Check Alertmanager: `docker compose logs alertmanager | grep -i pushover`
4. Test Pushover credentials via curl (see ALERTING_SETUP.md)

**High memory usage:**
- PostgreSQL shared_buffers (4GB) is always allocated
- ClamAV (up to 2GB) for virus definitions
- Check per-service: `docker stats`

## Development Workflow

When modifying infrastructure:

1. **Configuration changes:**
   - Edit files in `observability/` directory
   - Reload service: `docker compose restart [service]` or use HUP signal for Prometheus

2. **Adding new alerts:**
   - Add rule to `observability/alerts/*.yml`
   - Reload: `docker compose exec prometheus kill -HUP 1`
   - Verify: Check Prometheus UI at port 9090

3. **Adding Grafana dashboards:**
   - Create JSON in `observability/dashboards/`
   - Auto-loaded within 30 seconds (watch `docker compose logs grafana`)

4. **Updating application images:**
   - Backend: `docker compose pull web celery_default beat flower telegram`
   - Frontend: `docker compose pull frontend`
   - Deploy: `docker compose up -d`

5. **Testing changes locally:**
   - Use `.env.example` as template
   - Override domains in `/etc/hosts` if needed
   - Disable HTTPS in Caddyfile for local testing

## Related Documentation

- Full alerting guide: `ALERTING_SETUP.md`
- Service overview: `README.md`
- Dashboard management: `observability/dashboards/README.md`