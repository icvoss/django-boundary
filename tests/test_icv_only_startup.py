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


# ── Issue #33: cache-invalidation signals under an ICV-only config ──

_RECEIVERS_SCRIPT_TEMPLATE = """
import django
from django.conf import settings

settings.configure(
    ICV_TENANT_MODEL="boundary_testapp.Tenant",
    BOUNDARY_TENANT_MODEL={boundary_tenant_model!r},
    DATABASES={{"default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}}},
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "boundary",
        "boundary_testapp",
    ],
    USE_TZ=True,
)
django.setup()

from django.db.models.signals import post_delete, post_save
from boundary_testapp.models import Tenant

save_count = len(post_save._live_receivers(sender=Tenant)[0])
delete_count = len(post_delete._live_receivers(sender=Tenant)[0])
print(f"RECEIVERS save={{save_count}} delete={{delete_count}}")
"""


def _run_receivers_script(*, boundary_tenant_model):
    """Run the receiver-count probe in a fresh subprocess and return (save, delete) counts.

    A subprocess is required, not just the settings fixture: BoundaryConfig
    connects its signals once, in AppConfig.ready(), at django.setup() time.
    The main test suite's settings module (tests/settings.py) already sets
    BOUNDARY_TENANT_MODEL and has already called django.setup() by the time
    any test runs, so there is no in-process way to observe what ready()
    would have connected under a different starting configuration; the
    subprocess gets its own fresh django.setup() call, mirroring
    test_icv_tenant_model_only_project_starts above and the reproduction
    script from issue #33.
    """
    import os

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    tests_dir = str(Path(__file__).resolve().parent)
    path_entries = [_SRC_DIR, tests_dir]
    if existing:
        path_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(path_entries)

    script = _RECEIVERS_SCRIPT_TEMPLATE.format(boundary_tenant_model=boundary_tenant_model)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"receiver-count probe failed (returncode={result.returncode}).\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    line = next(line for line in result.stdout.splitlines() if line.startswith("RECEIVERS "))
    parts = dict(item.split("=") for item in line.removeprefix("RECEIVERS ").split())
    return int(parts["save"]), int(parts["delete"])


def test_icv_only_config_connects_cache_invalidation_signals():
    """BOUNDARY_TENANT_MODEL unset, ICV_TENANT_MODEL set, still connects signals.

    Regression test for issue #33: BoundaryConfig._connect_cache_invalidation_
    signals() read settings.BOUNDARY_TENANT_MODEL directly via getattr() and
    returned early when it was unset, ignoring the ADR-025 T2 fallback to
    ICV_TENANT_MODEL that every other resolution site in the package applies
    (conf.resolve_tenant_model_setting, conf.get_tenant_model,
    checks._check_tenant_model). A project configured with only
    ICV_TENANT_MODEL, a configuration checks.E001 explicitly accepts,
    therefore connected zero post_save/post_delete receivers on its tenant
    model, so TenantMiddleware could serve a deactivated tenant from a stale
    resolver-cache entry for up to BOUNDARY_RESOLVER_CACHE_TTL seconds on
    every worker process, with no error or warning at startup.
    """
    save_count, delete_count = _run_receivers_script(boundary_tenant_model=None)
    assert save_count == 1
    assert delete_count == 1


def test_icv_only_config_receiver_probe_detects_disconnected_signals():
    """Positive control for the probe above: it must be able to report zero.

    An assertion that can only ever observe one value proves nothing (the
    exact failure mode issue #35 exists to fix). This drives the same
    subprocess probe with BoundaryConfig._connect_cache_invalidation_signals
    replaced by a no-op before django.setup() runs, everything else
    identical to the ICV-only test above (boundary installed,
    ICV_TENANT_MODEL set), demonstrating the probe reports 0, not just 1,
    when signal wiring genuinely does not happen.
    """
    import os

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    tests_dir = str(Path(__file__).resolve().parent)
    path_entries = [_SRC_DIR, tests_dir]
    if existing:
        path_entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(path_entries)

    script = """
import django
from django.conf import settings

settings.configure(
    ICV_TENANT_MODEL="boundary_testapp.Tenant",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "boundary",
        "boundary_testapp",
    ],
    USE_TZ=True,
)

import boundary.apps

boundary.apps.BoundaryConfig._connect_cache_invalidation_signals = lambda self: None

django.setup()

from django.db.models.signals import post_delete, post_save
from boundary_testapp.models import Tenant

save_count = len(post_save._live_receivers(sender=Tenant)[0])
delete_count = len(post_delete._live_receivers(sender=Tenant)[0])
print(f"RECEIVERS save={save_count} delete={delete_count}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"control probe failed (returncode={result.returncode}).\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    line = next(line for line in result.stdout.splitlines() if line.startswith("RECEIVERS "))
    parts = dict(item.split("=") for item in line.removeprefix("RECEIVERS ").split())
    assert int(parts["save"]) == 0
    assert int(parts["delete"]) == 0
