"""Models a third-party package might ship, covering every unique form.

No boundary import, no tenant field, no mixin: adoption works on a package
that knows nothing about boundary, and these models are the fixture that
proves it (BR-RLS-010).
"""

from django.db import models


class Tag(models.Model):
    """The far side of Widget's ManyToManyField.

    Its only job is to make Django auto-create a through table, so the
    adopted set can be proved to include auto-created models (BR-RLS-010,
    AC-RLS-012).
    """

    name = models.CharField(max_length=50)

    class Meta:
        app_label = "thirdparty"

    def __str__(self):
        return self.name


class Widget(models.Model):
    """The ordinary adoption case, carrying two rewritable unique forms.

    ``code`` is a ``unique=True`` field, which Django emits as an inline
    UNIQUE that PostgreSQL names ``thirdparty_widget_code_key``;
    ``unique_together`` is a named constraint the schema editor hashes.
    The two together are what prove the rewrite introspects the catalogue
    rather than constructing the names it expects (BR-RLS-012).
    """

    name = models.CharField(max_length=50)
    code = models.CharField(max_length=50, unique=True)
    owner = models.CharField(max_length=50)
    label = models.CharField(max_length=50)
    tags = models.ManyToManyField(Tag, related_name="widgets", blank=True)

    class Meta:
        app_label = "thirdparty"
        unique_together = [("owner", "label")]

    def __str__(self):
        return self.code


class Gadget(models.Model):
    """A model an upstream package upgrade adds after the first adoption.

    Used by AC-RLS-008's second half: a second AdoptTenantApp migration
    skips the already-adopted Widget unchanged and adopts only this one
    (BR-RLS-013's per-table idempotency).
    """

    name = models.CharField(max_length=50)

    class Meta:
        app_label = "thirdparty"

    def __str__(self):
        return self.name


class Seat(models.Model):
    """A model whose unique constraint a ForeignKey targets.

    ``SeatBooking.seat_code`` is a ``ForeignKey(to_field="code")``, which
    makes PostgreSQL attach an FK to this table's unique constraint on
    ``code``. Dropping that constraint would invalidate the referencing
    one and the composite replacement cannot satisfy it, so adoption must
    refuse rather than skip (BR-RLS-012, AC-RLS-010).
    """

    code = models.CharField(max_length=50, unique=True)

    class Meta:
        app_label = "thirdparty"

    def __str__(self):
        return self.code


class SeatBooking(models.Model):
    """The referencing side that makes Seat's unique constraint FK-targeted."""

    seat_code = models.ForeignKey(
        Seat,
        to_field="code",
        on_delete=models.CASCADE,
        related_name="bookings",
    )

    class Meta:
        app_label = "thirdparty"

    def __str__(self):
        return f"{self.seat_code_id}"


class Coupon(models.Model):
    """A model whose unique constraint carries a condition.

    A ``UniqueConstraint`` with a ``condition`` becomes a PARTIAL unique
    index rather than a constraint, whose predicate boundary cannot safely
    re-express against the added column, so adoption refuses (BR-RLS-012,
    AC-RLS-010).
    """

    code = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)

    class Meta:
        app_label = "thirdparty"
        constraints = [
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(is_active=True),
                name="thirdparty_coupon_active_code_uniq",
            )
        ]

    def __str__(self):
        return self.code
