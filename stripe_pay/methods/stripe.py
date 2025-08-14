import stripe
import frappe
from frappe import _
from frappe.utils import flt, now_datetime
from frappe.utils import nowdate

connected_account_id = "acct_1RdUXWQw0gf1zitu"

@frappe.whitelist()
def create_stripe_payment(sales_invoice):
    si_doc = frappe.get_doc("Sales Invoice", sales_invoice)

    if si_doc.docstatus != 1:
        frappe.throw("Sales Invoice must be submitted before creating a payment.")

    total = flt(si_doc.grand_total) * 100  

    stripe_settings = frappe.get_single("Stripe Payment Settings")
    sk = stripe_settings.get_password("secret_key")
    stripe.api_key = sk


    try:
        transfer = stripe.Transfer.create(
            amount=int(total),
            currency="usd",
            destination=connected_account_id,
            description=f"Transfer for Sales Invoice {sales_invoice}"
        )
        transfer_id = transfer.id
        frappe.msgprint(f"Transfer successful! Transfer ID: {transfer_id}")
        create_stripe_transfer_log(transfer_id, "paid", "Sales Invoice", si_doc.name)
    except Exception as e:
        create_stripe_transfer_log("N/A", "failed", "Sales Invoice", si_doc.name)
        frappe.throw(f"Stripe Transfer failed: {e}")

    try:
        payout = stripe.Payout.create(
            amount=int(total),  
            currency="usd",
            description=f"Payout for Sales Invoice {sales_invoice}",
            stripe_account=connected_account_id
        )
        payout_id = payout.id
        frappe.msgprint(f"Payout initiated! Payout ID: {payout_id}")
        create_stripe_transfer_log(payout_id, "paid", "Sales Invoice", si_doc.name)
    except Exception as e:
        create_stripe_transfer_log("N/A", "failed", "Sales Invoice", si_doc.name)
        frappe.throw(f"Stripe Payout failed: {e}")

    payment_entry = frappe.new_doc("Payment Entry")
    payment_entry.payment_type = "Receive"
    payment_entry.company = si_doc.company
    payment_entry.posting_date = now_datetime().date()
    payment_entry.mode_of_payment = "Cash"
    payment_entry.party_type = "Customer"
    payment_entry.party = si_doc.customer
    payment_entry.paid_from = frappe.db.get_value("Company", si_doc.company, "default_receivable_account")
    payment_entry.paid_to = frappe.db.get_value("Mode of Payment Account", {"parent": "Stripe"}, "default_account")
    payment_entry.paid_amount = si_doc.grand_total
    payment_entry.received_amount = si_doc.grand_total
    payment_entry.target_exchange_rate = 1
    payment_entry.reference_no = transfer_id
    payment_entry.reference_date = now_datetime().date()

    payment_entry.append("references", {
        "reference_doctype": "Sales Invoice",
        "reference_name": si_doc.name,
        "allocated_amount": si_doc.grand_total
    })

    payment_entry.insert(ignore_permissions=True)
    payment_entry.submit()

    frappe.msgprint(f"Payment Entry created: {payment_entry.name}")

    return {
        "transfer_id": transfer_id,
        "payout_id": payout_id,
        "payment_entry": payment_entry.name
    }


def create_stripe_transfer_log(reference_id, status, reference_doc, reference_name):
    doc = frappe.new_doc("Stripe Transfer Details")
    doc.reference_id = reference_id
    doc.status = status
    doc.datetime = now_datetime()
    doc.reference_doc = reference_doc
    doc.refrence_name = reference_name
    doc.account = connected_account_id
    doc.insert(ignore_permissions=True)
    frappe.db.commit() 
    frappe.msgprint(f"Stripe Transfer Log created: {doc.name}")
    return doc.name


@frappe.whitelist()
def check_transfer_status(account, reference_id):
    stripe_settings = frappe.get_single("Stripe Payment Settings")
    sk = stripe_settings.get_password("secret_key")
    stripe.api_key = sk

    try:
        transfer = stripe.Transfer.retrieve(reference_id)
        return {"status": transfer.status}
    except stripe.error.InvalidRequestError:
        try:
            payout = stripe.Payout.retrieve(
                reference_id,
                stripe_account=account
            )
            return {"status": payout.status}
        except Exception as e:
            frappe.throw(f"Could not retrieve payout status: {str(e)}")
    except Exception as e:
        frappe.throw(f"Could not retrieve status: {str(e)}")


@frappe.whitelist()
def create_stripe_url(sales_invoice=None):
    if not sales_invoice:
        sales_invoice = frappe.local.request.args.get("sales_invoice")

    if not sales_invoice:
        frappe.throw(_("Missing Sales Invoice"))
    
    si_doc = frappe.get_doc("Sales Invoice", sales_invoice)

    if si_doc.docstatus != 1:
        frappe.throw(_("Sales Invoice must be submitted before creating a payment."))

    stripe_settings = frappe.get_single("Stripe Payment Settings")
    sk = stripe_settings.get_password("secret_key")
    stripe.api_key = sk

    currency = "usd"  

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["us_bank_account"],
            
            line_items=[{
                "price_data": {
                    "currency": currency,
                    "product_data": {
                        "name": f"Payment for {si_doc.name} by {si_doc.customer}",
                    },
                    "unit_amount": int(flt(si_doc.grand_total) * 100),
                },
                "quantity": 1,
            }],
            mode="payment",
            
            success_url=frappe.utils.get_url(f"/api/method/stripe_pay.methods.stripe.handle_success_callback?invoice={si_doc.name}"),
            cancel_url=frappe.utils.get_url(f"/api/method/stripe_pay.methods.stripe.handle_failure_callback?invoice={si_doc.name}"),
            
            metadata={
                "sales_invoice": si_doc.name,
                "customer": si_doc.customer
            },
            
            customer_creation="if_required",
            
            billing_address_collection="auto",
            
            custom_fields=[
                {
                    "key": "invoice_number",
                    "label": {
                        "type": "custom",
                        "custom": "Invoice Number"
                    },
                    "type": "text",
                    "optional": True
                }
            ],
            
            automatic_tax={"enabled": False},
            
            expires_at=int((now_datetime().timestamp() + 86400)),
            
            payment_method_options={
                "us_bank_account": {
                    "verification_method": "automatic"  
                }
            }
        )

        si_doc.db_set("stripe_session_id", session.id)
        if session.get("payment_intent"):
            si_doc.db_set("stripe_payment_intent_id", session.payment_intent)

        frappe.db.commit()

        return {
            "session_id": session.id,
            "url": session.url,
            "payment_methods": ["card", "us_bank_account"]
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Stripe Session Creation Failed")
        frappe.throw(_("Stripe Checkout Session creation failed: ") + str(e))


@frappe.whitelist(allow_guest=True)
def handle_success_callback():
    try:
        invoice_id = frappe.local.request.args.get("invoice")
        frappe.log_error(invoice_id, "Callback Received for Invoice")

        if not invoice_id:
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = "/app"
            return

        invoice = frappe.get_doc("Sales Invoice", invoice_id)
        frappe.log_error(invoice.name, "Loaded Invoice")

        if invoice.docstatus != 1:
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = f"/app/sales-invoice/{invoice_id}"
            return

        stripe_settings = frappe.get_single("Stripe Payment Settings")
        sk = stripe_settings.get_password("secret_key")
        stripe.api_key = sk
        
        session_id = invoice.stripe_session_id
        if session_id:
            session = stripe.checkout.Session.retrieve(session_id)
            payment_intent = stripe.PaymentIntent.retrieve(session.payment_intent)
            payment_method = stripe.PaymentMethod.retrieve(payment_intent.payment_method)
            
            frappe.log_error(f"Payment method used: {payment_method.type}", "Payment Method Info")

        paid_from = frappe.get_cached_value("Company", invoice.company, "default_receivable_account")
        
        paid_to = frappe.get_cached_value("Mode of Payment Account", {"parent": "Stripe", "company": invoice.company}, "default_account")
        mode_of_payment = "Stripe"
        
        if not paid_to:
            paid_to = frappe.get_cached_value("Mode of Payment Account", {"parent": "Cash", "company": invoice.company}, "default_account")
            mode_of_payment = "Stripe"

        if not paid_from or not paid_to:
            frappe.log_error("Paid From or Paid To account missing", "Payment Account Error")
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = f"/app/sales-invoice/{invoice_id}?payment_status=account_error"
            return

        payment_entry = frappe.new_doc("Payment Entry")
        payment_entry.payment_type = "Receive"
        payment_entry.company = invoice.company
        payment_entry.posting_date = nowdate()
        payment_entry.mode_of_payment = mode_of_payment
        payment_entry.party_type = "Customer"
        payment_entry.party = invoice.customer
        payment_entry.paid_from = paid_from
        payment_entry.paid_to = paid_to
        payment_entry.paid_amount = invoice.outstanding_amount
        payment_entry.received_amount = invoice.outstanding_amount
        
        if hasattr(invoice, 'stripe_session_id') and invoice.stripe_session_id:
            payment_entry.reference_no = invoice.stripe_session_id
            payment_entry.reference_date = nowdate()

        payment_entry.append("references", {
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice.name,
            "total_amount": invoice.grand_total,
            "outstanding_amount": invoice.outstanding_amount,
            "allocated_amount": invoice.outstanding_amount
        })

        payment_entry.insert(ignore_permissions=True)
        payment_entry.submit()
        frappe.db.commit()

        frappe.log_error(payment_entry.name, "Payment Entry Created")

        if hasattr(invoice, 'stripe_session_id') and invoice.stripe_session_id:
            create_stripe_transfer_log(
                invoice.stripe_session_id, 
                "paid", 
                "Sales Invoice", 
                invoice.name
            )

        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = f"/app/sales-invoice/{invoice_id}?payment_status=success&payment_entry={payment_entry.name}"
        return

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Payment Callback Error")
        invoice_id = frappe.local.request.args.get("invoice", "")
        if invoice_id:
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = f"/app/sales-invoice/{invoice_id}?payment_status=error"
        else:
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = "/app"
        return

@frappe.whitelist(allow_guest=True)
def handle_failure_callback():
    invoice_id = frappe.local.request.args.get("invoice", "")
    frappe.log_error(f"Payment cancelled for invoice {invoice_id}", "Payment Cancelled")
    
    if invoice_id:
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = f"/app/sales-invoice/{invoice_id}?payment_status=cancelled"
    else:
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/app"
    return


@frappe.whitelist(allow_guest=True)
def stripe_webhook():
    """Handle Stripe webhook events for payment status updates"""
    try:
        payload = frappe.request.get_data()
        sig_header = frappe.request.headers.get('Stripe-Signature')
        
        stripe_settings = frappe.get_single("Stripe Payment Settings")
        endpoint_secret = stripe_settings.get_password("webhook_secret")
        
        if not endpoint_secret:
            frappe.log_error("Webhook secret not configured", "Stripe Webhook")
            return {"status": "error", "message": "Webhook secret not configured"}
        
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )

        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            handle_checkout_session_completed(session)
        elif event['type'] == 'payment_intent.succeeded':
            payment_intent = event['data']['object']
            handle_payment_intent_succeeded(payment_intent)
        elif event['type'] == 'payment_intent.payment_failed':
            payment_intent = event['data']['object']
            handle_payment_intent_failed(payment_intent)

        return {"status": "success"}

    except ValueError as e:
        frappe.log_error(f"Invalid payload: {str(e)}", "Stripe Webhook")
        return {"status": "error", "message": "Invalid payload"}
    except stripe.error.SignatureVerificationError as e:
        frappe.log_error(f"Invalid signature: {str(e)}", "Stripe Webhook")
        return {"status": "error", "message": "Invalid signature"}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Webhook Error")
        return {"status": "error", "message": "Webhook processing failed"}

def handle_checkout_session_completed(session):
    """Handle completed checkout session"""
    try:
        sales_invoice = session.metadata.get('sales_invoice')
        if sales_invoice:
            frappe.log_error(f"Checkout session completed for invoice {sales_invoice}", "Stripe Webhook")
    except Exception as e:
        frappe.log_error(f"Error processing checkout session: {str(e)}", "Stripe Webhook")

def handle_payment_intent_succeeded(payment_intent):
    """Handle successful payment intent (useful for ACH payments)"""
    try:
        frappe.log_error(f"Payment intent succeeded: {payment_intent.id}", "Stripe Webhook")
    except Exception as e:
        frappe.log_error(f"Error processing payment success: {str(e)}", "Stripe Webhook")

def handle_payment_intent_failed(payment_intent):
    """Handle failed payment intent"""
    try:
        frappe.log_error(f"Payment intent failed: {payment_intent.id}", "Stripe Webhook")
    except Exception as e:
        frappe.log_error(f"Error processing payment failure: {str(e)}", "Stripe Webhook")

@frappe.whitelist(allow_guest=True)
def handle_failure_callback():
    frappe.throw(_("Payment failed or cancelled by the user. Please try again."))


