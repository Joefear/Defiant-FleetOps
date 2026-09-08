"""Explicit real-credential setup for authenticated runtime database proofs."""

from fleetops.auth import token_digest
from fleetops.db.tenancy import set_credential_context, set_organization


def set_authenticated(connection, tenant):
    """Supply a fixture's real credential alongside scope; no database privilege is added.

    Invalid-credential proofs can still populate this context: the production durable
    boundary, not this helper, must decide whether the credential is currently valid.
    """
    set_organization(connection, tenant.org_id)
    set_credential_context(connection, token_digest(tenant.raw_token))
