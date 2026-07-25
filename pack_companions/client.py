"""Async client for the Pack Companions service."""

from __future__ import annotations

import asyncio
import json
from typing import Any, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from pack_companions._signing import build_auth_headers
from pack_companions.comment import (
    Comment as Comment,
    CommentEvent,
    CommentEventType,
    CommentResult,
)
from pack_companions.manifest import AppManifestResponse
from pack_companions.privacy import (
    ConnectedAppsStatusRequest,
    ConnectedAppsStatusResponse,
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
    PrivacyOperationErrorCode,
    PrivacyOperationEvent,
    PrivacyGenerationResult,
    ShareMemoryEvent,
    ShareMemoryResult,
)
from pack_companions.runtime_catalog import RuntimeCatalogDiscoveryResponse
from pack_companions.snapshot import CompanionSnapshot


class CompanionsError(Exception):
    """Base class for sanitized SDK failures."""


class CompanionsAuthError(CompanionsError):
    """The service rejected our credentials or signature."""


class CompanionsProtocolError(CompanionsError):
    """The service returned an invalid or incompatible wire response."""


class CompanionsTransportError(CompanionsError):
    """The service could not be reached or the response stream failed."""


class CompanionsServiceError(CompanionsError):
    """The service returned a non-success status without exposing its response."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Pack Companions service returned HTTP {status_code}")


class ExistingIdentityLinkRequiredError(CompanionsServiceError):
    """Existing-only identify found no active link for the authenticated host pair."""

    code = "existing_identity_link_required"

    def __init__(self) -> None:
        super().__init__(404)


class PrivacyOperationError(CompanionsServiceError):
    """Sanitized, actionable error from a generation-bound mutation.

    The exception retains only an optional allowlisted machine code, a safe
    disposition, HTTP status, optional ``operation_completed`` bit, and a
    bounded integer ``retry_after_seconds``. An unknown response code or raw
    header is never reflected. The exception never retains Pack's body,
    request bytes, HMAC headers, host identifier, or payload.
    """

    def __init__(
        self,
        status_code: int,
        *,
        code: PrivacyOperationErrorCode | None,
        disposition: PrivacyErrorDisposition | None = None,
        operation_completed: bool | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.operation_completed = operation_completed
        self.retry_after_seconds = retry_after_seconds
        self._disposition = (
            code.disposition
            if code is not None
            else disposition or self._default_disposition(status_code)
        )
        super().__init__(status_code)
        label = code.value if code is not None else "unclassified_privacy_error"
        self.args = (
            f"Pack privacy operation returned {label} "
            f"(HTTP {status_code}; disposition={self._disposition.value})",
        )

    @property
    def disposition(self) -> PrivacyErrorDisposition:
        return self._disposition

    @staticmethod
    def _default_disposition(status_code: int) -> PrivacyErrorDisposition:
        """Fail closed while preserving exact retry for transient statuses."""
        if status_code in {408, 425, 429} or status_code >= 500:
            return PrivacyErrorDisposition.RETRY_SAME_EVENT
        return PrivacyErrorDisposition.DISCARD_EVENT

    @property
    def retry_same_event(self) -> bool:
        """Whether a retry must reuse this event's exact bytes and UUID."""
        return self.disposition is PrivacyErrorDisposition.RETRY_SAME_EVENT

    @property
    def terminal(self) -> bool:
        """Whether the original queued event must never be submitted again."""
        return not self.retry_same_event


_V1_DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "decision_id",
        "outcome",
        "deduplicated",
        "text",
        "opportunity_type",
        "coalesced_opportunity_types",
        "expression",
        "response_locale",
        "speech_locale",
        "next_evaluation_at",
    }
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_PrivacyResultT = TypeVar("_PrivacyResultT", bound=PrivacyGenerationResult)


class CompanionsClient:
    """Python client for the Pack Companions service.

    Example:
        client = CompanionsClient(
            api_key="...",
            secret="...",
            service_url="http://localhost:9200",
        )
        result = await client.ping()
    """

    DEFAULT_SERVICE_URL = "https://companions.getpacked.ai"
    DEFAULT_TIMEOUT_SECONDS = 5.0
    DEFAULT_COMMENT_MAX_ATTEMPTS = 2
    MAX_COMMENT_ATTEMPTS = 3
    DEFAULT_PRIVACY_MAX_ATTEMPTS = 2
    MAX_PRIVACY_ATTEMPTS = 3
    # Mirrors the service's default MAX_REQUEST_BODY_BYTES middleware cap.
    MAX_REQUEST_BYTES = 3 * 1024 * 1024
    MAX_RESPONSE_BYTES = 256 * 1024
    RESPONSE_CHUNK_BYTES = 16 * 1024
    MAX_RETRY_AFTER_SECONDS = 60 * 60
    _RETRYABLE_COMMENT_STATUSES = frozenset({502, 503, 504})

    def __init__(
        self,
        api_key: str,
        secret: str,
        service_url: str | None = None,
        timeout_seconds: float | None = None,
        *,
        comment_max_attempts: int = DEFAULT_COMMENT_MAX_ATTEMPTS,
        privacy_max_attempts: int = DEFAULT_PRIVACY_MAX_ATTEMPTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not secret:
            raise ValueError("secret is required")
        if not 1 <= comment_max_attempts <= self.MAX_COMMENT_ATTEMPTS:
            raise ValueError(
                f"comment_max_attempts must be between 1 and {self.MAX_COMMENT_ATTEMPTS}"
            )
        if not 1 <= privacy_max_attempts <= self.MAX_PRIVACY_ATTEMPTS:
            raise ValueError(
                f"privacy_max_attempts must be between 1 and {self.MAX_PRIVACY_ATTEMPTS}"
            )
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self.api_key = api_key
        self.secret = secret
        self.service_url = (service_url or self.DEFAULT_SERVICE_URL).rstrip("/")
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else self.DEFAULT_TIMEOUT_SECONDS
        )
        self.comment_max_attempts = comment_max_attempts
        self.privacy_max_attempts = privacy_max_attempts
        self._transport = transport

    async def ping(self) -> dict[str, Any]:
        """Authenticated round-trip — verifies signing works end-to-end."""
        return await self._signed_request("GET", "/v1/ping")

    async def get_app_manifest(self) -> AppManifestResponse:
        """Return the authenticated app's validated platform-v2 manifest.

        The response must declare a compatible platform/comment contract,
        known posture vocabulary, canonical unique roster IDs, and the exact
        platform-v2 endpoint paths before it is exposed to the host.
        """
        data = await self._signed_request(
            "GET",
            AppManifestResponse.PATH,
            max_attempts=1,
        )
        return self._parse_model(
            data,
            AppManifestResponse,
            invalid_message="service returned an invalid app manifest",
        )

    async def get_runtime_catalog(self) -> RuntimeCatalogDiscoveryResponse:
        """Return validated runtime availability and immutable catalog pointers.

        This discovery call does not fetch or validate any referenced asset
        catalog. Renderer SDKs and native clients own that boundary.
        """
        data = await self._signed_request(
            "GET",
            RuntimeCatalogDiscoveryResponse.PATH,
            max_attempts=1,
        )
        return self._parse_model(
            data,
            RuntimeCatalogDiscoveryResponse,
            invalid_message=("service returned an invalid runtime catalog discovery response"),
        )

    async def identify(self, request: IdentifyRequest) -> IdentifyResponse:
        """Bootstrap or explicitly recover one app-local account link.

        ``identify`` is the only ceremony that may return a new incarnation
        and reset the host's persisted generation. It must be initiated for a
        genuinely new authenticated operation; never call it to refresh a
        stale queued privacy event and replay that old payload.

        Set ``require_existing_link=True`` when repairing missing local token
        state for an authenticated account that Pack must already know. That
        mode fails rather than creating or mutating an identity and always
        returns ``is_new=False``.
        """
        require_existing_link = request.require_existing_link
        body = request.request_bytes
        del request
        try:
            data = await self._signed_request(
                "POST",
                "/v1/identity/identify",
                body=body,
                max_attempts=1,
                classify_existing_identity_miss=require_existing_link,
            )
        except CompanionsError:
            body = b""
            raise
        body = b""
        response = self._parse_model(
            data,
            IdentifyResponse,
            invalid_message="service returned an invalid identify response",
        )
        if require_existing_link and response.is_new:
            raise CompanionsProtocolError(
                "service created an identity during existing-only recovery"
            )
        return response

    async def get_connected_apps_status(
        self,
        request: ConnectedAppsStatusRequest,
    ) -> ConnectedAppsStatusResponse:
        """Read explicit-link status without identifying or joining accounts."""
        body = request.request_bytes
        del request
        try:
            data = await self._signed_request(
                "POST",
                ConnectedAppsStatusRequest.PATH,
                body=body,
                max_attempts=1,
            )
        except CompanionsError:
            body = b""
            raise
        body = b""
        return self._parse_model(
            data,
            ConnectedAppsStatusResponse,
            invalid_message=("service returned an invalid connected-app status response"),
        )

    async def erase_for_app(
        self,
        event: EraseForAppEvent,
    ) -> EraseForAppResult:
        """Attempt one durable ID-4 erasure delivery.

        The client never identifies, replaces the captured incarnation, or
        retries automatically. The durable host queue owns observability and
        retries by restoring the original immutable bytes and event ID.
        """
        body = event.request_bytes
        del event
        try:
            data = await self._signed_request(
                "POST",
                EraseForAppEvent.PATH,
                body=body,
                max_attempts=1,
                classify_privacy_error=True,
            )
        except CompanionsError:
            body = b""
            raise
        body = b""
        return self._parse_model(
            data,
            EraseForAppResult,
            invalid_message="service returned an invalid erase-for-app response",
        )

    async def exclude_memory(
        self,
        event: MemoryExcludeEvent,
    ) -> MemoryExcludeResult:
        try:
            return await self._send_privacy_event(event, MemoryExcludeResult)
        except CompanionsError:
            del event
            raise

    async def forget_memory(
        self,
        event: MemoryForgetEvent,
    ) -> MemoryForgetResult:
        try:
            return await self._send_privacy_event(event, MemoryForgetResult)
        except CompanionsError:
            del event
            raise

    async def delete_chat_history(
        self,
        event: MemoryDeleteChatHistoryEvent,
    ) -> MemoryDeleteChatHistoryResult:
        try:
            return await self._send_privacy_event(
                event,
                MemoryDeleteChatHistoryResult,
            )
        except CompanionsError:
            del event
            raise

    async def delete_observation(
        self,
        event: ObservationDeleteEvent,
    ) -> ObservationDeleteResult:
        try:
            return await self._send_privacy_event(
                event,
                ObservationDeleteResult,
            )
        except CompanionsError:
            del event
            raise

    async def set_share_memory_across_apps(
        self,
        event: ShareMemoryEvent,
    ) -> ShareMemoryResult:
        try:
            return await self._send_privacy_event(event, ShareMemoryResult)
        except CompanionsError:
            del event
            raise

    async def set_perception_consent(
        self,
        event: PerceptionConsentEvent,
    ) -> PerceptionConsentResult:
        try:
            return await self._send_privacy_event(event, PerceptionConsentResult)
        except CompanionsError:
            del event
            raise

    async def set_link_status(
        self,
        event: LinkStatusEvent,
    ) -> LinkStatusResult:
        try:
            return await self._send_privacy_event(event, LinkStatusResult)
        except CompanionsError:
            del event
            raise

    async def _send_privacy_event(
        self,
        event: PrivacyOperationEvent,
        result_type: type[_PrivacyResultT],
    ) -> _PrivacyResultT:
        """Retry only exact immutable event bytes; never refresh its tokens."""
        body = event.request_bytes
        path = event.PATH
        del event
        try:
            data = await self._signed_request(
                "POST",
                path,
                body=body,
                max_attempts=self.privacy_max_attempts,
                retryable_statuses=self._RETRYABLE_COMMENT_STATUSES,
                classify_privacy_error=True,
            )
        except CompanionsError:
            body = b""
            raise
        body = b""
        return self._parse_model(
            data,
            result_type,
            invalid_message="service returned an invalid privacy-operation response",
        )

    async def get_comment_event(self, event: CommentEvent) -> CommentResult:
        """Submit one typed event and safely reuse its ID on internal retries.

        Keep and reuse the same :class:`CommentEvent` if the caller itself
        retries after an ambiguous timeout.  Never rebuild it with a new UUID
        for the same logical fact.
        """
        body = event.request_bytes
        del event
        try:
            data = await self._signed_request(
                "POST",
                "/v1/comment",
                body=body,
                max_attempts=self.comment_max_attempts,
                retryable_statuses=self._RETRYABLE_COMMENT_STATUSES,
                # Typed facts are bound to the same account incarnation and
                # privacy generation as mutations. Preserve only the bounded,
                # allowlisted machine metadata so a host queue can terminally
                # discard stale work instead of retrying it forever.
                classify_privacy_error=True,
            )
        except CompanionsError:
            body = b""
            raise
        body = b""
        try:
            self._require_v1_decision(data)
            return self._parse_comment_result(
                data,
                invalid_message="service returned an invalid typed comment decision",
            )
        finally:
            # Pydantic has copied every accepted value into the result. Scrub
            # the untrusted wire dictionary from traceback-visible locals.
            data.clear()

    async def get_comment(
        self,
        snapshot: CompanionSnapshot,
        *,
        event_type: CommentEventType | None = None,
        client_event_id: UUID | None = None,
    ) -> CommentResult:
        """Submit a snapshot using the typed contract or bounded legacy mode.

        New integrations must provide both ``event_type`` and the same
        ``client_event_id`` on every retry.  Omitting both keeps the historical
        snapshot-only request for one compatibility window.  That legacy path
        deliberately performs only one network attempt because it has no
        idempotency key.
        """
        if (event_type is None) != (client_event_id is None):
            raise ValueError("event_type and client_event_id must be supplied together")
        if event_type is None:
            try:
                return await self.get_legacy_comment(snapshot)
            except CompanionsError:
                del snapshot
                raise
        if client_event_id is None:  # pragma: no cover - paired check above
            raise ValueError("event_type and client_event_id must be supplied together")
        event = CommentEvent(
            snapshot=snapshot,
            client_event_id=client_event_id,
            event_type=event_type,
        )
        try:
            return await self.get_comment_event(event)
        except CompanionsError:
            del event
            del snapshot
            raise

    async def get_legacy_comment(
        self,
        snapshot: CompanionSnapshot,
    ) -> CommentResult:
        """Compatibility-only snapshot request with no automatic retry."""
        # Keep the historical wire bytes (including the single space) stable.
        body = ('{"snapshot": ' + snapshot.model_dump_json() + "}").encode("utf-8")
        del snapshot
        try:
            data = await self._signed_request(
                "POST",
                "/v1/comment",
                body=body,
                max_attempts=1,
            )
        except CompanionsError:
            body = b""
            raise
        body = b""
        try:
            return self._parse_comment_result(
                data,
                invalid_message="service returned an invalid legacy comment decision",
            )
        finally:
            data.clear()

    @staticmethod
    def _parse_comment_result(
        data: dict[str, Any],
        *,
        invalid_message: str,
    ) -> CommentResult:
        """Validate protocol data without reflecting rejected upstream values."""
        try:
            return CommentResult.model_validate(data)
        except ValidationError:
            # Leave the validation exception's context before raising. Pydantic
            # error objects retain the rejected input, so chaining one here
            # would make an untrusted response recoverable from the public
            # protocol exception.
            invalid = True
        if invalid:
            data.clear()
        raise CompanionsProtocolError(invalid_message)

    @staticmethod
    def _parse_model(
        data: dict[str, Any],
        model_type: type[_ModelT],
        *,
        invalid_message: str,
    ) -> _ModelT:
        """Validate a response while retaining no rejected upstream values."""
        try:
            result = model_type.model_validate(data)
        except ValidationError:
            invalid = True
        else:
            data.clear()
            return result
        if invalid:
            data.clear()
        raise CompanionsProtocolError(invalid_message)

    @staticmethod
    def _require_v1_decision(data: dict[str, Any]) -> None:
        missing = sorted(_V1_DECISION_FIELDS.difference(data))
        if missing:
            message = "typed /v1/comment response is missing required fields: " + ", ".join(missing)
            data.clear()
            raise CompanionsProtocolError(message)
        if data.get("schema_version") != "v1":
            data.clear()
            raise CompanionsProtocolError(
                "typed /v1/comment response has an unsupported schema version"
            )
        if data.get("decision_id") is None or data.get("outcome") is None:
            data.clear()
            raise CompanionsProtocolError(
                "typed /v1/comment response lacks a durable terminal decision"
            )

    async def _signed_request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
        *,
        max_attempts: int = 1,
        retryable_statuses: frozenset[int] = frozenset(),
        classify_privacy_error: bool = False,
        classify_existing_identity_miss: bool = False,
    ) -> dict[str, Any]:
        """Send an exact signed body, with bounded opt-in transport retries."""
        if len(body) > self.MAX_REQUEST_BYTES:
            body = b""
            raise CompanionsProtocolError("request body exceeded the SDK size limit")
        url_path = urlsplit(path).path or path
        terminal_kind: str | None = None
        terminal_status: int | None = None
        privacy_error_code: PrivacyOperationErrorCode | None = None
        privacy_operation_completed: bool | None = None
        privacy_retry_after_seconds: int | None = None
        existing_identity_link_required = False
        protocol_message: str | None = None
        response_body: bytes | None = None
        request_headers: dict[str, str] = {}
        active_response: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(
                base_url=self.service_url,
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                for attempt in range(max_attempts):
                    # Re-sign each attempt with a fresh timestamp, but preserve
                    # the exact body bytes and stable client_event_id.
                    request_headers = build_auth_headers(
                        api_key=self.api_key,
                        secret=self.secret,
                        method=method,
                        path=url_path,
                        body=body,
                    )
                    if body:
                        request_headers.setdefault("Content-Type", "application/json")
                    request_headers.setdefault("Accept-Encoding", "identity")
                    retry = False
                    try:
                        async with client.stream(
                            method=method,
                            url=path,
                            headers=request_headers,
                            content=body if body else None,
                        ) as active_response:
                            status_code = active_response.status_code
                            if status_code in retryable_statuses and attempt + 1 < max_attempts:
                                retry = True
                            elif status_code in (401, 403):
                                terminal_kind = "auth"
                                terminal_status = status_code
                            elif not 200 <= status_code < 300:
                                terminal_kind = "service"
                                terminal_status = status_code
                                if classify_privacy_error or classify_existing_identity_miss:
                                    privacy_retry_after_seconds = self._parse_retry_after_seconds(
                                        active_response.headers.get("Retry-After")
                                    )
                                    (
                                        error_body,
                                        _error_protocol_message,
                                    ) = await self._read_response_bounded(active_response)
                                    if error_body is not None:
                                        if classify_privacy_error:
                                            (
                                                privacy_error_code,
                                                privacy_operation_completed,
                                            ) = self._classify_privacy_error_body(error_body)
                                        if classify_existing_identity_miss and status_code == 404:
                                            existing_identity_link_required = (
                                                self._is_existing_identity_link_required(error_body)
                                            )
                                        error_body = None
                            else:
                                response_body, protocol_message = await self._read_response_bounded(
                                    active_response
                                )
                    except httpx.TransportError:
                        if attempt + 1 < max_attempts:
                            retry = True
                        else:
                            terminal_kind = "transport"
                    active_response = None
                    request_headers.clear()
                    if retry:
                        await asyncio.sleep(0.05 * (2**attempt))
                        continue
                    break
                else:  # pragma: no cover - max_attempts is always positive
                    terminal_kind = "transport"
        except (httpx.HTTPError, httpx.InvalidURL):
            # Includes constructor/close failures outside the per-attempt
            # transport block. Never expose httpx's request-bearing exception.
            terminal_kind = "transport"

        # Remove signed headers and payloads before any public exception is
        # created. The custom exceptions below never retain request/response
        # objects, and their tracebacks see only these scrubbed locals.
        request_headers.clear()
        body = b""
        if terminal_kind == "auth":
            response_body = None
            raise CompanionsAuthError(f"service rejected credentials ({terminal_status})")
        if terminal_kind == "service":
            response_body = None
            if terminal_status is None:  # pragma: no cover - defensive invariant
                raise CompanionsProtocolError("service failure without a status")
            if classify_privacy_error:
                raise PrivacyOperationError(
                    terminal_status,
                    code=privacy_error_code,
                    operation_completed=privacy_operation_completed,
                    retry_after_seconds=privacy_retry_after_seconds,
                )
            if (
                classify_existing_identity_miss
                and terminal_status == 404
                and existing_identity_link_required
            ):
                raise ExistingIdentityLinkRequiredError()
            raise CompanionsServiceError(terminal_status)
        if terminal_kind == "transport":
            response_body = None
            raise CompanionsTransportError("Pack Companions transport failed after bounded retries")
        if protocol_message is not None:
            response_body = None
            raise CompanionsProtocolError(protocol_message)
        if response_body is None:  # pragma: no cover - defensive invariant
            raise CompanionsProtocolError("service returned no response body")
        invalid_json = False
        try:
            data = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            # Do not chain a decoder error that may retain response fragments.
            invalid_json = True
        if invalid_json:
            response_body = None
            raise CompanionsProtocolError("service returned a non-JSON response")
        response_body = None
        if not isinstance(data, dict):
            if isinstance(data, list):
                data.clear()
            data = None
            raise CompanionsProtocolError("service response must be a JSON object")
        return data

    @staticmethod
    def _classify_privacy_error_body(
        response_body: bytes,
    ) -> tuple[PrivacyOperationErrorCode | None, bool | None]:
        """Extract only allowlisted privacy metadata from an error body."""
        parsed: object | None = None
        try:
            parsed = json.loads(response_body)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ):
            return None, None
        finally:
            response_body = b""
        if not isinstance(parsed, dict):
            if isinstance(parsed, list):
                parsed.clear()
            return None, None
        detail = parsed.get("detail")
        code_value = detail.get("code") if isinstance(detail, dict) else None
        completed_value = detail.get("operation_completed") if isinstance(detail, dict) else None
        try:
            code = PrivacyOperationErrorCode(code_value)
        except (TypeError, ValueError):
            parsed.clear()
            return None, None
        operation_completed = completed_value if isinstance(completed_value, bool) else None
        parsed.clear()
        return code, operation_completed

    @staticmethod
    def _is_existing_identity_link_required(response_body: bytes) -> bool:
        """Recognize only the allowlisted existing-only identify miss code."""
        parsed: object | None = None
        try:
            parsed = json.loads(response_body)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ):
            return False
        finally:
            response_body = b""
        if not isinstance(parsed, dict):
            if isinstance(parsed, list):
                parsed.clear()
            return False
        detail = parsed.get("detail")
        matched = (
            isinstance(detail, dict)
            and detail.get("code") == ExistingIdentityLinkRequiredError.code
        )
        parsed.clear()
        return matched

    @classmethod
    def _parse_retry_after_seconds(
        cls,
        value: str | None,
    ) -> int | None:
        """Accept only bounded HTTP delta-seconds; never retain raw headers."""
        if value is None or len(value) > 10 or not value.isascii() or not value.isdigit():
            return None
        seconds = int(value, 10)
        if seconds > cls.MAX_RETRY_AFTER_SECONDS:
            return None
        return seconds

    async def _read_response_bounded(
        self,
        response: httpx.Response,
    ) -> tuple[bytes | None, str | None]:
        """Return bounded bytes or a safe protocol error without raising it."""
        content_encoding = response.headers.get("content-encoding", "").strip().lower()
        if content_encoding not in {"", "identity"}:
            # The request explicitly advertises identity encoding. Refusing an
            # unsolicited compressed body avoids allocating a decompression
            # bomb before an application-level decoded-byte cap can run.
            return None, "service returned an unsupported encoded response"
        # Some in-memory/custom transports hand httpx an already-consumed
        # response even when the caller used ``client.stream``. The network
        # transport remains streaming; keep this bounded compatibility path
        # for tests and embedding transports.
        if response.is_stream_consumed:
            content = response.content
            if len(content) > self.MAX_RESPONSE_BYTES:
                content = b""
                return None, "service response exceeded the SDK size limit"
            return content, None
        body = bytearray()
        async for chunk in response.aiter_raw(chunk_size=self.RESPONSE_CHUNK_BYTES):
            if len(body) + len(chunk) > self.MAX_RESPONSE_BYTES:
                body.clear()
                return None, "service response exceeded the SDK size limit"
            body.extend(chunk)
        content = bytes(body)
        body.clear()
        return content, None
