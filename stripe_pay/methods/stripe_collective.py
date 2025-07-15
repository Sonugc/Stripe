import stripe
import frappe
from frappe import _
from frappe.utils import flt, now_datetime, nowdate
from frappe.utils import get_url

connected_account_id = "acct_1RdUXWQw0gf1zitu"

@frappe.whitelist()
def create_stripe_payment_collective(collective_invoice):
    """Create Stripe payment for Collective Invoice"""
    ci_doc = frappe.get_doc("Collective Invoices", collective_invoice)

    if ci_doc.docstatus != 1:
        frappe.throw("Collective Invoice must be submitted before creating a payment.")

    total = flt(ci_doc.total_amount) * 100  # Convert to cents

    stripe_settings = frappe.get_single("Stripe Payment Settings")
    sk = stripe_settings.get_password("secret_key")
    stripe.api_key = sk

    try:
        # Create transfer
        transfer = stripe.Transfer.create(
            amount=int(total),
            currency="usd",
            destination=connected_account_id,
            description=f"Transfer for Collective Invoice {collective_invoice}"
        )
        transfer_id = transfer.id
        frappe.msgprint(f"Transfer successful! Transfer ID: {transfer_id}")
        create_stripe_transfer_log(transfer_id, "paid", "Collective Invoices", ci_doc.name)
    except Exception as e:
        create_stripe_transfer_log("N/A", "failed", "Collective Invoices", ci_doc.name)
        frappe.throw(f"Stripe Transfer failed: {e}")

    try:
        # Create payout
        payout = stripe.Payout.create(
            amount=int(total),  
            currency="usd",
            description=f"Payout for Collective Invoice {collective_invoice}",
            stripe_account=connected_account_id
        )
        payout_id = payout.id
        frappe.msgprint(f"Payout initiated! Payout ID: {payout_id}")
        create_stripe_transfer_log(payout_id, "paid", "Collective Invoices", ci_doc.name)
    except Exception as e:
        create_stripe_transfer_log("N/A", "failed", "Collective Invoices", ci_doc.name)
        frappe.throw(f"Stripe Payout failed: {e}")

    # Create payment entry for collective invoice
    payment_entry = create_collective_payment_entry(ci_doc, transfer_id)

    return {
        "transfer_id": transfer_id,
        "payout_id": payout_id,
        "payment_entry": payment_entry
    }

def create_collective_payment_entry(ci_doc, reference_no):
    """Create payment entry for collective invoice"""
    # Get company from first reference invoice or use default
    company = None
    if ci_doc.reference_invoices:
        first_invoice = frappe.get_doc("Sales Invoice", ci_doc.reference_invoices[0].sales_invoice)
        company = first_invoice.company
    
    if not company:
        company = frappe.defaults.get_user_default("Company")
    
    if not company:
        frappe.throw("Cannot determine company for payment entry")

    # Get customer from collective invoice
    customer = ci_doc.customer

    payment_entry = frappe.new_doc("Payment Entry")
    payment_entry.payment_type = "Receive"
    payment_entry.company = company
    payment_entry.posting_date = now_datetime().date()
    payment_entry.mode_of_payment = "Stripe"
    payment_entry.party_type = "Customer"
    payment_entry.party = customer
    
    # Get accounts
    paid_from = frappe.db.get_value("Company", company, "default_receivable_account")
    paid_to = frappe.db.get_value("Mode of Payment Account", 
                                  {"parent": "Stripe", "company": company}, 
                                  "default_account")
    
    if not paid_to:
        paid_to = frappe.db.get_value("Mode of Payment Account", 
                                      {"parent": "Cash", "company": company}, 
                                      "default_account")
    
    if not paid_from or not paid_to:
        frappe.throw("Payment accounts not configured properly")

    payment_entry.paid_from = paid_from
    payment_entry.paid_to = paid_to
    payment_entry.paid_amount = ci_doc.total_amount
    payment_entry.received_amount = ci_doc.total_amount
    payment_entry.target_exchange_rate = 1
    payment_entry.reference_no = reference_no
    payment_entry.reference_date = now_datetime().date()

    # Add all reference invoices to payment entry
    for ref_invoice in ci_doc.reference_invoices:
        si_doc = frappe.get_doc("Sales Invoice", ref_invoice.sales_invoice)
        outstanding_amount = min(si_doc.outstanding_amount, ref_invoice.outstanding)
        
        if outstanding_amount > 0:
            payment_entry.append("references", {
                "reference_doctype": "Sales Invoice",
                "reference_name": si_doc.name,
                "total_amount": si_doc.grand_total,
                "outstanding_amount": si_doc.outstanding_amount,
                "allocated_amount": outstanding_amount
            })

    payment_entry.insert(ignore_permissions=True)
    payment_entry.submit()

    frappe.msgprint(f"Payment Entry created: {payment_entry.name}")
    return payment_entry.name

@frappe.whitelist()
def create_stripe_url_collective(collective_invoice=None):
    """Create Stripe checkout URL for Collective Invoice"""
    if not collective_invoice:
        collective_invoice = frappe.local.request.args.get("collective_invoice")

    if not collective_invoice:
        frappe.throw(_("Missing Collective Invoice"))
    
    ci_doc = frappe.get_doc("Collective Invoices", collective_invoice)

    stripe_settings = frappe.get_single("Stripe Payment Settings")
    sk = stripe_settings.get_password("secret_key")
    
    if not sk:
        frappe.throw(_("Stripe secret key not configured"))
    
    import stripe
    stripe.api_key = sk

    currency = "usd"

    try:
        # Create line items description
        invoice_list = []
        for ref_invoice in ci_doc.reference_invoices:
            invoice_list.append(ref_invoice.sales_invoice)
        
        description = f"Collective Payment for {ci_doc.name} - Customer: {ci_doc.customer}"
        if len(invoice_list) <= 3:
            description += f" - Invoices: {', '.join(invoice_list)}"
        else:
            description += f" - {len(invoice_list)} invoices"

        # Get the site URL properly
        site_url = frappe.utils.get_url()
        
        session = stripe.checkout.Session.create(
            payment_method_types=["card", "us_bank_account"],
            
            line_items=[{
                "price_data": {
                    "currency": currency,
                    "product_data": {
                        "name": description,
                        "description": f"Payment for collective invoice covering {len(ci_doc.reference_invoices)} sales invoices"
                    },
                    "unit_amount": int(flt(ci_doc.total_amount) * 100),
                },
                "quantity": 1,
            }],
            mode="payment",
            
            # Use the full module path for the success/cancel URLs
            success_url=f"{site_url}/api/method/stripe_pay.methods.stripe_collective.handle_collective_success_callback?collective_invoice={ci_doc.name}",
            cancel_url=f"{site_url}/api/method/stripe_pay.methods.stripe_collective.handle_collective_failure_callback?collective_invoice={ci_doc.name}",
            
            metadata={
                "collective_invoice": ci_doc.name,
                "customer": ci_doc.customer,
                "total_amount": str(ci_doc.total_amount),
                "invoice_count": str(len(ci_doc.reference_invoices))
            },
            
            customer_creation="if_required",
            billing_address_collection="auto",
            
            custom_fields=[
                {
                    "key": "collective_invoice_number",
                    "label": {
                        "type": "custom",
                        "custom": "Collective Invoice Number"
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

        # Store session info in collective invoice
        ci_doc.db_set("custom_stripe_session_id", session.id)
        if session.get("payment_intent"):
            ci_doc.db_set("custom_stripe_payment_intent_id", session.payment_intent)

        frappe.db.commit()

        return {
            "session_id": session.id,
            "url": session.url,
            "payment_methods": ["card", "us_bank_account"]
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Stripe Collective Session Creation Failed")
        frappe.throw(_("Stripe Checkout Session creation failed: ") + str(e))

@frappe.whitelist(allow_guest=True)
def handle_collective_success_callback():
    """Handle successful payment for collective invoice"""
    try:
        collective_invoice_id = frappe.local.request.args.get("collective_invoice")
        frappe.log_error(f"Collective Callback Received for: {collective_invoice_id}", "Collective Success Callback")

        if not collective_invoice_id:
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = "/app/collective-invoices"
            return

        # Check if collective invoice exists
        if not frappe.db.exists("Collective Invoices", collective_invoice_id):
            frappe.log_error(f"Collective Invoice {collective_invoice_id} not found", "Collective Invoice Error")
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = "/app/collective-invoices"
            return

        ci_doc = frappe.get_doc("Collective Invoices", collective_invoice_id)
        frappe.log_error(f"Loaded Collective Invoice: {ci_doc.name}", "Collective Invoice Loaded")

        if ci_doc.docstatus != 1:
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = f"/app/collective-invoices/{collective_invoice_id}"
            return

        # Get company from first reference invoice
        company = None
        if ci_doc.reference_invoices:
            first_invoice = frappe.get_doc("Sales Invoice", ci_doc.reference_invoices[0].sales_invoice)
            company = first_invoice.company

        if not company:
            company = frappe.defaults.get_user_default("Company")

        if not company:
            frappe.log_error("No company found for payment entry", "Company Error")
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = f"/app/collective-invoices/{collective_invoice_id}?payment_status=company_error"
            return

        # Initialize Stripe
        stripe_settings = frappe.get_single("Stripe Payment Settings")
        sk = stripe_settings.get_password("secret_key")
        
        if not sk:
            frappe.log_error("Stripe secret key not found", "Stripe Config Error")
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = f"/app/collective-invoices/{collective_invoice_id}?payment_status=config_error"
            return

        stripe.api_key = sk
        
        # Get payment method info if session exists
        session_id = getattr(ci_doc, 'custom_stripe_session_id', None)
        if session_id:
            try:
                session = stripe.checkout.Session.retrieve(session_id)
                if session.payment_intent:
                    payment_intent = stripe.PaymentIntent.retrieve(session.payment_intent)
                    payment_method = stripe.PaymentMethod.retrieve(payment_intent.payment_method)
                    frappe.log_error(f"Payment method used: {payment_method.type}", "Collective Payment Method Info")
            except Exception as e:
                frappe.log_error(f"Error retrieving payment info: {str(e)}", "Payment Info Error")

        # Get accounts
        paid_from = frappe.get_cached_value("Company", company, "default_receivable_account")
        paid_to = frappe.get_cached_value("Mode of Payment Account", 
                                          {"parent": "Stripe", "company": company}, 
                                          "default_account")
        
        if not paid_to:
            paid_to = frappe.get_cached_value("Mode of Payment Account", 
                                              {"parent": "Cash", "company": company}, 
                                              "default_account")

        if not paid_from or not paid_to:
            frappe.log_error("Paid From or Paid To account missing", "Collective Payment Account Error")
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = f"/app/collective-invoices/{collective_invoice_id}?payment_status=account_error"
            return

        # Create payment entry
        payment_entry = frappe.new_doc("Payment Entry")
        payment_entry.payment_type = "Receive"
        payment_entry.company = company
        payment_entry.posting_date = nowdate()
        payment_entry.mode_of_payment = "Stripe"
        payment_entry.party_type = "Customer"
        payment_entry.party = ci_doc.customer
        payment_entry.paid_from = paid_from
        payment_entry.paid_to = paid_to
        payment_entry.paid_amount = ci_doc.total_amount
        payment_entry.received_amount = ci_doc.total_amount
        payment_entry.target_exchange_rate = 1
        
        # Set reference details
        if session_id:
            payment_entry.reference_no = session_id
            payment_entry.reference_date = nowdate()
        else:
            payment_entry.reference_no = f"Collective-{ci_doc.name}"
            payment_entry.reference_date = nowdate()

        # Add all reference invoices to payment entry
        total_allocated = 0
        for ref_invoice in ci_doc.reference_invoices:
            try:
                si_doc = frappe.get_doc("Sales Invoice", ref_invoice.sales_invoice)
                outstanding_amount = min(si_doc.outstanding_amount, ref_invoice.outstanding)
                
                if outstanding_amount > 0:
                    payment_entry.append("references", {
                        "reference_doctype": "Sales Invoice",
                        "reference_name": si_doc.name,
                        "total_amount": si_doc.grand_total,
                        "outstanding_amount": si_doc.outstanding_amount,
                        "allocated_amount": outstanding_amount
                    })
                    total_allocated += outstanding_amount
            except Exception as e:
                frappe.log_error(f"Error processing reference invoice {ref_invoice.sales_invoice}: {str(e)}", "Reference Invoice Error")

        if total_allocated == 0:
            frappe.log_error("No amount allocated to references", "Allocation Error")
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = f"/app/collective-invoices/{collective_invoice_id}?payment_status=allocation_error"
            return

        # Save and submit payment entry
        payment_entry.insert(ignore_permissions=True)
        payment_entry.submit()
        frappe.db.commit()

        frappe.log_error(f"Collective Payment Entry Created: {payment_entry.name}", "Payment Entry Success")

        # Create transfer log
        if session_id:
            create_stripe_transfer_log(
                session_id, 
                "paid", 
                "Collective Invoices", 
                ci_doc.name
            )

        # Redirect back to collective invoice with success message
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = f"/app/collective-invoices/{collective_invoice_id}?payment_status=success&payment_entry={payment_entry.name}"
        return

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Collective Payment Callback Error")
        collective_invoice_id = frappe.local.request.args.get("collective_invoice", "")
        if collective_invoice_id:
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = f"/app/collective-invoices/{collective_invoice_id}?payment_status=error"
        else:
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = "/app/collective-invoices"
        return

@frappe.whitelist(allow_guest=True)
def handle_collective_failure_callback():
    """Handle payment failure/cancellation for collective invoice"""
    collective_invoice_id = frappe.local.request.args.get("collective_invoice", "")
    frappe.log_error(f"Collective payment cancelled for invoice {collective_invoice_id}", "Collective Payment Cancelled")
    
    if collective_invoice_id:
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = f"/app/collective-invoices/{collective_invoice_id}?payment_status=cancelled"
    else:
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = "/app/collective-invoices"
    return

# Keep the existing utility functions
def create_stripe_transfer_log(reference_id, status, reference_doc, reference_name):
    """Create transfer log entry"""
    doc = frappe.new_doc("Stripe Transfer Details")
    doc.reference_id = reference_id
    doc.status = status
    doc.datetime = now_datetime()
    doc.reference_doc = reference_doc
    doc.refrence_name = reference_name  # Note: keeping the typo from original
    doc.account = connected_account_id
    doc.insert(ignore_permissions=True)
    frappe.db.commit() 
    frappe.msgprint(f"Stripe Transfer Log created: {doc.name}")
    return doc.name

@frappe.whitelist()
def check_collective_transfer_status(account, reference_id):
    """Check status of collective invoice transfer"""
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

# Keep existing webhook and other functions unchanged
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
        # Check if it's a collective invoice or sales invoice
        collective_invoice = session.metadata.get('collective_invoice')
        sales_invoice = session.metadata.get('sales_invoice')
        
        if collective_invoice:
            frappe.log_error(f"Checkout session completed for collective invoice {collective_invoice}", "Stripe Webhook")
        elif sales_invoice:
            frappe.log_error(f"Checkout session completed for sales invoice {sales_invoice}", "Stripe Webhook")
    except Exception as e:
        frappe.log_error(f"Error processing checkout session: {str(e)}", "Stripe Webhook")

def handle_payment_intent_succeeded(payment_intent):
    """Handle successful payment intent"""
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

# Keep existing Sales Invoice functions for backward compatibility
@frappe.whitelist()
def create_stripe_payment(sales_invoice):
    """Original function for Sales Invoice payments"""
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

@frappe.whitelist()
def create_stripe_url(sales_invoice=None):
    """Original function for Sales Invoice Stripe URL"""
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
            
            success_url=get_url(f"/api/method/stripe_pay.methods.stripe.handle_success_callback?invoice={si_doc.name}"),
            cancel_url=get_url(f"/api/method/stripe_pay.methods.stripe.handle_failure_callback?invoice={si_doc.name}"),
            
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
    """Original success callback for Sales Invoice"""
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