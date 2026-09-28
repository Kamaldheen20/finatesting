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

    @app.route("/company_settings", methods=["GET", "POST"])
    @login_required
    def company_settings():
        settings = CompanySettings.query.filter_by(user_id=current_user.id).first()
    
        if not settings:
            settings = CompanySettings(user_id=current_user.id)
            db.session.add(settings)
            db.session.commit()
    
        if request.method == "POST":
            settings.company_name = request.form["company_name"]
            settings.address = request.form["address"]
            settings.phone = request.form["phone"]
    
            try:
                db.session.commit()
                flash("Settings Saved Successfully")
            except Exception as e:
                db.session.rollback()
                flash(f"Error saving settings: {str(e)}", "danger")
            return redirect(url_for("company_settings"))
    
        return render_template("company_settings.html", settings=settings)
    
    
    # ==========================
    # LOGOUT
    # ==========================
