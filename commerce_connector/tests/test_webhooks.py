# Integration test for commerce_connector.webhooks; runs on a site with ERPNext.
# Not runnable without a bench. The signing itself is covered by tests/test_signing.py.

from unittest.mock import patch

import frappe

from commerce_connector import signing, webhooks

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # older frappe
	from frappe.tests.utils import FrappeTestCase as TestCase


class TestWebhookDelivery(TestCase):
	def setUp(self):
		settings = frappe.get_single("Commerce Connector Settings")
		settings.enabled = 1
		settings.platform_webhook_url = "https://platform.invalid/v1/webhooks/erpnext"
		settings.webhook_secret = "test-secret"
		settings.save(ignore_permissions=True)

	def test_deliver_posts_signed_body(self):
		event = {"event_id": "e-1", "event": "sales_invoice.submitted", "occurred_at": "x", "data": {}}
		with patch("commerce_connector.webhooks.requests.post") as post:
			post.return_value.status_code = 200
			webhooks.deliver(event)
		args, kwargs = post.call_args
		self.assertEqual(args[0], "https://platform.invalid/v1/webhooks/erpnext")
		body = kwargs["data"]
		headers = kwargs["headers"]
		ts = headers[signing.TIMESTAMP_HEADER]
		self.assertEqual(signing.sign_timestamped("test-secret", ts, body), headers[signing.SIGNATURE_HEADER])
		self.assertEqual(headers[signing.EVENT_ID_HEADER], "e-1")
