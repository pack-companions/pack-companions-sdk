from __future__ import annotations

import copy
import hashlib
import hmac
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from pack_companions import (
    AppManifestResponse,
    CompanionsClient,
    CompanionsProtocolError,
)


def _manifest_payload(*, app_id: UUID | None = None) -> dict[str, object]:
    return {
        "app": {
            "id": str(app_id or uuid4()),
            "name": "manifest-test-app",
        },
        "posture": {
            "policy_version": 1,
            "purpose": "Help the user practice.",
            "allowed_context": ["page_path", "problem"],
            "capabilities": [
                "coding_topic_fence",
                "learning_support",
                "semantic_expression",
            ],
            "surface_kind": "web",
            "impulse_pacing": "sparse",
            "senses_enabled": False,
            "memory_access_policy": "app_local_only",
            "expression_surfaces": ["animation", "bubble", "text"],
            "forbidden_assumptions": ["whole_life_friend", "tool_claims"],
            "chat_daily_cap": 100,
            "reply_token_cap": 320,
        },
        "companions": [
            {
                "id": "byte",
                "species": "puppy",
                "display_name": "Byte",
            },
            {
                "id": "spike",
                "species": "hedgehog",
                "display_name": "Spike",
            },
        ],
        "endpoints": {
            "comment": "/v1/comment",
            "chat": "/v1/companion/chat",
            "chat_stream": "/v1/companion/chat/stream",
            "history": "/v1/companion/chat/history",
            "unread": "/v1/companion/chat/unread",
            "mood": "/v1/companion/chat/mood",
            "impulse": "/v1/companion/chat/impulse",
            "perceive": "/v1/companion/perceive",
            "perception_consent": "/v1/companion/perception/consent",
            "memory_facts": "/v1/memory/facts",
            "memory_exclude": "/v1/memory/exclude",
            "forget": "/v1/memory/forget",
            "delete_chat_history": "/v1/memory/delete-chat-history",
            "observation_delete": "/v1/memory/observations/delete",
            "share_memory_set": "/v1/user-settings/share-memory-across-apps/set",
            "identify": "/v1/identity/identify",
            "link_status": "/v1/identity/link-status",
            "erase_for_app": "/v1/identity/erase-for-app",
            "letters": "/v1/companion/letters",
            "letters_ack": "/v1/companion/letters/ack",
            "companion_picker": "/v1/companions/picker",
        },
        "service": {
            "version": "0.1.0",
            "platform_contract_version": "2.0.0",
            "comment_contract_version": "v1",
            "picker_version": "2026-07-23.1",
        },
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


def test_manifest_models_are_typed_immutable_and_forward_compatible() -> None:
    app_id = uuid4()
    payload = _manifest_payload(app_id=app_id)
    payload["future_top_level_field"] = {"ignored": True}
    assert isinstance(payload["posture"], dict)
    payload["posture"]["future_posture_field"] = "ignored"

    manifest = AppManifestResponse.model_validate(payload)

    assert manifest.app.id == app_id
    assert manifest.service.platform_contract_version == "2.0.0"
    assert manifest.posture.allowed_context == ("page_path", "problem")
    assert manifest.companions[0].id == "byte"
    assert manifest.companions[0].species == "puppy"
    assert manifest.endpoints.companion_picker == "/v1/companions/picker"
    with pytest.raises(ValidationError, match="frozen"):
        manifest.posture.surface_kind = "mobile"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["service"].update(  # type: ignore[union-attr]
            platform_contract_version="3.0.0"
        ),
        lambda payload: payload["service"].update(  # type: ignore[union-attr]
            comment_contract_version="v2"
        ),
        lambda payload: payload["posture"].update(  # type: ignore[union-attr]
            policy_version=2
        ),
        lambda payload: payload["posture"].update(  # type: ignore[union-attr]
            policy_version=True
        ),
        lambda payload: payload["posture"].update(  # type: ignore[union-attr]
            capabilities=["future_unreviewed_capability"]
        ),
        lambda payload: payload["endpoints"].update(  # type: ignore[union-attr]
            comment="https://attacker.invalid/v1/comment"
        ),
        lambda payload: payload["companions"].append(  # type: ignore[union-attr]
            {
                "id": "byte",
                "species": "mouse",
                "display_name": "Not Byte",
            }
        ),
    ],
)
def test_manifest_rejects_incompatible_authority(
    mutate,
) -> None:
    payload = _manifest_payload()
    mutate(payload)

    with pytest.raises(ValidationError):
        AppManifestResponse.model_validate(payload)


@pytest.mark.asyncio
async def test_client_fetches_manifest_with_exact_authenticated_empty_get() -> None:
    api_key = "manifest-app-key"
    secret = "manifest-hmac-secret"
    app_id = uuid4()
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        body = await request.aread()
        timestamp = request.headers["X-Pack-Timestamp"]
        signing_string = f"{timestamp}\nGET\n/v1/app/manifest\n".encode("utf-8")
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            signing_string,
            hashlib.sha256,
        ).hexdigest()
        assert request.method == "GET"
        assert request.url.path == "/v1/app/manifest"
        assert body == b""
        assert request.headers["X-Pack-App-Key"] == api_key
        assert request.headers["X-Pack-Signature"] == expected_signature
        assert request.headers["Accept-Encoding"] == "identity"
        assert "Content-Type" not in request.headers
        return httpx.Response(200, json=_manifest_payload(app_id=app_id))

    client = CompanionsClient(
        api_key=api_key,
        secret=secret,
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    manifest = await client.get_app_manifest()

    assert requests == 1
    assert manifest.app.id == app_id
    assert manifest.companions[0].id == "byte"
    assert manifest.companions[0].species == "puppy"


@pytest.mark.asyncio
async def test_client_manifest_protocol_failure_retains_no_upstream_values() -> None:
    upstream_marker = "manifest-upstream-private-marker"
    api_key = "manifest-api-key-marker"
    secret = "manifest-signing-secret-marker"
    payload = _manifest_payload()
    invalid = copy.deepcopy(payload)
    assert isinstance(invalid["app"], dict)
    invalid["app"]["name"] = upstream_marker
    assert isinstance(invalid["service"], dict)
    invalid["service"]["platform_contract_version"] = "99.0.0"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=invalid)

    client = CompanionsClient(
        api_key=api_key,
        secret=secret,
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsProtocolError) as raised:
        await client.get_app_manifest()

    assert str(raised.value) == "service returned an invalid app manifest"
    retained = _sdk_traceback_locals(raised.value)
    assert upstream_marker not in retained
    assert api_key not in retained
    assert secret not in retained


@pytest.mark.asyncio
async def test_client_manifest_response_is_bounded_before_validation() -> None:
    marker = "oversized-manifest-body-marker"
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=(
                b'{"value":"'
                + marker.encode("utf-8")
                + (b"x" * CompanionsClient.MAX_RESPONSE_BYTES)
                + b'"}'
            ),
            headers={"Content-Type": "application/json"},
        )

    client = CompanionsClient(
        api_key="manifest-app-key",
        secret="manifest-hmac-secret",
        service_url="https://brain.example.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CompanionsProtocolError) as raised:
        await client.get_app_manifest()

    assert calls == 1
    assert str(raised.value) == "service response exceeded the SDK size limit"
    assert marker not in _sdk_traceback_locals(raised.value)
