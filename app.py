# ==========================
# IMPORTS
# ==========================
from datetime import datetime
from sqlalchemy.exc import IntegrityError
import re
import os
import secrets
import logging
from dotenv import load_dotenv
import io

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_file,
    session,
    jsonify
)


from flask_login import (
    LoginManager,
    login_user,
    login_required,
    logout_user,
    current_user
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

from openpyxl import Workbook
from models import (
    db,
    Admin,
    Customer,
    Payment,
    PendingCustomer
)

# ==========================
# APP CONFIG
# ==========================

import sys

# ---------------------------------------------------------------------
# FIX: locate .env correctly whether running as a normal script or as
# a PyInstaller-built .exe.
#
# - Normal `python app.py`: base dir = folder containing this file.
# - Frozen .exe (PyInstaller): sys.frozen is True and sys.executable
#   points at the .exe itself. We use the folder the .exe lives in
#   (NOT sys._MEIPASS, which is a temp extraction folder that gets
#   deleted after the app closes and isn't where you'd want users
#   editing credentials anyway).
#
# This means: ship a `.env` file in the SAME FOLDER as the built
# .exe (e.g. dist/YourApp/.env), and users can edit DB credentials
# there without rebuilding the exe.
# ---------------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------
# CUSTOMER-SPECIFIC DESKTOP BUILD DATABASE
#
# For the public GitHub source, leave this value EMPTY.
# For a customer-specific EXE, make a PRIVATE local copy of this file
# and put that customer's Supabase PostgreSQL URL in EMBEDDED_DATABASE_URL
# before running PyInstaller.
#
# Example (PRIVATE BUILD ONLY):
# EMBEDDED_DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DATABASE"
#
# Never commit a real customer DATABASE_URL to GitHub.
# ---------------------------------------------------------------------
EMBEDDED_DATABASE_URL = ""

# .env is still supported for development/Render. In a customer EXE,
# the embedded URL takes priority, so the customer does not need a .env.
env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(env_path)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------
# FIX (Security): the old fallback was a fixed, publicly-known string
# ("change-this-secret-key"). If SECRET_KEY was ever left unset, every
# session cookie in production would be signed with a secret an
# attacker could read straight out of this source file, letting them
# forge login sessions. We now fall back to a random key instead, and
# log a loud warning so the missing .env value gets noticed. Sessions
# just won't survive an app restart until SECRET_KEY is set properly -
# no worse than before, but no longer a static, guessable secret.
# ---------------------------------------------------------------------
_secret_key = os.getenv("SECRET_KEY")
if not _secret_key:
    _secret_key = secrets.token_hex(32)
    logger.warning(
        "SECRET_KEY not set in .env - using a random key for this run. "
        "Sessions will be invalidated on every restart. Set SECRET_KEY in "
        "your .env file for stable, secure sessions."
    )
app.config["SECRET_KEY"] = _secret_key

# Harden session cookies for production (safe no-ops for local/dev use).
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# NOTE: defaults to False because the PyInstaller desktop build serves
# the UI over plain http://127.0.0.1 (no TLS) - a Secure cookie would
# silently break login there. Set SESSION_COOKIE_SECURE=true in the
# Render .env (HTTPS-only deployment) to enable it there.
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
# Cap upload size (restore_database accepts file uploads) to prevent
# large-file / decompression-bomb style denial-of-service uploads.
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB

# Customer EXE builds use the embedded URL.
# Development/Render builds continue to use DATABASE_URL from the environment.
database_url = EMBEDDED_DATABASE_URL.strip() or os.getenv("DATABASE_URL")

if not database_url:
    # Fail loudly with a clear, actionable message instead of letting
    # Flask-SQLAlchemy raise its generic "SQLALCHEMY_DATABASE_URI must
    # be set" RuntimeError with no context about WHY it's missing.
    error_msg = (
        f"DATABASE_URL not configured.\\n\\n"
        f"For development/Render, set DATABASE_URL in .env/environment.\\n"
        f"For a customer EXE, put that customer's database URL in\\n"
        f"EMBEDDED_DATABASE_URL in app.py before building the EXE."
    )
    if getattr(sys, "frozen", False):
        # Show a native Windows message box since there's no console
        # attached to a windowed PyInstaller build to print to.
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, error_msg, "Configuration Error", 0x10)
        sys.exit(1)
    else:
        raise RuntimeError(error_msg)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url

# Recycle stale Supabase/PostgreSQL SSL connections and validate pooled
# connections before reuse. This prevents intermittent "bad record mac"
# errors after Render workers have been idle.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
    "pool_timeout": 30,
    "pool_size": 2,
    "max_overflow": 1,
}

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

login_manager = LoginManager()

login_manager.init_app(app)

login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    # FIX: a tampered/stale session cookie could contain a non-numeric
    # user_id. int() on that previously raised an uncaught ValueError,
    # turning EVERY request (any page load) into a 500 error until the
    # cookie was manually cleared. Fail safe -> treat as logged-out.
    try:
        return db.session.get(Admin, int(user_id))
    except (TypeError, ValueError):
        return None

# ==========================
# COMPANY SETTINGS MODEL
# ==========================


from utils.pdf_helpers import _register_pdf_fonts
from routes.auth import register as register_auth
from routes.dashboard import register as register_dashboard
from routes.customers import register as register_customers
from routes.collections import register as register_collections
from routes.pending import register as register_pending
from routes.reports import register as register_reports
from routes.exports import register as register_exports
from routes.settings import register as register_settings

register_auth(app)
register_dashboard(app)
register_customers(app)
register_collections(app)
register_pending(app)
register_reports(app)
register_exports(app)
register_settings(app)

try:
    _register_pdf_fonts()
except Exception:
    pass

with app.app_context():
    db.create_all()

    admin = Admin.query.filter_by(username="admin").first()

    if not admin:
        admin = Admin(
            username="admin",
            mobile="9999999999",
            password=generate_password_hash("admin123")
        )

        db.session.add(admin)
        try:
            db.session.commit()
            logger.info("Default admin account created (username: admin).")
        except IntegrityError:
            # FIX (Deployment): this module runs once per gunicorn worker
            # process on Render. With more than one worker, two workers
            # can both see "no admin yet" and both try to insert the same
            # row at startup; the loser previously crashed the whole
            # worker with an unhandled IntegrityError instead of just
            # noticing the other worker already created it.
            db.session.rollback()


        # ==========================
        # RUN APP
        # ==========================

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False
    )
