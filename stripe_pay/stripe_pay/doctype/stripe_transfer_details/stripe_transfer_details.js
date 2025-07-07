// Copyright (c) 2025, S and contributors
// For license information, please see license.txt

frappe.ui.form.on("Stripe Transfer Details", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Check Status"), function () {
				frappe.call({
					method: "stripe_pay.methods.stripe.check_transfer_status",
					args: {
						account: frm.doc.account,
						reference_id: frm.doc.reference_id
					},
					callback: function (r) {
						if (r.message) {
							frappe.msgprint("Status: " + r.message.status);
							frm.set_value("status", r.message.status);
							frm.set_value("datetime", frappe.datetime.now_datetime());
							frm.save();
						}
					}
				});
			});
		}
	},
});

