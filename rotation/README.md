# Secret rotation

Routine rotation of every secret in a Revel Compose deployment, with overlap windows so web
stays up. Python 3 standard library only. The catalogue of secrets is `manifest.py`; the CLI is
`rotate.py`. Values are never printed, logged, put in argv or written to `state.json`.

Run it on the host, from the deploy dir:

```bash
ssh -t revel 'cd infra && ./rotation/rotate.py status'
```

`-t` is needed: the tool prompts (hidden input) for third-party values and confirmations.

## Prerequisites

- `python3` >= 3.10 on the host (no third-party packages).
- The `docker rollout` plugin (`./deploy-rollout.sh install`).
- `age` and a recipient public key in `ROTATION_AGE_RECIPIENT` (`.env` or the environment), to
  archive retired secrets. Without them `finalize` refuses unless you pass `--no-archive`.
- A backend release that ships key fallbacks (`SECRET_KEY_FALLBACKS`, `SALT_KEY_FALLBACKS`,
  `UNSUBSCRIBE_SIGNING_KEYS`, `rotate_encrypted_fields`; backend #1283). `stage` of `SECRET_KEY` or
  `SALT_KEY` checks that `manage.py help rotate_encrypted_fields` works in the running `web`.
- A deploy dir with `docker-compose.yml`, `.env` and `deploy-rollout.sh`. Prod: `~/infra`
  (the tool defaults to the parent of `rotation/`). Demo: `~/infra/revel`, run
  `./rotation/rotate.py --dir revel ...`; that dir needs `deploy-rollout.sh`, `notify-discord.sh`
  and `image-diff.sh` next to its compose file (symlinks are fine). State and archives live in
  `<dir>/rotation/`.

## Commands

| Command | What it does |
|---|---|
| `plan` | Per secret: kind, set/unset, step, grace end, services that get rolled. |
| `status` | Same without the services column. |
| `check [--example PATH]` | Drift check. Fails (exit 1) on any key matching `SECRET\|TOKEN\|KEY\|PASSWORD\|WEBHOOK_URL` in `.env.example` (commented ones included) or the live `.env` that is not in the manifest. Lists manifest secrets that are not set in `.env`. Runs in CI against `.env.example`. |
| `stage <name...>` / `stage --group self\|thirdparty\|all` | Back up `.env` (`.env.bak.<ts>`, 0600), edit only the target lines, record the step, roll the affected services, `verify`. A failed verify restores that secret and rolls again; if that rollback itself cannot complete (the live credential is not restorable, or the recovery roll fails), the secret stays `staged` and the command exits non-zero (see the rollback rules below). `--batch` rolls and verifies once for the whole invocation (on failure it rolls each secret back, one by one: a secret whose rollback fails is reported, stays `staged` and must be rolled back again with `rollback <name>` (`rollback <bundle>` for a bundle member), and the exit code is non-zero); it does not apply to `SECRET_KEY` / `SALT_KEY`, which always roll twice. Unset secrets are skipped, except `STRIPE_WEBHOOK_SECRETS` (a list), which is staged even when unset: it is seeded from the legacy `STRIPE_WEBHOOK_SECRET` so no endpoint loses its secret before the grace period. Pressing Enter at a hidden prompt skips that secret (`skipped <NAME>`, not an error; for a bundle, the whole bundle). Paired credentials are bundles (below). `DB_PASSWORD` is refused in `--batch` and next to any other secret. |
| `migrate` | `rotate_encrypted_fields`, then `rotate_encrypted_fields --verify`, for staged `SECRET_KEY` / `SALT_KEY`. |
| `verify` | Health, API, frontend, `manage.py check`, error-line budget (below). |
| `finalize <name...> [--force] [--no-archive]` | After the grace window. Order: trial-encrypt to the recipient, **archive the retired values with `age`**, then edit `.env`, roll, verify, then delete the run's `.env.bak.*` (and, for OIDC, the retired PEM, only if it was archived and sits inside `certs/`). An archive failure leaves `.env`, the state and the backups untouched. If verify fails after the edit, the old values are restored, the plaintext copy is deleted and the roll is repeated; if that roll fails too the tool says the services are out of sync and prints the command to re-roll. |
| `rollback <name...>` | Restore the secret's previous values from its backup (only while `staged`). See the rollback rule below; `TELEGRAM_BOT_TOKEN` is forward-only and `rollback` refuses it. A bundle is rolled back as a whole: name the bundle (`rollback aws`) or every member. Members already rolled back are skipped, so the same command retries a bundle that got stuck halfway. |

Groups: `self` = `SECRET_KEY`, `SALT_KEY`, OIDC key, Grafana password. `thirdparty` = everything
rolled in a provider console, plus `STRIPE_WEBHOOK_SECRETS`, except `TELEGRAM_BOT_TOKEN`. `DB_PASSWORD` and
`TELEGRAM_BOT_TOKEN` are in no group: they are staged by name only.

### Two-phase stage for `SECRET_KEY` / `SALT_KEY`

During a rollout old containers (which only know the old key) run next to new ones. The
encrypted-fields library swallows `InvalidToken` and returns the ciphertext as the value, so an
old container saving a row that a new one wrote under the new key would corrupt it for good.
`stage` therefore rolls twice:

1. **Introduce**: the new key is appended to `*_FALLBACKS`, the old key stays primary (for
   `SECRET_KEY` the unsubscribe pin is introduced here, too). Roll, verify. Every container can now read the new key; nothing is written with it.
2. **Promote**: the new key becomes primary and the old key goes FIRST in `*_FALLBACKS`, the rest of the list kept. Roll, verify.

Both phases are recorded in `state.json` (`phase`, shown in `status`). If a run stops after phase 1,
running `stage` again resumes with the promotion and reuses the introduced key. `migrate` and
`finalize` require phase 2 to be complete.

**Rollback rule: a key that has ever been introduced as primary is never removed.** Rolling back
phase 1 (nothing was ever written with the new key) restores the old config. Rolling back phase 2,
manually or after a failed verify, makes the old key primary again and KEEPS the new key in
`*_FALLBACKS`; its `.env.bak.*` is kept until `finalize`. After that, `rollback` is refused: re-run
`stage` to promote again.

If the live credential (`DB_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`) cannot be restored, `rollback`
aborts before touching `.env` or rolling anything: the credential is still the new one, `.env` and the
state still match it, and you re-run `rollback` once the service is back.

If the recovery roll fails after `.env` was restored, `rollback` raises as well: `.env` holds the old value,
but the services may still run the new one. The secret stays `staged` and no backup is deleted (nor the new
OIDC key file), so re-run `rollback <name>` once the rollout works. Every step is idempotent (credential reset
to the old value, `.env` rewritten from the same backup, roll). A bundle is rolled back member by member and a
failing member does not stop the others; re-run `rollback <bundle>` (e.g. `rollback aws`): members already rolled back (`idle`) are skipped with `already rolled back, skipping`, and the stuck ones are retried. Naming only some members is refused, as for `stage`.

### Bundles

Credentials that only work as a set are staged atomically, with one backup and one roll, and
rolled back together: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (`aws`), and
`APPLE_WALLET_CERT_PATH` + `APPLE_WALLET_KEY_PATH` + `APPLE_WALLET_KEY_PASSWORD` (`apple_wallet`).
Naming only some members is an error that names the bundle. If a required member is unset in `.env` the whole bundle is skipped (`skipped bundle <name>: <member> is unset, nothing was changed`); only optional members (`APPLE_WALLET_KEY_PASSWORD`, a key may have no password) may be unset.

States per secret: `idle -> staged -> (migrated) -> finalized`, and `staged -> idle` on rollback.
`SECRET_KEY` and `SALT_KEY` cannot finalize before `migrate` (not even with `--force`).
One-shot secrets (a Stripe key, a token) sit in `staged` until you `finalize` them, which archives
the old value and deletes the plaintext `.env.bak.*`; do this once the old credential is revoked.

### Forward-only: `TELEGRAM_BOT_TOKEN`

BotFather `/revoke` kills the old token the moment you do it, so the old value cannot be restored.
`stage TELEGRAM_BOT_TOKEN` (by name, never in a group or `--batch`) asks you to type
`TELEGRAM REVOKED` once you have revoked, then for the new token. If `verify` fails, the new token is
KEPT in `.env` and the tool prints the recovery steps (check the token, then
`./deploy-rollout.sh roll telegram` and `rotate.py verify`). `rollback TELEGRAM_BOT_TOKEN` is refused.

### Verify

Each probe prints PASS/FAIL, never a value:

1. Rolled services are `running` and healthy (`docker compose ps --format json`; retried for 2 min).
2. `GET /api/version` inside `web` (compose publishes no host ports, so probes run in the containers).
3. Frontend `GET /` inside the `frontend` container (same check as its healthcheck).
4. `docker compose exec -T web python manage.py check` exits 0; the warning IDs it prints are shown, and `oauth.W002` (unreadable OIDC key) fails.
5. Lines at ERROR/CRITICAL in `docker compose logs --since <rollout start> web celery_default` at most 5 (`verify --error-threshold N`, inclusive).
6. Advisory: `curl -fsS https://$API_DOMAIN/api/version` from the host.

## Grace windows and why

| Secret | Window | Reason |
|---|---|---|
| `SECRET_KEY` | 30 days | Refresh-token lifetime: sessions signed with the old key keep refreshing. Also needs `migrate`. |
| `SALT_KEY` | until `migrate` verifies | Only encrypted columns depend on it; once every row is re-encrypted the old salt is dead weight. |
| OIDC signing key | 30 days | The old public key stays in JWKS until ID tokens it signed have expired. |
| `STRIPE_WEBHOOK_SECRETS` | 24 hours | Stripe lets the rolled secret live up to 24h. Set that expiry in the dashboard. |
| everything else | none | Providers handle the overlap (expiry on the old key); `finalize` is only housekeeping. |

`UNSUBSCRIBE_SIGNING_KEYS` follows the same introduce/promote pattern. If it is empty when
`SECRET_KEY` phase 1 runs, the tool pins `<current SECRET_KEY>,<new random>`: the old key stays
first, so containers still sign with it and old containers still verify, while every container
learns the new one. In phase 2 it is reordered to `<new random>,<old SECRET_KEY>` (further entries
kept in order), so links already in mailboxes keep verifying after the `SECRET_KEY` fallback is
finalized away. A list that was not created by this run is never touched, and old unsubscribe keys
are never dropped automatically (the list is long-lived by design). Rolling back phase 1 removes a
pin created in that run; after phase 2 the pin is kept as it is.

## First-run order

Run on **demo first, always**, smoke-test it, then prod. Put first whatever invalidates the most,
so the grace windows overlap.

0. `./deploy.sh backup`; `rotate.py check` must be green on demo and prod. Set `ROTATION_AGE_RECIPIENT`.
1. `rotate.py stage SECRET_KEY SALT_KEY`. A session obtained before still works; new logins work.
2. `rotate.py migrate` (needs phase 2 done; re-encrypts TOTP secrets and integration tokens, then verifies with the primary key only).
3. Third parties, in parallel with the windows running: follow each secret's printed console steps.
   `rotate.py stage --group thirdparty --batch` (Stripe key and webhook secrets, Brevo SMTP key,
   Google OAuth secrets, GCP service-account key, Cloudflare token, LLM / Hugging Face / IP2Location
   keys, Pushover, Discord, IndexNow, Eventbrite; not Telegram). At each hidden prompt paste the new
   value, or press Enter to skip that secret (its console step is not done yet). One `--batch` rolls once.
4. `rotate.py stage TELEGRAM_BOT_TOKEN` last, by name (it is in no group). It is NOT reversible: revoke in BotFather first, then type `TELEGRAM REVOKED` and paste the new token. A failed verify keeps the new token and prints manual recovery steps. Only `telegram` restarts.
5. `DB_PASSWORD` (below), in its window.
6. `rotate.py stage OIDC_SIGNING_KEY_PATH`. Skipped automatically when unset.
7. After the grace windows, run `rotate.py status` and finalize only the names it shows as `staged` or
   `migrated` (secrets skipped earlier are `idle`): for example `rotate.py finalize SECRET_KEY SALT_KEY
   OIDC_SIGNING_KEY_PATH STRIPE_WEBHOOK_SECRETS`, then the one-shot secrets once their old credentials are
   revoked. In a multi-name `finalize`, a name that is `idle` or already `finalized` is a noted no-op; a
   single idle name is an error. Let the old Stripe keys expire.
8. `rotate.py check`, `rotate.py status`. Done. Calendar a reminder for the next round (and the Apple Wallet certificate expiry).

Rotate `GRAFANA_ADMIN_PASSWORD` any time: `rotate.py stage GRAFANA_ADMIN_PASSWORD` resets it with
`grafana cli admin reset-admin-password --password-from-stdin` (the env var only applies at first
init) and updates `.env`. Nothing is restarted.

## DB password (maintenance window)

Not zero-downtime, by decision. `POSTGRES_PASSWORD` only applies when the volume is initialised,
so changing `.env` alone does nothing.

```bash
ssh -t revel 'cd infra && ./rotation/rotate.py stage DB_PASSWORD --maintenance'
```

You must type `ROTATE DB PASSWORD`. The tool then: backs up `.env`; runs `ALTER ROLE <DB_USER> PASSWORD ...`
through `psql` in `revel_postgres` with a SCRAM-SHA-256 verifier computed on the host (stdin, never argv;
the plaintext never reaches the server, whose `log_min_error_statement` would write a failed statement to its log); writes `DB_PASSWORD`; recreates
`pgbouncer` and `postgres-exporter` (`up -d --no-deps --force-recreate`); rolls `web`, `celery_default`,
`beat`, `telegram`; verifies. On failure it sets the old password back and rolls again. Expect a few
seconds of errors while the pooler restarts: schedule it for a quiet hour.

`docker-compose.yml` feeds `DB_PASSWORD` to `revel_postgres` as `POSTGRES_PASSWORD`, so the next full
`docker compose up -d` (for example `deploy.sh update`) will see a changed config on that container and
recreate it. Do not run that casually afterwards; use `deploy-rollout.sh deploy` / `roll`.

## Disaster recovery: restoring a pre-rotation backup

Columns in an old dump are encrypted under the `SECRET_KEY` x `SALT_KEY` of that time, and the
retired values are in `rotation/archive/*.age`.

1. From the infra root, copy the retired keys into the `*_FALLBACKS` variables of `.env`. List `rotation/archive/*.age` and pick **every**
   file dated after the backup was taken (by the date in its name; do not assume the names): `finalize` writes one file per secret,
   so rotating both keys leaves two files, and one file can hold several retired values. Every retired
   `SECRET_KEY` / `SALT_KEY` found is added (an old dump decrypts under any of them) and values already in a
   `*_FALLBACKS` variable are kept. The secrets never reach the terminal: the archives are decrypted into a fresh
   `mktemp -d` directory (mode 0700) that the subshell deletes on exit, and only counts are printed. If no key is
   found, `.env` is left unchanged and the step fails.

   ```sh
   ( d=$(mktemp -d) && trap 'rm -rf "$d"' EXIT || exit 1
     for f in rotation/archive/<file>.age rotation/archive/<file>.age; do
       age -d -i <your identity> -o "$d/$(basename "$f").env" "$f" || exit 1
     done
     python3 - "$d"/*.env <<'PY'
   import sys
   from pathlib import Path
   sys.path.insert(0, "rotation")
   from rotate import get_env, set_env, write_atomic
   lines = [line for p in sys.argv[1:] for line in Path(p).read_text().splitlines()]
   env = Path(".env")
   text, found = env.read_text(), 0
   for key in ("SECRET_KEY", "SALT_KEY"):
       retired = list(dict.fromkeys(v for line in lines if (v := get_env(line, key))))
       current = [v for v in (get_env(text, f"{key}_FALLBACKS") or "").split(",") if v]
       if retired:
           text = set_env(text, f"{key}_FALLBACKS", ",".join(current + [v for v in retired if v not in current]))
       found += len(retired)
       print(f"{key}_FALLBACKS: {len(retired)} retired value(s) found")
   if not found:
       sys.exit("no SECRET_KEY / SALT_KEY in these archives: .env NOT changed")
   write_atomic(env, text)
   PY
   )
   ```
2. Restore the dump.
3. Roll: `./deploy-rollout.sh roll web celery_default beat telegram`.
4. `docker compose exec -T web python manage.py rotate_encrypted_fields`, then `... --verify`.
5. Remove the retired values from the two `*_FALLBACKS` variables again and roll.

Each `finalize` writes its own age file, created exclusively with mode 0600 (UTC): `<YYYY-MM-DD>.age`, or
`<YYYY-MM-DD-HHMMSS>-<NAME>.age` when that exists, or `<YYYY-MM-DD-HHMMSS>-<NAME>-<N>.age` when that exists too.
age streams cannot be concatenated, so nothing is appended. Archived OIDC keys appear as
`OIDC_SIGNING_KEY_FILE:<path>=<base64 PEM>`. Back the archive up off the host.

## Warning: do not re-run `setup.sh` on a rotated install

`setup.sh` regenerates `SECRET_KEY` and `SALT_KEY` and rewrites `.env` (the old file is kept as a
backup). On an install that has been rotated that discards the current keys, the fallbacks and the
unsubscribe pin, and makes encrypted columns unreadable. Restore the needed values from that backup
or from the `age` archive instead of re-running the wizard.

## Tests

```bash
python3 -m unittest rotation/test_rotate.py
```

Docker is mocked entirely. Also run in CI (`.github/workflows/rotation.yml`) together with `rotate.py check`.
