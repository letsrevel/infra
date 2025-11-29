# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Important Constraints

**ABSOLUTELY FORBIDDEN COMMANDS:**
- **NEVER** run `git commit` or `git push` - You are not allowed to commit or push changes to this repository
- **NEVER** run `scp` - You are not allowed to copy files to the server using scp
- The user will manually handle all git operations and file transfers to the server

## Working Environment

**Local Development:** Commands are run locally on the developer's machine. To execute commands on the production server, use `ssh revel "cd infra && <command>"` format.

**Server Directory:** The infrastructure is deployed in the `infra` directory on the production server.

## Repository Overview

This is the infrastructure repository for **Revel**, a Django-based application platform. It contains the complete Docker Compose setup orchestrating all application services, infrastructure components, and a comprehensive observability stack. This repository is part of a multi-repo architecture alongside:

- **revel-backend** - Django REST API with business logic
- **revel-frontend** - SvelteKit web application
- **infra** (this repo) - Deployment and infrastructure configuration

The application runs on a **Hetzner CCX33** instance (8 vCPU, 32GB RAM, 240GB disk).

## Architecture

### Service Categories

**Application Services:**
- `web` - Django app running on Gunicorn (6 workers, 4 threads, gthread worker class)
- `frontend` - SvelteKit application on port 3000
- `celery_default` - Background task worker (4 concurrency)
- `beat` - Celery scheduler with Django database scheduler
- `flower` - Celery monitoring UI with Google SSO auth
- `telegram` - Telegram bot service

**Infrastructure Services:**
- `caddy` - Reverse proxy with automatic HTTPS (serves 4 domains)
- `revel_postgres` - PostGIS 17-3.5 with optimized configuration for 32GB RAM
- `pgbouncer` - Connection pooler (transaction mode, max 1000 client connections, pool size 25)
- `redis` - Cache and message broker (512MB maxmemory, LRU eviction, AOF persistence)

**Observability Stack:**
- `prometheus` - Metrics collection (30d retention)
- `alertmanager` - Alert routing with Pushover integration
- `loki` - Log aggregation
- `tempo` - Distributed tracing (OTLP on ports 4317/4318)
- `pyroscope` - Continuous profiling
- `alloy` - eBPF-based profiling collector (requires privileged mode)
- `grafana` - Visualization dashboard
- `postgres-exporter` - PostgreSQL metrics
- `redis-exporter` - Redis metrics
- `blackbox-exporter` - Health check probing

**Security:**
- `clamav` - Antivirus scanning (256MB max file size)

### Networking

All services run on `revel_network` (bridge network). Services communicate using container names as hostnames.

### Volume Management

Persistent data volumes:
- `revel_postgres_data` - Database (most critical)
- `redis_data` - Cache persistence
- `caddy_data` - SSL certificates
- `prometheus_data`, `loki_data`, `tempo_data` - Observability data
- `grafana_data` - Dashboard configurations

Bind mounts:
- `./media` - User uploads (shared between web, celery, telegram)
- `./geo-data` - Geographic data files
- `./sentinel` - LLM sentinel data

## Common Commands

### Starting and Stopping

```bash
# Start all services
docker compose up -d

# Start with deploy script (includes validation)
./deploy.sh up

# Stop all services
docker compose down

# Update to latest images
./deploy.sh update
# or manually:
docker compose pull
docker compose up -d
```

### Monitoring and Debugging

```bash
# View all service status
docker compose ps

# View logs for specific service
docker compose logs -f web
docker compose logs -f celery_default

# View logs with timestamps
docker compose logs -f --timestamps web

# Follow logs for multiple services
docker compose logs -f web celery_default

# Check health of all services
docker compose ps --format json | jq '.[] | {name: .Name, health: .Health}'
```

### Database Operations

```bash
# Access PostgreSQL directly (bypassing PgBouncer)
docker compose exec revel_postgres psql -U $DB_USER -d $DB_NAME

# Access via PgBouncer
docker compose exec pgbouncer psql -h localhost -p 6432 -U $DB_USER -d $DB_NAME

# Create backup
./deploy.sh backup
# or manually:
docker compose exec -T revel_postgres pg_dump -U $DB_USER $DB_NAME > backup_$(date +%Y%m%d_%H%M%S).sql

# Restore from backup
cat backup.sql | docker compose exec -T revel_postgres psql -U $DB_USER -d $DB_NAME
```

### Celery Operations

```bash
# Scale workers
docker compose up -d --scale celery_default=4

# Inspect active tasks
docker compose exec celery_default celery -A revel inspect active

# Purge all tasks from queue
docker compose exec celery_default celery -A revel purge
```

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

## Resource Limits

Key resource allocations in docker-compose.yml:

- `web`: 4-12GB RAM, 2-6 CPUs (largest allocation)
- `celery_default`: 2-8GB RAM, 1-4 CPUs
- `revel_postgres`: No explicit limit (uses shared_buffers=4GB, effective_cache_size=16GB)
- `grafana`, `prometheus`: 2GB RAM, 1 CPU each
- `clamav`: 2GB RAM, 1 CPU (memory-intensive)

Total observability overhead: ~6GB RAM, 8 CPUs

## Important Configuration Details

**PostgreSQL Tuning:**
- Configured for 32GB RAM system
- `max_connections=100` (limited due to PgBouncer)
- `shared_buffers=4GB`, `effective_cache_size=16GB`
- `work_mem=32MB`, `maintenance_work_mem=1GB`

**PgBouncer:**
- `POOL_MODE=transaction` - Best for Django
- `MAX_CLIENT_CONN=1000` - High for multiple services
- `DEFAULT_POOL_SIZE=25` - Actual PostgreSQL connections
- Django must use `DB_CONN_MAX_AGE=0` with transaction pooling

**Gunicorn (web service):**
- Worker class: `gthread` (supports async operations)
- Workers: 6 (recommended: 2-4 × CPU cores)
- Threads: 4 per worker (total 24 threads)
- `max-requests=4000` with 10% jitter for memory leak prevention
- Timeout: 60s, graceful timeout: 30s

**Redis:**
- `maxmemory=512mb` with `allkeys-lru` eviction
- AOF persistence enabled with saves every 15min, 5min, 1min
- `maxclients=10000`

**Celery:**
- `--concurrency=4` for default worker
- `--max-tasks-per-child=1000` for memory management
- Beat uses `DatabaseScheduler` (tasks stored in PostgreSQL)

## Health Checks

All critical services have health checks:
- `web`: `curl http://localhost:8000/api/healthcheck`
- `celery_default`: `celery -A revel inspect ping`
- `flower`: `curl http://localhost:5555/`
- `revel_postgres`: `pg_isready`
- `redis`: `redis-cli ping`

Failed health checks trigger automatic restarts.

## Observability Access

**Internal URLs (within docker network):**
- Prometheus: `http://prometheus:9090`
- Alertmanager: `http://alertmanager:9093`
- Loki: `http://loki:3100`
- Tempo: `http://tempo:3200`
- Pyroscope: `http://pyroscope:4040`

**External URLs:**
- Grafana: `https://grafana.letsrevel.io`
- Flower: `https://flower.letsrevel.io`

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