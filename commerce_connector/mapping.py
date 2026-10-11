"""Payload mapping between the commerce platform and ERPNext documents.

Pure functions only: plain dicts in, plain dicts out, no frappe import, so the
contract can be unit tested without a bench. Money arrives as rupee strings with
exactly two decimal places ("1234.56") and is handled as `Decimal` throughout.
"""

import re
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

PAISA = Decimal("0.01")
MONEY_RE = re.compile(r"^\d+\.\d{2}$")
QUANTITY_RE = re.compile(r"^\d+(\.\d+)?$")
RATE_RE = re.compile(r"^\d+\.\d{6}$")
RATE_PRECISION = 6

SUPPLY_TAX_TYPES = {
	"intra": {"CGST", "SGST"},
	"inter": {"IGST"},
}

ITEM_CREATE_ONLY_FIELDS = ("item_code", "item_group", "stock_uom", "is_stock_item")

STATUS_LABELS = {
	"dispatched": "Dispatched",
	"delivered": "Delivered",
}

APD_ACTIONS = ("accrue", "void", "payout")
APD_ACCOUNT_PREFIX = "Architect Privilege Discount"
_UNSAFE_NAME_CHARS = re.compile(r"[<>\"#%*?`{}|\\/\[\]]")

ERP_EVENTS = {
	"Sales Invoice": "sales_invoice.submitted",
	"Delivery Note": "delivery_note.submitted",
}
_EVENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "commerce_connector/erp-events")


class MappingError(ValueError):
	"""Raised when a payload breaks the contract. The message is safe to return."""


# ---------------------------------------------------------------- primitives


def money(value, field: str) -> Decimal:
	"""Parse a rupee string with exactly two decimals. Floats are rejected."""
	if not isinstance(value, str) or not MONEY_RE.match(value):
		raise MappingError(f'{field} must be a rupee string with 2 decimals, e.g. "1234.56"')
	return Decimal(value)


def quantity(value, field: str) -> Decimal:
	"""Parse a non-negative decimal string (or int). Floats are rejected."""
	if isinstance(value, bool) or isinstance(value, float):
		raise MappingError(f"{field} must be a decimal string")
	if isinstance(value, int):
		value = str(value)
	if not isinstance(value, str) or not QUANTITY_RE.match(value):
		raise MappingError(f"{field} must be a non-negative decimal string")
	try:
		return Decimal(value)
	except InvalidOperation:
		raise MappingError(f"{field} must be a non-negative decimal string")


def line_rate(value, field: str) -> Decimal:
	"""Parse a line rate: a string with exactly 6 decimals (amount / qty, rounded)."""
	if not isinstance(value, str) or not RATE_RE.match(value):
		raise MappingError(f'{field} must be a string with 6 decimals, e.g. "333.333333"')
	return Decimal(value)


def round_paisa(value: Decimal) -> Decimal:
	return value.quantize(PAISA, rounding=ROUND_HALF_UP)


def required(payload: dict, key: str, label: str | None = None) -> str:
	value = payload.get(key)
	if value is None or (isinstance(value, str) and not value.strip()):
		raise MappingError(f"{label or key} is required")
	return str(value).strip()


def optional(payload: dict, key: str) -> str | None:
	value = payload.get(key)
	if value is None:
		return None
	value = str(value).strip()
	return value or None


def iso_date(value, field: str) -> str:
	if not isinstance(value, str):
		raise MappingError(f"{field} must be a date string YYYY-MM-DD")
	try:
		return date.fromisoformat(value).isoformat()
	except ValueError:
		raise MappingError(f"{field} must be a date string YYYY-MM-DD")


def as_rupees(value: Decimal) -> str:
	return str(value.quantize(PAISA))


def assert_total(label: str, expected: Decimal, actual, context: str) -> None:
	"""Tolerance 0: ERPNext's computed value must equal the platform value to the paisa."""
	try:
		got = Decimal(str(actual if actual is not None else 0)).quantize(PAISA)
	except InvalidOperation:
		raise MappingError(f"{label} for {context} is not a number")
	if got != expected:
		raise MappingError(
			f"{label} mismatch for {context}: ERPNext computed {as_rupees(got)}, "
			f"platform total is {as_rupees(expected)}"
		)


# ---------------------------------------------------------------------- item


def item_from_payload(payload: dict, *, default_item_group: str | None = None) -> dict:
	"""Map `upsert_item` payload to Item fields.

	Fields listed in ITEM_CREATE_ONLY_FIELDS are applied on insert only.
	"""
	item_code = required(payload, "item_code")
	item_group = optional(payload, "item_group") or default_item_group
	if not item_group:
		raise MappingError("item_group is required (payload or Commerce Connector Settings)")
	gst_rate = quantity(payload.get("gst_rate", "0"), "gst_rate")
	if gst_rate > 100:
		raise MappingError("gst_rate must be between 0 and 100")
	item_name = required(payload, "item_name")[:140]
	fields = {
		"item_code": item_code,
		"item_name": item_name,
		"description": optional(payload, "description") or item_name,
		"item_group": item_group,
		"stock_uom": required(payload, "stock_uom"),
		"is_stock_item": 0,
		"weight_per_unit": quantity(payload.get("weight_per_unit", "0"), "weight_per_unit"),
		"weight_uom": optional(payload, "weight_uom") or "Kg",
		"platform_product_id": required(payload, "platform_product_id"),
		"platform_gst_rate": gst_rate,
		"disabled": 1 if payload.get("disabled") else 0,
	}
	return fields


# ------------------------------------------------------------------ customer


def customer_from_payload(
	payload: dict,
	*,
	default_customer_group: str | None = None,
	default_territory: str | None = None,
) -> dict:
	"""Map `upsert_customer` payload to {"customer": {...}, "addresses": [...]}."""
	platform_user_id = required(payload, "platform_user_id")
	customer_name = required(payload, "customer_name")[:140]
	gstin = optional(payload, "gstin")
	if gstin:
		gstin = gstin.upper()
		if len(gstin) != 15:
			raise MappingError("gstin must be 15 characters")
	customer = {
		"customer_name": customer_name,
		"customer_type": "Company" if gstin else "Individual",
		"customer_group": optional(payload, "customer_group") or default_customer_group,
		"territory": optional(payload, "territory") or default_territory,
		"platform_user_id": platform_user_id,
		"mobile_no": optional(payload, "phone"),
		"gstin": gstin,
		"gst_category": "Registered Regular" if gstin else "Unregistered",
	}
	addresses = []
	seen_types = set()
	for index, row in enumerate(payload.get("addresses") or []):
		label = f"addresses[{index}]"
		address_type = optional(row, "address_type") or "Billing"
		if address_type not in ("Billing", "Shipping"):
			raise MappingError(f"{label}.address_type must be Billing or Shipping")
		if address_type in seen_types:
			raise MappingError(f"{label}.address_type {address_type} is repeated")
		seen_types.add(address_type)
		addresses.append(
			{
				"address_title": customer_name,
				"address_type": address_type,
				"address_line1": required(row, "line1", f"{label}.line1"),
				"address_line2": optional(row, "line2"),
				"city": required(row, "city", f"{label}.city"),
				"state": required(row, "state", f"{label}.state"),
				"pincode": required(row, "pincode", f"{label}.pincode"),
				"country": optional(row, "country") or "India",
				"phone": optional(payload, "phone"),
				"gstin": gstin,
				"is_primary_address": 1 if address_type == "Billing" else 0,
				"is_shipping_address": 1 if address_type == "Shipping" else 0,
			}
		)
	return {"customer": customer, "addresses": addresses}


# --------------------------------------------------------------- sales order


def sales_order_from_payload(payload: dict, *, customer: str, tax_accounts: dict, item_codes: dict) -> dict:
	"""Map `create_sales_order` payload to a Sales Order dict.

	Returns {"doc": <Sales Order dict>, "net_total", "tax_total", "grand_total"} with
	Decimal totals. Raises MappingError unless lines and taxes sum exactly to the
	payload totals and the tax rows match the supply type.

	`item_codes` maps platform_product_id -> ERPNext Item name. Lines are resolved by
	product id; the SKU and name on a line are frozen at order time and only go into
	the line description.
	"""
	part_id = required(payload, "platform_part_id")
	supply_type = required(payload, "supply_type")
	if supply_type not in SUPPLY_TAX_TYPES:
		raise MappingError("supply_type must be intra or inter")

	transaction_date = iso_date(payload.get("transaction_date"), "transaction_date")
	delivery_date = (
		iso_date(payload["delivery_date"], "delivery_date")
		if payload.get("delivery_date")
		else transaction_date
	)

	lines = payload.get("items") or []
	if not lines:
		raise MappingError("items must not be empty")
	items = []
	net_sum = Decimal("0.00")
	weight_sum = Decimal("0")
	for index, row in enumerate(lines):
		label = f"items[{index}]"
		qty = quantity(row.get("qty"), f"{label}.qty")
		if qty <= 0:
			raise MappingError(f"{label}.qty must be greater than 0")
		rate = line_rate(row.get("rate"), f"{label}.rate")
		amount = money(row.get("amount"), f"{label}.amount")
		if round_paisa(qty * rate) != amount:
			raise MappingError(
				f"{label}.amount {as_rupees(amount)} does not equal qty x rate rounded to the paisa "
				f"({as_rupees(round_paisa(qty * rate))})"
			)
		product_id = required(row, "platform_product_id", f"{label}.platform_product_id")
		item_code = item_codes.get(product_id)
		if not item_code:
			raise MappingError(f"{label}: item for product {product_id} has not been synced yet")
		weight = quantity(row.get("weight_kg", "0"), f"{label}.weight_kg")
		net_sum += amount
		weight_sum += weight
		item = {
			"item_code": item_code,
			"qty": qty,
			"rate": rate,
			"weight_kg": weight,
			"delivery_date": delivery_date,
		}
		description = line_description(optional(row, "item_name"), optional(row, "sku"))
		if description:
			item["description"] = description
		uom = optional(row, "uom")
		if uom:
			item["uom"] = uom
		items.append(item)

	net_total = money(payload.get("net_total"), "net_total")
	tax_total = money(payload.get("tax_total"), "tax_total")
	grand_total = money(payload.get("grand_total"), "grand_total")
	if net_sum != net_total:
		raise MappingError(
			f"item amounts sum to {as_rupees(net_sum)} but net_total is {as_rupees(net_total)}"
		)

	taxes = []
	tax_sum = Decimal("0.00")
	types_seen = set()
	for index, row in enumerate(payload.get("taxes") or []):
		label = f"taxes[{index}]"
		tax_type = required(row, "type", f"{label}.type").upper()
		if tax_type not in SUPPLY_TAX_TYPES[supply_type]:
			raise MappingError(f"{label}.type {tax_type} is not allowed for {supply_type}-state supply")
		if tax_type in types_seen:
			raise MappingError(f"{label}.type {tax_type} is repeated")
		types_seen.add(tax_type)
		account = tax_accounts.get(tax_type)
		if not account:
			raise MappingError(f"no {tax_type} account set in Commerce Connector Settings")
		rate = quantity(row.get("rate", "0"), f"{label}.rate")
		amount = money(row.get("amount"), f"{label}.amount")
		tax_sum += amount
		taxes.append(
			{
				"charge_type": "Actual",
				"account_head": account,
				"description": f"{tax_type} @ {rate.normalize():f}%",
				"tax_amount": amount,
			}
		)
	if tax_sum != tax_total:
		raise MappingError(f"tax rows sum to {as_rupees(tax_sum)} but tax_total is {as_rupees(tax_total)}")
	if tax_total > 0 and types_seen != SUPPLY_TAX_TYPES[supply_type]:
		expected = "+".join(sorted(SUPPLY_TAX_TYPES[supply_type]))
		raise MappingError(f"{supply_type}-state supply needs {expected} tax rows")
	if net_total + tax_total != grand_total:
		raise MappingError(
			f"net_total + tax_total is {as_rupees(net_total + tax_total)} but grand_total is {as_rupees(grand_total)}"
		)

	total_weight = quantity(payload.get("total_weight_kg", "0"), "total_weight_kg")
	if total_weight != weight_sum:
		raise MappingError(f"item weights sum to {weight_sum} but total_weight_kg is {total_weight}")

	doc = {
		"doctype": "Sales Order",
		"company": required(payload, "company"),
		"customer": customer,
		"order_type": "Sales",
		"transaction_date": transaction_date,
		"delivery_date": delivery_date,
		"po_no": required(payload, "platform_order_number"),
		"ignore_pricing_rule": 1,
		"disable_rounded_total": 1,
		"platform_order_id": required(payload, "platform_order_id"),
		"platform_part_id": part_id,
		"platform_order_number": required(payload, "platform_order_number"),
		"platform_dealer_id": required(payload, "platform_dealer_id"),
		"dealer_name": required(payload, "dealer_name"),
		"dealer_gstin": optional(payload, "dealer_gstin"),
		"total_weight_kg": total_weight,
		"platform_status": "Paid",
		"items": items,
		"taxes": taxes,
	}
	place_of_supply = optional(payload, "place_of_supply")
	if place_of_supply:
		doc["place_of_supply"] = place_of_supply
	return {"doc": doc, "net_total": net_total, "tax_total": tax_total, "grand_total": grand_total}


def line_description(item_name: str | None, sku: str | None) -> str | None:
	"""Frozen name and SKU from the order, kept on the line for reference only."""
	if item_name and sku:
		return f"{item_name} (SKU {sku})"
	if sku:
		return f"SKU {sku}"
	return item_name


def line_product_ids(payload: dict) -> list[str]:
	"""Distinct platform product ids on the order lines, for the item lookup."""
	ids = []
	for row in payload.get("items") or []:
		product_id = optional(row, "platform_product_id") if isinstance(row, dict) else None
		if product_id and product_id not in ids:
			ids.append(product_id)
	return ids


def status_update(status: str, invoice_no: str | None = None, invoice_date: str | None = None) -> dict:
	"""Fields to set on a submitted Sales Order for `update_sales_order_status`."""
	key = (status or "").strip().lower()
	if key not in STATUS_LABELS:
		raise MappingError(f"status must be one of: {', '.join(STATUS_LABELS)}")
	fields = {"platform_status": STATUS_LABELS[key]}
	if invoice_no and str(invoice_no).strip():
		fields["dealer_invoice_no"] = str(invoice_no).strip()
	if invoice_date:
		fields["dealer_invoice_date"] = iso_date(invoice_date, "invoice_date")
	return fields


# ----------------------------------------------------------------------- APD


def apd_account_name(architect_name: str, platform_architect_id: str) -> str:
	"""`Architect Privilege Discount - {architect_name} - {platform_architect_id[:8]}`."""
	name = _UNSAFE_NAME_CHARS.sub("", architect_name or "")
	name = " ".join(name.split())[:80]
	short_id = (platform_architect_id or "").strip()[:8]
	if not name:
		raise MappingError("architect_name is required")
	if not short_id:
		raise MappingError("platform_architect_id is required")
	return f"{APD_ACCOUNT_PREFIX} - {name} - {short_id}"


def apd_entry(payload: dict) -> dict:
	"""Validate an `apd_journal` payload and return a normalised entry."""
	action = required(payload, "action")
	if action not in APD_ACTIONS:
		raise MappingError(f"action must be one of: {', '.join(APD_ACTIONS)}")
	ref_key = "platform_payout_id" if action == "payout" else "platform_apd_id"
	ref_id = required(payload, ref_key)
	amount = money(payload.get("amount"), "amount")
	if amount <= 0:
		raise MappingError("amount must be greater than 0")
	entry = {
		"action": action,
		"platform_ref": f"apd.{action}:{ref_id}",
		"platform_architect_id": required(payload, "platform_architect_id"),
		"architect_name": required(payload, "architect_name"),
		"amount": amount,
		"posting_date": iso_date(payload.get("posting_date"), "posting_date"),
		"company": required(payload, "company"),
		"platform_order_number": optional(payload, "platform_order_number"),
		"utr": None,
	}
	if action == "payout":
		entry["utr"] = required(payload, "utr")
	return entry


def apd_journal(
	entry: dict,
	*,
	apd_account: str,
	expense_account: str | None = None,
	bank_account: str | None = None,
	cost_center: str | None = None,
) -> dict:
	"""Journal Entry dict for an APD accrual, void or payout.

	accrue: Dr expense / Cr architect liability
	void:   Dr architect liability / Cr expense (reverses the accrual)
	payout: Dr architect liability / Cr bank, UTR as cheque reference
	"""
	action = entry["action"]
	amount = entry["amount"]
	zero = Decimal("0.00")

	def row(account, debit, credit, with_cost_center=False):
		data = {
			"account": account,
			"debit_in_account_currency": debit,
			"credit_in_account_currency": credit,
		}
		if with_cost_center and cost_center:
			data["cost_center"] = cost_center
		return data

	reference = entry.get("platform_order_number")
	if action in ("accrue", "void"):
		if not expense_account:
			raise MappingError("APD expense account is not set in Commerce Connector Settings")
		if action == "accrue":
			rows = [row(expense_account, amount, zero, True), row(apd_account, zero, amount)]
			remark = "Architect Privilege Discount accrued"
		else:
			rows = [row(apd_account, amount, zero), row(expense_account, zero, amount, True)]
			remark = "Architect Privilege Discount accrual reversed"
		voucher_type = "Journal Entry"
	else:
		if not bank_account:
			raise MappingError("bank account is not set in Commerce Connector Settings")
		rows = [row(apd_account, amount, zero), row(bank_account, zero, amount)]
		remark = "Architect Privilege Discount paid out"
		voucher_type = "Bank Entry"

	if reference:
		remark = f"{remark} (order {reference})"
	doc = {
		"doctype": "Journal Entry",
		"voucher_type": voucher_type,
		"company": entry["company"],
		"posting_date": entry["posting_date"],
		"user_remark": remark,
		"platform_ref": entry["platform_ref"],
		"accounts": rows,
	}
	if action == "payout":
		doc["cheque_no"] = entry["utr"]
		doc["cheque_date"] = entry["posting_date"]
	return doc


# ------------------------------------------------------------------ webhooks


def erp_event(
	*,
	doctype: str,
	name: str,
	sales_order: str,
	platform_order_id: str,
	platform_part_id: str,
	posting_date: str | None,
	occurred_at: str,
	grand_total=None,
) -> dict:
	"""Webhook event for a submitted Sales Invoice or Delivery Note.

	The event id is deterministic so a redelivery dedupes on the platform.
	"""
	event = ERP_EVENTS.get(doctype)
	if not event:
		raise MappingError(f"no webhook event for {doctype}")
	event_id = str(uuid.uuid5(_EVENT_NAMESPACE, f"{event}:{name}:{platform_part_id}"))
	data = {
		"platform_order_id": platform_order_id,
		"platform_part_id": platform_part_id,
		"sales_order": sales_order,
		"erp_doctype": doctype,
		"erp_name": name,
		"posting_date": posting_date,
	}
	if grand_total is not None:
		data["grand_total"] = as_rupees(Decimal(str(grand_total)))
	return {"event_id": event_id, "event": event, "occurred_at": occurred_at, "data": data}
