"""Known Answer Tests for the legacy email-hash discovery recipe.

Locks the SDK's `hash_email` implementation to the same canonical
recipe as the service and JS SDK. The hash is not account-ownership proof and
does not authorize cross-app linking.

The fixture is mirrored from
pack-companions/tests/fixtures/hash_email_kat.json.
When the service repo updates the fixture, copy it here AND ensure
this test still passes — that's the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pack_companions import hash_email

FIXTURE = Path(__file__).parent / "fixtures" / "hash_email_kat.json"


def _load_vectors() -> list[dict]:
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["vectors"]


@pytest.mark.parametrize("vector", _load_vectors(), ids=lambda v: v["name"])
def test_hash_email_kat(vector: dict) -> None:
    got = hash_email(vector["input"])
    assert got == vector["expected"], (
        f"recipe drift on vector {vector['name']!r}: "
        f"input={vector['input']!r} expected={vector['expected']} got={got}"
    )
