"""A stand-in for a third-party package's app, for adoption tests.

Deliberately carries no boundary import of any kind: no mixin, no manager,
no tenant field, and no knowledge that boundary exists. That passivity is
the whole point of app adoption (BR-RLS-010), and a boundary import here
would make every adoption test prove something weaker than the contract.
"""
