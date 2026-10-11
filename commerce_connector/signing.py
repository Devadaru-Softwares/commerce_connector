"""Webhook signing. Pure functions: no frappe import.

The platform verifies `X-Commerce-Signature` as lowercase hex HMAC-SHA256 of the
raw request body, keyed with the shared webhook secret. The event id travels in
`X-Commerce-Event-Id` and is used by the platform to dedupe deliveries.

When `X-Commerce-Timestamp` (unix seconds) is sent, the signature covers
`{timestamp}.{raw_body}` instead, and the platform rejects events more than five
minutes off. The connector always sends the timestamp.
"""

import hashlib
import hmac
import json

SIGNATURE_HEADER = "X-Commerce-Signature"
EVENT_ID_HEADER = "X-Commerce-Event-Id"
TIMESTAMP_HEADER = "X-Commerce-Timestamp"


def _to_bytes(value: str | bytes) -> bytes:
	return value if isinstance(value, bytes) else value.encode("utf-8")


def sign(secret: str | bytes, body: str | bytes) -> str:
	"""Return the lowercase hex HMAC-SHA256 of `body` keyed with `secret`."""
	if not secret:
		raise ValueError("webhook secret is empty")
	return hmac.new(_to_bytes(secret), _to_bytes(body), hashlib.sha256).hexdigest()


def sign_timestamped(secret: str | bytes, timestamp: int | str, body: str | bytes) -> str:
	"""Lowercase hex HMAC-SHA256 of `{timestamp}.{body}`."""
	ts = str(int(timestamp)).encode("ascii")
	return sign(secret, ts + b"." + _to_bytes(body))


def verify(secret: str | bytes, body: str | bytes, signature: str) -> bool:
	"""Constant-time check of a signature produced by `sign`."""
	if not signature:
		return False
	return hmac.compare_digest(sign(secret, body), signature.strip().lower())


def encode_body(event: dict) -> bytes:
	"""Serialise an event exactly once; the signature covers these bytes."""
	return json.dumps(event, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")


def signed_headers(secret: str | bytes, body: bytes, event_id: str, timestamp: int) -> dict:
	"""Headers for one delivery attempt; the timestamp is part of the signed message."""
	ts = str(int(timestamp))
	return {
		"Content-Type": "application/json",
		SIGNATURE_HEADER: sign_timestamped(secret, ts, body),
		EVENT_ID_HEADER: str(event_id),
		TIMESTAMP_HEADER: ts,
	}


def build_request(secret: str | bytes, event: dict, timestamp: int) -> tuple[bytes, dict]:
	"""Return (raw_body, headers) for a signed, timestamped webhook POST."""
	body = encode_body(event)
	return body, signed_headers(secret, body, event["event_id"], timestamp)
