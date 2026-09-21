"""The adoption migration a consumer would write (BR-RLS-013).

``exclude`` names the models whose unique forms adoption deliberately
refuses (Seat's FK-targeted constraint and Coupon's partial unique index),
so this migration exercises the ordinary path. The refusals themselves are
asserted directly against those models in tests/test_adoption.py.
"""

from django.db import migrations

from boundary.migrations_ops import AdoptTenantApp


class Migration(migrations.Migration):
    # The adopted app's tables must exist before the adoption DDL runs.
    # Django gives no implicit ordering between two unrelated apps, so the
    # dependency is declared, exactly as a real consumer would declare it.
    dependencies = [
        ("boundary_consumer", "0001_initial"),
        ("thirdparty", "0001_initial"),
    ]

    operations = [
        AdoptTenantApp(
            "thirdparty",
            exclude=(
                "thirdparty.Seat",
                "thirdparty.SeatBooking",
                "thirdparty.Coupon",
            ),
        ),
    ]
