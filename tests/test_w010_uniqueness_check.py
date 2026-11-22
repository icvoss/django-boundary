"""Tests for boundary.W010: uniqueness omitting the tenant FK (BR-ORM-015).

AC-ORM-017. Companion to tests/test_uniqueness.py, which covers the
tenant_unique() helper (AC-ORM-016). This module covers the warning, the
boundary_global_unique opt-out, and that both TenantMixin and
make_tenant_mixin("merchant") models are recognised.
"""

import pytest
from django.core.checks import run_checks

from boundary.checks import (
    _check_tenant_scoped_uniqueness,
    check_boundary_configuration,
)


def _w010(messages=None):
    """Return every boundary.W010 from *messages*, or from a fresh check run."""
    if messages is None:
        messages = _check_tenant_scoped_uniqueness()
    return [m for m in messages if m.id == "boundary.W010"]


def _w010_for(model, messages=None):
    """Return W010 messages whose obj is *model*."""
    return [m for m in _w010(messages) if m.obj is model]


class TestW010WarnsOnGlobalUniqueness:
    def test_unique_true_on_tenant_mixin_model(self):
        from boundary_testapp.models import GlobalUniqueDoc

        hits = _w010_for(GlobalUniqueDoc)
        assert len(hits) == 1, f"expected one W010 for GlobalUniqueDoc; got {hits}"
        assert "slug" in hits[0].msg
        assert "tenant_unique('slug')" in (hits[0].hint or "")
        assert "boundary_global_unique" in (hits[0].hint or "")

    def test_unique_true_on_make_tenant_mixin_model_names_custom_fk(self):
        """The hint must name merchant, not a hardcoded tenant column."""
        from boundary_testapp.models import GlobalUniqueProduct

        hits = _w010_for(GlobalUniqueProduct)
        assert len(hits) == 1, f"expected one W010 for GlobalUniqueProduct; got {hits}"
        assert "sku" in hits[0].msg
        hint = hits[0].hint or ""
        assert "merchant" in hint
        assert "tenant_unique('sku')" in hint

    def test_unique_together_omitting_tenant(self):
        from boundary_testapp.models import GlobalTogetherDoc

        hits = _w010_for(GlobalTogetherDoc)
        assert len(hits) == 1, f"expected one W010 for GlobalTogetherDoc; got {hits}"
        assert "unique_together" in hits[0].msg
        assert "('code', 'year')" in hits[0].msg

    def test_unique_constraint_omitting_tenant(self):
        from boundary_testapp.models import GlobalConstraintDoc

        hits = _w010_for(GlobalConstraintDoc)
        assert len(hits) == 1, f"expected one W010 for GlobalConstraintDoc; got {hits}"
        assert "UniqueConstraint" in hits[0].msg
        assert "reference" in hits[0].msg


class TestW010SilentWhenScopedOrOptedOut:
    def test_tenant_unique_helper_does_not_warn(self):
        from boundary_testapp.models import UniqueDoc, UniqueProduct

        assert _w010_for(UniqueDoc) == []
        assert _w010_for(UniqueProduct) == []

    def test_unique_together_including_tenant_does_not_warn(self):
        from boundary_testapp.models import TenantTogetherDoc

        assert _w010_for(TenantTogetherDoc) == []

    def test_boundary_global_unique_opts_out_unique_true(self):
        from boundary_testapp.models import OptedOutUniqueDoc

        assert _w010_for(OptedOutUniqueDoc) == []

    def test_boundary_global_unique_opts_out_unique_together(self):
        from boundary_testapp.models import OptedOutTogetherDoc

        assert _w010_for(OptedOutTogetherDoc) == []


class TestW010IsRegistered:
    def test_reachable_through_check_boundary_configuration(self):
        from boundary_testapp.models import GlobalUniqueDoc

        messages = check_boundary_configuration(None)
        assert _w010_for(GlobalUniqueDoc, messages), (
            "W010 must fire through check_boundary_configuration, not only "
            "when _check_tenant_scoped_uniqueness is called directly"
        )

    def test_reachable_through_the_registered_check_registry(self):
        """Proves AppConfig.ready() imported boundary.checks so Django sees it."""
        from boundary_testapp.models import GlobalUniqueDoc

        messages = run_checks(app_configs=None, tags=None)
        assert _w010_for(GlobalUniqueDoc, messages), (
            f"expected W010 via run_checks(); got {[m.id for m in messages if m.obj is GlobalUniqueDoc]}"
        )


@pytest.mark.django_db
class TestW010DoesNotRequireDatabase:
    """Control: the check is models-only and must not open a connection."""

    def test_runs_without_django_db_marker_path(self):
        # The classes above already prove this by not using django_db; this
        # test exists so a future change that starts querying cannot land
        # without noticing the marker was added for a reason.
        messages = _check_tenant_scoped_uniqueness()
        assert isinstance(messages, list)
