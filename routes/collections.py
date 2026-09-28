from datetime import datetime
from sqlalchemy.exc import IntegrityError
import io
import logging
import re

from flask import render_template, request, redirect, url_for, flash, send_file, session, jsonify
from flask_login import login_required, login_user, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from openpyxl import Workbook

from models import db, Admin, Customer, Payment, PendingCustomer, CompanySettings
from utils.helpers import _sort_customers, _compute_alert, _normalize_amount, _upsert_pending_customer
from utils.pdf_helpers import _register_tamil_font, _pdf_text, _add_company_pdf_header

logger = logging.getLogger(__name__)

def register(app):

    @app.route("/add_payment", methods=["POST"])
    @login_required
    def add_payment():
        # FIX: float() on bad/missing input used to raise an uncaught
        # ValueError -> 500 error page instead of a friendly flash message.
        try:
            amount = float(request.form["amount"])
        except (ValueError, TypeError):
            flash("Payment amount must be a valid number.", "danger")
            return redirect(url_for("dashboard"))
    
        if amount < 0:
            flash("Payment amount cannot be negative.", "danger")
            return redirect(url_for("dashboard"))
    
        # FIX: the dashboard's Daily Due Collection now uses a searchable
        # "Reg. No. - Name" field (datalist) instead of a <select>, backed by
        # a hidden #customer_id_hidden input that only gets filled in when the
        # typed text exactly matches a real customer. If nothing was matched
        # (blank search, JS didn't run, stale page, etc.) that hidden field
        # posts as "" - previously that empty string was accepted here, a
        # Payment row got created with customer_id="", and it silently never
        # showed up in the Collection Sheet / Daily Report / Customer Ledger
        # because nothing joins to an empty customer_id. Reject it up front
        # instead of saving an orphaned payment.
        customer_id = request.form.get("customer_id", "").strip()
    
        if not customer_id:
            flash("Please pick a customer from the search box before saving the payment.", "danger")
            return redirect(url_for("dashboard"))
    
        payment_date = request.form["payment_date"]
    
        # FIX (Transaction safety): lock the customer row for the duration of
        # this transaction with SELECT ... FOR UPDATE. Without this, two
        # payments submitted for the same customer at nearly the same time
        # could both read the same starting total_paid, and the second
        # commit would silently overwrite (lose) the first payment's balance
        # update - a real risk for a finance app where staff may double-tap
        # "Save" or two collectors record the same customer concurrently.
        # This is a no-op on SQLite but takes effect on PostgreSQL/Supabase.
        customer = Customer.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).with_for_update().first()
    
        # FIX: if the id made it through non-empty but still doesn't match any
        # customer of this user (typo, stale/duplicate id, wrong account),
        # the old code carried on anyway - inserting a Payment row that has
        # no matching Customer and, again, never appears in any report.
        if not customer:
            flash(f"No customer found with ID '{customer_id}'. Payment was not saved.", "danger")
            return redirect(url_for("dashboard"))
    
        # Check if a payment already exists for this customer on this date.
        # If it does, update it instead of inserting a duplicate row -
        # this prevents the amount from being double-counted when the
        # same id/date is submitted again (accidentally or otherwise).
        existing_payment = Payment.query.filter_by(
            customer_id=customer_id,
            payment_date=payment_date,
            user_id=current_user.id
        ).first()
    
        if existing_payment:
            old_amount = existing_payment.amount
            existing_payment.amount = amount
            amount_diff = amount - old_amount
    
            if customer:
                customer.total_paid += amount_diff
    
            flash("Existing payment for this date was updated (no duplicate created)")
        else:
            payment = Payment(
                customer_id=customer_id,
                payment_date=payment_date,
                amount=amount,
                user_id=current_user.id
            )
            db.session.add(payment)
    
            if customer:
                customer.total_paid += amount
    
            flash("Payment Added Successfully")
    
        if customer:
            customer.remaining_balance = customer.loan_amount - customer.total_paid
    
            if customer.remaining_balance <= 0:
                customer.remaining_balance = 0
                customer.status = "Closed"
            else:
                customer.status = "Active"
    
        db.session.commit()
    
        return redirect(url_for("dashboard"))
    
    
    # ==========================
    # CUSTOMER AMOUNT UPDATE
    # Fast, dedicated collection-entry page (replaces the old Daily Due
    # Collection form that used to live on the dashboard). Reuses the same
    # Customer/Payment models and financial logic as /add_payment, but:
    #   - Talks JSON over fetch() instead of doing full-page form posts.
    #   - Blocks duplicate collections for the same customer/date outright,
    #     rather than silently upserting like the legacy dashboard form did
    #     (see requirement: prevent accidental double-entry during fast,
    #     continuous reg-no scanning).
    #   - Never trusts customer_id / amount / date sent by JS; everything is
    #     re-validated server-side exactly like the legacy route.
    # The Working Date itself is still a client-only (localStorage) concept,
    # same as before - it's passed in on every request rather than stored
    # server-side, so this page automatically follows whatever Working Date
    # is set elsewhere in the app.
    # ==========================

    @app.route("/customer-amount-update")
    @login_required
    def customer_amount_update():
        return render_template("customer_amount_update.html")

    @app.route("/api/customer-amount-update/lookup", methods=["POST"])
    @login_required
    def api_customer_amount_update_lookup():
        data = request.get_json(silent=True) or {}
    
        customer_id = str(data.get("customer_id", "")).strip()
        working_date = str(data.get("working_date", "")).strip()
    
        if not customer_id:
            return jsonify({"error": "Please enter a registration number."}), 400
    
        customer = Customer.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).first()
    
        if not customer:
            return jsonify({
                "error": "Customer not found. Please check the registration number."
            }), 404
    
        already_collected = False
        if working_date:
            already_collected = Payment.query.filter_by(
                customer_id=customer.customer_id,
                payment_date=working_date,
                user_id=current_user.id
            ).first() is not None
    
        # ---- Overdue days (O.D column) ----
        # expected_paid = how much should have been collected by "today" if
        # every due day was paid, based on daily_due * days elapsed since
        # start_date (inclusive, same convention as customer_details()).
        # overdue_days = how many full daily_due installments behind the
        # customer currently is, based on what's actually been paid.
        overdue_days = 0
        try:
            daily_due = customer.daily_due or 0
            if customer.start_date and daily_due > 0:
                fmt = "%Y-%m-%d"
                start = datetime.strptime(str(customer.start_date), fmt)
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
                if today < start:
                    days_passed = 0
                else:
                    # Total days passed since start_date, uncapped by end_date,
                    # so OD reflects the real number of days overdue (e.g. 365+),
                    # not just up to the 3-month loan tenure.
                    days_passed = (today - start).days + 1
    
                expected_paid = daily_due * days_passed
                overdue_amount = expected_paid - (customer.total_paid or 0)
    
                if overdue_amount > 0:
                    overdue_days = int(overdue_amount // daily_due)
        except (ValueError, TypeError):
            overdue_days = 0
    
        return jsonify({
            "customer_id": customer.customer_id,
            "name": customer.name,
            "daily_due": customer.daily_due or 0,
            "remaining_balance": customer.remaining_balance or 0,
            "status": customer.status,
            "already_collected": already_collected,
            "overdue_days": overdue_days
        })

    @app.route("/api/customer-amount-update/upload", methods=["POST"])
    @login_required
    def api_customer_amount_update_upload():
        data = request.get_json(silent=True) or {}
    
        customer_id = str(data.get("customer_id", "")).strip()
        payment_date = str(data.get("payment_date", "")).strip()
        raw_amount = data.get("amount", "")
    
        if not customer_id:
            return jsonify({"error": "Customer registration number is required."}), 400
    
        if not payment_date:
            return jsonify({"error": "Working Date is missing. Please reload the page."}), 400
    
        try:
            datetime.strptime(payment_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid Working Date."}), 400
    
        # FIX: reject non-numeric / missing amounts before touching the DB,
        # same reasoning as /add_payment.
        try:
            amount = float(raw_amount)
        except (ValueError, TypeError):
            return jsonify({"error": "Please enter a valid amount."}), 400
    
        if amount <= 0:
            return jsonify({"error": "Amount must be greater than zero."}), 400
    
        try:
            # Lock the customer row for the duration of this transaction -
            # same reasoning as /add_payment: prevents two near-simultaneous
            # collections for the same customer from losing an update.
            customer = Customer.query.filter_by(
                customer_id=customer_id,
                user_id=current_user.id
            ).with_for_update().first()
    
            if not customer:
                db.session.rollback()
                return jsonify({
                    "error": "Customer not found. Please check the registration number."
                }), 404
    
            # Duplicate protection: unlike the legacy /add_payment route
            # (which updates an existing same-day payment instead of
            # rejecting it), this quick-entry flow must not silently
            # overwrite a prior collection - block it outright per spec.
            existing_payment = Payment.query.filter_by(
                customer_id=customer.customer_id,
                payment_date=payment_date,
                user_id=current_user.id
            ).with_for_update().first()
    
            if existing_payment:
                db.session.rollback()
                return jsonify({
                    "error": f"{customer.customer_id} has already been collected for this date."
                }), 409
    
            payment = Payment(
                customer_id=customer.customer_id,
                payment_date=payment_date,
                amount=amount,
                user_id=current_user.id
            )
            db.session.add(payment)
    
            customer.total_paid = (customer.total_paid or 0) + amount
            customer.remaining_balance = customer.loan_amount - customer.total_paid
    
            if customer.remaining_balance <= 0:
                customer.remaining_balance = 0
                customer.status = "Closed"
            else:
                customer.status = "Active"
    
            db.session.commit()
    
        except Exception:
            db.session.rollback()
            logging.exception("Error saving collection via Customer Amount Update")
            return jsonify({
                "error": "Something went wrong while saving. Please try again."
            }), 500
    
        return jsonify({
            "success": True,
            "customer_id": customer.customer_id,
            "name": customer.name,
            "amount": amount,
            "total_paid": customer.total_paid,
            "remaining_balance": customer.remaining_balance,
            "status": customer.status,
            "message": f"₹{amount:,.0f} successfully added for {customer.customer_id}."
        })

    @app.route("/api/customer-amount-update/add-customer", methods=["POST"])
    @login_required
    def api_customer_amount_update_add_customer():
        data = request.get_json(silent=True) or {}
    
        customer_id = str(data.get("customer_id", "")).strip()
        name = str(data.get("name", "")).strip()
        mobile = str(data.get("mobile", "")).strip()
        address = str(data.get("address", "")).strip()
        start_date = str(data.get("start_date", "")).strip()
        end_date = str(data.get("end_date", "")).strip()
    
        if not customer_id:
            return jsonify({"error": "Registration number is required."}), 400
        if not name:
            return jsonify({"error": "Customer name is required."}), 400
    
        try:
            loan_amount = float(data.get("loan_amount", 0))
            daily_due = float(data.get("daily_due", 0))
        except (ValueError, TypeError):
            return jsonify({"error": "Loan Amount and Daily Due must be valid numbers."}), 400
    
        if loan_amount < 0 or daily_due < 0:
            return jsonify({"error": "Loan Amount and Daily Due cannot be negative."}), 400
    
        existing = Customer.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).first()
        if existing:
            return jsonify({"error": f"Customer ID '{customer_id}' already exists."}), 409
    
        if not end_date and start_date:
            try:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                month = start_dt.month - 1 + 3
                year = start_dt.year + month // 12
                month = month % 12 + 1
                day = min(start_dt.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
                end_dt = start_dt.replace(year=year, month=month, day=day)
                end_date = end_dt.strftime("%Y-%m-%d")
            except ValueError:
                end_date = ""
    
        customer = Customer(
            customer_id=customer_id,
            name=name,
            mobile=mobile,
            address=address,
            loan_amount=loan_amount,
            daily_due=daily_due,
            total_paid=0,
            remaining_balance=loan_amount,
            status="Active",
            start_date=start_date,
            end_date=end_date,
            user_id=current_user.id
        )
    
        try:
            db.session.add(customer)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"error": f"Customer ID '{customer_id}' already exists."}), 409
        except Exception:
            db.session.rollback()
            logger.exception("Error adding customer from bulk CSV validation")
            return jsonify({"error": "Could not add the customer. Please try again."}), 500
    
        return jsonify({
            "success": True,
            "customer": {
                "customer_id": customer.customer_id,
                "name": customer.name,
                "mobile": customer.mobile,
                "address": customer.address or "",
                "loan_amount": customer.loan_amount,
                "daily_due": customer.daily_due,
                "remaining_balance": customer.remaining_balance,
                "status": customer.status,
                "start_date": customer.start_date or "",
                "end_date": customer.end_date or ""
            },
            "message": f"Customer {customer.customer_id} added successfully."
        })

    @app.route("/api/customer-amount-update/bulk-validate", methods=["POST"])
    @login_required
    def api_customer_amount_update_bulk_validate():
        """Validate a CSV-shaped list of collection rows without writing data."""
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
        valid_count = 0
        pending_count = 0
        pending_to_save = []
    
        for index, item in enumerate(rows, start=1):
            customer_id = str((item or {}).get("customer_id", "")).strip()
            raw_amount = (item or {}).get("amount", "")
            message = ""
            valid = True
            name = ""
    
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
    
            customer = None
            if valid:
                customer = Customer.query.filter_by(
                    customer_id=customer_id, user_id=current_user.id
                ).first()
                if not customer:
                    valid, message = False, "Customer not found."
                    if amount is not None and amount > 0:
                        # Queue missing customers during validation itself.
                        # Keep the original working date and amount so the
                        # record is available even if the user never clicks
                        # "Upload Valid Customers".
                        pending_to_save.append(
                            (customer_id, amount, working_date, message)
                        )
                        pending_count += 1
                else:
                    name = customer.name or ""
                    existing = Payment.query.filter_by(
                        customer_id=customer_id,
                        payment_date=working_date,
                        user_id=current_user.id
                    ).first()
                    if existing:
                        valid, message = False, "Already collected for this date."
    
            if valid:
                valid_count += 1
    
            results.append({
                "row": int((item or {}).get("csv_row", index)),
                "customer_id": customer_id,
                "name": name,
                "valid": valid,
                "message": message
            })
    
        # Persist the complete pending queue in one transaction after all
        # rows have been validated. This is safer than committing once per row
        # and guarantees the pending records exist before the 200 response is
        # returned to the browser.
        try:
            for customer_id, amount, payment_date, reason in pending_to_save:
                _upsert_pending_customer(customer_id, amount, payment_date, reason)
            if pending_to_save:
                db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Could not persist %s pending customer row(s) during CSV validation",
                len(pending_to_save)
            )
            return jsonify({
                "error": "CSV validation completed, but the pending customer records could not be saved."
            }), 500
    
        return jsonify({
            "valid_count": valid_count,
            "invalid_count": len(results) - valid_count,
            "pending_count": pending_count,
            "rows": results
        })

    @app.route("/api/customer-amount-update/bulk-upload", methods=["POST"])
    @login_required
    def api_customer_amount_update_bulk_upload():
        """Atomically save a validated collection batch."""
        data = request.get_json(silent=True) or {}
        working_date = str(data.get("working_date", "")).strip()
        rows = data.get("rows")
    
        try:
            datetime.strptime(working_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid Working Date."}), 400
        if not isinstance(rows, list) or not rows:
            return jsonify({"error": "No CSV rows were supplied."}), 400
        if len(rows) > 10000:
            return jsonify({"error": "Maximum 10,000 CSV rows can be imported at once."}), 400
    
        # Validate everything again immediately before writing. The preview is
        # never treated as authorization for a financial write.
        seen = set()
        prepared = []
        try:
            for item in rows:
                customer_id = str((item or {}).get("customer_id", "")).strip()
                if not customer_id or customer_id in seen:
                    raise ValueError("CSV contains a missing or duplicate registration number.")
                seen.add(customer_id)
    
                try:
                    amount = float(_normalize_amount((item or {}).get("amount", "")))
                except (ValueError, TypeError):
                    raise ValueError(f"Invalid amount for {customer_id}.")
                if amount <= 0:
                    raise ValueError(f"Amount must be greater than zero for {customer_id}.")
    
                customer = Customer.query.filter_by(
                    customer_id=customer_id, user_id=current_user.id
                ).with_for_update().first()
                if not customer:
                    raise ValueError(f"Customer {customer_id} was not found.")
    
                existing = Payment.query.filter_by(
                    customer_id=customer_id,
                    payment_date=working_date,
                    user_id=current_user.id
                ).with_for_update().first()
                if existing:
                    raise ValueError(f"{customer_id} has already been collected for this date.")
    
                prepared.append((customer, amount))
    
            result_rows = []
            total_amount = 0
            for customer, amount in prepared:
                payment = Payment(
                    customer_id=customer.customer_id,
                    payment_date=working_date,
                    amount=amount,
                    user_id=current_user.id
                )
                db.session.add(payment)
                customer.total_paid = (customer.total_paid or 0) + amount
                customer.remaining_balance = customer.loan_amount - customer.total_paid
                if customer.remaining_balance <= 0:
                    customer.remaining_balance = 0
                    customer.status = "Closed"
                else:
                    customer.status = "Active"
    
                total_amount += amount
                result_rows.append({
                    "customer_id": customer.customer_id,
                    "name": customer.name,
                    "amount": amount,
                    "remaining_balance": customer.remaining_balance,
                    "status": customer.status
                })
    
            db.session.commit()
            return jsonify({
                "success": True,
                "count": len(result_rows),
                "total_amount": total_amount,
                "rows": result_rows,
                "message": f"{len(result_rows)} payment(s) uploaded successfully"
            })
    
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400
        except Exception:
            db.session.rollback()
            logger.exception("Bulk CSV collection upload failed")
            return jsonify({"error": "Bulk upload failed. No entries were saved."}), 500

    @app.route("/api/customer-amount-update/delete", methods=["POST"])
    @login_required
    def api_customer_amount_update_delete():
        # Undo a payment created via the Customer Amount Update screen, e.g.
        # after an accidental wrong-amount upload, so the customer can be
        # re-added and the correct amount entered. Reverses the exact math
        # /api/customer-amount-update/upload applied.
        data = request.get_json(silent=True) or {}
    
        customer_id = str(data.get("customer_id", "")).strip()
        payment_date = str(data.get("payment_date", "")).strip()
    
        if not customer_id:
            return jsonify({"error": "Customer registration number is required."}), 400
    
        if not payment_date:
            return jsonify({"error": "Working Date is missing. Please reload the page."}), 400
    
        try:
            datetime.strptime(payment_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid Working Date."}), 400
    
        try:
            customer = Customer.query.filter_by(
                customer_id=customer_id,
                user_id=current_user.id
            ).with_for_update().first()
    
            if not customer:
                db.session.rollback()
                return jsonify({
                    "error": "Customer not found. Please check the registration number."
                }), 404
    
            payment = Payment.query.filter_by(
                customer_id=customer_id,
                payment_date=payment_date,
                user_id=current_user.id
            ).with_for_update().first()
    
            if not payment:
                db.session.rollback()
                return jsonify({
                    "error": "No payment found for this customer on this date."
                }), 404
    
            amount = payment.amount or 0
            db.session.delete(payment)
    
            customer.total_paid = (customer.total_paid or 0) - amount
            if customer.total_paid < 0:
                customer.total_paid = 0
            customer.remaining_balance = customer.loan_amount - customer.total_paid
    
            if customer.remaining_balance <= 0:
                customer.remaining_balance = 0
                customer.status = "Closed"
            else:
                customer.status = "Active"
    
            db.session.commit()
    
        except Exception:
            db.session.rollback()
            logging.exception("Error deleting collection via Customer Amount Update")
            return jsonify({
                "error": "Something went wrong while deleting. Please try again."
            }), 500
    
        return jsonify({
            "success": True,
            "customer_id": customer.customer_id,
            "amount": amount,
            "total_paid": customer.total_paid,
            "remaining_balance": customer.remaining_balance,
            "status": customer.status,
            "message": f"Deleted ₹{amount:,.0f} for {customer.customer_id}."
        })

    @app.route("/api/customer-amount-update/delete-all", methods=["POST"])
    @login_required
    def api_customer_amount_update_delete_all():
        """Delete all collection payments for the current user and working date."""
        data = request.get_json(silent=True) or {}
        payment_date = str(data.get("payment_date", "")).strip()
    
        if not payment_date:
            return jsonify({"error": "Working Date is missing."}), 400
    
        try:
            datetime.strptime(payment_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid Working Date."}), 400
    
        try:
            payments = Payment.query.filter_by(
                payment_date=payment_date,
                user_id=current_user.id
            ).all()
    
            if not payments:
                return jsonify({
                    "success": True,
                    "deleted_count": 0,
                    "total_amount": 0,
                    "message": f"No collection entries found for {payment_date}."
                })
    
            customer_ids = list({p.customer_id for p in payments})
            customers = Customer.query.filter(
                Customer.customer_id.in_(customer_ids),
                Customer.user_id == current_user.id
            ).all()
            customers_by_id = {c.customer_id: c for c in customers}
    
            total_amount = 0
            for payment in payments:
                amount = float(payment.amount or 0)
                total_amount += amount
                customer = customers_by_id.get(payment.customer_id)
                if customer:
                    customer.total_paid = max(0, (customer.total_paid or 0) - amount)
    
            for customer in customers:
                customer.remaining_balance = customer.loan_amount - customer.total_paid
                if customer.remaining_balance <= 0:
                    customer.remaining_balance = 0
                    customer.status = "Closed"
                else:
                    customer.status = "Active"
    
            deleted_count = len(payments)
            for payment in payments:
                db.session.delete(payment)
    
            db.session.commit()
    
            return jsonify({
                "success": True,
                "deleted_count": deleted_count,
                "total_amount": total_amount,
                "message": f"Deleted {deleted_count} collection entr{'y' if deleted_count == 1 else 'ies'} for {payment_date}."
            })
    
        except Exception:
            db.session.rollback()
            logging.exception("Error deleting all collections via Customer Amount Update")
            return jsonify({
                "error": "Something went wrong while deleting the entries. No changes were saved."
            }), 500
    
    
    # ==========================
    # COLLECTION SHEET
    # ==========================

    @app.route("/collection_sheet")
    @login_required
    def collection_sheet():
        selected_month = request.args.get(
            "month",
            datetime.now().strftime("%Y-%m")
        )
    
        customers = Customer.query.filter_by(user_id=current_user.id).all()
        payments = Payment.query.filter_by(user_id=current_user.id).all()
    
        customers = _sort_customers(customers)
    
        # FIX (500 error): this bucketing used to happen inside the Jinja
        # template with payment.payment_date.split('-')[2]|int, run inside
        # THREE nested loops (customer x day x payment) for every page
        # load. Any payment_date that wasn't a clean "YYYY-MM-DD" string -
        # blank, None, or a "YYYY-MM-DD HH:MM:SS" string left over from an
        # Excel restore - raised an uncaught IndexError/ValueError deep
        # inside template rendering, which Flask turns into a 500 for the
        # whole page with no useful message. Doing it here in Python lets
        # us skip bad rows instead of crashing, and log them so they're
        # easy to find and fix in the data.
        daily_totals = {}                    # {customer_id: {day: amount}}
        day_totals = {d: 0 for d in range(1, 32)}
        month_totals = {}                    # {customer_id: total}
        month_collection = 0
    
        for payment in payments:
            pd = (payment.payment_date or "").strip()
            if not pd.startswith(selected_month):
                continue
            try:
                day = int(pd.split("-")[2][:2])
            except (IndexError, ValueError):
                logger.warning(
                    f"collection_sheet: skipping payment id={payment.id} "
                    f"with unparseable payment_date={pd!r}"
                )
                continue
            if not (1 <= day <= 31):
                continue
    
            cust_days = daily_totals.setdefault(payment.customer_id, {})
            cust_days[day] = cust_days.get(day, 0) + payment.amount
    
            day_totals[day] += payment.amount
            month_totals[payment.customer_id] = (
                month_totals.get(payment.customer_id, 0) + payment.amount
            )
            month_collection += payment.amount
    
        total_paid_all = sum(c.total_paid or 0 for c in customers)
        total_balance_all = sum(c.remaining_balance or 0 for c in customers)
    
        return render_template(
            "collection_sheet.html",
            customers=customers,
            selected_month=selected_month,
            daily_totals=daily_totals,
            day_totals=day_totals,
            month_totals=month_totals,
            month_collection=month_collection,
            total_paid_all=total_paid_all,
            total_balance_all=total_balance_all,
        )
    
    
    # ==========================
    # DAILY REPORT
    # ==========================

    @app.route("/edit_payment/<int:payment_id>", methods=["GET", "POST"])
    @login_required
    def edit_payment(payment_id):
        payment = Payment.query.filter_by(
            id=payment_id,
            user_id=current_user.id
        ).first_or_404()
    
        if request.method == "POST":
            # FIX (Transaction safety): lock the customer row, same reasoning
            # as add_payment - prevents lost updates if two edits land
            # concurrently on the same customer's balance.
            customer = Customer.query.filter_by(
                customer_id=payment.customer_id,
                user_id=current_user.id
            ).with_for_update().first()
    
            # FIX (Bug): if the customer record this payment points to was
            # deleted separately (data drift) this used to raise
            # AttributeError on customer.total_paid below -> 500 error.
            if not customer:
                flash("Cannot update this payment: its customer record no longer exists.", "danger")
                return redirect(url_for("dashboard"))
    
            old_amount = payment.amount
            try:
                new_amount = float(request.form["amount"])
            except (ValueError, TypeError):
                flash("Payment amount must be a valid number.", "danger")
                return redirect(url_for("edit_payment", payment_id=payment_id))
    
            if new_amount < 0:
                flash("Payment amount cannot be negative.", "danger")
                return redirect(url_for("edit_payment", payment_id=payment_id))
    
            new_date = request.form["payment_date"]
    
            # If moving this payment to a date that already has a different
            # payment for the same customer, merge into that one instead of
            # creating a second row for the same day.
            duplicate = Payment.query.filter(
                Payment.customer_id == payment.customer_id,
                Payment.payment_date == new_date,
                Payment.user_id == current_user.id,
                Payment.id != payment.id
            ).first()
    
            if duplicate:
                duplicate.amount += new_amount
                customer.total_paid = customer.total_paid - old_amount + new_amount
                db.session.delete(payment)
                flash("Merged into existing payment for that date")
            else:
                payment.payment_date = new_date
                payment.amount = new_amount
                customer.total_paid = customer.total_paid - old_amount + new_amount
                flash("Payment Updated Successfully")
    
            customer.remaining_balance = customer.loan_amount - customer.total_paid
    
            if customer.remaining_balance <= 0:
                customer.remaining_balance = 0
                customer.status = "Closed"
            else:
                customer.status = "Active"
    
            db.session.commit()
            return redirect(url_for("customer_details", customer_id=customer.customer_id))
    
        return render_template("edit_payment.html", payment=payment)
    
    
    # ==========================
    # DELETE PAYMENT
    # ==========================

    @app.route("/delete_payment/<int:payment_id>")
    @login_required
    def delete_payment(payment_id):
        payment = Payment.query.filter_by(
            id=payment_id,
            user_id=current_user.id
        ).first_or_404()
    
        # FIX (Transaction safety): lock the row, same reasoning as add_payment.
        customer = Customer.query.filter_by(
            customer_id=payment.customer_id,
            user_id=current_user.id
        ).with_for_update().first()
    
        # FIX (Bug): previously this crashed with AttributeError (500) if the
        # customer record no longer existed for this payment.
        if not customer:
            db.session.delete(payment)
            db.session.commit()
            flash("Payment Deleted Successfully (its customer record was already gone)")
            return redirect(url_for("dashboard"))
    
        customer.total_paid -= payment.amount
        customer.remaining_balance = customer.loan_amount - customer.total_paid
    
        if customer.remaining_balance <= 0:
            customer.remaining_balance = 0
            customer.status = "Closed"
        else:
            customer.status = "Active"
    
        db.session.delete(payment)
        db.session.commit()
    
        flash("Payment Deleted Successfully")
        return redirect(url_for("customer_details", customer_id=customer.customer_id))
    
    
    # ==========================
    # EXPORT SINGLE CUSTOMER EXCEL
    # ==========================
