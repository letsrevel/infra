# By design: infra

## Old unsubscribe signing keys are kept
- behaviour: secret rotation never drops old `UNSUBSCRIBE_SIGNING_KEYS` entries automatically, so links already in mailboxes keep verifying. The list is long-lived.
- boundary: a run must not change a list that it did not create, and the `SECRET_KEY` / `SALT_KEY` fallbacks must still be finalized away.
- reference: `rotation/README.md` § Grace windows and why

## HighHTTP4xxRate ignores unrouted 404s and the TLS ask endpoint
- behaviour: the alert counts only routed views. It excludes `<unnamed view>` (scanner probes) and `api:tls_ask` (Caddy's on-demand TLS check answers 404 for junk SNI).
- boundary: 4xx responses from routed views must still count; no other view may be excluded without a reason.
- reference: `observability/alerts/application.yml` (HighHTTP4xxRate comment), PR #74

## CanaryLoginFailed can fire during a full-stack restart
- behaviour: a lockstep restart (apt upgrade, reboot) gives a short 502 while services start, and the canary can alert. This is accepted noise; the operator silences it before maintenance.
- boundary: a canary failure outside a restart window is a real incident; the alert must not be weakened to hide it.
- reference: `observability/alerts/frontend.yml` (CanaryLoginFailed), `canary/README.md`

## Infrastructure images are pinned and bumped by hand
- behaviour: caddy and pgbouncer use fixed tags and are upgraded deliberately in their own window, never by an app deploy.
- boundary: an app deploy must never change their tag (a `docker compose pull` of the same pinned tag is fine); only the app images follow the deploy.
- reference: `docker-compose.yml` (caddy and pgbouncer image comments), PR #39
