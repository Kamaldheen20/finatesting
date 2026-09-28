"""Shared non-route helpers used by the finance application."""
from datetime import datetime
import re
from flask_login import current_user
from models import db, PendingCustomer

def _sort_customers(customers):
    def _id_sort_key(c):
        try:
            return (0, int(c.customer_id))
        except (ValueError, TypeError):
            return (1, c.customer_id)
    return sorted(customers, key=_id_sort_key)


# ==========================
# CUSTOMER ALERT HELPER
# Used by the Customer Ledger page. Computes a finer-grained status than
# the plain Active/Closed `status` field:
#   - "Settled"             -> status is already Closed (fully paid off)
#   - "Collection Required" -> loan term (end_date) has passed but a
#                               balance is still owed
#   - "Active"               -> everything else (within term, still owing)
# NOTE: this is display-only. It's set as a plain Python attribute on
# each Customer object for template rendering and is never committed to
# the database.
# ==========================

def _compute_alert(customer):
    if customer.status == "Closed":
        return "Settled"

    try:
        if customer.end_date:
            end_dt = datetime.strptime(str(customer.end_date), "%Y-%m-%d")
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            if today > end_dt and (customer.remaining_balance or 0) > 0:
                return "Collection Required"
    except (ValueError, TypeError):
        pass  # malformed/missing end_date -> fall through to "Active"

    return "Active"


# ==========================
# HOME / REDIRECT
# ==========================

def _normalize_amount(raw):
    """Best-effort clean-up of a CSV amount cell before float().

    Accepts values like "1,000", "Rs. 500", "₹1000", " 250.50 ", or
    "(200)" for a negative amount. Kept in sync with the client-side
    normalizeAmountCell() in customer_amount_update.html so validation
    and the actual save agree on what's a valid number, even if a row
    reaches this endpoint without going through the browser's CSV parser.
    """
    s = str(raw if raw is not None else "").strip()
    if not s:
        return s
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9.\-]", "", s)
    if negative and not s.startswith("-"):
        s = "-" + s
    return s

def _upsert_pending_customer(customer_id, amount, payment_date, reason="Customer not found"):
    pending = PendingCustomer.query.filter_by(
        customer_id=customer_id, payment_date=payment_date, user_id=current_user.id
    ).first()
    if pending:
        pending.amount = amount
        pending.reason = reason
        pending.updated_at = datetime.utcnow()
        return pending
    pending = PendingCustomer(
        customer_id=customer_id, amount=amount, payment_date=payment_date,
        reason=reason, user_id=current_user.id
    )
    db.session.add(pending)
    return pending
