from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from pack_companions.client import Comment as LegacyModuleComment
from pack_companions import (
    Comment,
    CommentEvent,
    CommentEventType,
    CommentOutcome,
    CommentResult,
    CompanionSnapshot,
    CompanionsAuthError,
    CompanionsClient,
    CompanionsProtocolError,
    CompanionsServiceError,
    CompanionsTransportError,
    ExpressionBehaviorIntent,
    ExpressionIntent,
    ExpressionResponseMode,
    PrivacyErrorDisposition,
    PrivacyOperationError,
    PrivacyOperationErrorCode,
    SemanticExpressionEnvelope,
    SnapshotActivity,
    SnapshotCompanionPreferences,
    SnapshotCourse,
    SnapshotHistory,
    SnapshotLocation,
    SnapshotProblem,
    SnapshotSession,
    SnapshotUser,
)


def test_legacy_client_module_comment_import_is_preserved() -> None:
    assert LegacyModuleComment is Comment


def _snapshot(
    *,
    session_id: UUID | None = None,
    started: bool = False,
    timezone_name: str | None = None,
) -> CompanionSnapshot:
    return CompanionSnapshot(
        user=SnapshotUser(
            id="host-user-1",
            companion_id="puppy",
        ),
        location=SnapshotLocation(
            route="/practice/two-sum",
            route_intent="practice",
        ),
        session=SnapshotSession(
            id=session_id,
            started=started,
            absence_seconds=7200 if started else None,
            timezone=timezone_name,
        ),
    )


def _delivered_response(
    decision_id: UUID,
    *,
    deduplicated: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": "v1",
        "decision_id": str(decision_id),
        "outcome": "delivered",
        "deduplicated": deduplicated,
        "text": "You came back. I was starting to suspect a plot twist.",
        "opportunity_type": "comeback",
        "coalesced_opportunity_types": [],
        "expression": {
            "schema_version": "v1",
            "intent": "greeting",
            "intensity": 0.7,
            "response_mode": "speak_and_act",
        },
        "response_locale": "en",
        "speech_locale": "en-US",
        "next_evaluation_at": "2026-07-23T06:45:00Z",
        "comment": {
            "id": str(decision_id),
            "text": "You came back. I was starting to suspect a plot twist.",
            "opportunity_type": "comeback",
            "was_ai": False,
            "model": None,
        },
        "throttled": False,
        "throttle_mode": "normal",
        "declined": False,
        "reason": None,
    }


def _sdk_traceback_locals(exc: BaseException) -> str:
    """Render only SDK-owned traceback locals for retention assertions."""
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


def test_snapshot_session_serializes_validated_iana_facts() -> None:
    session_id = uuid4()
    snapshot = _snapshot(
        session_id=session_id,
        started=True,
        timezone_name="America/Chicago",
    )

    assert snapshot.session.id == session_id
    assert snapshot.session.absence_seconds == 7200
    assert snapshot.model_dump(mode="json")["session"] == {
        "id": str(session_id),
        "started": True,
        "absence_seconds": 7200,
        "timezone": "America/Chicago",
    }


def test_snapshot_session_rejects_non_iana_timezone() -> None:
    with pytest.raises(ValidationError, match="valid IANA timezone"):
        SnapshotSession(timezone="Definitely/Not_A_Zone")


def test_snapshot_companion_preferences_default_serialization_is_exact() -> None:
    preferences = SnapshotCompanionPreferences()
    snapshot = _snapshot()
    event = CommentEvent.create(snapshot=snapshot, event_type="navigation")

    expected = {
        "ai_personalization_enabled": True,
        "care_reminders_enabled": True,
        "hydration_enabled": True,
        "food_enabled": True,
        "break_reminders_enabled": True,
        "late_night_banter_enabled": True,
        "quiet_hours_active": False,
        "quiet_hours_allow_session_greeting": True,
        "reduced_motion": False,
    }
    assert preferences.model_dump(mode="json") == expected
    assert snapshot.preferences.model_dump(mode="json") == expected
    assert snapshot.model_dump(mode="json")["preferences"] == expected
    assert json.loads(event.request_bytes)["snapshot"]["preferences"] == expected


def test_snapshot_companion_preferences_are_immutable_and_overridable() -> None:
    preferences = SnapshotCompanionPreferences(
        ai_personalization_enabled=False,
        hydration_enabled=False,
        quiet_hours_active=True,
        quiet_hours_allow_session_greeting=False,
        reduced_motion=True,
    )

    assert preferences.model_dump(mode="json") == {
        "ai_personalization_enabled": False,
        "care_reminders_enabled": True,
        "hydration_enabled": False,
        "food_enabled": True,
        "break_reminders_enabled": True,
        "late_night_banter_enabled": True,
        "quiet_hours_active": True,
        "quiet_hours_allow_session_greeting": False,
        "reduced_motion": True,
    }
    with pytest.raises(ValidationError, match="frozen"):
        preferences.reduced_motion = False


def test_snapshot_user_serializes_expected_link_incarnation_id() -> None:
    link_incarnation_id = uuid4()
    user = SnapshotUser(
        id="host-user-1",
        companion_id="puppy",
        expected_link_incarnation_id=link_incarnation_id,
    )

    assert user.expected_link_incarnation_id == link_incarnation_id
    assert user.model_dump(mode="json")["expected_link_incarnation_id"] == str(link_incarnation_id)
    assert (
        SnapshotUser.model_validate(
            {
                "id": "host-user-1",
                "companion_id": "puppy",
                "expected_link_incarnation_id": str(link_incarnation_id),
            }
        ).expected_link_incarnation_id
        == link_incarnation_id
    )

    for invalid in ("not-a-uuid", str(UUID(int=0))):
        with pytest.raises(ValidationError):
            SnapshotUser.model_validate(
                {
                    "id": "host-user-1",
                    "companion_id": "puppy",
                    "expected_link_incarnation_id": invalid,
                }
            )


def test_comment_event_create_keeps_one_stable_id() -> None:
    session_id = uuid4()
    event = CommentEvent.create(
        snapshot=_snapshot(session_id=session_id, started=True),
        event_type="session_started",
    )

    assert isinstance(event.client_event_id, UUID)
    assert event.snapshot.session.id == session_id
    assert event.event_type == "session_started"


def test_comment_event_captures_canonical_bytes_before_nested_mutation() -> None:
    snapshot = _snapshot()
    event = CommentEvent(
        snapshot=snapshot,
        client_event_id=uuid4(),
        event_type="navigation",
    )
    canonical = event.request_bytes

    with pytest.raises(ValidationError, match="frozen"):
        snapshot.location.route = "/mutated-outside-the-event"
    with pytest.raises(ValidationError, match="frozen"):
        event.snapshot.user.display_name = "also mutated"

    assert event.request_bytes == canonical
    decoded = json.loads(canonical)
    assert decoded["snapshot"]["location"]["route"] == "/practice/two-sum"
    assert decoded["snapshot"]["user"]["display_name"] is None
    assert event.model_dump(mode="json") == decoded
    assert canonical.startswith(b'{"client_event_id":')


def test_snapshot_collections_are_deeply_immutable() -> None:
    activity = SnapshotActivity.model_validate({"recent_actions": ["opened_problem"]})
    history = SnapshotHistory.model_validate(
        {
            "weak_categories": ["graphs"],
            "recent_milestones": [],
        }
    )

    assert activity.recent_actions == ("opened_problem",)
    assert history.weak_categories == ("graphs",)
    with pytest.raises(AttributeError):
        activity.recent_actions.append("mutated")  # type: ignore[attr-defined]


def test_comment_event_round_trips_exact_durable_bytes_and_safe_copies() -> None:
    event = CommentEvent(
        snapshot=_snapshot(),
        client_event_id=uuid4(),
        event_type="navigation",
    )

    with pytest.raises(TypeError, match="request bytes are immutable"):
        event._canonical_request_bytes = b'{"tampered":true}'

    restored = CommentEvent.from_request_bytes(event.request_bytes)
    copied = event.model_copy()
    updated = event.model_copy(update={"event_type": "activity_tick"})

    assert restored.request_bytes == event.request_bytes
    assert copied.request_bytes == event.request_bytes
    assert updated.client_event_id == event.client_event_id
    assert updated.event_type == "activity_tick"
    assert updated.request_bytes != event.request_bytes
    assert json.loads(updated.request_bytes)["event_type"] == "activity_tick"


@pytest.mark.parametrize(
    ("event_type", "started"),
    [
        ("session_started", False),
        ("activity_tick", True),
        ("navigation", True),
    ],
)
def test_comment_event_rejects_incoherent_session_fact(
    event_type: CommentEventType,
    started: bool,
) -> None:
    with pytest.raises(ValidationError):
        CommentEvent(
            snapshot=_snapshot(session_id=uuid4(), started=started),
            client_event_id=uuid4(),
            event_type=event_type,
        )


def test_expression_normalizes_unknown_semantics_without_exposing_assets() -> None:
    expression = SemanticExpressionEnvelope.model_validate(
        {
            "intent": "not-a-renderer-state",
            "bubble_intent": "also-unknown",
            "behavior_intent": "open-secret-file",
            "intensity": 0.5,
            "response_mode": "speak_and_act",
        }
    )

    assert expression.intent is ExpressionIntent.NEUTRAL
    assert expression.bubble_intent is ExpressionIntent.NEUTRAL
    assert expression.behavior_intent is None

    with pytest.raises(ValidationError, match="asset_url"):
        SemanticExpressionEnvelope.model_validate(
            {
                "intent": "excited",
                "intensity": 0.8,
                "response_mode": "speak_and_act",
                "asset_url": "https://example.invalid/private.webp",
            }
        )


def test_expression_preserves_controlled_behavior_intent() -> None:
    expression = SemanticExpressionEnvelope(
        intent=ExpressionIntent.CONCERNED,
        behavior_intent=ExpressionBehaviorIntent.DRINK,
        intensity=0.6,
        response_mode=ExpressionResponseMode.SPEAK_AND_ACT,
    )

    assert expression.behavior_intent is ExpressionBehaviorIntent.DRINK


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SnapshotUser(id="", companion_id="puppy"),
        lambda: SnapshotUser(id="u", companion_id="PUPPY"),
        lambda: SnapshotUser(id="u", companion_id="puppy", streak=1_000_001),
        lambda: SnapshotLocation(route=""),
        lambda: SnapshotLocation(route="/" + ("x" * 2048)),
        lambda: SnapshotActivity(idle_seconds=-1),
        lambda: SnapshotActivity.model_validate({"recent_actions": ["x" * 257]}),
        lambda: SnapshotProblem(language="x" * 33),
        lambda: SnapshotProblem(attempt_count=1_000_001),
        lambda: SnapshotCourse(quiz_misses=1_000_001),
        lambda: SnapshotHistory(time_of_day_local="x" * 17),
        lambda: SnapshotHistory.model_validate({"weak_categories": ["category"] * 33}),
        lambda: SnapshotHistory.model_validate({"weak_categories": ["x" * 101]}),
    ],
)
def test_snapshot_mirrors_service_storage_and_prompt_bounds(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValidationError):
        factory()


@pytest.mark.parametrize("mode", ["silent", "act_only"])
def test_silent_expression_modes_cannot_smuggle_speech(mode: str) -> None:
    with pytest.raises(ValidationError, match="cannot carry speech_text"):
        SemanticExpressionEnvelope.model_validate(
            {
                "intent": "neutral",
                "intensity": 0.0,
                "response_mode": mode,
                "speech_text": "fallback speech must not appear",
            }
        )


def test_action_only_and_no_opportunity_remain_deliberately_silent() -> None:
    action = CommentResult(
        decision_id=uuid4(),
        outcome=CommentOutcome.ACTION_ONLY,
        expression=SemanticExpressionEnvelope(
            intent=ExpressionIntent.CONCERNED,
            behavior_intent=ExpressionBehaviorIntent.STRETCH,
            intensity=0.4,
            response_mode=ExpressionResponseMode.ACT_ONLY,
        ),
    )
    no_opportunity = CommentResult(
        decision_id=uuid4(),
        outcome=CommentOutcome.NO_OPPORTUNITY,
        expression=SemanticExpressionEnvelope(
            intent=ExpressionIntent.NEUTRAL,
            intensity=0.0,
            response_mode=ExpressionResponseMode.SILENT,
        ),
    )

    assert action.deliberately_silent is True
    assert action.speaks is False
    assert action.expression is not None
    assert action.expression.response_mode is ExpressionResponseMode.ACT_ONLY
    assert no_opportunity.deliberately_silent is True
    assert no_opportunity.text is None


def test_action_only_requires_act_only_expression() -> None:
    with pytest.raises(ValidationError, match="act_only expression"):
        CommentResult(
            decision_id=uuid4(),
            outcome=CommentOutcome.ACTION_ONLY,
            expression=SemanticExpressionEnvelope(
                intent=ExpressionIntent.CONCERNED,
                intensity=0.4,
                response_mode=ExpressionResponseMode.SILENT,
            ),
        )


@pytest.mark.parametrize(
    ("outcome", "mode", "message"),
    [
        ("delivered", "silent", "speaking expression mode"),
        ("no_opportunity", "speak_and_act", "require silent expression mode"),
    ],
)
def test_outcome_and_expression_speech_modes_must_agree(
    outcome: str,
    mode: str,
    message: str,
) -> None:
    payload = _delivered_response(uuid4())
    payload["outcome"] = outcome
    payload["expression"] = {
        "intent": "neutral",
        "intensity": 0.0,
        "response_mode": mode,
    }
    if outcome != "delivered":
        payload["comment"] = None
        payload["text"] = None

    with pytest.raises(ValidationError, match=message):
        CommentResult.model_validate(payload)


@pytest.mark.parametrize("outcome", ["throttled", "declined", "no_opportunity"])
def test_non_content_outcomes_cannot_smuggle_act_only_mode(outcome: str) -> None:
    with pytest.raises(ValidationError, match="require silent expression mode"):
        CommentResult.model_validate(
            {
                "decision_id": uuid4(),
                "outcome": outcome,
                "expression": {
                    "intent": "neutral",
                    "intensity": 0.0,
                    "response_mode": "act_only",
                },
            }
        )


def test_expression_speech_must_match_screened_top_level_text() -> None:
    payload = _delivered_response(uuid4())
    payload["expression"] = {
        "intent": "greeting",
        "intensity": 0.7,
        "response_mode": "speak_and_act",
        "speech_text": "unscreened alternate copy",
    }

    with pytest.raises(ValidationError, match="must match screened"):
        CommentResult.model_validate(payload)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Comment(
            id=uuid4(),
            text="x" * 241,
            opportunity_type="default",
            was_ai=False,
        ),
        lambda: Comment(
            id=uuid4(),
            text="ok",
            opportunity_type="default",
            was_ai=True,
            model="x" * 51,
        ),
        lambda: CommentResult(
            decision_id=uuid4(),
            outcome=CommentOutcome.NO_OPPORTUNITY,
            coalesced_opportunity_types=["x" * 51],
        ),
        lambda: CommentResult(
            decision_id=uuid4(),
            outcome=CommentOutcome.NO_OPPORTUNITY,
            coalesced_opportunity_types=["../../asset.webp"],
        ),
        lambda: CommentResult(
            decision_id=uuid4(),
            outcome=CommentOutcome.THROTTLED,
            throttle_mode="x" * 17,
        ),
        lambda: CommentResult(
            decision_id=uuid4(),
            outcome=CommentOutcome.DECLINED,
            reason="x" * 65,
        ),
    ],
)
def test_comment_response_fields_mirror_service_bounds(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_legacy_comment_response_is_derived_without_inventing_new_speech() -> None:
    decision_id = uuid4()
    result = CommentResult.model_validate(
        {
            "comment": {
                "id": str(decision_id),
                "text": "Nice recovery.",
                "opportunity_type": "test_pass",
                "was_ai": False,
                "model": None,
            },
            "throttled": False,
        }
    )
    throttled = CommentResult.model_validate(
        {
            "comment": None,
            "throttled": True,
            "throttle_mode": "normal",
        }
    )

    assert result.outcome is CommentOutcome.DELIVERED
    assert result.decision_id == decision_id
    assert result.text == "Nice recovery."
    assert result.speaks is True
    assert throttled.outcome is CommentOutcome.THROTTLED
    assert throttled.text is None
    assert throttled.deliberately_silent is True


def test_durable_decision_id_can_differ_from_delivered_comment_row_id() -> None:
    decision_id = uuid4()
    comment_id = uuid4()
    result = CommentResult.model_validate(
        {
            **_delivered_response(comment_id),
            "decision_id": str(decision_id),
        }
    )

    assert result.decision_id == decision_id
    assert result.comment is not None
    assert result.comment.id == comment_id


def test_locale_pair_must_be_coherent() -> None:
    with pytest.raises(ValidationError, match="Spanish responses"):
        CommentResult(
            decision_id=uuid4(),
            outcome=CommentOutcome.NO_OPPORTUNITY,
            response_locale="es",
            speech_locale="en-US",
        )


def test_full_spanish_coalesced_decision_preserves_every_semantic_field() -> None:
    comment_id = uuid4()
    decision_id = uuid4()
    result = CommentResult.model_validate(
        {
            "schema_version": "v1",
            "decision_id": str(decision_id),
            "outcome": "delivered",
            "deduplicated": False,
            "text": "Volviste tarde. ¿Todo bien?",
            "opportunity_type": "late_night_comeback",
            "coalesced_opportunity_types": ["comeback", "late_night"],
            "expression": {
                "schema_version": "v1",
                "intent": "greeting",
                "intensity": 0.65,
                "response_mode": "speak_and_act",
                "bubble_intent": "concerned",
                "behavior_intent": "tired",
            },
            "response_locale": "es",
            "speech_locale": "es-MX",
            "next_evaluation_at": "2026-07-23T07:15:00Z",
            "comment": {
                "id": str(comment_id),
                "text": "Volviste tarde. ¿Todo bien?",
                "opportunity_type": "late_night_comeback",
                "was_ai": False,
                "model": None,
            },
            "throttled": False,
            "declined": False,
        }
    )

    assert result.decision_id == decision_id
    assert result.response_locale == "es"
    assert result.speech_locale == "es-MX"
    assert result.coalesced_opportunity_types == ["comeback", "late_night"]
    assert result.expression is not None
    assert result.expression.bubble_intent is ExpressionIntent.CONCERNED
    assert result.expression.behavior_intent is ExpressionBehaviorIntent.TIRED


@pytest.mark.asyncio
async def test_typed_comment_retry_reuses_exact_event_id_and_body() -> None:
    event_id = uuid4()
    decision_id = uuid4()
    event = CommentEvent(
        snapshot=_snapshot(),
        client_event_id=event_id,
        event_type="learning_event",
    )
    bodies: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(await request.aread())
        if len(bodies) == 1:
            raise httpx.ConnectError("ambiguous disconnect", request=request)
        return httpx.Response(
            200,
            json=_delivered_response(decision_id, deduplicated=True),
        )

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )
    result = await client.get_comment_event(event)

    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
    assert json.loads(bodies[0])["client_event_id"] == str(event_id)
    assert result.decision_id == decision_id
    assert result.deduplicated is True
    assert result.next_evaluation_at == datetime(
        2026,
        7,
        23,
        6,
        45,
        tzinfo=timezone.utc,
    )


@pytest.mark.asyncio
async def test_separate_typed_calls_reuse_captured_bytes_after_nested_mutation() -> None:
    decision_id = uuid4()
    event = CommentEvent(
        snapshot=_snapshot(),
        client_event_id=uuid4(),
        event_type="navigation",
    )
    bodies: list[bytes] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(await request.aread())
        return httpx.Response(200, json=_delivered_response(decision_id))

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )
    await client.get_comment_event(event)
    with pytest.raises(ValidationError, match="frozen"):
        event.snapshot.location.route = "/changed-between-caller-retries"
    await client.get_comment_event(event)

    assert bodies[0] == bodies[1] == event.request_bytes
    assert json.loads(bodies[1])["snapshot"]["location"]["route"] == ("/practice/two-sum")


@pytest.mark.asyncio
async def test_typed_comment_retries_bounded_service_unavailability() -> None:
    decision_id = uuid4()
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503, json={"detail": "try the same event again"})
        return httpx.Response(200, json=_delivered_response(decision_id))

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )
    result = await client.get_comment_event(
        CommentEvent(
            snapshot=_snapshot(),
            client_event_id=uuid4(),
            event_type="activity_tick",
        )
    )

    assert result.decision_id == decision_id
    assert requests == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "stale_account_incarnation",
        "stale_privacy_generation",
    ],
)
async def test_typed_comment_preserves_only_bounded_stale_machine_code(
    code: str,
) -> None:
    upstream_marker = "comment-upstream-body-marker"
    snapshot_marker = "comment-private-snapshot-marker"
    api_key = "comment-api-key-marker"
    secret = "comment-signing-secret-marker"
    event = CommentEvent(
        snapshot=CompanionSnapshot(
            user=SnapshotUser(
                id="host-user-1",
                companion_id="puppy",
                display_name=snapshot_marker,
            ),
            location=SnapshotLocation(
                route="/practice/two-sum",
                route_intent="practice",
            ),
        ),
        client_event_id=uuid4(),
        event_type="navigation",
    )
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": code,
                    "message": upstream_marker,
                    "untrusted_extra": {"payload": snapshot_marker},
                }
            },
        )

    client = CompanionsClient(
        api_key=api_key,
        secret=secret,
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PrivacyOperationError) as raised:
        await client.get_comment_event(event)

    assert calls == 1
    assert raised.value.status_code == 409
    assert raised.value.code is PrivacyOperationErrorCode(code)
    assert raised.value.disposition is PrivacyErrorDisposition.DISCARD_EVENT
    assert raised.value.terminal is True
    assert raised.value.retry_same_event is False
    assert upstream_marker not in str(raised.value)
    assert snapshot_marker not in str(raised.value)
    retained = _sdk_traceback_locals(raised.value)
    assert upstream_marker not in retained
    assert snapshot_marker not in retained
    assert api_key not in retained
    assert secret not in retained


@pytest.mark.asyncio
async def test_typed_comment_does_not_reflect_unknown_error_code() -> None:
    unknown_code = "future_code_with_private_suffix"
    upstream_marker = "unknown-comment-error-body-marker"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": unknown_code,
                    "message": upstream_marker,
                }
            },
        )

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PrivacyOperationError) as raised:
        await client.get_comment_event(
            CommentEvent(
                snapshot=_snapshot(),
                client_event_id=uuid4(),
                event_type="navigation",
            )
        )

    assert raised.value.code is None
    assert raised.value.disposition is PrivacyErrorDisposition.DISCARD_EVENT
    assert unknown_code not in str(raised.value)
    assert upstream_marker not in str(raised.value)
    retained = _sdk_traceback_locals(raised.value)
    assert unknown_code not in retained
    assert upstream_marker not in retained


@pytest.mark.asyncio
async def test_convenience_method_requires_both_typed_event_fields() -> None:
    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
    )

    with pytest.raises(ValueError, match="must be supplied together"):
        await client.get_comment(_snapshot(), event_type="activity_tick")

    with pytest.raises(ValueError, match="must be supplied together"):
        await client.get_comment(_snapshot(), client_event_id=uuid4())


@pytest.mark.parametrize("attempts", [0, 4])
def test_comment_retry_count_is_bounded(attempts: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 3"):
        CompanionsClient(
            api_key="public-app-key",
            secret="test-hmac-secret",
            comment_max_attempts=attempts,
        )


@pytest.mark.asyncio
async def test_request_body_is_rejected_before_transport_when_over_cap() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )
    client.MAX_REQUEST_BYTES = 16

    with pytest.raises(CompanionsProtocolError, match="request body exceeded"):
        await client.get_comment_event(
            CommentEvent(
                snapshot=_snapshot(),
                client_event_id=uuid4(),
                event_type="navigation",
            )
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_legacy_comment_path_does_not_retry_ambiguous_transport_failure() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("ambiguous disconnect", request=request)

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsTransportError) as exc_info:
        await client.get_legacy_comment(_snapshot())
    assert calls == 1
    assert not hasattr(exc_info.value, "request")
    assert not hasattr(exc_info.value, "response")
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


@pytest.mark.asyncio
async def test_typed_path_rejects_legacy_shaped_response() -> None:
    decision_id = uuid4()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "comment": {
                    "id": str(decision_id),
                    "text": "Old response.",
                    "opportunity_type": "default",
                    "was_ai": False,
                    "model": None,
                },
                "throttled": False,
            },
        )

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsProtocolError, match="missing required fields"):
        await client.get_comment_event(
            CommentEvent(
                snapshot=_snapshot(),
                client_event_id=uuid4(),
                event_type="learning_event",
            )
        )


@pytest.mark.asyncio
async def test_auth_exception_never_echoes_upstream_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            text="debug detail containing server-only configuration",
        )

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsAuthError) as exc_info:
        await client.ping()
    assert "server-only configuration" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_protocol_validation_error_does_not_retain_rejected_values() -> None:
    rejected = "DO-NOT-REFLECT-THIS-UPSTREAM-VALUE"
    payload = _delivered_response(uuid4())
    payload["expression"] = {
        "intent": "greeting",
        "intensity": 0.7,
        "response_mode": "silent",
        "speech_text": rejected,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsProtocolError) as exc_info:
        await client.get_comment_event(
            CommentEvent(
                snapshot=_snapshot(),
                client_event_id=uuid4(),
                event_type="navigation",
            )
        )
    assert rejected not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert rejected not in _sdk_traceback_locals(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "service", "auth"])
async def test_public_http_failures_retain_no_signed_request_or_snapshot(
    failure: str,
) -> None:
    marker = "PRIVATE-HOST-MARKER"

    async def handler(request: httpx.Request) -> httpx.Response:
        if failure == "transport":
            raise httpx.ReadError("wire failed", request=request)
        if failure == "auth":
            return httpx.Response(401, request=request)
        return httpx.Response(503, request=request)

    client = CompanionsClient(
        api_key="private-app-key",
        secret="private-hmac-secret",
        service_url="https://brain.example.test",
        comment_max_attempts=1,
        transport=httpx.MockTransport(handler),
    )
    event = CommentEvent(
        snapshot=CompanionSnapshot(
            user=SnapshotUser(id=marker, companion_id="puppy"),
            location=SnapshotLocation(route="/private"),
        ),
        client_event_id=uuid4(),
        event_type="navigation",
    )
    expected_error = {
        "transport": CompanionsTransportError,
        "service": CompanionsServiceError,
        "auth": CompanionsAuthError,
    }[failure]

    with pytest.raises(expected_error) as exc_info:
        await client.get_comment_event(event)

    error = exc_info.value
    assert not hasattr(error, "request")
    assert not hasattr(error, "response")
    assert error.__cause__ is None
    assert error.__context__ is None
    traceback_locals = _sdk_traceback_locals(error)
    assert marker not in traceback_locals
    assert "private-app-key" not in traceback_locals
    assert "private-hmac-secret" not in traceback_locals


@pytest.mark.asyncio
async def test_non_json_protocol_error_scrubs_wire_bytes_from_traceback() -> None:
    marker = "PRIVATE-UPSTREAM-BODY-MARKER"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=marker.encode(), request=request)

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsProtocolError) as exc_info:
        await client.ping()

    assert marker not in str(exc_info.value)
    assert marker not in _sdk_traceback_locals(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


class _RecordingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_success_response_stream_stops_at_decoded_byte_cap() -> None:
    stream = _RecordingStream([b"x" * 16, b"y" * 16, b"secret-third-chunk"])

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=stream)

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )
    client.MAX_RESPONSE_BYTES = 24
    client.RESPONSE_CHUNK_BYTES = 8

    with pytest.raises(CompanionsProtocolError, match="size limit"):
        await client.ping()
    assert stream.yielded == 2
    assert stream.closed is True


@pytest.mark.asyncio
async def test_unsolicited_compressed_success_is_rejected_before_decoding() -> None:
    compressed = gzip.compress(b'{"value":"' + (b"x" * 4096) + b'"}')
    stream = _RecordingStream([compressed])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=stream,
        )

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CompanionsProtocolError, match="encoded response"):
        await client.ping()
    assert stream.yielded == 0
    assert stream.closed is True


@pytest.mark.asyncio
async def test_http_error_body_is_closed_without_being_consumed() -> None:
    stream = _RecordingStream([b"private upstream diagnostics"])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, stream=stream)

    client = CompanionsClient(
        api_key="public-app-key",
        secret="test-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsServiceError) as exc_info:
        await client.ping()
    assert exc_info.value.status_code == 500
    assert "private upstream diagnostics" not in str(exc_info.value)
    assert not hasattr(exc_info.value, "request")
    assert not hasattr(exc_info.value, "response")
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert stream.yielded == 0
    assert stream.closed is True
