"""The secret catalogue: one entry per secret-bearing .env key.

Plain dataclasses, no parser. ``rotate.py check`` diffs this against ``.env.example`` and
the live ``.env`` so a newly added secret cannot be silently skipped by a rotation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta

GENERATED_WITH_FALLBACK = "generated_with_fallback"
GENERATED_FILE_WITH_INACTIVE = "generated_file_with_inactive"
LIST_APPEND = "list_append"
THIRD_PARTY = "third_party"
THIRD_PARTY_FILE = "third_party_file"
DB_PASSWORD = "db_password"
GRAFANA_ADMIN = "grafana_admin"

# Backend image services that read the Django env (SECRET_KEY, DB_*, ...).
BACKEND = ("web", "celery_default", "beat", "telegram")
WEB_CELERY = ("web", "celery_default")


@dataclass(frozen=True)
class Secret:
    name: str
    kind: str
    services: tuple[str, ...] = ()  # compose services to roll after a change
    grace: timedelta | None = None  # for kinds with fallbacks
    instructions: str = ""  # exact console steps (third-party kinds)
    fallback_var: str | None = None  # *_FALLBACKS / *_INACTIVE_PATHS companion variable
    extra_vars: tuple[str, ...] = ()  # other managed variables (not rotated themselves)
    migrate: bool = False  # needs `rotate_encrypted_fields` between stage and finalize
    bundle: str | None = None  # paired credentials: staged atomically, in one roll (see BUNDLES)

    @property
    def group(self) -> str:
        if self.kind in (GENERATED_WITH_FALLBACK, GENERATED_FILE_WITH_INACTIVE, GRAFANA_ADMIN):
            return "self"
        if self.kind == DB_PASSWORD:
            return "db"  # never part of a group: needs --maintenance and a typed confirmation
        return "thirdparty"


def _tp(name: str, services: tuple[str, ...], instructions: str, kind: str = THIRD_PARTY) -> Secret:
    return Secret(name, kind, services, instructions=instructions)


_NEW_KEY = "Create the new key in the provider console, then paste it here. Delete the old key after finalize."

_SECRETS: tuple[Secret, ...] = (
    Secret(
        "SECRET_KEY", GENERATED_WITH_FALLBACK, BACKEND, grace=timedelta(days=30),
        fallback_var="SECRET_KEY_FALLBACKS", extra_vars=("UNSUBSCRIBE_SIGNING_KEYS",), migrate=True,
        instructions="Grace 30 days = refresh-token lifetime. Unsubscribe links are pinned to the old key first.",
    ),
    Secret(
        "SALT_KEY", GENERATED_WITH_FALLBACK, BACKEND, grace=None,
        fallback_var="SALT_KEY_FALLBACKS", migrate=True,
        instructions="Grace ends when `migrate` has re-encrypted and verified every encrypted column.",
    ),
    Secret(
        "OIDC_SIGNING_KEY_PATH", GENERATED_FILE_WITH_INACTIVE, BACKEND, grace=timedelta(days=30),
        fallback_var="OIDC_SIGNING_KEYS_INACTIVE_PATHS",
        instructions="New RSA-2048 key next to the current one; the old one stays in JWKS until finalize.",
    ),
    Secret(
        "STRIPE_WEBHOOK_SECRETS", LIST_APPEND, WEB_CELERY, grace=timedelta(hours=24),
        instructions=(
            "Stripe dashboard -> Developers -> Webhooks -> each endpoint (platform and Connect) -> "
            "Roll secret, with an expiry (up to 24h) on the old one. Paste the new signing secret(s), "
            "comma-separated."
        ),
    ),
    _tp("STRIPE_SECRET_KEY", WEB_CELERY,
        "Stripe dashboard -> Developers -> API keys -> Roll key, expiry on the old key (up to 7 days)."),
    _tp("STRIPE_WEBHOOK_SECRET", WEB_CELERY,
        "Legacy single webhook secret (STRIPE_WEBHOOK_SECRETS takes precedence). Roll it as for the list."),
    _tp("EMAIL_HOST_PASSWORD", WEB_CELERY,
        "Brevo -> SMTP & API -> SMTP -> generate a new SMTP key (several can coexist). Paste it here."),
    _tp("EMAIL_WEBHOOK_SECRET", ("web",),
        "Pick a new random value, paste it here, then update the Brevo webhook URL "
        "(https://revel:<secret>@<API_DOMAIN>/api/email-events/brevo) to use it."),
    _tp("GOOGLE_SSO_CLIENT_SECRET", ("web",),
        "Google Cloud console -> APIs & Services -> Credentials -> OAuth client -> add a second secret."),
    _tp("OIDC_GOOGLE_CLIENT_SECRET", ("web",),
        "Google Cloud console -> APIs & Services -> Credentials -> OAuth client -> add a second secret."),
    _tp("CLOUDFLARE_API_TOKEN", WEB_CELERY,
        "Cloudflare -> My Profile -> API Tokens: create the replacement (Zone -> SSL and Certificates -> Edit "
        "on the zone), then delete the old one after finalize."),
    _tp("TURNSTILE_SECRET_KEY", ("web",),
        "Cloudflare -> Turnstile -> widget -> Rotate secret key. Check whether the old key keeps a grace period."),
    _tp("LLM_API_KEY", WEB_CELERY, _NEW_KEY),
    _tp("OPENAI_API_KEY", WEB_CELERY, _NEW_KEY),
    _tp("HUGGING_FACE_HUB_TOKEN", WEB_CELERY, "huggingface.co -> Settings -> Access Tokens: create a new token."),
    _tp("IP2LOCATION_TOKEN", WEB_CELERY, "IP2Location account -> download token: generate a new one."),
    _tp("INTEGRATIONS_EVENTBRITE_CLIENT_SECRET", WEB_CELERY,
        "Eventbrite -> Account settings -> Developer links -> API keys -> app -> rotate the client secret "
        "(verify first whether two can coexist; a short connect blip is possible)."),
    _tp("AWS_ACCESS_KEY_ID", WEB_CELERY,
        "Verify these are still used; if not, delete them instead. Otherwise create a new IAM access key "
        "and rotate the ID and the secret together (stage both names)."),
    _tp("AWS_SECRET_ACCESS_KEY", WEB_CELERY, "See AWS_ACCESS_KEY_ID."),
    _tp("PUSHOVER_APP_TOKEN", ("alertmanager",) + WEB_CELERY,
        "Pushover -> Your Applications -> create a new application, paste its API token. Delete the old app after."),
    _tp("PUSHOVER_USER_KEY", ("alertmanager",) + WEB_CELERY,
        "Pushover dashboard -> Reset user key (the old one stops working at once)."),
    _tp("DISCORD_ADMIN_WEBHOOK_URL", WEB_CELERY,
        "Discord -> channel settings -> Integrations -> Webhooks: create a new webhook, paste its URL, delete the old."),
    _tp("DISCORD_BACKEND_WEBHOOK_URL", (),
        "Same as DISCORD_ADMIN_WEBHOOK_URL. Read by deploy scripts on the host only: nothing to roll."),
    _tp("DISCORD_FRONTEND_WEBHOOK_URL", (),
        "Same as DISCORD_ADMIN_WEBHOOK_URL. Read by deploy scripts on the host only: nothing to roll."),
    _tp("TELEGRAM_BOT_TOKEN", ("telegram",),
        "BotFather -> /revoke. The old token dies INSTANTLY: the bot is offline until the `telegram` "
        "container restarts (seconds). Only `telegram` is rolled; web is unaffected."),
    _tp("INDEXNOW_KEY", ("frontend",),
        "Pick a new key (8-128 chars, a-z A-Z 0-9 -). The frontend serves it as a key file for search engines."),
    _tp("INDEXNOW_TRIGGER_SECRET", ("frontend",), "Pick a new random value and paste it here."),
    _tp("CANARY_PASSWORD", ("canary",),
        "FIRST change the canary account's password in the app (log in as it, or reset it in the Django admin), "
        "then paste the same new password here."),
    _tp("GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH", BACKEND,
        "GCP console -> IAM -> Service Accounts -> Keys -> Add key (JSON). Copy it into ./certs, chmod 644, "
        "then enter its container path (/app/certs/<file>).", THIRD_PARTY_FILE),
    _tp("APPLE_WALLET_CERT_PATH", BACKEND,
        "Apple developer -> new Pass Type ID certificate. Place the PEM in ./certs, chmod 644, enter its "
        "container path. Stage APPLE_WALLET_KEY_PATH and APPLE_WALLET_KEY_PASSWORD with it.", THIRD_PARTY_FILE),
    _tp("APPLE_WALLET_KEY_PATH", BACKEND,
        "Matching private key for the new Pass Type ID certificate: ./certs, chmod 644, container path.",
        THIRD_PARTY_FILE),
    _tp("APPLE_WALLET_KEY_PASSWORD", BACKEND, "Password of the new Apple Wallet private key."),
    Secret("DB_PASSWORD", DB_PASSWORD, BACKEND,
           instructions="Maintenance window, not zero-downtime. ALTER ROLE, then pgbouncer, exporter, app."),
    Secret("GRAFANA_ADMIN_PASSWORD", GRAFANA_ADMIN, ("grafana",),
           instructions="Reset through the grafana CLI (the env var only applies at first init)."),
)

# Credentials that only work as a set: naming any member means naming them all.
BUNDLES: dict[str, tuple[str, ...]] = {
    "aws": ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"),
    "apple_wallet": ("APPLE_WALLET_CERT_PATH", "APPLE_WALLET_KEY_PATH", "APPLE_WALLET_KEY_PASSWORD"),
}
_BUNDLE_OF = {n: b for b, names in BUNDLES.items() for n in names}

MANIFEST: dict[str, Secret] = {s.name: replace(s, bundle=_BUNDLE_OF.get(s.name)) for s in _SECRETS}

# Keys that look secret but are not (or are managed outside the tool).
EXEMPT_NAMES = frozenset({
    "TURNSTILE_SITE_KEY",  # public widget key
    "ROTATION_AGE_RECIPIENT",  # age public key
})
EXEMPT_PATTERNS = ("_LIFETIME_",)  # ACCESS_TOKEN_LIFETIME_HOURS etc.


def known_names() -> set[str]:
    names: set[str] = set(EXEMPT_NAMES)
    for s in _SECRETS:
        names.add(s.name)
        names.update(s.extra_vars)
        if s.fallback_var:
            names.add(s.fallback_var)
    return names
