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

    @app.route("/export_daily_report_excel/<date>")
    @login_required
    def export_daily_report_excel(date):
        payments = Payment.query.filter_by(
            payment_date=date,
            user_id=current_user.id
        ).all()
    
        wb = Workbook()
        ws = wb.active
        ws.title = "Daily Report"
        ws.append(["Date", "Customer ID", "Amount"])
    
        for payment in payments:
            ws.append([payment.payment_date, payment.customer_id, payment.amount])
    
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
    
        return send_file(
            output,
            as_attachment=True,
            download_name=f"Daily_Report_{date}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    
    
    # ==========================
    # EXPORT DAILY REPORT PDF
    # ==========================

    @app.route("/export_daily_report_pdf/<date>")
    @login_required
    def export_daily_report_pdf(date):
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.pagesizes import A4
    
        normal_font, bold_font, has_tamil = _register_tamil_font()
    
        payments = Payment.query.filter_by(
            payment_date=date,
            user_id=current_user.id
        ).all()
    
        total_collection = sum(p.amount for p in payments)
    
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        styles = getSampleStyleSheet()
    
        _reg = _FONT_CACHE.get("result", (None, "Helvetica", "Helvetica-Bold", False))
        base_font = _reg[1]   # NotoSans  (Latin)
        hdr_font  = _reg[2]   # NotoSans-Bold
    
        normal_style = ParagraphStyle(
            "DailyNormal",
            parent=styles["Normal"],
            fontName=base_font
        )
    
        elements = []
        _add_company_pdf_header(elements, normal_style, f"Daily Report - {date}")
    
        data = [["No", "Customer ID", "Date", "Amount"]]
        for i, p in enumerate(payments, start=1):
            data.append([
                Paragraph(_pdf_text(str(i)), normal_style),
                Paragraph(_pdf_text(str(p.customer_id)), normal_style),
                Paragraph(_pdf_text(p.payment_date), normal_style),
                Paragraph(_pdf_text(f"RS-{p.amount}"), normal_style)
            ])
    
        data.append([
            "",
            Paragraph(_pdf_text("TOTAL"), normal_style),
            "",
            Paragraph(_pdf_text(f"RS-{total_collection}"), normal_style)
        ])
    
        table = Table(data)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("BACKGROUND", (0, -1), (-1, -1), colors.lightyellow),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
            ("FONTNAME", (0, 0), (-1, 0), hdr_font),
            ("FONTNAME", (0, -1), (-1, -1), hdr_font),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
        ]))
    
        elements.append(table)
        doc.build(elements)
        buffer.seek(0)
    
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"Daily_Report_{date}.pdf",
            mimetype="application/pdf"
        )
    
    
    # ==========================
    # EXPORT MONTHLY COLLECTION EXCEL
    # ==========================

    @app.route("/export_collection/<month>")
    @login_required
    def export_collection(month):
        from sqlalchemy import func, cast, Integer
        from collections import defaultdict
    
        wb = Workbook()
        ws = wb.active
        ws.title = "Collection Sheet"
    
        headers = ["ID", "Name", "Loan"]
        for day in range(1, 32):
            headers.append(str(day))
        headers.extend(["Month Total", "Total Paid", "Balance", "Status"])
        ws.append(headers)
    
        customers = Customer.query.filter_by(user_id=current_user.id).all()
        customers = _sort_customers(customers)
    
        # ------------------------------------------------------------------
        # FIX (N+1 query / performance): the old code ran ONE
        # Payment.query.filter_by(...).all() PER CUSTOMER inside this loop,
        # plus a second full-table Payment.query.filter_by(...).all() below
        # for the summary row - for ~1,000 customers that's 1,000+ separate
        # round trips to Supabase on every export. This is the exact N+1
        # problem that was already fixed in export_collection_pdf but was
        # left unfixed here in the Excel export. We now pull every payment
        # for this user+month ONCE, pre-aggregated (SUM + GROUP BY) inside
        # Postgres, into a small in-memory lookup:
        #   payments_by_customer[customer_id][day] = total_amount_that_day
        # ------------------------------------------------------------------
        day_expr = cast(func.substr(Payment.payment_date, 9, 2), Integer)
        payment_query = (
            db.session.query(
                Payment.customer_id,
                day_expr.label("day"),
                func.sum(Payment.amount).label("day_total")
            )
            .filter(
                Payment.user_id == current_user.id,
                Payment.payment_date.like(f"{month}%")
            )
            .group_by(Payment.customer_id, day_expr)
        )
    
        payments_by_customer = defaultdict(dict)   # {customer_id: {day: amount}}
        day_totals = defaultdict(float)            # {day: total across ALL customers}
        month_total_all = 0
    
        for cust_id, day, day_total in payment_query.yield_per(500):
            payments_by_customer[cust_id][day] = day_total
            day_totals[day] += day_total
            month_total_all += day_total
    
        for customer in customers:
            row = [customer.customer_id, customer.name, customer.loan_amount]
            month_total = 0
            cust_payments = payments_by_customer.get(customer.customer_id, {})
    
            for day in range(1, 32):
                amt = cust_payments.get(day)
                if amt is None:
                    row.append("-")
                else:
                    row.append(amt)
                    month_total += amt
    
            row.extend([
                month_total,
                customer.total_paid,
                customer.remaining_balance,
                customer.status
            ])
            ws.append(row)
    
        # DAY TOTAL SUMMARY ROW - built entirely from the aggregates computed
        # in the single pass above (no second full-table Payment query).
        total_paid_all = sum(c.total_paid for c in customers)
        total_balance_all = sum(c.remaining_balance for c in customers)
    
        summary_row = ["DAY TOTAL", "", ""]
        for day in range(1, 32):
            summary_row.append(day_totals.get(day, 0))
    
        summary_row.extend([month_total_all, total_paid_all, total_balance_all, "-"])
        ws.append(summary_row)
    
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
    
        return send_file(
            output,
            as_attachment=True,
            download_name=f"Monthly_Collection_{month}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    
    
    # ==========================
    # EXPORT MONTHLY COLLECTION PDF
    # (Tamil Language Rendering Fixed)
    # ==========================

    @app.route("/export_collection_pdf/<month>")
    @login_required
    def export_collection_pdf(month):
        """Generate the 31-day monthly collection register as an A4 landscape PDF.
    
        Print layout:
          Reg No | Customer Name | Loan | Days 1-31 | Month | Paid | Balance
    
        The Status column is intentionally removed. All 31 collection days remain
        on the same A4 landscape sheet, and daily amounts are rendered as compact
        single-line values so digits do not wrap vertically.
        """
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.pagesizes import landscape, A4
        from reportlab.lib.enums import TA_CENTER, TA_LEFT
        from sqlalchemy import func, cast, Integer
        from collections import defaultdict
    
        _register_tamil_font()
    
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=landscape(A4),
            rightMargin=14,
            leftMargin=14,
            topMargin=18,
            bottomMargin=20,
            title=f"Monthly Collection Register - {month}",
        )
    
        styles = getSampleStyleSheet()
        font_cache = _FONT_CACHE.get(
            "result",
            ("Helvetica", "Helvetica", "Helvetica-Bold", False)
        )
        base_font = font_cache[1]
        hdr_font = font_cache[2]
    
        title_style = ParagraphStyle(
            "MonthlyA4Title",
            parent=styles["Normal"],
            fontName=hdr_font,
            fontSize=9.5,
            leading=11,
            alignment=TA_CENTER,
            textColor=colors.black,
            spaceAfter=0,
        )
        name_style = ParagraphStyle(
            "MonthlyA4Name",
            parent=styles["Normal"],
            fontName=base_font,
            fontSize=5.2,
            leading=5.5,
            alignment=TA_LEFT,
            textColor=colors.black,
        )
        head_style = ParagraphStyle(
            "MonthlyA4Head",
            parent=styles["Normal"],
            fontName=hdr_font,
            fontSize=4.2,
            leading=4.5,
            alignment=TA_CENTER,
            textColor=colors.black,
        )
        day_style = ParagraphStyle(
            "MonthlyA4Day",
            parent=styles["Normal"],
            fontName=base_font,
            fontSize=3.9,
            leading=4.1,
            alignment=TA_CENTER,
            textColor=colors.black,
        )
        summary_style = ParagraphStyle(
            "MonthlyA4Summary",
            parent=styles["Normal"],
            fontName=base_font,
            fontSize=4.4,
            leading=4.7,
            alignment=TA_CENTER,
            textColor=colors.black,
        )
        total_style = ParagraphStyle(
            "MonthlyA4Total",
            parent=styles["Normal"],
            fontName=hdr_font,
            fontSize=4.0,
            leading=4.3,
            alignment=TA_CENTER,
            textColor=colors.black,
        )
    
        elements = []
        _add_company_pdf_header(
            elements,
            ParagraphStyle(
                "MonthlyA4CompanyBase",
                parent=styles["Normal"],
                fontName=base_font,
                fontSize=8,
                leading=9,
                alignment=TA_CENTER,
            ),
            f"Monthly Collection Register - {month}",
        )
    
        # Use one compact register table. There is no artificial page split
        # between sections; ReportLab repeats the two header rows automatically.
        day_expr = cast(func.substr(Payment.payment_date, 9, 2), Integer)
        payment_query = (
            db.session.query(
                Payment.customer_id,
                day_expr.label("day"),
                func.sum(Payment.amount).label("day_total"),
            )
            .filter(
                Payment.user_id == current_user.id,
                Payment.payment_date.like(f"{month}%"),
            )
            .group_by(Payment.customer_id, day_expr)
        )
    
        payments_by_customer = defaultdict(dict)
        day_totals = defaultdict(float)
        month_total_all = 0.0
    
        for cust_id, day, day_total in payment_query.yield_per(500):
            value = float(day_total or 0)
            payments_by_customer[cust_id][day] = value
            day_totals[day] += value
            month_total_all += value
    
        customers = list(
            Customer.query
            .filter_by(user_id=current_user.id)
            .order_by(func.length(Customer.customer_id), Customer.customer_id)
            .yield_per(200)
        )
    
        total_paid_all = sum(float(c.total_paid or 0) for c in customers)
        total_balance_all = sum(float(c.remaining_balance or 0) for c in customers)
    
        def money(value):
            if value is None:
                return "-"
            number = float(value)
            if number == int(number):
                return f"₹{number:,.0f}"
            return f"₹{number:,.2f}"
    
        def day_amount(value):
            if value is None:
                return "-"
            number = float(value)
            # Day cells deliberately omit the currency symbol. This keeps the
            # 31 daily amounts on one line on A4 while preserving the exact value.
            if number == int(number):
                return f"{number:,.0f}"
            return f"{number:,.2f}"
    
        # Column widths are calculated for A4 landscape. The 31 daily columns
        # are deliberately compact, while the customer name remains readable.
        # Total width = 790.2pt, safely inside the printable A4 landscape width.
        col_widths = (
            [40, 92, 43]
            + [15.2] * 31
            + [45, 47, 52]
        )
    
        top_header = [
            Paragraph("<b>Reg No</b>", head_style),
            Paragraph("<b>Customer Name</b>", head_style),
            Paragraph("<b>Loan</b>", head_style),
            Paragraph("<b>COLLECTION DATE</b>", head_style),
        ] + [""] * 30 + [
            Paragraph("<b>Month</b>", head_style),
            Paragraph("<b>Paid</b>", head_style),
            Paragraph("<b>Balance</b>", head_style),
        ]
    
        day_header = (
            ["", "", ""]
            + [Paragraph(f"<b>{day}</b>", head_style) for day in range(1, 32)]
            + ["", "", ""]
        )
    
        table_rows = [top_header, day_header]
    
        for customer in customers:
            cust_payments = payments_by_customer.get(customer.customer_id, {})
            month_total = sum(
                float(cust_payments.get(day, 0) or 0)
                for day in range(1, 32)
            )
    
            row = [
                Paragraph(_pdf_text(str(customer.customer_id)), summary_style),
                Paragraph(_pdf_text(customer.name or ""), name_style),
                Paragraph(_pdf_text(money(customer.loan_amount)), summary_style),
            ]
    
            for day in range(1, 32):
                amount = cust_payments.get(day)
                row.append(
                    Paragraph(_pdf_text(day_amount(amount)), day_style)
                    if amount is not None
                    else "-"
                )
    
            row.extend([
                Paragraph(_pdf_text(money(month_total)), summary_style),
                Paragraph(_pdf_text(money(customer.total_paid)), summary_style),
                Paragraph(_pdf_text(money(customer.remaining_balance)), summary_style),
            ])
            table_rows.append(row)
    
        # DAY TOTAL remains inside the same table and has no Status cell.
        total_row = [
            Paragraph("<b>DAY TOTAL</b>", total_style),
            "",
            "",
        ]
        for day in range(1, 32):
            total_row.append(
                Paragraph(
                    _pdf_text(day_amount(day_totals.get(day, 0))),
                    total_style,
                )
            )
        total_row.extend([
            Paragraph(_pdf_text(money(month_total_all)), total_style),
            Paragraph(_pdf_text(money(total_paid_all)), total_style),
            Paragraph(_pdf_text(money(total_balance_all)), total_style),
        ])
        table_rows.append(total_row)
    
        table = Table(
            table_rows,
            colWidths=col_widths,
            repeatRows=2,
            hAlign="CENTER",
            splitByRow=1,
        )
    
        total_idx = len(table_rows) - 1
    
        style_cmds = [
            # White/black print layout.
            ("BACKGROUND", (0, 0), (-1, 1), colors.white),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
    
            # Two-level header.
            ("SPAN", (0, 0), (0, 1)),
            ("SPAN", (1, 0), (1, 1)),
            ("SPAN", (2, 0), (2, 1)),
            ("SPAN", (3, 0), (33, 0)),
            ("SPAN", (34, 0), (34, 1)),
            ("SPAN", (35, 0), (35, 1)),
            ("SPAN", (36, 0), (36, 1)),
    
            # Strong but compact print grid.
            ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#374151")),
            ("BOX", (0, 0), (-1, -1), 0.9, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("ALIGN", (1, 2), (1, -1), "LEFT"),
    
            # Compact row sizing prevents vertical wrapping in day cells.
            ("LEFTPADDING", (0, 0), (-1, -1), 0.8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0.8),
            ("TOPPADDING", (0, 0), (-1, 1), 2.2),
            ("BOTTOMPADDING", (0, 0), (-1, 1), 2.2),
            ("TOPPADDING", (0, 2), (-1, -1), 1.7),
            ("BOTTOMPADDING", (0, 2), (-1, -1), 1.7),
    
            # Slightly stronger separators around the summary columns.
            ("LINEBEFORE", (34, 0), (34, -1), 0.9, colors.black),
            ("LINEBEFORE", (0, 0), (0, -1), 0.9, colors.black),
            ("LINEAFTER", (36, 0), (36, -1), 0.9, colors.black),
        ]
    
        # Light alternating rows improve scanning without adding heavy ink.
        for row_idx in range(2, 2 + len(customers)):
            if (row_idx - 2) % 2 == 1:
                style_cmds.append(
                    ("BACKGROUND", (0, row_idx), (-1, row_idx), colors.HexColor("#f8fafc"))
                )
    
        # The DAY TOTAL row is part of the same table and gets a strong border.
        style_cmds.extend([
            ("SPAN", (0, total_idx), (2, total_idx)),
            ("BACKGROUND", (0, total_idx), (-1, total_idx), colors.white),
            ("FONTNAME", (0, total_idx), (-1, total_idx), hdr_font),
            ("LINEABOVE", (0, total_idx), (-1, total_idx), 1.1, colors.black),
            ("LINEBELOW", (0, total_idx), (-1, total_idx), 1.1, colors.black),
            ("BOX", (0, total_idx), (-1, total_idx), 1.0, colors.black),
            ("INNERGRID", (0, total_idx), (-1, total_idx), 0.5, colors.HexColor("#374151")),
            ("TOPPADDING", (0, total_idx), (-1, total_idx), 2.5),
            ("BOTTOMPADDING", (0, total_idx), (-1, total_idx), 2.5),
        ])
    
        table.setStyle(TableStyle(style_cmds))
        elements.append(table)
    
        def draw_footer(canvas, pdf_doc):
            canvas.saveState()
            canvas.setFont(base_font, 6.5)
            canvas.setFillColor(colors.HexColor("#6b7280"))
            canvas.drawString(14, 9, "Monthly Collection Register")
            canvas.drawRightString(
                pdf_doc.pagesize[0] - 14,
                9,
                f"Page {pdf_doc.page}",
            )
            canvas.restoreState()
    
        db.session.expunge_all()
        doc.build(elements, onFirstPage=draw_footer, onLaterPages=draw_footer)
        buffer.seek(0)
    
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"Monthly_Collection_{month}.pdf",
            mimetype="application/pdf",
        )
    
    # ==========================
    # CUSTOMER LEDGER
    # ==========================

    @app.route("/backup_database")
    @login_required
    def backup_database():
        # NOTE: the app now runs on PostgreSQL (Supabase), so there is no local
        # .db file to send anymore. Export all of this user's data to Excel
        # instead, with one sheet per table, so backups actually work.
        wb = Workbook()
    
        ws_customers = wb.active
        ws_customers.title = "Customers"
        ws_customers.append([
            "Customer ID", "Name", "Mobile", "Address", "Loan Amount",
            "Daily Due", "Total Paid", "Remaining Balance", "Status",
            "Start Date", "End Date"
        ])
        customers = Customer.query.filter_by(user_id=current_user.id).all()
        for c in customers:
            ws_customers.append([
                c.customer_id, c.name, c.mobile, c.address, c.loan_amount,
                c.daily_due, c.total_paid, c.remaining_balance, c.status,
                c.start_date, c.end_date
            ])
    
        ws_payments = wb.create_sheet("Payments")
        ws_payments.append(["Customer ID", "Payment Date", "Amount"])
        payments = Payment.query.filter_by(user_id=current_user.id).all()
        for p in payments:
            ws_payments.append([p.customer_id, p.payment_date, p.amount])
    
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
    
        backup_name = f"finance_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            output,
            as_attachment=True,
            download_name=backup_name,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    
    
    # ==========================
    # RESTORE DATABASE
    # ==========================

    @app.route("/restore_database", methods=["GET", "POST"])
    @login_required
    def restore_database():
        if request.method == "POST":
            backup_file = request.files.get("backup_file")
    
            if backup_file and backup_file.filename.endswith(".xlsx"):
                from openpyxl import load_workbook
                try:
                    wb = load_workbook(backup_file, data_only=True)
    
                    # Wipe this user's existing data before restoring
                    Payment.query.filter_by(user_id=current_user.id).delete()
                    Customer.query.filter_by(user_id=current_user.id).delete()
    
                    if "Customers" in wb.sheetnames:
                        ws = wb["Customers"]
                        for row in ws.iter_rows(min_row=2, values_only=True):
                            if not row or row[0] is None:
                                continue
                            (customer_id, name, mobile, address, loan_amount,
                             daily_due, total_paid, remaining_balance, status,
                             start_date, end_date) = row
                            db.session.add(Customer(
                                customer_id=str(customer_id),
                                name=name,
                                mobile=mobile,
                                address=address,
                                loan_amount=loan_amount or 0,
                                daily_due=daily_due or 0,
                                total_paid=total_paid or 0,
                                remaining_balance=remaining_balance or 0,
                                status=status or "Active",
                                start_date=start_date,
                                end_date=end_date,
                                user_id=current_user.id
                            ))
    
                    if "Payments" in wb.sheetnames:
                        ws = wb["Payments"]
                        for row in ws.iter_rows(min_row=2, values_only=True):
                            if not row or row[0] is None:
                                continue
                            customer_id, payment_date, amount = row
                            db.session.add(Payment(
                                customer_id=str(customer_id),
                                payment_date=str(payment_date),
                                amount=amount or 0,
                                user_id=current_user.id
                            ))
    
                    db.session.commit()
                    flash("Database Restored Successfully")
                except Exception as e:
                    db.session.rollback()
                    flash(f"Restore failed: {str(e)}", "danger")
    
                return redirect(url_for("dashboard"))
    
            flash("Please upload a valid .xlsx backup file")
    
        return render_template("restore_database.html")
    
    
    # ==========================
    # COMPANY SETTINGS
    # ==========================
