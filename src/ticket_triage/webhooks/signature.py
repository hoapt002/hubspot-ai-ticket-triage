"""HMAC-SHA256 webhook signatures with replay protection.

Header format (same scheme as Stripe):

    X-Triage-Signature: t=<unix seconds>,v1=<hex hmac>

where the HMAC key is the source's shared secret and the signed message is
``f"{t}.".encode() + raw_body``.
"""

from __future__ import annotations

import hashlib
import hmac
import time

HEADER = "X-Triage-Signature"


class SignatureError(Exception):
    pass


def sign(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(
    secret: str,
    body: bytes,
    header: str | None,
    tolerance_seconds: int,
    now: float | None = None,
) -> None:
    if not header:
        raise SignatureError("missing signature header")
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if sep:
            parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError) as exc:
        raise SignatureError("malformed timestamp") from exc
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        raise SignatureError("timestamp outside tolerance")
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
        raise SignatureError("signature mismatch")
