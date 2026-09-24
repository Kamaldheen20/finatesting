import hashlib
import json
import os
import platform
import urllib.error
import urllib.request
import uuid

LICENSE_SERVER_URL = os.getenv(
    "LICENSE_SERVER_URL",
    "https://finatesting-ac6r.onrender.com"
).rstrip("/")


def get_device_id():
    """Create a stable device identifier without using private hardware secrets."""
    raw = "|".join([
        platform.system(),
        platform.machine(),
        platform.node(),
        str(uuid.getnode()),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_license(license_key, timeout=15):
    payload = json.dumps({
        "license_key": license_key.strip(),
        "device_id": get_device_id(),
        "app_version": "1.0.0",
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
        return False, f"License server unavailable: {exc}"

    return bool(data.get("success")), data.get(
        "message", "License verification failed."
    )
