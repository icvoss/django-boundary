# Choose and order resolvers

## Goal

Pick the right built-in resolver for how your clients identify a tenant, order them correctly with `BOUNDARY_RESOLVERS`, and add a custom resolver when none of the built-ins fit.

A resolver answers one question per request: which tenant does this request belong to? The middleware walks `BOUNDARY_RESOLVERS` in order, calls `resolve(request)` on each, and stops at the first resolver that returns a tenant. First match wins.

**A resolver answers *which* tenant, never *whether the caller may access it*.** That second question, whether the authenticated principal is actually a member of the resolved tenant, is entirely the consumer's responsibility. See [Enforce membership after resolution](#enforce-membership-after-resolution) below before choosing `HeaderResolver` or `JWTClaimResolver` for anything with authenticated users.

## Prerequisites

- `boundary.middleware.TenantMiddleware` installed in `MIDDLEWARE`.
- `BOUNDARY_TENANT_MODEL` set to your tenant model (for example `"clubs.Club"`).
- The middleware setup is covered in the [README quick start](../../README.md#quick-start).

## Steps

### 1. Understand each built-in resolver

All built-ins live in `boundary.resolvers` and subclass `BaseResolver`. Each `resolve(request)` returns a tenant instance or `None` to pass control to the next resolver. None of them raise on a miss.

| Resolver | Reads from | Lookup | Setting |
|----------|-----------|--------|---------|
| `SubdomainResolver` | First label of the host (`club-a.example.com`) | `SUBDOMAIN_FIELD` on the tenant model | `BOUNDARY_SUBDOMAIN_FIELD` (default `"slug"`), `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` (default `None`) |
| `HeaderResolver` | An HTTP header | UUID pk, then raw pk, then slug | `BOUNDARY_HEADER_NAME` (default `"X-Tenant-ID"`) |
| `JWTClaimResolver` | The `Authorization: Bearer` token payload | tenant pk from a claim, signature NOT validated | `BOUNDARY_JWT_CLAIM` (default `"tenant_id"`) |
| `SessionResolver` | The Django session | tenant pk from a session key | `BOUNDARY_SESSION_KEY` (default `"boundary_tenant_id"`) |
| `ExplicitResolver` | `request.boundary_tenant` set by upstream code | direct attribute read, no DB query | none |

When to use each:

- **SubdomainResolver**: public-facing multi-tenant apps where each tenant has its own subdomain. Requires a host with at least three labels (`slug.domain.tld`); a bare `example.com` returns `None`. This is the default when `BOUNDARY_RESOLVERS` is unset. If the deployment serves any host it does not fully control (a customer-owned custom domain, for example), set `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN`; see [Constrain SubdomainResolver to your own domain](#constrain-subdomainresolver-to-your-own-domain) below.
- **HeaderResolver**: internal or service-to-service APIs where a trusted client names the tenant. It tries the header value as a UUID pk, then as a raw pk, then as a slug (`SUBDOMAIN_FIELD`), so both `X-Tenant-ID: <uuid>` and `X-Tenant-ID: club-a` work. **Client-controlled**: any caller can set the header to any value, so the resolved tenant is whatever the caller asked for, not necessarily one they belong to. If the application has authenticated users, pair this with the membership check in [Enforce membership after resolution](#enforce-membership-after-resolution); `boundary.W006` warns when it is missing.
- **JWTClaimResolver**: APIs that already authenticate with a bearer token carrying the tenant in a claim. It base64-decodes the JWT payload and reads the claim, but does NOT verify the signature, so put real token verification (for example DRF auth or an upstream gateway) in front of it. The claim value must be the tenant pk. **Client-controlled**: boundary trusts whatever the claim says once the token's signature has been verified elsewhere; a valid token proves who the caller is, not which tenants they may act as. The same membership check applies as for `HeaderResolver`.
- **SessionResolver**: server-rendered apps where the user selects or is assigned a tenant and you store its pk in `request.session[BOUNDARY_SESSION_KEY]`. Not client-controlled in the header/JWT sense (the client cannot set an arbitrary session value by tampering with a cookie), but the trust boundary shifts to whatever server-side code writes that session key: if a "switch tenant" view sets it from a client-supplied id without checking membership first, the same gap reappears one layer up.
- **ExplicitResolver**: when other code (a different middleware, a test, a management command path) has already set `request.boundary_tenant`. It is a pure attribute read with no DB lookup, useful as a first-priority override or in test setups. Not client-controlled: it never reads the request at all, so whatever set the attribute already made (or deferred) the access decision.

### 2. Configure the order

`BOUNDARY_RESOLVERS` is a list of dotted class paths. Order is precedence: the middleware returns the first non-`None` result.

```python
# settings.py
BOUNDARY_RESOLVERS = [
    "boundary.resolvers.ExplicitResolver",   # honour explicit overrides first
    "boundary.resolvers.SubdomainResolver",  # then public subdomain
    "boundary.resolvers.SessionResolver",    # then logged-in session choice
]
```

Order by trust and specificity. Put the most authoritative source first and the broadest fallback last.

### 3. Set per-resolver options

Each resolver reads its own `BOUNDARY_` setting. Override only the ones whose resolver you use.

```python
# settings.py
BOUNDARY_SUBDOMAIN_FIELD = "slug"          # tenant field for subdomain and header-slug lookups
BOUNDARY_HEADER_NAME = "X-Tenant-ID"       # header HeaderResolver reads
BOUNDARY_JWT_CLAIM = "org_id"              # claim JWTClaimResolver reads
BOUNDARY_SESSION_KEY = "boundary_tenant_id"  # session key SessionResolver reads
```

Note that `HeaderResolver`'s slug fallback uses `BOUNDARY_SUBDOMAIN_FIELD`, not a separate setting.

The full settings table is in the [settings reference](../reference/settings.md).

### Constrain SubdomainResolver to your own domain

`SubdomainResolver.resolve()` takes the first label of any host with three or more labels and looks it up as a tenant slug. It does not check that the host is one your deployment actually intends to serve; it only checks that the *shape* looks like `slug.domain.tld`. `ALLOWED_HOSTS` constrains which hosts Django will answer at all, but a deployment that serves several domains behind one `ALLOWED_HOSTS` entry (a wildcard, or a list that includes customer-owned custom domains alongside your own platform subdomains) still lets every one of those hosts through to `SubdomainResolver`.

This matters because a foreign host can collide with a tenant slug by accident. If your platform has a tenant slugged `shop`, and a customer separately points their own domain `shop.example.co.uk` (a domain you never intended to serve tenant-by-subdomain traffic on, but that still passes `ALLOWED_HOSTS` because it is on the customer's custom-domain allowlist for a different feature) at your application, `SubdomainResolver` resolves the `shop` tenant for it, because the first label matches, with no check that `example.co.uk` is a domain you control. That is cross-tenant serving: a request meant for one customer's custom domain gets served in the security context of an unrelated tenant.

`BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` closes this by constraining `SubdomainResolver` to hosts that are exactly one label above a domain (or one of several domains) you name explicitly:

```python
# settings.py
BOUNDARY_SUBDOMAIN_PARENT_DOMAIN = "example.com"
# or, for a deployment serving several parent domains:
BOUNDARY_SUBDOMAIN_PARENT_DOMAIN = ["example.com", "example.co.uk"]
```

With this set, `club-a.example.com` still resolves the `club-a` tenant, but `club-a.evil.org` returns `None` even though it has three labels and would otherwise have matched. Matching is case-insensitive, by label boundary rather than substring (`evilexample.com` and `evil-example.com` do not match a parent of `example.com`), and exactly one level deep (`club-a.staging.example.com` does not match a parent of `example.com`; only `club-a.example.com` does).

**This setting is opt-in and unset by default**, which preserves the exact pre-existing behaviour for backwards compatibility. `boundary.W008` warns at startup when `SubdomainResolver` is configured without it, as a prompt to make a deliberate choice rather than an error, because a deployment whose `ALLOWED_HOSTS` is already a small, fully-trusted, closed list has nothing to gain from it. If that describes your deployment, silence `boundary.W008` in `SILENCED_SYSTEM_CHECKS` rather than leaving the warning unaddressed.

### 4. Write a custom resolver

Subclass `BaseResolver` and implement `resolve(self, request)`. Return a tenant or `None`. Call `self.get_tenant_model()` to get the configured tenant model rather than importing it directly, and never let `resolve` raise: log and return `None` on error.

```python
# myapp/resolvers.py
import logging

from boundary.resolvers import BaseResolver

logger = logging.getLogger(__name__)


class PathPrefixResolver(BaseResolver):
    """Resolve tenant from a /t/<slug>/ URL prefix."""

    def resolve(self, request):
        parts = request.path.split("/")
        if len(parts) < 3 or parts[1] != "t":
            return None

        slug = parts[2]
        TenantModel = self.get_tenant_model()
        try:
            return TenantModel.objects.get(slug=slug, is_active=True)
        except TenantModel.DoesNotExist:
            return None
        except Exception:
            logger.exception("PathPrefixResolver failed for slug=%s", slug)
            return None
```

Register it by dotted path:

```python
# settings.py
BOUNDARY_RESOLVERS = [
    "myapp.resolvers.PathPrefixResolver",
    "boundary.resolvers.SubdomainResolver",
]
```

## Verify it worked

Call the resolver directly with a Django `RequestFactory`, the same way the test suite does.

```python
import pytest
from django.test import RequestFactory

from boundary.resolvers import SubdomainResolver


@pytest.mark.django_db
def test_subdomain_resolution(tenant_a):
    request = RequestFactory().get("/", HTTP_HOST="club-a.example.com")
    assert SubdomainResolver().resolve(request) == tenant_a
```

To confirm ordering end to end, send a real request through the middleware and read the resolved tenant off the request. After successful resolution the middleware sends the `tenant_resolved` signal with `tenant`, `resolver`, and `request`; if nothing matches and `BOUNDARY_REQUIRED` is `True` it sends `tenant_resolution_failed` and returns a 404. The resolved tenant is available as `request.tenant` (and as `request.<BOUNDARY_REQUEST_ATTR>` when you have customised that).

## Enforce membership after resolution

Resolution only answers *which* tenant a request names. It never checks *whether the authenticated caller belongs to that tenant*. For `HeaderResolver` and `JWTClaimResolver` the tenant comes straight from client-supplied input (a header value, an unverified JWT claim), so if your application has authenticated users, add an explicit membership check. `boundary.W006` warns at startup when a client-controlled resolver is configured alongside `django.contrib.auth` and nothing has silenced the check, which is the configuration this section addresses.

### Where the check sits

The membership check runs as its own middleware, positioned **after** `TenantMiddleware` (so `request.tenant` is set) and **after** your authentication middleware (so `request.user` is set and authenticated):

```python
# settings.py
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",  # sets request.user
    "boundary.middleware.TenantMiddleware",                     # sets request.tenant
    "myapp.middleware.TenantMembershipMiddleware",               # checks request.user is a member of request.tenant
    # ...
]
```

If `TenantMiddleware` runs first, an unauthenticated request that fails resolution already gets `boundary`'s own 404 (`BOUNDARY_REQUIRED = True`, the default) before your membership check ever runs, so the check does not need to defend against a missing `request.tenant`.

### A worked example

The membership model itself belongs to your application; boundary has no opinion on it and ships no such model. The shape below is illustrative, not a contract boundary ships:

```python
# myapp/middleware.py
from django.http import HttpResponseForbidden


class TenantMembershipMiddleware:
    """Enforce that request.user belongs to request.tenant.

    Runs after boundary.middleware.TenantMiddleware and after
    AuthenticationMiddleware. boundary resolves tenancy; it does not
    authorise access, so this check is the application's responsibility
    (issue #38 / BR-RES-009).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(request, "tenant", None)
        user = getattr(request, "user", None)

        if tenant is not None and user is not None and user.is_authenticated:
            # Replace with your own membership model / lookup. A common
            # shape is a through-table (Membership) or an M2M on the user
            # or tenant model; the details are yours to design.
            if not user.memberships.filter(tenant=tenant).exists():
                return HttpResponseForbidden(f"Not a member of {tenant}.")

        return self.get_response(request)
```

A request with no resolved tenant (`BOUNDARY_REQUIRED = False`, public routes) or no authenticated user passes through unchanged; there is nothing to check membership against yet, and those cases are the caller's own concern further down the stack.

### Interaction with `BOUNDARY_REQUIRED` and the inactive-tenant 403

`TenantMiddleware` already returns two responses of its own before your membership check ever runs: a 404 when `BOUNDARY_REQUIRED = True` and no resolver matched, and a 403 when the resolved tenant has `is_active = False`. Your membership check is a third, independent gate, checking a different fact (is *this user* allowed in *this* tenant, as opposed to does the tenant exist and is it active) and should return its own 403 rather than trying to reuse boundary's.

### If you use icv-identity

If `icv-identity` is installed, it owns the tenant domain model and provides its own middleware for this per ADR-025 T1; you should not hand-roll `TenantMembershipMiddleware` on top of it. See icv-identity's own documentation for the specifics: boundary does not import `icv_identity` and this guide does not describe its internals.

## Common pitfalls

- **Trusting `HeaderResolver` on public endpoints.** Any client can set the header, so any client can choose the tenant. Use it only behind a trusted boundary, and do not place it ahead of `SubdomainResolver` on public-facing apps.
- **Assuming `JWTClaimResolver` validates the token.** It decodes the payload without verifying the signature. Authenticate the token separately before relying on the resolved tenant.
- **Assuming resolution is authorisation.** Neither `HeaderResolver` nor `JWTClaimResolver` checks that the caller belongs to the tenant they named; they just read the value. See [Enforce membership after resolution](#enforce-membership-after-resolution) above. `boundary.W006` exists specifically to catch this gap in the one configuration where it bites: a client-controlled resolver alongside `django.contrib.auth`.
- **Subdomain lookups on a two-label host.** `SubdomainResolver` returns `None` for `example.com` because it needs at least three labels. Local development on `localhost` will not resolve; use a `*.localhost` style host or a different resolver in dev.
- **Assuming `ALLOWED_HOSTS` is a domain boundary.** It constrains which hosts Django answers at all, not which hosts `SubdomainResolver` should resolve tenants from. A deployment serving customer-owned custom domains alongside platform subdomains needs `BOUNDARY_SUBDOMAIN_PARENT_DOMAIN` too; see [Constrain SubdomainResolver to your own domain](#constrain-subdomainresolver-to-your-own-domain).
- **Wrong lookup field.** `SubdomainResolver` and the `HeaderResolver` slug fallback both look up `BOUNDARY_SUBDOMAIN_FIELD` (default `"slug"`). If your tenant key column has another name, set it once via `BOUNDARY_SUBDOMAIN_FIELD`.
- **Claim or session value is not the pk.** `JWTClaimResolver` and `SessionResolver` look up the tenant by primary key. Store the tenant pk, not its slug, in the claim or session.
- **Letting `resolve` raise.** A raised exception is logged and skipped per resolver, but you lose control over the fallback. Catch and return `None` yourself.

## Related

- [README: Resolvers](../../README.md#resolvers): full resolver table and resolver-cache behaviour.
- [Settings reference](../reference/settings.md): all `BOUNDARY_` settings and defaults, including `boundary.W006`.
- [README: Signals](../../README.md#signals): `tenant_resolved` and `tenant_resolution_failed`.
- [README: System Checks](../../README.md#system-checks): full table of `boundary.E*` / `boundary.W*` checks.
