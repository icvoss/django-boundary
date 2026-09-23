"""Tests for boundary.models.tenant_unique() - per-tenant uniqueness.

AC-ORM-016 (BR-ORM-015, issue #72). A field declared ``unique=True`` on a
tenant-scoped model is unique across every tenant, not within one;
``tenant_unique()`` is the affordance for saying what the author meant.

AC-ORM-017 (boundary.W010) is void: BR-ORM-015 makes the check conditional on
a prototype showing more genuine hits than spurious ones, and the prototype
recorded on icvoss/django-boundary#72 on 2026-09-23 found one genuine hit
against twenty spurious ones. W010 is RESERVED, NOT IMPLEMENTED; the helper
and the documented pattern ship regardless, and this module is BR-ORM-015's
whole verification.
"""

import pytest
from django.db import IntegrityError, models, transaction

from boundary.models import (
    TenantUniqueConstraint,
    make_tenant_path_mixin,
    tenant_unique,
)
from boundary.testing import set_tenant


class TestConstraintResolution:
    """The tenant FK field is resolved at class preparation, not call time.

    No database: these assert on ``_meta``, which is built at import time.
    """

    def test_tenant_mixin_model_leads_with_tenant(self):
        from boundary_testapp.models import UniqueDoc

        constraint = _only_tenant_unique(UniqueDoc)
        assert constraint.fields == ("tenant", "slug"), (
            f"tenant_unique('slug') on a TenantMixin model must resolve to ('tenant', 'slug'); got {constraint.fields}"
        )

    def test_make_tenant_mixin_model_leads_with_its_own_fk_name(self):
        """The point of resolving at class preparation. A helper that
        hardcoded "tenant", or read the FK name when the Meta body ran (when
        no model exists to read it from), would produce ('tenant', 'slug')
        here and the constraint would name a column the table does not have.
        """
        from boundary_testapp.models import UniqueProduct

        constraint = _only_tenant_unique(UniqueProduct)
        assert constraint.fields == ("merchant", "slug"), (
            f"tenant_unique('slug') on a make_tenant_mixin('merchant') model must "
            f"resolve to ('merchant', 'slug'); got {constraint.fields}"
        )

    def test_the_two_models_share_a_helper_call_and_differ_only_in_fk_name(self):
        """Direction check: the two fixtures above declare the identical
        ``tenant_unique("slug")`` call, so the only thing that can account for
        the different first field is resolution against the model."""
        from boundary_testapp.models import UniqueDoc, UniqueProduct

        doc = _only_tenant_unique(UniqueDoc)
        product = _only_tenant_unique(UniqueProduct)
        assert doc.tenant_fields == product.tenant_fields == ("slug",)
        assert doc.fields[0] != product.fields[0]

    def test_resolution_survives_djangos_constraint_clone(self):
        """``Options.contribute_to_class`` clones every constraint through
        ``deconstruct()`` for name interpolation, so the object in
        ``_meta.constraints`` is a rebuilt instance. A subclass whose state was
        not in ``deconstruct()`` would arrive with it lost."""
        from boundary_testapp.models import UniqueDoc

        constraint = _only_tenant_unique(UniqueDoc)
        _, _, kwargs = constraint.deconstruct()
        assert kwargs["tenant_fields"] == ("slug",)
        assert kwargs["fields"] == ("tenant", "slug")

        clone = constraint.clone()
        assert isinstance(clone, TenantUniqueConstraint)
        assert clone.fields == ("tenant", "slug")

    def test_path_scoped_model_raises_at_class_preparation(self):
        """A path-scoped model has no local tenant column to lead with, so
        per-tenant uniqueness is not expressible for it. Saying so at
        preparation is better than a migration that fails later."""
        PathMixin = make_tenant_path_mixin("brand__merchant")

        with pytest.raises(ValueError) as excinfo:

            class PathScopedDoc(PathMixin):
                code = models.CharField(max_length=20)

                class Meta:
                    app_label = "boundary_testapp"
                    constraints = [tenant_unique("code")]

        message = str(excinfo.value)
        assert "PathScopedDoc" in message, f"the message must name the model; got {message}"
        assert "local tenant column" in message


class TestMigrationStateRendersTheResolvedConstraint:
    """A migration state render prepares a model with no tenant FK field.

    BR-ORM-015. ``ModelState.render()`` rebuilds the model with
    ``type(name, bases, body)`` on ``models.Model`` rather than on the tenant
    mixin, so ``_boundary_fk_field`` is absent from the rendered class, while
    the constraint it carries is ALREADY resolved: it round-tripped through
    ``deconstruct()``, which serialises both the resolved ``fields`` and the
    author's ``tenant_fields``.

    The regression this pins: ``_resolve()`` used to check for the FK field
    before checking whether there was anything left to resolve, so every state
    render raised. That is every ``migrate`` and every ``makemigrations``, and
    on this suite it meant the test database could not be created at all, so
    all 284 database-backed tests errored in setup rather than one test
    failing. No database here: state rendering reads the app registry.
    """

    def test_rendering_the_model_from_project_state_does_not_raise(self):
        """The defect's own reproduction, at the smallest scope that shows it."""
        from django.apps import apps
        from django.db.migrations.state import ProjectState

        rendered = ProjectState.from_apps(apps).apps.get_model("boundary_testapp", "UniqueDoc")

        assert rendered is not None

    def test_the_rendered_models_constraint_keeps_the_resolved_fields(self):
        """Not merely "does not raise": the rendered constraint must still
        carry the tenant column, since that field list is what the schema
        editor builds the UNIQUE constraint from. An early return that also
        dropped the resolution would pass the test above and ship a constraint
        on the author's fields alone."""
        from django.apps import apps
        from django.db.migrations.state import ProjectState

        rendered = ProjectState.from_apps(apps).apps.get_model("boundary_testapp", "UniqueDoc")

        constraint = _only_tenant_unique(rendered)
        assert constraint.fields == ("tenant", "slug"), (
            f"the state-rendered constraint must keep its resolved fields; got {constraint.fields}"
        )

    def test_the_rendered_class_really_lacks_the_attribute_resolution_needs(self):
        """Direction check, so the two tests above cannot pass vacuously.

        If the rendered class carried ``_boundary_fk_field`` after all, the
        ordering in ``_resolve()`` would be irrelevant and this module would be
        proving nothing. It does not carry it, which is exactly why the
        already-resolved check has to come first.
        """
        from django.apps import apps
        from django.db.migrations.state import ProjectState

        rendered = ProjectState.from_apps(apps).apps.get_model("boundary_testapp", "UniqueDoc")

        assert getattr(rendered, "_boundary_fk_field", None) is None
        from boundary_testapp.models import UniqueDoc

        assert UniqueDoc._boundary_fk_field == "tenant", (
            "the real model must carry the attribute the rendered one lacks, "
            "otherwise the contrast this test draws does not exist"
        )

    def test_migration_writer_serialises_the_resolved_form(self):
        """What makemigrations writes into a migration file. The resolved
        ``fields`` and the author's ``tenant_fields`` both appear, which is
        what lets the constraint arrive in state already resolved and needing
        nothing from the model."""
        from boundary_testapp.models import UniqueDoc
        from django.db.migrations.writer import MigrationWriter

        statement, imports = MigrationWriter.serialize(_only_tenant_unique(UniqueDoc))

        assert "boundary.models.TenantUniqueConstraint" in statement
        assert "fields=('tenant', 'slug')" in statement, (
            f"the serialised form must carry the resolved fields; got {statement}"
        )
        assert "tenant_fields=('slug',)" in statement, (
            f"the serialised form must carry the author's fields too; got {statement}"
        )
        assert "import boundary.models" in imports

    def test_a_constraint_rebuilt_from_the_serialised_kwargs_resolves_no_further(self):
        """The apply-time path: a migration file's constraint is reconstructed
        from those kwargs, and must not prepend the tenant column a second
        time when the model it is applied against prepares."""
        from boundary_testapp.models import UniqueDoc

        rebuilt = TenantUniqueConstraint(
            fields=("tenant", "slug"),
            tenant_fields=("slug",),
            name="rebuilt_from_a_migration",
        )

        rebuilt._resolve(UniqueDoc)

        assert rebuilt.fields == ("tenant", "slug"), f"resolution must be idempotent; got {rebuilt.fields}"


class TestConstraintNaming:
    def test_an_explicit_name_is_used_verbatim(self):
        constraint = tenant_unique("a", "b", name="inv_ab")
        assert constraint.name == "inv_ab"

    def test_two_field_lists_on_one_model_get_distinct_names(self):
        """Two constraints on one model cannot collide (BR-ORM-015)."""
        from boundary_testapp.models import UniqueLabel

        names = [c.name for c in UniqueLabel._meta.constraints]
        assert len(names) == len(set(names)), f"names collided: {names}"

    def test_an_auto_derived_name_is_deterministic(self):
        """Same field list, same name, so a migration written today and
        regenerated tomorrow do not disagree about what to drop."""
        assert tenant_unique("slug").name == tenant_unique("slug").name
        assert tenant_unique("slug").name != tenant_unique("slug", "year").name

    def test_an_auto_derived_name_is_interpolated_with_the_model(self):
        """The model half comes from Django's own
        ``%(app_label)s``/``%(class)s`` mechanism, which is what makes the
        same field list on two models produce two names."""
        from boundary_testapp.models import UniqueDoc

        assert tenant_unique("slug").name.startswith("%(app_label)s_%(class)s_")
        resolved = _only_tenant_unique(UniqueDoc).name
        assert "%" not in resolved
        assert resolved.startswith("boundary_testapp_uniquedoc_")

    def test_an_auto_derived_name_fits_postgresqls_identifier_limit(self):
        """A joined-field-names scheme would not; the digest is why this
        holds for any field list at all."""
        from boundary_testapp.models import UniqueDoc, UniqueLabel, UniqueProduct

        long_fields = [f"a_very_long_field_name_indeed_{i}" for i in range(6)]
        for name in [
            tenant_unique(*long_fields).name % {"app_label": "boundary_testapp", "class": "uniquedoc"},
            *(c.name for m in (UniqueDoc, UniqueLabel, UniqueProduct) for c in m._meta.constraints),
        ]:
            assert len(name) <= 63, f"{name} is {len(name)} characters"


@pytest.mark.django_db(transaction=True)
class TestPerTenantUniquenessIsEnforced:
    """The constraint exists in the database and scopes uniqueness to a tenant.

    ``transaction=True`` on the PostgreSQL default alias: the IntegrityError
    assertions need a real commit boundary to roll back to, and the point of
    the class is the database's own behaviour, not the ORM's.
    """

    def test_two_tenants_may_hold_the_same_slug(self, tenant_a, tenant_b):
        from boundary_testapp.models import UniqueDoc

        with set_tenant(tenant_a):
            UniqueDoc.objects.create(slug="annual-report")
        with set_tenant(tenant_b):
            UniqueDoc.objects.create(slug="annual-report")

        assert UniqueDoc.unscoped.filter(slug="annual-report").count() == 2

    def test_one_tenant_may_not_hold_the_same_slug_twice(self, tenant_a):
        from boundary_testapp.models import UniqueDoc

        with set_tenant(tenant_a):
            UniqueDoc.objects.create(slug="annual-report")
            with pytest.raises(IntegrityError), transaction.atomic():
                UniqueDoc.objects.create(slug="annual-report")

    def test_the_same_holds_for_a_custom_fk_name_model(self, tenant_a, tenant_b):
        """The merchant-flavoured model is where a hardcoded "tenant" would
        have produced a constraint on a column that does not exist, so the
        table would either have failed to create or carry no constraint at
        all; both halves are asserted here."""
        from boundary_testapp.models import UniqueProduct

        with set_tenant(tenant_a):
            UniqueProduct.objects.create(slug="widget")
        with set_tenant(tenant_b):
            UniqueProduct.objects.create(slug="widget")

        with set_tenant(tenant_a), pytest.raises(IntegrityError), transaction.atomic():
            UniqueProduct.objects.create(slug="widget")

    def test_the_constraint_columns_in_the_database_lead_with_the_tenant(self):
        """Read from ``pg_constraint`` rather than from ``_meta``: the ORM
        view of the constraint and the constraint PostgreSQL actually built
        are different facts, and only the second one isolates anything."""
        from django.db import connection

        for table, expected in (
            ("boundary_testapp_uniquedoc", ["tenant_id", "slug"]),
            ("boundary_testapp_uniqueproduct", ["merchant_id", "slug"]),
        ):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT c.conname,
                           array_agg(a.attname ORDER BY k.ord)
                    FROM pg_constraint c
                    JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
                    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                    WHERE c.conrelid = to_regclass(%s)::oid
                      AND c.contype = 'u'
                    GROUP BY c.conname
                    """,
                    [table],
                )
                found = {name: list(columns) for name, columns in cursor.fetchall()}

            assert expected in found.values(), (
                f"expected a UNIQUE constraint on {table} with columns {expected}; got {found}"
            )


def _only_tenant_unique(model):
    """Return the single TenantUniqueConstraint on *model*."""
    constraints = [c for c in model._meta.constraints if isinstance(c, TenantUniqueConstraint)]
    assert constraints, f"{model.__name__} declares no tenant_unique() constraint"
    return constraints[0]


def test_tenant_unique_requires_at_least_one_field():
    """Control: the empty call is a mistake, not a constraint on the tenant
    column alone, which would make every row in a tenant collide."""
    with pytest.raises(ValueError):
        tenant_unique()
