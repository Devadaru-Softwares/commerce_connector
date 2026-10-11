import copy
from decimal import Decimal

import pytest

from commerce_connector import mapping
from commerce_connector.mapping import MappingError

TAX_ACCOUNTS = {
	"CGST": "Output Tax CGST - DS",
	"SGST": "Output Tax SGST - DS",
	"IGST": "Output Tax IGST - DS",
}

# ERPNext Item names keyed by platform_product_id. The order line's SKU is frozen at
# order time and may differ from the current item code (prod-1 was re-SKUed).
ITEM_CODES = {"prod-1": "TL-600-GLOSS", "prod-2": "TL-300-MT", "prod-3": "TL-SLAB"}


def intra_payload():
	return {
		"platform_order_id": "8d0c5d7e-0000-4000-8000-000000000001",
		"platform_part_id": "8d0c5d7e-0000-4000-8000-0000000000a1",
		"platform_order_number": "ORD-1042",
		"platform_dealer_id": "8d0c5d7e-0000-4000-8000-0000000000d1",
		"dealer_name": "Kochi Tiles",
		"dealer_gstin": "32ABCDE1234F1Z5",
		"company": "Devadaru Softwares",
		"customer_platform_user_id": "8d0c5d7e-0000-4000-8000-0000000000c1",
		"transaction_date": "2026-10-11",
		"supply_type": "intra",
		"total_weight_kg": "152.500",
		"items": [
			{
				"platform_product_id": "prod-1",
				"sku": "TL-600-GL",
				"item_name": "Glossy 600x600",
				"qty": "10",
				"rate": "1234.500000",
				"amount": "12345.00",
				"weight_kg": "140.000",
			},
			{
				"platform_product_id": "prod-2",
				"qty": "3",
				"rate": "333.330000",
				"amount": "999.99",
				"weight_kg": "12.500",
			},
		],
		"taxes": [
			{"type": "CGST", "rate": "9", "amount": "1201.00"},
			{"type": "SGST", "rate": "9", "amount": "1201.00"},
		],
		"net_total": "13344.99",
		"tax_total": "2402.00",
		"grand_total": "15746.99",
	}


def inter_payload():
	payload = intra_payload()
	payload["supply_type"] = "inter"
	payload["taxes"] = [{"type": "IGST", "rate": "18", "amount": "2402.00"}]
	return payload


# ------------------------------------------------------------------ sales order


def test_intra_sales_order_maps_fields_items_taxes_and_weights():
	result = mapping.sales_order_from_payload(
		intra_payload(), customer="CUST-0001", tax_accounts=TAX_ACCOUNTS, item_codes=ITEM_CODES
	)
	doc = result["doc"]
	assert doc["doctype"] == "Sales Order"
	assert doc["customer"] == "CUST-0001"
	assert doc["company"] == "Devadaru Softwares"
	assert doc["transaction_date"] == "2026-10-11"
	assert doc["delivery_date"] == "2026-10-11"
	assert doc["ignore_pricing_rule"] == 1
	assert doc["disable_rounded_total"] == 1
	assert doc["platform_part_id"] == "8d0c5d7e-0000-4000-8000-0000000000a1"
	assert doc["platform_order_number"] == "ORD-1042"
	assert doc["po_no"] == "ORD-1042"
	assert doc["dealer_name"] == "Kochi Tiles"
	assert doc["dealer_gstin"] == "32ABCDE1234F1Z5"
	assert doc["platform_status"] == "Paid"
	assert doc["total_weight_kg"] == Decimal("152.500")

	assert doc["items"] == [
		{
			"item_code": "TL-600-GLOSS",
			"qty": Decimal("10"),
			"rate": Decimal("1234.500000"),
			"weight_kg": Decimal("140.000"),
			"delivery_date": "2026-10-11",
			"description": "Glossy 600x600 (SKU TL-600-GL)",
		},
		{
			"item_code": "TL-300-MT",
			"qty": Decimal("3"),
			"rate": Decimal("333.330000"),
			"weight_kg": Decimal("12.500"),
			"delivery_date": "2026-10-11",
		},
	]
	assert doc["taxes"] == [
		{
			"charge_type": "Actual",
			"account_head": "Output Tax CGST - DS",
			"description": "CGST @ 9%",
			"tax_amount": Decimal("1201.00"),
		},
		{
			"charge_type": "Actual",
			"account_head": "Output Tax SGST - DS",
			"description": "SGST @ 9%",
			"tax_amount": Decimal("1201.00"),
		},
	]
	assert result["net_total"] == Decimal("13344.99")
	assert result["tax_total"] == Decimal("2402.00")
	assert result["grand_total"] == Decimal("15746.99")
	assert sum(i["qty"] * i["rate"] for i in doc["items"]) + sum(
		t["tax_amount"] for t in doc["taxes"]
	) == Decimal("15746.99")


def test_inter_sales_order_uses_igst():
	doc = mapping.sales_order_from_payload(
		inter_payload(), customer="C", tax_accounts=TAX_ACCOUNTS, item_codes=ITEM_CODES
	)["doc"]
	assert [t["account_head"] for t in doc["taxes"]] == ["Output Tax IGST - DS"]
	assert doc["taxes"][0]["description"] == "IGST @ 18%"
	assert doc["taxes"][0]["tax_amount"] == Decimal("2402.00")


def test_optional_fields_delivery_date_uom_and_place_of_supply():
	payload = intra_payload()
	payload["delivery_date"] = "2026-10-20"
	payload["place_of_supply"] = "32-Kerala"
	payload["items"][0]["uom"] = "Box"
	doc = mapping.sales_order_from_payload(
		payload, customer="C", tax_accounts=TAX_ACCOUNTS, item_codes=ITEM_CODES
	)["doc"]
	assert doc["delivery_date"] == "2026-10-20"
	assert doc["items"][0]["delivery_date"] == "2026-10-20"
	assert doc["items"][0]["uom"] == "Box"
	assert "uom" not in doc["items"][1]
	assert doc["place_of_supply"] == "32-Kerala"


def test_zero_tax_order_needs_no_tax_rows():
	payload = intra_payload()
	payload["taxes"] = []
	payload["tax_total"] = "0.00"
	payload["grand_total"] = "13344.99"
	result = mapping.sales_order_from_payload(
		payload, customer="C", tax_accounts=TAX_ACCOUNTS, item_codes=ITEM_CODES
	)
	assert result["doc"]["taxes"] == []
	assert result["grand_total"] == Decimal("13344.99")


@pytest.mark.parametrize(
	("mutate", "message"),
	[
		(lambda p: p.update(grand_total="15747.00"), "grand_total"),
		(lambda p: p.update(net_total="13345.00"), "net_total"),
		(lambda p: p.update(tax_total="2402.01"), "tax_total"),
		(lambda p: p["items"][0].update(amount="12345.01"), "qty x rate"),
		(lambda p: p["items"][0].update(rate="1234.501000"), "qty x rate"),
		(lambda p: p["items"][0].update(rate=1234.5), "6 decimals"),
		(lambda p: p["items"][0].update(rate="1234.50"), "6 decimals"),
		(lambda p: p["items"][0].update(rate="1234.5000000"), "6 decimals"),
		(lambda p: p["items"][0].pop("platform_product_id"), "platform_product_id"),
		(lambda p: p["items"][0].update(platform_product_id="prod-unknown"), "not been synced"),
		(lambda p: p["items"][0].update(qty="0"), "greater than 0"),
		(lambda p: p["items"][0].update(qty=10.0), "decimal string"),
		(lambda p: p.update(total_weight_kg="150.000"), "weights"),
		(lambda p: p.update(supply_type="export"), "supply_type"),
		(lambda p: p["taxes"][0].update(type="IGST"), "not allowed"),
		(lambda p: p["taxes"][1].update(type="CGST"), "repeated"),
		(lambda p: p.update(items=[]), "items"),
		(lambda p: p.update(transaction_date="11/10/2026"), "transaction_date"),
		(lambda p: p.pop("platform_part_id"), "platform_part_id"),
		(lambda p: p.pop("company"), "company"),
	],
)
def test_sales_order_contract_violations_raise(mutate, message):
	payload = intra_payload()
	mutate(payload)
	with pytest.raises(MappingError, match=message):
		mapping.sales_order_from_payload(
			payload, customer="C", tax_accounts=TAX_ACCOUNTS, item_codes=ITEM_CODES
		)


def test_intra_needs_both_cgst_and_sgst():
	payload = intra_payload()
	payload["taxes"] = [{"type": "CGST", "rate": "18", "amount": "2402.00"}]
	with pytest.raises(MappingError, match="CGST\\+SGST"):
		mapping.sales_order_from_payload(
			payload, customer="C", tax_accounts=TAX_ACCOUNTS, item_codes=ITEM_CODES
		)


def test_missing_tax_account_raises():
	with pytest.raises(MappingError, match="IGST account"):
		mapping.sales_order_from_payload(
			inter_payload(), customer="C", tax_accounts={"CGST": "x", "SGST": "y"}, item_codes=ITEM_CODES
		)


def test_mapping_does_not_mutate_payload():
	payload = intra_payload()
	before = copy.deepcopy(payload)
	mapping.sales_order_from_payload(payload, customer="C", tax_accounts=TAX_ACCOUNTS, item_codes=ITEM_CODES)
	assert payload == before


def test_assert_total_has_zero_tolerance():
	mapping.assert_total("grand_total", Decimal("15746.99"), 15746.99, "part x")
	mapping.assert_total("grand_total", Decimal("15746.99"), 15746.990000000002, "part x")
	with pytest.raises(MappingError, match=r"ERPNext computed 15747\.00, platform total is 15746\.99"):
		mapping.assert_total("grand_total", Decimal("15746.99"), 15747.0, "part x")
	with pytest.raises(MappingError):
		mapping.assert_total("grand_total", Decimal("1.00"), 0.99, "part x")


def test_status_update():
	assert mapping.status_update("dispatched") == {"platform_status": "Dispatched"}
	assert mapping.status_update("Delivered", "INV/26/001", "2026-10-12") == {
		"platform_status": "Delivered",
		"dealer_invoice_no": "INV/26/001",
		"dealer_invoice_date": "2026-10-12",
	}
	with pytest.raises(MappingError, match="status"):
		mapping.status_update("paid")
	with pytest.raises(MappingError, match="invoice_date"):
		mapping.status_update("delivered", "X", "12-10-2026")


# ------------------------------------------------------------------ item / customer


def test_item_mapping():
	fields = mapping.item_from_payload(
		{
			"platform_product_id": "p-1",
			"item_code": "TL-600-GL",
			"item_name": "Glossy 600x600",
			"stock_uom": "Box",
			"weight_per_unit": "14.000",
			"gst_rate": "18",
		},
		default_item_group="Tiles",
	)
	assert fields == {
		"item_code": "TL-600-GL",
		"item_name": "Glossy 600x600",
		"description": "Glossy 600x600",
		"item_group": "Tiles",
		"stock_uom": "Box",
		"is_stock_item": 0,
		"weight_per_unit": Decimal("14.000"),
		"weight_uom": "Kg",
		"platform_product_id": "p-1",
		"platform_gst_rate": Decimal("18"),
		"disabled": 0,
	}
	assert set(mapping.ITEM_CREATE_ONLY_FIELDS) <= set(fields)


def test_item_archived_is_disabled_and_group_required():
	base = {
		"platform_product_id": "p",
		"item_code": "S",
		"item_name": "N",
		"stock_uom": "Box",
		"disabled": True,
	}
	assert mapping.item_from_payload(base, default_item_group="G")["disabled"] == 1
	with pytest.raises(MappingError, match="item_group"):
		mapping.item_from_payload(base)
	with pytest.raises(MappingError, match="gst_rate"):
		mapping.item_from_payload({**base, "gst_rate": "180"}, default_item_group="G")


def test_customer_mapping_with_addresses():
	result = mapping.customer_from_payload(
		{
			"platform_user_id": "u-1",
			"customer_name": "Anu Joseph",
			"phone": "+919800000000",
			"gstin": "32abcde1234f1z5",
			"addresses": [
				{
					"address_type": "Billing",
					"line1": "1 MG Road",
					"city": "Kochi",
					"state": "Kerala",
					"pincode": "682001",
				},
				{
					"address_type": "Shipping",
					"line1": "Site 4",
					"city": "Thrissur",
					"state": "Kerala",
					"pincode": "680001",
				},
			],
		},
		default_customer_group="Individual",
		default_territory="India",
	)
	customer = result["customer"]
	assert customer["platform_user_id"] == "u-1"
	assert customer["customer_type"] == "Company"
	assert customer["gstin"] == "32ABCDE1234F1Z5"
	assert customer["gst_category"] == "Registered Regular"
	assert customer["mobile_no"] == "+919800000000"
	assert customer["customer_group"] == "Individual"
	assert [a["address_type"] for a in result["addresses"]] == ["Billing", "Shipping"]
	assert result["addresses"][1]["is_shipping_address"] == 1
	assert result["addresses"][0]["country"] == "India"


def test_customer_without_gstin_is_unregistered_individual():
	result = mapping.customer_from_payload({"platform_user_id": "u", "customer_name": "A"})
	assert result["customer"]["customer_type"] == "Individual"
	assert result["customer"]["gst_category"] == "Unregistered"
	assert result["addresses"] == []
	with pytest.raises(MappingError, match="gstin"):
		mapping.customer_from_payload({"platform_user_id": "u", "customer_name": "A", "gstin": "123"})


# ----------------------------------------------------------------------------- APD


def apd_payload(action="accrue", **extra):
	payload = {
		"action": action,
		"platform_apd_id": "a-1",
		"platform_architect_id": "3f2a9c1e-77aa-4bbb-8ccc-000000000001",
		"architect_name": "Meera Nair",
		"amount": "500.00",
		"posting_date": "2026-10-11",
		"company": "Devadaru Softwares",
		"platform_order_number": "ORD-1042",
	}
	payload.update(extra)
	return payload


ACCOUNTS = {
	"apd_account": "Architect Privilege Discount - Meera Nair - 3f2a9c1e - DS",
	"expense_account": "Architect Privilege Discount Expense - DS",
	"bank_account": "HDFC Current - DS",
	"cost_center": "Main - DS",
}


def test_apd_account_name():
	assert (
		mapping.apd_account_name("Meera  Nair", "3f2a9c1e-77aa-4bbb-8ccc-000000000001")
		== "Architect Privilege Discount - Meera Nair - 3f2a9c1e"
	)
	assert mapping.apd_account_name("A<b>#c", "12345678") == "Architect Privilege Discount - Abc - 12345678"
	with pytest.raises(MappingError):
		mapping.apd_account_name("  ", "12345678")


def test_apd_accrue_rows():
	entry = mapping.apd_entry(apd_payload())
	assert entry["platform_ref"] == "apd.accrue:a-1"
	doc = mapping.apd_journal(entry, **ACCOUNTS)
	assert doc["voucher_type"] == "Journal Entry"
	assert doc["platform_ref"] == "apd.accrue:a-1"
	assert doc["posting_date"] == "2026-10-11"
	assert doc["user_remark"] == "Architect Privilege Discount accrued (order ORD-1042)"
	assert doc["accounts"] == [
		{
			"account": "Architect Privilege Discount Expense - DS",
			"debit_in_account_currency": Decimal("500.00"),
			"credit_in_account_currency": Decimal("0.00"),
			"cost_center": "Main - DS",
		},
		{
			"account": "Architect Privilege Discount - Meera Nair - 3f2a9c1e - DS",
			"debit_in_account_currency": Decimal("0.00"),
			"credit_in_account_currency": Decimal("500.00"),
		},
	]


def test_apd_void_reverses_accrual():
	doc = mapping.apd_journal(mapping.apd_entry(apd_payload("void")), **ACCOUNTS)
	assert doc["platform_ref"] == "apd.void:a-1"
	debit, credit = doc["accounts"]
	assert debit["account"] == ACCOUNTS["apd_account"]
	assert debit["debit_in_account_currency"] == Decimal("500.00")
	assert credit["account"] == ACCOUNTS["expense_account"]
	assert credit["credit_in_account_currency"] == Decimal("500.00")


def test_apd_payout_credits_bank_with_utr():
	payload = apd_payload("payout", platform_payout_id="po-9", utr="UTR123456")
	del payload["platform_apd_id"]
	doc = mapping.apd_journal(mapping.apd_entry(payload), **ACCOUNTS)
	assert doc["voucher_type"] == "Bank Entry"
	assert doc["platform_ref"] == "apd.payout:po-9"
	assert doc["cheque_no"] == "UTR123456"
	assert doc["cheque_date"] == "2026-10-11"
	debit, credit = doc["accounts"]
	assert debit == {
		"account": ACCOUNTS["apd_account"],
		"debit_in_account_currency": Decimal("500.00"),
		"credit_in_account_currency": Decimal("0.00"),
	}
	assert credit["account"] == "HDFC Current - DS"
	assert credit["credit_in_account_currency"] == Decimal("500.00")


def test_apd_journal_rows_balance():
	for action, extra in (("accrue", {}), ("void", {}), ("payout", {"platform_payout_id": "p", "utr": "U"})):
		doc = mapping.apd_journal(mapping.apd_entry(apd_payload(action, **extra)), **ACCOUNTS)
		debits = sum(r["debit_in_account_currency"] for r in doc["accounts"])
		credits = sum(r["credit_in_account_currency"] for r in doc["accounts"])
		assert debits == credits == Decimal("500.00")


@pytest.mark.parametrize(
	("payload", "message"),
	[
		(apd_payload("refund"), "action"),
		(apd_payload(amount="0.00"), "greater than 0"),
		(apd_payload(amount="500"), "rupee string"),
		(apd_payload("payout", platform_payout_id="p"), "utr"),
		(apd_payload("payout", utr="U"), "platform_payout_id"),
	],
)
def test_apd_contract_violations(payload, message):
	with pytest.raises(MappingError, match=message):
		mapping.apd_entry(payload)


def test_apd_missing_settings_accounts():
	entry = mapping.apd_entry(apd_payload())
	with pytest.raises(MappingError, match="expense account"):
		mapping.apd_journal(entry, apd_account="A")
	payout = mapping.apd_entry(apd_payload("payout", platform_payout_id="p", utr="U"))
	with pytest.raises(MappingError, match="bank account"):
		mapping.apd_journal(payout, apd_account="A")


# ------------------------------------------------------------------------ events


def test_erp_event_is_deterministic():
	kwargs = {
		"doctype": "Sales Invoice",
		"name": "ACC-SINV-2026-00001",
		"sales_order": "SAL-ORD-2026-00001",
		"platform_order_id": "o-1",
		"platform_part_id": "p-1",
		"posting_date": "2026-10-12",
		"occurred_at": "2026-10-12T10:00:00Z",
		"grand_total": 15746.99,
	}
	first = mapping.erp_event(**kwargs)
	second = mapping.erp_event(**{**kwargs, "occurred_at": "2026-10-12T11:00:00Z"})
	assert first["event_id"] == second["event_id"]
	assert first["event"] == "sales_invoice.submitted"
	assert first["data"] == {
		"platform_order_id": "o-1",
		"platform_part_id": "p-1",
		"sales_order": "SAL-ORD-2026-00001",
		"erp_doctype": "Sales Invoice",
		"erp_name": "ACC-SINV-2026-00001",
		"posting_date": "2026-10-12",
		"grand_total": "15746.99",
	}
	dn = mapping.erp_event(**{**kwargs, "doctype": "Delivery Note", "grand_total": None})
	assert dn["event"] == "delivery_note.submitted"
	assert dn["event_id"] != first["event_id"]
	assert "grand_total" not in dn["data"]
	with pytest.raises(MappingError):
		mapping.erp_event(**{**kwargs, "doctype": "Sales Order"})


# --------------------------------------------------- GST-inclusive pricing (R1)


def slab_payload():
	"""GST-inclusive price 1180.00 for 3 units: ex-GST 1000.00, rate 333.333333... per unit."""
	return {
		**intra_payload(),
		"total_weight_kg": "45.000",
		"items": [
			{
				"platform_product_id": "prod-3",
				"sku": "SLAB-OLD",
				"qty": "3",
				"rate": "333.333333",
				"amount": "1000.00",
				"weight_kg": "45.000",
			}
		],
		"taxes": [
			{"type": "CGST", "rate": "9", "amount": "90.00"},
			{"type": "SGST", "rate": "9", "amount": "90.00"},
		],
		"net_total": "1000.00",
		"tax_total": "180.00",
		"grand_total": "1180.00",
	}


def test_non_terminating_ex_gst_rate_is_accepted():
	result = mapping.sales_order_from_payload(
		slab_payload(), customer="C", tax_accounts=TAX_ACCOUNTS, item_codes=ITEM_CODES
	)
	line = result["doc"]["items"][0]
	assert line["item_code"] == "TL-SLAB"
	assert line["rate"] == Decimal("333.333333")
	assert line["qty"] * line["rate"] == Decimal("999.999999")
	assert mapping.round_paisa(line["qty"] * line["rate"]) == Decimal("1000.00")
	assert line["description"] == "SKU SLAB-OLD"
	assert result["net_total"] == Decimal("1000.00")
	assert result["grand_total"] == Decimal("1180.00")


def test_rate_rounding_off_by_a_paisa_is_rejected():
	payload = slab_payload()
	payload["items"][0]["rate"] = "333.336667"  # 3 x rate = 1000.010001 -> 1000.01
	with pytest.raises(MappingError, match=r"\(1000\.01\)"):
		mapping.sales_order_from_payload(
			payload, customer="C", tax_accounts=TAX_ACCOUNTS, item_codes=ITEM_CODES
		)


def test_round_paisa_is_half_up():
	assert mapping.round_paisa(Decimal("0.005")) == Decimal("0.01")
	assert mapping.round_paisa(Decimal("0.015")) == Decimal("0.02")
	assert mapping.round_paisa(Decimal("0.004999")) == Decimal("0.00")


def test_line_product_ids_are_distinct_in_order():
	payload = intra_payload()
	payload["items"].append({**payload["items"][0]})
	assert mapping.line_product_ids(payload) == ["prod-1", "prod-2"]
	assert mapping.line_product_ids({}) == []
