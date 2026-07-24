"""Typed request and decision models for ``POST /v1/comment``.

The service owns opportunity selection, wording, and semantic expression.  A
host sends facts with one stable ``client_event_id`` and treats every response
mode, including deliberate silence, as authoritative.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from pack_companions.snapshot import CompanionSnapshot

CommentEventType = Literal[
    "session_started",
    "navigation",
    "learning_event",
    "hint_requested",
    "activity_tick",
    "settings_changed",
]

OpportunityType = Annotated[str, Field(min_length=1, max_length=50)]
CoalescedOpportunityType = Annotated[
    str,
    Field(
        min_length=1,
        max_length=50,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]


class CommentOutcome(str, Enum):
    """Terminal outcomes produced by the v1 comment decision contract."""

    DELIVERED = "delivered"
    ACTION_ONLY = "action_only"
    THROTTLED = "throttled"
    DECLINED = "declined"
    NO_OPPORTUNITY = "no_opportunity"


class ExpressionIntent(str, Enum):
    """Controlled renderer-independent expression intentions."""

    NEUTRAL = "neutral"
    THINKING = "thinking"
    SURPRISED = "surprised"
    EXCITED = "excited"
    CELEBRATING = "celebrating"
    CONCERNED = "concerned"
    ANNOYED = "annoyed"
    ANGRY = "angry"
    SAD = "sad"
    COMFORTING = "comforting"
    CURIOUS = "curious"
    REFUSING = "refusing"
    GREETING = "greeting"
    BORED = "bored"


class ExpressionResponseMode(str, Enum):
    """How speech and visible action combine for one response."""

    SPEAK = "speak"
    SILENT = "silent"
    ACT_ONLY = "act_only"
    SPEAK_AND_ACT = "speak_and_act"


class ExpressionBehaviorIntent(str, Enum):
    """Controlled physical-behavior hints; never asset selectors."""

    DRINK = "drink"
    EAT = "eat"
    STRETCH = "stretch"
    REST = "rest"
    TIRED = "tired"
    SLEEPING = "sleeping"


_NO_SPEECH_MODES = frozenset(
    {
        ExpressionResponseMode.SILENT,
        ExpressionResponseMode.ACT_ONLY,
    }
)
_SILENT_OUTCOMES = frozenset(
    {
        CommentOutcome.THROTTLED,
        CommentOutcome.DECLINED,
        CommentOutcome.NO_OPPORTUNITY,
    }
)


class SemanticExpressionEnvelope(BaseModel):
    """A semantic expression that contains no renderer filenames or URLs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["v1"] = "v1"
    intent: ExpressionIntent
    intensity: float = Field(ge=0.0, le=1.0)
    response_mode: ExpressionResponseMode
    bubble_intent: ExpressionIntent | None = None
    behavior_intent: ExpressionBehaviorIntent | None = None
    speech_text: str | None = Field(default=None, max_length=32 * 1024)

    @field_validator("intent", mode="before")
    @classmethod
    def _unknown_intent_falls_back_to_neutral(
        cls,
        value: object,
    ) -> ExpressionIntent:
        if isinstance(value, ExpressionIntent):
            return value
        if isinstance(value, str):
            try:
                return ExpressionIntent(value.strip().casefold())
            except ValueError:
                pass
        return ExpressionIntent.NEUTRAL

    @field_validator("bubble_intent", mode="before")
    @classmethod
    def _unknown_bubble_intent_falls_back_to_neutral(
        cls,
        value: object,
    ) -> ExpressionIntent | None:
        if value is None or isinstance(value, ExpressionIntent):
            return value
        if isinstance(value, str):
            try:
                return ExpressionIntent(value.strip().casefold())
            except ValueError:
                pass
        return ExpressionIntent.NEUTRAL

    @field_validator("behavior_intent", mode="before")
    @classmethod
    def _unknown_behavior_intent_is_ignored(
        cls,
        value: object,
    ) -> ExpressionBehaviorIntent | None:
        if value is None or isinstance(value, ExpressionBehaviorIntent):
            return value
        if isinstance(value, str):
            try:
                return ExpressionBehaviorIntent(value.strip().casefold())
            except ValueError:
                pass
        return None

    @field_validator("speech_text", mode="before")
    @classmethod
    def _empty_speech_is_absent(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _silent_modes_cannot_carry_speech(self) -> SemanticExpressionEnvelope:
        if self.response_mode in _NO_SPEECH_MODES and self.speech_text is not None:
            raise ValueError(
                f"{self.response_mode.value} expression responses cannot carry speech_text"
            )
        return self


class CommentEvent(BaseModel):
    """One immutable typed fact envelope.

    The snapshot tree is deeply immutable and construction captures canonical
    request bytes once. Create this object once and reuse it for every
    caller-level retry; rebuilding the same ``client_event_id`` around
    different facts is a service conflict.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: CompanionSnapshot
    client_event_id: UUID
    event_type: CommentEventType
    _canonical_request_bytes: bytes = PrivateAttr()

    def model_post_init(self, __context: object) -> None:
        """Freeze the exact wire representation independently of nested models."""
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
        """Keep the captured private payload write-once as well as immutable."""
        if name == "_canonical_request_bytes" and hasattr(self, name):
            raise TypeError("CommentEvent request bytes are immutable")
        super().__setattr__(name, value)

    @property
    def request_bytes(self) -> bytes:
        """Exact immutable bytes to sign and reuse for every transmission."""
        return self._canonical_request_bytes

    @classmethod
    def from_request_bytes(cls, payload: bytes | str) -> CommentEvent:
        """Restore a durable event from the exact payload saved before delivery."""
        return cls.model_validate_json(payload)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> CommentEvent:
        """Return a fully revalidated event whose bytes match any updates.

        Pydantic's default ``model_copy(update=...)`` deliberately skips
        validation and copies private attributes. That would let a copied event
        expose changed fields while retaining the old signed payload. Rebuild
        through validation instead; ``deep`` is accepted for API compatibility
        but immaterial because the complete snapshot tree is immutable.
        """
        del deep
        material: dict[str, Any] = json.loads(self.request_bytes)
        if update:
            material.update(update)
        return type(self).model_validate(material)

    @classmethod
    def create(
        cls,
        *,
        snapshot: CompanionSnapshot,
        event_type: CommentEventType,
        client_event_id: UUID | None = None,
    ) -> CommentEvent:
        """Create a typed event with one caller-visible stable UUID."""
        return cls(
            snapshot=snapshot,
            client_event_id=client_event_id or uuid4(),
            event_type=event_type,
        )

    @model_validator(mode="after")
    def _session_start_fact_is_coherent(self) -> CommentEvent:
        if self.event_type == "session_started" and (
            self.snapshot.session.id is None or not self.snapshot.session.started
        ):
            raise ValueError("session_started events require a session id and started=true")
        if self.event_type != "session_started" and self.snapshot.session.started:
            raise ValueError("started=true is only valid for session_started events")
        return self


class Comment(BaseModel):
    """Legacy-compatible delivered-comment object."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    text: str = Field(min_length=1, max_length=240)
    opportunity_type: OpportunityType
    was_ai: bool
    model: str | None = Field(default=None, max_length=50)


class CommentResult(BaseModel):
    """Full v1 decision envelope with bounded legacy response parsing.

    Unknown top-level fields are intentionally ignored for additive forward
    compatibility.  Nested expressions remain strict, so an upstream response
    cannot smuggle renderer paths or arbitrary animation selectors into the
    semantic envelope.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: Literal["v1"] = "v1"
    decision_id: UUID | None = None
    outcome: CommentOutcome | None = None
    deduplicated: bool = False
    text: str | None = Field(default=None, max_length=240)
    opportunity_type: OpportunityType | None = None
    coalesced_opportunity_types: list[CoalescedOpportunityType] = Field(
        default_factory=list,
        max_length=8,
    )
    expression: SemanticExpressionEnvelope | None = None
    response_locale: Literal["en", "es"] = "en"
    speech_locale: Literal["en-US", "es-MX"] = "en-US"
    next_evaluation_at: datetime | None = None

    # One compatibility window for pre-contract service responses.
    comment: Comment | None = None
    throttled: bool = False
    throttle_mode: str | None = Field(default=None, max_length=16)
    declined: bool = False
    reason: str | None = Field(default=None, max_length=64)

    @property
    def speaks(self) -> bool:
        """Whether this decision authoritatively contains companion speech."""
        return self.outcome is CommentOutcome.DELIVERED and self.text is not None

    @property
    def deliberately_silent(self) -> bool:
        """Whether a host must preserve a no-speech decision."""
        return self.outcome in {
            CommentOutcome.ACTION_ONLY,
            CommentOutcome.DECLINED,
            CommentOutcome.NO_OPPORTUNITY,
            CommentOutcome.THROTTLED,
        }

    @model_validator(mode="after")
    def _derive_and_validate_decision(self) -> CommentResult:
        if self.comment is not None:
            if self.text is None:
                self.text = self.comment.text
            elif self.text != self.comment.text:
                raise ValueError("top-level text must match comment.text")
            if self.opportunity_type is None:
                self.opportunity_type = self.comment.opportunity_type
            elif self.opportunity_type != self.comment.opportunity_type:
                raise ValueError("top-level opportunity_type must match comment.opportunity_type")
            if self.decision_id is None:
                self.decision_id = self.comment.id

        if self.outcome is None:
            if self.comment is not None:
                self.outcome = CommentOutcome.DELIVERED
            elif self.throttled:
                self.outcome = CommentOutcome.THROTTLED
            elif self.declined:
                self.outcome = CommentOutcome.DECLINED
            else:
                self.outcome = CommentOutcome.NO_OPPORTUNITY

        if self.outcome is CommentOutcome.DELIVERED and self.comment is None:
            raise ValueError("delivered comment decisions require comment")
        if self.outcome is not CommentOutcome.DELIVERED and self.comment is not None:
            raise ValueError("non-delivered comment decisions cannot carry comment")
        if self.outcome is CommentOutcome.ACTION_ONLY and (
            self.expression is None
            or self.expression.response_mode is not ExpressionResponseMode.ACT_ONLY
        ):
            raise ValueError("action-only decisions require act_only expression")
        if self.outcome is CommentOutcome.THROTTLED:
            self.throttled = True
        elif self.throttled:
            raise ValueError("throttled=true requires the throttled outcome")
        if self.outcome is CommentOutcome.DECLINED:
            self.declined = True
        elif self.declined:
            raise ValueError("declined=true requires the declined outcome")
        if self.text is not None and self.comment is None:
            raise ValueError("text requires a delivered comment")
        if self.expression is not None:
            mode = self.expression.response_mode
            if self.outcome is CommentOutcome.DELIVERED and mode not in {
                ExpressionResponseMode.SPEAK,
                ExpressionResponseMode.SPEAK_AND_ACT,
            }:
                raise ValueError("delivered decisions require a speaking expression mode")
            if self.outcome in _SILENT_OUTCOMES and mode is not ExpressionResponseMode.SILENT:
                raise ValueError(
                    "throttled, declined, and no-opportunity decisions require "
                    "silent expression mode"
                )
            if (
                mode is ExpressionResponseMode.ACT_ONLY
                and self.outcome is not CommentOutcome.ACTION_ONLY
            ):
                raise ValueError("act_only expression mode requires the action_only outcome")
            if self.expression.speech_text is not None and self.expression.speech_text != self.text:
                raise ValueError("expression speech_text must match screened response text")
        if self.response_locale == "es" and self.speech_locale != "es-MX":
            raise ValueError("Spanish responses require es-MX speech locale")
        if self.response_locale == "en" and self.speech_locale != "en-US":
            raise ValueError("English responses require en-US speech locale")
        return self
