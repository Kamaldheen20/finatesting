import os
import sys
import threading
import time

import webview
from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    env_path = os.path.join(os.path.dirname(sys.executable), ".env")
else:
    env_path = ".env"
load_dotenv(env_path)

from license_client import verify_license, verify_local_activation, save_local_activation

webview.settings["ALLOW_DOWNLOADS"] = True


def run_server():
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    activated, _ = verify_local_activation()

    if not activated:
        license_key = os.getenv("FINANCE_LICENSE_KEY", "").strip()
        if not license_key:
            import tkinter as tk
            from tkinter import simpledialog
            root = tk.Tk()
            root.withdraw()
            license_key = simpledialog.askstring(
                "License Activation",
                "Enter your Finance Collection System license key:",
            ) or ""
            root.destroy()

        if not license_key:
            raise SystemExit("A license key is required.")

        ok, message, token = verify_license(license_key)
        if not ok:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("License Activation", message)
            root.destroy()
            raise SystemExit(1)

        save_local_activation(token)

    from app import app

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(2)

    webview.create_window(
        title="Finance Collection Management System",
        url="http://127.0.0.1:5000",
        width=1400,
        height=850,
        min_size=(1100, 700),
        resizable=True,
    )
    webview.start()
