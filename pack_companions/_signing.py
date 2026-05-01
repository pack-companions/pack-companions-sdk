"""HMAC request signing — must match the service's verifier.

Wire protocol (every authed request includes these three headers):

    X-Pack-App-Key    plaintext API key
    X-Pack-Timestamp  Unix epoch seconds
    X-Pack-Signature  HMAC-SHA256 over `{timestamp}\\n{method}\\n{path}\\n{body}`
                      using the app's hmac_secret, hex-encoded

Keep this in sync with the service's app/services/auth.py.
"""

from __future__ import annotations

import hashlib
import hmac
import time

APP_KEY_HEADER = "X-Pack-App-Key"
TIMESTAMP_HEADER = "X-Pack-Timestamp"
SIGNATURE_HEADER = "X-Pack-Signature"


def _build_signing_string(timestamp: str, method: str, path: str, body: bytes) -> str:
    body_str = body.decode("utf-8", errors="replace") if body else ""
    return f"{timestamp}\n{method.upper()}\n{path}\n{body_str}"


def _sign(secret: str, timestamp: str, method: str, path: str, body: bytes) -> str:
    msg = _build_signing_string(timestamp, method, path, body)
    return hmac.new(
        secret.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_auth_headers(
    api_key: str,
    secret: str,
    method: str,
    path: str,
    body: bytes = b"",
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build the three signed headers for a request."""
    ts = str(timestamp if timestamp is not None else int(time.time()))
    return {
        APP_KEY_HEADER: api_key,
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: _sign(secret, ts, method, path, body),
    }
