#!/usr/bin/env bash
#
# ADR-027's consumer smoke gate: stand the throwaway consumer in
# smoke/consumer/ up against django-boundary as an INSTALLED DISTRIBUTION and
# run the checks a real consumer runs.
#
#   ./smoke/run.sh sqlite
#   ./smoke/run.sh postgresql
#   ./smoke/run.sh sqlite --mypy-only
#
# The three steps run in one pass because they share a virtualenv and a
# migrated database. --mypy-only runs the same setup and then step 3 alone,
# so CI can mark the typing leg advisory (continue-on-error) without also
# making the migration legs advisory, which ADR-027 requires to be blocking.
#
# The distinction ADR-027 is built on: the wheel goes into a clean virtualenv
# with `pip install dist/*.whl`, never `pip install -e .`, and the consumer is
# its own Django project rather than boundary's bundled test app. An editable
# install puts src/ on sys.path and the gate would then be testing the working
# tree, which is exactly the blind spot that let django-icv-identity 0.3.0
# ship a model change with no migration.
#
# Override the wheel with BOUNDARY_SMOKE_WHEEL_GLOB (default dist/*.whl).
# PostgreSQL connection details come from the standard PG* variables.

set -euo pipefail

BACKEND="${1:-sqlite}"
MODE="${2:-all}"
case "${BACKEND}" in
    sqlite | postgresql) ;;
    *)
        echo "usage: $0 {sqlite|postgresql} [--mypy-only]" >&2
        exit 2
        ;;
esac
case "${MODE}" in
    all | --mypy-only) ;;
    *)
        echo "usage: $0 {sqlite|postgresql} [--mypy-only]" >&2
        exit 2
        ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONSUMER_DIR="${REPO_ROOT}/smoke/consumer"
WHEEL_GLOB="${BOUNDARY_SMOKE_WHEEL_GLOB:-${REPO_ROOT}/dist/*.whl}"

# shellcheck disable=SC2086
WHEEL="$(ls ${WHEEL_GLOB} 2>/dev/null | head -1 || true)"
if [[ -z "${WHEEL}" ]]; then
    echo "ERROR: no wheel matched '${WHEEL_GLOB}'. Build one first:" >&2
    echo "    python -m build --wheel --outdir dist/" >&2
    exit 1
fi

VENV_DIR="$(mktemp -d)/venv"
SQLITE_DB="$(mktemp -d)/smoke.sqlite3"
cleanup() {
    rm -rf "$(dirname "${VENV_DIR}")" "$(dirname "${SQLITE_DB}")"
}
trap cleanup EXIT

echo "=============================================================="
echo "ADR-027 consumer smoke test: ${BACKEND} (${MODE})"
echo "wheel:    ${WHEEL}"
echo "consumer: ${CONSUMER_DIR}"
echo "=============================================================="

echo
echo "--- Creating a clean virtualenv and installing the wheel"
python3 -m venv "${VENV_DIR}"
PY="${VENV_DIR}/bin/python"
"${PY}" -m pip install --quiet --upgrade pip
# The wheel, not the source tree. The [dev] extra brings the declared mypy and
# django-stubs pair with it, so the pin stays in pyproject.toml rather than
# being restated here where it could rot away from it.
"${PY}" -m pip install --quiet "${WHEEL}[dev]"
if [[ "${BACKEND}" == "postgresql" ]]; then
    "${PY}" -m pip install --quiet "psycopg[binary]"
fi

echo
echo "--- Guard: the package must resolve from site-packages, not the source tree"
# ADR-027 and AC-TEST-007: the gate asserts boundary.__file__ resolves inside
# site-packages. Run from a directory that is NOT the repo root, with an empty
# PYTHONPATH, so a stray src/ on the path cannot shadow the wheel silently.
(
    cd "${VENV_DIR}"
    PYTHONPATH="" "${PY}" -c '
import os
import sys

import boundary

origin = os.path.realpath(boundary.__file__)
if "site-packages" not in origin:
    sys.exit(f"ERROR: the source tree shadowed the wheel: {origin}")
print(f"boundary resolves from {origin}")
print(f"version {boundary.__version__}")
'
)

export DJANGO_SETTINGS_MODULE=settings
export BOUNDARY_SMOKE_DB="${BACKEND}"
export BOUNDARY_SMOKE_SQLITE_PATH="${SQLITE_DB}"
export PYTHONPATH="${CONSUMER_DIR}"

cd "${CONSUMER_DIR}"

if [[ "${BACKEND}" == "postgresql" ]]; then
    echo
    echo "--- Dropping any leftover smoke tables so migrate runs on a fresh schema"
    # ADR-027 step 2 is "migrate on a FRESH database". The CI service database
    # is fresh per job, but a local run against a reused database is not, and a
    # migrate that no-ops because the tables already exist proves nothing.
    PGPASSWORD="${PGPASSWORD:-icv_dev}" psql \
        -h "${PGHOST:-localhost}" -p "${PGPORT:-5432}" \
        -U "${PGUSER:-icv_app}" -d "${PGDATABASE:-boundary_smoke}" \
        -v ON_ERROR_STOP=1 -q -c '
        DROP TABLE IF EXISTS smokeapp_booking CASCADE;
        DROP TABLE IF EXISTS smokeapp_organisation CASCADE;
        DROP TABLE IF EXISTS django_migrations CASCADE;
        DROP TABLE IF EXISTS django_content_type CASCADE;
        DROP TABLE IF EXISTS auth_permission CASCADE;
        DROP TABLE IF EXISTS auth_group_permissions CASCADE;
        DROP TABLE IF EXISTS auth_user_groups CASCADE;
        DROP TABLE IF EXISTS auth_user_user_permissions CASCADE;
        DROP TABLE IF EXISTS auth_group CASCADE;
        DROP TABLE IF EXISTS auth_user CASCADE;
        DROP FUNCTION IF EXISTS boundary_current_tenant_id() CASCADE;'
fi

if [[ "${MODE}" == "all" ]]; then
    echo
    echo "--- Step 1 of 3: manage.py makemigrations --check --dry-run"
    # ADR-027 defect 1: a model or manager change shipped with no migration.
    # The consumer cannot self-remedy one, because Django wants to write the
    # migration into site-packages, which is unwritable.
    "${PY}" manage.py makemigrations --check --dry-run

    echo
    echo "--- Step 2 of 3: manage.py migrate on a fresh database, then manage.py check"
    "${PY}" manage.py migrate --no-input
    "${PY}" manage.py check
else
    echo
    echo "--- Steps 1 and 2 skipped (--mypy-only); migrating so the plugin has a schema"
    "${PY}" manage.py migrate --no-input >/dev/null

    echo
    echo "--- Step 3 of 3: mypy on the consumer with the django-stubs plugin"
    # ADR-027 defect 2: the package failing to typecheck clean in a consumer.
    # This step is meant to FAIL the job when it fails; it is advisory only
    # while icvoss/django-boundary#81's typing fix is outstanding, and the CI
    # step that calls this carries the matching continue-on-error and note.
    # Added 2026-09-23.
    "${VENV_DIR}/bin/mypy" --config-file mypy.ini smokeapp smokerls settings.py urls.py
fi

echo
echo "=============================================================="
echo "ADR-027 consumer smoke test PASSED on ${BACKEND} (${MODE})"
echo "=============================================================="
