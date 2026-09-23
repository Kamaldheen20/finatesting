from datetime import datetime

from flask import jsonify, request
from flask_login import current_user, login_required

from models import db, Customer, Payment, PendingCustomer


def _upsert_pending_customer(customer_id, amount, payment_date, reason="Customer not found"):
    pending = PendingCustomer.query.filter_by(
        customer_id=customer_id,
        payment_date=payment_date,
        user_id=current_user.id,
    ).first()
    if pending:
        pending.amount = amount
        pending.reason = reason
        pending.updated_at = datetime.utcnow()
        return pending
    pending = PendingCustomer(
        customer_id=customer_id,
        amount=amount,
        payment_date=payment_date,
        reason=reason,
        user_id=current_user.id,
    )
    db.session.add(pending)
    return pending


def _normalize_amount(raw):
    s = str(raw if raw is not None else "").strip()
    if not s:
        return s
    negative = s.startswith("(") and s.endswith(")")
    import re
    s = re.sub(r"[^0-9.\-]", "", s)
    if negative and not s.startswith("-"):
        s = "-" + s
    return s


@login_required
def bulk_validate_fast():
    """Fast bulk CSV validation: two database queries instead of one per row."""
    data = request.get_json(silent=True) or {}
    working_date = str(data.get("working_date", "")).strip()
    rows = data.get("rows")

    if not working_date:
        return jsonify({"error": "Working Date is missing."}), 400
    try:
        datetime.strptime(working_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid Working Date."}), 400
    if not isinstance(rows, list) or not rows:
        return jsonify({"error": "No CSV rows were supplied."}), 400
    if len(rows) > 10000:
        return jsonify({"error": "Maximum 10,000 CSV rows can be imported at once."}), 400

    seen = set()
    results = []
    parsed = []

    # First pass: validate CSV structure and amounts without touching DB.
    for index, item in enumerate(rows, start=1):
        item = item or {}
        customer_id = str(item.get("customer_id", "")).strip()
        raw_amount = item.get("amount", "")
        valid = True
        message = ""

        if not customer_id:
            valid, message = False, "Registration number is missing."
        elif customer_id in seen:
            valid, message = False, "Duplicate registration number in CSV."
        else:
            seen.add(customer_id)

        amount = None
        if valid:
            try:
                amount = float(_normalize_amount(raw_amount))
            except (ValueError, TypeError):
                valid, message = False, "Amount is not a valid number."
            if valid and amount <= 0:
                valid, message = False, "Amount must be greater than zero."

        parsed.append({
            "row": int(item.get("csv_row", index)),
            "customer_id": customer_id,
            "amount": amount,
            "valid": valid,
            "message": message,
            "name": "",
        })

    valid_ids = [r["customer_id"] for r in parsed if r["valid"]]
    if valid_ids:
        # One customer query for the complete CSV.
        customers = Customer.query.filter(
            Customer.user_id == current_user.id,
            Customer.customer_id.in_(valid_ids),
        ).all()
        customer_map = {c.customer_id: c for c in customers}

        # One payment query for the complete CSV/date.
        payments = Payment.query.filter(
            Payment.user_id == current_user.id,
            Payment.payment_date == working_date,
            Payment.customer_id.in_(valid_ids),
        ).all()
        paid_ids = {p.customer_id for p in payments}

        for result in parsed:
            if not result["valid"]:
                continue
            customer_id = result["customer_id"]
            customer = customer_map.get(customer_id)
            if not customer:
                result["valid"] = False
                result["message"] = "Customer not found."
            elif customer_id in paid_ids:
                result["valid"] = False
                result["message"] = "Already collected for this date."
            else:
                result["name"] = customer.name or ""

    # Missing customers must be persisted during validation. This fast
    # endpoint replaces the route in app.py at startup, so pending handling
    # has to live here as well.
    pending_rows = [
        r for r in parsed
        if (
            not r["valid"]
            and r["message"] == "Customer not found."
            and r["amount"] is not None
            and r["amount"] > 0
        )
    ]

    try:
        for r in pending_rows:
            _upsert_pending_customer(
                r["customer_id"],
                r["amount"],
                working_date,
                "Customer not found.",
            )
        if pending_rows:
            db.session.commit()
    except Exception:
        db.session.rollback()
        app_logger = getattr(request, "app", None)
        raise

    valid_count = sum(1 for r in parsed if r["valid"])
    return jsonify({
        "valid_count": valid_count,
        "invalid_count": len(parsed) - valid_count,
        "pending_count": len(pending_rows),
        "rows": [
            {
                "row": r["row"],
                "customer_id": r["customer_id"],
                "name": r["name"],
                "valid": r["valid"],
                "message": r["message"],
            }
            for r in parsed
        ],
    })


def install(app):
    endpoint = "api_customer_amount_update_bulk_validate"
    if endpoint in app.view_functions:
        app.view_functions[endpoint] = bulk_validate_fast
        app.logger.info("Installed optimized bulk CSV validation endpoint")
