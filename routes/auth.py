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

    @app.route("/")
    def home():
        return redirect(url_for("login"))
    
    
    # ==========================
    # LOGIN
    # ==========================

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form["username"]
            password = request.form["password"]
    
            admin = Admin.query.filter_by(username=username).first()
    
            if admin and check_password_hash(admin.password, password):
                login_user(admin)
                return redirect(url_for("dashboard"))
    
            flash("Invalid Username or Password")
    
        return render_template("login.html")
    
    
    # ==========================
    # REGISTER
    # ==========================

    @app.route("/register", methods=["GET", "POST"])
    def register():
        
    
        if request.method == "POST":
    
            username = request.form["username"].strip()
            mobile = request.form["mobile"].strip()
            password = request.form["password"]
    
            # Check username
            if Admin.query.filter_by(username=username).first():
                flash("Username already exists", "danger")
                return redirect(url_for("register"))
    
            # Check mobile
            if Admin.query.filter_by(mobile=mobile).first():
                flash("Mobile number already registered", "danger")
                return redirect(url_for("register"))
    
            admin = Admin(
                username=username,
                mobile=mobile,
                password=generate_password_hash(password)
            )
    
            db.session.add(admin)
            db.session.commit()
    
            flash("Account Created Successfully", "success")
            return redirect(url_for("login"))
        return render_template("register.html")
    #forget pass

    @app.route("/forgot_password", methods=["GET", "POST"])
    def forgot_password():
    
        if request.method == "POST":
    
            username = request.form["username"].strip()
            mobile = request.form["mobile"].strip()
            password = request.form["password"]
            confirm_password = request.form["confirm_password"]
    
            # Check password confirmation
            if password != confirm_password:
                flash("Passwords do not match.", "danger")
                return redirect(url_for("forgot_password"))
    
            # Find the user
            admin = Admin.query.filter_by(
                username=username,
                mobile=mobile
            ).first()
    
            if not admin:
                flash(
                    "Invalid Username or Mobile Number.",
                    "danger"
                )
                return redirect(url_for("forgot_password"))
    
            # Update password
            admin.password = generate_password_hash(password)
    
            db.session.commit()
    
            flash(
                "Password updated successfully. Please login.",
                "success"
            )
    
            return redirect(url_for("login"))
        return render_template("forgot_password.html")
    # ==========================
    # DASHBOARD
    # ==========================

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        session.clear()
        return redirect(url_for("login"))
    
    # ==========================
    # DATABASE INIT
    # ==========================
