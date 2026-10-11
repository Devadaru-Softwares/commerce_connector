"""Whitelisted methods called by the commerce platform.

Every method is idempotent by platform id: a repeat call returns the existing
document instead of creating another one. Call as
POST /api/method/commerce_connector.api.<method> with token auth.
"""

from decimal import Decimal

import frappe
from frappe import _

from commerce_connector import mapping
from commerce_connector.mapping import MappingError

API_ROLES = ("Commerce Connector", "System Manager")
SETTINGS = "Commerce Connector Settings"


# ------------------------------------------------------------------- helpers


def _guard():
	frappe.only_for(API_ROLES)
	settings = frappe.get_cached_doc(SETTINGS)
	if not settings.enabled:
		frappe.throw(_("Commerce Connector is disabled"), frappe.ValidationError)
	return settings


def _parse(payload) -> dict:
	data = frappe.parse_json(payload) if payload is not None else None
	if not isinstance(data, dict):
		frappe.throw(_("payload must be a JSON object"), frappe.ValidationError)
	return data


def _run(fn, *args, **kwargs):
	try:
		return fn(*args, **kwargs)
	except MappingError as e:
		frappe.throw(str(e), frappe.ValidationError)


def _to_frappe(value):
	"""Decimal -> float at the document boundary; Frappe numeric fields are floats."""
	if isinstance(value, Decimal):
		return float(value)
	if isinstance(value, dict):
		return {k: _to_frappe(v) for k, v in value.items()}
	if isinstance(value, list):
		return [_to_frappe(v) for v in value]
	return value


def _known_fields(doctype: str, data: dict) -> dict:
	"""Drop fields that this site does not have (e.g. India Compliance fields)."""
	meta = frappe.get_meta(doctype)
	return {k: v for k, v in data.items() if k == "doctype" or meta.has_field(k)}


def _find(doctype: str, filters: dict) -> str | None:
	return frappe.db.get_value(doctype, filters, "name", order_by="creation desc")


def _result(doc, created: bool, **extra) -> dict:
	return {"name": doc.name, "doctype": doc.doctype, "docstatus": doc.docstatus, "created": created, **extra}


def _ensure_uom(uom: str):
	if uom and not frappe.db.exists("UOM", uom):
		frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)


# ---------------------------------------------------------------------- item


@frappe.whitelist(methods=["POST"])
def upsert_item(payload):
	settings = _guard()
	data = _parse(payload)
	fields = _run(mapping.item_from_payload, data, default_item_group=settings.default_item_group)
	fields = _to_frappe(fields)

	# Items are keyed by platform_product_id; the SKU can change on the platform and
	# item_code is never renamed. An existing unlinked Item with the same code is adopted.
	name = _find("Item", {"platform_product_id": fields["platform_product_id"]}) or _find(
		"Item", {"item_code": fields["item_code"], "platform_product_id": ["in", ["", None]]}
	)
	if name:
		doc = frappe.get_doc("Item", name)
		updates = {k: v for k, v in fields.items() if k not in mapping.ITEM_CREATE_ONLY_FIELDS}
		doc.update(_known_fields("Item", updates))
		doc.save(ignore_permissions=True)
		return _result(doc, False, item_code=doc.item_code)

	_ensure_uom(fields["stock_uom"])
	doc = frappe.get_doc({"doctype": "Item", **_known_fields("Item", fields)})
	doc.insert(ignore_permissions=True)
	return _result(doc, True, item_code=doc.item_code)


# ------------------------------------------------------------------ customer


@frappe.whitelist(methods=["POST"])
def upsert_customer(payload):
	settings = _guard()
	data = _parse(payload)
	mapped = _run(
		mapping.customer_from_payload,
		data,
		default_customer_group=settings.default_customer_group,
		default_territory=settings.default_territory,
	)
	fields = {k: v for k, v in mapped["customer"].items() if v is not None}

	name = _find("Customer", {"platform_user_id": fields["platform_user_id"]})
	if name:
		doc = frappe.get_doc("Customer", name)
		# mobile_no on an existing customer belongs to its primary contact; do not overwrite.
		updates = {k: v for k, v in fields.items() if k not in ("mobile_no", "customer_group", "territory")}
		doc.update(_known_fields("Customer", updates))
		doc.save(ignore_permissions=True)
		created = False
	else:
		doc = frappe.get_doc({"doctype": "Customer", **_known_fields("Customer", fields)})
		doc.insert(ignore_permissions=True)
		created = True

	for address in mapped["addresses"]:
		_upsert_address(doc.name, address)
	return _result(doc, created)


def _upsert_address(customer: str, address: dict):
	existing = frappe.db.sql(
		"""
		select a.name from `tabAddress` a
		join `tabDynamic Link` l on l.parent = a.name and l.parenttype = 'Address'
		where l.link_doctype = 'Customer' and l.link_name = %s and a.address_type = %s
		order by a.creation desc limit 1
		""",
		(customer, address["address_type"]),
	)
	values = _known_fields("Address", {k: v for k, v in address.items() if v is not None})
	if existing:
		doc = frappe.get_doc("Address", existing[0][0])
		doc.update(values)
		doc.save(ignore_permissions=True)
		return doc
	doc = frappe.get_doc({"doctype": "Address", **values})
	doc.append("links", {"link_doctype": "Customer", "link_name": customer})
	doc.insert(ignore_permissions=True)
	return doc


# --------------------------------------------------------------- sales order


@frappe.whitelist(methods=["POST"])
def create_sales_order(payload):
	settings = _guard()
	data = _parse(payload)
	part_id = data.get("platform_part_id")
	if not part_id:
		frappe.throw(_("platform_part_id is required"), frappe.ValidationError)

	existing = _find("Sales Order", {"platform_part_id": part_id, "docstatus": ["!=", 2]}) or _find(
		"Sales Order", {"platform_part_id": part_id}
	)
	if existing:
		doc = frappe.get_doc("Sales Order", existing)
		return _result(doc, False, grand_total=mapping.as_rupees(Decimal(str(doc.grand_total))))

	customer_user = data.get("customer_platform_user_id")
	customer = customer_user and _find("Customer", {"platform_user_id": customer_user})
	if not customer:
		frappe.throw(_("Customer for this order has not been synced yet"), frappe.ValidationError)

	tax_accounts = {
		"CGST": settings.cgst_account,
		"SGST": settings.sgst_account,
		"IGST": settings.igst_account,
	}
	product_ids = mapping.line_product_ids(data)
	item_codes = {
		row.platform_product_id: row.name
		for row in frappe.get_all(
			"Item",
			filters={"platform_product_id": ["in", product_ids or [""]]},
			fields=["name", "platform_product_id"],
		)
	}
	plan = _run(
		mapping.sales_order_from_payload,
		data,
		customer=customer,
		tax_accounts=tax_accounts,
		item_codes=item_codes,
	)

	so = frappe.get_doc(_to_frappe(_known_fields("Sales Order", plan["doc"])))
	for address_type, field in (("Billing", "customer_address"), ("Shipping", "shipping_address_name")):
		address = _customer_address(customer, address_type)
		if address:
			so.set(field, address)
	so.flags.ignore_permissions = True
	so.insert(ignore_permissions=True)

	# Tolerance 0: ERPNext must not have recalculated anything differently. Checked
	# before and after submit; a failure raises and the request transaction rolls back.
	_assert_totals(plan, so, part_id)
	so.submit()
	_assert_totals(plan, so, part_id)
	return _result(so, True, grand_total=mapping.as_rupees(Decimal(str(so.grand_total))))


def _assert_totals(plan: dict, so, part_id: str):
	context = f"part {part_id}"
	_run(mapping.assert_total, "Net total", plan["net_total"], so.net_total, context)
	_run(mapping.assert_total, "Tax total", plan["tax_total"], so.total_taxes_and_charges, context)
	_run(mapping.assert_total, "Grand total", plan["grand_total"], so.grand_total, context)


def _customer_address(customer: str, address_type: str) -> str | None:
	rows = frappe.db.sql(
		"""
		select a.name from `tabAddress` a
		join `tabDynamic Link` l on l.parent = a.name and l.parenttype = 'Address'
		where l.link_doctype = 'Customer' and l.link_name = %s and a.address_type = %s and a.disabled = 0
		order by a.creation desc limit 1
		""",
		(customer, address_type),
	)
	return rows[0][0] if rows else None


@frappe.whitelist(methods=["POST"])
def cancel_sales_order(platform_part_id):
	_guard()
	name = _find("Sales Order", {"platform_part_id": platform_part_id, "docstatus": ["!=", 2]})
	if not name:
		cancelled = _find("Sales Order", {"platform_part_id": platform_part_id})
		if cancelled:
			doc = frappe.get_doc("Sales Order", cancelled)
			return _result(doc, False, status="cancelled")
		# A refund after a failed create must not error forever.
		return {"name": None, "doctype": "Sales Order", "status": "not_found", "created": False}

	doc = frappe.get_doc("Sales Order", name)
	if doc.docstatus == 0:
		# Never left in draft by create_sales_order, but a manual draft must not block the refund.
		frappe.delete_doc("Sales Order", name, ignore_permissions=True)
		return {"name": name, "doctype": "Sales Order", "status": "deleted_draft", "created": False}
	doc.flags.ignore_permissions = True
	doc.db_set("platform_status", "Cancelled")
	doc.cancel()
	return _result(doc, False, status="cancelled")


@frappe.whitelist(methods=["POST"])
def update_sales_order_status(platform_part_id, status, invoice_no=None, invoice_date=None):
	_guard()
	fields = _run(mapping.status_update, status, invoice_no, invoice_date)
	name = _find("Sales Order", {"platform_part_id": platform_part_id, "docstatus": 1})
	if not name:
		frappe.throw(_("No submitted Sales Order for this part"), frappe.DoesNotExistError)
	doc = frappe.get_doc("Sales Order", name)
	changed = {k: v for k, v in fields.items() if str(doc.get(k) or "") != str(v)}
	if changed:
		doc.db_set(changed)
	return _result(doc, False, platform_status=fields["platform_status"])


# ----------------------------------------------------------------------- APD


@frappe.whitelist(methods=["POST"])
def apd_journal(payload):
	settings = _guard()
	data = _parse(payload)
	entry = _run(mapping.apd_entry, data)

	existing = _find("Journal Entry", {"platform_ref": entry["platform_ref"], "docstatus": 1})
	if existing:
		return _result(frappe.get_doc("Journal Entry", existing), False, platform_ref=entry["platform_ref"])

	apd_account = _ensure_apd_account(settings, entry)
	je = _run(
		mapping.apd_journal,
		entry,
		apd_account=apd_account,
		expense_account=settings.apd_expense_account,
		bank_account=settings.bank_account,
		cost_center=frappe.get_cached_value("Company", entry["company"], "cost_center"),
	)
	doc = frappe.get_doc(_to_frappe(_known_fields("Journal Entry", je)))
	doc.insert(ignore_permissions=True)
	doc.submit()
	return _result(doc, True, platform_ref=entry["platform_ref"], account=apd_account)


def _ensure_apd_account(settings, entry: dict) -> str:
	company = entry["company"]
	architect_id = entry["platform_architect_id"]
	name = _find("Account", {"platform_architect_id": architect_id, "company": company})
	if name:
		return name

	account_name = _run(mapping.apd_account_name, entry["architect_name"], architect_id)
	name = _find("Account", {"account_name": account_name, "company": company})
	if name:
		frappe.db.set_value("Account", name, "platform_architect_id", architect_id)
		return name

	parent = settings.apd_parent_account
	if not parent:
		frappe.throw(_("APD parent liability account is not set in Commerce Connector Settings"))
	if frappe.db.get_value("Account", parent, "company") != company:
		frappe.throw(_("APD parent account belongs to another company"))
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"parent_account": parent,
			"company": company,
			"is_group": 0,
			"platform_architect_id": architect_id,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name
