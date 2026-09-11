"""Tests for boundary.routing — RegionalRouter."""

import pytest

from boundary.exceptions import RegionNotConfiguredError
from boundary.routing import RegionalRouter, all_regions, require_region, specific_region
from boundary.testing import set_tenant


class TestRegionalRouterNoConfig:
    """AC-REG-003: No routing without config."""

    def test_returns_default_without_regions(self, settings):
        settings.BOUNDARY_REGIONS = None
        router = RegionalRouter()
        from boundary_testapp.models import Booking

        assert router.db_for_read(Booking) == "default"
        assert router.db_for_write(Booking) == "default"


@pytest.mark.django_db
class TestRegionalRouterWithConfig:
    """AC-REG-001/002: Routes by tenant region; non-tenant models use default."""

    @pytest.fixture(autouse=True)
    def _setup_regions(self, settings, monkeypatch):
        from boundary.context import TenantContext

        settings.BOUNDARY_REGIONS = {
            "eu-west": {"ENGINE": "django.db.backends.postgresql"},
            "us": {"ENGINE": "django.db.backends.postgresql"},
        }
        # Mock _set_db_session and _clear_db_session to avoid requiring
        # regional DB connections during routing tests (BR-CTX-009 will
        # attempt to set session var on the regional connection, but these
        # tests only need to verify routing logic).
        monkeypatch.setattr(TenantContext, "_set_db_session", staticmethod(lambda *a, **k: None))
        monkeypatch.setattr(TenantContext, "_clear_db_session", staticmethod(lambda *a, **k: None))

    @pytest.mark.django_db(databases=["default", "eu-west"])
    def test_routes_to_tenant_region(self, tenant_a):
        from boundary_testapp.models import Booking

        tenant_a.region = "eu-west"
        tenant_a.save()
        router = RegionalRouter()

        with set_tenant(tenant_a):
            assert router.db_for_read(Booking) == "eu-west"
            assert router.db_for_write(Booking) == "eu-west"

    @pytest.mark.django_db(databases=["default", "eu-west"])
    def test_non_tenant_model_uses_default(self, tenant_a):
        from django.contrib.auth.models import User

        tenant_a.region = "eu-west"
        tenant_a.save()
        router = RegionalRouter()

        with set_tenant(tenant_a):
            assert router.db_for_read(User) == "default"

    def test_no_tenant_context_uses_default(self):
        from boundary_testapp.models import Booking

        router = RegionalRouter()
        assert router.db_for_read(Booking) == "default"

    def test_unknown_region_falls_back_to_default(self, tenant_a):
        from boundary_testapp.models import Booking

        tenant_a.region = "ap-southeast"  # Not in BOUNDARY_REGIONS
        tenant_a.save()
        router = RegionalRouter()

        with set_tenant(tenant_a):
            assert router.db_for_read(Booking) == "default"

    def test_allow_relation_returns_none(self):
        router = RegionalRouter()
        assert router.allow_relation(None, None) is None

    def test_allow_migrate_returns_none(self):
        router = RegionalRouter()
        assert router.allow_migrate("default", "boundary_testapp") is None


@pytest.mark.django_db
class TestUnmatchedRegionWarnsOnce:
    """Issue #59: an unmatched tenant region must be operator-visible without
    flooding on every query.

    _route() runs on every ORM query, so an unconditional warning would
    flood; the fix warns at most once per (tenant, region) pair through a
    small bounded cache, while _route() itself must still always return
    "default" (a Django router cannot raise).
    """

    @pytest.fixture(autouse=True)
    def _setup(self, settings, monkeypatch):
        from boundary.context import TenantContext
        from boundary.routing import _warned_unmatched_regions

        settings.BOUNDARY_REGIONS = {"eu-west": {}, "us": {}}
        monkeypatch.setattr(TenantContext, "_set_db_session", staticmethod(lambda *a, **k: None))
        monkeypatch.setattr(TenantContext, "_clear_db_session", staticmethod(lambda *a, **k: None))
        # Isolate the module-level warn-once cache per test.
        _warned_unmatched_regions.clear()
        yield
        _warned_unmatched_regions.clear()

    def test_repeated_query_warns_exactly_once(self, tenant_a, caplog):
        from boundary_testapp.models import Booking

        tenant_a.region = "ap-southeast"  # Not in BOUNDARY_REGIONS
        tenant_a.save()
        router = RegionalRouter()

        with caplog.at_level("WARNING", logger="boundary.routing"), set_tenant(tenant_a):
            for _ in range(5):
                assert router.db_for_read(Booking) == "default"
                assert router.db_for_write(Booking) == "default"

        records = [r for r in caplog.records if r.name == "boundary.routing"]
        assert len(records) == 1, f"expected exactly one warning, got {len(records)}"
        assert records[0].tenant_id == str(tenant_a.pk)
        assert records[0].region == "ap-southeast"

    def test_second_tenant_produces_its_own_warning(self, tenant_a, tenant_b, caplog):
        from boundary_testapp.models import Booking

        tenant_a.region = "ap-southeast"
        tenant_a.save()
        tenant_b.region = "ap-southeast"
        tenant_b.save()
        router = RegionalRouter()

        with caplog.at_level("WARNING", logger="boundary.routing"):
            with set_tenant(tenant_a):
                router.db_for_read(Booking)
            with set_tenant(tenant_b):
                router.db_for_read(Booking)

        records = [r for r in caplog.records if r.name == "boundary.routing"]
        tenant_ids = {r.tenant_id for r in records}
        assert tenant_ids == {str(tenant_a.pk), str(tenant_b.pk)}

    def test_second_region_for_same_tenant_produces_its_own_warning(self, tenant_a, caplog):
        from boundary_testapp.models import Booking

        router = RegionalRouter()

        with caplog.at_level("WARNING", logger="boundary.routing"), set_tenant(tenant_a):
            tenant_a.region = "ap-southeast"
            tenant_a.save()
            router.db_for_read(Booking)
            tenant_a.region = "sa-east"
            tenant_a.save()
            router.db_for_read(Booking)

        records = [r for r in caplog.records if r.name == "boundary.routing"]
        regions = {r.region for r in records}
        assert regions == {"ap-southeast", "sa-east"}

    def test_route_always_returns_default_for_unmatched_region(self, tenant_a):
        """The contract requires _route() to always return an alias."""
        from boundary_testapp.models import Booking

        tenant_a.region = "nonexistent"
        tenant_a.save()
        router = RegionalRouter()

        with set_tenant(tenant_a):
            assert router.db_for_read(Booking) == "default"
            assert router.db_for_write(Booking) == "default"


@pytest.mark.django_db
class TestAllRegions:
    """AC-REG-004: all_regions iteration."""

    def test_yields_all_region_keys(self, settings):
        settings.BOUNDARY_REGIONS = {
            "eu-west": {},
            "us": {},
            "ap": {},
        }
        with all_regions() as aliases:
            assert set(aliases) == {"eu-west", "us", "ap"}

    def test_yields_default_without_config(self, settings):
        settings.BOUNDARY_REGIONS = None
        with all_regions() as aliases:
            assert aliases == ["default"]


@pytest.mark.django_db
class TestSpecificRegion:
    """AC-REG-005: specific_region pinning."""

    @pytest.fixture(autouse=True)
    def _mock_db_session(self, monkeypatch):
        """Mock _set_db_session to avoid requiring regional DB connections."""
        from boundary.context import TenantContext

        monkeypatch.setattr(TenantContext, "_set_db_session", staticmethod(lambda *a, **k: None))

    def test_overrides_tenant_region(self, tenant_a, settings):
        from boundary_testapp.models import Booking

        settings.BOUNDARY_REGIONS = {
            "eu-west": {},
            "us": {},
        }
        tenant_a.region = "us"
        tenant_a.save()
        router = RegionalRouter()

        with set_tenant(tenant_a):
            with specific_region("eu-west"):
                assert router.db_for_read(Booking) == "eu-west"
            # After exiting, routes back to tenant's region
            assert router.db_for_read(Booking) == "us"

    def test_unknown_override_falls_back(self, settings):
        from boundary_testapp.models import Booking

        settings.BOUNDARY_REGIONS = {"eu-west": {}}
        router = RegionalRouter()

        with specific_region("nonexistent"):
            assert router.db_for_read(Booking) == "default"


@pytest.mark.django_db
class TestRequireRegion:
    """Issue #6: require_region() raises instead of silently using default."""

    @pytest.fixture(autouse=True)
    def _mock_db_session(self, monkeypatch):
        """Mock _set_db_session to avoid requiring regional DB connections."""
        from boundary.context import TenantContext

        monkeypatch.setattr(TenantContext, "_set_db_session", staticmethod(lambda *a, **k: None))

    @pytest.mark.django_db(databases=["default", "eu-west"])
    def test_returns_region_for_routable_tenant(self, tenant_a, settings):
        settings.BOUNDARY_REGIONS = {"eu-west": {}, "us": {}}
        tenant_a.region = "eu-west"
        tenant_a.save()
        with set_tenant(tenant_a):
            assert require_region() == "eu-west"

    def test_accepts_explicit_tenant(self, tenant_a, settings):
        settings.BOUNDARY_REGIONS = {"eu-west": {}}
        tenant_a.region = "eu-west"
        tenant_a.save()
        assert require_region(tenant_a) == "eu-west"

    def test_raises_when_regions_unconfigured(self, settings):
        settings.BOUNDARY_REGIONS = None
        with pytest.raises(RegionNotConfiguredError):
            require_region()

    def test_raises_when_no_tenant_active(self, settings):
        settings.BOUNDARY_REGIONS = {"eu-west": {}}
        with pytest.raises(RegionNotConfiguredError):
            require_region()

    def test_raises_for_unknown_region(self, tenant_a, settings):
        settings.BOUNDARY_REGIONS = {"eu-west": {}}
        tenant_a.region = "ap-southeast"
        tenant_a.save()
        with pytest.raises(RegionNotConfiguredError):
            require_region(tenant_a)
