"""Settings for the ADR-027 smoke consumer.

One project, two backends, selected by ``BOUNDARY_SMOKE_DB``, because a
migration or typing defect can appear on only one of them and the gate has to
catch either. Nothing here is a fixture shortcut: every boundary setting is
one the documentation tells a consumer to set.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = "smoke-consumer-not-a-secret"  # noqa: S105
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "boundary",
    "smokeapp",
    "smokerls",
]

MIDDLEWARE = [
    # BR-RES-006: the middleware a consumer mounts to resolve a tenant per
    # request. boundary.E004 reports its absence, so `manage.py check` is
    # only representative with it here.
    "boundary.middleware.TenantMiddleware",
]

ROOT_URLCONF = "urls"

_BACKEND = os.environ.get("BOUNDARY_SMOKE_DB", "sqlite").lower()

if _BACKEND == "postgresql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "boundary_smoke"),
            "USER": os.environ.get("PGUSER", "icv_app"),
            "PASSWORD": os.environ.get("PGPASSWORD", "icv_dev"),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("BOUNDARY_SMOKE_SQLITE_PATH", str(BASE_DIR / "smoke.sqlite3")),
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

# Boundary settings, exactly as a consumer sets them.
BOUNDARY_TENANT_MODEL = "smokeapp.Organisation"
BOUNDARY_STRICT_MODE = True
BOUNDARY_RESOLVERS = ["boundary.resolvers.SubdomainResolver"]
BOUNDARY_SUBDOMAIN_PARENT_DOMAIN = "smoke.example.com"
