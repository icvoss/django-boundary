"""Test-only resolver subclasses.

CustomHeaderResolver exists solely to prove boundary.W006 matches by
issubclass rather than by dotted-path suffix (issue #38): a consumer
subclass of a client-controlled resolver inherits the same trust boundary
and must still trigger the check even though its import path is not
boundary.resolvers.HeaderResolver.
"""

from boundary.resolvers import HeaderResolver


class CustomHeaderResolver(HeaderResolver):
    """A no-op subclass; behaviour is irrelevant, only the base class is."""
