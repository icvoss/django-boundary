"""Tests for indirect / traversal tenancy (make_tenant_path_mixin).

Covers models scoped to a tenant through a relation rather than a local FK:
auto-filtering on the lookup path, write paths correctly skipping the
(absent) column, and registry/check integration.
"""

import pytest

from boundary.exceptions import TenantNotSetError
from boundary.models import (
    get_tenant_fk_field,
    get_tenant_lookup,
    has_tenant_column,
    is_tenant_model,
)
from boundary.testing import set_tenant


def _make_brand(tenant, name="B"):
    from boundary_testapp.models import Brand

    return Brand.objects.create(name=name)


@pytest.mark.django_db
class TestPathFiltering:
    """Auto-filtering follows the declared boundary_tenant_path."""

    def test_single_hop_filtering(self, tenant_a, tenant_b):
        from boundary_testapp.models import BrandAsset

        with set_tenant(tenant_a):
            brand_a = _make_brand(tenant_a, "A")
            BrandAsset.objects.create(brand=brand_a, label="a1")
            BrandAsset.objects.create(brand=brand_a, label="a2")

        with set_tenant(tenant_b):
            brand_b = _make_brand(tenant_b, "B")
            BrandAsset.objects.create(brand=brand_b, label="b1")

        with set_tenant(tenant_a):
            assert BrandAsset.objects.count() == 2
        with set_tenant(tenant_b):
            assert BrandAsset.objects.count() == 1

    def test_multi_hop_filtering(self, tenant_a, tenant_b):
        from boundary_testapp.models import AssetVariant, BrandAsset

        with set_tenant(tenant_a):
            brand_a = _make_brand(tenant_a, "A")
            asset_a = BrandAsset.objects.create(brand=brand_a, label="a1")
            AssetVariant.objects.create(asset=asset_a, fmt="webp")
            AssetVariant.objects.create(asset=asset_a, fmt="png")

        with set_tenant(tenant_b):
            brand_b = _make_brand(tenant_b, "B")
            asset_b = BrandAsset.objects.create(brand=brand_b, label="b1")
            AssetVariant.objects.create(asset=asset_b, fmt="webp")

        with set_tenant(tenant_a):
            assert AssetVariant.objects.count() == 2
        with set_tenant(tenant_b):
            assert AssetVariant.objects.count() == 1

    def test_unscoped_returns_all(self, tenant_a, tenant_b):
        from boundary_testapp.models import BrandAsset

        with set_tenant(tenant_a):
            brand_a = _make_brand(tenant_a, "A")
            BrandAsset.objects.create(brand=brand_a, label="a1")
        with set_tenant(tenant_b):
            brand_b = _make_brand(tenant_b, "B")
            BrandAsset.objects.create(brand=brand_b, label="b1")

        with set_tenant(tenant_a):
            assert BrandAsset.unscoped.count() == 2

    def test_strict_mode_raises_without_context(self, settings):
        """Path models get the same strict-mode protection as direct-FK ones."""
        from boundary_testapp.models import BrandAsset

        settings.BOUNDARY_STRICT_MODE = True
        with pytest.raises(TenantNotSetError):
            BrandAsset.objects.count()


@pytest.mark.django_db
class TestPathWritePathsSkipColumn:
    """save / bulk_create / bulk_update must not touch a non-existent column."""

    def test_save_does_not_populate_column(self, tenant_a):
        from boundary_testapp.models import BrandAsset

        with set_tenant(tenant_a):
            brand_a = _make_brand(tenant_a, "A")
            # Should not raise AttributeError trying to set a tenant FK
            asset = BrandAsset.objects.create(brand=brand_a, label="a1")
            assert asset.pk is not None
            assert not hasattr(asset, "tenant_id")
            assert not hasattr(asset, "merchant_id")

    def test_bulk_create_no_column_logic(self, tenant_a):
        from boundary_testapp.models import BrandAsset

        with set_tenant(tenant_a):
            brand_a = _make_brand(tenant_a, "A")
            created = BrandAsset.objects.bulk_create(
                [
                    BrandAsset(brand=brand_a, label="a1"),
                    BrandAsset(brand=brand_a, label="a2"),
                ]
            )
            assert len(created) == 2
            assert BrandAsset.objects.count() == 2

    def test_bulk_update_no_cross_tenant_check(self, tenant_a):
        from boundary_testapp.models import BrandAsset

        with set_tenant(tenant_a):
            brand_a = _make_brand(tenant_a, "A")
            asset = BrandAsset.objects.create(brand=brand_a, label="a1")
            asset.label = "a1-updated"
            BrandAsset.objects.bulk_update([asset], ["label"])
            assert BrandAsset.objects.get(pk=asset.pk).label == "a1-updated"

    def test_create_without_context_does_not_require_tenant(self, tenant_a):
        """A path model has nothing to stamp, so create needs no active tenant
        for the populate step (the parent relation carries scoping)."""
        from boundary_testapp.models import Brand, BrandAsset

        # Build a brand under a tenant, then create the asset with no context.
        with set_tenant(tenant_a):
            brand_a = Brand.objects.create(name="A")
        # No active tenant here; path model create must not raise TenantNotSetError.
        asset = BrandAsset.unscoped.create(brand=brand_a, label="x")
        assert asset.pk is not None


@pytest.mark.django_db
class TestPathRegistry:
    """Registry / introspection helpers classify path models correctly."""

    def test_is_tenant_model_true(self):
        from boundary_testapp.models import AssetVariant, BrandAsset

        assert is_tenant_model(BrandAsset)
        assert is_tenant_model(AssetVariant)

    def test_has_tenant_column_false(self):
        from boundary_testapp.models import BrandAsset

        assert has_tenant_column(BrandAsset) is False

    def test_get_tenant_fk_field_none(self):
        from boundary_testapp.models import BrandAsset

        # No local column → the column accessor returns None ...
        assert get_tenant_fk_field(BrandAsset) is None

    def test_get_tenant_lookup_returns_path(self):
        from boundary_testapp.models import AssetVariant, BrandAsset

        assert get_tenant_lookup(BrandAsset) == "brand__merchant"
        assert get_tenant_lookup(AssetVariant) == "asset__brand__merchant"

    def test_direct_fk_model_unaffected(self):
        from boundary_testapp.models import Product

        assert has_tenant_column(Product) is True
        assert get_tenant_fk_field(Product) == "merchant"
        assert get_tenant_lookup(Product) == "merchant"


class TestRLSCheckSkipsPathModels:
    """The RLS system check must not flag path-scoped models (no column)."""

    def test_path_model_excluded_from_rls_check(self):
        from boundary.checks import _check_rls_enabled

        # Run the check; it must not raise and must not emit E006 for the
        # path-scoped test models (which have no table column to secure).
        errors = _check_rls_enabled()
        flagged = {e.msg for e in errors}
        assert not any("BrandAsset" in m for m in flagged)
        assert not any("AssetVariant" in m for m in flagged)
