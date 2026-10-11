"""Signed webhooks from ERPNext to the commerce platform.

`doc_events` call `on_submit` for Sales Invoice and Delivery Note. When the
document links to a platform Sales Order, a background job POSTs a signed event.
"""

import time
from datetime import UTC, datetime

import frappe
import requests

from commerce_connector import mapping, signing

SETTINGS = "Commerce Connector Settings"
LINK_FIELDS = {
	"Sales Invoice": "sales_order",
	"Delivery Note": "against_sales_order",
}
TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 3


def on_submit(doc, method=None):
	settings = frappe.get_cached_doc(SETTINGS)
	if not settings.enabled or not settings.platform_webhook_url:
		return
	link_field = LINK_FIELDS.get(doc.doctype)
	if not link_field:
		return

	sales_orders = sorted({row.get(link_field) for row in doc.get("items") or [] if row.get(link_field)})
	for so_name in sales_orders:
		so = frappe.db.get_value(
			"Sales Order", so_name, ["platform_order_id", "platform_part_id"], as_dict=True
		)
		if not so or not so.platform_part_id:
			continue
		event = mapping.erp_event(
			doctype=doc.doctype,
			name=doc.name,
			sales_order=so_name,
			platform_order_id=so.platform_order_id,
			platform_part_id=so.platform_part_id,
			posting_date=str(doc.posting_date) if doc.get("posting_date") else None,
			occurred_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
			grand_total=doc.grand_total if doc.doctype == "Sales Invoice" else None,
		)
		frappe.enqueue(
			"commerce_connector.webhooks.deliver",
			queue="short",
			event=event,
			enqueue_after_commit=True,
		)


def deliver(event: dict):
	"""Background job: POST the signed event, retrying a few times on failure."""
	settings = frappe.get_single(SETTINGS)
	url = settings.platform_webhook_url
	secret = settings.get_password("webhook_secret", raise_exception=False)
	if not settings.enabled or not url or not secret:
		return

	body, headers = signing.build_request(secret, event)
	last_error = None
	for attempt in range(MAX_ATTEMPTS):
		try:
			response = requests.post(url, data=body, headers=headers, timeout=TIMEOUT_SECONDS)
			if response.status_code < 300:
				return
			last_error = f"HTTP {response.status_code}"
			if 400 <= response.status_code < 500 and response.status_code != 429:
				break
		except requests.RequestException as e:
			last_error = type(e).__name__
		if attempt < MAX_ATTEMPTS - 1:
			time.sleep(2**attempt)

	# Log only codes and ids, never the payload or the response body.
	frappe.log_error(
		title="Commerce webhook failed",
		message=f"event {event.get('event')} {event.get('event_id')}: {last_error}",
	)
