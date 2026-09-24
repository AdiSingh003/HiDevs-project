"""HMAC-SHA256 signatures for the telemetry webhook (Stripe-style: signed "{timestamp}.{body}")."""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Telemetry-Signature"
TIMESTAMP_HEADER = "X-Telemetry-Timestamp"


class SignatureError(ValueError):
    pass


def sign(secret: str, body: bytes, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    digest = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return ts, f"sha256={digest}"


def verify(secret: str, body: bytes, timestamp: str | None, signature: str | None, tolerance_s: int = 300) -> None:
    if not timestamp or not signature:
        raise SignatureError("missing signature headers")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("invalid timestamp") from exc
    if abs(time.time() - ts) > tolerance_s:
        raise SignatureError("timestamp outside the replay window")
    _, expected = sign(secret, body, ts)
    if not hmac.compare_digest(expected, signature.strip()):
        raise SignatureError("signature mismatch")
