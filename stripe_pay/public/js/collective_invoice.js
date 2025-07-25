frappe.ui.form.on('Collective Invoices', {
    refresh: function(frm) {
        if (!frm.doc.__islocal) {
            frm.add_custom_button(__('Pay with Stripe'), function() {
                pay_with_stripe(frm);
            }, __('Actions'));
        }
    }
});

function pay_with_stripe(frm) {
    frappe.show_alert({
        message: __('Creating Stripe payment session...'),
        indicator: 'blue'
    });

    frappe.call({
        method: "stripe_pay.methods.stripe_collective.create_stripe_url_collective",

        args: {
            collective_invoice: frm.doc.name  // Correct parameter
        },
        callback: function(r) {
            if (r.message && r.message.url) {
                frappe.show_alert({
                    message: __('Redirecting to Stripe...'),
                    indicator: 'green'
                });
                window.open(r.message.url, '_blank');
                frm.reload_doc();
            } else {
                frappe.show_alert({
                    message: __('Failed to create Stripe payment link'),
                    indicator: 'red'
                });
            }
        },
        error: function(r) {
            frappe.show_alert({
                message: __('Error connecting to Stripe'),
                indicator: 'red'
            });
            console.error('Stripe error:', r);
        }
    });
}
