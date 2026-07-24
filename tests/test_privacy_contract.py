from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from pack_companions import (
    AccountLinkState,
    CompanionsClient,
    CompanionsProtocolError,
    CompanionsTransportError,
    EraseForAppEvent,
    EraseForAppResult,
    IdentifyRequest,
    IdentifyResponse,
    LinkStatusEvent,
    LinkStatusResult,
    MemoryDeleteChatHistoryEvent,
    MemoryDeleteChatHistoryResult,
    MemoryExcludeEvent,
    MemoryExcludeResult,
    MemoryForgetEvent,
    MemoryForgetResult,
    ObservationDeleteEvent,
    ObservationDeleteResult,
    PerceptionConsentEvent,
    PerceptionConsentResult,
    PrivacyErrorDisposition,
    PrivacyOperationError,
    PrivacyOperationErrorCode,
    PrivacySnapshotUser,
    ShareMemoryEvent,
    ShareMemoryResult,
    SnapshotUser,
)


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


def _state(
    *,
    incarnation: UUID | None = None,
    generation: int = 4,
) -> AccountLinkState:
    return AccountLinkState(
        link_incarnation_id=incarnation or uuid4(),
        privacy_generation=generation,
    )


def _privacy_user(state: AccountLinkState | None = None) -> PrivacySnapshotUser:
    return (state or _state()).snapshot_user(
        host_user_id="host-user-1",
        tier="pro",
        identity_subject="boop.v1.0123456789abcdef0123456789abcdef",
    )


def test_snapshot_user_serializes_strict_expected_privacy_generation() -> None:
    incarnation = uuid4()
    user = SnapshotUser(
        id="host-user-1",
        companion_id="puppy",
        expected_link_incarnation_id=incarnation,
        expected_privacy_generation=9,
    )

    assert user.model_dump(mode="json")["expected_privacy_generation"] == 9
    for invalid in (-1, 1.5, "9", True):
        with pytest.raises(ValidationError):
            SnapshotUser(
                id="host-user-1",
                companion_id="puppy",
                expected_link_incarnation_id=incarnation,
                expected_privacy_generation=invalid,  # type: ignore[arg-type]
            )


def test_identify_response_exposes_atomic_account_link_state() -> None:
    incarnation = uuid4()
    response = IdentifyResponse(
        global_user_id="pcu_abcdefghijklmnopqrstuvwx23",
        is_new=True,
        link_incarnation_id=incarnation,
        privacy_generation=7,
        linked_keys=[],
    )

    assert response.account_link_state == AccountLinkState(
        link_incarnation_id=incarnation,
        privacy_generation=7,
    )


def test_account_link_state_is_max_only_within_incarnation_and_resets_on_new_one() -> None:
    first_incarnation = uuid4()
    state = _state(incarnation=first_incarnation, generation=8)
    stale = IdentifyResponse(
        global_user_id="pcu_abcdefghijklmnopqrstuvwx23",
        is_new=False,
        link_incarnation_id=first_incarnation,
        privacy_generation=3,
        linked_keys=[],
    )
    newer = stale.model_copy(update={"privacy_generation": 11})
    replacement = stale.model_copy(
        update={
            "link_incarnation_id": uuid4(),
            "privacy_generation": 0,
        }
    )

    assert state.observe_generation(3) is state
    assert state.reconcile_identify(stale) is state
    assert state.reconcile_identify(newer).privacy_generation == 11
    replaced = state.reconcile_identify(replacement)
    assert replaced.link_incarnation_id == replacement.link_incarnation_id
    assert replaced.privacy_generation == 0


def test_account_link_state_builds_minimal_generation_bound_snapshot_user() -> None:
    state = _state(generation=12)

    user = state.snapshot_user(host_user_id="host-user-1", tier="mobile")

    assert user.model_dump(mode="json") == {
        "id": "host-user-1",
        "tier": "mobile",
        "email_hash": None,
        "identity_subject": None,
        "expected_link_incarnation_id": str(state.link_incarnation_id),
        "expected_privacy_generation": 12,
    }


def test_erasure_event_captures_exact_incarnation_and_stable_event_id() -> None:
    state = _state(generation=12)
    event_id = uuid4()
    event = EraseForAppEvent.create_for_state(
        user_id="host-user-1",
        state=state,
        client_erasure_event_id=event_id,
    )

    assert json.loads(event.request_bytes) == {
        "client_erasure_event_id": str(event_id),
        "expected_link_incarnation_id": str(state.link_incarnation_id),
        "user_id": "host-user-1",
    }
    restored = EraseForAppEvent.from_request_bytes(event.request_bytes)
    assert restored.client_erasure_event_id == event_id
    assert restored.expected_link_incarnation_id == state.link_incarnation_id
    assert restored.request_bytes == event.request_bytes


def test_never_linked_erasure_requires_explicit_null_incarnation() -> None:
    event = EraseForAppEvent.create(
        user_id="host-user-never-linked",
        expected_link_incarnation_id=None,
    )

    wire = json.loads(event.request_bytes)
    assert wire["expected_link_incarnation_id"] is None
    assert UUID(wire["client_erasure_event_id"]) == event.client_erasure_event_id

    with pytest.raises(ValidationError, match="expected_link_incarnation_id"):
        EraseForAppEvent.model_validate(
            {
                "user_id": "host-user-never-linked",
                "client_erasure_event_id": uuid4(),
            }
        )


def test_erasure_event_cannot_reuse_its_id_with_a_changed_payload() -> None:
    event = EraseForAppEvent.create(
        user_id="host-user-1",
        expected_link_incarnation_id=None,
    )

    with pytest.raises(TypeError, match="cannot be updated"):
        event.model_copy(update={"user_id": "host-user-2"})
    assert event.model_copy().request_bytes == event.request_bytes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_erasure_event_id", UUID(int=0)),
        ("expected_link_incarnation_id", UUID(int=0)),
    ],
)
def test_erasure_event_rejects_nil_uuid_fields(
    field: str,
    value: UUID,
) -> None:
    payload: dict[str, object] = {
        "user_id": "host-user-1",
        "client_erasure_event_id": uuid4(),
        "expected_link_incarnation_id": uuid4(),
    }
    payload[field] = value

    with pytest.raises(ValidationError, match="nil UUID"):
        EraseForAppEvent.model_validate(payload)


def test_erasure_result_exposes_opaque_non_nil_receipt_capability() -> None:
    erasure_id = uuid4()
    result = EraseForAppResult(
        erased=False,
        identity_deleted=False,
        erasure_id=erasure_id,
    )

    assert result.erasure_id == erasure_id
    with pytest.raises(ValidationError, match="nil UUID"):
        EraseForAppResult(
            erased=True,
            identity_deleted=True,
            erasure_id=UUID(int=0),
        )


def _all_events() -> list[tuple[type[Any], Any, str]]:
    user = _privacy_user()
    state = AccountLinkState(
        link_incarnation_id=user.expected_link_incarnation_id,
        privacy_generation=user.expected_privacy_generation,
    )
    return [
        (
            MemoryExcludeEvent,
            MemoryExcludeEvent.create(
                snapshot_user=user,
                companion_id="puppy",
                fact_key="favorite_music",
                fact_value="jazz",
            ),
            "/v1/memory/exclude",
        ),
        (
            MemoryForgetEvent,
            MemoryForgetEvent.create(
                snapshot_user=user,
                companion_id="puppy",
            ),
            "/v1/memory/forget",
        ),
        (
            MemoryDeleteChatHistoryEvent,
            MemoryDeleteChatHistoryEvent.create(snapshot_user=user),
            "/v1/memory/delete-chat-history",
        ),
        (
            ObservationDeleteEvent,
            ObservationDeleteEvent.create(
                snapshot_user=user,
                companion_id="puppy",
                observation_type="night_owl",
            ),
            "/v1/memory/observations/delete",
        ),
        (
            ShareMemoryEvent,
            ShareMemoryEvent.create(snapshot_user=user, value=False),
            "/v1/user-settings/share-memory-across-apps/set",
        ),
        (
            PerceptionConsentEvent,
            PerceptionConsentEvent.create(
                snapshot_user=user,
                modality="camera",
                granted=False,
            ),
            "/v1/companion/perception/consent",
        ),
        (
            LinkStatusEvent,
            LinkStatusEvent.create_for_state(
                host_user_id="host-user-1",
                state=state,
                status="suspended",
            ),
            "/v1/identity/link-status",
        ),
    ]


@pytest.mark.parametrize(
    ("event_type", "event", "expected_path"),
    _all_events(),
)
def test_all_seven_privacy_events_capture_tokens_id_and_exact_durable_bytes(
    event_type: type[Any],
    event: Any,
    expected_path: str,
) -> None:
    wire = json.loads(event.request_bytes)

    assert event.PATH == expected_path
    assert UUID(wire["client_privacy_event_id"]) == event.client_privacy_event_id
    if "snapshot_user" in wire:
        assert wire["snapshot_user"]["expected_link_incarnation_id"]
        assert wire["snapshot_user"]["expected_privacy_generation"] == 4
    else:
        assert wire["expected_link_incarnation_id"]
        assert wire["expected_privacy_generation"] == 4

    restored = event_type.from_request_bytes(event.request_bytes)
    assert restored.client_privacy_event_id == event.client_privacy_event_id
    assert restored.request_bytes == event.request_bytes
    assert restored.request_bytes is restored.request_bytes


def test_privacy_event_copy_cannot_split_stable_id_from_payload() -> None:
    event = ShareMemoryEvent.create(
        snapshot_user=_privacy_user(),
        value=False,
    )
    with pytest.raises(TypeError, match="cannot be updated"):
        event.model_copy(update={"value": True})
    assert event.model_copy().request_bytes == event.request_bytes
    with pytest.raises(TypeError, match="request bytes are immutable"):
        event._canonical_request_bytes = b'{"tampered":true}'


def test_privacy_events_require_non_nil_stable_event_ids() -> None:
    with pytest.raises(ValidationError, match="nil UUID"):
        MemoryForgetEvent(
            snapshot_user=_privacy_user(),
            companion_id="puppy",
            client_privacy_event_id=UUID(int=0),
        )


@pytest.mark.asyncio
async def test_identify_returns_generation_and_uses_one_exact_request() -> None:
    incarnation = uuid4()
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        return httpx.Response(
            200,
            json={
                "global_user_id": "pcu_abcdefghijklmnopqrstuvwx23",
                "is_new": False,
                "link_incarnation_id": str(incarnation),
                "privacy_generation": 13,
                "linked_keys": [],
            },
        )

    identify = IdentifyRequest(
        host_user_id="host-user-1",
        expected_link_incarnation_id=incarnation,
    )
    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        transport=httpx.MockTransport(handler),
    )

    result = await client.identify(identify)

    assert result.privacy_generation == 13
    assert seen == [("/v1/identity/identify", identify.request_bytes)]
    assert "expected_privacy_generation" not in json.loads(seen[0][1])


@pytest.mark.asyncio
async def test_erase_for_app_sends_exact_bytes_once_and_never_identifies() -> None:
    event = EraseForAppEvent.create_for_state(
        user_id="host-user-1",
        state=_state(),
    )
    erasure_id = uuid4()
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        return httpx.Response(
            200,
            json={
                "erased": True,
                "identity_deleted": False,
                "erasure_id": str(erasure_id),
            },
        )

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        privacy_max_attempts=2,
        transport=httpx.MockTransport(handler),
    )

    result = await client.erase_for_app(event)

    assert result == EraseForAppResult(
        erased=True,
        identity_deleted=False,
        erasure_id=erasure_id,
    )
    assert seen == [
        ("/v1/identity/erase-for-app", event.request_bytes),
    ]
    assert all(path != "/v1/identity/identify" for path, _body in seen)
    assert {json.loads(body)["client_erasure_event_id"] for _path, body in seen} == {
        str(event.client_erasure_event_id)
    }


@pytest.mark.asyncio
async def test_erase_for_app_never_auto_retries_an_ambiguous_failure() -> None:
    event = EraseForAppEvent.create_for_state(
        user_id="host-user-1",
        state=_state(),
    )
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(503, json={"detail": "retry later"})

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        # Even a larger privacy retry setting never applies to ID-4 erasure.
        privacy_max_attempts=3,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PrivacyOperationError) as raised:
        await client.erase_for_app(event)

    assert raised.value.status_code == 503
    assert raised.value.disposition is PrivacyErrorDisposition.RETRY_SAME_EVENT
    assert seen == [event.request_bytes]
    restored = EraseForAppEvent.from_request_bytes(event.request_bytes)
    assert restored.request_bytes == event.request_bytes
    assert restored.client_erasure_event_id == event.client_erasure_event_id


@pytest.mark.asyncio
async def test_erase_for_app_attempts_an_ambiguous_transport_failure_once() -> None:
    event = EraseForAppEvent.create_for_state(
        user_id="host-user-1",
        state=_state(),
    )
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection failed", request=request)

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        privacy_max_attempts=3,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsTransportError):
        await client.erase_for_app(event)

    assert attempts == 1


@pytest.mark.asyncio
async def test_erase_for_app_rejects_invalid_receipt_without_retaining_it() -> None:
    private_marker = "private-user-marker"
    event = EraseForAppEvent.create(
        user_id=private_marker,
        expected_link_incarnation_id=None,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "erased": True,
                "identity_deleted": True,
                "erasure_id": str(UUID(int=0)),
                "unexpected": private_marker,
            },
        )

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsProtocolError) as raised:
        await client.erase_for_app(event)

    assert private_marker not in _sdk_traceback_locals(raised.value)


def _operation_cases() -> list[
    tuple[
        str,
        Callable[[], Any],
        type[Any],
        dict[str, Any],
        str,
    ]
]:
    return [
        (
            "exclude_memory",
            lambda: MemoryExcludeEvent.create(
                snapshot_user=_privacy_user(),
                companion_id="puppy",
                fact_key="favorite_music",
                fact_value="jazz",
            ),
            MemoryExcludeResult,
            {
                "scrubbed_summary_rows": 2,
                "exclusion_recorded": True,
                "privacy_generation": 5,
            },
            "/v1/memory/exclude",
        ),
        (
            "forget_memory",
            lambda: MemoryForgetEvent.create(
                snapshot_user=_privacy_user(),
                companion_id="puppy",
            ),
            MemoryForgetResult,
            {
                "deleted_summaries": 1,
                "deleted_exclusions": 2,
                "deleted_observations": 3,
                "deleted_state": 4,
                "privacy_generation": 5,
            },
            "/v1/memory/forget",
        ),
        (
            "delete_chat_history",
            lambda: MemoryDeleteChatHistoryEvent.create(snapshot_user=_privacy_user()),
            MemoryDeleteChatHistoryResult,
            {
                "deleted_messages": 1,
                "deleted_summaries": 2,
                "deleted_usage": 3,
                "deleted_observations": 4,
                "deleted_exclusions": 5,
                "deleted_state": 6,
                "privacy_generation": 5,
            },
            "/v1/memory/delete-chat-history",
        ),
        (
            "delete_observation",
            lambda: ObservationDeleteEvent.create(
                snapshot_user=_privacy_user(),
                companion_id="puppy",
                observation_type="night_owl",
            ),
            ObservationDeleteResult,
            {
                "deleted": True,
                "exclusion_created": False,
                "observation_type": "night_owl",
                "privacy_generation": 5,
            },
            "/v1/memory/observations/delete",
        ),
        (
            "set_share_memory_across_apps",
            lambda: ShareMemoryEvent.create(
                snapshot_user=_privacy_user(),
                value=False,
            ),
            ShareMemoryResult,
            {
                "share_memory_across_apps": False,
                "is_default": False,
                "privacy_generation": 5,
            },
            "/v1/user-settings/share-memory-across-apps/set",
        ),
        (
            "set_perception_consent",
            lambda: PerceptionConsentEvent.create(
                snapshot_user=_privacy_user(),
                modality="camera",
                granted=False,
            ),
            PerceptionConsentResult,
            {
                "modality": "camera",
                "granted": False,
                "privacy_generation": 5,
            },
            "/v1/companion/perception/consent",
        ),
        (
            "set_link_status",
            lambda: LinkStatusEvent.create_for_state(
                host_user_id="host-user-1",
                state=_state(),
                status="suspended",
            ),
            LinkStatusResult,
            {
                "host_user_id": "host-user-1",
                "status": "suspended",
                "privacy_generation": 5,
            },
            "/v1/identity/link-status",
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "event_factory", "result_type", "response", "path"),
    _operation_cases(),
)
async def test_all_seven_client_mutation_methods_preserve_wire_contract(
    method_name: str,
    event_factory: Callable[[], Any],
    result_type: type[Any],
    response: dict[str, Any],
    path: str,
) -> None:
    event = event_factory()
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        return httpx.Response(200, json=response)

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        transport=httpx.MockTransport(handler),
    )
    result = await getattr(client, method_name)(event)

    assert isinstance(result, result_type)
    assert result.privacy_generation == 5
    assert seen == [(path, event.request_bytes)]


@pytest.mark.asyncio
async def test_ambiguous_privacy_retry_reuses_exact_event_bytes_and_id() -> None:
    event = ShareMemoryEvent.create(
        snapshot_user=_privacy_user(),
        value=False,
    )
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        if len(seen) == 1:
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        return httpx.Response(
            200,
            json={
                "share_memory_across_apps": False,
                "is_default": False,
                "privacy_generation": 5,
            },
        )

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        privacy_max_attempts=2,
        transport=httpx.MockTransport(handler),
    )

    await client.set_share_memory_across_apps(event)

    assert seen == [event.request_bytes, event.request_bytes]
    assert {json.loads(body)["client_privacy_event_id"] for body in seen} == {
        str(event.client_privacy_event_id)
    }


_ERROR_CASES = [
    (
        "privacy_operation_in_progress",
        PrivacyErrorDisposition.RETRY_SAME_EVENT,
    ),
    (
        "privacy_operation_limit_exceeded",
        PrivacyErrorDisposition.RETRY_SAME_EVENT,
    ),
    (
        "privacy_receipt_key_unavailable",
        PrivacyErrorDisposition.RETRY_SAME_EVENT,
    ),
    (
        "privacy_receipt_key_unattested",
        PrivacyErrorDisposition.RETRY_SAME_EVENT,
    ),
    (
        "privacy_generation_inconsistent",
        PrivacyErrorDisposition.RETRY_SAME_EVENT,
    ),
    (
        "stale_account_incarnation",
        PrivacyErrorDisposition.DISCARD_EVENT,
    ),
    (
        "stale_privacy_generation",
        PrivacyErrorDisposition.DISCARD_EVENT,
    ),
    (
        "privacy_event_conflict",
        PrivacyErrorDisposition.DISCARD_EVENT,
    ),
    (
        "privacy_operation_superseded",
        PrivacyErrorDisposition.START_NEW_READ,
    ),
    (
        "privacy_receipt_expired",
        PrivacyErrorDisposition.START_NEW_READ,
    ),
    (
        "privacy_receipt_schema_unsupported",
        PrivacyErrorDisposition.START_NEW_READ,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "disposition"), _ERROR_CASES)
async def test_privacy_errors_are_sanitized_and_actionably_classified(
    code: str,
    disposition: PrivacyErrorDisposition,
) -> None:
    secret_marker = "raw-private-fact-that-must-not-escape"
    event = ShareMemoryEvent.create(
        snapshot_user=_privacy_user(),
        value=False,
    )
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        status_code = 409
        if code == "privacy_operation_limit_exceeded":
            status_code = 429
        elif code in {
            "privacy_receipt_key_unavailable",
            "privacy_receipt_key_unattested",
        }:
            status_code = 503
        return httpx.Response(
            status_code,
            headers=({"Retry-After": "1"} if code == "privacy_operation_in_progress" else {}),
            json={
                "detail": {
                    "code": code,
                    "operation_completed": code
                    in {
                        "privacy_operation_superseded",
                        "privacy_receipt_expired",
                    },
                    "message": secret_marker,
                }
            },
        )

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        privacy_max_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PrivacyOperationError) as raised:
        await client.set_share_memory_across_apps(event)

    assert raised.value.code is PrivacyOperationErrorCode(code)
    assert raised.value.disposition is disposition
    assert raised.value.retry_same_event is (
        disposition is PrivacyErrorDisposition.RETRY_SAME_EVENT
    )
    assert raised.value.terminal is (disposition is not PrivacyErrorDisposition.RETRY_SAME_EVENT)
    assert raised.value.retry_after_seconds == (
        1 if code == "privacy_operation_in_progress" else None
    )
    assert secret_marker not in str(raised.value)
    assert seen_paths == ["/v1/user-settings/share-memory-across-apps/set"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header_value", "expected"),
    [
        ("0", 0),
        ("15", 15),
        ("3600", 3600),
        ("3601", None),
        ("999999999999999999999", None),
        ("Wed, 21 Oct 2015 07:28:00 GMT", None),
        ("1; raw-secret-marker", None),
        ("-1", None),
        (" 1 ", None),
    ],
)
async def test_retry_after_exposes_only_bounded_delta_seconds(
    header_value: str,
    expected: int | None,
) -> None:
    event = ShareMemoryEvent.create(
        snapshot_user=_privacy_user(),
        value=False,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={"Retry-After": header_value},
            json={
                "detail": {
                    "code": "privacy_operation_in_progress",
                    "message": "not retained",
                }
            },
        )

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        privacy_max_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PrivacyOperationError) as raised:
        await client.set_share_memory_across_apps(event)

    assert raised.value.retry_after_seconds == expected
    if "raw-secret-marker" in header_value:
        assert "raw-secret-marker" not in _sdk_traceback_locals(raised.value)


def test_retry_after_parser_rejects_non_ascii_digits() -> None:
    assert CompanionsClient._parse_retry_after_seconds("١") is None


@pytest.mark.asyncio
async def test_unknown_privacy_error_stays_sanitized_and_fails_closed() -> None:
    secret_marker = "do-not-reflect-this-body"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "future_unknown_code",
                    "message": secret_marker,
                }
            },
        )

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        privacy_max_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PrivacyOperationError) as raised:
        await client.forget_memory(
            MemoryForgetEvent.create(
                snapshot_user=_privacy_user(),
                companion_id="puppy",
            )
        )

    assert raised.value.code is None
    assert raised.value.disposition is PrivacyErrorDisposition.DISCARD_EVENT
    assert secret_marker not in str(raised.value)


@pytest.mark.asyncio
async def test_erased_account_status_is_terminal_even_without_machine_code() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"detail": "This identity was erased."})

    client = CompanionsClient(
        api_key="test-key",
        secret="test-secret",
        service_url="https://pack.invalid",
        privacy_max_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PrivacyOperationError) as raised:
        await client.forget_memory(
            MemoryForgetEvent.create(
                snapshot_user=_privacy_user(),
                companion_id="puppy",
            )
        )

    assert raised.value.status_code == 410
    assert raised.value.code is None
    assert raised.value.disposition is PrivacyErrorDisposition.DISCARD_EVENT
    assert raised.value.terminal


@pytest.mark.asyncio
async def test_privacy_error_traceback_retains_no_payload_headers_or_body() -> None:
    private_fact = "private-fact-marker"
    upstream_body = "upstream-body-marker"
    api_key = "api-key-marker"
    signing_secret = "signing-secret-marker"
    event = MemoryExcludeEvent.create(
        snapshot_user=_privacy_user(),
        companion_id="puppy",
        fact_key="favorite_music",
        fact_value=private_fact,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "stale_privacy_generation",
                    "message": upstream_body,
                }
            },
        )

    client = CompanionsClient(
        api_key=api_key,
        secret=signing_secret,
        service_url="https://pack.invalid",
        privacy_max_attempts=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PrivacyOperationError) as raised:
        await client.exclude_memory(event)

    retained = _sdk_traceback_locals(raised.value)
    assert private_fact not in retained
    assert upstream_body not in retained
    assert api_key not in retained
    assert signing_secret not in retained


def test_account_link_state_observes_success_without_generation_rollback() -> None:
    state = _state(generation=10)
    older = ShareMemoryResult(
        share_memory_across_apps=False,
        is_default=False,
        privacy_generation=9,
    )
    newer = older.model_copy(update={"privacy_generation": 12})

    assert state.observe_result(older) is state
    assert state.observe_result(newer).privacy_generation == 12
