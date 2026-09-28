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

    @app.route("/pending-customers")
    @login_required
    def pending_customers():
        pending = PendingCustomer.query.filter_by(user_id=current_user.id).order_by(
            PendingCustomer.payment_date.desc(), PendingCustomer.customer_id.asc()
        ).all()
        return render_template("pending_customers.html", pending=pending)

    @app.route("/api/customer-amount-update/pending/<int:pending_id>", methods=["GET"])
    @login_required
    def api_pending_customer_get(pending_id):
        p = PendingCustomer.query.filter_by(id=pending_id, user_id=current_user.id).first_or_404()
        return jsonify({
            "id": p.id, "customer_id": p.customer_id, "amount": p.amount,
            "payment_date": p.payment_date, "reason": p.reason, "name": p.name or "",
            "mobile": p.mobile or "", "address": p.address or "",
            "loan_amount": p.loan_amount or 0, "daily_due": p.daily_due or 0,
            "start_date": p.start_date or "", "end_date": p.end_date or ""
        })

    @app.route("/api/customer-amount-update/pending/<int:pending_id>/save", methods=["POST"])
    @login_required
    def api_pending_customer_save(pending_id):
        data = request.get_json(silent=True) or {}
        p = PendingCustomer.query.filter_by(id=pending_id, user_id=current_user.id).first_or_404()
        customer_id = str(data.get("customer_id", p.customer_id)).strip()
        name = str(data.get("name", "")).strip()
        mobile = str(data.get("mobile", "")).strip()
        address = str(data.get("address", "")).strip()
        start_date = str(data.get("start_date", "")).strip()
        end_date = str(data.get("end_date", "")).strip()
    
        if not customer_id or not name:
            return jsonify({"error": "Registration number and customer name are required."}), 400
        try:
            loan_amount = float(data.get("loan_amount", 0))
            daily_due = float(data.get("daily_due", 0))
            amount = float(p.amount)
        except (ValueError, TypeError):
            return jsonify({"error": "Loan Amount, Daily Due and collection amount must be valid numbers."}), 400
        if loan_amount < 0 or daily_due < 0 or amount <= 0:
            return jsonify({"error": "Enter valid Loan Amount, Daily Due and collection amount."}), 400
    
        existing = Customer.query.filter_by(customer_id=customer_id, user_id=current_user.id).with_for_update().first()
        existing_payment = Payment.query.filter_by(
            customer_id=customer_id, payment_date=p.payment_date, user_id=current_user.id
        ).first()
        if existing_payment:
            return jsonify({"error": "A collection already exists for this customer and date."}), 409
    
        # If this customer was created from an earlier pending date, keep the
        # later pending rows as a persistent queue and apply this row directly
        # to the existing customer instead of asking the user to create the
        # customer again.
        if existing:
            try:
                existing.total_paid = (existing.total_paid or 0) + amount
                existing.remaining_balance = (existing.loan_amount or 0) - existing.total_paid
                if existing.remaining_balance <= 0:
                    existing.remaining_balance = 0
                    existing.status = "Closed"
                else:
                    existing.status = "Active"
                db.session.add(Payment(
                    customer_id=existing.customer_id,
                    payment_date=p.payment_date,
                    amount=amount,
                    user_id=current_user.id
                ))
                db.session.delete(p)
                db.session.commit()
            except Exception:
                db.session.rollback()
                logger.exception("Could not apply pending collection %s to existing customer", pending_id)
                return jsonify({"error": "Could not apply the pending collection. Nothing was changed."}), 500
            return jsonify({
                "success": True,
                "message": f"Collection ₹{amount:,.2f} applied to existing customer {existing.customer_id} for {p.payment_date}."
            })
    
        if not end_date and start_date:
            try:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                month = start_dt.month - 1 + 3
                year = start_dt.year + month // 12
                month = month % 12 + 1
                day = min(start_dt.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                                          31, 30, 31, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
                end_date = start_dt.replace(year=year, month=month, day=day).strftime("%Y-%m-%d")
            except ValueError:
                end_date = ""
    
        customer = Customer(
            customer_id=customer_id, name=name, mobile=mobile, address=address,
            loan_amount=loan_amount, daily_due=daily_due, total_paid=amount,
            remaining_balance=max(0, loan_amount - amount),
            status="Closed" if loan_amount - amount <= 0 else "Active",
            start_date=start_date, end_date=end_date, user_id=current_user.id
        )
        payment = Payment(customer_id=customer_id, payment_date=p.payment_date, amount=amount, user_id=current_user.id)
        try:
            db.session.add(customer)
            db.session.add(payment)
            db.session.delete(p)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"error": f"Customer ID '{customer_id}' already exists."}), 409
        except Exception:
            db.session.rollback()
            logger.exception("Could not create customer from pending row %s", pending_id)
            return jsonify({"error": "Could not save customer and collection. Nothing was changed."}), 500
        return jsonify({"success": True, "message": f"Customer {customer_id} added and {amount:,.2f} collected for {p.payment_date}."})

    @app.route("/api/pending-customers/clear-all", methods=["POST"])
    @login_required
    def api_pending_customer_clear_all():
        try:
            deleted = PendingCustomer.query.filter_by(user_id=current_user.id).delete(
                synchronize_session=False
            )
            db.session.commit()
            return jsonify({
                "success": True,
                "deleted_count": deleted,
                "message": f"{deleted} pending customer record(s) cleared."
            })
        except Exception:
            db.session.rollback()
            logger.exception("Could not clear pending customer queue")
            return jsonify({"error": "Could not clear the pending customer queue."}), 500

    @app.route("/api/customer-amount-update/pending/<int:pending_id>/delete", methods=["POST"])
    @login_required
    def api_pending_customer_delete(pending_id):
        p = PendingCustomer.query.filter_by(id=pending_id, user_id=current_user.id).first_or_404()
        db.session.delete(p)
        db.session.commit()
        return jsonify({"success": True, "message": "Pending customer removed."})
