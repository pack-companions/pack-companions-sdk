"""Canonical hashing helper for the legacy email discovery hint.

The hashes produced here MUST be byte-identical to those produced by:
- service: app/services/identity.hash_email
- JS SDK:  @pack-companions/companions hashEmail()

An email hash is not proof of ownership and must never auto-link or auto-merge
accounts. Explicit cross-app verification is a separate service workflow.
"""

from __future__ import annotations

import hashlib


def hash_email(email: str) -> str:
    """Hash an email for the legacy ``SnapshotUser.email_hash`` hint.

    Recipe (must match service + JS SDK exactly):

        sha256(email.strip().lower()).hexdigest()

    Returns: 64-character lowercase hex digest. The digest is still personal
    data and is not an account-linking credential.
    """
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
