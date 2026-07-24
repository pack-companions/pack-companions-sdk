"""Typed account-link and privacy-operation contract.

Privacy mutations are generation-bound, exactly retryable operations.  The
host persists :class:`AccountLinkState`, captures it together with one stable
``client_privacy_event_id`` when the user initiates a mutation, and reuses the
exact event bytes for every retry.

This module deliberately does not keep process-global or client-global account
state.  One SDK client can serve many host users concurrently, so persistence
and compare-and-swap belong to the authenticated host backend.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Any, ClassVar, Literal, TypeVar
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
)

from pack_companions.snapshot import UserTier

PrivacyGeneration = Annotated[int, Field(strict=True, ge=0)]
LinkStatus = Literal["active", "suspended", "revoked"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrivacyErrorDisposition(str, Enum):
    """What a durable worker may safely do after a privacy-operation error."""

    RETRY_SAME_EVENT = "retry_same_event"
    DISCARD_EVENT = "discard_event"
    START_NEW_READ = "start_new_read"


class PrivacyOperationErrorCode(str, Enum):
    """Machine codes emitted by Pack's privacy-operation receipt boundary."""

    STALE_ACCOUNT_INCARNATION = "stale_account_incarnation"
    STALE_PRIVACY_GENERATION = "stale_privacy_generation"
    PRIVACY_EVENT_CONFLICT = "privacy_event_conflict"
    PRIVACY_OPERATION_SUPERSEDED = "privacy_operation_superseded"
    PRIVACY_RECEIPT_EXPIRED = "privacy_receipt_expired"
    PRIVACY_OPERATION_IN_PROGRESS = "privacy_operation_in_progress"
    PRIVACY_RECEIPT_SCHEMA_UNSUPPORTED = "privacy_receipt_schema_unsupported"
    PRIVACY_GENERATION_INCONSISTENT = "privacy_generation_inconsistent"
    PRIVACY_OPERATION_LIMIT_EXCEEDED = "privacy_operation_limit_exceeded"
    PRIVACY_RECEIPT_KEY_UNAVAILABLE = "privacy_receipt_key_unavailable"
    PRIVACY_RECEIPT_KEY_UNATTESTED = "privacy_receipt_key_unattested"

    @property
    def disposition(self) -> PrivacyErrorDisposition:
        """Return the only safe next action for the original queued event."""
        if self in {
            PrivacyOperationErrorCode.PRIVACY_OPERATION_IN_PROGRESS,
            PrivacyOperationErrorCode.PRIVACY_OPERATION_LIMIT_EXCEEDED,
            PrivacyOperationErrorCode.PRIVACY_GENERATION_INCONSISTENT,
            PrivacyOperationErrorCode.PRIVACY_RECEIPT_KEY_UNAVAILABLE,
            PrivacyOperationErrorCode.PRIVACY_RECEIPT_KEY_UNATTESTED,
        }:
            return PrivacyErrorDisposition.RETRY_SAME_EVENT
        if self in {
            PrivacyOperationErrorCode.PRIVACY_OPERATION_SUPERSEDED,
            PrivacyOperationErrorCode.PRIVACY_RECEIPT_EXPIRED,
            PrivacyOperationErrorCode.PRIVACY_RECEIPT_SCHEMA_UNSUPPORTED,
            PrivacyOperationErrorCode.STALE_ACCOUNT_INCARNATION,
            PrivacyOperationErrorCode.STALE_PRIVACY_GENERATION,
        }:
            return PrivacyErrorDisposition.START_NEW_READ
        return PrivacyErrorDisposition.DISCARD_EVENT


class AccountLinkState(_FrozenModel):
    """Host-persisted token pair for one concrete app/account incarnation."""

    link_incarnation_id: UUID
    privacy_generation: PrivacyGeneration

    @field_validator("link_incarnation_id")
    @classmethod
    def _reject_nil_incarnation(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("link_incarnation_id must not be the nil UUID")
        return value

    @classmethod
    def from_identify(cls, response: IdentifyResponse) -> AccountLinkState:
        """Create state from an explicit, authenticated identify ceremony."""
        return cls(
            link_incarnation_id=response.link_incarnation_id,
            privacy_generation=response.privacy_generation,
        )

    def reconcile_identify(self, response: IdentifyResponse) -> AccountLinkState:
        """Observe identify without allowing out-of-order generation rollback.

        A different incarnation is an explicit account replacement boundary,
        so its identify generation replaces the old value.  Within the same
        incarnation only the maximum observed generation is retained.
        """
        if response.link_incarnation_id != self.link_incarnation_id:
            return type(self).from_identify(response)
        return self.observe_generation(response.privacy_generation)

    def observe_generation(self, generation: int) -> AccountLinkState:
        """Retain the maximum generation observed for this incarnation."""
        validated = type(self)(
            link_incarnation_id=self.link_incarnation_id,
            privacy_generation=generation,
        )
        if validated.privacy_generation <= self.privacy_generation:
            return self
        return validated

    def observe_result(
        self,
        result: PrivacyGenerationResult,
    ) -> AccountLinkState:
        """Advance from a successful mutation response, never roll back."""
        return self.observe_generation(result.privacy_generation)

    def snapshot_user(
        self,
        *,
        host_user_id: str,
        tier: UserTier = "free",
        email_hash: str | None = None,
        identity_subject: str | None = None,
    ) -> PrivacySnapshotUser:
        """Bind this exact state to a minimal privacy-operation identity."""
        return PrivacySnapshotUser(
            id=host_user_id,
            tier=tier,
            email_hash=email_hash,
            identity_subject=identity_subject,
            expected_link_incarnation_id=self.link_incarnation_id,
            expected_privacy_generation=self.privacy_generation,
        )


_RequestT = TypeVar("_RequestT", bound="_CanonicalRequest")


class _CanonicalRequest(_FrozenModel):
    """Immutable request whose canonical bytes can be durably retried."""

    _canonical_request_bytes: bytes = PrivateAttr()

    def model_post_init(self, __context: object) -> None:
        material = self.model_dump(mode="json")
        object.__setattr__(
            self,
            "_canonical_request_bytes",
            json.dumps(
                material,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_canonical_request_bytes" and hasattr(self, name):
            raise TypeError("request bytes are immutable")
        super().__setattr__(name, value)

    @property
    def request_bytes(self) -> bytes:
        """Exact immutable bytes to sign on every transmission."""
        return self._canonical_request_bytes

    @classmethod
    def from_request_bytes(
        cls: type[_RequestT],
        payload: bytes | str,
    ) -> _RequestT:
        """Restore one exact durable request without generating any new ID."""
        return cls.model_validate_json(payload)

    def model_copy(
        self: _RequestT,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> _RequestT:
        """Revalidate updates so fields and captured bytes cannot diverge."""
        del deep
        material: dict[str, Any] = json.loads(self.request_bytes)
        if update:
            material.update(update)
        return type(self).model_validate(material)


class IdentifyRequest(_CanonicalRequest):
    """Explicit identity bootstrap/recovery request."""

    host_user_id: str = Field(min_length=1, max_length=255)
    email_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    identity_subject: str | None = Field(
        default=None,
        min_length=32,
        max_length=128,
        pattern=r"^[A-Za-z0-9._~-]+$",
    )
    # Opt-in recovery mode: Pack may return only an already-established active
    # app-local link and its current token pair. The default preserves the
    # historical identify ceremony, including first-account creation.
    require_existing_link: bool = False
    expected_link_incarnation_id: UUID | None = None
    re_registration_erasure_id: UUID | None = None

    def model_post_init(self, __context: object) -> None:
        """Preserve pre-0.3.1 bytes unless existing-only mode is requested.

        Some older provider builds reject unknown request fields. Keeping the
        new false default out of canonical bytes lets ordinary identifies
        remain byte-for-byte compatible while true is transported explicitly.
        """
        super().model_post_init(__context)
        if self.require_existing_link:
            return
        material: dict[str, Any] = json.loads(self.request_bytes)
        material.pop("require_existing_link", None)
        object.__setattr__(
            self,
            "_canonical_request_bytes",
            json.dumps(
                material,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )

    @field_validator(
        "expected_link_incarnation_id",
        "re_registration_erasure_id",
    )
    @classmethod
    def _reject_nil_identity_uuid(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("identity UUID fields must not be the nil UUID")
        return value


class IdentifyResponse(_FrozenModel):
    """Identity response including the complete host-persisted token pair."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    global_user_id: str = Field(
        min_length=5,
        max_length=128,
        pattern=r"^pcu_[a-z2-7]+$",
    )
    is_new: bool
    link_incarnation_id: UUID
    privacy_generation: PrivacyGeneration
    linked_keys: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("link_incarnation_id")
    @classmethod
    def _reject_nil_incarnation(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("link_incarnation_id must not be the nil UUID")
        return value

    @property
    def account_link_state(self) -> AccountLinkState:
        """Return the pair the host must persist atomically."""
        return AccountLinkState.from_identify(self)


class ConnectedAppsStatusRequest(_CanonicalRequest):
    """Read one existing app-owned link without minting an identity."""

    PATH: ClassVar[str] = "/v1/identity/connected-apps/status"

    host_user_id: str = Field(min_length=1, max_length=255)
    expected_link_incarnation_id: UUID | None = None
    expected_privacy_generation: PrivacyGeneration | None = None

    @field_validator("expected_link_incarnation_id")
    @classmethod
    def _reject_nil_incarnation(cls, value: UUID | None) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("expected_link_incarnation_id must not be the nil UUID")
        return value


class CurrentAppConnectionStatus(_FrozenModel):
    """Calling-app view of its link without echoing the host handle."""

    app_id: UUID
    app_key: str = Field(min_length=1, max_length=100)
    linked: bool
    link_status: (
        Literal[
            "provisional",
            "active",
            "suspended",
            "revoked",
        ]
        | None
    ) = None

    @field_validator("app_id")
    @classmethod
    def _reject_nil_app_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("app_id must not be the nil UUID")
        return value


class ConnectedAppStatus(_FrozenModel):
    """Proof-verified connected app safe for account-management UI."""

    app_id: UUID
    app_key: str = Field(min_length=1, max_length=100)
    link_status: Literal["active"]
    app_status: Literal["active", "suspended", "revoked"]
    is_current_app: bool

    @field_validator("app_id")
    @classmethod
    def _reject_nil_app_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("app_id must not be the nil UUID")
        return value


class ConnectedAppsStatusResponse(_FrozenModel):
    """Read-only proof and connection status for an existing account link."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal["v1"]
    current_app_link: CurrentAppConnectionStatus
    explicit_cross_app_proof_verified: bool
    verified_connected_app_count: int = Field(strict=True, ge=0)
    connected_apps_truncated: bool
    connected_apps: tuple[ConnectedAppStatus, ...] = Field(
        default_factory=tuple,
        max_length=32,
    )


class EraseForAppEvent(_CanonicalRequest):
    """One durable app-account erasure job from Platform Contract ID-4.

    ``expected_link_incarnation_id`` is deliberately required as an argument
    even though its value may be ``None``. Null means the trusted host knows
    Pack never established a link; it is not a fallback for a lost token.
    """

    PATH: ClassVar[str] = "/v1/identity/erase-for-app"

    user_id: str = Field(min_length=1, max_length=255)
    client_erasure_event_id: UUID
    expected_link_incarnation_id: UUID | None

    @field_validator(
        "client_erasure_event_id",
        "expected_link_incarnation_id",
    )
    @classmethod
    def _reject_nil_erasure_uuid(
        cls,
        value: UUID | None,
    ) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("erasure UUID fields must not use the nil UUID")
        return value

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> EraseForAppEvent:
        """Copy without permitting one erasure UUID to name another job."""
        if update:
            raise TypeError(
                "erase-for-app events cannot be updated; create a new event "
                "with a new client_erasure_event_id"
            )
        return super().model_copy(update=None, deep=deep)

    @classmethod
    def create(
        cls,
        *,
        user_id: str,
        expected_link_incarnation_id: UUID | None,
        client_erasure_event_id: UUID | None = None,
    ) -> EraseForAppEvent:
        """Create the deletion job once; every retry restores these bytes."""
        return cls(
            user_id=user_id,
            client_erasure_event_id=(
                client_erasure_event_id if client_erasure_event_id is not None else uuid4()
            ),
            expected_link_incarnation_id=expected_link_incarnation_id,
        )

    @classmethod
    def create_for_state(
        cls,
        *,
        user_id: str,
        state: AccountLinkState,
        client_erasure_event_id: UUID | None = None,
    ) -> EraseForAppEvent:
        """Capture the exact linked-account incarnation being erased."""
        return cls.create(
            user_id=user_id,
            expected_link_incarnation_id=state.link_incarnation_id,
            client_erasure_event_id=client_erasure_event_id,
        )


class EraseForAppResult(BaseModel):
    """Terminal erasure receipt that must outlive the deleted user row."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    erased: bool
    identity_deleted: bool
    erasure_id: UUID

    @field_validator("erasure_id")
    @classmethod
    def _reject_nil_erasure_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("erasure_id must not be the nil UUID")
        return value


class PrivacySnapshotUser(_FrozenModel):
    """Minimal identity carried by generation-bound privacy mutations."""

    id: str = Field(min_length=1, max_length=255)
    tier: UserTier = "free"
    email_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    identity_subject: str | None = Field(
        default=None,
        min_length=32,
        max_length=128,
        pattern=r"^[A-Za-z0-9._~-]+$",
    )
    expected_link_incarnation_id: UUID
    expected_privacy_generation: PrivacyGeneration

    @field_validator("expected_link_incarnation_id")
    @classmethod
    def _reject_nil_incarnation(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("expected_link_incarnation_id must not be the nil UUID")
        return value


_EventT = TypeVar("_EventT", bound="PrivacyOperationEvent")


class PrivacyOperationEvent(_CanonicalRequest):
    """Base for one immutable, exactly retryable privacy mutation."""

    PATH: ClassVar[str]
    OPERATION: ClassVar[str]

    client_privacy_event_id: UUID

    @field_validator("client_privacy_event_id")
    @classmethod
    def _reject_nil_event_id(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("client_privacy_event_id must not be the nil UUID")
        return value

    def model_copy(
        self: _EventT,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> _EventT:
        """Copy without permitting an event/payload split.

        Pydantic normally allows ``model_copy(update=...)`` even for frozen
        models. For a durable privacy event that convenience is unsafe: it can
        retain the event UUID while changing the payload or captured account
        tokens. A different logical operation must be created explicitly with
        a new UUID.
        """
        if update:
            raise TypeError(
                "privacy operation events cannot be updated; create a new "
                "event with a new client_privacy_event_id"
            )
        return super().model_copy(update=None, deep=deep)

    @classmethod
    def _create(
        cls: type[_EventT],
        *,
        client_privacy_event_id: UUID | None = None,
        **payload: Any,
    ) -> _EventT:
        """Create the event once; retries reuse this object or its saved bytes."""
        return cls.model_validate(
            {
                **payload,
                "client_privacy_event_id": (
                    client_privacy_event_id if client_privacy_event_id is not None else uuid4()
                ),
            }
        )


class MemoryExcludeEvent(PrivacyOperationEvent):
    PATH = "/v1/memory/exclude"
    OPERATION = "memory.exclude"

    snapshot_user: PrivacySnapshotUser
    companion_id: str = Field(min_length=1, max_length=64)
    fact_key: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
    )
    fact_value: str = Field(min_length=1, max_length=1000)

    @classmethod
    def create(
        cls,
        *,
        snapshot_user: PrivacySnapshotUser,
        companion_id: str,
        fact_key: str,
        fact_value: str,
        client_privacy_event_id: UUID | None = None,
    ) -> MemoryExcludeEvent:
        return super()._create(
            snapshot_user=snapshot_user,
            companion_id=companion_id,
            fact_key=fact_key,
            fact_value=fact_value,
            client_privacy_event_id=client_privacy_event_id,
        )


class MemoryForgetEvent(PrivacyOperationEvent):
    PATH = "/v1/memory/forget"
    OPERATION = "memory.forget"

    snapshot_user: PrivacySnapshotUser
    companion_id: str = Field(min_length=1, max_length=64)

    @classmethod
    def create(
        cls,
        *,
        snapshot_user: PrivacySnapshotUser,
        companion_id: str,
        client_privacy_event_id: UUID | None = None,
    ) -> MemoryForgetEvent:
        return super()._create(
            snapshot_user=snapshot_user,
            companion_id=companion_id,
            client_privacy_event_id=client_privacy_event_id,
        )


class MemoryDeleteChatHistoryEvent(PrivacyOperationEvent):
    PATH = "/v1/memory/delete-chat-history"
    OPERATION = "memory.delete_chat_history"

    snapshot_user: PrivacySnapshotUser

    @classmethod
    def create(
        cls,
        *,
        snapshot_user: PrivacySnapshotUser,
        client_privacy_event_id: UUID | None = None,
    ) -> MemoryDeleteChatHistoryEvent:
        return super()._create(
            snapshot_user=snapshot_user,
            client_privacy_event_id=client_privacy_event_id,
        )


class ObservationDeleteEvent(PrivacyOperationEvent):
    PATH = "/v1/memory/observations/delete"
    OPERATION = "memory.observation_delete"

    snapshot_user: PrivacySnapshotUser
    companion_id: str = Field(min_length=1, max_length=64)
    observation_type: str = Field(min_length=1, max_length=50)

    @classmethod
    def create(
        cls,
        *,
        snapshot_user: PrivacySnapshotUser,
        companion_id: str,
        observation_type: str,
        client_privacy_event_id: UUID | None = None,
    ) -> ObservationDeleteEvent:
        return super()._create(
            snapshot_user=snapshot_user,
            companion_id=companion_id,
            observation_type=observation_type,
            client_privacy_event_id=client_privacy_event_id,
        )


class ShareMemoryEvent(PrivacyOperationEvent):
    PATH = "/v1/user-settings/share-memory-across-apps/set"
    OPERATION = "user_settings.share_memory"

    snapshot_user: PrivacySnapshotUser
    value: bool

    @classmethod
    def create(
        cls,
        *,
        snapshot_user: PrivacySnapshotUser,
        value: bool,
        client_privacy_event_id: UUID | None = None,
    ) -> ShareMemoryEvent:
        return super()._create(
            snapshot_user=snapshot_user,
            value=value,
            client_privacy_event_id=client_privacy_event_id,
        )


class PerceptionConsentEvent(PrivacyOperationEvent):
    PATH = "/v1/companion/perception/consent"
    OPERATION = "perception.consent"

    snapshot_user: PrivacySnapshotUser
    modality: str = Field(min_length=1, max_length=16)
    granted: bool

    @classmethod
    def create(
        cls,
        *,
        snapshot_user: PrivacySnapshotUser,
        modality: str,
        granted: bool,
        client_privacy_event_id: UUID | None = None,
    ) -> PerceptionConsentEvent:
        return super()._create(
            snapshot_user=snapshot_user,
            modality=modality,
            granted=granted,
            client_privacy_event_id=client_privacy_event_id,
        )


class LinkStatusEvent(PrivacyOperationEvent):
    PATH = "/v1/identity/link-status"
    OPERATION = "identity.link_status"

    host_user_id: str = Field(min_length=1, max_length=255)
    expected_link_incarnation_id: UUID
    expected_privacy_generation: PrivacyGeneration
    status: LinkStatus

    @field_validator("expected_link_incarnation_id")
    @classmethod
    def _reject_nil_incarnation(cls, value: UUID) -> UUID:
        if value.int == 0:
            raise ValueError("expected_link_incarnation_id must not be the nil UUID")
        return value

    @classmethod
    def create_for_state(
        cls,
        *,
        host_user_id: str,
        state: AccountLinkState,
        status: LinkStatus,
        client_privacy_event_id: UUID | None = None,
    ) -> LinkStatusEvent:
        """Capture one link-status transition against an exact token pair."""
        return cls.create(
            host_user_id=host_user_id,
            expected_link_incarnation_id=state.link_incarnation_id,
            expected_privacy_generation=state.privacy_generation,
            status=status,
            client_privacy_event_id=client_privacy_event_id,
        )

    @classmethod
    def create(
        cls,
        *,
        host_user_id: str,
        expected_link_incarnation_id: UUID,
        expected_privacy_generation: int,
        status: LinkStatus,
        client_privacy_event_id: UUID | None = None,
    ) -> LinkStatusEvent:
        return super()._create(
            host_user_id=host_user_id,
            expected_link_incarnation_id=expected_link_incarnation_id,
            expected_privacy_generation=expected_privacy_generation,
            status=status,
            client_privacy_event_id=client_privacy_event_id,
        )


class PrivacyGenerationResult(BaseModel):
    """Base for successful mutation responses carrying the resulting epoch."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    privacy_generation: PrivacyGeneration


class MemoryExcludeResult(PrivacyGenerationResult):
    scrubbed_summary_rows: int = Field(strict=True, ge=0)
    exclusion_recorded: bool


class MemoryForgetResult(PrivacyGenerationResult):
    deleted_summaries: int = Field(strict=True, ge=0)
    deleted_exclusions: int = Field(strict=True, ge=0)
    deleted_observations: int = Field(strict=True, ge=0)
    deleted_state: int = Field(default=0, strict=True, ge=0)


class MemoryDeleteChatHistoryResult(PrivacyGenerationResult):
    deleted_messages: int = Field(strict=True, ge=0)
    deleted_summaries: int = Field(strict=True, ge=0)
    deleted_usage: int = Field(strict=True, ge=0)
    deleted_observations: int = Field(strict=True, ge=0)
    deleted_exclusions: int = Field(strict=True, ge=0)
    deleted_state: int = Field(default=0, strict=True, ge=0)


class ObservationDeleteResult(PrivacyGenerationResult):
    deleted: bool
    exclusion_created: bool
    observation_type: str = Field(min_length=1, max_length=50)


class ShareMemoryResult(PrivacyGenerationResult):
    share_memory_across_apps: bool
    is_default: bool


class PerceptionConsentResult(PrivacyGenerationResult):
    modality: str = Field(min_length=1, max_length=16)
    granted: bool


class LinkStatusResult(PrivacyGenerationResult):
    host_user_id: str = Field(min_length=1, max_length=255)
    status: LinkStatus
