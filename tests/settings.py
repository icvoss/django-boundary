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

# Boundary settings
BOUNDARY_TENANT_MODEL = "boundary_testapp.Tenant"
BOUNDARY_STRICT_MODE = True
