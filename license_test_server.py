import base64
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from cryptography.hazmat.primitives import serialization

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
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def load_private_key():
    pem = os.getenv("LICENSE_PRIVATE_KEY_PEM", "").replace("\\n", "\n").strip()
    if not pem:
        raise RuntimeError("LICENSE_PRIVATE_KEY_PEM is not configured.")
    return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)


def make_token(license_row, device_id, app_version):
    payload = {
        "license_id": license_row["id"],
        "customer_name": license_row["customer_name"],
        "device_id": device_id,
        "app_version": app_version or "1.0.0",
        "status": "active",
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    signature = load_private_key().sign(encoded.encode())
    sig = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded}.{sig}"


@app.post("/admin/create")
def create_license():
    if request.headers.get("X-License-Admin-Key", "") != ADMIN_KEY:
        return jsonify(success=False, message="Unauthorized"), 401

    data = request.get_json(silent=True) or {}
    customer_name = str(data.get("customer_name", "")).strip()
    try:
        max_devices = int(data.get("max_devices", 1) or 1)
    except (TypeError, ValueError):
        return jsonify(success=False, message="max_devices must be an integer"), 400

    if not customer_name:
        return jsonify(success=False, message="customer_name is required"), 400
    if max_devices < 1:
        return jsonify(success=False, message="max_devices must be at least 1"), 400

    license_key = "FIN-" + secrets.token_hex(9).upper()
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO licenses (license_hash, customer_name, max_devices, status, created_at) VALUES (?, ?, ?, 'active', ?)",
            (key_hash(license_key), customer_name, max_devices, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return jsonify(success=True, license_id=cur.lastrowid, license_key=license_key,
                       customer_name=customer_name, max_devices=max_devices), 201
    finally:
        conn.close()


@app.post("/api/license/activate")
def activate():
    data = request.get_json(silent=True) or {}
    license_key = str(data.get("license_key", "")).strip()
    device_id = str(data.get("device_id", "")).strip()
    app_version = str(data.get("app_version", "1.0.0")).strip()

    if not license_key or not device_id:
        return jsonify(success=False, message="License key and device ID are required"), 400

    conn = connect()
    try:
        row = conn.execute("SELECT * FROM licenses WHERE license_hash = ?", (key_hash(license_key),)).fetchone()
        if not row:
            return jsonify(success=False, message="Invalid license key"), 403
        if row["status"] != "active":
            return jsonify(success=False, message="License is not active"), 403

        device = conn.execute(
            "SELECT * FROM devices WHERE license_id = ? AND device_id = ?",
            (row["id"], device_id),
        ).fetchone()

        if not device:
            count = conn.execute(
                "SELECT COUNT(*) FROM devices WHERE license_id = ?", (row["id"],)
            ).fetchone()[0]
            if count >= row["max_devices"]:
                return jsonify(success=False, message="This license has reached its device limit"), 403

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO devices (license_id, device_id, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
                (row["id"], device_id, now, now),
            )
        else:
            conn.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), device["id"]),
            )

        conn.commit()
        token = make_token(row, device_id, app_version)
        return jsonify(success=True, message=f"License activated for {row['customer_name']}",
                       activation_token=token)
    finally:
        conn.close()


@app.get("/health")
def health():
    return jsonify(status="ok")


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5050")), debug=False)
