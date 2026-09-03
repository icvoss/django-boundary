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

# Fired on entry to boundary.context.admin_bypass(), before the wrapped block
# runs. The RLS admin bypass flag is the most privileged variable boundary
# manages (issue #37): any code path that reaches it should be observable, so
# a consumer can wire up auditing or alerting on every use of the escape
# hatch. No corresponding exit signal is sent; see admin_bypass()'s docstring
# for why.
# Arguments: sender (always None), flag_var, using
admin_bypass_activated = Signal()
