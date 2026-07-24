from __future__ import annotations

import copy
import hashlib
import hmac
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from pack_companions import (
    CompanionsClient,
    CompanionsProtocolError,
    RuntimeCatalogDiscoveryResponse,
)


def _discovery_payload() -> dict[str, object]:
    digest = "a" * 64
    return {
        "schema_version": "pack-runtime-discovery/v1",
        "catalog_schema_version": "pack-runtime-catalog/v1",
        "asset_origin": "https://companion-frames.getpacked.ai",
        "quarantined_asset_prefixes": ["byte"],
        "companions": [
            {
                "character_id": "byte",
                "species_id": "puppy",
                "availability": "available",
                "pointer": {
                    "catalog_schema_version": "pack-runtime-catalog/v1",
                    "sequence": 3,
                    "url": (
                        f"https://companion-frames.getpacked.ai/runtime/v1/catalogs/{digest}.json"
                    ),
                    "sha256": digest,
                    "byte_size": 4096,
                },
                "unavailable_reason": None,
                "fallback": "neutral_medallion",
            },
            {
                "character_id": "gizmo",
                "species_id": "lizard",
                "availability": "unavailable",
                "pointer": None,
                "unavailable_reason": "runtime_pack_not_published",
                "fallback": "neutral_medallion",
            },
        ],
    }


def _sdk_traceback_locals(exc: BaseException) -> str:
    rendered: list[str] = []
    traceback = exc.__traceback__
    while traceback is not None:
        filename = Path(traceback.tb_frame.f_code.co_filename)
        if "pack_companions" in filename.parts:
            rendered.extend(
                f"{name}={value!r}" for name, value in traceback.tb_frame.f_locals.items()
            )
        traceback = traceback.tb_next
    return "\n".join(rendered)


def test_runtime_discovery_models_are_exact_typed_and_immutable() -> None:
    discovery = RuntimeCatalogDiscoveryResponse.model_validate(_discovery_payload())

    assert discovery.schema_version == "pack-runtime-discovery/v1"
    assert discovery.catalog_schema_version == "pack-runtime-catalog/v1"
    assert discovery.asset_origin == "https://companion-frames.getpacked.ai"
    assert discovery.quarantined_asset_prefixes == ("byte",)
    assert isinstance(discovery.companions, tuple)
    assert discovery.companions[0].character_id == "byte"
    assert discovery.companions[0].species_id == "puppy"
    assert discovery.companions[0].pointer is not None
    assert discovery.companions[0].pointer.sequence == 3
    assert discovery.companions[1].availability == "unavailable"
    assert discovery.companions[1].pointer is None

    with pytest.raises(ValidationError, match="frozen"):
        discovery.companions[0].availability = "unavailable"


@pytest.mark.parametrize(
    "url",
    [
        "http://companion-frames.getpacked.ai/runtime/v1/catalogs/{digest}.json",
        "https://companion-frames.getpacked.ai:443/runtime/v1/catalogs/{digest}.json",
        "https://user@companion-frames.getpacked.ai/runtime/v1/catalogs/{digest}.json",
        "https://assets.companion-frames.getpacked.ai/runtime/v1/catalogs/{digest}.json",
        "https://companion-frames.getpacked.ai/runtime/v1/catalogs/{digest}.json?x=1",
        " https://companion-frames.getpacked.ai/runtime/v1/catalogs/{digest}.json",
    ],
)
def test_runtime_pointer_requires_exact_provider_content_address(url: str) -> None:
    payload = _discovery_payload()
    companions = payload["companions"]
    assert isinstance(companions, list)
    byte = companions[0]
    assert isinstance(byte, dict)
    pointer = byte["pointer"]
    assert isinstance(pointer, dict)
    pointer["url"] = url.format(digest="a" * 64)

    with pytest.raises(ValidationError):
        RuntimeCatalogDiscoveryResponse.model_validate(payload)


@pytest.mark.parametrize(
    "case",
    [
        "top_level_extra",
        "wrong_discovery_schema",
        "wrong_catalog_schema",
        "wrong_asset_origin",
        "missing_quarantine",
        "duplicate_quarantine",
        "entry_extra",
        "byte_wrong_species",
        "puppy_wrong_character",
        "available_without_pointer",
        "available_with_unavailable_reason",
        "unavailable_with_pointer",
        "unavailable_without_reason",
        "unknown_availability",
        "missing_explicit_pointer",
        "missing_explicit_reason",
        "wrong_fallback",
        "pointer_extra",
        "wrong_pointer_schema",
        "wrong_pointer_host",
        "wrong_pointer_digest",
        "uppercase_sha256",
        "boolean_sequence",
        "string_byte_size",
        "duplicate_character",
        "duplicate_species",
    ],
)
def test_runtime_discovery_rejects_incompatible_or_ambiguous_wire_data(
    case: str,
) -> None:
    payload = copy.deepcopy(_discovery_payload())
    companions = payload["companions"]
    assert isinstance(companions, list)
    byte = companions[0]
    gizmo = companions[1]
    assert isinstance(byte, dict)
    assert isinstance(gizmo, dict)
    pointer = byte["pointer"]
    assert isinstance(pointer, dict)

    if case == "top_level_extra":
        payload["future_field"] = True
    elif case == "wrong_discovery_schema":
        payload["schema_version"] = "pack-runtime-discovery/v2"
    elif case == "wrong_catalog_schema":
        payload["catalog_schema_version"] = "pack-runtime-catalog/v2"
    elif case == "wrong_asset_origin":
        payload["asset_origin"] = "https://assets.example.invalid"
    elif case == "missing_quarantine":
        payload["quarantined_asset_prefixes"] = []
    elif case == "duplicate_quarantine":
        payload["quarantined_asset_prefixes"] = ["byte", "byte"]
    elif case == "entry_extra":
        byte["asset_path"] = "/runtime/v1/assets/puppy/unsafe.png"
    elif case == "byte_wrong_species":
        byte["species_id"] = "mouse"
    elif case == "puppy_wrong_character":
        byte["character_id"] = "imposter"
    elif case == "available_without_pointer":
        byte["pointer"] = None
    elif case == "available_with_unavailable_reason":
        byte["unavailable_reason"] = "runtime_pack_not_published"
    elif case == "unavailable_with_pointer":
        gizmo["pointer"] = copy.deepcopy(pointer)
    elif case == "unavailable_without_reason":
        gizmo["unavailable_reason"] = None
    elif case == "unknown_availability":
        gizmo["availability"] = "pending"
    elif case == "missing_explicit_pointer":
        gizmo.pop("pointer")
    elif case == "missing_explicit_reason":
        gizmo.pop("unavailable_reason")
    elif case == "wrong_fallback":
        gizmo["fallback"] = "picker_svg"
    elif case == "pointer_extra":
        pointer["cache_key"] = "unsafe"
    elif case == "wrong_pointer_schema":
        pointer["catalog_schema_version"] = "pack-runtime-catalog/v2"
    elif case == "wrong_pointer_host":
        pointer["url"] = str(pointer["url"]).replace(
            "companion-frames.getpacked.ai",
            "assets.example.invalid",
        )
    elif case == "wrong_pointer_digest":
        pointer["sha256"] = "b" * 64
    elif case == "uppercase_sha256":
        pointer["sha256"] = "A" * 64
    elif case == "boolean_sequence":
        pointer["sequence"] = True
    elif case == "string_byte_size":
        pointer["byte_size"] = "4096"
    elif case == "duplicate_character":
        duplicate = copy.deepcopy(gizmo)
        duplicate["species_id"] = "hedgehog"
        companions.append(duplicate)
    elif case == "duplicate_species":
        duplicate = copy.deepcopy(gizmo)
        duplicate["character_id"] = "spike"
        companions.append(duplicate)
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(case)

    with pytest.raises(ValidationError):
        RuntimeCatalogDiscoveryResponse.model_validate(payload)


@pytest.mark.asyncio
async def test_client_fetches_runtime_discovery_with_exact_authenticated_empty_get() -> None:
    api_key = "runtime-catalog-app-key"
    secret = "runtime-catalog-hmac-secret"
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        body = await request.aread()
        timestamp = request.headers["X-Pack-Timestamp"]
        signing_string = f"{timestamp}\nGET\n/v1/runtime-catalog\n".encode("utf-8")
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            signing_string,
            hashlib.sha256,
        ).hexdigest()
        assert request.method == "GET"
        assert request.url.path == "/v1/runtime-catalog"
        assert body == b""
        assert request.headers["X-Pack-App-Key"] == api_key
        assert request.headers["X-Pack-Signature"] == expected_signature
        assert request.headers["Accept-Encoding"] == "identity"
        assert "Content-Type" not in request.headers
        return httpx.Response(200, json=_discovery_payload())

    client = CompanionsClient(
        api_key=api_key,
        secret=secret,
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    discovery = await client.get_runtime_catalog()

    assert requests == 1
    assert discovery.companions[0].character_id == "byte"
    assert discovery.companions[0].species_id == "puppy"
    assert discovery.companions[0].pointer is not None
    assert discovery.companions[1].availability == "unavailable"


@pytest.mark.asyncio
async def test_client_runtime_discovery_failure_is_sanitized_and_retains_no_wire_data() -> None:
    upstream_marker = "runtime-discovery-upstream-private-marker"
    api_key = "runtime-discovery-api-key-marker"
    secret = "runtime-discovery-signing-secret-marker"
    invalid = _discovery_payload()
    companions = invalid["companions"]
    assert isinstance(companions, list)
    byte = companions[0]
    assert isinstance(byte, dict)
    pointer = byte["pointer"]
    assert isinstance(pointer, dict)
    pointer["url"] = (
        "https://companion-frames.getpacked.ai/runtime/v1/catalogs/" + upstream_marker + ".json"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=invalid)

    client = CompanionsClient(
        api_key=api_key,
        secret=secret,
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsProtocolError) as raised:
        await client.get_runtime_catalog()

    assert str(raised.value) == "service returned an invalid runtime catalog discovery response"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__dict__ == {}
    retained = _sdk_traceback_locals(raised.value)
    assert upstream_marker not in retained
    assert api_key not in retained
    assert secret not in retained
