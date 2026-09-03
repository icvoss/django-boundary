"""Test-only router subclass.

CustomRegionalRouter exists solely to prove boundary.E005 matches by
issubclass rather than by dotted-path suffix (issue #36): a consumer
subclass of RegionalRouter still performs the same routing decision and
must satisfy the check even though its import path is not
boundary.routing.RegionalRouter.
"""

from boundary.routing import RegionalRouter


class CustomRegionalRouter(RegionalRouter):
    """A no-op subclass; behaviour is irrelevant, only the base class is."""
