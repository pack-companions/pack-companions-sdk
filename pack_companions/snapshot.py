"""CompanionSnapshot — mirror of the service's snapshot contract.

This file MUST stay in sync with:
- service: app/schemas/snapshot.py
- JS SDK: src/snapshot.ts

Phase 0b locked the v1 shape; new fields land as optional with safe
defaults so existing senders keep working.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

UserTier = Literal["free", "plus", "pro"]
RouteIntent = Literal["practice", "course", "browse", "social", "settings", "unknown"]
ProblemStatus = Literal["not_started", "in_progress", "completed", "abandoned"]
TestResult = Literal["pass", "fail", "error", "none"]
MilestoneType = Literal[
    "first_solve",
    "level_up",
    "achievement_unlocked",
    "daily_challenge_completed",
]


class SnapshotUser(BaseModel):
    id: str
    tier: UserTier = "free"
    companion_id: str
    streak: int = 0
    display_name: str | None = None
    # Phase H — optional cross-app identity signal. sha256 hex digest of
    # the user's normalized email (email.strip().lower()). When present,
    # the service uses it as the canonical global_user_id so companion
    # memory follows the user across apps. Use pack_companions.hash_email()
    # to produce it correctly. Plaintext email never crosses the wire.
    email_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )


class SnapshotLocation(BaseModel):
    route: str
    section: str | None = None
    sub_section: str | None = None
    route_intent: RouteIntent = "unknown"


class SnapshotActivity(BaseModel):
    idle_seconds: int = 0
    time_on_page_seconds: int = 0
    session_seconds: int = 0
    last_action: str | None = None
    recent_actions: list[str] = Field(default_factory=list, max_length=10)


class SnapshotProblem(BaseModel):
    id: str | None = None
    language: str | None = None
    status: ProblemStatus | None = None
    time_in_editor_seconds: int = 0
    edits_since_run: int = 0
    last_test_result: TestResult = "none"
    last_error_type: str | None = None
    attempt_count: int = 0
    hint_used: bool = False
    pattern_first_touch: bool = False
    followup_available: bool = False


class SnapshotCourse(BaseModel):
    course_id: str | None = None
    lesson_id: str | None = None
    time_on_lesson_seconds: int = 0
    quiz_misses: int = 0


class Milestone(BaseModel):
    type: MilestoneType
    id: str | None = None
    value: int | None = None
    occurred_within_seconds: int = 0


class SnapshotHistory(BaseModel):
    problems_today: int = 0
    last_completed_at: datetime | None = None
    time_of_day_local: str | None = None
    comeback_gap_hours: float | None = None
    weak_categories: list[str] = Field(default_factory=list)
    recent_milestones: list[Milestone] = Field(default_factory=list, max_length=5)


class CompanionSnapshot(BaseModel):
    schema_version: Literal["v1"] = "v1"
    user: SnapshotUser
    location: SnapshotLocation
    activity: SnapshotActivity = Field(default_factory=SnapshotActivity)
    problem: SnapshotProblem | None = None
    course: SnapshotCourse | None = None
    history: SnapshotHistory = Field(default_factory=SnapshotHistory)
