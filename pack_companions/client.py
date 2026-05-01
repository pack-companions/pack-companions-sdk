"""Async client for the Pack Companions service."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel

from pack_companions._signing import build_auth_headers
from pack_companions.snapshot import CompanionSnapshot


class CompanionsAuthError(Exception):
    """The service rejected our credentials or signature."""


class Comment(BaseModel):
    """A comment returned by the service."""

    id: UUID
    text: str
    opportunity_type: str
    was_ai: bool
    model: str | None = None


class CommentResult(BaseModel):
    """Result envelope — comment is None when throttled."""

    comment: Comment | None = None
    throttled: bool = False
    throttle_mode: str | None = None


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

    def __init__(
        self,
        api_key: str,
        secret: str,
        service_url: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not secret:
            raise ValueError("secret is required")

        self.api_key = api_key
        self.secret = secret
        self.service_url = (service_url or self.DEFAULT_SERVICE_URL).rstrip("/")
        self.timeout_seconds = timeout_seconds or self.DEFAULT_TIMEOUT_SECONDS

    async def ping(self) -> dict[str, Any]:
        """Authenticated round-trip — verifies signing works end-to-end."""
        return await self._signed_request("GET", "/v1/ping")

    async def get_comment(self, snapshot: CompanionSnapshot) -> CommentResult:
        """Submit a snapshot, get a comment (or throttle marker) back."""
        body = (
            '{"snapshot": ' + snapshot.model_dump_json() + "}"
        ).encode("utf-8")
        data = await self._signed_request("POST", "/v1/comment", body=body)
        return CommentResult.model_validate(data)

    async def _signed_request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
    ) -> dict[str, Any]:
        # Path-only signing — matches service-side `request.url.path`
        url_path = urlsplit(path).path or path
        headers = build_auth_headers(
            api_key=self.api_key,
            secret=self.secret,
            method=method,
            path=url_path,
            body=body,
        )
        request_headers = dict(headers)
        if body:
            request_headers.setdefault("Content-Type", "application/json")
        async with httpx.AsyncClient(
            base_url=self.service_url,
            timeout=self.timeout_seconds,
        ) as client:
            response = await client.request(
                method=method,
                url=path,
                headers=request_headers,
                content=body if body else None,
            )

        if response.status_code in (401, 403):
            raise CompanionsAuthError(
                f"{response.status_code}: {response.text}"
            )
        response.raise_for_status()
        return response.json()
