"""Pluggable tenant resolution from incoming requests.

Each resolver implements resolve(request) -> tenant | None.
The middleware walks the resolver chain; first non-None result wins.
"""

import base64
import json
import logging
import threading
import time
import uuid

from boundary.conf import boundary_settings, get_tenant_model

logger = logging.getLogger("boundary.resolvers")

# ── Resolver Cache ────────────────────────────────────────────

_cache: dict[str, tuple[object, float]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str) -> object | None:
    """Return cached tenant or None if missing/expired."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        tenant, ts = entry
        if time.monotonic() - ts > boundary_settings.RESOLVER_CACHE_TTL:
            del _cache[key]
            return None
        return tenant


def _cache_set(key: str, tenant: object) -> None:
    """Cache a resolved tenant, evicting oldest if at capacity."""
    with _cache_lock:
        if len(_cache) >= boundary_settings.RESOLVER_CACHE_SIZE and key not in _cache:
            oldest_key = min(_cache, key=lambda k: _cache[k][1])
            del _cache[oldest_key]
        _cache[key] = (tenant, time.monotonic())


def _cache_invalidate(tenant) -> None:
    """Remove all cache entries for a given tenant."""
    tenant_pk = tenant.pk if hasattr(tenant, "pk") else tenant
    with _cache_lock:
        keys_to_remove = [k for k, (t, _) in _cache.items() if hasattr(t, "pk") and t.pk == tenant_pk]
        for k in keys_to_remove:
            del _cache[k]


def _cache_clear() -> None:
    """Clear the entire resolver cache."""
    with _cache_lock:
        _cache.clear()


# ── Base Resolver ─────────────────────────────────────────────


class BaseResolver:
    """Interface for tenant resolvers."""

    def resolve(self, request) -> object | None:
        """Return the tenant for this request, or None to pass to next resolver.

        Must not raise — log and return None on error.
        """
        raise NotImplementedError

    def get_tenant_model(self):
        return get_tenant_model()


# ── Built-in Resolvers ────────────────────────────────────────


class SubdomainResolver(BaseResolver):
    """Resolve tenant from the first subdomain of the request host."""

    def resolve(self, request):
        host = request.get_host().split(":")[0]
        parts = host.split(".")
        if len(parts) < 3:
            return None

        slug = parts[0]
        logger.debug(
            "SubdomainResolver attempt",
            extra={"resolver_name": "SubdomainResolver", "lookup_value": slug},
        )

        cached = _cache_get(f"subdomain:{slug}")
        if cached is not None:
            return cached

        TenantModel = self.get_tenant_model()
        field = boundary_settings.SUBDOMAIN_FIELD
        try:
            tenant = TenantModel.objects.get(**{field: slug})
        except TenantModel.DoesNotExist:
            return None

        _cache_set(f"subdomain:{slug}", tenant)
        return tenant


class HeaderResolver(BaseResolver):
    """Resolve tenant from an HTTP header (UUID first, slug fallback)."""

    def resolve(self, request):
        header_name = boundary_settings.HEADER_NAME
        meta_key = f"HTTP_{header_name.upper().replace('-', '_')}"
        value = request.META.get(meta_key)
        if not value:
            return None

        logger.debug(
            "HeaderResolver attempt",
            extra={"resolver_name": "HeaderResolver", "lookup_value": value},
        )

        cached = _cache_get(f"header:{value}")
        if cached is not None:
            return cached

        TenantModel = self.get_tenant_model()
        tenant = None

        # UUID first, then PK, then slug fallback (BR-RES-007 / AC-RES-014)
        try:
            tenant_uuid = uuid.UUID(value)
            tenant = TenantModel.objects.get(pk=tenant_uuid)
        except (ValueError, TenantModel.DoesNotExist):
            # Not a valid UUID or no match — try direct PK lookup
            try:
                tenant = TenantModel.objects.get(pk=value)
            except (TenantModel.DoesNotExist, ValueError, TypeError):
                # Fall back to slug lookup
                try:
                    tenant = TenantModel.objects.get(**{boundary_settings.SUBDOMAIN_FIELD: value})
                except TenantModel.DoesNotExist:
                    return None

        _cache_set(f"header:{value}", tenant)
        return tenant


class JWTClaimResolver(BaseResolver):
    """Resolve tenant from a JWT claim (does NOT validate signature)."""

    def resolve(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header[7:]
        parts = token.split(".")
        if len(parts) != 3:
            return None

        try:
            # Decode payload (second segment) without signature verification
            padding = 4 - len(parts[1]) % 4
            payload_bytes = base64.urlsafe_b64decode(parts[1] + "=" * padding)
            payload = json.loads(payload_bytes)
        except Exception:
            return None

        claim = boundary_settings.JWT_CLAIM
        tenant_id = payload.get(claim)
        if not tenant_id:
            return None

        logger.debug(
            "JWTClaimResolver attempt",
            extra={"resolver_name": "JWTClaimResolver", "lookup_value": tenant_id},
        )

        TenantModel = self.get_tenant_model()
        try:
            return TenantModel.objects.get(pk=tenant_id)
        except (TenantModel.DoesNotExist, ValueError):
            return None


class SessionResolver(BaseResolver):
    """Resolve tenant from Django session."""

    def resolve(self, request):
        session = getattr(request, "session", None)
        if session is None:
            return None

        key = boundary_settings.SESSION_KEY
        tenant_id = session.get(key)
        if not tenant_id:
            return None

        logger.debug(
            "SessionResolver attempt",
            extra={"resolver_name": "SessionResolver", "lookup_value": tenant_id},
        )

        cached = _cache_get(f"session:{tenant_id}")
        if cached is not None:
            return cached

        TenantModel = self.get_tenant_model()
        try:
            tenant = TenantModel.objects.get(pk=tenant_id)
        except (TenantModel.DoesNotExist, ValueError):
            return None

        _cache_set(f"session:{tenant_id}", tenant)
        return tenant


class ExplicitResolver(BaseResolver):
    """Resolve tenant from request.boundary_tenant (set by upstream code)."""

    def resolve(self, request):
        return getattr(request, "boundary_tenant", None)
