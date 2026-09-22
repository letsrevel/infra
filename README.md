# Revel infrastructure

**Revel is an open-source event management, ticketing and membership platform for communities, clubs, independent venues and independent artists.**

[![License](https://img.shields.io/badge/license-MIT-blue?style=for-the-badge)](./LICENSE)
[![Discord](https://img.shields.io/badge/Discord-Join%20us-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.gg/Rnwbzuvxvn)

This repository is the Docker Compose deployment for Revel: the stack, three Caddyfile variants, the `setup.sh` wizard for self-hosters and the scripts we use to run [letsrevel.io](https://letsrevel.io). For what Revel does and who it is for, read the [main README](https://github.com/letsrevel/revel-backend#readme). The full self-hosting guide is at [docs.letsrevel.io/self-hosting](https://docs.letsrevel.io/self-hosting/).

## Install

You need:

- A Linux x86-64 server (the images are built for `linux/amd64` only).
- A root shell, or a user in the `docker` group.
- `git`, `openssl` and `curl`.
- DNS A records for two hostnames: one for the web app and one for the API, which defaults to `api.<your domain>`. Add a third for Grafana if you enable observability.
- Ports 80 and 443 open.

Then:

```bash
git clone https://github.com/letsrevel/infra && cd infra && ./setup.sh
```

The wizard is interactive and has no unattended mode. It:

1. Offers to install Docker (via `get.docker.com`) if it is missing, and checks that ports 80 and 443 are free.
2. Suggests a tier from the detected CPU and RAM.
3. Asks for the frontend and API domains, SMTP (or dry-run email) and which optional services to enable: observability (plus its Grafana domain), ClamAV, the Telegram bot, LLM questionnaire evaluation, Stripe, Google login and the login canary. It also asks whether you are behind Cloudflare.
4. Asks whether this is a single-organization instance (the default), which turns off public organization creation.
5. Backs up any existing `.env`, writes a new one with generated secrets and picks the matching Caddyfile.
6. Downloads the city list (and, optionally, the 182 MB IP2Location LITE database), pulls the images from `ghcr.io/letsrevel` and runs `docker compose up -d`.
7. Once the API is healthy, registers Stripe webhooks (if Stripe is enabled and `jq` is installed) and creates the admin user and first organization.

Caddy obtains Let's Encrypt certificates on first start. Behind Cloudflare, keep the DNS records DNS-only (gray cloud) until the certificates are issued; the wizard pauses for this.

## Tiers

A tier is a preset of defaults for the questions above and for resource limits. Every optional service can still be switched on or off on its own.

| Tier | Hardware | Runs |
|---|---|---|
| Slim | 2 vCPU, 4 GB RAM | Web app, API, Celery worker and beat, PostgreSQL/PostGIS, PgBouncer, Redis, Caddy |
| Full | 8 vCPU, 32 GB RAM | Slim plus any of: Grafana/Loki/Tempo/Prometheus observability, ClamAV, the Telegram bot, a login canary. The wizard defaults observability and ClamAV to on; Telegram and the canary stay opt-in |

The slim tier costs about €20/month (Hetzner CPX22, September 2026). letsrevel.io runs the full tier on a Hetzner CCX33 (8 dedicated vCPU, 32 GB RAM, 240 GB disk). The wizard suggests full only on hosts with at least 8 vCPU and 24 GB RAM, and warns below 2 vCPU and 3.5 GB.

| | Slim | Full |
|---|---|---|
| Gunicorn workers x threads | 2 x 2 | 6 x 4 |
| Celery concurrency | 2 | 4 |
| Postgres `shared_buffers` / `max_connections` | 256MB / 50 | 4GB / 100 |
| Web memory limit | 1500m | 12g |

## Services, profiles and feature flags

Always running: `caddy`, `web` (Gunicorn, gthread workers), `celery_default`, `beat`, `frontend`, `revel_postgres` (PostGIS 17), `pgbouncer` and `redis`.

Optional services are Compose profiles, listed in `COMPOSE_PROFILES` in `.env`. Most profiles have a matching feature flag that the backend reads, and the wizard sets both together. `GET /api/version` reports some of the flags (organization creation, Telegram, LLM evaluation) so the web app can hide features that are off.

| Profile | Services | Flag |
|---|---|---|
| `observability` | grafana, prometheus, alertmanager, loki, tempo, pyroscope, alloy, postgres-exporter, redis-exporter, node_exporter, blackbox-exporter | `FEATURE_OBSERVABILITY` |
| `antivirus` | clamav | `FEATURE_MALWARE_SCAN` |
| `telegram` | telegram | `FEATURE_TELEGRAM` |
| `canary` | canary (synthetic login check) | none |

Flags with no service behind them: `FEATURE_LLM_EVALUATION` (with `LLM_*`), `FEATURE_ORGANIZATION_CREATION` (off for single-organization instances), Stripe keys, `OIDC_PROVIDERS` for user login through Google or any OpenID Connect provider, `GOOGLE_SSO_*` for the Django admin login, `APPLE_WALLET_*`, `GOOGLE_WALLET_*` and `INTEGRATIONS_EVENTBRITE_*`. All are optional. SMTP is optional too, but without it nobody receives verification or ticket emails. See [`.env.example`](.env.example) for every variable; its values document our own production deployment.

The wizard asks for one Google OAuth client and uses it for two separate toggles. User-facing login writes `OIDC_PROVIDERS=google` with `OIDC_GOOGLE_ISSUER`, `OIDC_GOOGLE_CLIENT_ID` and `OIDC_GOOGLE_CLIENT_SECRET`; add `https://<API_DOMAIN>/api/auth/oidc/google/callback` as a redirect URI on that client. Admin login writes `GOOGLE_SSO_*`. To add another OpenID Connect provider, see [tiers and configuration](https://docs.letsrevel.io/self-hosting/tiers/).

### Caddyfiles

Domains come from `.env` (`FRONTEND_DOMAIN`, `API_DOMAIN` and `GRAFANA_DOMAIN`, plus `DOCS_DOMAIN` in the production `Caddyfile` only). Pick a variant with `CADDYFILE_PATH`; the wizard sets it.

- `Caddyfile`: Cloudflare-aware plus legacy redirects. The production default; its fallbacks resolve to the letsrevel.io domains.
- `Caddyfile.cloudflare`: self-hosting behind Cloudflare's proxy.
- `Caddyfile.generic`: self-hosting with Caddy as the edge.

**Never buffer frontend responses.** The frontend uses SvelteKit streaming SSR. A `flush_interval -1` on the frontend `reverse_proxy` makes Caddy hold the whole response, and pages hang on a loading state until SSR finishes. None of the shipped Caddyfiles set it. To check a deployment, compare time to first byte with total time; they should differ:

```bash
curl -w "\nTTFB: %{time_starttransfer}s\nTotal: %{time_total}s\n" -o /dev/null "https://<your-frontend-domain>/events"
```

### Apple Wallet certificates

Apple Wallet passes need a Pass Type ID certificate, its private key and Apple's WWDR certificate. Put them in `certs/`, which is mounted into the containers at `/app/certs`, and point `APPLE_WALLET_CERT_PATH`, `APPLE_WALLET_KEY_PATH` and `APPLE_WALLET_WWDR_CERT_PATH` at them (for example `/app/certs/pass-certificate.pem`). Also set `APPLE_WALLET_PASS_TYPE_ID`, `APPLE_WALLET_TEAM_ID` and, if the key has one, `APPLE_WALLET_KEY_PASSWORD`.

The containers run as a non-root user whose UID differs from the host's, so every file, including the key, must be mode `644`. With `600`, pass generation fails with `PermissionError: [Errno 13] Permission denied: '/app/certs/pass-certificate.pem'`.

```bash
chmod 644 certs/*.pem
```

Google Wallet setup is in the [self-hosting docs](https://docs.letsrevel.io/self-hosting/#google-wallet-setup-one-time).

## Operations

```bash
./deploy.sh update      # pull the latest images and redeploy
./deploy.sh backup      # pg_dump to backup_<timestamp>.sql
./deploy.sh logs web    # also: up, down, restart, ps, pull
docker compose up -d --scale celery_default=4
```

`deploy-rollout.sh` replaces containers with `docker-rollout` instead of stopping them first, `safe-reboot.sh` drains Celery tasks before rebooting the host and `ALERTING_SETUP.md` covers Pushover alerts and Grafana dashboards. Persistent data lives in named volumes (`revel_postgres_data`, `redis_data`, `caddy_data`, `caddy_config` and `clamav_data`, plus one per observability service). `docker compose down -v` deletes all of it.

Before exposing an instance: change every default password in `.env`, keep `.env` out of version control and keep the images updated.

## License

MIT. See [LICENSE](LICENSE).

## Related repositories

- [revel-backend](https://github.com/letsrevel/revel-backend): the Django API and the main project page
- [revel-frontend](https://github.com/letsrevel/revel-frontend): the SvelteKit web app
- [.github](https://github.com/letsrevel/.github): the organization profile
