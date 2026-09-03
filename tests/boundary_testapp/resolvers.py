"""Test-only resolver subclasses.

CustomHeaderResolver exists solely to prove boundary.W006 matches by
issubclass rather than by dotted-path suffix (issue #38): a consumer
subclass of a client-controlled resolver inherits the same trust boundary
and must still trigger the check even though its import path is not
boundary.resolvers.HeaderResolver.

CustomSubdomainResolver exists solely to prove boundary.W008 matches by
issubclass rather than by dotted-path suffix (issue #22): a consumer
subclass of SubdomainResolver inherits the same unconstrained-host
behaviour and must still trigger the check even though its import path is
not boundary.resolvers.SubdomainResolver.
"""

from boundary.resolvers import HeaderResolver, SubdomainResolver


class CustomHeaderResolver(HeaderResolver):
    """A no-op subclass; behaviour is irrelevant, only the base class is."""


class CustomSubdomainResolver(SubdomainResolver):
    """A no-op subclass; behaviour is irrelevant, only the base class is."""
