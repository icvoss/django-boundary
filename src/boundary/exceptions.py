"""Boundary exception hierarchy.

All exceptions inherit from BoundaryError so consuming code can catch
the entire family with a single except clause.
"""


class BoundaryError(Exception):
    """Base exception for all boundary errors."""


class TenantNotSetError(BoundaryError):
    """No tenant is active in context and STRICT_MODE is True."""


class TenantResolutionError(BoundaryError):
    """A resolver raised an unexpected exception during resolution."""


class TenantInactiveError(BoundaryError):
    """The resolved tenant has is_active=False."""


class TenantNotFoundError(BoundaryError):
    """A Celery task header references a tenant UUID that no longer exists."""


class RegionNotConfiguredError(BoundaryError):
    """The active tenant's region is not present in BOUNDARY_REGIONS."""


class AdminBypassNotActiveError(BoundaryError):
    """admin_bypass() set the admin flag but a read-back found it not active.

    Raised instead of silently proceeding (issue #37). The transaction-local
    form of set_config() only has scope for the lifetime of the surrounding
    transaction; with no active transaction and BOUNDARY_WRAP_ATOMIC=False,
    the setting vanishes before the read-back statement runs, so a caller
    that assumed the bypass was active would silently keep operating under
    ordinary tenant isolation instead. That failure is not self-announcing:
    a maintenance script would see filtered or zero rows and either produce
    wrong output or raise TenantNotSetError somewhere downstream, with no
    indication the cause was a bypass that never took effect. Verifying the
    postcondition and raising here surfaces the misconfiguration at the
    point of entry instead.
    """


class RLSNotEnforcedError(BoundaryError):
    """assert_rls_enforced() found the connecting role or a tenant table is
    not actually enforcing Row Level Security (issue #55).

    Raised rather than returning a bool so a consumer who calls the
    assertion directly (outside the pytest fixture wrapper) cannot
    accidentally ignore a falsy return value. The message names which of
    the two conditions failed and the value observed, so the run stops
    loudly instead of the suite proceeding to pass every isolation test
    vacuously against a bypassing connection or an unprotected table
    (the same trap boundary.W003 and boundary.E006 diagnose at
    ``manage.py check`` time, but that check never runs under a plain
    ``pytest`` invocation).
    """


class AdoptionRefusedError(BoundaryError):
    """AdoptTenantApp refused to adopt a table, naming the table and the reason.

    A single exception class covers every refusal the operation makes
    (BR-RLS-012, BR-RLS-013, BR-RLS-014, BR-RLS-016), because each one has
    the same consequence for the caller: the migration stops, and nothing
    the operation had already altered in that call survives, since the
    operation runs inside the migration's own transaction and PostgreSQL's
    DDL is transactional.

    Refusing rather than skipping is deliberate. A silently skipped unique
    constraint stays globally unique, which is exactly the cross-tenant
    collapse adoption exists to prevent, and a silently skipped populated
    table would be stamped by whichever tenant happened to be current at
    migration time, which during `migrate` is normally no tenant at all.
    The message always names the table and states why, so the consumer can
    decide between excluding that model, splitting the operation, or
    supplying a backfill tenant.
    """


class RLSOperationRefusedError(BoundaryError):
    """An RLS migration operation refused to run, naming itself and the reason.

    Raised by :class:`~boundary.migrations_ops.EnableRLS`,
    :class:`~boundary.migrations_ops.CreateTenantPolicy` and
    :class:`~boundary.migrations_ops.DropTenantPolicy` when the database
    router admitted an alias whose backend is not PostgreSQL (BR-RLS-021
    gate 2).

    A sibling of :class:`AdoptionRefusedError` in behaviour, deliberately not
    a reuse of it. The two operations fail for the same underlying reason and
    on the same terms, but a consumer reading a traceback from ``EnableRLS``
    must not be told about app adoption, which is a different operation they
    may not have written into any migration at all. The message always names
    the operation that raised it, the alias, and the vendor observed, so the
    refusal identifies its own source without the consumer reading the
    package to work out which operation adoption refers to.

    Refusing rather than proceeding is the whole point: on SQLite or MySQL the
    generated ``ALTER TABLE ... ENABLE ROW LEVEL SECURITY`` and
    ``CREATE POLICY`` statements are a syntax error, so the migration fails
    either way and only the message differs. The backend's own parser error
    names a fragment of generated SQL and leaves the consumer to infer that
    the RLS layer is PostgreSQL-only; this names it.

    Not raised for a router denial. An alias a consumer's router keeps off
    the operation's graph is a legitimate per-alias skip and returns silently
    (BR-RLS-021 gate 1), because that router is the documented remedy this
    exception's own message points at and so cannot itself raise.
    """
