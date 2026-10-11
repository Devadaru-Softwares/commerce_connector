import hashlib
import hmac
import json

from commerce_connector import signing

# Shared vector: the platform's verifier test uses the same secret, body and digest.
VECTOR_SECRET = "s"
VECTOR_BODY = '{"a":1}'
VECTOR_SIGNATURE = "37beaf650f70b40ec9706929c2e9d835cbd63729988f48781e6383a147215f07"


def test_vector_matches_hmac_sha256_hex():
	expected = hmac.new(VECTOR_SECRET.encode(), VECTOR_BODY.encode(), hashlib.sha256).hexdigest()
	assert expected == VECTOR_SIGNATURE
	assert signing.sign(VECTOR_SECRET, VECTOR_BODY) == VECTOR_SIGNATURE
	assert signing.sign(VECTOR_SECRET.encode(), VECTOR_BODY.encode()) == VECTOR_SIGNATURE


def test_signature_is_lowercase_hex():
	sig = signing.sign("secret", b"body")
	assert len(sig) == 64
	assert sig == sig.lower()
	int(sig, 16)


def test_verify_accepts_valid_and_rejects_tampering():
	assert signing.verify(VECTOR_SECRET, VECTOR_BODY, VECTOR_SIGNATURE)
	assert signing.verify(VECTOR_SECRET, VECTOR_BODY, VECTOR_SIGNATURE.upper())
	assert not signing.verify(VECTOR_SECRET, '{"a":2}', VECTOR_SIGNATURE)
	assert not signing.verify("t", VECTOR_BODY, VECTOR_SIGNATURE)
	assert not signing.verify(VECTOR_SECRET, VECTOR_BODY, "")


def test_empty_secret_is_rejected():
	try:
		signing.sign("", "x")
	except ValueError:
		return
	raise AssertionError("empty secret must raise")


def test_build_request_signs_the_exact_body_sent():
	event = {"event_id": "e-1", "event": "sales_invoice.submitted", "data": {"b": 1, "a": "₹"}}
	body, headers = signing.build_request("k", event)
	assert json.loads(body) == event
	assert body == b'{"data":{"a":"\xe2\x82\xb9","b":1},"event":"sales_invoice.submitted","event_id":"e-1"}'
	assert headers[signing.SIGNATURE_HEADER] == hmac.new(b"k", body, hashlib.sha256).hexdigest()
	assert headers[signing.EVENT_ID_HEADER] == "e-1"
	assert headers["Content-Type"] == "application/json"
	assert signing.SIGNATURE_HEADER == "X-Commerce-Signature"
	assert signing.EVENT_ID_HEADER == "X-Commerce-Event-Id"
