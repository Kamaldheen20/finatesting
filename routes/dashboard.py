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

    @app.route("/dashboard")
    @login_required
    def dashboard():
        customers = Customer.query.filter_by(user_id=current_user.id).all()
        customers = _sort_customers(customers)
    
        total_customers = len(customers)
        active_customers = len([c for c in customers if c.status == "Active"])
        closed_customers = len([c for c in customers if c.status == "Closed"])
    
        total_loan = sum(c.loan_amount for c in customers)
        total_paid = sum(c.total_paid for c in customers)
        total_balance = sum(c.remaining_balance for c in customers)
    
        today = datetime.now().strftime("%Y-%m-%d")
        current_month = datetime.now().strftime("%Y-%m")
    
        # FIX (Performance): the old code loaded EVERY payment row for the
        # user into Python just to sum two numbers. At ~1,000 customers with
        # years of daily payment history that's an ever-growing, unbounded
        # query on every single dashboard load. Let Postgres do the sum.
        from sqlalchemy import func
    
        today_collection = db.session.query(
            func.coalesce(func.sum(Payment.amount), 0)
        ).filter(
            Payment.user_id == current_user.id,
            Payment.payment_date == today
        ).scalar()
    
        month_collection = db.session.query(
            func.coalesce(func.sum(Payment.amount), 0)
        ).filter(
            Payment.user_id == current_user.id,
            Payment.payment_date.like(f"{current_month}%")
        ).scalar()
    
        return render_template(
            "dashboard.html",
            customers=customers,
            total_customers=total_customers,
            active_customers=active_customers,
            closed_customers=closed_customers,
            total_loan=total_loan,
            total_paid=total_paid,
            total_balance=total_balance,
            today_collection=today_collection,
            month_collection=month_collection
        )
    
    
    # ==========================
    # ADD CUSTOMER
    # ==========================
