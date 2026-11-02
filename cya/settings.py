"""Django settings for Check Your Ambulance.

All deployment-specific values come from CYA_* environment variables, so no
configuration or secret is committed (SPEC 17.6). A missing required value
stops startup with ImproperlyConfigured instead of falling back to a default.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    """Return CYA_<name>, or default; raise if it is unset and has no default."""
    value = os.environ.get(f"CYA_{name}")
    if value is None or value == "":
        if default is None:
            raise ImproperlyConfigured(f"Environment variable CYA_{name} is required.")
        return default
    return value


def env_int(name, default, minimum, maximum):
    """Parse an optional integer CYA_<name> within [minimum, maximum]."""
    value = env(name, str(default)).strip()
    if not value.isdigit() or not minimum <= int(value) <= maximum:
        raise ImproperlyConfigured(f"CYA_{name} must be an integer from {minimum} to {maximum}.")
    return int(value)


def env_flag(name):
    """Parse an optional boolean CYA_<name>; only 0/1/true/false are accepted."""
    value = env(name, "0").strip().lower()
    if value not in ("0", "1", "true", "false"):
        raise ImproperlyConfigured(f"CYA_{name} must be 0, 1, true or false, not {value!r}.")
    return value in ("1", "true")


SECRET_KEY = env("SECRET_KEY")
CYA_DATABASE_ROLE = env("DATABASE_ROLE", "runtime")
if CYA_DATABASE_ROLE not in {"runtime", "schema_owner"}:
    raise ImproperlyConfigured("CYA_DATABASE_ROLE must be runtime or schema_owner.")
DEBUG = env_flag("DEBUG")
ALLOWED_HOSTS = [h.strip() for h in env("ALLOWED_HOSTS", "localhost").split(",") if h.strip()]
if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "CYA_ALLOWED_HOSTS must be explicit so WebSocket origins can be checked."
    )

INSTALLED_APPS = [
    "channels",
    # Workspace sign-in and identity anchors live in accounts (issue 21 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/21)); role
    # evidence and server-side authorization belong to authorization (issue 22 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/22)).
    # Conditional rules and roster integration belong to conditions (issue 58 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/58)).
    # Obligations, their selection stream and reconciliation belong to obligations (issue 59 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/59)).
    # Check records and the record-check command belong to checks (issue 73 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/73)).
    # Odometer entries, anomaly confirmation and obligation satisfaction belong to odometer (issue 79 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/79)).
    # Kit assignment history and its commands belong to kits (issue 92 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/92)).
    # Deficiencies, evidence, links and their creation belong to deficiencies (issue 80 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/80)).
    "cya.core",
    "cya.authorization",
    "cya.accounts",
    "cya.checklists",
    "cya.schedules",
    "cya.conditions",
    "cya.operations",
    "cya.notifications",
    "cya.obligations",
    "cya.checks",
    "cya.deficiencies",
    "cya.attention",
    "cya.odometer",
    "cya.kits",
    "cya.inspections",
    "cya.shell",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "cya.accounts.middleware.CurrentUserMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Sessions (SPEC 16.2, 17.7 item 5; AUTH-4). Signed-cookie sessions need no
# session table. Every request re-checks the user (cya.accounts.middleware), and
# a session older than CYA_SESSION_MAX_AGE seconds requires signing in again
# with Google, which bounds how long a Workspace-disabled account keeps access.
CYA_SESSION_MAX_AGE = env_int("SESSION_MAX_AGE", 8 * 3600, 300, 24 * 3600)
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_NAME = "cya_session"
SESSION_COOKIE_AGE = CYA_SESSION_MAX_AGE
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "Strict"
# JSON refusal for /api/ routes; HTML pages keep Django's default page.
CSRF_FAILURE_VIEW = "cya.checks.api.csrf_failure"

# HTTPS behind the TLS-terminating reverse proxy (SPEC DEP-10; issue 25). All off by default, so
# development and tests are unchanged; a deployment turns them on explicitly
# (docs/deployment-runbook.md, "Proxy and application service").
# CYA_TRUST_PROXY_SSL_HEADER=1 trusts X-Forwarded-Proto. That is safe only when the proxy
# overwrites the header on every request and the application is unreachable except through it
# (loopback or a Unix socket, DEP-10); otherwise a client could claim HTTPS.
# CYA_SSL_REDIRECT=1 and CYA_HSTS_SECONDS > 0 need that trust: behind the proxy every request
# reaches the application as plain HTTP, so a redirect would loop and HSTS would never be sent.
# HSTS starts short (the runbook starts at 300 seconds) and grows only after HTTPS sign-in and
# forms are checked; subdomains and preload stay off unless the owner approves them.
CYA_TRUST_PROXY_SSL_HEADER = env_flag("TRUST_PROXY_SSL_HEADER")
CYA_SSL_REDIRECT = env_flag("SSL_REDIRECT")
CYA_HSTS_SECONDS = env_int("HSTS_SECONDS", 0, 0, 63072000)
if (CYA_SSL_REDIRECT or CYA_HSTS_SECONDS) and not CYA_TRUST_PROXY_SSL_HEADER:
    raise ImproperlyConfigured(
        "CYA_SSL_REDIRECT and CYA_HSTS_SECONDS need CYA_TRUST_PROXY_SSL_HEADER=1 behind the proxy."
    )
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if CYA_TRUST_PROXY_SSL_HEADER else None
)
SECURE_SSL_REDIRECT = CYA_SSL_REDIRECT
SECURE_HSTS_SECONDS = CYA_HSTS_SECONDS
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

# Google Workspace sign-in (cya.accounts.oidc). Unset values make sign-in
# unavailable (fail closed); the rest of the application still starts.
CYA_GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", "")
CYA_GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET", "")
CYA_GOOGLE_HOSTED_DOMAIN = env("GOOGLE_HOSTED_DOMAIN", "")
CYA_GOOGLE_REDIRECT_URI = env("GOOGLE_REDIRECT_URI", "")

# Notification email (cya.notifications). Unset values leave email intents queued and visible
# to administrators; nothing is hard-coded. SENDER is a dotted path to a zero-argument factory.
CYA_PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "")
CYA_NOTIFICATION_SENDER = env("NOTIFICATION_SENDER", "")
CYA_NOTIFICATION_MAX_ATTEMPTS = env_int("NOTIFICATION_MAX_ATTEMPTS", 5, 1, 20)
CYA_NOTIFICATION_RETRY_SECONDS = env_int("NOTIFICATION_RETRY_SECONDS", 60, 1, 86400)

ROOT_URLCONF = "cya.urls"
ASGI_APPLICATION = "cya.asgi.application"

# DEP-2 assumes one application process initially. The in-memory layer is
# process-local; a multi-process deployment must select a shared backend.
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
CYA_LIVE_MAX_CONNECTIONS_PER_USER = env_int("LIVE_MAX_CONNECTIONS_PER_USER", 4, 1, 32)
CYA_LIVE_AUTH_RECHECK_SECONDS = env_int("LIVE_AUTH_RECHECK_SECONDS", 60, 10, 300)
CYA_LIVE_MAX_MESSAGE_BYTES = 64 * 1024

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["cya.shell.context.shell"]},
    }
]

# Every connection is configured, then verified by cya.core.db (SPEC 17.7
# item 4; docs/data-model-contracts.md section 3).
DATABASE_INIT_COMMAND = (
    "SET time_zone = '+00:00', "
    "sql_mode = 'STRICT_ALL_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ZERO_DATE,"
    "NO_ZERO_IN_DATE,NO_ENGINE_SUBSTITUTION', "
    "foreign_key_checks = 1, check_constraint_checks = 1"
)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env("DB_NAME", "cya"),
        "USER": env("DB_USER"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST", "127.0.0.1"),
        "PORT": env("DB_PORT", "3306"),
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": DATABASE_INIT_COMMAND,
            "isolation_level": "read committed",
        },
        "TEST": {
            "NAME": env("DB_TEST_NAME", "test_cya"),
            # Match tools/dev-db/accounts.sql; later issues may refine collation.
            "CHARSET": "utf8mb4",
            "COLLATION": "utf8mb4_unicode_ci",
        },
    }
}

# Store and compute in UTC; the station display zone is a later decision (OQ-25).
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"
USE_I18N = False

TEST_RUNNER = "cya.testing.runner.OfflineTestRunner"

# Dotted paths to factories for external-service adapters (cya.integrations).
# None means "not configured", which fails closed; tests inject stubs.
CYA_OIDC_PROVIDER = "cya.accounts.oidc.google_provider"
CYA_ROSTER_PROVIDER = None
# Sign-in collaborators; the role callable also needs the backend below.
# CYA_INITIAL_ROLE_GRANTER: grants Crew to a newly provisioned user (issue 22 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/22) / SPEC 1.4.0).
# CYA_AUTH_AUDIT_SINK: records sign-in audit events durably (issue 23 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/23)).
CYA_INITIAL_ROLE_GRANTER = "cya.authorization.roles.grant_initial_crew"
CYA_AUTH_AUDIT_SINK = "cya.core.auth_audit.record_auth_event"
# Factory for current role evidence, atomic role appends and durable denials.
# Missing/failed storage remains fail-closed; there is no in-memory fallback.
CYA_AUTHORIZATION_BACKEND = "cya.authorization.persistence.DatabaseRoleBackend"
# Current uncached contract role codes for the shell and authentication audit.
CYA_ROLE_LOOKUP = "cya.authorization.persistence.current_role_codes"
# Collaborator for current item-check evidence used by conditional carry-over
# evaluation and waiver re-evaluation (issue 58 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/58); concrete check storage arrives with the
# check-recording work, not with obligation generation).
# None means "not configured", which fails closed; there is no process-local fallback.
CYA_PRIOR_CHECK_LOOKUP = None
# Obligation generation window (issue 59 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/59), SPEC TIME-1): hours ahead to materialize, and hours
# back to repair after missed job runs. The generate_obligations command can override both.
CYA_OBLIGATION_HORIZON_HOURS = 48
CYA_OBLIGATION_LOOKBACK_HOURS = 168
