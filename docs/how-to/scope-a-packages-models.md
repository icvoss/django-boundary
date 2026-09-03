# Scope a package's models into your tenancy

## Goal

Tenant-scope a model that a third-party ICV package ships, rather than one you
wrote yourself. By the end, the package's concrete model is filtered by your
active tenant like any other boundary model, and the package's own manager
contract (its methods, its exceptions, its fail-closed behaviour) is still in
force.

This is composition at the consumer, not a package-side mechanism. A domain
package does not ship a tenancy system, does not resolve a tenancy mixin from
settings, and does not read `BOUNDARY_TENANT_MODEL` in order to build one.
Where a package's models are a genuine extension point, it ships them as an
abstract base plus a concrete default behind a swappable seam; tenancy is
something you add when you swap in your own concrete model.

## Prerequisites

- django-boundary installed and configured with `BOUNDARY_TENANT_MODEL`. See
  [Set up a tenant model](set-up-a-tenant-model.md) if you have not done this.
- The package you are adopting ships an abstract base model behind a
  `Meta.swappable` seam (for example `AbstractArticle` plus a settings key
  such as `ICV_ARTICLES_ARTICLE_MODEL`). Check the package's own docs for its
  swap settings; this page only covers the boundary side of the composition.
- Know whether the package ships its own manager on the abstract base. If it
  does, you need its class (for example `ArticleManager`) importable from the
  package.

## Steps

### 1. Compose `TenantMixin` onto the package's abstract base

```python
# myapp/models.py
from django.db import models
from boundary.models import TenantMixin
from thirdparty_pkg.models import AbstractArticle, ArticleManager


class Article(TenantMixin, AbstractArticle):
    # Declare the manager explicitly. Without this line the package's own
    # manager is silently replaced by boundary's TenantManager, and the
    # package's fail-closed contract disappears with a green test suite.
    objects = ArticleManager()
```

**Read that warning again before you copy the snippet.** `TenantMixin` sits
earlier in the MRO than the package's abstract base, so a subclass that
declares no `objects` of its own does not inherit the package's manager: it
gets `TenantMixin`'s `TenantManager` instead. Nothing about this fails loudly.
Your test suite passes, `Article.objects.all()` returns rows, and the
package's manager, its custom queryset methods, its own exception class,
whatever contract it carried, is gone. The only way to see it is to check
what `Article.objects` actually is (see Verify, below).

**When you do not need the `objects` line.** If the package's abstract base
declares no manager of its own (plain `models.Model` behaviour, no custom
`objects`), the plain two-base form is correct and there is nothing to
redeclare:

```python
class Article(TenantMixin, AbstractArticle):
    pass
```

Check the package's abstract base for an `objects = SomeManager()` class
attribute before deciding which form applies. Do not add an `objects` line
for a manager class that does not exist.

### 2. Keep the package's manager contract with `TenantManager` subclassing

`TenantManager` is designed to be subclassed. A package that owns a manager
carrying its own contract, custom queryset methods, a distinct exception it
raises when no tenant is set, whatever it uses to make it feel like the rest
of the package, keeps that contract by subclassing boundary's `TenantManager`
rather than being replaced by it:

```python
# thirdparty_pkg/managers.py (package code, shown for context)
from boundary.models import TenantManager


class ArticleManager(TenantManager):
    def published(self):
        return self.filter(status="published")
```

Composed as in step 1, `Article.objects` is now `ArticleManager`: boundary's
tenant filtering runs underneath, and `Article.objects.published()` is still
there.

If the package's manager also needs a custom queryset, for example to expose
`published()` as a chainable method rather than only a manager method,
`TenantManager.from_queryset()` is the supported way to combine the two. See
[Add boundary to an existing app](add-boundary-to-an-existing-app.md) for the
worked `from_queryset()` example; this page does not repeat it. Do not
hand-override `get_queryset()` to bolt a custom queryset onto tenant
filtering: that duplicates boundary's tenant filter, its strict-mode branch,
its signal send and its exception message, and `from_queryset()` is the
route that avoids all of that duplication.

## Verify it worked

Check the MRO puts boundary's mixin ahead of the package's base, and that the
resulting manager is the one you intended:

```python
>>> from myapp.models import Article
>>> [c.__name__ for c in Article.__mro__[:4]]
['Article', 'TenantMixin', 'AbstractArticle', 'Model']
>>> type(Article.objects)
<class 'thirdparty_pkg.managers.ArticleManager'>
```

If `type(Article.objects)` prints `TenantManager` instead of the package's
own manager class, the `objects` line from step 1 is missing or was not
picked up: add it and re-check before doing anything else.

Confirm boundary recognises the model as tenant-scoped:

```python
>>> from boundary.models import is_tenant_model, get_tenant_fk_field
>>> is_tenant_model(Article)
True
>>> get_tenant_fk_field(Article)
'tenant'
```

Confirm scoped reads only see one tenant's rows:

```python
from boundary.context import TenantContext

with TenantContext.using(tenant_a):
    Article.objects.create(title="A1")
with TenantContext.using(tenant_b):
    Article.objects.create(title="B1")

with TenantContext.using(tenant_a):
    assert Article.objects.count() == 1

assert Article.unscoped.count() == 2   # bypass still sees all
```

Confirm the fail-closed path still raises outside a tenant context, with
`BOUNDARY_STRICT_MODE` at its default of `True`:

```python
from boundary.exceptions import TenantNotSetError
import pytest

with pytest.raises(TenantNotSetError):
    Article.objects.count()
```

If the package's manager re-raises its own exception (see step 2), confirm
that exception is what surfaces instead, not boundary's:

```python
from thirdparty_pkg.exceptions import ArticleContextError

with pytest.raises(ArticleContextError):
    Article.objects.count()
```

## Related

- [Add boundary to an existing app](add-boundary-to-an-existing-app.md): the
  full retrofit sequence, including the `from_queryset()` example referenced
  above.
- [Scope a model through a relation](scope-models-through-a-relation.md): the
  mixin to use when the model reaches the tenant through a relation instead
  of owning its own FK.
- [Isolation layers and the threat model](../explanation/isolation-layers.md):
  why the ORM layer and RLS are both needed, and what each does and does not
  catch.
- [Set up a tenant model](set-up-a-tenant-model.md): define the tenant model
  this composition points at.
