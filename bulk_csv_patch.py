from datetime import datetime

from flask import jsonify, request
from flask_login import current_user, login_required

from models import db, Customer, Payment


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

    valid_count = sum(1 for r in parsed if r["valid"])
    return jsonify({
        "valid_count": valid_count,
        "invalid_count": len(parsed) - valid_count,
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
