"""The consuming project's own app, in adoption tests.

BR-RLS-013 requires the adoption DDL to be applied by a migration in the
CONSUMER's app, never by the adopted app's own migrations, a post_migrate
handler, or an AppConfig.ready() hook. This app exists to be that consumer:
it ships no models, only adoption migrations, which is exactly the shape a
real consumer's adoption migration takes.

Unlike boundary_testapp, this app keeps real migration modules, because
AC-RLS-011 and AC-RLS-013 must be driven through MigrationExecutor so real
historical state is exercised rather than a FakeState standing in for it.
"""
