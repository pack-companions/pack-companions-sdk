"""Canonical hashing helpers for cross-app identity (Phase H).

The hashes produced here MUST be byte-identical to those produced by:
- service: app/services/identity.hash_email
- JS SDK:  @pack-companions/companions hashEmail()

If any of the three diverges, cross-app identity linking silently
breaks. Parity is locked by tests/test_identity_parity.py + the JS
SDK's hashEmail.test.ts using a shared fixture.
"""

from __future__ import annotations

import hashlib


def hash_email(email: str) -> str:
    """Hash an email for use as `SnapshotUser.email_hash`.

    Recipe (must match service + JS SDK exactly):

        sha256(email.strip().lower()).hexdigest()

    Returns: 64-character lowercase hex digest.
    """
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()
