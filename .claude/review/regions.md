# Gauntlet regions: infra

## edge-proxy
- globs: `Caddyfile`, `Caddyfile.cloudflare`, `Caddyfile.generic`, `.github/workflows/cloudflare-ip-drift.yml`
- context: `docker-compose.yml`
- risk: HIGH
- tier: opus
- security focus: on-demand TLS ask gating for custom domains, trusted_proxies and client IP, security headers (CSP, CORP, media served as downloads), `/metrics` and admin blocks, scanner-probe drops, log redaction of tokens, SSR streaming not buffered
- debt focus: drift between the three Caddyfiles

## deploy-lifecycle
- globs: `docker-compose.yml`, `deploy.sh`, `deploy-rollout.sh`, `image-diff.sh`, `notify-discord.sh`, `safe-reboot.sh`
- context: `rotation/rotate.py`, `README.md`
- risk: HIGH
- tier: opus
- security focus: exposed ports, privileged containers, volume and secret mounts, image pinning, `.env` handling in scripts, webhook URLs
- debt focus: duplicated logic between `deploy.sh` and `deploy-rollout.sh`, error handling under `set -e`, resource limits

## install-wizard
- globs: `setup.sh`, `.env.example`
- context: `rotation/manifest.py`, `docker-compose.yml`
- risk: HIGH
- tier: opus
- security focus: secret generation and quoting, `.env` file mode and owner, bind-mount ownership, values kept or lost on a re-run
- debt focus: wizard length, drift between the wizard, `.env.example` and README

## secret-rotation
- globs: `rotation/rotate.py`, `rotation/manifest.py`, `rotation/README.md`, `.github/workflows/rotation.yml`
- context: `deploy-rollout.sh`, `.env.example`
- risk: CRITICAL
- tier: opus
- security focus: atomic `.env` writes, backups and archives of retired secrets, journal recovery, grace windows (fallback keys), rollback correctness, lock handling
- debt focus: `rotate.py` size, test coverage of the journal and restore paths

## observability
- globs: `observability/**`, `canary/**`, `ALERTING_SETUP.md`
- context: `docker-compose.yml`, `../revel-backend`
- risk: MEDIUM
- tier: sonnet
- security focus: PII and secrets in shipped log fields (Alloy allowlist, redaction), Pushover and Grafana credentials, canary account credentials
- debt focus: dead alerts (metrics that no longer exist), thresholds, docs that disagree with the rules

## repo-docs
- globs: `README.md`, `CLAUDE.md`, `LICENSE`, `.gitignore`, `geo-data/NOTICE`, `*/.gitkeep`
- risk: LOW
- tier: sonnet
- security focus: `.gitignore` covers `.env`, backups and rotation state
- debt focus: stale commands in the docs

## Specialists (debt sweeps, cross-cutting)
- dependencies: image tags in `docker-compose.yml`
- tests: `rotation/test_rotate.py`
- consistency: house rules across regions (the three Caddyfiles, wizard vs `.env.example` vs README)
