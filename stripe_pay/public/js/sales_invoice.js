frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        if (!frm.doc.__islocal && frm.doc.docstatus === 1) {
            frm.add_custom_button(__("Pay with Stripe"), function() {
                frappe.call({
                    method: "stripe_pay.methods.stripe.create_stripe_url",
                    args: {
                        sales_invoice: frm.doc.name
                    },
                    callback: function(r) {
                        if (r.message && r.message.url) {
                            frappe.msgprint(__("Redirecting to Stripe payment page..."));
                            window.open(r.message.url, '_blank');

                            frm.reload_doc();
                        } else {
                            frappe.msgprint(__("Failed to generate Stripe payment link."));
                        }
                    },
                    freeze: true,
                    freeze_message: __("Creating Stripe payment session...")
                });
            }, __("Actions"));
        }

        if (frm.doc.stripe_session_id) {
            frm.add_custom_button(__("View Stripe Session"), () => {
                window.open(`https://dashboard.stripe.com/test/checkouts/${frm.doc.stripe_session_id}`, '_blank');
            }, __("Stripe"));
        }
    }
});
