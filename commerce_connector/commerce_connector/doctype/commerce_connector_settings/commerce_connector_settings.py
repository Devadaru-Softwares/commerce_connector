# Copyright (c) 2026, Devadaru Softwares
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CommerceConnectorSettings(Document):
	def validate(self):
		if self.apd_parent_account and not frappe.db.get_value(
			"Account", self.apd_parent_account, "is_group"
		):
			frappe.throw(_("APD Parent Liability Account must be a group account"))
		if self.apd_parent_account:
			root_type = frappe.db.get_value("Account", self.apd_parent_account, "root_type")
			if root_type != "Liability":
				frappe.throw(_("APD Parent Liability Account must be a Liability account"))
