"""
Django settings for django-boundary standalone tests.

Used by the publish workflow (CI) and for running tests independently
of the monorepo sandbox settings. Requires PostgreSQL for RLS tests.
"""

import os

SECRET_KEY = "boundary-test-secret-key"  # noqa: S105

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "boundary",
    "boundary_testapp",
]

# PostgreSQL required — RLS tests use raw SQL against pg_class.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "boundary_test"),
        "USER": os.environ.get("POSTGRES_USER", "icv_test"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "icv_test_password"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

MIGRATION_MODULES = {
    "boundary": None,
    "boundary_testapp": None,
    "contenttypes": None,
    "auth": None,
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

ALLOWED_HOSTS = ["*"]

# Only the ASGI integration test in test_middleware.py needs a URLconf (it
# drives an async view through django.test.AsyncClient). MIDDLEWARE is
# deliberately not set here: several tests in test_checks.py assert on
# boundary.E004/W002 by setting settings.MIDDLEWARE themselves per-test, and
# a global default here would just be overridden by every one of them, so
# the ASGI test sets MIDDLEWARE itself via the settings fixture instead.
ROOT_URLCONF = "urls"

# Boundary settings
BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
BOUNDARY_STRICT_MODE = True
