# Write tenant-safe tests

## Goal

Write tests that activate a tenant before exercising tenant-scoped models, assert
that data does not leak between tenants, and avoid the common strict-mode failures
that bite tenant-aware test suites.

## Prerequisites

- `django-boundary` installed and `BOUNDARY_TENANT_MODEL` configured. See the
  [README Quick Start](../../README.md#quick-start).
- At least one model that inherits from `TenantMixin`, `TenantModel`, or a mixin
  built with `make_tenant_mixin()`.
- A test runner (pytest with `pytest-django`, or Django's `TestCase`). Both are
  supported by the helpers in `boundary.testing`.

Boundary ships three test helpers from `boundary.testing`:

- `set_tenant(tenant)`: a context manager that activates a tenant for a block.
- `tenant_factory(**kwargs)`: creates a tenant with unique defaults.
- `TenantTestMixin`: a `TestCase` mixin that creates `self.tenant` and activates
  it for the whole test.

## Steps

### 1. Activate a tenant with `set_tenant`

`set_tenant` wraps `TenantContext.using()`. Inside the block, the default manager
filters every query to that tenant, and `save()`/`create()` auto-populate the
tenant FK from context. On exit, the context is cleared.

```python
import pytest

from boundary.context import TenantContext
from boundary.testing import set_tenant, tenant_factory


@pytest.mark.django_db
def test_booking_is_scoped_to_active_tenant():
    tenant = tenant_factory()

    with set_tenant(tenant):
        booking = Booking.objects.create(court=1)
        assert booking.tenant == tenant
        assert TenantContext.get() == tenant

    # Context is cleared on exit.
    assert TenantContext.get() is None
```

`tenant_factory()` generates a unique `slug` and `name` per call, so it is safe to
call repeatedly without hitting the `slug` unique constraint. Pass keyword
arguments to override the defaults, for example
`tenant_factory(name="Acme", slug="acme")`. All kwargs are forwarded to the tenant
model's `create()`.

### 2. Use `TenantTestMixin` for class-based tests

For `TestCase` classes, `TenantTestMixin` creates `self.tenant` in `setUp` and
activates its context for the duration of each test, then tears the context down
in `tearDown`. You do not need a `with` block: the tenant is active for the whole
test method.

```python
from django.test import TestCase

from boundary.context import TenantContext
from boundary.testing import TenantTestMixin


class BookingTests(TenantTestMixin, TestCase):
    def test_tenant_is_active(self):
        assert TenantContext.get() == self.tenant

    def test_create_is_auto_scoped(self):
        booking = Booking.objects.create(court=1)
        assert booking.tenant == self.tenant
```

Put `TenantTestMixin` first in the MRO so its `setUp`/`tearDown` run around
`TestCase`'s.

### 3. Customise the tenant via `get_tenant_factory_kwargs`

Override `get_tenant_factory_kwargs` to control the tenant that `TenantTestMixin`
creates. It returns a dict of kwargs passed straight to `tenant_factory`.

```python
class RegionalBookingTests(TenantTestMixin, TestCase):
    def get_tenant_factory_kwargs(self):
        return {"name": "UK Club", "region": "uk"}

    def test_region_is_set(self):
        assert self.tenant.region == "uk"
```

The `region` field above comes from `AbstractTenant`. If your tenant model does not
define a field, do not pass it. Only return kwargs your tenant model's `create()`
accepts.

### 4. Test isolation between two tenants

Create data under one tenant, then switch context and assert the other tenant
cannot see it. This is the core leakage test and should exist for every scoped
model.

```python
@pytest.mark.django_db
def test_tenants_cannot_see_each_others_bookings():
    tenant_a = tenant_factory()
    tenant_b = tenant_factory()

    with set_tenant(tenant_a):
        Booking.objects.create(court=1)
        Booking.objects.create(court=2)

    with set_tenant(tenant_b):
        Booking.objects.create(court=3)

    with set_tenant(tenant_a):
        assert Booking.objects.count() == 2

    with set_tenant(tenant_b):
        assert Booking.objects.count() == 1
```

Nesting `set_tenant` blocks works correctly: on exit, boundary restores the
*previous* tenant context rather than simply clearing it, so you can nest a
`tenant_b` block inside a `tenant_a` block and the outer scope survives.

### 5. Assert across all tenants with the unscoped manager

The default manager (`objects`) is filtered, so you cannot use it to assert "the
correct total number of rows exists across both tenants". Use the `unscoped`
manager, which returns every row regardless of the active context. It is ideal for
admin-style assertions and for confirming a row really was written under the right
tenant.

```python
@pytest.mark.django_db
def test_unscoped_sees_all_tenants():
    tenant_a = tenant_factory()
    tenant_b = tenant_factory()

    with set_tenant(tenant_a):
        Booking.objects.create(court=1)
    with set_tenant(tenant_b):
        Booking.objects.create(court=2)

    # Filtered manager only sees the active tenant.
    with set_tenant(tenant_a):
        assert Booking.objects.count() == 1
        # Unscoped manager sees both, even with tenant_a active.
        assert Booking.unscoped.count() == 2
```

`unscoped.create()` does not auto-populate the tenant FK, so you must pass the
tenant explicitly when you create through it:

```python
with set_tenant(tenant_a):
    # Writes a row for tenant_b while tenant_a is active.
    booking = Booking.unscoped.create(court=1, tenant=tenant_b)
    assert booking.tenant == tenant_b
```

Omitting the tenant on an `unscoped.create()` for a non-nullable FK raises
`IntegrityError`, because nothing populates the column for you.

### 6. Test strict-mode behaviour deliberately

`BOUNDARY_STRICT_MODE` defaults to `True`. Under strict mode, any query through the
default manager with no active tenant raises `TenantNotSetError`. Test both paths
by flipping the setting.

```python
import pytest

from boundary.exceptions import TenantNotSetError


@pytest.mark.django_db
def test_strict_mode_raises_without_tenant(settings):
    settings.BOUNDARY_STRICT_MODE = True
    with pytest.raises(TenantNotSetError):
        Booking.objects.count()


@pytest.mark.django_db
def test_non_strict_returns_all_without_tenant(settings):
    settings.BOUNDARY_STRICT_MODE = False
    with set_tenant(tenant_factory()):
        Booking.objects.create(court=1)
    # No active tenant, strict mode off: returns unfiltered.
    assert Booking.objects.count() == 1
```

Use the pytest `settings` fixture (or Django's `override_settings`) so the change
is scoped to the single test. With `TestCase`, use `@override_settings` on the
method or class.

### 7. Call a class-based view directly with `call_view`

`RequestFactory` bypasses middleware, so a CBV called directly in a test has no
active tenant and any scoped query inside it raises `TenantNotSetError`. Use
`call_view` from `boundary.testing` to build the request and activate a tenant in
one line.

```python
from boundary.testing import call_view


@pytest.mark.django_db
def test_list_view_is_scoped(tenant_a, tenant_b):
    with set_tenant(tenant_a):
        Booking.objects.create(court=1)
    with set_tenant(tenant_b):
        Booking.objects.create(court=2)

    response = call_view(BookingListView, tenant=tenant_a)
    assert response.status_code == 200
    # the view only saw tenant_a's row
```

Pass URL kwargs via `view_kwargs`, choose the HTTP method with `method`, and
forward anything else (request body, headers) as keyword arguments:

```python
response = call_view(
    BookingCreateView,
    tenant=tenant_a,
    method="post",
    data={"court": 1},
)
detail = call_view(
    BookingDetailView, tenant=tenant_a, view_kwargs={"pk": booking.pk}
)
```

Prefer `call_view` over the Django test client when you want to exercise the view
class directly without routing through URLconf and middleware.

## Verify it worked

Run the suite and confirm both the isolation and strict-mode tests pass:

```bash
pytest packages/django-boundary/tests/test_models.py packages/django-boundary/tests/test_testing.py -v
```

A correctly isolated model shows the filtered manager counts differ per tenant
while the `unscoped` count equals the sum. A correctly configured strict mode
raises `TenantNotSetError` when no tenant is active.

## Common pitfalls

- **No active tenant in strict mode.** A bare `Booking.objects.count()` at module
  import time or in a fixture that runs outside `set_tenant` raises
  `TenantNotSetError`. Wrap every scoped query in `set_tenant` or use
  `TenantTestMixin`.
- **Asserting totals with the filtered manager.** `objects` only ever returns the
  active tenant's rows. Use `unscoped` for cross-tenant assertions.
- **`unscoped.create()` without a tenant.** It skips auto-populate, so a
  non-nullable FK raises `IntegrityError`. Always pass `tenant=...` to
  `unscoped.create()`.
- **Reusing a fixed `slug`.** Hard-coding `slug="club"` across tests collides on
  the unique constraint. Prefer `tenant_factory()` (unique by default) or unique
  slugs per test.
- **Leaking context between tests.** If you call `TenantContext.set()` directly,
  you must `clear()` it. Prefer `set_tenant` or `TenantTestMixin`, which always
  restore the previous context in their `finally`/`tearDown`.
- **Forgetting `db` access.** Mark pytest tests with `@pytest.mark.django_db` (or
  request the `db`/`tenant_factory` fixtures), otherwise tenant creation fails.
- **Mocking a manager method.** boundary has no `for_tenant`/`for_merchant`
  method to mock — filtering lives entirely in the default manager's
  `get_queryset`. Do not patch a manager method with a `side_effect`; assert on
  the querysets the real (auto-filtered) manager returns, or use `set_tenant` /
  `call_view` to establish context. A mock pinned to a method that does not
  exist gives a false sense of coverage and breaks silently.
- **Calling a CBV without context.** A view called via a bare `RequestFactory`
  raises `TenantNotSetError` under strict mode. Use `call_view` (above), which
  wraps the call in an active tenant context.

## Related

- [README: Quick Start](../../README.md#quick-start) for installing and
  configuring `BOUNDARY_TENANT_MODEL`.
- [README: Features](../../README.md#features) for the full settings reference,
  including `BOUNDARY_STRICT_MODE`.
- Source of truth for the helpers: `src/boundary/testing.py`. Context API:
  `src/boundary/context.py`. Manager behaviour: `src/boundary/models.py`.
