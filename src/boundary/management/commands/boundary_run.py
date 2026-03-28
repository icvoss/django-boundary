"""Run a management command scoped to a single tenant."""

import argparse

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from boundary.conf import get_tenant_model
from boundary.context import TenantContext


class Command(BaseCommand):
    help = "Run a management command scoped to a single tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant UUID, PK, or slug")
        parser.add_argument("command_name", help="Management command to run")
        parser.add_argument(
            "command_args",
            nargs=argparse.REMAINDER,
            help="Arguments for the inner command",
        )

    def handle(self, *args, **options):
        tenant = self._resolve_tenant(options["tenant"])
        inner_args = options.get("command_args", [])

        with TenantContext.using(tenant):
            call_command(
                options["command_name"],
                *inner_args,
                stdout=self.stdout,
                stderr=self.stderr,
            )

    def _resolve_tenant(self, identifier):
        TenantModel = get_tenant_model()
        try:
            return TenantModel.objects.get(pk=identifier)
        except (TenantModel.DoesNotExist, ValueError, TypeError):
            pass
        try:
            return TenantModel.objects.get(slug=identifier)
        except TenantModel.DoesNotExist as exc:
            raise CommandError(f"Tenant not found: {identifier}") from exc
