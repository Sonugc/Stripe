import frappe
import stripe
import json
from frappe import _

@frappe.whitelist(allow_guest=True)
def stripe_payment_webhook():
    """
    Stripe webhook endpoint to handle payment events
    URL: /api/method/stripe_pay.api.stripe_webhook.stripe_payment_webhook
    """
    try:
        # Get the webhook signature and payload
        payload = frappe.request.get_data()
        sig_header = frappe.get_request_header("Stripe-Signature")
        
        # Get webhook secret from site config
        webhook_secret = frappe.conf.get("stripe_webhook_secret")
        
        if not webhook_secret:
            frappe.log_error(
                "Stripe webhook secret not found in site config",
                "Stripe Webhook Error"
            )
            frappe.local.response.http_status_code = 400
            return {"error": "Webhook secret not configured"}
        
        # Verify the webhook signature
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, webhook_secret
            )
        except ValueError as e:
            frappe.log_error(f"Invalid payload: {str(e)}", "Stripe Webhook Error")
            frappe.local.response.http_status_code = 400
            return {"error": "Invalid payload"}
        except stripe.error.SignatureVerificationError as e:
            frappe.log_error(f"Invalid signature: {str(e)}", "Stripe Webhook Error")
            frappe.local.response.http_status_code = 400
            return {"error": "Invalid signature"}
        
        # Log the event
        frappe.log_error(
            f"📥 Received Event: {event['type']}\n"
            f"Event ID: {event['id']}\n"
            f"Data: {json.dumps(event['data']['object'], indent=2)}",
            "Stripe Webhook Received"
        )
        
        # Handle different event types
        event_type = event["type"]
        
        if event_type == "checkout.session.completed":
            handle_checkout_completed(event["data"]["object"])
        elif event_type == "checkout.session.async_payment_succeeded":
            # ACH payments fire this event when payment actually succeeds
            handle_async_payment_succeeded(event["data"]["object"])
        elif event_type == "payment_intent.succeeded":
            handle_payment_succeeded(event["data"]["object"])
        elif event_type == "payment_intent.payment_failed":
            handle_payment_failed(event["data"]["object"])
        elif event_type == "checkout.session.async_payment_failed":
            # ACH payment failed
            handle_async_payment_failed(event["data"]["object"])
        else:
            frappe.log_error(
                f"Unhandled event type: {event_type}",
                "Stripe Webhook Info"
            )
        
        return {"status": "success"}
        
    except Exception as e:
        frappe.log_error(
            f"Webhook processing error: {str(e)}\n{frappe.get_traceback()}",
            "Stripe Webhook Error"
        )
        frappe.local.response.http_status_code = 500
        return {"error": str(e)}


def handle_checkout_completed(session):
    """Handle checkout session completed - for immediate payments (cards)"""
    try:
        session_id = session.get("id")
        payment_status = session.get("payment_status")
        
        frappe.log_error(
            f"🔄 Processing checkout.session.completed\n"
            f"Session ID: {session_id}\n"
            f"Payment Status: {payment_status}\n"
            f"Amount: {session.get('amount_total')/100} {session.get('currency', '').upper()}",
            "Stripe Checkout Completed"
        )
        
        # Only update to Paid if payment_status is "paid" (immediate payments like cards)
        # For ACH, payment_status will be "unpaid" here, so we wait for async_payment_succeeded
        if payment_status != "paid":
            frappe.log_error(
                f"⏳ Payment status is '{payment_status}' - waiting for async payment completion",
                "Stripe Checkout Completed"
            )
            return
        
        # Find and update invoice for immediate payments
        update_invoice_status(session_id, session.get("payment_intent"), "Paid")
        
    except Exception as e:
        frappe.log_error(
            f"❌ Error in handle_checkout_completed: {str(e)}\n{frappe.get_traceback()}",
            "Stripe Webhook Error"
        )


def handle_async_payment_succeeded(session):
    """Handle async payment success - for ACH/bank payments"""
    try:
        session_id = session.get("id")
        
        frappe.log_error(
            f"✅ Processing checkout.session.async_payment_succeeded\n"
            f"Session ID: {session_id}\n"
            f"Payment Status: {session.get('payment_status')}\n"
            f"Amount: {session.get('amount_total')/100} {session.get('currency', '').upper()}",
            "Stripe Async Payment Succeeded"
        )
        
        # Update invoice to Paid
        update_invoice_status(session_id, session.get("payment_intent"), "Paid")
        
    except Exception as e:
        frappe.log_error(
            f"❌ Error in handle_async_payment_succeeded: {str(e)}\n{frappe.get_traceback()}",
            "Stripe Webhook Error"
        )


def handle_async_payment_failed(session):
    """Handle async payment failure - for ACH/bank payments"""
    try:
        session_id = session.get("id")
        
        frappe.log_error(
            f"❌ Processing checkout.session.async_payment_failed\n"
            f"Session ID: {session_id}",
            "Stripe Async Payment Failed"
        )
        
        # Update invoice to Failed
        update_invoice_status(session_id, session.get("payment_intent"), "Failed")
        
    except Exception as e:
        frappe.log_error(
            f"❌ Error in handle_async_payment_failed: {str(e)}\n{frappe.get_traceback()}",
            "Stripe Webhook Error"
        )


def update_invoice_status(session_id, payment_intent_id, new_status):
    """Helper function to update invoice status"""
    try:
        # Find the invoice
        invoices = frappe.get_all(
            "Collective Invoices",
            filters={"custom_stripe_session_id": session_id},
            fields=["name", "status", "custom_stripe_session_id", "custom_stripe_payment_intent_id"]
        )
        
        frappe.log_error(
            f"🔍 Search Results:\n"
            f"Looking for session_id: {session_id}\n"
            f"Found {len(invoices)} invoice(s): {invoices}",
            "Stripe Invoice Search"
        )
        
        if not invoices:
            frappe.log_error(
                f"⚠️ No invoice found for session: {session_id}",
                "Stripe Webhook Warning"
            )
            return
        
        # Update invoice
        invoice = frappe.get_doc("Collective Invoices", invoices[0].name)
        
        frappe.log_error(
            f"📄 Found invoice: {invoice.name}\n"
            f"Current status: {invoice.status}\n"
            f"Current payment_intent_id: {invoice.custom_stripe_payment_intent_id}",
            "Stripe Invoice Found"
        )
        
        old_status = invoice.status
        invoice.status = new_status
        if payment_intent_id:
            invoice.custom_stripe_payment_intent_id = payment_intent_id
        
        invoice.save(ignore_permissions=True)
        frappe.db.commit()
        
        frappe.log_error(
            f"✅ SUCCESS: Invoice {invoice.name} updated!\n"
            f"Old Status: {old_status}\n"
            f"New Status: {invoice.status}\n"
            f"Payment Intent: {invoice.custom_stripe_payment_intent_id}",
            "Stripe Payment Success"
        )
        
    except Exception as e:
        frappe.log_error(
            f"❌ Error updating invoice: {str(e)}\n{frappe.get_traceback()}",
            "Stripe Invoice Update Error"
        )


def handle_payment_succeeded(payment_intent):
    """Handle successful payment intent"""
    try:
        payment_intent_id = payment_intent.get("id")
        
        frappe.log_error(
            f"💰 Payment succeeded: {payment_intent_id}\n"
            f"Amount: {payment_intent.get('amount')/100} {payment_intent.get('currency', '').upper()}",
            "Stripe Payment Intent Succeeded"
        )
        
    except Exception as e:
        frappe.log_error(
            f"Error in handle_payment_succeeded: {str(e)}",
            "Stripe Webhook Error"
        )


def handle_payment_failed(payment_intent):
    """Handle failed payment intent"""
    try:
        payment_intent_id = payment_intent.get("id")
        
        frappe.log_error(
            f"❌ Payment failed: {payment_intent_id}\n"
            f"Error: {payment_intent.get('last_payment_error', {}).get('message')}",
            "Stripe Payment Failed"
        )
        
        # Find and update invoice
        invoices = frappe.get_all(
            "Collective Invoices",
            filters={"custom_stripe_payment_intent_id": payment_intent_id},
            fields=["name"]
        )
        
        if invoices:
            invoice = frappe.get_doc("Collective Invoices", invoices[0].name)
            invoice.status = "Failed"
            invoice.save(ignore_permissions=True)
            frappe.db.commit()
            
            frappe.log_error(
                f"Updated invoice {invoice.name} to Failed status",
                "Stripe Payment Failed Update"
            )
        
    except Exception as e:
        frappe.log_error(
            f"Error in handle_payment_failed: {str(e)}",
            "Stripe Webhook Error"
        )