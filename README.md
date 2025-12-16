# Revel Infrastructure

This repository contains the complete infrastructure configuration for the Revel project, including all services, observability stack, and reverse proxy setup.

---

## 🔗 Related Repositories

This repository contains the **infrastructure and deployment configurations** for Revel. The complete platform consists of:

- **[revel-backend](https://github.com/letsrevel/revel-backend)** - Django REST API, business logic, database models
- **[revel-frontend](https://github.com/letsrevel/revel-frontend)** - SvelteKit web application, user interface
- **[infra](https://github.com/letsrevel/infra)** (this repository) - Docker Compose setup, reverse proxy, observability stack, deployment configurations

---

## Overview

This is a consolidated Docker Compose setup that includes:

### Application Services
- **Web** - Django application (Gunicorn + Uvicorn workers)
- **Frontend** - Next.js/SvelteKit frontend application
- **Celery Workers** - Background task processing (default queue)
- **Beat** - Celery scheduler for periodic tasks
- **Flower** - Celery monitoring UI
- **Telegram Bot** - Telegram bot service

### Infrastructure Services
- **Caddy** - Reverse proxy with automatic HTTPS
- **PostgreSQL** (PostGIS) - Primary database
- **PgBouncer** - Connection pooler for PostgreSQL
- **Redis** - Cache and message broker

### Observability Stack
- **Grafana** - Metrics and logs visualization
- **Prometheus** - Metrics collection
- **Loki** - Log aggregation
- **Tempo** - Distributed tracing
- **Pyroscope** - Continuous profiling
- **Alloy** - eBPF-based profiling collector
- **postgres-exporter** - PostgreSQL metrics exporter

### Security
- **ClamAV** - Antivirus scanning for uploaded files

## Quick Start

1. **Clone the repository**
   ```bash
   cd /path/to/revel/infra
   ```

2. **Create environment file**
   ```bash
   cp .env.example .env
   # Edit .env with your actual configuration
   ```

3. **Start all services**
   ```bash
   docker compose up -d
   ```

4. **Check service status**
   ```bash
   docker compose ps
   ```

5. **View logs**
   ```bash
   docker compose logs -f [service_name]
   ```

## Configuration

### Environment Variables

All configuration is done through the `.env` file. See `.env.example` for all available options.

Key variables to configure:
- Database credentials (`DB_*`)
- Django secret key (`SECRET_KEY`)
- Google SSO for Flower (`GOOGLE_SSO_*`)
- Grafana admin credentials (`GRAFANA_ADMIN_*`)
- Pushover notifications (`PUSHOVER_USER_KEY`, `PUSHOVER_APP_TOKEN`)

### Domain Configuration

The Caddyfile is configured for the following domains:
- `beta.letsrevel.io` - Frontend application
- `beta-api.letsrevel.io` - Backend API
- `flower.letsrevel.io` - Celery monitoring
- `grafana.letsrevel.io` - Grafana dashboard

Update the Caddyfile if you need to change domains or add new ones.

## Directory Structure

```
.
├── docker-compose.yml          # Main compose file
├── Caddyfile                   # Reverse proxy configuration
├── .env                        # Environment variables (create from .env.example)
├── .env.example                # Environment variables template
├── observability/              # Observability stack configurations
│   ├── alloy-config.alloy
│   ├── grafana-datasources.yaml
│   ├── loki-config.yaml
│   ├── prometheus-config.yml
│   └── tempo-config.yaml
├── media/                      # User-uploaded media files
├── geo-data/                   # Geographic data files
├── sentinel/                   # LLM sentinel data
└── certs/                      # Apple Wallet certificates (optional)
```

## Apple Wallet Pass Certificates

The application supports generating Apple Wallet passes for event tickets. This requires Apple Developer certificates to be placed in the `certs/` directory.

### Required Files

```bash
certs/
├── pass_certificate.pem  # Apple Pass Type ID certificate
├── pass_key.pem          # Private key for the certificate
└── wwdr.pem              # Apple Worldwide Developer Relations certificate
```

### Setting File Permissions

**IMPORTANT:** All certificate files must be readable by the Docker container (which runs as non-root user `appuser`):

```bash
chmod 644 /path/to/revel/infra/certs/pass_certificate.pem
chmod 644 /path/to/revel/infra/certs/pass_key.pem
chmod 644 /path/to/revel/infra/certs/wwdr.pem
```

**Why `644` for all files (including the private key)?**
- `644` (rw-r--r--): Owner can read/write, others can read
- The Docker container runs as `appuser` (non-root), which needs read access to mounted files
- While `600` would be ideal for private keys in traditional setups, Docker volume mounts require readable permissions when the container user differs from the host file owner
- The files are only accessible within the server's filesystem (not exposed externally)

If you see errors like `PermissionError: [Errno 13] Permission denied: '/app/certs/pass_certificate.pem'`, verify the file permissions are set correctly.

## Volumes

The following Docker volumes are created for persistent data:
- `caddy_data` - Caddy data (SSL certificates)
- `caddy_config` - Caddy configuration cache
- `revel_postgres_data` - PostgreSQL database
- `redis_data` - Redis persistence
- `loki_data` - Log storage
- `tempo_data` - Trace storage
- `prometheus_data` - Metrics storage
- `pyroscope_data` - Profiling data
- `alloy_data` - Alloy configuration
- `grafana_data` - Grafana dashboards and settings

## Networking

All services run on a dedicated bridge network called `revel_network`.

## Deployment

### Server Specifications

Revel is deployed on a **Hetzner CCX33** instance:
- **CPU**: 8 vCPU
- **RAM**: 32 GB
- **Disk**: 240 GB

### Initial Deployment

1. Set up your server with Docker and Docker Compose
2. Clone this repository
3. Configure your `.env` file
4. Ensure DNS records point to your server:
   - beta.letsrevel.io
   - beta-api.letsrevel.io
   - flower.letsrevel.io
   - grafana.letsrevel.io
5. Start services: `docker compose up -d`
6. Caddy will automatically provision SSL certificates

### Updates

To update a service to the latest image:
```bash
docker compose pull [service_name]
docker compose up -d [service_name]
```

To update all services:
```bash
./deploy.sh update
```

## 🚨 Critical: SvelteKit Streaming SSR

**IMPORTANT:** The Revel frontend uses SvelteKit with **streaming Server-Side Rendering (SSR)**. Improper Caddy configuration will cause pages to hang indefinitely with "eternal loading" states.

### ⚠️ Reverse Proxy Configuration

**The Caddyfile MUST NOT buffer responses.** Buffering breaks SvelteKit's streaming SSR.

**❌ WRONG - DO NOT USE:**

```caddy
beta.letsrevel.io {
    reverse_proxy revel_frontend:3000 {
        flush_interval -1  # ❌ Buffers entire response - BREAKS SVELTEKIT!
    }
}
```

**✅ CORRECT - Streaming enabled:**

```caddy
beta.letsrevel.io {
    encode zstd gzip

    reverse_proxy revel_frontend:3000 {
        # No flush_interval = streaming enabled by default ✅
        transport http {
            keepalive 90s
            keepalive_idle_conns 32
            max_conns_per_host 100
        }
    }
}
```

### Why This Matters

**With `flush_interval -1` (buffering enabled):**
- ❌ Caddy holds the ENTIRE HTML response in memory
- ❌ Browser receives nothing until SSR fully completes
- ❌ Pages appear to "hang" or show eternal loading spinner
- ❌ Poor user experience, especially on slow connections
- ❌ Potential timeout issues on large pages

**Without buffering (default):**
- ✅ Caddy streams HTML as SvelteKit generates it
- ✅ Browser receives and renders content progressively
- ✅ Faster perceived load times (better TTFB)
- ✅ Better user experience
- ✅ Support for large pages without timeouts

### Symptoms of Misconfiguration

If you experience these issues, check the Caddyfile for response buffering:
- Pages show eternal loading spinner
- Network tab shows request pending for many seconds
- HTML arrives all at once after a long delay
- Time-to-first-byte (TTFB) equals total response time

### Testing Your Configuration

```bash
# Test response timing - should see progressive delivery
curl -w "\nTTFB: %{time_starttransfer}s\nTotal: %{time_total}s\n" \
  -o /dev/null \
  "https://beta.letsrevel.io/events"

# Good: TTFB and Total are close (streaming)
# Bad: TTFB ≈ Total time (buffering!)
```

### Verified Working Configuration

See the `Caddyfile` in this repository for the correct production configuration. All `reverse_proxy` blocks for the frontend **must not** include `flush_interval -1`.

---

## Monitoring

Access monitoring tools:
- Grafana: https://grafana.letsrevel.io
- Flower (Celery): https://flower.letsrevel.io
- Prometheus (metrics): Available internally at `revel_prometheus:9090`

## Maintenance

### Database Backups

```bash
docker compose exec revel_postgres pg_dump -U $DB_USER $DB_NAME > backup.sql
```

### Logs

View logs for a specific service:
```bash
docker compose logs -f [service_name]
```

View all logs:
```bash
docker compose logs -f
```

### Scaling Celery Workers

To scale up celery workers:
```bash
docker compose up -d --scale celery_default=4
```

## Troubleshooting

### Certificate Permission Errors

If you see errors like:
```
PermissionError: [Errno 13] Permission denied: '/app/certs/pass_certificate.pem'
```

Fix the certificate file permissions:
```bash
chmod 644 /path/to/revel/infra/certs/*.pem
```

Verify permissions are correct:
```bash
ls -la /path/to/revel/infra/certs/
```

All files should show `-rw-r--r--` (644 permissions).

### Check service health
```bash
docker compose ps
```

### Restart a service
```bash
docker compose restart [service_name]
```

### Reset everything (CAUTION: destroys data)
```bash
docker compose down -v
```

## Security Notes

- Change all default passwords in `.env`
- Never commit `.env` to version control
- Regularly update Docker images
- Monitor Grafana for security alerts
- Review Pushover alert notifications

## Differences from Old Setup

This consolidated setup differs from the previous multi-repo setup:
- All services are in a single `docker-compose.yml`
- Uses a dedicated `revel_network` instead of an external `shared` network
- Includes Redis directly (was previously in shared services)
- Simplified volume management
- All observability services are included
- Comprehensive alerting via Prometheus and Pushover

## License

See LICENSE file in the repository root.
