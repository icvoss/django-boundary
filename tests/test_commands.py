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


@pytest.mark.django_db
class TestBoundaryRun:
    """AC-CMD-005: boundary_run executes command with tenant context."""

    def test_scoped_execution(self, tenant_a, capsys):
        """The inner command runs with tenant context active."""
        call_command("boundary_run", f"--tenant={tenant_a.slug}", "showmigrations", "--list")

    def test_nonexistent_tenant_raises(self):
        with pytest.raises(CommandError, match="not found"):
            call_command("boundary_run", "--tenant=nonexistent", "showmigrations")


@pytest.mark.django_db
class TestBoundaryRunAll:
    """AC-CMD-006/007: boundary_run_all with parallel and region filter."""

    def test_runs_for_all_active_tenants(self, tenant_a, tenant_b, capsys):
        call_command("boundary_run_all", "showmigrations")

    def test_json_output(self, tenant_a, capsys):
        call_command("boundary_run_all", "showmigrations", json_output=True)
        output = capsys.readouterr().out.strip()
        for line in output.split("\n"):
            if line:
                data = json.loads(line)
                assert "tenant" in data
                assert "status" in data

    def test_region_filter(self, tenant_a, tenant_b, capsys):
        tenant_a.region = "eu-west"
        tenant_a.save()
        tenant_b.region = "us"
        tenant_b.save()

        call_command("boundary_run_all", "showmigrations", region="eu-west", json_output=True)
        output = capsys.readouterr().out.strip()
        results = [json.loads(line) for line in output.split("\n") if line]
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
        results = [json.loads(line) for line in output.split("\n") if line]
        slugs = [r["tenant"] for r in results]
        assert tenant_a.slug in slugs
        assert tenant_b.slug not in slugs
