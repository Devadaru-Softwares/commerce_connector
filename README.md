## Commerce Connector

Frappe app for ERPNext v16 that receives data from the commerce platform and sends
signed events back. It handles:

- items and customers (upsert)
- one Sales Order per dealer part, with weight, quantity and explicit GST rows
- Sales Order cancel and status updates
- Architect Privilege Discount (APD) journals: accrue, void and payout
- signed webhooks to the platform when a Sales Invoice or Delivery Note is submitted

Requires: Frappe and ERPNext `version-16` (Python 3.12+, Node 20+). India Compliance is
recommended. The app only uses its fields when they exist on the site.

### Install on Frappe Cloud

1. In the bench group, open **Apps → Add App → GitHub**. Choose
   `Devadaru-Softwares/commerce_connector` and the release branch, then deploy the bench.
2. On the site, open **Apps → Install App → Commerce Connector**. The installer loads the custom
   fields (fixtures) and the **Commerce Connector** role.
3. Fill in **Commerce Connector Settings** (see below), then tick **Enabled**.

On a self-hosted bench:

```bash
bench get-app https://github.com/Devadaru-Softwares/commerce_connector --branch main
bench --site <site> install-app commerce_connector
bench --site <site> migrate
```

### Settings (Commerce Connector Settings)

| Field | Purpose |
| --- | --- |
| Enabled | When off, every API method refuses and no webhooks are sent. |
| Platform Webhook URL | e.g. `https://api.<platform-domain>/v1/webhooks/erpnext` |
| Webhook Secret | Shared secret. The same value goes in the platform's `ERPNEXT_WEBHOOK_SECRET`. |
| Default Item Group / Customer Group / Territory | Used when a payload does not name one. |
| CGST / SGST / IGST Account | Output tax accounts used for Sales Order tax rows. |
| APD Parent Liability Account | A group account with root type Liability. One child account per architect is created under it. |
| APD Expense Account | Debited on accrual and credited on void. |
| Payout Bank Account | Credited on APD payout. |

### API user and token

1. Create a User, e.g. `platform-sync@<company-domain>`, with user type **System User**. Give it the
   role **Commerce Connector** only. The methods run their inserts with elevated permissions after
   checking this role.
2. On the user, open **Settings → API Access → Generate Keys**. Copy the key and the secret.
3. On the platform, set `ERPNEXT_URL`, `ERPNEXT_API_KEY`, `ERPNEXT_API_SECRET` and `ERPNEXT_COMPANY`.

Each call is `POST {ERPNEXT_URL}/api/method/commerce_connector.api.<method>` with
`Authorization: token <api_key>:<api_secret>` and `Content-Type: application/json`. Methods that take
`payload` expect a body of `{"payload": {...}}`. The other methods take their arguments as top-level
JSON keys. A response is `{"message": {...}}`. A contract violation returns HTTP 417 with
`exc_type: "ValidationError"` and a readable message. Do not retry it until the data is fixed.

### Methods

All methods are idempotent by platform id. A repeat call returns the existing document with
`"created": false`.

Conventions:
- Money is a rupee string with exactly 2 decimals, e.g. `"1234.56"`. Floats and other formats are
  rejected.
- Quantities, weights and rates are decimal strings, e.g. `"10"` or `"14.000"`.
- Dates are `YYYY-MM-DD`.

#### `upsert_item(payload)`

```json
{"payload": {
  "platform_product_id": "6f1c…",
  "item_code": "TL-600-GL",
  "item_name": "Glossy 600x600",
  "description": "optional",
  "item_group": "optional, defaults to settings",
  "stock_uom": "Box",
  "weight_per_unit": "14.000",
  "weight_uom": "Kg",
  "gst_rate": "18",
  "disabled": false
}}
```

The Item is looked up by `platform_product_id`, then by `item_code`. Items are created as non-stock,
because stock stays on the platform. `item_code`, `item_group`, `stock_uom` and `is_stock_item` are
set only on create. A missing UOM is created.

Returns `{"name", "doctype", "docstatus", "created", "item_code"}`.

#### `upsert_customer(payload)`

```json
{"payload": {
  "platform_user_id": "0b7e…",
  "customer_name": "Anu Joseph",
  "phone": "+919800000000",
  "gstin": "32ABCDE1234F1Z5",
  "addresses": [
    {"address_type": "Billing", "line1": "1 MG Road", "line2": null, "city": "Kochi",
     "state": "Kerala", "pincode": "682001", "country": "India"}
  ]
}}
```

The Customer is looked up by `platform_user_id`. There is at most one address per type
(`Billing` or `Shipping`), and each is updated in place on a repeat call. `gstin` is optional.

#### `create_sales_order(payload)`

The platform sends one call per dealer part.

```json
{"payload": {
  "platform_order_id": "…", "platform_part_id": "…", "platform_order_number": "ORD-1042",
  "platform_dealer_id": "…", "dealer_name": "Kochi Tiles", "dealer_gstin": "32ABCDE1234F1Z5",
  "company": "Devadaru Softwares",
  "customer_platform_user_id": "0b7e…",
  "transaction_date": "2026-10-11", "delivery_date": "2026-10-11",
  "supply_type": "intra",
  "place_of_supply": "32-Kerala",
  "total_weight_kg": "152.500",
  "items": [
    {"item_code": "TL-600-GL", "qty": "10", "rate": "1234.50", "amount": "12345.00", "weight_kg": "140.000", "uom": "Box"},
    {"item_code": "TL-300-MT", "qty": "3", "rate": "333.33", "amount": "999.99", "weight_kg": "12.500"}
  ],
  "taxes": [
    {"type": "CGST", "rate": "9", "amount": "1201.00"},
    {"type": "SGST", "rate": "9", "amount": "1201.00"}
  ],
  "net_total": "13344.99", "tax_total": "2402.00", "grand_total": "15746.99"
}}
```

Rules:
- `supply_type` is `intra` (CGST and SGST rows) or `inter` (one IGST row). Tax rows may be empty only
  when `tax_total` is `"0.00"`.
- `items[].amount` must equal `qty × rate` exactly. `items[].weight_kg` is the line's total weight.
- The amounts must sum exactly: lines to `net_total`, taxes to `tax_total`, `net_total + tax_total`
  to `grand_total`, and line weights to `total_weight_kg`.
- `delivery_date`, `place_of_supply`, `dealer_gstin` and `items[].uom` are optional.
- The customer and the items must already be synced.

The Sales Order is inserted with `ignore_pricing_rule` and `disable_rounded_total` set. Tax rows use
charge type **Actual** with the payload amounts. The order is then submitted. Net total, tax total and
grand total are checked against the payload with zero tolerance, both before and after submit. On a
mismatch, everything rolls back and a ValidationError names the part and both values.

Returns `{"name", "doctype", "docstatus", "created", "grand_total"}`.

#### `cancel_sales_order(platform_part_id)`

```json
{"platform_part_id": "…"}
```

Cancels the submitted Sales Order. Returns `status`, which is one of:
- `cancelled` (also returned on a repeat call)
- `deleted_draft`
- `not_found`, when no order was ever created. This is not an error, so a refund after a failed
  create completes.

#### `update_sales_order_status(platform_part_id, status, invoice_no=None, invoice_date=None)`

```json
{"platform_part_id": "…", "status": "delivered", "invoice_no": "INV/26/001", "invoice_date": "2026-10-12"}
```

`status` is `dispatched` or `delivered`. The method sets `platform_status`, `dealer_invoice_no` and
`dealer_invoice_date` on the submitted Sales Order. It raises DoesNotExistError if there is no
submitted order for the part.

#### `apd_journal(payload)`

```json
{"payload": {
  "action": "accrue",
  "platform_apd_id": "…",
  "platform_architect_id": "3f2a9c1e-77aa-…",
  "architect_name": "Meera Nair",
  "amount": "500.00",
  "posting_date": "2026-10-11",
  "company": "Devadaru Softwares",
  "platform_order_number": "ORD-1042"
}}
```

| action | id field | Journal Entry |
| --- | --- | --- |
| `accrue` | `platform_apd_id` | Dr APD expense, Cr architect liability |
| `void` | `platform_apd_id` | Dr architect liability, Cr APD expense |
| `payout` | `platform_payout_id`, plus `utr` | Bank Entry: Dr architect liability, Cr bank. `cheque_no` = UTR |

- The idempotency key is `apd.{action}:{id}`, stored in `Journal Entry.platform_ref`.
- The architect account is `Architect Privilege Discount - {architect_name} - {platform_architect_id[:8]}`.
  It is created under the APD parent account if it is missing, and it is found later by
  `Account.platform_architect_id`.
- `platform_order_number` is optional and only appears in the remark.

Returns `{"name", "doctype", "docstatus", "created", "platform_ref", "account"}`.

### Webhooks (ERPNext → platform)

When a Sales Invoice or Delivery Note is submitted and one of its items links to a Sales Order that
has a `platform_part_id`, a background job sends:

```
POST <Platform Webhook URL>
Content-Type: application/json
X-Commerce-Event-Id: <event_id>
X-Commerce-Signature: <lowercase hex HMAC-SHA256 of the raw body, keyed with Webhook Secret>
```

```json
{"data":{"erp_doctype":"Sales Invoice","erp_name":"ACC-SINV-2026-00001","grand_total":"15746.99",
"platform_order_id":"…","platform_part_id":"…","posting_date":"2026-10-12","sales_order":"SAL-ORD-2026-00001"},
"event":"sales_invoice.submitted","event_id":"…","occurred_at":"2026-10-12T10:00:00Z"}
```

- `event` is `sales_invoice.submitted` or `delivery_note.submitted`. `grand_total` is sent only for
  Sales Invoices.
- `event_id` is a UUID derived from the event, document and part. A redelivery carries the same id.
- The body is compact JSON with sorted keys. The signature covers the exact bytes sent, so verify it
  over the raw body before parsing.
- The job makes 3 attempts, with a 15 s timeout each. A failure is written to the Error Log with the
  event id only.

**Signing test vector** (shared with the platform's verifier test):

| secret | raw body | X-Commerce-Signature |
| --- | --- | --- |
| `s` | `{"a":1}` | `37beaf650f70b40ec9706929c2e9d835cbd63729988f48781e6383a147215f07` |

### Custom fields (fixtures)

| DocType | Fields |
| --- | --- |
| Item | `platform_product_id`, `platform_gst_rate` |
| Customer | `platform_user_id` |
| Sales Order | `platform_order_id`, `platform_part_id`, `platform_order_number`, `platform_dealer_id`, `dealer_name`, `dealer_gstin`, `total_weight_kg`, `platform_status`, `dealer_invoice_no`, `dealer_invoice_date` |
| Sales Order Item | `weight_kg` |
| Journal Entry | `platform_ref` |
| Account | `platform_architect_id` |

### Development

Pure payload mapping and signing live in `commerce_connector/mapping.py` and
`commerce_connector/signing.py` and do not import frappe. Test them without a bench:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest ruff
.venv/bin/python -m pytest tests
.venv/bin/ruff check .
```

The integration tests in `commerce_connector/tests/` need a site with ERPNext:

```bash
bench --site <site> set-config allow_tests true
bench --site <site> run-tests --app commerce_connector
```

The app uses `pre-commit` for ruff, eslint and prettier: `pre-commit install`.

### License

Proprietary – Devadaru Softwares. See `license.txt`.
