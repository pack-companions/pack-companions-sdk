"""CompanionSnapshot — mirror of the service's snapshot contract.

This file MUST stay in sync with:
- service: app/schemas/snapshot.py
- JS SDK: src/snapshot.ts

Phase 0b locked the v1 shape; new fields land as optional with safe
defaults so existing senders keep working.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

UserTier = Literal["free", "plus", "pro", "mobile"]
RouteIntent = Literal["practice", "course", "browse", "social", "settings", "unknown"]
ProblemStatus = Literal["not_started", "in_progress", "completed", "abandoned"]
TestResult = Literal["pass", "fail", "error", "none"]
MilestoneType = Literal[
    "first_solve",
    "level_up",
    "achievement_unlocked",
    "daily_challenge_completed",
    # Event-driven streak celebration — push it (value = crossed marker)
    # on the snapshot where the streak crosses a marker, so the service
    # celebrates the event once instead of the raw streak value on every
    # POST. Mirrors app/schemas/snapshot.py in the service.
    "streak_milestone",
]

_MAX_COMEBACK_HOURS = 100 * 365 * 24
_MAX_COUNTER = 1_000_000
_MAX_ELAPSED_SECONDS = 365 * 24 * 60 * 60

RecentAction = Annotated[str, Field(max_length=256)]
WeakCategory = Annotated[str, Field(max_length=100)]


class _FrozenSnapshotModel(BaseModel):
    """Immutable fact value used to build one exact comment event."""

    model_config = ConfigDict(frozen=True)


class SnapshotUser(_FrozenSnapshotModel):
    id: str = Field(min_length=1, max_length=255)
    tier: UserTier = "free"
    companion_id: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$",
    )
    streak: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    display_name: str | None = Field(default=None, max_length=100)
    # Legacy discovery hint retained for wire compatibility. It is not proof
    # of account ownership and must never auto-link or auto-merge apps.
    # Plaintext email never crosses the wire.
    email_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )
    # Opaque, app-audience-scoped subject established by the host backend.
    # Cross-app continuity still requires Pack's explicit verification flow.
    identity_subject: str | None = Field(
        default=None,
        min_length=32,
        max_length=128,
        pattern=r"^[A-Za-z0-9._~-]+$",
    )
    # Echo the immutable value returned by /v1/identity/identify. Pack uses it
    # to reject queued work from an erased account after the same host id is
    # explicitly re-registered.
    expected_link_incarnation_id: UUID | None = None
    # Monotonic privacy epoch returned together with the incarnation by
    # /v1/identity/identify. A host persists the maximum observed value for
    # that incarnation and captures it inside each durable queued operation.
    # Omission exists only for the service's first-generation compatibility
    # window; new integrations identify first and always send it.
    expected_privacy_generation: int | None = Field(
        default=None,
        strict=True,
        ge=0,
    )

    @field_validator("expected_link_incarnation_id")
    @classmethod
    def _reject_nil_link_incarnation(
        cls,
        value: UUID | None,
    ) -> UUID | None:
        if value is not None and value.int == 0:
            raise ValueError("expected_link_incarnation_id must not be the nil UUID")
        return value


class SnapshotLocation(_FrozenSnapshotModel):
    route: str = Field(min_length=1, max_length=2048)
    section: str | None = Field(default=None, max_length=100)
    sub_section: str | None = Field(default=None, max_length=100)
    route_intent: RouteIntent = "unknown"


class SnapshotActivity(_FrozenSnapshotModel):
    idle_seconds: int = Field(default=0, ge=0, le=_MAX_ELAPSED_SECONDS)
    time_on_page_seconds: int = Field(
        default=0,
        ge=0,
        le=_MAX_ELAPSED_SECONDS,
    )
    session_seconds: int = Field(default=0, ge=0, le=_MAX_ELAPSED_SECONDS)
    last_action: str | None = Field(default=None, max_length=256)
    recent_actions: tuple[RecentAction, ...] = Field(default_factory=tuple, max_length=10)


class SnapshotSession(_FrozenSnapshotModel):
    """Facts about one explicit app session.

    ``id`` supplies the durable once-per-session boundary.  ``started`` says
    that this event represents the opening of that session; it does not tell
    the brain which greeting or care behavior to choose.
    """

    id: UUID | None = None
    started: bool = False
    absence_seconds: int | None = Field(
        default=None,
        ge=0,
        le=int(_MAX_COMEBACK_HOURS * 3600),
    )
    timezone: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._+\-/]+$",
    )

    @field_validator("timezone")
    @classmethod
    def _valid_iana_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class SnapshotCompanionPreferences(_FrozenSnapshotModel):
    """User controls that govern companion personalization and care behavior."""

    ai_personalization_enabled: bool = True
    care_reminders_enabled: bool = True
    hydration_enabled: bool = True
    food_enabled: bool = True
    break_reminders_enabled: bool = True
    late_night_banter_enabled: bool = True
    quiet_hours_active: bool = False
    quiet_hours_allow_session_greeting: bool = True
    reduced_motion: bool = False


class SnapshotProblem(_FrozenSnapshotModel):
    id: str | None = Field(default=None, max_length=255)
    language: str | None = Field(default=None, max_length=32)
    status: ProblemStatus | None = None
    time_in_editor_seconds: int = Field(
        default=0,
        ge=0,
        le=_MAX_ELAPSED_SECONDS,
    )
    edits_since_run: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    last_test_result: TestResult = "none"
    last_error_type: str | None = Field(default=None, max_length=100)
    attempt_count: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    hint_used: bool = False
    pattern_first_touch: bool = False
    followup_available: bool = False
    # Server-side enriched. category is the problem's track (e.g.
    # "debugging"); bug_type is the planted-bug class on debugging problems
    # only (null elsewhere). Both optional so pricing/mobile snapshots omit them.
    category: str | None = Field(default=None, max_length=100)
    bug_type: str | None = Field(default=None, max_length=50)


class SnapshotCourse(_FrozenSnapshotModel):
    course_id: str | None = Field(default=None, max_length=255)
    lesson_id: str | None = Field(default=None, max_length=255)
    time_on_lesson_seconds: int = Field(
        default=0,
        ge=0,
        le=_MAX_ELAPSED_SECONDS,
    )
    quiz_misses: int = Field(default=0, ge=0, le=_MAX_COUNTER)


class Milestone(_FrozenSnapshotModel):
    type: MilestoneType
    id: str | None = Field(default=None, max_length=255)
    value: int | None = Field(default=None, ge=0, le=1_000_000_000)
    occurred_within_seconds: int = Field(
        default=0,
        ge=0,
        le=_MAX_ELAPSED_SECONDS,
    )


class SnapshotHistory(_FrozenSnapshotModel):
    problems_today: int = Field(default=0, ge=0, le=_MAX_COUNTER)
    last_completed_at: datetime | None = None
    time_of_day_local: str | None = Field(default=None, max_length=16)
    comeback_gap_hours: float | None = Field(
        default=None,
        ge=0,
        le=_MAX_COMEBACK_HOURS,
    )
    weak_categories: tuple[WeakCategory, ...] = Field(default_factory=tuple, max_length=32)
    recent_milestones: tuple[Milestone, ...] = Field(default_factory=tuple, max_length=5)


class CompanionSnapshot(_FrozenSnapshotModel):
    schema_version: Literal["v1"] = "v1"
    user: SnapshotUser
    location: SnapshotLocation
    activity: SnapshotActivity = Field(default_factory=SnapshotActivity)
    session: SnapshotSession = Field(default_factory=SnapshotSession)
    preferences: SnapshotCompanionPreferences = Field(default_factory=SnapshotCompanionPreferences)
    problem: SnapshotProblem | None = None
    course: SnapshotCourse | None = None
    history: SnapshotHistory = Field(default_factory=SnapshotHistory)
