import base64
import hashlib
import json
import os
import platform
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

APP_VERSION = "1.0.0"
PUBLIC_KEY_FILE = "license_public_key.pem"

_default_license_server = (
    "https://finatesting-ac6r.onrender.com"
    if getattr(__import__("sys"), "frozen", False)
    else "http://127.0.0.1:5050"
)
LICENSE_SERVER_URL = os.getenv("LICENSE_SERVER_URL", _default_license_server).rstrip("/")


def _base_dir():
    if getattr(__import__("sys"), "frozen", False):
        return os.path.dirname(__import__("sys").executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_device_id():
    raw = "|".join([
        platform.system(),
        platform.machine(),
        platform.node(),
        str(uuid.getnode()),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _verify_signed_token(token):
    try:
        encoded, signature = token.split(".", 1)
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        with open(os.path.join(_base_dir(), PUBLIC_KEY_FILE), "rb") as f:
            public_key = serialization.load_pem_public_key(f.read())
        public_key.verify(
            base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
            encoded.encode("utf-8"),
        )
        if payload.get("status") != "active":
            return False
        if payload.get("device_id") != get_device_id():
            return False
        if payload.get("app_version") != APP_VERSION:
            return False
        return True
    except (ValueError, KeyError, OSError, InvalidSignature, json.JSONDecodeError):
        return False


def get_activation_path():
    return os.path.join(_base_dir(), "activation.dat")


def save_local_activation(token):
    data = {"activation_token": token}
    with open(get_activation_path(), "w", encoding="utf-8") as f:
        json.dump(data, f)


def verify_local_activation():
    try:
        with open(get_activation_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        token = data.get("activation_token", "")
        return _verify_signed_token(token), "Local activation verified." if _verify_signed_token(token) else "Activation is invalid."
    except (OSError, json.JSONDecodeError):
        return False, "No local activation found."


def verify_license(license_key, timeout=15):
    payload = json.dumps({
        "license_key": license_key.strip(),
        "device_id": get_device_id(),
        "app_version": APP_VERSION,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{LICENSE_SERVER_URL}/api/license/activate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"License server unavailable: {exc}", None

    if not data.get("success"):
        return False, data.get("message", "License verification failed."), None

    token = data.get("activation_token", "")
    if not token or not _verify_signed_token(token):
        return False, "License server returned an invalid activation token.", None

    return True, data.get("message", "License activated."), token
