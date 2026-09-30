from flask import redirect, render_template_string, request, session, url_for
from license_test_server import app, ADMIN_KEY, connect

app.secret_key = app.secret_key if getattr(app, "secret_key", None) else ADMIN_KEY

LOGIN_HTML = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>License Admin Login</title>
<style>
body{font-family:Arial;background:#f4f6f8;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:white;padding:32px;border-radius:16px;box-shadow:0 8px 30px #0002;width:340px}
input,button{width:100%;box-sizing:border-box;padding:12px;margin-top:12px;border-radius:8px}
input{border:1px solid #ddd}button{border:0;background:#111827;color:white;cursor:pointer}
.err{color:#b91c1c;margin-top:12px}
</style></head><body><div class="card">
<h2>Finance License Admin</h2><p>Administrator Login</p>
<form method="post"><input name="admin_key" type="password" placeholder="Admin key" required>
<button type="submit">Login</button></form>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
</div></body></html>
"""

ADMIN_HTML = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Finance License Admin</title>
<style>
body{font-family:Arial;background:#f4f6f8;margin:0;padding:24px;color:#111827}
.top{display:flex;justify-content:space-between;align-items:center}
.card{background:white;padding:20px;border-radius:16px;box-shadow:0 4px 18px #0001;margin:18px 0}
input,button{padding:10px;margin:4px;border-radius:7px}input{border:1px solid #ddd}
button{border:0;cursor:pointer}.primary{background:#111827;color:white}.danger{background:#dc2626;color:white}.warn{background:#d97706;color:white}
table{width:100%;border-collapse:collapse;background:white}th,td{padding:10px;border-bottom:1px solid #e5e7eb;text-align:left}
.badge{padding:4px 8px;border-radius:6px;background:#dcfce7}.revoked{background:#fee2e2}
.msg{padding:12px;background:#dcfce7;border-radius:8px;margin-bottom:15px}
</style></head><body>
<div class="top"><h1>Finance License Admin</h1><a href="/admin/logout">Logout</a></div>
{% if message %}<div class="msg">{{ message }}</div>{% endif %}
<div class="card"><h2>Create License</h2>
<form method="post" action="/admin/create-web">
<input name="customer_name" placeholder="Customer name" required>
<input name="max_devices" type="number" min="1" value="1" required>
<button class="primary" type="submit">Create License</button>
</form></div>
<div class="card"><h2>Licenses</h2>
<table><tr><th>ID</th><th>Customer</th><th>Status</th><th>Devices</th><th>Created</th><th>Actions</th></tr>
{% for x in licenses %}
<tr><td>{{x["id"]}}</td><td>{{x["customer_name"]}}</td>
<td><span class="badge {% if x["status"] != "active" %}revoked{% endif %}">{{x["status"]}}</span></td>
<td>{{x["device_count"]}} / {{x["max_devices"]}}</td><td>{{x["created_at"]}}</td>
<td>{% if x["status"] == "active" %}
<form style="display:inline" method="post" action="/admin/license/{{x["id"]}}/reset-device"><button class="warn">Reset Device</button></form>
<form style="display:inline" method="post" action="/admin/license/{{x["id"]}}/revoke"><button class="danger">Revoke</button></form>
{% endif %}</td></tr>
{% endfor %}</table></div></body></html>
"""

def logged_in():
    return session.get("license_admin") is True

@app.get("/")
def admin_home():
    return redirect(url_for("admin_login"))

@app.get("/admin/login")
def admin_login():
    if logged_in():
        return redirect(url_for("admin_panel"))
    return render_template_string(LOGIN_HTML, error=None)

@app.post("/admin/login")
def admin_login_submit():
    if request.form.get("admin_key", "") != ADMIN_KEY:
        return render_template_string(LOGIN_HTML, error="Invalid admin key"), 401
    session["license_admin"] = True
    return redirect(url_for("admin_panel"))

@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.get("/admin")
def admin_panel():
    if not logged_in():
        return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT l.*, COUNT(d.id) AS device_count
                FROM licenses l
                LEFT JOIN devices d ON d.license_id = l.id
                GROUP BY l.id
                ORDER BY l.id DESC
            """)
            rows = cur.fetchall()
        return render_template_string(
            ADMIN_HTML,
            licenses=rows,
            message=request.args.get("message", ""),
        )
    finally:
        conn.close()

@app.post("/admin/create-web")
def admin_create_web():
    if not logged_in():
        return redirect(url_for("admin_login"))

    customer_name = request.form.get("customer_name", "").strip()
    try:
        max_devices = int(request.form.get("max_devices", "1"))
    except ValueError:
        max_devices = 1

    if not customer_name or max_devices < 1:
        return redirect(url_for("admin_panel", message="Invalid customer name or device limit."))

    import secrets
    from datetime import datetime, timezone
    from license_test_server import key_hash

    license_key = "FIN-" + secrets.token_hex(9).upper()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO licenses
                   (license_hash, customer_name, max_devices, status, created_at)
                   VALUES (%s, %s, %s, 'active', %s)""",
                (key_hash(license_key), customer_name, max_devices, datetime.now(timezone.utc)),
            )
        conn.commit()
    finally:
        conn.close()

    return redirect(
        url_for(
            "admin_panel",
            message=f"License created: {license_key} — copy this key now; only its hash is stored.",
        )
    )

@app.post("/admin/license/<int:license_id>/revoke")
def admin_revoke(license_id):
    if not logged_in():
        return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE licenses SET status='revoked' WHERE id=%s", (license_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin_panel", message="License revoked."))

@app.post("/admin/license/<int:license_id>/reset-device")
def admin_reset_device(license_id):
    if not logged_in():
        return redirect(url_for("admin_login"))
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM devices WHERE license_id=%s", (license_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for("admin_panel", message="Device binding reset."))
