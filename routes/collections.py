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

    @app.route("/customer-amount-update")
    @login_required

    @app.route("/api/customer-amount-update/lookup", methods=["POST"])
    @login_required

    @app.route("/api/customer-amount-update/upload", methods=["POST"])
    @login_required

    @app.route("/api/customer-amount-update/add-customer", methods=["POST"])
    @login_required

    @app.route("/api/customer-amount-update/bulk-validate", methods=["POST"])
    @login_required

    @app.route("/api/customer-amount-update/bulk-upload", methods=["POST"])
    @login_required

    @app.route("/api/customer-amount-update/delete", methods=["POST"])
    @login_required

    @app.route("/api/customer-amount-update/delete-all", methods=["POST"])
    @login_required

    @app.route("/collection_sheet")
    @login_required

    @app.route("/edit_payment/<int:payment_id>", methods=["GET", "POST"])
    @login_required

    @app.route("/delete_payment/<int:payment_id>")
    @login_required
