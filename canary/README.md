# Login canary

`login_canary.py` performs a **real** end-to-end login through the public edge
(Cloudflare → Caddy → SvelteKit → backend) every 10 minutes and exposes the
result as Prometheus metrics on `:8080/metrics`. It exists because the
2026-06-10 → 06-12 outage (SSR dropping POST bodies, revel-frontend #398) broke
login for ~2 days while producing zero error-level events — only a synthetic
login deterministically catches that class of failure.

Stdlib only; runs in a plain `python:3.13-slim` container with the script
bind-mounted (no image build, no registry push).

## Metrics

| Metric | Type | Meaning |
|---|---|---|
| `canary_login_last_success_timestamp_seconds` | gauge | Unix time of the last successful login (`0` until the first success — so a broken-from-boot login pages). Drives `CanaryLoginFailed`. |
| `canary_login_failures_total` | counter | Total failed cycles. |
| `canary_login_attempts_total` | counter | Total cycles. |
| `canary_login_last_duration_seconds` | gauge | Duration of the last attempt. |

`CanaryLoginFailed` (critical) fires when `time() - last_success > 1800` — i.e.
~3 consecutive failed 10-min cycles, tolerant of a single network blip.

## Configuration (infra `.env`)

```
CANARY_EMAIL=canary@letsrevel.io
CANARY_PASSWORD=<high-entropy password>
# optional:
CANARY_BASE_URL=https://letsrevel.io   # default
CANARY_INTERVAL=600                     # seconds, default 600 (stays under the 250/day anon throttle)
```

### Canary account requirements

- A **dedicated** prod user (e.g. `canary@letsrevel.io`), no org/staff roles, no memberships.
- **2FA MUST be disabled** — a 2FA-gated login returns HTTP 200 with a temp token
  rather than the 303 redirect the canary treats as success, so it would always
  read as failed.
- Random high-entropy password, stored only in infra `.env` (never in git).

## Success criterion

The canary asserts:
1. `GET /login` → `200` (page renders through the edge), then
2. `POST /login?/login` (form-encoded `email`+`password`, `Origin` header set for
   SvelteKit's CSRF guard) → `303` with a `Location` that isn't `/login`.

Only a `303` is success: `fail()` → `400`, a returned object (2FA) → `200`.

## Local test (before prod)

```bash
# Point at a locally-running frontend+backend with a seeded account:
CANARY_BASE_URL=http://localhost:3000 \
CANARY_EMAIL=you@example.com CANARY_PASSWORD=secret \
CANARY_INTERVAL=15 python3 canary/login_canary.py

# In another shell:
curl -s localhost:8080/metrics
```

The script picks HTTP vs HTTPS from the `CANARY_BASE_URL` scheme, so an
`http://localhost:3000` target works for local testing.
