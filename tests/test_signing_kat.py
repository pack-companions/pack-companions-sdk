"""Known Answer Tests for HMAC request signing.

Locks the SDK's signing implementation to the same wire protocol as the
Pack Companions service and JS SDK. If any vector fails, the SDK has
drifted from the canonical wire protocol and would produce signatures
the service rejects.

The fixture is mirrored from pack-companions/tests/fixtures/signing_kat.json.
When the service repo updates the fixture, copy it here AND ensure this
test still passes — that's the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pack_companions._signing import _sign

FIXTURE = Path(__file__).parent / "fixtures" / "signing_kat.json"


def _load_fixture() -> dict:
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


_DATA = _load_fixture()


@pytest.mark.parametrize("vector", _DATA["vectors"], ids=lambda v: v["name"])
def test_signing_kat(vector: dict) -> None:
    """Each vector must produce its expected signature byte-for-byte."""
    body_bytes = vector["body"].encode("utf-8")
    actual = _sign(
        secret=_DATA["secret"],
        timestamp=vector["timestamp"],
        method=vector["method"],
        path=vector["path"],
        body=body_bytes,
    )
    assert actual == vector["expected"], (
        f"KAT mismatch for vector {vector['name']!r}:\n"
        f"  expected: {vector['expected']}\n"
        f"  actual:   {actual}\n"
        f"  Wire protocol drifted. Coordinate update across all three clients "
        f"(service auth.py, this SDK _signing.py, JS SDK signing.ts)."
    )
