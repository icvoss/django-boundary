"""Boundary Django signals for observability.

These allow consuming projects to wire up metrics (StatsD, Prometheus,
OpenTelemetry) without boundary taking a dependency on any metrics library.
"""

from django.dispatch import Signal

# Fired after successful tenant resolution by middleware.
# Arguments: sender, tenant, resolver, request
tenant_resolved = Signal()

# Fired when no resolver matched and BOUNDARY_REQUIRED is True.
# Arguments: sender, request
tenant_resolution_failed = Signal()

# Fired when TenantNotSetError is about to be raised in strict mode.
# Arguments: sender, model, queryset
strict_mode_violation = Signal()
