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

    @app.route("/daily_report")
    @login_required
    def daily_report():
        selected_date = request.args.get(
            "date",
            datetime.now().strftime("%Y-%m-%d")
        )
    
        payments = Payment.query.filter_by(
            payment_date=selected_date,
            user_id=current_user.id
        ).all()
    
        total_collection = sum(payment.amount for payment in payments)
    
        return render_template(
            "daily_report.html",
            payments=payments,
            selected_date=selected_date,
            total_collection=total_collection
        )
    
    
    # ==========================
    # PENDING REPORT
    # ==========================

    @app.route("/pending_report")
    @login_required
    def pending_report():
        customers = Customer.query.filter(
            Customer.remaining_balance > 0,
            Customer.user_id == current_user.id
        ).all()
    
        total_pending = sum(
            customer.remaining_balance for customer in customers
        )
    
        return render_template(
            "pending_report.html",
            customers=customers,
            total_pending=total_pending
        )
    
    
    # ==========================
    # EXPORT DAILY REPORT EXCEL
    # ==========================
