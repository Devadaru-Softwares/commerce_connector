// Copyright (c) 2026, Devadaru Softwares
// For license information, please see license.txt

frappe.ui.form.on("Commerce Connector Settings", {
	setup(frm) {
		const company_filter = (extra) => () => ({ filters: { is_group: 0, ...extra } });
		frm.set_query("apd_parent_account", () => ({ filters: { is_group: 1, root_type: "Liability" } }));
		frm.set_query("apd_expense_account", company_filter({ root_type: "Expense" }));
		frm.set_query("bank_account", company_filter({ account_type: ["in", ["Bank", "Cash"]] }));
		frm.set_query("cgst_account", company_filter({ account_type: "Tax" }));
		frm.set_query("sgst_account", company_filter({ account_type: "Tax" }));
		frm.set_query("igst_account", company_filter({ account_type: "Tax" }));
	},
});
