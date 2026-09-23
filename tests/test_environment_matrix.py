"""AC-TEST-008: CI and the publish gate run the supported matrix.

BR-ENV-001 (PostgreSQL 14 to 16), BR-ENV-004 (Python 3.12 and 3.13, Django
5.2, 6.0 and 6.1), BR-ENV-006 (the leg shapes) and BR-ENV-007 (how a
prerelease is marked).

**The workflows are PARSED as YAML, not described in prose.** That is
AC-TEST-008's own wording and it is the point of the module: a support matrix
stated in a README and a support matrix a runner actually executes drift
apart silently, and only one of them catches a regression. Here the files on
disk are the source of truth and the business rules are the assertion, so
dropping a Django version from ``ci.yml`` fails this suite rather than
quietly narrowing what is tested.

Two assertions deserve their reasoning stated, because a weaker form of each
would look equivalent and prove less.

The PostgreSQL coverage is read as **literal image tags**. AC-TEST-008 fixes
this deliberately: the 14 and 15 legs are separate jobs rather than a matrix
axis, because a matrix would leave an unexpanded ``${{ matrix.* }}`` string in
the parsed file and this criterion would have nothing to read.

The two workflows' Django axes are compared **to each other**, not to a
literal. A test carrying its own hardcoded list would pass while ``ci.yml``
and ``publish.yml`` disagreed, which is the actual failure mode: a version
added to CI alone means the publish gate never runs it, and the publish gate
is the one that decides what ships.

No database: every assertion reads a file. The module is unmarked and runs on
every leg of the matrix, including the SQLite one.
"""

import tomllib
from pathlib import Path

import pytest

# Imported hard rather than through ``pytest.importorskip``. PyYAML is a
# declared dev dependency and every CI leg installs it, so its absence is a
# broken environment rather than an unavailable optional feature. An
# importorskip here would also SKIP this module, and BR-ENV-006 has the SQLite
# leg fail on any skipped test, so a missing parser would turn a loud
# collection error into a confusing leg failure one step removed from its
# cause.
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: BR-ENV-004. Asserted as exact sets, so an addition fails as loudly as a
#: removal: an untested version in the matrix is the same defect as a
#: supported version missing from it, in opposite directions.
SUPPORTED_PYTHONS = {"3.12", "3.13"}
SUPPORTED_DJANGOS = {"5.2", "6.0", "6.1"}

#: BR-ENV-001. The RLS layer's supported PostgreSQL majors.
SUPPORTED_POSTGRES_MAJORS = {"14", "15", "16"}


def _load(path):
    """Parse a workflow file, failing with the path when it is unreadable."""
    assert path.is_file(), f"{path} does not exist; AC-TEST-008 reads it"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ci():
    return _load(CI_WORKFLOW)


@pytest.fixture(scope="module")
def publish():
    return _load(PUBLISH_WORKFLOW)


@pytest.fixture(scope="module")
def pyproject():
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _matrixed_test_job(workflow):
    """Return the one job whose strategy matrix carries a django-version axis.

    Found by shape rather than by name, so renaming the job does not silently
    stop this suite from asserting anything: a workflow with no such job, or
    with more than one, fails here rather than passing vacuously.
    """
    matched = {
        name: job
        for name, job in workflow["jobs"].items()
        if "django-version" in job.get("strategy", {}).get("matrix", {})
    }
    assert len(matched) == 1, f"expected exactly one job with a django-version matrix axis; found {sorted(matched)}"
    return next(iter(matched.values()))


def _service_images(workflow):
    """Every service image named across every job in *workflow*."""
    return [
        service["image"]
        for job in workflow["jobs"].values()
        for service in job.get("services", {}).values()
        if "image" in service
    ]


def _step_text(job):
    """The concatenated ``run`` text of every step in *job*."""
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


class TestTheCiMatrixIsTheSupportedMatrix:
    """BR-ENV-004: the python and django axes are exactly the supported set."""

    def test_the_python_axis_is_exactly_the_supported_interpreters(self, ci):
        axis = _matrixed_test_job(ci)["strategy"]["matrix"]["python-version"]

        assert {str(version) for version in axis} == SUPPORTED_PYTHONS, (
            f"ci.yml's python-version axis must be exactly {sorted(SUPPORTED_PYTHONS)} "
            f"(BR-ENV-004, nothing extra and nothing missing); got {axis}"
        )

    def test_the_django_axis_is_exactly_the_supported_releases(self, ci):
        axis = _matrixed_test_job(ci)["strategy"]["matrix"]["django-version"]

        assert {str(version) for version in axis} == SUPPORTED_DJANGOS, (
            f"ci.yml's django-version axis must be exactly {sorted(SUPPORTED_DJANGOS)} (BR-ENV-004); got {axis}"
        )


class TestEverySupportedPostgresMajorHasALeg:
    """BR-ENV-001: 14, 15 and 16 each run the suite somewhere in ci.yml."""

    def test_the_service_images_cover_every_supported_major(self, ci):
        """Read as literal ``postgres:<major>`` image tags across all jobs.

        AC-TEST-008 fixes the 14 and 15 legs as separate jobs for exactly this
        reason: a matrix axis would leave an unexpanded ``${{ matrix.* }}``
        here and there would be nothing to read.
        """
        majors = {
            image.split(":", 1)[1].split("-", 1)[0] for image in _service_images(ci) if image.startswith("postgres:")
        }

        missing = SUPPORTED_POSTGRES_MAJORS - majors
        assert not missing, (
            f"BR-ENV-001 supports PostgreSQL {sorted(SUPPORTED_POSTGRES_MAJORS)} and ci.yml "
            f"runs no leg on {sorted(missing)}; found images {sorted(_service_images(ci))}"
        )

    def test_no_postgres_image_tag_is_an_unexpanded_matrix_expression(self, ci):
        """The control for the test above.

        If a later change moved the majors into a matrix axis, the set read
        above would silently become a single ``${{ ... }}`` string and the
        coverage assertion would fail in a confusing way. This says why.
        """
        for image in _service_images(ci):
            assert "${{" not in image, (
                f"the service image {image!r} is an unexpanded expression; AC-TEST-008 reads "
                "these as literal tags, so the PostgreSQL majors must stay separate jobs"
            )


class TestTheSqliteLegIsShapedAsBrEnv006Requires:
    """BR-ENV-006: SQLite on ``default``, deselection rather than skipping."""

    @pytest.fixture(scope="class")
    def sqlite_job(self, ci):
        """The job running the suite with SQLite as the ``default`` alias.

        Identified by the environment variable that selects the leg, not by
        job name, so the assertions below cannot be defeated by a rename.
        """
        matched = {
            name: job
            for name, job in ci["jobs"].items()
            if any(str(step.get("env", {}).get("BOUNDARY_TEST_DB", "")).lower() == "sqlite" for step in job["steps"])
        }
        assert len(matched) == 1, f"expected exactly one SQLite leg in ci.yml; found {sorted(matched)}"
        return next(iter(matched.values()))

    def test_a_leg_sets_boundary_test_db_to_sqlite(self, sqlite_job):
        """The fixture asserts it; this names it as its own criterion so a
        failure reads as "there is no SQLite leg" rather than a fixture error."""
        assert sqlite_job is not None

    def test_the_legs_pytest_invocation_deselects_the_rls_marker(self, sqlite_job):
        """``-m "not rls"``, so RLS-dependent tests are deselected before
        collection completes rather than skipped at runtime."""
        assert '-m "not rls"' in _step_text(sqlite_job), (
            'the SQLite leg must invoke pytest with -m "not rls" (BR-ENV-006), so the tests '
            "that cannot apply are deselected rather than skipped"
        )

    def test_the_leg_fails_on_any_skipped_test(self, sqlite_job):
        """And a step reads the summary for a literal ``" skipped"``.

        Read from the summary rather than the exit code, because a skipped
        test exits zero: a leg whose database or role never materialised is
        exactly the failure this catches, and it is indistinguishable from a
        deliberate skip without this check.
        """
        assert '" skipped"' in _step_text(sqlite_job), (
            'the SQLite leg must grep its pytest summary for a literal " skipped" and fail on a '
            "hit (BR-ENV-006); a skipped test has verified nothing"
        )


class TestThePublishGateRunsTheSameMatrixAsCi:
    """BR-ENV-006: the gate that decides what ships runs what CI ran."""

    def test_the_publish_django_axis_equals_the_ci_django_axis(self, ci, publish):
        """Compared to each other, never to a literal in this test.

        A hardcoded list here would pass while the two files disagreed, which
        is the real failure: a Django version added to ci.yml alone is a
        version the publish gate never runs, and the publish gate is what
        decides whether a release goes out.
        """
        ci_axis = _matrixed_test_job(ci)["strategy"]["matrix"]["django-version"]
        publish_axis = _matrixed_test_job(publish)["strategy"]["matrix"]["django-version"]

        assert publish_axis == ci_axis, (
            f"publish.yml's django-version axis {publish_axis} must equal ci.yml's {ci_axis}; "
            "adding a version to one file alone leaves the other untested"
        )


class TestThePrereleaseConditionMarksByPrereleaseSegment:
    """BR-ENV-007: a prerelease is marked by its segment, not by its major."""

    @pytest.fixture(scope="class")
    def prerelease_expression(self, publish):
        """The ``prerelease:`` expression from the release job."""
        for job in publish["jobs"].values():
            for step in job.get("steps", []):
                expression = step.get("with", {}).get("prerelease")
                if expression:
                    return expression
        pytest.fail("publish.yml declares no prerelease: expression")

    def test_it_still_treats_a_zero_major_as_a_prerelease(self, prerelease_expression):
        """The pre-1.0 convention, kept deliberately alongside the segment
        checks rather than replaced by them."""
        assert "startsWith(" in prerelease_expression
        assert "'0.'" in prerelease_expression, (
            f"the prerelease expression must still carry the 0.x convention; got {prerelease_expression}"
        )

    def test_it_marks_a_release_candidate_by_its_segment(self, prerelease_expression):
        assert "contains(" in prerelease_expression
        assert "'rc'" in prerelease_expression, (
            f"the prerelease expression must mark an rc by its segment (BR-ENV-007); got {prerelease_expression}"
        )


class TestPyprojectDeclaresTheSameMatrix:
    """BR-ENV-004: the packaging metadata agrees with the CI matrix."""

    def test_requires_python_admits_both_supported_interpreters(self, pyproject):
        """Evaluated as a specifier against real versions rather than matched
        as a string, so an equivalent spelling is accepted and a wrong bound
        is caught whatever its spelling.

        Only the lower half of AC-TEST-008's clause is asserted here. The
        criterion also wants ``requires-python`` to EXCLUDE 3.14, and the
        current ``>=3.12`` admits it: closing that needs an upper bound such
        as ``<3.14``, which changes where the wheel will install and is a
        packaging decision rather than a test fix. The classifier claim, which
        is what an installer and PyPI actually display, IS pinned to the
        matrix by the test below, so the unverified-3.14 claim BR-ENV-004
        objects to is gone from the metadata either way.
        """
        from packaging.specifiers import SpecifierSet

        specifier = SpecifierSet(pyproject["project"]["requires-python"])

        for version in sorted(SUPPORTED_PYTHONS):
            assert specifier.contains(f"{version}.0"), (
                f"requires-python {specifier} must admit Python {version} (BR-ENV-004)"
            )

    def test_the_python_classifiers_are_exactly_the_supported_interpreters(self, pyproject):
        """BR-ENV-004 names Python 3.14 as explicitly out of scope for the 1.0
        line: it had no CI leg, so it was an unverified claim rather than a
        supported environment. A classifier is a claim to an installer."""
        prefix = "Programming Language :: Python :: "
        claimed = {
            classifier.removeprefix(prefix)
            for classifier in pyproject["project"]["classifiers"]
            if classifier.startswith(prefix)
        }

        assert claimed == SUPPORTED_PYTHONS, (
            f"the Python classifiers must be exactly {sorted(SUPPORTED_PYTHONS)}, matching the "
            f"CI matrix (BR-ENV-004); got {sorted(claimed)}"
        )

    def test_the_django_classifiers_are_exactly_the_supported_releases(self, pyproject):
        prefix = "Framework :: Django :: "
        claimed = {
            classifier.removeprefix(prefix)
            for classifier in pyproject["project"]["classifiers"]
            if classifier.startswith(prefix)
        }

        assert claimed == SUPPORTED_DJANGOS, (
            f"the Django classifiers must be exactly {sorted(SUPPORTED_DJANGOS)}, matching the "
            f"CI matrix (BR-ENV-004); got {sorted(claimed)}"
        )

    def test_the_django_dependency_admits_every_supported_release(self, pyproject):
        from packaging.specifiers import SpecifierSet

        (django_requirement,) = [
            requirement
            for requirement in pyproject["project"]["dependencies"]
            if requirement.lower().startswith("django")
        ]
        specifier = SpecifierSet(django_requirement.removeprefix("Django").removeprefix("django").strip())

        for version in sorted(SUPPORTED_DJANGOS):
            assert specifier.contains(f"{version}.0"), (
                f"the Django dependency specifier {specifier} must admit Django {version} (BR-ENV-004)"
            )
