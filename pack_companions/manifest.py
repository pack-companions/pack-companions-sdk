"""Typed response contract for authenticated ``GET /v1/app/manifest``.

The manifest is the server-authoritative integration boundary for one app's
posture, roster, and Pack endpoint paths.  Hosts validate it before enabling a
companion runtime; they do not infer a roster or character/species mapping
locally.
"""

from __future__ import annotations

import re
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ManifestSurfaceKind = Literal["web", "mobile", "creator"]
ManifestImpulsePacing = Literal["none", "sparse", "friend", "heartbeat"]
ManifestMemoryAccessPolicy = Literal[
    "none",
    "app_local_only",
    "explicit_consent_shared",
]
ManifestAllowedContext = Literal[
    "page_path",
    "problem",
    "current_mood",
    "learning_progress",
    "ambient_signals",
]
ManifestCapability = Literal[
    "coding_topic_fence",
    "chat_cap_exemption",
    "companion_initiative",
    "creator_workshop",
    "fitness_support",
    "learning_support",
    "perception",
    "semantic_expression",
    "web_lookup",
    "whole_life_conversation",
]
ManifestExpressionSurface = Literal[
    "animation",
    "bubble",
    "notification",
    "semantic_expression",
    "speech",
    "text",
]
ManifestForbiddenAssumption = Literal[
    "app_specific_mission",
    "coding_coach",
    "coding_help",
    "code_problem_solver",
    "fitness_coach",
    "medical_advice",
    "tool_claims",
    "whole_life_friend",
]

_StrictPolicyVersion = Annotated[int, Field(strict=True, ge=1, le=1)]
_StrictOptionalChatCap = Annotated[int, Field(strict=True, ge=1, le=1000)] | None
_StrictOptionalReplyCap = Annotated[int, Field(strict=True, ge=32, le=1200)] | None
_CanonicalId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]

_PLATFORM_V2_PATTERN = re.compile(
    r"^2\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class _ManifestModel(BaseModel):
    """Immutable response model that tolerates additive v2 response fields."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class ManifestApp(_ManifestModel):
    id: UUID
    name: str = Field(min_length=1, max_length=255)

    @field_validator("id")
    @classmethod
    def _reject_nil_app_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("manifest app id must not be the nil UUID")
        return value


class ManifestPosture(_ManifestModel):
    policy_version: _StrictPolicyVersion
    purpose: str = Field(max_length=500)
    allowed_context: tuple[ManifestAllowedContext, ...] = Field(max_length=32)
    capabilities: tuple[ManifestCapability, ...] = Field(max_length=32)
    surface_kind: ManifestSurfaceKind
    impulse_pacing: ManifestImpulsePacing
    senses_enabled: bool = Field(strict=True)
    memory_access_policy: ManifestMemoryAccessPolicy
    expression_surfaces: tuple[ManifestExpressionSurface, ...] = Field(max_length=32)
    forbidden_assumptions: tuple[ManifestForbiddenAssumption, ...] = Field(max_length=32)
    chat_daily_cap: _StrictOptionalChatCap
    reply_token_cap: _StrictOptionalReplyCap

    @model_validator(mode="after")
    def _policy_lists_are_canonical(self) -> ManifestPosture:
        for field in (
            "allowed_context",
            "capabilities",
            "expression_surfaces",
            "forbidden_assumptions",
        ):
            values = getattr(self, field)
            if len(values) != len(set(values)):
                raise ValueError("manifest posture lists must not contain duplicates")
        return self


class ManifestCompanion(_ManifestModel):
    id: _CanonicalId
    species: _CanonicalId
    display_name: str = Field(min_length=1, max_length=128)


class ManifestEndpoints(_ManifestModel):
    """The platform-v2 endpoint set.

    These values are authenticated but still validated against the SDK
    contract.  A host must never turn a future or malformed manifest string
    into an arbitrary request target.
    """

    EXPECTED_PATHS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("comment", "/v1/comment"),
        ("chat", "/v1/companion/chat"),
        ("chat_stream", "/v1/companion/chat/stream"),
        ("history", "/v1/companion/chat/history"),
        ("unread", "/v1/companion/chat/unread"),
        ("mood", "/v1/companion/chat/mood"),
        ("impulse", "/v1/companion/chat/impulse"),
        ("perceive", "/v1/companion/perceive"),
        ("perception_consent", "/v1/companion/perception/consent"),
        ("memory_facts", "/v1/memory/facts"),
        ("memory_exclude", "/v1/memory/exclude"),
        ("forget", "/v1/memory/forget"),
        ("delete_chat_history", "/v1/memory/delete-chat-history"),
        ("observation_delete", "/v1/memory/observations/delete"),
        (
            "share_memory_set",
            "/v1/user-settings/share-memory-across-apps/set",
        ),
        ("identify", "/v1/identity/identify"),
        ("link_status", "/v1/identity/link-status"),
        (
            "connected_apps_status",
            "/v1/identity/connected-apps/status",
        ),
        ("erase_for_app", "/v1/identity/erase-for-app"),
        ("letters", "/v1/companion/letters"),
        ("letters_ack", "/v1/companion/letters/ack"),
        ("companion_picker", "/v1/companions/picker"),
    )

    comment: str
    chat: str
    chat_stream: str
    history: str
    unread: str
    mood: str
    impulse: str
    perceive: str
    perception_consent: str
    memory_facts: str
    memory_exclude: str
    forget: str
    delete_chat_history: str
    observation_delete: str
    share_memory_set: str
    identify: str
    link_status: str
    connected_apps_status: str
    erase_for_app: str
    letters: str
    letters_ack: str
    companion_picker: str

    @model_validator(mode="after")
    def _paths_match_platform_v2(self) -> ManifestEndpoints:
        mismatched = [
            field for field, expected in self.EXPECTED_PATHS if getattr(self, field) != expected
        ]
        if mismatched:
            raise ValueError("manifest endpoint paths are incompatible with this SDK")
        return self


class ManifestService(_ManifestModel):
    version: str = Field(min_length=1, max_length=64)
    platform_contract_version: str = Field(min_length=5, max_length=64)
    comment_contract_version: Literal["v1"]
    picker_version: str = Field(min_length=1, max_length=64)

    @field_validator("platform_contract_version")
    @classmethod
    def _require_platform_v2(cls, value: str) -> str:
        if _PLATFORM_V2_PATTERN.fullmatch(value) is None:
            raise ValueError("manifest platform contract major is unsupported")
        return value


class AppManifestResponse(_ManifestModel):
    """Validated manifest for the authenticated app."""

    PATH: ClassVar[str] = "/v1/app/manifest"

    app: ManifestApp
    posture: ManifestPosture
    companions: tuple[ManifestCompanion, ...] = Field(max_length=64)
    endpoints: ManifestEndpoints
    service: ManifestService

    @model_validator(mode="after")
    def _roster_ids_are_unique(self) -> AppManifestResponse:
        companion_ids = tuple(companion.id for companion in self.companions)
        if len(companion_ids) != len(set(companion_ids)):
            raise ValueError("manifest contains duplicate companion ids")
        return self
