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

    @app.route("/add_customer", methods=["POST"])
    @login_required
    def add_customer():
    
        customer_id = request.form["customer_id"].strip()
    
        existing = Customer.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).first()
    
        if existing:
            flash(f"Customer ID '{customer_id}' already exists.", "danger")
            return redirect(url_for("dashboard"))
    
        # FIX: float() on bad/missing input used to raise an uncaught
        # ValueError -> 500 error page instead of a friendly flash message.
        try:
            loan_amount = float(request.form["loan_amount"])
            daily_due = float(request.form["daily_due"])
        except (ValueError, TypeError):
            flash("Loan Amount and Daily Due must be valid numbers.", "danger")
            return redirect(url_for("dashboard"))
    
        if loan_amount < 0 or daily_due < 0:
            flash("Loan Amount and Daily Due cannot be negative.", "danger")
            return redirect(url_for("dashboard"))
    
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        address = request.form.get("address", "").strip()
    
        if not end_date and start_date:
            try:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                # Fixed 3-month loan term, regardless of loan amount / daily due
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
            name=request.form["name"],
            mobile=request.form["mobile"],
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
            flash("Customer Added Successfully", "success")
    
        except IntegrityError:
            db.session.rollback()
            flash(f"Customer ID '{customer_id}' already exists.", "danger")
    
        except Exception as e:
            db.session.rollback()
            flash(f"Error: {str(e)}", "danger")
    
        return redirect(url_for("dashboard"))
    # ==========================
    # ADD PAYMENT
    # ==========================

    @app.route("/customer_ledger")
    @login_required
    def customer_ledger():
        customers = Customer.query.filter_by(user_id=current_user.id).all()
        customers = _sort_customers(customers)
    
        # FIX (Bug): customer_ledger.html reads customer.alert to render the
        # Alert column and drive the "Collection Required" filter, but that
        # attribute was never set here -> every row silently showed "Active"
        # and the filter option matched nothing.
        for customer in customers:
            customer.alert = _compute_alert(customer)
    
        return render_template("customer_ledger.html", customers=customers)
    
    
    # ==========================
    # EXPORT CUSTOMER LEDGER EXCEL
    # (Fixed: now filters by user_id)
    # ==========================

    @app.route("/export_customers")
    @login_required
    def export_customers():
        wb = Workbook()
        ws = wb.active
        ws.title = "Customers"
    
        headers = [
            "Customer ID", "Name", "Mobile", "Loan Amount",
            "Daily Due", "Total Paid", "Balance", "Status"
        ]
        ws.append(headers)
    
        # FIX: filter by current user (was missing user_id filter)
        customers = Customer.query.filter_by(user_id=current_user.id).all()
        customers = _sort_customers(customers)
    
        for customer in customers:
            ws.append([
                customer.customer_id,
                customer.name,
                customer.mobile,
                customer.loan_amount,
                customer.daily_due,
                customer.total_paid,
                customer.remaining_balance,
                customer.status
            ])
    
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
    
        return send_file(
            output,
            as_attachment=True,
            download_name="customer_ledger.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    
    
    # ==========================
    # CUSTOMER DETAILS
    # ==========================

    @app.route("/customer/<customer_id>")
    @login_required
    def customer_details(customer_id):
        customer = Customer.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).first_or_404()
    
        payments = Payment.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).all()
    
        # Calculate day-based fields from start_date and end_date
        total_days = "-"
        days_passed = "-"
        remaining_days = "-"
    
        try:
            if customer.start_date and customer.end_date:
                fmt = "%Y-%m-%d"
                start = datetime.strptime(str(customer.start_date), fmt)
                end   = datetime.strptime(str(customer.end_date),   fmt)
                today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
                # Inclusive counting: the start date itself counts as Day 1.
                total_days = (end - start).days + 1
    
                if today < start:
                    # Loan hasn't started yet
                    days_passed = 0
                else:
                    days_passed = (today - start).days + 1
                    days_passed = min(days_passed, total_days)
    
                remaining_days = max(0, total_days - days_passed)
        except (ValueError, TypeError):
            pass  # leave defaults as "-" if dates are missing / malformed
    
        return render_template(
            "customer_details.html",
            customer=customer,
            payments=payments,
            total_days=total_days,
            days_passed=days_passed,
            remaining_days=remaining_days
        )
    
    
    # ==========================
    # EDIT CUSTOMER
    # ==========================

    @app.route("/edit_customer/<customer_id>", methods=["GET", "POST"])
    @login_required
    def edit_customer(customer_id):
        customer = Customer.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).first_or_404()
    
        if request.method == "POST":
            # FIX: float() on bad/missing input used to raise an uncaught
            # ValueError -> 500 error page instead of a friendly flash message.
            try:
                new_loan_amount = float(request.form["loan_amount"])
                new_daily_due = float(request.form["daily_due"])
            except (ValueError, TypeError):
                flash("Loan Amount and Daily Due must be valid numbers.", "danger")
                return redirect(url_for("edit_customer", customer_id=customer_id))
    
            if new_loan_amount < 0 or new_daily_due < 0:
                flash("Loan Amount and Daily Due cannot be negative.", "danger")
                return redirect(url_for("edit_customer", customer_id=customer_id))
    
            customer.name        = request.form["name"]
            customer.mobile      = request.form["mobile"]
            customer.address     = request.form.get("address", customer.address or "")
            customer.loan_amount = new_loan_amount
            customer.daily_due   = new_daily_due
            customer.end_date    = request.form.get("end_date", customer.end_date or "")
            customer.remaining_balance = customer.loan_amount - customer.total_paid
    
            # Re-apply the same Closed/Active rule used everywhere else so
            # editing the loan amount can't leave status out of sync with
            # the recalculated balance (e.g. increasing the loan on a
            # previously "Closed" customer used to leave it stuck "Closed").
            if customer.remaining_balance <= 0:
                customer.remaining_balance = 0
                customer.status = "Closed"
            else:
                customer.status = "Active"
    
            # FIX (Missing rollback): a DB error mid-commit (lost connection,
            # constraint violation) previously left the session in a broken
            # state with no rollback, which can poison subsequent requests
            # sharing the same pooled connection.
            try:
                db.session.commit()
                flash("Customer Updated Successfully")
            except Exception as e:
                db.session.rollback()
                flash(f"Error updating customer: {str(e)}", "danger")
            return redirect(url_for("customer_ledger"))
    
        return render_template("edit_customer.html", customer=customer)
    
    
    # ==========================
    # DELETE CUSTOMER
    # ==========================

    @app.route("/delete_customer/<customer_id>")
    @login_required
    def delete_customer(customer_id):
        customer = Customer.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).first()
    
        if customer:
            Payment.query.filter_by(
                customer_id=customer_id,
                user_id=current_user.id
            ).delete()
    
            db.session.delete(customer)
            db.session.commit()
            flash("Customer Deleted Successfully")
    
        return redirect(url_for("customer_ledger"))
    
    
    # ==========================
    # EDIT PAYMENT
    # ==========================

    @app.route("/export_customer/<customer_id>")
    @login_required
    def export_customer(customer_id):
        customer = Customer.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).first_or_404()
    
        payments = Payment.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).all()
    
        wb = Workbook()
        ws = wb.active
        ws.title = "Customer Statement"
    
        ws.append(["Customer ID", customer.customer_id])
        ws.append(["Name", customer.name])
        ws.append(["Mobile", customer.mobile])
        ws.append(["Loan Amount", customer.loan_amount])
        ws.append(["Daily Due", customer.daily_due])
        ws.append(["Total Paid", customer.total_paid])
        ws.append(["Balance", customer.remaining_balance])
        ws.append(["Status", customer.status])
        ws.append([])
        ws.append(["Payment History"])
        ws.append(["Date", "Amount"])
    
        for payment in payments:
            ws.append([payment.payment_date, payment.amount])
    
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
    
        return send_file(
            output,
            as_attachment=True,
            download_name=f"{customer.customer_id}_statement.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    
    
    # ==========================
    # CUSTOMER STATEMENT PDF
    # (Tamil Language Rendering Fixed)
    # ==========================

    @app.route("/customer_statement_pdf/<customer_id>")
    @login_required
    def customer_statement_pdf(customer_id):
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.pagesizes import A4
    
        customer = Customer.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).first_or_404()
    
        payments = Payment.query.filter_by(
            customer_id=customer_id,
            user_id=current_user.id
        ).all()
    
        # Register both Tamil and Latin fonts
        normal_font, bold_font, has_tamil = _register_tamil_font()
    
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        styles = getSampleStyleSheet()
    
        # Use NotoSans (Latin) as base font; _pdf_text() switches to NotoSansTamil
        # per-character for Tamil Unicode ranges, so both scripts render correctly.
        # _register_tamil_font returns (tamil_font, bold_font, has_tamil);
        # retrieve the latin font name directly from the cache for the base style.
        _reg = _FONT_CACHE.get("result", (normal_font, normal_font, bold_font, has_tamil))
        base_font = _reg[1]   # latin_font (NotoSans)
        hdr_font  = _reg[2]   # bold font  (NotoSans-Bold)
    
        normal_style = ParagraphStyle(
            "StmtNormal",
            parent=styles["Normal"],
            fontName=base_font
        )
    
        elements = []
        _add_company_pdf_header(elements, normal_style, "CUSTOMER STATEMENT")
    
        # _pdf_text() wraps Tamil chars with NotoSansTamil, Latin stays NotoSans
        info_lines = [
            _pdf_text(f"Customer ID: {customer.customer_id}"),
            _pdf_text(f"Name: {customer.name}"),
            _pdf_text(f"Mobile: {customer.mobile}"),
            _pdf_text(f"Loan Amount: RS-{customer.loan_amount}"),
            _pdf_text(f"Total Paid: RS-{customer.total_paid}"),
            _pdf_text(f"Balance: RS-{customer.remaining_balance}"),
        ]
        for line in info_lines:
            elements.append(Paragraph(line, normal_style))
    
        elements.append(Spacer(1, 15))
    
        data = [["No", "Date", "Amount"]]
        for index, payment in enumerate(payments, start=1):
            data.append([
                Paragraph(_pdf_text(str(index)), normal_style),
                Paragraph(_pdf_text(payment.payment_date), normal_style),
                Paragraph(_pdf_text(f"RS-{payment.amount}"), normal_style)
            ])
    
        table = Table(data)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 1, colors.black),
            ("FONTNAME", (0, 0), (-1, 0), hdr_font),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
        ]))
    
        elements.append(table)
        doc.build(elements)
        buffer.seek(0)
    
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"{customer.customer_id}_statement.pdf",
            mimetype="application/pdf"
        )
    
    
    # ==========================
    # BACKUP DATABASE
    # ==========================
