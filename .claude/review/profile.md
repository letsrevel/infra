# Gauntlet profile: infra

## About this codebase
Docker Compose deployment of Revel (an event management and ticketing platform) on one Hetzner host. Caddy is the edge proxy (TLS, on-demand certificates for organization custom domains, security headers, Cloudflare trusted proxies). Behind it run the Django backend (web, celery_default, beat, telegram), the SvelteKit frontend, PostgreSQL through PgBouncer (transaction pool mode), Redis (one instance, split by logical DB index) and ClamAV. The observability stack is Prometheus, Alertmanager (Pushover), Grafana, Loki, Tempo and Alloy, plus a synthetic login canary. Lifecycle is shell: `setup.sh` (install wizard that writes `.env`), `deploy.sh`, `deploy-rollout.sh` (zero-downtime with docker-rollout) and `safe-reboot.sh`. `rotation/` is a Python secret-rotation tool. No money moves in this repo, but it holds the payment, OAuth, SMTP and signing secrets, and it alerts on Stripe money-correctness counters. The users are the operator only; there is no tenancy in this repo beyond the custom-domain routing.

## Gate
1. `python3 -m unittest rotation/test_rotate.py` (CI: `.github/workflows/rotation.yml`)
2. `python3 rotation/rotate.py check` (CI: manifest drift check)
3. `shellcheck -x deploy-rollout.sh` (CI; needs `shellcheck` installed locally)
4. `bash -n <file>` on each changed `*.sh`
5. If `docker-compose.yml` or `.env.example` changed: `docker compose --env-file .env.example config --quiet`

The full rotation suite is fast; run all of it. Parallel test runs: none needed.

## Docs locations
- CHANGELOG: none
- Docs: `README.md` (Operations, Pinned versions), `rotation/README.md`, `ALERTING_SETUP.md`, `canary/README.md`, `observability/dashboards/README.md`
- Decisions: none in this repo (backend ADRs live in `../revel-backend`)
- Other: comments in `docker-compose.yml` and the Caddyfiles carry the reasoning for most settings

## House rules reviewers should check
See `CLAUDE.md` and `../CLAUDE.md`. Key points: never commit or push to main; no ssh/scp to the server without explicit permission; PR and issue text in ASD-STE100; Caddy must not buffer SSR streaming responses; keep the three Redis DB indices disjoint; `DB_CONN_MAX_AGE=0` with PgBouncer; `/metrics` stays blocked on the API host. The three Caddyfiles (`Caddyfile`, `Caddyfile.cloudflare`, `Caddyfile.generic`) must stay in step.

## Security triggers
`Caddyfile*`, `setup.sh`, `.env.example`, `rotation/**`, `docker-compose.yml`, `deploy.sh`, `deploy-rollout.sh`, `image-diff.sh`, `safe-reboot.sh`, `canary/**`, `observability/alloy-config.alloy` (PII in shipped logs), `observability/alertmanager-*`, `.github/workflows/**`. Keywords: `secret`, `token`, `password`, `key`, `webhook`, `trusted_proxies`, `on_demand`, `header`, `ports:`, `privileged`.

## Sibling repos
- `../revel-backend` (the image the compose file runs; settings, logging config, management commands)
- `../revel-frontend`
- `~/repos/personal/infra/revel` and `~/repos/personal/infra/caddy` (demo instance; read only; not on every machine, skip if missing)

## External reviewer and merge policy
- External reviewer: CodeRabbit. Budget: 2 reviews per rolling 60 minutes across revel-backend, revel-frontend and infra together. Space out PR opens.
- Reviewer login: `coderabbitai`
- Skip external review for: docs-only PRs (README, CLAUDE.md, other `*.md`)
- Merge: squash. Never merge without the user's explicit approval; the user usually merges.

## Agent memory
`.claude/agent-memory/<agent>/MEMORY.md` in the main checkout (gitignored).

## Debt metrics
- Files over 800 lines: `git ls-files '*.sh' '*.py' '*.yml' '*.alloy' 'Caddyfile*' | xargs wc -l | awk '$1 > 800 && $2 != "total"'`
