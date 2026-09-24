from flask import Flask, jsonify, request
import hashlib
import os
import secrets
import sqlite3
from datetime import datetime

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "license_test.db")
ADMIN_KEY = os.getenv("LICENSE_ADMIN_KEY", "test-admin-key")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS licenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_hash TEXT UNIQUE NOT NULL,
            customer_name TEXT NOT NULL,
            max_devices INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            expires_at TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            license_id INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(license_id, device_id)
        );
    """)
    conn.commit()
    conn.close()


def key_hash(value):
    return hashlib.sha256(value.strip().encode()).hexdigest()


@app.post("/admin/create")
def create_license():
    if request.headers.get("X-License-Admin-Key", "") != ADMIN_KEY:
        return jsonify(error="Unauthorized"), 401

    data = request.get_json(silent=True) or {}
    customer_name = str(data.get("customer_name", "")).strip()
    max_devices = int(data.get("max_devices", 1) or 1)

    if not customer_name:
        return jsonify(error="customer_name is required"), 400

    license_key = "FIN-" + secrets.token_hex(9).upper()
    now = datetime.utcnow().isoformat()

    conn = connect()
    conn.execute(
        """INSERT INTO licenses
           (license_hash, customer_name, max_devices, created_at)
           VALUES (?, ?, ?, ?)""",
        (key_hash(license_key), customer_name, max_devices, now),
    )
    conn.commit()
    conn.close()

    return jsonify(
        success=True,
        license_key=license_key,
        customer_name=customer_name,
        max_devices=max_devices,
    ), 201


@app.post("/api/license/activate")
def activate():
    data = request.get_json(silent=True) or {}
    license_key = str(data.get("license_key", "")).strip()
    device_id = str(data.get("device_id", "")).strip()

    if not license_key or not device_id:
        return jsonify(success=False, message="License key and device ID are required"), 400

    conn = connect()
    license_row = conn.execute(
        "SELECT * FROM licenses WHERE license_hash = ?",
        (key_hash(license_key),),
    ).fetchone()

    if not license_row:
        conn.close()
        return jsonify(success=False, message="Invalid license key"), 403

    if license_row["status"] != "active":
        conn.close()
        return jsonify(success=False, message="License is not active"), 403

    devices = conn.execute(
        "SELECT device_id FROM devices WHERE license_id = ?",
        (license_row["id"],),
    ).fetchall()

    known = any(row["device_id"] == device_id for row in devices)

    if not known and len(devices) >= license_row["max_devices"]:
        conn.close()
        return jsonify(
            success=False,
            message="This license has reached its device limit",
        ), 403

    now = datetime.utcnow().isoformat()

    if known:
        conn.execute(
            """UPDATE devices SET last_seen_at = ?
               WHERE license_id = ? AND device_id = ?""",
            (now, license_row["id"], device_id),
        )
    else:
        conn.execute(
            """INSERT INTO devices
               (license_id, device_id, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?)""",
            (license_row["id"], device_id, now, now),
        )

    conn.commit()
    conn.close()

    return jsonify(
        success=True,
        message=f"License activated for {license_row['customer_name']}",
    )


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5050, debug=False)
