"""Router for the SQLite leg of the CI matrix (BR-ENV-006).

The leg runs the suite with SQLite as the ``default`` alias, to prove
BR-ENV-002's quiet returns on a backend that has no Row Level Security at
all. The test project's migration graph carries RLS DDL:
``tests/boundary_consumer/migrations/0002_adopt_thirdparty.py`` applies
``AdoptTenantApp`` against ``tests/thirdparty``, and ``MIGRATION_MODULES``
deliberately keeps both apps' real migration modules so AC-RLS-011 and
AC-RLS-013 can drive them through ``MigrationExecutor``.

Under BR-RLS-013 and BR-RLS-021 those operations refuse a SQLite alias the
router admits, so the leg cannot create its test database unless something
keeps those apps off that alias. This router is that something, and it is
not scaffolding around the rule: the leg's test database existing at all is
BR-RLS-013's and BR-RLS-021's "a denied alias receives no DDL and no error"
working end to end, on a real migration graph rather than a stub router in a
single test.

Installed only on the SQLite leg, gated on ``BOUNDARY_TEST_DB=sqlite`` in
``tests/settings.py``.
"""

#: Every app whose migrations carry an RLS operation. ``thirdparty`` is the
#: adopted app and ``boundary_consumer`` owns the adoption migration, which
#: is where BR-RLS-013 requires the DDL to live.
RLS_MIGRATION_APPS = frozenset({"thirdparty", "boundary_consumer"})


class DenyRlsMigrationsRouter:
    """Deny ``allow_migrate()`` for every app whose migrations emit RLS DDL.

    Written to Django's documented
    ``allow_migrate(db, app_label, model_name=None, **hints)`` signature, so
    what it answers is what a real consumer's router answers. It returns
    ``False`` for the RLS-bearing apps and ``None`` (no opinion) for
    everything else, rather than ``True``: a router that claimed every other
    app would silence any other router a test installs alongside it.
    """

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label in RLS_MIGRATION_APPS:
            return False
        return None
