"""Tests for boundary management commands."""

import json
import os
import tempfile

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from boundary.testing import set_tenant


@pytest.mark.django_db
class TestBoundaryProvision:
    """AC-CMD-001/002: boundary_provision creates tenant and calls hook."""

    def test_creates_tenant(self, capsys):
        from boundary_testapp.models import Tenant

        call_command("boundary_provision", name="New Club", slug="new-club")
        output = capsys.readouterr().out.strip()
        # Output should be the new tenant's PK
        tenant = Tenant.objects.get(slug="new-club")
        assert output == str(tenant.pk)

    def test_with_region(self, capsys):
        from boundary_testapp.models import Tenant

        call_command(
            "boundary_provision",
            name="EU Club",
            slug="eu-club",
            region="eu-west",
        )
        tenant = Tenant.objects.get(slug="eu-club")
        assert tenant.region == "eu-west"

    def test_with_extra_fields(self, capsys):
        """Extra fields are passed as kwargs to model constructor."""
        # AbstractTenant doesn't have custom fields, but we test the JSON parsing
        call_command(
            "boundary_provision",
            name="Pro Club",
            slug="pro-club",
            extra_fields="{}",
        )
        from boundary_testapp.models import Tenant

        assert Tenant.objects.filter(slug="pro-club").exists()

    def test_invalid_json_raises(self):
        with pytest.raises(CommandError, match="Invalid JSON"):
            call_command(
                "boundary_provision",
                name="Bad",
                slug="bad",
                extra_fields="not json",
            )

    def test_post_provision_hook(self, capsys, settings, tmp_path):
        hook_file = tmp_path / "hook_called.txt"
        # Create a hook module
        hook_module = tmp_path / "test_hook.py"
        hook_module.write_text(f"def hook(tenant): open('{hook_file}', 'w').write(str(tenant.pk))")
        import sys

        sys.path.insert(0, str(tmp_path))
        try:
            settings.BOUNDARY_POST_PROVISION_HOOK = "test_hook.hook"
            call_command("boundary_provision", name="Hooked", slug="hooked-club")
            assert hook_file.exists()
        finally:
            sys.path.remove(str(tmp_path))
            settings.BOUNDARY_POST_PROVISION_HOOK = None

    @pytest.mark.django_db(databases=["default", "eu-west"])
    def test_writes_to_regional_database(self, capsys, settings):
        """Issue #62: with BOUNDARY_REGIONS configured, a tenant provisioned
        with --region must land on that region's own database alias, not
        "default". Asserts row presence on both aliases directly, not a
        mocked create(using=...) call, so the test fails on the unfixed
        command (which calls TenantModel.objects.create(**kwargs) with no
        using=, landing every row on "default" regardless of --region).
        """
        from boundary_testapp.models import Tenant

        settings.BOUNDARY_REGIONS = {"eu-west": {"ENGINE": "django.db.backends.sqlite3"}}

        call_command(
            "boundary_provision",
            name="EU Club",
            slug="eu-region-club",
            region="eu-west",
        )

        assert Tenant.objects.using("eu-west").filter(slug="eu-region-club").exists()
        assert not Tenant.objects.using("default").filter(slug="eu-region-club").exists()

    def test_unknown_region_raises_command_error(self, settings):
        """Issue #62: a --region not present in BOUNDARY_REGIONS must fail
        the command rather than silently writing to "default".
        """
        settings.BOUNDARY_REGIONS = {"eu-west": {"ENGINE": "django.db.backends.sqlite3"}}

        with pytest.raises(CommandError, match="ap-southeast"):
            call_command(
                "boundary_provision",
                name="Bad Region Club",
                slug="bad-region-club",
                region="ap-southeast",
            )

        from boundary_testapp.models import Tenant

        assert not Tenant.objects.using("default").filter(slug="bad-region-club").exists()

    @pytest.mark.django_db(databases=["default", "eu-west"])
    def test_hook_fires_once_for_regional_tenant(self, settings, tmp_path):
        """Issue #62: the post-provision hook must still fire exactly once,
        after the row is written, with the same tenant argument as today,
        when the tenant is written to a regional alias.
        """
        calls_file = tmp_path / "calls.txt"
        hook_module = tmp_path / "region_hook.py"
        hook_module.write_text(
            f"def hook(tenant):\n    with open('{calls_file}', 'a') as f:\n        f.write(str(tenant.pk) + chr(10))\n"
        )
        import sys

        sys.path.insert(0, str(tmp_path))
        try:
            settings.BOUNDARY_REGIONS = {"eu-west": {"ENGINE": "django.db.backends.sqlite3"}}
            settings.BOUNDARY_POST_PROVISION_HOOK = "region_hook.hook"

            call_command(
                "boundary_provision",
                name="Hooked Regional Club",
                slug="hooked-regional-club",
                region="eu-west",
            )

            from boundary_testapp.models import Tenant

            tenant = Tenant.objects.using("eu-west").get(slug="hooked-regional-club")
            recorded_calls = calls_file.read_text().splitlines()
            assert recorded_calls == [str(tenant.pk)]
        finally:
            sys.path.remove(str(tmp_path))
            settings.BOUNDARY_POST_PROVISION_HOOK = None


@pytest.mark.django_db
class TestBoundaryDeprovision:
    """AC-CMD-003/004: boundary_deprovision with dry-run and export."""

    def test_dry_run(self, tenant_a, capsys):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)

        call_command("boundary_deprovision", tenant=tenant_a.slug, dry_run=True)
        output = capsys.readouterr().out
        assert "DRY RUN" in output
        # Tenant should still exist
        from boundary_testapp.models import Tenant

        assert Tenant.objects.filter(pk=tenant_a.pk).exists()

    def test_export_creates_ndjson(self, tenant_a):
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)
            Booking.objects.create(court=2)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
            export_path = f.name

        try:
            call_command(
                "boundary_deprovision",
                tenant=tenant_a.slug,
                export=export_path,
                yes=True,
            )
            with open(export_path) as f:
                lines = f.readlines()
            assert len(lines) == 2  # 2 bookings exported
            row = json.loads(lines[0])
            assert "_model" in row
            assert "_pk" in row
        finally:
            os.unlink(export_path)

    def test_deletes_tenant(self, tenant_a):
        from boundary_testapp.models import Booking, Tenant

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)

        call_command("boundary_deprovision", tenant=tenant_a.slug, yes=True)
        assert not Tenant.objects.filter(pk=tenant_a.pk).exists()

    def test_nonexistent_tenant_raises(self):
        with pytest.raises(CommandError, match="not found"):
            call_command("boundary_deprovision", tenant="nonexistent", yes=True)

    def test_discovers_custom_fk_model_rows(self, tenant_a, capsys):
        """Regression: models built via make_tenant_mixin (custom FK name) must
        be discovered (counted), not silently skipped. Exercises
        _collect_affected via the dry-run report."""
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            Product.objects.create(sku="A1")
            Product.objects.create(sku="A2")

        call_command("boundary_deprovision", tenant=tenant_a.slug, dry_run=True)
        output = capsys.readouterr().out
        assert "Product: 2 rows" in output

    def test_exports_custom_fk_model_rows(self, tenant_a):
        """Regression: custom-FK (make_tenant_mixin) rows must be included in
        the NDJSON export before deletion."""
        from boundary_testapp.models import Product

        with set_tenant(tenant_a):
            Product.objects.create(sku="A1")
            Product.objects.create(sku="A2")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".ndjson", delete=False) as f:
            export_path = f.name

        try:
            call_command(
                "boundary_deprovision",
                tenant=tenant_a.slug,
                export=export_path,
                yes=True,
            )
            with open(export_path) as f:
                rows = [json.loads(line) for line in f]
            product_rows = [r for r in rows if r["_model"] == "boundary_testapp.Product"]
            assert len(product_rows) == 2
        finally:
            os.unlink(export_path)


@pytest.mark.django_db
class TestBoundaryRun:
    """AC-CMD-005: boundary_run executes command with tenant context."""

    def test_scoped_execution(self, tenant_a, capsys):
        """The inner command runs with tenant context active."""
        call_command("boundary_run", f"--tenant={tenant_a.slug}", "showmigrations", "--list")

    def test_nonexistent_tenant_raises(self):
        with pytest.raises(CommandError, match="not found"):
            call_command("boundary_run", "--tenant=nonexistent", "showmigrations")


def _run_all_results(output):
    """Return boundary_run_all's own NDJSON result lines from mixed stdout.

    --json emits one JSON object per tenant, but the inner command writes to
    the same stdout, so the stream is interleaved. ``showmigrations`` prints
    an app header and a line per migration for every app that has a real
    migration graph, and tests/settings.py gives thirdparty and
    boundary_consumer exactly that. Selecting the JSON lines is therefore
    the assertion these tests always meant; treating every line as JSON only
    passed while no installed app had migrations to show.
    """
    results = []
    for line in output.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        results.append(json.loads(line))
    return results


@pytest.mark.django_db
class TestBoundaryRunAll:
    """AC-CMD-006/007: boundary_run_all with parallel and region filter."""

    def test_runs_for_all_active_tenants(self, tenant_a, tenant_b, capsys):
        call_command("boundary_run_all", "showmigrations")

    def test_json_output(self, tenant_a, capsys):
        call_command("boundary_run_all", "showmigrations", json_output=True)
        output = capsys.readouterr().out.strip()
        results = _run_all_results(output)
        assert results
        for data in results:
            assert "tenant" in data
            assert "status" in data

    def test_region_filter(self, tenant_a, tenant_b, capsys):
        tenant_a.region = "eu-west"
        tenant_a.save()
        tenant_b.region = "us"
        tenant_b.save()

        call_command("boundary_run_all", "showmigrations", region="eu-west", json_output=True)
        output = capsys.readouterr().out.strip()
        results = _run_all_results(output)
        slugs = [r["tenant"] for r in results]
        assert tenant_a.slug in slugs
        assert tenant_b.slug not in slugs

    def test_exclude(self, tenant_a, tenant_b, capsys):
        call_command(
            "boundary_run_all",
            "showmigrations",
            exclude=[str(tenant_b.pk)],
            json_output=True,
        )
        output = capsys.readouterr().out.strip()
        results = _run_all_results(output)
        slugs = [r["tenant"] for r in results]
        assert tenant_a.slug in slugs
        assert tenant_b.slug not in slugs


# ── boundary_deprovision over adopted tables (BR-PRV-009) ────
#
# BOUNDARY_TENANT_APPS is unset in tests/settings.py, so the command's
# adopted branch is inert by default and every existing deprovision test
# above is unaffected. Each test here turns it on, mirroring the consumer
# migration's own per-migration exclude= in BOUNDARY_ADOPT_EXCLUDE because
# the command, like boundary.E007, reads only the setting.

DEPROVISION_TENANT_APPS = ["thirdparty"]
DEPROVISION_ADOPT_EXCLUDE = [
    "thirdparty.Seat",
    "thirdparty.SeatBooking",
    "thirdparty.Coupon",
]


def _insert_widgets(tenant, count, prefix):
    """Insert *count* adopted rows for *tenant*, naming tenant_id explicitly.

    Raw SQL inside admin_bypass(), which is the documented way to write an
    adopted row from outside a tenant context (BR-RLS-018): the column
    DEFAULT would otherwise stamp NULL and the NOT NULL constraint would
    reject it. Uses Django's own connection, so the rows are committed by
    the surrounding transaction=True test rather than rolled back before
    the command's server-side cursor could read them.
    """
    from django.db import connection

    from boundary.context import admin_bypass

    with admin_bypass(), connection.cursor() as cursor:
        for index in range(count):
            cursor.execute(
                'INSERT INTO "thirdparty_widget" (name, code, owner, label, tenant_id) VALUES (%s, %s, %s, %s, %s)',
                [f"{prefix}{index}", f"{prefix}-code-{index}", "owner", f"{prefix}{index}", tenant.pk],
            )


def _widget_count(tenant):
    """Return the adopted table's row count for *tenant*, across RLS."""
    from django.db import connection

    from boundary.context import admin_bypass

    with admin_bypass(), connection.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM "thirdparty_widget" WHERE tenant_id = %s', [tenant.pk])
        return cursor.fetchone()[0]


def _clear_widgets():
    """Empty the adopted table between tests.

    transaction=True commits, so a row left behind by one test would be
    counted by the next one's assertions.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute('TRUNCATE "thirdparty_widget_tags", "thirdparty_widget" CASCADE')


#: The alias name the regional-routing test routes the adopted model to. It is
#: never opened: the ``alias_recorder`` fixture records the lookup and serves
#: the real ``default`` connection, so the command's SQL genuinely executes
#: while the alias it chose is still observable, and no third database is added
#: to the suite.
REGIONAL_ALIAS = "adopted-regional"


class _AdoptedModelRegionalRouter:
    """Route the adopted model to a named non-default alias.

    Stands in for a ``BOUNDARY_REGIONS`` deployment's router without needing
    regions configured: the only thing under test is whether the command's raw
    SQL asks the router for an alias at all, or hardcodes ``default``.

    Only ``thirdparty`` is routed, so every other model the command touches
    (the tenant itself, the scoped models) keeps going to ``default`` and the
    command's ordinary behaviour is unchanged around the branch being tested.
    """

    def _route(self, model):
        if model._meta.app_label == "thirdparty":
            return REGIONAL_ALIAS
        return None

    def db_for_read(self, model, **hints):
        return self._route(model)

    def db_for_write(self, model, **hints):
        return self._route(model)

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return None


@pytest.mark.django_db(transaction=True)
class TestBoundaryDeprovisionAdoptedTables:
    """AC-CMD-008/009/010 (BR-PRV-003, BR-PRV-004, BR-PRV-009): the
    deprovision command covers adopted tables.

    transaction=True is required rather than cosmetic: the command's export
    reads through a PostgreSQL server-side cursor and every adopted
    statement runs inside admin_bypass(), so the rows under test must be
    genuinely committed rather than held in an outer atomic block that a
    non-transactional django_db would roll back.
    """

    @pytest.fixture(autouse=True)
    def _adopted_settings(self, settings):
        settings.BOUNDARY_TENANT_APPS = DEPROVISION_TENANT_APPS
        settings.BOUNDARY_ADOPT_EXCLUDE = DEPROVISION_ADOPT_EXCLUDE
        _clear_widgets()
        yield
        _clear_widgets()

    @pytest.fixture
    def four_and_two(self, tenant_a, tenant_b):
        """Four adopted rows for tenant A and two for tenant B (AC-CMD-008)."""
        _insert_widgets(tenant_a, 4, "a")
        _insert_widgets(tenant_b, 2, "b")
        return tenant_a, tenant_b

    def test_ac_cmd_008_dry_run_counts_adopted_rows_for_the_target_tenant_only(self, four_and_two, capsys):
        """Given thirdparty.Widget is adopted and holds four rows for tenant
        A and two for tenant B, when boundary_deprovision --tenant club-a
        --dry-run is run, then the output names thirdparty.Widget with a
        count of 4, and no row is deleted from any table and the tenant row
        still exists.

        The count of 4 rather than 6 is the load-bearing half: a collect
        step that ran its raw SELECT without admin_bypass() would see zero
        rows under RLS, and one that ignored the WHERE clause would see six.
        """
        tenant_a, tenant_b = four_and_two

        call_command("boundary_deprovision", tenant=tenant_a.slug, dry_run=True)
        output = capsys.readouterr().out

        assert "DRY RUN" in output
        assert "thirdparty.Widget" in output, f"expected the adopted table named by label; got {output}"
        assert "adopted" in output, f"expected the adopted table marked as adopted; got {output}"
        widget_lines = [line for line in output.splitlines() if "thirdparty.Widget" in line]
        assert len(widget_lines) == 1, f"expected exactly one Widget line; got {widget_lines}"
        assert "4 rows" in widget_lines[0], f"expected a count of 4 for tenant A; got {widget_lines[0]}"

        # Nothing deleted, tenant still there.
        assert _widget_count(tenant_a) == 4
        assert _widget_count(tenant_b) == 2
        from boundary_testapp.models import Tenant

        assert Tenant.objects.filter(pk=tenant_a.pk).exists()

    def test_ac_cmd_009_export_carries_adopted_and_column_keys(self, four_and_two, tmp_path):
        """Given the same state, when boundary_deprovision --tenant club-a
        --export data.ndjson --yes is run, then data.ndjson contains four
        lines whose _model is thirdparty.Widget, each carrying _pk as text,
        _adopted as true, and every column of the adopted table keyed by
        column name, tenant_id included.

        And no line for an adopted row carries _adopted absent or false, and
        no line for a column-bearing model carries _adopted at all: that
        last clause is what makes _adopted a usable discriminator on
        re-import rather than a key a consumer must guess the meaning of.
        """
        tenant_a, _tenant_b = four_and_two

        # A column-bearing model's rows too, so the "no _adopted on a scoped
        # line" half of the criterion has something to be true about.
        from boundary_testapp.models import Booking

        with set_tenant(tenant_a):
            Booking.objects.create(court=1)

        export_path = tmp_path / "data.ndjson"
        call_command("boundary_deprovision", tenant=tenant_a.slug, export=str(export_path), yes=True)

        lines = [json.loads(line) for line in export_path.read_text().splitlines() if line]
        widget_lines = [row for row in lines if row["_model"] == "thirdparty.Widget"]
        assert len(widget_lines) == 4, f"expected four adopted lines; got {[r['_model'] for r in lines]}"

        for row in widget_lines:
            assert row["_adopted"] is True
            assert isinstance(row["_pk"], str), f"_pk must be text; got {row['_pk']!r}"
            # Every column, by column NAME, tenant_id included.
            assert row["tenant_id"] == tenant_a.pk
            for column in ("id", "name", "code", "owner", "label"):
                assert column in row, f"expected column '{column}' in the exported line; got {sorted(row)}"

        booking_lines = [row for row in lines if row["_model"] == "boundary_testapp.Booking"]
        assert booking_lines, "positive control: the scoped model's line must be exported too"
        for row in booking_lines:
            assert "_adopted" not in row, f"a column-bearing model's line must not carry _adopted; got {sorted(row)}"

    def test_ac_cmd_009_the_export_is_written_before_any_delete(self, four_and_two, tmp_path):
        """And the file is written before any delete statement runs.

        Proven by the file's own contents rather than by ordering the calls:
        the export holds four Widget lines AFTER the command has finished
        deleting them, which is only possible if it was written while the
        rows still existed. An export written after the delete would be
        empty, and that is the failure this clause exists to prevent, since
        the file is the operator's only copy of what was destroyed.
        """
        tenant_a, _tenant_b = four_and_two

        export_path = tmp_path / "data.ndjson"
        call_command("boundary_deprovision", tenant=tenant_a.slug, export=str(export_path), yes=True)

        lines = [json.loads(line) for line in export_path.read_text().splitlines() if line]
        widget_lines = [row for row in lines if row["_model"] == "thirdparty.Widget"]
        assert len(widget_lines) == 4, "the export must hold the rows the command then deleted"
        assert _widget_count(tenant_a) == 0, "precondition: the rows really were deleted afterwards"

    def test_ac_cmd_010_delete_removes_that_tenants_adopted_rows_only(self, four_and_two):
        """Given the same state, when boundary_deprovision --tenant club-a
        --yes is run, then the four tenant A rows are gone from
        thirdparty_widget and the two tenant B rows remain.

        The surviving tenant B rows are the whole assertion: a DELETE that
        omitted the WHERE clause, or ran without admin_bypass() and silently
        matched nothing, would leave either zero or six rows behind, and
        both of those are catastrophic in opposite directions.
        """
        tenant_a, tenant_b = four_and_two

        call_command("boundary_deprovision", tenant=tenant_a.slug, yes=True)

        assert _widget_count(tenant_a) == 0, "tenant A's adopted rows must be gone"
        assert _widget_count(tenant_b) == 2, "tenant B's adopted rows must survive"

    def test_ac_cmd_010_the_tenant_row_is_deleted_after_its_adopted_rows(self, four_and_two):
        """And the tenant A row is deleted after its adopted rows, not
        before.

        The adopted tenant_id column is not a Django ForeignKey and carries
        no database-level cascade, so ordering is the only thing keeping an
        adopted row from outliving the tenant it belongs to. Proven by the
        end state: the tenant is gone AND no adopted row of its remains. A
        command that deleted the tenant first would leave four orphan rows
        pointing at a primary key that no longer exists, which is exactly
        what BR-PRV-009 requires the ordering to prevent.
        """
        tenant_a, tenant_b = four_and_two
        tenant_a_pk = tenant_a.pk

        call_command("boundary_deprovision", tenant=tenant_a.slug, yes=True)

        from boundary_testapp.models import Tenant

        assert not Tenant.objects.filter(pk=tenant_a_pk).exists(), "the tenant row must be deleted"

        from django.db import connection

        from boundary.context import admin_bypass

        with admin_bypass(), connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM "thirdparty_widget" WHERE tenant_id = %s', [tenant_a_pk])
            orphans = cursor.fetchone()[0]
        assert orphans == 0, "no adopted row may outlive the tenant whose primary key it holds"
        assert _widget_count(tenant_b) == 2

    def test_adopted_tables_are_untouched_when_tenant_apps_is_unset(self, four_and_two, settings, capsys):
        """The whole adopted branch is inert without BOUNDARY_TENANT_APPS.

        The discriminator for every test above: they all set the setting, so
        without this counterpart none of them would show that the setting is
        what turns the behaviour on rather than it having always been there.
        A deployment that has adopted nothing must not pay for, or be
        changed by, any of this.
        """
        tenant_a, tenant_b = four_and_two
        settings.BOUNDARY_TENANT_APPS = []

        call_command("boundary_deprovision", tenant=tenant_a.slug, dry_run=True)
        output = capsys.readouterr().out
        assert "thirdparty.Widget" not in output, f"the adopted table must not be collected; got {output}"

        call_command("boundary_deprovision", tenant=tenant_a.slug, yes=True)
        assert _widget_count(tenant_a) == 4, "with the setting off, the adopted rows must be left alone"
        assert _widget_count(tenant_b) == 2

    @pytest.fixture
    def alias_recorder(self, monkeypatch):
        """Record every alias looked up in ``connections``, serving ``default``.

        A recording proxy rather than a real second alias, because a real one
        cannot be reached from here. Django's ``SimpleTestCase`` patches
        ``BaseDatabaseWrapper.ensure_connection`` at ``setUpClass`` to refuse
        any alias outside the test's own ``databases``, and validates that set
        against ``settings.DATABASES`` before any fixture runs, so an alias
        added later is refused on first use and an alias named in the mark
        would have to exist in ``tests/settings.py``. Adding a third database
        to the suite to prove a routing decision is the wrong trade.

        Every lookup is forwarded to the real ``default`` connection, so the
        command's SQL genuinely executes against the rows the fixtures
        inserted and the end-state assertions below are real. What the proxy
        adds is the record of WHICH alias was asked for, which is the decision
        under test.

        Both import sites are patched: the command resolves ``connections``
        from ``django.db`` inside its method bodies, while ``boundary.context``
        binds it at module import for ``admin_bypass()``. Patching only the
        first would leave the bypass flag set on whatever alias
        ``boundary.context`` reached, which is the exact bug this test exists
        to catch.

        What the record does NOT contain is the scoped branch, which is why the
        assertions can require every entry to be the routed alias.
        ``django.db.models.query`` and ``base`` bind ``connections`` at their
        own module import, so the ORM never reads the patched attribute; and
        the command enters no tenant context, so ``boundary.context``'s other
        ``connections`` users (``_set_db_session`` and friends) are never
        reached either.
        """
        from django.db import connections as real_connections

        looked_up = []

        class _RecordingConnections:
            def __getitem__(self, alias):
                looked_up.append(alias)
                return real_connections["default"]

            def __getattr__(self, name):
                return getattr(real_connections, name)

        proxy = _RecordingConnections()
        monkeypatch.setattr("django.db.connections", proxy)
        monkeypatch.setattr("boundary.context.connections", proxy)
        return looked_up

    def test_the_adopted_branch_runs_on_the_routers_alias_not_default(
        self, four_and_two, settings, alias_recorder, tmp_path
    ):
        """BR-PRV-009: an adopted model's count, export and delete run on the
        alias the router names, not on a hardcoded ``default``.

        The scoped branch of every step goes through the ORM and is therefore
        routed for free; the adopted branch issues raw SQL and has to ask the
        router itself. Under ``BOUNDARY_REGIONS`` a hardcoded ``default``
        counts zero rows on the wrong database, so the command reports nothing
        to delete, writes an empty export, and leaves every adopted row of the
        deprovisioned tenant behind on the regional alias, isolated by RLS and
        therefore visible to nobody and deleted by nothing.

        The router routes ``thirdparty`` and nothing else, so the aliases
        recorded are exactly the adopted branch's own: a command that still
        hardcoded ``default`` would record ``default`` and no occurrence of the
        routed name at all.
        """
        tenant_a, tenant_b = four_and_two
        settings.DATABASE_ROUTERS = [_AdoptedModelRegionalRouter()]
        export_path = tmp_path / "data.ndjson"

        call_command(
            "boundary_deprovision",
            tenant=tenant_a.slug,
            export=str(export_path),
            yes=True,
        )

        assert REGIONAL_ALIAS in alias_recorder, (
            f"the adopted branch must reach the router's alias; it asked for {alias_recorder}"
        )
        assert "default" not in alias_recorder, (
            f"no adopted statement may hardcode default when the router names an alias; it asked for {alias_recorder}"
        )

        # Three adopted steps run (count, export, delete), and each resolves a
        # connection for its raw SQL and again for admin_bypass()'s own flag,
        # so the routed alias is asked for several times over. Asserted as
        # "every lookup, at least three of them" rather than an exact number,
        # which would pin admin_bypass()'s internal call count rather than the
        # routing decision under test.
        #
        # The bypass alias is not incidental: the flag is a session variable on
        # ONE connection, so set on default while the statements run elsewhere
        # it would be inert there and RLS would match no rows at all. That is
        # why "default not in the record" above covers the bypass too.
        assert set(alias_recorder) == {REGIONAL_ALIAS}, (
            f"every adopted lookup must be the routed alias; got {alias_recorder}"
        )
        assert alias_recorder.count(REGIONAL_ALIAS) >= 3, (
            f"expected the count, export and delete steps each to route; got {alias_recorder}"
        )

        # And the routing did not break what the command is for. These run
        # against the real default connection the proxy forwarded to, which is
        # where the fixture's rows actually are.
        lines = [json.loads(line) for line in export_path.read_text().splitlines() if line]
        widget_lines = [row for row in lines if row["_model"] == "thirdparty.Widget"]
        assert len(widget_lines) == 4, (
            f"the adopted export must still read the rows; got {[r['_model'] for r in lines]}"
        )
        assert _widget_count(tenant_a) == 0, "tenant A's adopted rows must be gone"
        assert _widget_count(tenant_b) == 2, "tenant B's adopted rows must survive"

    def test_the_adopted_branch_stays_on_default_without_a_router(self, four_and_two, alias_recorder, tmp_path):
        """The positive control for the test above: with no router installed,
        every adopted step asks for ``default``.

        Without this, the routed test would pass against a command that sent
        its adopted SQL to some fixed alias that merely happened to match the
        router's name, and "default not in the record" would be proving the
        wrong thing. This pins what the router is overriding.
        """
        tenant_a, _tenant_b = four_and_two
        export_path = tmp_path / "data.ndjson"

        call_command(
            "boundary_deprovision",
            tenant=tenant_a.slug,
            export=str(export_path),
            yes=True,
        )

        assert alias_recorder, "the adopted branch must resolve a connection at all"
        assert set(alias_recorder) == {"default"}, (
            f"with no router, every adopted step must ask for default; it asked for {alias_recorder}"
        )
