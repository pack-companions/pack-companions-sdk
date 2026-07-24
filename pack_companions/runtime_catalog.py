"""Strict discovery contract for Pack-owned companion runtime catalogs.

This module validates only the authenticated discovery response and immutable
catalog pointer. Fetching and validating the referenced asset catalog belongs
to the renderer SDK or native client.
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RUNTIME_CATALOG_DISCOVERY_PATH = "/v1/runtime-catalog"
RUNTIME_DISCOVERY_SCHEMA_VERSION = "pack-runtime-discovery/v1"
RUNTIME_CATALOG_SCHEMA_VERSION = "pack-runtime-catalog/v1"
RUNTIME_CDN_ORIGIN = "https://companion-frames.getpacked.ai"
RUNTIME_FALLBACK = "neutral_medallion"
MAX_RUNTIME_CATALOG_BYTES = 1024 * 1024

_CanonicalId = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]
_Sha256 = Annotated[
    str,
    Field(strict=True, pattern=r"^[0-9a-f]{64}$"),
]
_CatalogUrl = Annotated[str, Field(strict=True, min_length=1, max_length=2048)]
_Sequence = Annotated[int, Field(strict=True, ge=1)]
_CatalogByteSize = Annotated[
    int,
    Field(strict=True, ge=2, le=MAX_RUNTIME_CATALOG_BYTES),
]


class _RuntimeDiscoveryModel(BaseModel):
    """Immutable versioned wire model with no unreviewed additive fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeCatalogPointer(_RuntimeDiscoveryModel):
    catalog_schema_version: Literal["pack-runtime-catalog/v1"]
    sequence: _Sequence
    url: _CatalogUrl
    sha256: _Sha256
    byte_size: _CatalogByteSize

    @model_validator(mode="after")
    def _url_is_exact_content_address(self) -> RuntimeCatalogPointer:
        expected = f"{RUNTIME_CDN_ORIGIN}/runtime/v1/catalogs/{self.sha256}.json"
        if self.url != expected:
            raise ValueError("runtime catalog URL must use the exact content-addressed Pack origin")
        return self


class RuntimePackAvailability(_RuntimeDiscoveryModel):
    character_id: _CanonicalId
    species_id: _CanonicalId
    availability: Literal["available", "unavailable"]
    pointer: RuntimeCatalogPointer | None
    unavailable_reason: Literal["runtime_pack_not_published"] | None
    fallback: Literal["neutral_medallion"]

    @model_validator(mode="after")
    def _availability_and_identity_are_exact(self) -> RuntimePackAvailability:
        if self.availability == "available":
            if self.pointer is None or self.unavailable_reason is not None:
                raise ValueError(
                    "available runtime packs require a pointer and no unavailable reason"
                )
        elif self.pointer is not None or self.unavailable_reason is None:
            raise ValueError("unavailable runtime packs require a reason and no pointer")

        if (self.character_id == "byte") != (self.species_id == "puppy"):
            raise ValueError("canonical Byte must bind character byte to species puppy")
        return self


class RuntimeCatalogDiscoveryResponse(_RuntimeDiscoveryModel):
    """Validated roster-filtered runtime availability for one authenticated app."""

    PATH: ClassVar[str] = RUNTIME_CATALOG_DISCOVERY_PATH

    schema_version: Literal["pack-runtime-discovery/v1"]
    catalog_schema_version: Literal["pack-runtime-catalog/v1"]
    asset_origin: Literal["https://companion-frames.getpacked.ai"]
    quarantined_asset_prefixes: tuple[Literal["byte"], ...] = Field(
        min_length=1,
        max_length=1,
    )
    companions: tuple[RuntimePackAvailability, ...] = Field(max_length=64)

    @field_validator("quarantined_asset_prefixes")
    @classmethod
    def _exact_quarantine(
        cls,
        value: tuple[Literal["byte"], ...],
    ) -> tuple[Literal["byte"], ...]:
        if value != ("byte",):
            raise ValueError("runtime discovery must preserve the /byte quarantine")
        return value

    @model_validator(mode="after")
    def _roster_identities_are_unique(self) -> RuntimeCatalogDiscoveryResponse:
        character_ids = tuple(item.character_id for item in self.companions)
        species_ids = tuple(item.species_id for item in self.companions)
        if len(character_ids) != len(set(character_ids)):
            raise ValueError("runtime discovery contains duplicate character ids")
        if len(species_ids) != len(set(species_ids)):
            raise ValueError("runtime discovery contains duplicate species ids")
        return self
