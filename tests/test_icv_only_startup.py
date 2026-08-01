"""Subprocess startup test for issue #15.

Proves that a project configured with ONLY ICV_TENANT_MODEL (no
BOUNDARY_TENANT_MODEL at all) can start: import boundary.models, build a
tenant mixin, and run the E001 check cleanly. Before the fix this was
impossible: boundary.models read settings.BOUNDARY_TENANT_MODEL directly at
import time, which raised a bare AttributeError for a project that never
sets that setting.

Runs in a real subprocess (not just django.test's settings fixture) so the
import actually happens fresh, with only sys.path adjusted to find boundary
and Django's contrib apps; nothing in the current test process's already
imported boundary.models is reused.
"""

import subprocess
import sys
from pathlib import Path

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")

_SCRIPT = """
import django
from django.conf import settings

settings.configure(
    ICV_TENANT_MODEL="auth.Group",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
    ],
    USE_TZ=True,
)
django.setup()

import boundary.models
from boundary import checks

MerchantMixin = boundary.models.make_tenant_mixin("merchant")
assert MerchantMixin is not None

errors = checks._check_tenant_model()
assert errors == [], f"Expected no E001 errors, got: {errors!r}"

print("OK")
"""


def test_icv_tenant_model_only_project_starts():
    """An ICV-only project (no BOUNDARY_TENANT_MODEL) must be able to start.

    Prepends this checkout's own src/ to PYTHONPATH so the subprocess
    imports the boundary package under test, not whatever django-boundary
    happens to be installed globally in the interpreter's site-packages.
    """
    import os

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _SRC_DIR if not existing else f"{_SRC_DIR}{os.pathsep}{existing}"

    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert result.returncode == 0, (
        f"ICV-only startup failed (returncode={result.returncode}).\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
