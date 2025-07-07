import stripe
import frappe
from frappe import _
from frappe.utils import flt, now_datetime

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
        # Try as a Transfer first (Transfers are from platform)
        transfer = stripe.Transfer.retrieve(reference_id)
        return {"status": transfer.status}
    except stripe.error.InvalidRequestError:
        try:
            # Try as a Payout (must include stripe_account)
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
def create_stripe_url(sales_invoice):
    si_doc = frappe.get_doc("Sales Invoice", sales_invoice)

    if si_doc.docstatus != 1:
        frappe.throw(_("Sales Invoice must be submitted before creating a payment."))

    stripe_settings = frappe.get_single("Stripe Payment Settings")
    sk = stripe_settings.get_password("secret_key")
    stripe.api_key = sk

    currency = ("USD").lower()

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": currency,
                    "product_data": {
                        "name": f"Payment for {si_doc.name}",
                    },
                    "unit_amount": int(flt(si_doc.grand_total) * 100),
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=frappe.utils.get_url(f"/success?invoice={si_doc.name}"),
            cancel_url=frappe.utils.get_url(f"/cancel?invoice={si_doc.name}"),
            metadata={
                "sales_invoice": si_doc.name,
                "customer": si_doc.customer
            }
        )

        # Store session ID and payment intent (if available)
        si_doc.db_set("stripe_session_id", session.id)
        if session.get("payment_intent"):
            si_doc.db_set("stripe_payment_intent_id", session.payment_intent)

        return {
            "session_id": session.id,
            "url": session.url
        }

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), "Stripe Session Creation Failed")
        frappe.throw(_("Stripe Checkout Session creation failed: ") + str(e))
