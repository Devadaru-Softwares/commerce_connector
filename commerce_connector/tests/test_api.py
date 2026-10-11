# Integration tests for commerce_connector.api. They need a site with ERPNext
# installed and a company with a standard chart of accounts:
#   bench --site <site> set-config allow_tests true
#   bench --site <site> run-tests --app commerce_connector
# The pure payload mapping is covered without a bench by tests/ at the repo root.

import frappe

from commerce_connector import api

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # older frappe
	from frappe.tests.utils import FrappeTestCase as TestCase

PART_ID = "test-part-0001"
USER_ID = "test-user-0001"
ARCHITECT_ID = "a1b2c3d4-0000-4000-8000-000000000001"


def _company():
	company = frappe.db.get_value("Company", {"name": "_Test Company"}) or frappe.db.get_value("Company", {})
	if not company:
		raise RuntimeError("These tests need at least one Company on the site")
	return company


def _account(company, account_name, parent_name, **extra):
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"{account_name} - {abbr}"
	if frappe.db.exists("Account", name):
		return name
	parent = frappe.db.get_value("Account", {"account_name": parent_name, "company": company})
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"parent_account": parent,
			"company": company,
			**extra,
		}
	).insert(ignore_permissions=True)
	return doc.name


class TestCommerceConnectorApi(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = _company()
		settings = frappe.get_single("Commerce Connector Settings")
		settings.enabled = 1
		settings.default_item_group = frappe.db.get_value("Item Group", {"is_group": 0}) or "All Item Groups"
		settings.default_customer_group = frappe.db.get_value("Customer Group", {"is_group": 0})
		settings.default_territory = frappe.db.get_value("Territory", {"is_group": 0})
		settings.cgst_account = _account(
			cls.company, "Test Output CGST", "Duties and Taxes", account_type="Tax"
		)
		settings.sgst_account = _account(
			cls.company, "Test Output SGST", "Duties and Taxes", account_type="Tax"
		)
		settings.igst_account = _account(
			cls.company, "Test Output IGST", "Duties and Taxes", account_type="Tax"
		)
		settings.apd_parent_account = _account(
			cls.company, "Test APD Payable", "Current Liabilities", is_group=1
		)
		settings.apd_expense_account = _account(cls.company, "Test APD Expense", "Indirect Expenses")
		settings.bank_account = _account(cls.company, "Test APD Bank", "Bank Accounts", account_type="Bank")
		settings.save(ignore_permissions=True)
		frappe.clear_cache(doctype="Commerce Connector Settings")

	def _item(self, code, product_id):
		return api.upsert_item(
			{
				"platform_product_id": product_id,
				"item_code": code,
				"item_name": f"Tile {code}",
				"stock_uom": "Nos",
				"weight_per_unit": "14.000",
				"gst_rate": "18",
			}
		)

	def _customer(self):
		return api.upsert_customer(
			{
				"platform_user_id": USER_ID,
				"customer_name": "Test Client",
				"addresses": [
					{
						"address_type": "Billing",
						"line1": "1 Test Road",
						"city": "Kochi",
						"state": "Kerala",
						"pincode": "682001",
					}
				],
			}
		)

	def _order_payload(self):
		return {
			"platform_order_id": "test-order-0001",
			"platform_part_id": PART_ID,
			"platform_order_number": "ORD-T-1",
			"platform_dealer_id": "test-dealer-0001",
			"dealer_name": "Test Dealer",
			"dealer_gstin": "32ABCDE1234F1Z5",
			"company": self.company,
			"customer_platform_user_id": USER_ID,
			"transaction_date": frappe.utils.today(),
			"supply_type": "intra",
			"total_weight_kg": "152.500",
			"items": [
				{
					"platform_product_id": "test-product-1",
					"sku": "CC-TEST-1",
					"qty": "10",
					"rate": "1234.500000",
					"amount": "12345.00",
					"weight_kg": "140.000",
				},
				{
					"platform_product_id": "test-product-2",
					"sku": "CC-TEST-2",
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

	def test_upsert_item_is_idempotent(self):
		first = self._item("CC-TEST-1", "test-product-1")
		second = self._item("CC-TEST-1", "test-product-1")
		self.assertEqual(first["name"], second["name"])
		self.assertFalse(second["created"])
		self.assertEqual(frappe.db.get_value("Item", first["name"], "platform_product_id"), "test-product-1")

	def test_upsert_customer_is_idempotent(self):
		first = self._customer()
		second = self._customer()
		self.assertEqual(first["name"], second["name"])
		self.assertFalse(second["created"])

	def test_sales_order_create_status_cancel(self):
		self._item("CC-TEST-1", "test-product-1")
		self._item("CC-TEST-2", "test-product-2")
		self._customer()

		created = api.create_sales_order(self._order_payload())
		self.assertTrue(created["created"])
		self.assertEqual(created["docstatus"], 1)
		self.assertEqual(created["grand_total"], "15746.99")

		again = api.create_sales_order(self._order_payload())
		self.assertEqual(again["name"], created["name"])
		self.assertFalse(again["created"])
		self.assertEqual(frappe.db.count("Sales Order", {"platform_part_id": PART_ID}), 1)

		so = frappe.get_doc("Sales Order", created["name"])
		self.assertEqual(so.total_weight_kg, 152.5)
		self.assertEqual([row.weight_kg for row in so.items], [140.0, 12.5])

		api.update_sales_order_status(PART_ID, "delivered", "INV-1", "2026-10-12")
		so.reload()
		self.assertEqual(so.platform_status, "Delivered")
		self.assertEqual(so.dealer_invoice_no, "INV-1")

		cancelled = api.cancel_sales_order(PART_ID)
		self.assertEqual(cancelled["status"], "cancelled")
		self.assertEqual(api.cancel_sales_order(PART_ID)["status"], "cancelled")

	def test_gst_inclusive_rate_with_six_decimals(self):
		self._item("CC-TEST-1", "test-product-1")
		self._customer()
		payload = self._order_payload()
		payload.update(
			platform_part_id="test-part-six-dp",
			total_weight_kg="42.000",
			items=[
				{
					"platform_product_id": "test-product-1",
					"sku": "CC-TEST-1-OLD",
					"qty": "3",
					"rate": "333.333333",
					"amount": "1000.00",
					"weight_kg": "42.000",
				}
			],
			taxes=[
				{"type": "CGST", "rate": "9", "amount": "90.00"},
				{"type": "SGST", "rate": "9", "amount": "90.00"},
			],
			net_total="1000.00",
			tax_total="180.00",
			grand_total="1180.00",
		)
		created = api.create_sales_order(payload)
		self.assertEqual(created["grand_total"], "1180.00")
		so = frappe.get_doc("Sales Order", created["name"])
		self.assertEqual(so.items[0].item_code, "CC-TEST-1")
		self.assertAlmostEqual(so.items[0].rate, 333.333333, places=6)
		self.assertEqual(so.net_total, 1000.0)

	def test_sales_order_total_mismatch_is_rejected(self):
		payload = self._order_payload()
		payload["platform_part_id"] = "test-part-mismatch"
		payload["grand_total"] = "15747.00"
		self._customer()
		with self.assertRaises(frappe.ValidationError):
			api.create_sales_order(payload)
		self.assertFalse(frappe.db.exists("Sales Order", {"platform_part_id": "test-part-mismatch"}))

	def test_cancel_unknown_part_is_not_an_error(self):
		self.assertEqual(api.cancel_sales_order("test-part-never-created")["status"], "not_found")

	def test_apd_accrue_void_payout(self):
		base = {
			"platform_architect_id": ARCHITECT_ID,
			"architect_name": "Test Architect",
			"amount": "500.00",
			"posting_date": frappe.utils.today(),
			"company": self.company,
		}
		accrued = api.apd_journal({**base, "action": "accrue", "platform_apd_id": "test-apd-1"})
		self.assertTrue(accrued["created"])
		self.assertTrue(
			accrued["account"].startswith("Architect Privilege Discount - Test Architect - a1b2c3d4")
		)
		self.assertFalse(
			api.apd_journal({**base, "action": "accrue", "platform_apd_id": "test-apd-1"})["created"]
		)

		voided = api.apd_journal({**base, "action": "void", "platform_apd_id": "test-apd-1"})
		self.assertEqual(voided["account"], accrued["account"])

		paid = api.apd_journal(
			{**base, "action": "payout", "platform_payout_id": "test-payout-1", "utr": "UTR0001"}
		)
		self.assertEqual(frappe.db.get_value("Journal Entry", paid["name"], "cheque_no"), "UTR0001")
