import csv
import io
import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import (
    Response, abort, jsonify, redirect, render_template_string,
    request, session, url_for
)
from license_test_server import app, ADMIN_KEY, connect, key_hash

# Configure a strong LICENSE_SESSION_SECRET in Render. Never use the admin key as the session secret.
app.secret_key = __import__("os").getenv("LICENSE_SESSION_SECRET") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=__import__("datetime").timedelta(minutes=30),
)

LOGIN_HTML = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>License Admin Login</title><style>
*{box-sizing:border-box}body{font-family:Inter,Arial;background:#eef2f7;display:grid;place-items:center;min-height:100vh;margin:0;color:#172033}
.card{background:#fff;padding:32px;border-radius:18px;box-shadow:0 12px 40px #16213e18;width:min(390px,92vw)}
input,button{width:100%;padding:12px;margin-top:12px;border-radius:9px}input{border:1px solid #d7deea}button{border:0;background:#172033;color:#fff;font-weight:700;cursor:pointer}
.err{color:#b91c1c;margin-top:12px}.muted{color:#64748b;font-size:13px}
</style></head><body><div class="card"><h2>Finance License Admin</h2><p class="muted">Secure administrator sign in</p>
<form method="post"><input type="hidden" name="csrf_token" value="{{csrf_token}}">
<input name="admin_key" type="password" placeholder="Admin key" autocomplete="current-password" required>
<button type="submit">Sign in</button></form>{% if error %}<div class="err">{{error}}</div>{% endif %}</div></body></html>
"""

ADMIN_HTML = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Finance License Admin</title>
<style>
*{box-sizing:border-box}body{font-family:Inter,Arial;background:#f3f6fb;margin:0;padding:22px;color:#172033}
a{color:#334155}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:25px;margin:8px 0}h2{font-size:18px;margin-top:0}.muted{color:#64748b;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px;margin:18px 0}
.metric,.card{background:#fff;border:1px solid #e5eaf2;border-radius:14px;padding:17px;box-shadow:0 3px 12px #16213e06}
.metric strong{display:block;font-size:25px;margin-top:7px}.metric span{font-size:12px;color:#64748b}
input,select,textarea,button{font:inherit;padding:9px;border-radius:8px;margin:3px;max-width:100%}
input,select,textarea{border:1px solid #d6deea;background:white}button{border:0;cursor:pointer;font-weight:650}
.primary{background:#172033;color:white}.danger{background:#fee2e2;color:#991b1b}.warn{background:#fef3c7;color:#92400e}.secondary{background:#e8edf5;color:#243147}
.formgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}.formgrid input,.formgrid select,.formgrid textarea{width:100%;margin:0}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.tools input{min-width:210px}
.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:900px}th,td{padding:10px;border-bottom:1px solid #e8edf4;text-align:left;font-size:13px;vertical-align:top}th{color:#64748b;font-size:12px;background:#f8fafc}
.badge{display:inline-block;padding:4px 8px;border-radius:99px;background:#dcfce7;color:#166534;font-size:12px}.expired,.revoked{background:#fee2e2;color:#991b1b}.warning{background:#fef3c7;color:#92400e}
.msg{padding:12px;border-radius:9px;background:#dcfce7;color:#166534;margin:14px 0}.keybox{padding:12px;background:#fff7d6;border:1px solid #f4d77b;border-radius:9px;overflow-wrap:anywhere}
.actions{display:flex;gap:4px;flex-wrap:wrap}.actions form{display:inline}.small{font-size:12px}
details{margin:4px 0}summary{cursor:pointer;color:#334155;font-weight:650}
@media(max-width:600px){body{padding:12px}.card{padding:13px}}
</style></head><body>
<div class="top"><div><h1>Finance License Admin</h1><div class="muted">License lifecycle · devices · payments · audit</div></div>
<div class="tools"><a href="/admin/export.csv">Export CSV</a><a href="/admin/backup.json">Backup JSON</a><a href="/admin/audit">Audit log</a><form method="post" action="/admin/notify-expiring" onsubmit="return confirm('Send expiry warning emails to customers expiring within 30 days?');"><input type="hidden" name="csrf_token" value="{{csrf_token}}"><button class="secondary">Send expiry alerts</button></form><a href="/admin/diagnostics">Activation diagnostics</a><a href="/admin/logout">Logout</a></div></div>
{% if message %}<div class="msg">{{message}}</div>{% endif %}
{% if created_key %}<div class="keybox"><strong>Copy this new license key now. It will not be shown again:</strong><br>{{created_key}}</div>{% endif %}
<div class="grid">
<div class="metric"><span>Total licenses</span><strong>{{stats.total}}</strong></div>
<div class="metric"><span>Active</span><strong>{{stats.active}}</strong></div>
<div class="metric"><span>Expired</span><strong>{{stats.expired}}</strong></div>
<div class="metric"><span>Revoked</span><strong>{{stats.revoked}}</strong></div>
<div class="metric"><span>Expiring in 30 days</span><strong>{{stats.expiring}}</strong></div>
<div class="metric"><span>Payments recorded</span><strong>{{stats.payments}}</strong></div>
<div class="metric"><span>Recorded revenue</span><strong>{{"%.2f"|format(stats.revenue or 0)}}</strong></div>
</div>
<div class="card"><h2>Create license</h2>
<form method="post" action="/admin/create-web"><input type="hidden" name="csrf_token" value="{{csrf_token}}">
<div class="formgrid">
<input name="customer_name" placeholder="Customer / business name" required maxlength="180">
<input name="customer_email" type="email" placeholder="Customer email (for alerts)">
<input name="max_devices" type="number" min="1" max="1000" value="1" required>
<select name="plan"><option value="monthly">Monthly (30 days)</option><option value="yearly">Yearly (365 days)</option><option value="lifetime">Lifetime</option><option value="custom">Custom expiry</option></select>
<input name="expires_at" type="date" aria-label="Custom expiry date">
<input name="notes" placeholder="Internal notes (optional)" maxlength="2000">
</div><p class="muted">Lifetime licenses have no expiry. Custom expiry uses the selected date in UTC.</p>
<button class="primary" type="submit">Create license</button></form></div>
<div class="card"><h2>License management</h2>
<form class="tools" method="get" action="/admin">
<input name="q" value="{{q}}" placeholder="Search customer, ID, status or notes">
<select name="status"><option value="">All statuses</option>{% for st in ["active","revoked","expired"] %}<option value="{{st}}" {% if status_filter==st %}selected{% endif %}>{{st|title}}</option>{% endfor %}</select>
<button class="secondary">Search / filter</button><a href="/admin">Clear</a></form>
<div class="tablewrap"><table><thead><tr><th>ID / Customer</th><th>Status / Expiry</th><th>Devices</th><th>Payments</th><th>Created</th><th>Actions</th></tr></thead><tbody>
{% for x in licenses %}<tr>
<td><strong>#{{x.id}} · {{x.customer_name}}</strong><div class="muted">{{x.customer_email or ""}}</div><div class="muted">{{x.notes or ""}}</div></td>
<td><span class="badge {% if x.effective_status!='active' %}{{'expired' if x.effective_status=='expired' else 'revoked'}}{% endif %}">{{x.effective_status}}</span><div class="small">{{x.expires_at or "Never expires"}}</div></td>
<td>{{x.device_count}} / {{x.max_devices}}<details><summary>Manage devices</summary>
{% for d in x.device_rows %}<div class="small">{{d.device_id}}<br><span class="muted">Last seen: {{d.last_seen or "Unknown"}}</span>
<form method="post" action="/admin/device/{{d.id}}/remove" onsubmit="return confirm('Remove this device binding?');"><input type="hidden" name="csrf_token" value="{{csrf_token}}"><button class="warn small">Remove device</button></form></div>{% else %}<span class="muted">No devices activated.</span>{% endfor %}
<form method="post" action="/admin/license/{{x.id}}/reset-device" onsubmit="return confirm('Remove all registered devices for this license?');"><input type="hidden" name="csrf_token" value="{{csrf_token}}"><button class="warn">Reset all devices</button></form></details></td>
<td>{{"%.2f"|format(x.payment_total or 0)}}<details><summary>Record payment</summary>
<form method="post" action="/admin/license/{{x.id}}/payment"><input type="hidden" name="csrf_token" value="{{csrf_token}}">
<input name="amount" type="number" min="0" step="0.01" placeholder="Amount" required>
<input name="payment_date" type="date" value="{{today}}" required>
<select name="method"><option>UPI</option><option>Cash</option><option>Bank transfer</option><option>Card</option><option>Other</option></select>
<input name="reference" placeholder="Transaction reference"><input name="notes" placeholder="Payment notes">
<button class="primary">Save payment</button></form>
{% for p in x.payment_rows %}<div class="small">{{p.payment_date}} · {{p.amount}} · {{p.method}}{% if p.reference %} · {{p.reference}}{% endif %}</div>{% endfor %}
</details></td>
<td>{{x.created_at}}</td>
<td><div class="actions">
<details><summary>Edit / renew</summary><form method="post" action="/admin/license/{{x.id}}/edit"><input type="hidden" name="csrf_token" value="{{csrf_token}}">
<input name="customer_name" value="{{x.customer_name}}" placeholder="Customer name" required>
<input name="customer_email" type="email" value="{{x.customer_email or ''}}" placeholder="Customer email">
<input name="max_devices" type="number" min="1" max="1000" value="{{x.max_devices}}" required>
<input name="expires_at" type="date" value="{{x.expiry_date or ''}}">
<textarea name="notes" placeholder="Internal notes">{{x.notes or ''}}</textarea>
<button class="primary">Save details</button></form>
<form method="post" action="/admin/license/{{x.id}}/renew"><input type="hidden" name="csrf_token" value="{{csrf_token}}">
<select name="duration"><option value="30">Extend 30 days</option><option value="365">Extend 365 days</option><option value="lifetime">Set lifetime</option></select>
<button class="secondary">Renew</button></form></details>
{% if x.status=='active' %}<form method="post" action="/admin/license/{{x.id}}/revoke" onsubmit="return confirm('Revoke this license?');"><input type="hidden" name="csrf_token" value="{{csrf_token}}"><button class="warn">Revoke</button></form>{% else %}<form method="post" action="/admin/license/{{x.id}}/activate" onsubmit="return confirm('Reactivate this license?');"><input type="hidden" name="csrf_token" value="{{csrf_token}}"><button class="secondary">Activate</button></form>{% endif %}
<form method="post" action="/admin/license/{{x.id}}/delete" onsubmit="return confirm('Permanently delete this license, devices and payment records? This cannot be undone.');"><input type="hidden" name="csrf_token" value="{{csrf_token}}"><button class="danger">Delete</button></form>
</div></td></tr>{% else %}<tr><td colspan="6">No licenses match this search.</td></tr>{% endfor %}
</tbody></table></div></div>
<div class="card"><h2>Operational notes</h2><p class="muted">Payment totals include only payments manually recorded here. Export/backup contains license hashes and metadata, not plaintext license keys. Store backup files securely.</p></div>
</body></html>
"""

AUDIT_HTML = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>License Audit</title>
<style>body{font:14px Arial;background:#f3f6fb;padding:20px;color:#172033}table{width:100%;border-collapse:collapse;background:white}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}.wrap{overflow:auto}</style></head><body>
<h1>License audit log</h1><p><a href="/admin">Back to admin</a></p><div class="wrap"><table><tr><th>When (UTC)</th><th>License</th><th>Action</th><th>Details</th></tr>
{% for x in logs %}<tr><td>{{x.created_at}}</td><td>{{x.license_id or "—"}}</td><td>{{x.action}}</td><td>{{x.details or ""}}</td></tr>{% endfor %}</table></div></body></html>
"""

def logged_in():
    return session.get("license_admin") is True

def audit(conn, license_id, action, details=""):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO license_audit_logs (license_id, action, details) VALUES (%s, %s, %s)",
            (license_id, action[:100], details[:2000]),
        )

def redirect_message(message):
    return redirect(url_for("admin_panel", message=message))

def parse_expiry(value):
    value = (value or "").strip()
    if not value:
        return None
    from datetime import date, time
    parsed = date.fromisoformat(value)
    return datetime.combine(parsed, time.max, tzinfo=timezone.utc)

def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token

@app.before_request
def admin_csrf_guard():
    if request.method != "POST" or not request.path.startswith("/admin/"):
        return None
    if request.path == "/admin/login" or request.path == "/admin/create":
        return None
    if logged_in() and request.form.get("csrf_token", "") != session.get("_csrf_token"):
        abort(400, "Invalid or expired CSRF token")
    return None

@app.get("/")
def admin_home():
    return redirect(url_for("admin_login"))

@app.get("/admin/login")
def admin_login():
    if logged_in():
        return redirect(url_for("admin_panel"))
    return render_template_string(LOGIN_HTML, error=None, csrf_token=csrf_token())

@app.post("/admin/login")
def admin_login_submit():
    failures = int(session.get("login_failures", 0))
    if failures >= 8:
        return render_template_string(LOGIN_HTML, error="Too many failed attempts. Clear the session and try again later.", csrf_token=csrf_token()), 429
    if request.form.get("admin_key", "") != ADMIN_KEY:
        session["login_failures"] = failures + 1
        return render_template_string(LOGIN_HTML, error="Invalid admin key", csrf_token=csrf_token()), 401
    session.clear()
    session["license_admin"] = True
    session.permanent = True
    csrf_token()
    return redirect(url_for("admin_panel"))

@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.get("/admin")
def admin_panel():
    if not logged_in():
        return redirect(url_for("admin_login"))
    q = request.args.get("q", "").strip()[:180]
    status_filter = request.args.get("status", "").strip().lower()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status='active' AND (expires_at IS NULL OR expires_at >= NOW())) AS active,
                       COUNT(*) FILTER (WHERE status='revoked') AS revoked,
                       COUNT(*) FILTER (WHERE status='active' AND expires_at < NOW()) AS expired,
                       COUNT(*) FILTER (WHERE status='active' AND expires_at >= NOW() AND expires_at < NOW()+INTERVAL '30 days') AS expiring
                FROM licenses
            """)
            stats = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS payments, COALESCE(SUM(amount),0) AS revenue FROM license_payments")
            stats.update(cur.fetchone())
            where = []
            params = []
            if q:
                where.append("(CAST(l.id AS TEXT) ILIKE %s OR l.customer_name ILIKE %s OR l.status ILIKE %s OR COALESCE(l.notes,'') ILIKE %s)")
                params.extend([f"%{q}%"] * 4)
            if status_filter == "active":
                where.append("l.status='active' AND (l.expires_at IS NULL OR l.expires_at >= NOW())")
            elif status_filter == "expired":
                where.append("l.status='active' AND l.expires_at < NOW()")
            elif status_filter == "revoked":
                where.append("l.status='revoked'")
            sql = """
                SELECT l.*,
                       COUNT(DISTINCT d.id) AS device_count,
                       COALESCE((SELECT SUM(p.amount) FROM license_payments p WHERE p.license_id=l.id),0) AS payment_total,
                       CASE WHEN l.status='revoked' THEN 'revoked'
                            WHEN l.expires_at IS NOT NULL AND l.expires_at < NOW() THEN 'expired'
                            ELSE l.status END AS effective_status
                FROM licenses l LEFT JOIN devices d ON d.license_id=l.id
            """
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " GROUP BY l.id ORDER BY l.id DESC LIMIT 500"
            cur.execute(sql, params)
            licenses = cur.fetchall()
            for lic in licenses:
                expiry = lic.get("expires_at")
                lic["expiry_date"] = expiry.date().isoformat() if expiry else ""
                cur.execute("SELECT id, device_id, last_seen FROM devices WHERE license_id=%s ORDER BY id", (lic["id"],))
                lic["device_rows"] = cur.fetchall()
                cur.execute("SELECT id, amount, payment_date, method, reference FROM license_payments WHERE license_id=%s ORDER BY payment_date DESC, id DESC LIMIT 10", (lic["id"],))
                lic["payment_rows"] = cur.fetchall()
        return render_template_string(
            ADMIN_HTML, licenses=licenses, stats=stats, q=q, status_filter=status_filter,
            message=request.args.get("message", ""), created_key=session.pop("created_license_key", ""),
            csrf_token=csrf_token(), today=datetime.now(timezone.utc).date().isoformat(),
        )
    finally:
        conn.close()

@app.post("/admin/create-web")
def admin_create_web():
    if not logged_in():
        return redirect(url_for("admin_login"))
    customer_name = request.form.get("customer_name", "").strip()
    notes = request.form.get("notes", "").strip()
    customer_email = request.form.get("customer_email", "").strip()[:254]
    if customer_email and "@" not in customer_email:
        return redirect_message("Enter a valid customer email address.")
    try:
        max_devices = int(request.form.get("max_devices", "1"))
        plan = request.form.get("plan", "monthly")
        if plan == "monthly":
            expiry = datetime.now(timezone.utc) + __import__("datetime").timedelta(days=30)
        elif plan == "yearly":
            expiry = datetime.now(timezone.utc) + __import__("datetime").timedelta(days=365)
        elif plan == "lifetime":
            expiry = None
        elif plan == "custom":
            expiry = parse_expiry(request.form.get("expires_at"))
            if expiry is None:
                raise ValueError("Select a custom expiry date.")
        else:
            raise ValueError("Unknown license plan.")
    except (ValueError, TypeError, OverflowError) as exc:
        return redirect_message(str(exc) or "Invalid device limit or expiry.")
    if not customer_name or len(customer_name) > 180 or not 1 <= max_devices <= 1000:
        return redirect_message("Enter a valid customer name and device limit (1–1000).")
    license_key = "FIN-" + secrets.token_hex(9).upper()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO licenses (license_hash, customer_name, customer_email, max_devices, status, expires_at, notes)
                           VALUES (%s,%s,%s,%s,'active',%s,%s) RETURNING id""",
                        (key_hash(license_key), customer_name, customer_email or None, max_devices, expiry, notes))
            license_id = cur.fetchone()["id"]
        audit(conn, license_id, "license_created", f"Customer={customer_name}; devices={max_devices}; plan={plan}; expiry={expiry}")
        conn.commit()
        session["created_license_key"] = license_key
    except Exception:
        conn.rollback()
        app.logger.exception("Could not create license")
        return redirect_message("License creation failed. Check database connectivity and logs.")
    finally:
        conn.close()
    return redirect_message("License created. Copy the one-time key displayed below.")

@app.post("/admin/license/<int:license_id>/edit")
def admin_edit_license(license_id):
    if not logged_in(): return redirect(url_for("admin_login"))
    name = request.form.get("customer_name", "").strip()
    notes = request.form.get("notes", "").strip()
    customer_email = request.form.get("customer_email", "").strip()[:254]
    if customer_email and "@" not in customer_email:
        return redirect_message("Enter a valid customer email address.")
    try:
        max_devices = int(request.form.get("max_devices", "1"))
        expiry = parse_expiry(request.form.get("expires_at"))
    except (ValueError, TypeError):
        return redirect_message("Invalid device limit or expiry date.")
    if not name or len(name) > 180 or not 1 <= max_devices <= 1000:
        return redirect_message("Enter a valid customer name and device limit.")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM licenses WHERE id=%s FOR UPDATE", (license_id,))
            old = cur.fetchone()
            if not old: return redirect_message("License not found.")
            cur.execute("UPDATE licenses SET customer_name=%s,customer_email=%s,max_devices=%s,expires_at=%s,notes=%s WHERE id=%s",
                        (name, customer_email or None, max_devices, expiry, notes, license_id))
        audit(conn, license_id, "license_updated", f"name={old['customer_name']}→{name}; max_devices={old['max_devices']}→{max_devices}; expiry={old['expires_at']}→{expiry}; notes updated")
        conn.commit()
    finally: conn.close()
    return redirect_message("License details updated.")

@app.post("/admin/license/<int:license_id>/renew")
def admin_renew_license(license_id):
    if not logged_in(): return redirect(url_for("admin_login"))
    duration = request.form.get("duration", "30")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at,status FROM licenses WHERE id=%s FOR UPDATE", (license_id,))
            row = cur.fetchone()
            if not row: return redirect_message("License not found.")
            if duration == "lifetime":
                expiry = None
            elif duration in ("30", "365"):
                now = datetime.now(timezone.utc)
                base = row["expires_at"] if row["expires_at"] and row["expires_at"] > now else now
                expiry = base + __import__("datetime").timedelta(days=int(duration))
            else:
                return redirect_message("Invalid renewal duration.")
            cur.execute("UPDATE licenses SET expires_at=%s,status='active' WHERE id=%s", (expiry, license_id))
        audit(conn, license_id, "license_renewed", f"duration={duration}; new_expiry={expiry}; status reactivated")
        conn.commit()
    finally: conn.close()
    return redirect_message("License renewed and activated.")

@app.post("/admin/license/<int:license_id>/payment")
def admin_record_payment(license_id):
    if not logged_in(): return redirect(url_for("admin_login"))
    try:
        amount = Decimal(request.form.get("amount", ""))
        if not amount.is_finite() or amount < 0: raise InvalidOperation()
        payment_date = request.form.get("payment_date", "")
        datetime.strptime(payment_date, "%Y-%m-%d")
    except (InvalidOperation, ValueError):
        return redirect_message("Enter a valid payment amount and date.")
    method = request.form.get("method", "Other")[:40]
    reference = request.form.get("reference", "").strip()[:180]
    notes = request.form.get("notes", "").strip()[:1000]
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM licenses WHERE id=%s", (license_id,))
            if not cur.fetchone(): return redirect_message("License not found.")
            cur.execute("""INSERT INTO license_payments (license_id,amount,payment_date,method,reference,notes)
                           VALUES (%s,%s,%s,%s,%s,%s)""",
                        (license_id, amount, payment_date, method, reference, notes))
        audit(conn, license_id, "payment_recorded", f"amount={amount}; date={payment_date}; method={method}; reference={reference}")
        conn.commit()
    finally: conn.close()
    return redirect_message("Payment recorded.")

@app.post("/admin/device/<int:device_id>/remove")
def admin_remove_device(device_id):
    if not logged_in(): return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM devices WHERE id=%s RETURNING license_id,device_id", (device_id,))
            row = cur.fetchone()
        if row:
            audit(conn, row["license_id"], "device_removed", f"device_id={row['device_id']}")
        conn.commit()
    finally: conn.close()
    return redirect_message("Device removed." if row else "Device not found.")

@app.post("/admin/license/<int:license_id>/revoke")
def admin_revoke(license_id):
    return change_status(license_id, "revoked")

@app.post("/admin/license/<int:license_id>/activate")
def admin_activate(license_id):
    return change_status(license_id, "active")

def change_status(license_id, status):
    if not logged_in(): return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE licenses SET status=%s WHERE id=%s RETURNING customer_name", (status, license_id))
            row = cur.fetchone()
        if row: audit(conn, license_id, "status_changed", f"status={status}; customer={row['customer_name']}")
        conn.commit()
    finally: conn.close()
    return redirect_message("License " + status + "." if row else "License not found.")

@app.post("/admin/license/<int:license_id>/delete")
def admin_delete_license(license_id):
    if not logged_in(): return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT customer_name FROM licenses WHERE id=%s", (license_id,))
            row = cur.fetchone()
            if not row: return redirect_message("License not found.")
            # Preserve an audit record before cascading deletion.
            audit(conn, license_id, "license_deleted", f"customer={row['customer_name']}; license and dependent records permanently deleted")
            cur.execute("DELETE FROM licenses WHERE id=%s", (license_id,))
        conn.commit()
    finally: conn.close()
    return redirect_message("License and dependent devices/payments deleted permanently.")

@app.post("/admin/license/<int:license_id>/reset-device")
def admin_reset_device(license_id):
    if not logged_in(): return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM devices WHERE license_id=%s", (license_id,))
            count = cur.rowcount
        audit(conn, license_id, "devices_reset", f"removed={count}")
        conn.commit()
    finally: conn.close()
    return redirect_message(f"Device bindings reset ({count} removed).")

@app.get("/admin/diagnostics")
def admin_diagnostics():
    if not logged_in(): return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id,license_id,device_id,outcome,message,created_at FROM license_activation_events ORDER BY id DESC LIMIT 500")
            events = cur.fetchall()
        html = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Activation diagnostics</title></head><body style="font:14px Arial;padding:20px"><h1>Activation diagnostics</h1><p><a href="/admin">Back to admin</a></p><div style="overflow:auto"><table border="1" cellpadding="8" cellspacing="0"><tr><th>Time (UTC)</th><th>License ID</th><th>Device ID</th><th>Outcome</th><th>Message</th></tr>{% for x in events %}<tr><td>{{x.created_at}}</td><td>{{x.license_id or "—"}}</td><td>{{x.device_id or "—"}}</td><td>{{x.outcome}}</td><td>{{x.message}}</td></tr>{% endfor %}</table></div></body></html>"""
        return render_template_string(html, events=events)
    finally: conn.close()

@app.post("/admin/notify-expiring")
def admin_notify_expiring():
    if not logged_in(): return redirect(url_for("admin_login"))
    import os, smtplib
    from email.message import EmailMessage
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", username).strip()
    if not host or not sender:
        return redirect_message("Email alerts are not configured. Set SMTP_HOST, SMTP_FROM, SMTP_USERNAME and SMTP_PASSWORD in Render.")
    conn = connect()
    sent = 0
    skipped = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id,customer_name,customer_email,expires_at FROM licenses WHERE status='active' AND expires_at >= NOW() AND expires_at < NOW()+INTERVAL '30 days' ORDER BY expires_at")
            rows = cur.fetchall()
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as server:
            server.starttls()
            if username: server.login(username, password)
            for row in rows:
                if not row.get("customer_email"):
                    skipped += 1
                    continue
                msg = EmailMessage()
                msg["Subject"] = "Finance application license expiry reminder"
                msg["From"] = sender
                msg["To"] = row["customer_email"]
                msg.set_content("Hello " + row["customer_name"] + ",\\n\\nYour license (ID " + str(row["id"]) + ") expires on " + row["expires_at"].date().isoformat() + ". Please contact your administrator to renew it.\\n")
                server.send_message(msg)
                sent += 1
        audit(conn, None, "expiry_notifications_sent", "sent=" + str(sent) + "; skipped_without_email=" + str(skipped))
        conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception("Expiry notification send failed")
        return redirect_message("Could not send expiry alerts. Check SMTP settings and Render logs.")
    finally: conn.close()
    return redirect_message("Expiry alerts sent: " + str(sent) + "; skipped without email: " + str(skipped) + ".")

@app.get("/admin/audit")
def admin_audit():
    if not logged_in(): return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id,license_id,action,details,created_at FROM license_audit_logs ORDER BY id DESC LIMIT 500")
            logs = cur.fetchall()
        return render_template_string(AUDIT_HTML, logs=logs)
    finally: conn.close()

@app.get("/admin/export.csv")
def admin_export_csv():
    if not logged_in(): return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT l.id,l.customer_name,l.customer_email,l.status,l.max_devices,l.expires_at,l.created_at,l.notes,
                           (SELECT COUNT(*) FROM devices d WHERE d.license_id=l.id) AS device_count,
                           (SELECT COALESCE(SUM(p.amount),0) FROM license_payments p WHERE p.license_id=l.id) AS payments_total
                           FROM licenses l ORDER BY l.id""")
            rows = cur.fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id","customer_name","customer_email","status","max_devices","expires_at","created_at","notes","device_count","payments_total"])
        for row in rows:
            writer.writerow([row.get(k) for k in ["id","customer_name","customer_email","status","max_devices","expires_at","created_at","notes","device_count","payments_total"]])
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition":"attachment; filename=license-report.csv"})
    finally: conn.close()

@app.get("/admin/backup.json")
def admin_backup():
    if not logged_in(): return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id,license_hash,customer_name,customer_email,max_devices,status,expires_at,created_at,notes FROM licenses ORDER BY id")
            licenses = cur.fetchall()
            cur.execute("SELECT id,license_id,device_id,created_at,last_seen FROM devices ORDER BY id")
            devices = cur.fetchall()
            cur.execute("SELECT id,license_id,amount,payment_date,method,reference,notes,created_at FROM license_payments ORDER BY id")
            payments = cur.fetchall()
        from datetime import date
        def encode(value):
            if isinstance(value, (datetime, date)): return value.isoformat()
            if isinstance(value, Decimal): return str(value)
            raise TypeError(f"Unsupported type: {type(value).__name__}")
        payload = {"created_at":datetime.now(timezone.utc).isoformat(),"warning":"Contains license hashes and customer/payment metadata. Store securely; plaintext license keys cannot be recovered.","licenses":licenses,"devices":devices,"payments":payments}
        return Response(json.dumps(payload,default=encode,indent=2),mimetype="application/json",headers={"Content-Disposition":"attachment; filename=license-backup.json","Cache-Control":"no-store"})
    finally: conn.close()
